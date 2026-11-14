"""Bounded regression checks for narrative text swallowed by organisation spans."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core/scripts"))
import contract_sensitive_detector as D
import contract_pdf_project as P

NAME = "星河（北京）科技有限公司"
PREFIX = "投资方之关联方"


def scan(text):
    doc = D.Document("fictional.txt", "txt", [D.Block(text)])
    return D.DetectorEngine(D.Settings(enabled_only={"ORG_NAME"}, retain_low_confidence=True)).scan_document(doc)


def pdf_candidates():
    text = PREFIX + NAME + "应履行协议。"
    block = D.Block(text, page=1, word_boxes=[D.WordBox(i, i + 1, i * 12, 20, (i + 1) * 12, 32)
                                             for i in range(len(text))])
    block.ocr_geometry = "character"
    doc = D.Document("fictional.pdf", "pdf", [block])
    found, _, _ = P._suggestions(doc, "contract", [], pages=[dict(page=1, width=600, height=80)])
    return [x for x in found if x["type"] == "ORG_NAME" and x["value"] == NAME]


class OrgNarrativeScope(unittest.TestCase):
    def test_01_role_phrase_stays_outside_single_line_name(self):
        for prefix in (PREFIX, "投资方的关联方"):
            with self.subTest(prefix=prefix):
                found = scan(prefix + NAME + "应履行协议。")
                self.assertIn(NAME, [x.value for x in found])
                self.assertFalse(any("投资方" in x.value for x in found))

    def test_02_wrapped_name_keeps_only_its_two_source_spans(self):
        text = PREFIX + "星河（北京）\n科技有限公司应履行协议。"
        found = next(x for x in scan(text) if x.value == NAME)
        self.assertEqual(min(x.start for x in found.occurrences), len(PREFIX))
        self.assertEqual("".join(text[x.start:x.end] for x in found.occurrences), NAME)

    def test_03_legitimate_brand_words_are_not_global_boundaries(self):
        for name in ("之江科技有限公司", "投资方科技有限公司", "关联方科技有限公司", "投资方之星科技有限公司"):
            with self.subTest(name=name):
                self.assertIn(name, [x.value for x in scan(name + "应履行协议。")])

    def test_04_alias_and_later_full_name_remain_detectable(self):
        found = scan(PREFIX + NAME + "（以下简称“星河科技”）。星河科技与" + NAME + "履行协议。")
        full = next(x for x in found if x.value == NAME)
        self.assertGreaterEqual(len(full.occurrences), 2)
        self.assertIn("星河科技", [x.value for x in found])
        self.assertFalse(any("星河科技与" in x.value for x in found))

    def test_07_other_known_name_and_wrapped_conjunction(self):
        other = "晨光（上海）软件有限公司"
        text = (NAME + "（以下简称“星河科技”）。" + other + "（以下简称“晨光软件”）。"
                "星河科技与晨光（上海）\n软件有限公司共同履约。")
        found = scan(text)
        full = next(x for x in found if x.value == other)
        self.assertGreaterEqual(len(full.occurrences), 3)
        self.assertFalse(any("星河科技与" in x.value for x in found))
        joins = {i for i, char in enumerate(text) if char == "与"}
        self.assertFalse(any(o.start <= i < o.end for c in found for o in c.occurrences for i in joins))

    def test_08_declared_name_can_contain_a_conjunction(self):
        other = "晨光软件有限公司"
        joint = "星河科技与" + other
        text = (NAME + "（以下简称“星河科技”）。" + other + "（以下简称“晨光软件”）。"
                + joint + "（以下简称“联合公司”）。" + joint + "履约。知与行科技有限公司履约。")
        values = [x.value for x in scan(text)]
        self.assertIn(joint, values)
        self.assertIn("知与行科技有限公司", values)

    def test_05_pdf_range_starts_at_the_name(self):
        found = pdf_candidates()
        self.assertTrue(found)
        self.assertEqual(found[0]["bbox"][0], len(PREFIX) * 12)
        self.assertEqual(found[0]["bbox"][2], (len(PREFIX) + len(NAME)) * 12)

    def test_06_pixel_replacement_keeps_the_narrative_body(self):
        found = pdf_candidates()
        self.assertTrue(found)
        image = Image.new("RGB", (1200, 160), "white")
        draw = ImageDraw.Draw(image)
        prefix_box = (0, 40, (len(PREFIX) * 12 - 4) * 2, 64)
        draw.rectangle(prefix_box, fill=(10, 80, 150))
        name_box = (len(PREFIX) * 24, 40, (len(PREFIX) + len(NAME)) * 24 - 1, 63)
        draw.rectangle(name_box, fill=(220, 20, 20))
        before = image.crop(prefix_box).tobytes()
        P._redact_image(image, found, [])
        self.assertEqual(image.crop(prefix_box).tobytes(), before)
        self.assertNotIn((220, 20, 20), set(image.crop(name_box).getdata()))

    def test_09_coarse_box_containing_body_cannot_auto_apply(self):
        text = PREFIX + NAME + "应当履约。"
        block = D.Block(text, page=1, word_boxes=[D.WordBox(0, len(text), 0, 20, len(text) * 12, 32)])
        block.ocr_geometry = "word"
        doc = D.Document("fictional.pdf", "pdf", [block])
        page = dict(page=1, width=600, height=80, reviewed=False)
        found, _, _ = P._suggestions(doc, "contract", [], pages=[page])
        target = next(x for x in found if x["type"] == "ORG_NAME")
        self.assertTrue(target["scope_review_required"])
        self.assertFalse(target["machine_selected"])
        self.assertEqual(P._candidate_render_plan(target, page)["mode"], "none")
        p = dict(stage="review", revision=0, pages=[page], candidates=[target], regions=[],
                 unlocated_candidates=[], coverage_links=[], review_schema=2, type_defaults={})
        P._auto_disposition(p)
        self.assertFalse(P.evaluate_review(p)["can_export"])

    def test_10_review_page_script_remains_valid(self):
        import contract_pdf_ui as UI
        html = UI.page("abcdef123456").decode("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "review.js"
            path.write_text(script)
            result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            handler = script.split("canvas.onpointerdown=", 1)[1].split("\ncanvas.onpointermove=", 1)[0]
            import json
            check = Path(raw) / "pointer.cjs"
            check.write_text("""
const vm=require('node:vm'),assert=require('node:assert/strict');
const calls=[];
const box={id:'existing',page:1,bbox:[0,0,20,20]};
const ctx={canvas:{setPointerCapture(){}},p:{stage:'review',regions:[box],candidates:[box]},
 cur:1,pos:()=>[10,10],candidateBoxes:a=>[a.bbox],reviewState:()=> 'redact',
 change:(...args)=>calls.push(args),activeCandidateRef:{collection:'candidates',id:'coarse'},
 activeUnlocatedId:null,activeRegion:null,base:null,start:null,drawMode:'',drag:null,zoom:1};
vm.runInNewContext('canvas.onpointerdown='+HANDLER,ctx);
ctx.canvas.onpointerdown({pointerId:1});
assert.equal(ctx.drawMode,'add');assert.equal(calls.length,0);
ctx.activeCandidateRef=null;ctx.p.regions=[];ctx.drawMode='';ctx.drag=null;
ctx.canvas.onpointerdown({pointerId:2});
assert.equal(calls.length,1);assert.equal(calls[0][0],'candidate');
ctx.p.regions=[box];ctx.canvas.onpointerdown({pointerId:3});
assert.equal(ctx.drawMode,'move');
""".replace("HANDLER", json.dumps(handler)))
            result = subprocess.run(["node", str(check)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
