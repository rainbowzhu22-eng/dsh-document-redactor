"""Bounded, offline directed tests for the approved 1.4.0 scope."""
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('redactor_runtime', ROOT / 'runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
sys.path.insert(0, str(ROOT / 'core/scripts'))
import contract_pdf_project as P
import contract_sensitive_detector as D


class InstallationCompatibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='redactor140-install-')
        self.root = Path(self.temp.name)
        self.package = ROOT

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_environment_is_reused_without_install(self):
        with patch.object(runtime, '_environment_valid', return_value=True), \
                patch.object(runtime.subprocess, 'run') as run, patch.object(runtime, '_progress'):
            runtime.prepare_environment(self.root, self.package, 'tuna')
        run.assert_not_called()

    def test_failed_install_preserves_files_and_retry_repairs_without_clear(self):
        env = self.root / 'environment'
        env.mkdir()
        marker = env / 'installed-package.txt'
        marker.write_text('preserve')
        with patch.object(runtime, '_environment_valid', return_value=False), \
                patch.object(runtime.subprocess, 'run', side_effect=[None, None, subprocess.CalledProcessError(1, 'pip')]), \
                patch.object(runtime, '_progress'):
            with self.assertRaises(subprocess.CalledProcessError):
                runtime.prepare_environment(self.root, self.package)
        self.assertEqual(marker.read_text(), 'preserve')
        with patch.object(runtime, '_environment_valid', side_effect=[False, True]), \
                patch.object(runtime.subprocess, 'run') as run, patch.object(runtime, '_progress'):
            runtime.prepare_environment(self.root, self.package, 'tuna')
        self.assertTrue(all('--clear' not in c.args[0] for c in run.call_args_list))
        pip = run.call_args_list[-1].args[0]
        self.assertIn(runtime.DOWNLOAD_SOURCES['tuna'], pip)
        self.assertEqual(pip[pip.index('--timeout') + 1], '60')
        self.assertEqual(marker.read_text(), 'preserve')

    def test_failed_post_install_validation_is_not_success(self):
        with patch.object(runtime, '_environment_valid', return_value=False), \
                patch.object(runtime.subprocess, 'run'), patch.object(runtime, '_progress') as progress:
            with self.assertRaisesRegex(RuntimeError, 'ENVIRONMENT_VALIDATION_FAILED'):
                runtime.prepare_environment(self.root, self.package)
        self.assertNotIn(unittest.mock.call('ready'), progress.call_args_list)

    def test_windows_self_check_includes_ocr_models_and_all_projections(self):
        python = self.root / 'python.exe'
        python.touch()
        with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run:
            self.assertFalse(runtime._environment_valid(python, self.package / 'core', True))
        command = run.call_args.args[0][-1]
        self.assertIn('_rapid_engine()', command)
        for module in ('onnxruntime', 'media.ocr', 'graphics.imaging', 'foundation.collections', 'storage.streams'):
            self.assertIn(module, command)

    def test_requirements_decode_as_utf8_under_legacy_gbk_fallback(self):
        raw = (self.package / 'core/scripts/requirements.txt').read_bytes()
        first_lines = b'\n'.join(raw.splitlines()[:2])
        self.assertIn(b'coding: utf-8', first_lines)
        text = raw.decode('utf-8')
        self.assertIn('rapidocr==3.9.2', text)
        self.assertEqual(len([x for x in text.splitlines() if x.strip() and not x.startswith('#')]), 15)

    def test_unapproved_download_source_rejected_before_environment_changes(self):
        with patch.object(runtime.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'INVALID_DOWNLOAD_SOURCE'):
                runtime.prepare_environment(self.root, self.package, 'http://untrusted.invalid')
        run.assert_not_called()


class PdfStructureCompatibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='redactor140-pdf-')
        self.root = Path(self.temp.name)
        self.sid = '123456abcdef'
        self.snapshot = D.build_rules_snapshot(str(ROOT / 'core/references/脱敏规则清单.md'))

    def tearDown(self):
        self.temp.cleanup()

    def sample(self, broken=False):
        from PIL import Image
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject
        images = [Image.new('RGB', (300, 400), c) for c in ('white', 'lightgray')]
        raw = io.BytesIO()
        images[0].save(raw, 'PDF', save_all=True, append_images=images[1:])
        writer = PdfWriter()
        for page in PdfReader(raw).pages:
            writer.add_page(page)
        ref = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Annot'),
            NameObject('/Subtype'): NameObject('/Link'), NameObject('/Rect'): ArrayObject([NumberObject(v) for v in (10, 10, 30, 30)])}))
        writer.pages[0][NameObject('/Annots')] = ArrayObject([ref])
        writer.add_metadata({'/Author': 'SYNTHETIC-PRIVATE-METADATA'})
        out = io.BytesIO()
        writer.write(out)
        data = out.getvalue()
        if broken:
            match = re.search(rb'(?m)^' + str(ref.idnum).encode() + rb' 0 obj\n(.*?)\nendobj', data, re.DOTALL)
            data = data[:match.start(1)] + b'trux' + b' ' * (len(match.group(1)) - 4) + data[match.end(1):]
        path = self.root / ('broken.pdf' if broken else 'normal.pdf')
        path.write_bytes(data)
        return path

    def create(self, broken=False):
        return P.create(self.root / 'sessions', self.sid, self.sample(broken), 'synthetic.pdf', rules_snapshot=self.snapshot)

    def process_and_review(self):
        root = self.root / 'sessions'
        with patch.object(P, '_ocr', return_value=[]):
            P.process(root, self.sid)
        self.assertEqual(P.load(root, self.sid)['stage'], 'review')
        for page in (1, 2):
            for request in ({'action':'region-add', 'bbox':[10, 10, 50, 50]}, {'action':'review', 'manual_confirm':True}):
                P.update(root, self.sid, dict(request, page=page, revision=P.load(root, self.sid)['revision']))
        return root

    def test_normal_annotations_count_remains_known(self):
        project = self.create()
        self.assertEqual(project['link_annotation_count'], 1)
        self.assertEqual(project['link_annotation_status'], 'complete')

    def test_malformed_annotation_reaches_review_and_pixel_export(self):
        from pypdf import PdfReader
        import pypdfium2 as pdfium
        project = self.create(True)
        self.assertIsNone(project['link_annotation_count'])
        self.assertEqual(project['link_annotation_status'], 'unknown')
        root = self.process_and_review()
        result = P.export(root, self.sid, confirmed=True)
        self.assertIn('数量未知', '\n'.join(result['notes']))
        output = root / self.sid / 'out' / result['name']
        reader = PdfReader(output)
        self.assertEqual(len(reader.pages), 2)
        self.assertEqual(P._count_link_annotations(output), 0)
        self.assertNotIn(b'SYNTHETIC-PRIVATE-METADATA', output.read_bytes())
        self.assertFalse(any(page.extract_text() or page.get('/Annots') for page in reader.pages))
        with pdfium.PdfDocument(output) as pdf:
            page = pdf[0]
            self.assertEqual(page.get_size(), (300.0, 400.0))
            bitmap = page.render(scale=1)
            image = bitmap.to_pil().convert('RGB')
            self.assertTrue(all(v < 15 for v in image.getpixel((30, 30))))
            image.close(); bitmap.close(); page.close()

    def test_text_parser_failure_falls_back_to_image_review(self):
        self.create()
        with patch.object(D, 'extract_pdf', side_effect=RuntimeError('synthetic-parser-failure')):
            self.process_and_review()
        self.assertTrue(P.load(self.root / 'sessions', self.sid)['text_parse_fallback'])

    def test_output_validation_remains_strict(self):
        self.create(True)
        root = self.process_and_review()
        with patch.object(P, '_count_link_annotations', return_value=1):
            with self.assertRaisesRegex(ValueError, '仍检测到可点击链接'):
                P.export(root, self.sid, confirmed=True)
        self.assertFalse((root / self.sid / 'out' / '脱敏文书-001.pdf').exists())

    def test_unreadable_source_does_not_create_success_manifest(self):
        bad = self.root / 'unreadable.pdf'
        bad.write_bytes(b'not a PDF')
        with self.assertRaises(Exception):
            P.create(self.root / 'sessions', self.sid, bad, 'unreadable.pdf')
        self.assertFalse((self.root / 'sessions' / self.sid / 'pdf-project.json').exists())


class RuleReloadCompatibility(unittest.TestCase):
    def test_custom_rule_regex_survives_repeated_txt_and_docx_exports(self):
        import contract_redactor as redactor
        from docx import Document
        original_meta = dict(D.TYPE_META)
        try:
            with tempfile.TemporaryDirectory(prefix='redactor140-rule-reload-') as folder:
                root=Path(folder)
                rules=(ROOT/'core/references/脱敏规则清单.md').read_text()
                anchor=next(line for line in rules.splitlines() if line.startswith('| 35 | ABA_ROUTING'))
                rules=rules.replace(anchor,anchor+'\n| 36 | FY_PUBLIC | 自定义 | 中 | 公开样式 | 替换为代号 | keep_head=0; keep_tail=0 | 是 | FY-PUBLIC-[A-Z]{4} |')
                path=root/'rules.md';path.write_text(rules)
                token='FY-PUBLIC-LUMA'
                (root/'input.txt').write_text(token)
                doc=Document();doc.add_paragraph(token);doc.save(root/'input.docx')
                for index,suffix in enumerate(('txt','docx','docx','txt')):
                    target=root/('out%d.'%index+suffix)
                    result=redactor.redact_file(str(root/('input.'+suffix)),types=['FY_PUBLIC'],out_path=str(target),rules_path=str(path),write_log=False,allow_no_replacements=True)
                    self.assertEqual(result['applied'],1)
                    text=target.read_text() if suffix=='txt' else '\n'.join(p.text for p in Document(target).paragraphs)
                    self.assertNotIn(token,text)
        finally:
            D.TYPE_META.clear();D.TYPE_META.update(original_meta)


class OcrLineReview(unittest.TestCase):
    setUp = PdfStructureCompatibility.setUp
    tearDown = PdfStructureCompatibility.tearDown
    sample = PdfStructureCompatibility.sample
    create = PdfStructureCompatibility.create

    def ready_page(self, lines):
        project = self.create()
        project['stage'] = 'review'
        project['pages'][0].update(stage='ready',ocr='complete')
        root = self.root / 'sessions'
        P.save(root,self.sid,project)
        cache = P._blocks_file(root,self.sid,1).with_suffix('.ocr.json')
        P._atomic(cache,{'lines':lines})
        return root

    def test_all_rows_visible_including_unmatched_low_score_and_invalid_geometry(self):
        root = self.ready_page([
            {'text':'公开说明','confidence':0.99,'geometry':'character','boxes':[[.1,.1,.2,.2]]*4},
            {'text':'低分行','confidence':0.1,'geometry':'line','boxes':[[.1,.3,.5,.4]]*3},
            {'text':'无坐标','confidence':None,'boxes':[]},
            {'text':'无效分值','confidence':float('nan'),'boxes':[]},
            {'text':'<script>用户原文</script>','confidence':0.9,'boxes':[]},
        ])
        view=P.ocr_lines(root,self.sid,1)
        self.assertEqual(len(view['lines']),5)
        self.assertEqual(view['lines'][0]['text'],'公开说明')
        self.assertEqual(view['lines'][0]['bbox'],[30.0,40.0,60.0,80.0])
        self.assertIn('未纳入自动敏感识别',view['lines'][1]['issues'])
        self.assertIn('仅有行级定位，请核对遮盖范围',view['lines'][1]['issues'])
        self.assertIsNone(view['lines'][2]['bbox'])
        self.assertIn('定位精度未标注，请核对遮盖范围',view['lines'][2]['issues'])
        self.assertIsNone(view['lines'][3]['confidence'])
        self.assertIn('<script>',view['lines'][4]['text'])
        json.dumps(view,allow_nan=False)

    def test_missing_legacy_cache_is_explicitly_unavailable(self):
        self.create()
        root=self.root/'sessions'
        p=P.load(root,self.sid);p['pages'][0]['stage']='ready';P.save(root,self.sid,p)
        self.assertFalse(P.ocr_lines(root,self.sid,1)['available'])
        with self.assertRaisesRegex(ValueError,'页码无效'):
            P.ocr_lines(root,self.sid,3)

    def test_ocr_check_is_persistent_metadata_and_does_not_change_export_decisions(self):
        root=self.ready_page([])
        before=P.load(root,self.sid)
        evaluation=P.evaluate_review(before)
        result=P.update(root,self.sid,{'action':'ocr-review','page':1,'revision':before['revision'],'checked':True})
        after=P.load(root,self.sid)
        self.assertTrue(after['pages'][0]['ocr_checked'])
        self.assertEqual(after['revision'],before['revision'])
        self.assertEqual(P.evaluate_review(after),evaluation)
        self.assertFalse(after['pages'][0]['reviewed'])

    def test_reprocessing_invalidates_prior_ocr_check(self):
        root=self.ready_page([])
        p=P.load(root,self.sid);p['pages'][0]['ocr_checked']=True;P.save(root,self.sid,p)
        with patch.object(P,'_ocr',return_value=[]):
            P.process(root,self.sid)
        self.assertFalse(P.load(root,self.sid)['pages'][0]['ocr_checked'])

    def test_ocr_text_endpoint_requires_local_project_header(self):
        import contract_app_server as server
        root=self.ready_page([{'text':'LOCAL-ONLY-TEXT','confidence':1,'boxes':[]}])
        handler=object.__new__(server.AppHandler)
        handler.path='/api/pdf/ocr-lines/'+self.sid+'/1'
        handler.state=SimpleNamespace(work_dir=str(root))
        handler._local_request_allowed=lambda:True
        sent=[];handler._send_json=lambda value,code=200:sent.append((code,value))
        handler.headers={};handler.do_GET();self.assertEqual(sent[-1][0],403)
        handler.headers={'X-Local-Project':'1'};handler.do_GET()
        self.assertEqual(sent[-1][0],200)
        self.assertEqual(sent[-1][1]['lines'][0]['text'],'LOCAL-ONLY-TEXT')

    def test_generated_review_script_parses_and_uses_safe_text_nodes(self):
        from contract_pdf_ui import page
        html=page(self.sid).decode('utf-8')
        script=html.split('<script>',1)[1].split('</script>',1)[0]
        result=subprocess.run(['node','--check'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('document.createTextNode(line.index',script)
        self.assertIn('OCR 人工核对',script)


if __name__ == '__main__':
    unittest.main()
