"""Bounded DOCX compatibility regression via the actual export/restore engine."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from docx import Document
from lxml import etree as E

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'package/core' if (ROOT / 'package/core').is_dir() else ROOT / 'core'
sys.path.insert(0, str(CORE / 'scripts'))
import contract_review_ext as R
import contract_redactor as X
import contract_sensitive_detector as D

W = R._W
TOKEN = 'CONTROL-PRIVATE-LUMA'


def control(content, kind='richText'):
    node = E.Element(W + 'sdt')
    props = E.SubElement(node, W + 'sdtPr')
    if kind:
        E.SubElement(props, W + kind)
    E.SubElement(node, W + 'sdtContent').append(content)
    return node


class ContentControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='redaction-controls-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def fixture(self, kind='richText', location='body', table=False, nested=False):
        doc = Document()
        if location == 'header':
            doc.sections[0].header.paragraphs[0].text = TOKEN
        elif table:
            doc.add_table(rows=1, cols=1).cell(0, 0).text = TOKEN
        else:
            doc.add_paragraph(TOKEN)
        path = self.work / 'source.docx'
        doc.save(path)
        part = 'word/header1.xml' if location == 'header' else 'word/document.xml'
        with zipfile.ZipFile(path) as package:
            data = {n: package.read(n) for n in package.namelist()}
        root = E.fromstring(data[part])
        item = root.find('.//' + W + ('tbl' if table else 'p'))
        parent = item.getparent(); index = parent.index(item); parent.remove(item)
        wrapper = control(item, kind)
        if nested:
            wrapper = control(wrapper, 'docPartObj')
        parent.insert(index, wrapper)
        data[part] = E.tostring(root)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as package:
            for name, value in data.items():
                package.writestr(name, value)
        return path, part, root

    def rewrite_part(self, path, part, root):
        with zipfile.ZipFile(path) as package:
            data = {n: package.read(n) for n in package.namelist()}
        data[part] = E.tostring(root)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as package:
            for name, value in data.items():
                package.writestr(name, value)

    def export(self, path):
        return X.redact_file(
            str(path),
            selection={'enabled_types': [R._custom_type('测试字段')]},
            custom_terms=[{'label': '测试字段', 'values': [TOKEN]}],
            out_path=str(self.work / 'redacted.docx'), external_clean=True,
            restore_mode=True, write_log=False)

    def roundtrip(self, path):
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.export(path)
        self.assertEqual(result['applied'], 1)
        redacted = D.load_document(result['output'], include_headers=True)
        self.assertNotIn(TOKEN, '\n'.join(b.text for b in redacted.blocks))
        restored = self.work / 'restored.docx'
        X.restore_file(result['output'], result['mapping'], str(restored), write_log=False)
        restored_doc = D.load_document(str(restored), include_headers=True)
        self.assertIn(TOKEN, '\n'.join(b.text for b in restored_doc.blocks))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source_hash)
        return result

    def test_static_control_types_export_and_restore(self):
        for kind in ('text', 'richText', 'date', 'docPartObj', 'dropDownList', None):
            with self.subTest(kind=kind):
                path, _, _ = self.fixture(kind)
                self.roundtrip(path)

    def test_table_and_nested_controls_keep_layout_and_restore(self):
        path, _, _ = self.fixture(table=True, nested=True)
        result = self.roundtrip(path)
        with zipfile.ZipFile(result['output']) as package:
            root = E.fromstring(package.read('word/document.xml'))
        self.assertEqual(len(list(root.iter(W + 'tbl'))), 1)
        self.assertEqual(len(list(root.iter(W + 'sdt'))), 2)
        self.assertEqual(len(list(root.iter(W + 'richText'))), 2)

    def test_header_control_export_and_restore(self):
        path, _, _ = self.fixture('date', location='header')
        self.roundtrip(path)

    def test_date_cache_and_dropdown_values_cannot_repopulate_original(self):
        for kind in ('date', 'dropDownList'):
            with self.subTest(kind=kind):
                path, part, root = self.fixture(kind)
                props = root.find('.//' + W + 'sdtPr')
                typed = props.find(W + kind)
                if kind == 'date':
                    typed.set(W + 'fullDate', TOKEN)
                else:
                    item = E.SubElement(typed, W + 'listItem')
                    item.set(W + 'displayText', TOKEN); item.set(W + 'value', TOKEN)
                alias = E.SubElement(props, W + 'alias'); alias.set(W + 'val', TOKEN)
                self.rewrite_part(path, part, root)
                result = self.roundtrip(path)
                with zipfile.ZipFile(result['output']) as package:
                    self.assertNotIn(TOKEN.encode(), package.read(part))

    def test_unsupported_binding_relationship_and_unknown_stay_blocked(self):
        for kind in ('dataBinding', 'unknownControl', 'relation'):
            with self.subTest(kind=kind):
                path, part, root = self.fixture()
                props = root.find('.//' + W + 'sdtPr')
                if kind == 'relation':
                    props.set(R._R + 'id', 'rIdUnsafe')
                else:
                    E.SubElement(props, W + kind)
                self.rewrite_part(path, part, root)
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, '第1个内容控件') as caught:
                    self.export(path)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertFalse((self.work / 'redacted.docx').exists())
                self.assertEqual(before, path.read_bytes())

    def test_mixed_supported_control_classification_is_independent(self):
        _, _, root = self.fixture()
        bad = control(E.Element(W + 'p'), 'dataBinding')
        root.find(W + 'body').insert(1, bad)
        rows = R.classify_content_controls('word/document.xml', root)
        self.assertEqual([r['classification'] for r in rows], ['safe_text', 'unsupported'])
        self.assertIn('数据绑定', rows[1]['reason'])

    def test_duplicate_content_and_hidden_unknown_property_are_rejected(self):
        for mode in ('duplicate', 'unknown'):
            _, _, root = self.fixture()
            node = next(root.iter(W + 'sdt'))
            if mode == 'duplicate':
                E.SubElement(node, W + 'sdtContent')
            else:
                rpr = E.SubElement(node.find(W + 'sdtPr'), W + 'rPr')
                E.SubElement(rpr, W + 'secretCache').text = TOKEN
            self.assertEqual(R.classify_content_controls('word/document.xml', root)[0]['classification'], 'unsupported')

    def test_control_structure_digest_detects_mutation(self):
        _, _, root = self.fixture('date')
        before = R._content_control_structure_digest(root)
        next(root.iter(W + 'date')).set(W + 'fullDate', '2026-11-17')
        self.assertNotEqual(before, R._content_control_structure_digest(root))


if __name__ == '__main__':
    unittest.main()
