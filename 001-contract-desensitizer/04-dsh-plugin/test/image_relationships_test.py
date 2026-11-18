"""Small formal-export regressions for supported VML image relationship aliases."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from docx import Document
from lxml import etree as E
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'package/core' if (ROOT / 'package/core').is_dir() else ROOT / 'core'
sys.path.insert(0, str(CORE / 'scripts'))
import contract_review_ext as R
import contract_redactor as X
import contract_sensitive_detector as D

W, V, O = R._W, R._V, R._O
NAME = '星河（北京）科技有限公司'


class ImageRelationships(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='redaction-image-rel-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def fixture(self, kind, header=False):
        path = self.base / 'source.docx'
        image = io.BytesIO(); Image.new('RGB', (4, 4), 'white').save(image, format='PNG'); image.seek(0)
        doc = Document(); doc.add_paragraph(NAME)
        owner = doc.sections[0].header if header else doc
        owner.add_paragraph().add_run().add_picture(image)
        doc.save(path)
        part = 'word/header1.xml' if header else 'word/document.xml'
        with zipfile.ZipFile(path) as z:
            data = {n: z.read(n) for n in z.namelist()}
        root = E.fromstring(data[part]); drawing = next(root.iter(W + 'drawing'))
        rid = next(drawing.iter(R._A + 'blip')).get(R._R + 'embed')
        if kind != 'drawing':
            pict = E.Element(W + 'pict'); shape = E.SubElement(pict, V + 'shape')
            shape.set('style', 'width:4pt;height:4pt')
            image_node = E.SubElement(shape, V + 'imagedata')
            if kind not in ('missing',):
                image_node.set(O + 'relid' if kind != 'rid' else R._R + 'id', rid)
            if kind in ('matching', 'conflict'):
                image_node.set(R._R + 'id', rid if kind == 'matching' else 'rIdWrong')
            if kind == 'unknown':
                image_node.set(O + 'relid', 'rIdAbsent')
            drawing.getparent().replace(drawing, pict)
        data[part] = E.tostring(root)
        relpart = R._relationship_part(part)
        if kind == 'external':
            rels = E.fromstring(data[relpart])
            rel = next(n for n in rels if n.get('Id') == rid)
            rel.set('TargetMode', 'External'); rel.set('Target', 'https://example.invalid/never-fetch.png')
            data[relpart] = E.tostring(rels)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            for n, b in data.items(): z.writestr(n, b)
        return path, data

    def export(self, path):
        return X.redact_file(str(path), types=['ORG_NAME'], restore_mode=True,
                             external_clean=True, out_path=str(self.base / 'out.docx'), write_log=False)

    def test_supported_aliases_keep_image_bytes_and_restore_selected_text(self):
        for kind in ('drawing', 'rid', 'relid', 'matching'):
            with self.subTest(kind=kind):
                path, source = self.fixture(kind); original = path.read_bytes()
                result = self.export(path); self.assertGreater(result['applied'], 0)
                self.assertNotIn(NAME, '\n'.join(b.text for b in D.load_document(result['output']).blocks))
                with zipfile.ZipFile(result['output']) as z:
                    media = [n for n in source if n.startswith('word/media/')]
                    self.assertTrue(media)
                    for n in media: self.assertEqual(z.read(n), source[n])
                    self.assertIn(b'/image', z.read('word/_rels/document.xml.rels'))
                restored = self.base / 'restored.docx'
                X.restore_file(result['output'], result['mapping'], str(restored), write_log=False)
                self.assertIn(NAME, '\n'.join(b.text for b in D.load_document(str(restored)).blocks))
                self.assertEqual(path.read_bytes(), original)

    def test_legacy_header_image_is_preserved(self):
        path, source = self.fixture('relid', header=True); result = self.export(path)
        with zipfile.ZipFile(result['output']) as z:
            self.assertIn(b'/image', z.read('word/_rels/header1.xml.rels'))
            for n in source:
                if n.startswith('word/media/'): self.assertEqual(z.read(n), source[n])

    def test_missing_or_unknown_references_still_fail_with_position(self):
        for kind in ('missing', 'unknown'):
            with self.subTest(kind=kind):
                path, _ = self.fixture(kind); before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'word/document.xml第1个图片'):
                    self.export(path)
                self.assertEqual(before, path.read_bytes()); self.assertFalse((self.base/'out.docx').exists())

    def test_conflicting_aliases_cannot_select_a_different_image(self):
        path, _ = self.fixture('conflict')
        with self.assertRaisesRegex(ValueError, '互相冲突'):
            self.export(path)
        self.assertFalse((self.base/'out.docx').exists())

    def test_removed_external_legacy_reference_leaves_no_dangling_reference(self):
        path, _ = self.fixture('external'); result = self.export(path)
        with zipfile.ZipFile(result['output']) as z:
            self.assertNotIn(b'relid=', z.read('word/document.xml'))
            self.assertNotIn(b'example.invalid', z.read('word/_rels/document.xml.rels'))


if __name__ == '__main__':
    unittest.main()
