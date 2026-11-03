"""Directed source tests for the four returned-package blockers; no host/model/build."""
import copy
from email.message import Message
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core/scripts'))
import contract_pdf_project as P
import contract_app_server as S
import contract_sensitive_detector as D
import contract_docx_revisions as X
import contract_redaction_policy as Policy


class ReturnedBlockerRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix='redaction-blockers-')
        cls.base = Path(cls.sandbox.name)
        cls.rules = cls.base / 'rules.md'
        cls.rules_text = (ROOT / 'core/references/脱敏规则清单.md').read_text()
        lines = cls.rules_text.splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith('| 35 | ABA_ROUTING')) + 1
        lines.insert(at, '| 36 | DIY_BATCH | 自定义 | 中 | 批号 | 替换为代号 | keep_head=0; keep_tail=0 | 是 | Batch:\\s*AB-\\d{6} |')
        cls.rules_text = '\n'.join(lines) + '\n'
        cls.rules.write_text(cls.rules_text)
        cls.snapshot = D.build_rules_snapshot(str(cls.rules))
        assert any(x.type_id == 'DIY_BATCH' for x in cls.snapshot['extra_specs'])
        cls.sid = 'abcd1234abcd'
        cls.reference = cls.base / 'reference'
        P.create(cls.reference, cls.sid, ROOT / 'test/fixtures/fictional-batch.pdf',
                 'fictional.pdf', policy_id=Policy.PRIVACY_FIRST,
                 policy_selection=Policy.PRIVACY_FIRST, rules_snapshot=cls.snapshot)
        P.process(cls.reference, cls.sid)
        cls.number_source = ROOT / 'test/fixtures/fictional-number.pdf'
        cls.number_capture = json.loads((ROOT / 'test/fixtures/number-project.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='redaction-repair-case-')
        self.work = Path(self.temp.name)
        self.rules.write_text(self.rules_text)
        self.sessions = self.work / 'sessions'
        shutil.copytree(self.reference, self.sessions)

    def tearDown(self):
        self.temp.cleanup()

    def number_project(self):
        root = self.work / 'number'
        p = P.create(root, self.sid, self.number_source, 'fictional-number.pdf',
                     policy_id=Policy.PRIVACY_FIRST, policy_selection=Policy.PRIVACY_FIRST,
                     rules_snapshot=self.snapshot)
        p['candidates'] = copy.deepcopy(self.number_capture['candidates'])
        p['stage'], p['done'] = 'review', 1
        for page in p['pages']:
            page.update(stage='ready', reviewed=True, ocr='text', ocr_supplement='empty')
        P.save(root, self.sid, p)
        return root, P.load(root, self.sid)

    def pending(self, project):
        return P.evaluate_review(project)['pending_occurrences']

    def request_export(self, root, confirm):
        raw = json.dumps({'confirm': confirm, 'revision': P.load(root, self.sid)['revision']}).encode()
        handler = object.__new__(S.AppHandler)
        handler.path = '/api/pdf/export/' + self.sid
        handler.headers = Message()
        for name, value in {'Content-Type': 'application/json', 'Content-Length': str(len(raw)), 'X-Local-Project': '1'}.items():
            handler.headers[name] = value
        handler.rfile = io.BytesIO(raw)
        handler.connection = SimpleNamespace(gettimeout=lambda: None, settimeout=lambda _: None)
        handler.state = SimpleNamespace(work_dir=str(root))
        handler._local_request_allowed = lambda **_: True
        sent = []
        handler._send_json = lambda value, code=200: sent.append((code, value))
        with patch.object(S, 'user_rules_path', return_value=str(self.rules)):
            handler.do_POST()
        return sent[-1]

    def test_01_pdf_custom_rule_snapshot_survives_reopen_add_term_and_case_sync(self):
        p = P.load(self.sessions, self.sid)
        self.assertEqual(p['stage'], 'review', p.get('error'))
        self.assertTrue(any(c['type'] == 'DIY_BATCH' for c in p['candidates']))
        self.assertEqual(p['rules_sha256'], self.snapshot['sha256'])
        self.rules.write_text(self.rules_text + '\nchanged only live rules\n')
        P.add_term(self.sessions, self.sid, {'label': '备注', 'values': ['Fictional document'], 'scope': 'document'})
        P.sync_case(self.sessions, self.sid, [], [])
        p = P.load(self.sessions, self.sid)
        self.assertTrue(any(c['type'] == 'DIY_BATCH' for c in p['candidates']))
        self.assertEqual(P._rules_snapshot(self.sessions, self.sid, p)['sha256'], self.snapshot['sha256'])

    def test_02_tampered_pdf_rule_snapshot_cannot_be_used_or_exported(self):
        path = self.sessions / self.sid / 'private/rules.md'
        path.write_text(path.read_text() + '\nmodified snapshot\n')
        with self.assertRaisesRegex(ValueError, '规则快照'):
            P._rules_snapshot(self.sessions, self.sid, P.load(self.sessions, self.sid))
        with self.assertRaisesRegex(ValueError, '规则快照'):
            P.export(self.sessions, self.sid, confirmed=True)

    def test_03_export_handler_blocks_changed_missing_and_legacy_rules_even_with_confirm(self):
        with patch.object(P, 'export', return_value={'name': 'fictional.pdf'}) as export:
            self.assertEqual(self.request_export(self.sessions, False)[0], 200)
            self.assertEqual(export.call_count, 1)
            self.rules.write_text(self.rules_text + '\nchanged\n')
            for confirm in (False, True):
                code, data = self.request_export(self.sessions, confirm)
                self.assertEqual((code, data.get('code')), (409, 'RULES_CHANGED'))
            self.rules.unlink()
            self.assertEqual(self.request_export(self.sessions, True)[0], 409)
            self.rules.write_text(self.rules_text)
            p = P.load(self.sessions, self.sid)
            p.pop('rules_sha256')
            P.save(self.sessions, self.sid, p)
            self.assertEqual(self.request_export(self.sessions, True)[0], 409)
            self.assertEqual(export.call_count, 1, 'blocked requests must never enter export')

    def test_04_untouched_number_and_returned_saved_defaults_remain_blocked(self):
        root, p = self.number_project()
        self.assertGreater(self.pending(p), 0)
        self.assertNotEqual(P._effective_candidate_state(p, p['candidates'][0])['state'], 'type-default-keep')
        for confirmed in (False, True):
            with self.assertRaises(P.PdfDispositionRequired):
                P.export(root, self.sid, confirmed=confirmed)
        p['stage'] = 'exported'  # A previously exported return must not gain a keep decision.
        p.pop('type_choices', None)
        P.save(root, self.sid, p)
        self.assertGreater(self.pending(P.load(root, self.sid)), 0)
        code, data = self.request_export(root, True)
        self.assertEqual((code, data.get('code')), (400, 'PDF_DISPOSITION_REQUIRED'))

    def test_05_explicit_keep_or_cover_allows_export_undo_or_delete_blocks_again(self):
        root, p = self.number_project()
        item = p['candidates'][0]
        P.update(root, self.sid, {'action': 'candidate', 'page': 1, 'id': item['id'], 'selected': False, 'revision': p['revision']})
        p = P.load(root, self.sid)
        self.assertEqual(self.pending(p), 0)
        self.assertEqual(P._effective_candidate_state(p, p['candidates'][0])['source'], 'user')
        result = P.export(root, self.sid, revision=p['revision'], confirmed=True)
        self.assertTrue(result['name'].endswith('.pdf'))
        P.update(root, self.sid, {'action': 'undo', 'page': 1, 'revision': P.load(root, self.sid)['revision']})
        p = P.load(root, self.sid)
        self.assertGreater(self.pending(p), 0)
        p['regions'] = [{'id': 'manual-example', 'page': 1, 'bbox': item['bbox'], 'requires_confirmation': False}]
        P.save(root, self.sid, p)
        self.assertEqual(self.pending(P.load(root, self.sid)), 0)
        p['regions'] = []
        P.save(root, self.sid, p)
        with self.assertRaises(P.PdfDispositionRequired):
            P.export(root, self.sid, confirmed=True)

    def test_06_explicit_type_choice_is_persistent_undo_and_reset_restore_pending(self):
        root, p = self.number_project()
        def change(action, **kwargs):
            P.update(root, self.sid, dict(action=action, revision=P.load(root, self.sid)['revision'], **kwargs))
        change('type-default-toggle', type='NUMBER_CANDIDATE', enabled=False)
        self.assertEqual(self.pending(P.load(root, self.sid)), 0)
        change('undo', page=1)
        self.assertGreater(self.pending(P.load(root, self.sid)), 0)
        change('all-keep')
        self.assertEqual(self.pending(P.load(root, self.sid)), 0)
        change('type-default-reset', type='NUMBER_CANDIDATE')
        self.assertGreater(self.pending(P.load(root, self.sid)), 0)
        p = P.create(self.work / 'policy', 'abcd1234abce', self.number_source, 'fictional.pdf',
                     policy_id=Policy.PRESERVE_TRANSACTION, policy_selection=Policy.PRESERVE_TRANSACTION,
                     rules_snapshot=self.snapshot)
        self.assertIs(p['type_choices']['AMOUNT'], False)
        self.assertNotIn('NUMBER_CANDIDATE', p['type_choices'])

    @unittest.skipUnless(sys.platform == 'darwin', 'native Mac OCR')
    def test_07_mac_ocr_executable_and_real_recognition(self):
        helper = ROOT / 'core/scripts/pdf_vision_ocr'
        self.assertTrue(os.access(helper, os.X_OK))
        self.assertEqual(helper.stat().st_mode & 0o777, 0o755)
        image = ROOT / 'test/fixtures/fictional-number.png'
        lines = P._ocr(image)
        self.assertTrue(lines)
        self.assertIn('DOC', ' '.join(line['text'] for line in lines).upper())

    def test_08_word_readable_attribute_rewrite_and_restore_remain_functional(self):
        target = self.work / 'fictional-attributes.docx'
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
               '<w:body><w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="fictional" '
               'descr="虚构甲 联系人" title="虚构甲 案件"/></wp:inline></w:drawing></w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(target, 'w') as z:
            z.writestr('word/document.xml', xml)
        rows = X.read_docx_readable_attribute_targets(str(target))
        self.assertEqual(len(rows), 2)
        operations = [SimpleNamespace(target=dict(row, kind='docx_readable_attribute'),
                       local_start=0, local_end=3, value='虚构甲', replacement='[人员#1]') for row in rows]
        self.assertEqual(X.rewrite_docx_readable_attribute_targets(str(target), operations)['applied'], 2)
        self.assertTrue(all('虚构甲' not in row['value'] for row in X.read_docx_readable_attribute_targets(str(target))))
        mapping = [dict(op.target, original=op.value, order=op.target['output_order']) for op in operations]
        self.assertEqual(X.restore_docx_readable_attribute_targets(str(target), mapping)['applied'], 2)
        self.assertEqual([r['value'] for r in X.read_docx_readable_attribute_targets(str(target))], [r['value'] for r in rows])
        before = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '摘要不一致'):
            X.restore_docx_readable_attribute_targets(str(target), mapping)
        self.assertEqual(before, target.read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
