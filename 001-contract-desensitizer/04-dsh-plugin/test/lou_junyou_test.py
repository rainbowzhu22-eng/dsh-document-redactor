"""Eight directed checks for 楼军有's image-redaction feedback."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core/scripts"))
import contract_sensitive_detector as D
import contract_pdf_project as P
import contract_pdf_ui as UI
import contract_pixel_regions as Pixels
import contract_pixel_seals as Seals
import contract_pixel_extra as Extra


def project(candidates=None, **page_fields):
    page = dict(page=1, width=600, height=800, stage="ready", reviewed=False,
                ocr="complete", revision=0, content_revision=0)
    page.update(page_fields)
    return dict(sid="abcdef123456", kind="pdf-v1.3", stage="review", revision=0,
                pages=[page], candidates=candidates or [], unlocated_candidates=[],
                regions=[], coverage_links=[], review_schema=2, type_defaults={},
                type_choices={}, policy_id="preserve_transaction", numbers=[], terms=[])


class LouJunyouRepair(unittest.TestCase):
    def test_01_spaced_case_numbers_and_boundaries(self):
        regex = re.compile(D.LITIGATION_CASE_PATTERN)
        for text in ("（2025）浙 0109 破申 155 号", "（2025）浙0109民初155号",
                     "(2025)最高法民终123号", "（2025）沪 01 执恢 8 号"):
            self.assertIsNotNone(regex.fullmatch(text), text)
        for text in ("（2025）年度第三号", "（2025）年度第3号", "（2025）浙0109其他155号"):
            self.assertIsNone(regex.search(text), text)
        self.assertEqual(regex.search("（2025）浙0109民初155号送达编号1号").group(), "（2025）浙0109民初155号")

    def test_02_seal_feedback_and_unmarked_sample(self):
        with tempfile.TemporaryDirectory() as raw:
            image = Image.new("RGB", (1200, 1600), "white")
            draw = ImageDraw.Draw(image)
            draw.ellipse((750, 1000, 1050, 1300), outline=(230, 65, 70), width=8)
            draw.line((800, 1100, 1000, 1200), fill=(230, 65, 70), width=12)
            path = Path(raw) / "seal.png"
            image.save(path)
            self.assertTrue(Seals.detect(str(path))["seals"])
        actual = os.environ.get("LOU_JUNYOU_REVIEW_IMAGE")
        if actual:
            from hashlib import sha256
            before = sha256(Path(actual).read_bytes()).hexdigest()
            found = Pixels.detect(actual, [], 1, 716, 405)
            seals = [x for x in found if x["type"] == "SEAL_IMAGE"]
            self.assertTrue(seals)
            image = Image.open(actual).convert("RGB")
            original_band = image.crop((0, 0, 1400, 160)).tobytes()
            P._redact_image(image, seals, [])
            pixels = np.asarray(image.crop((765, 345, 1127, 700)))
            self.assertEqual(int(pixels.max()), 0)
            self.assertEqual(original_band, image.crop((0, 0, 1400, 160)).tobytes())
            self.assertEqual(before, sha256(Path(actual).read_bytes()).hexdigest())

    def test_03_isolated_handwriting_scale_and_ocr_exclusion(self):
        image = Image.new("RGB", (400, 400), "white")
        ImageDraw.Draw(image).line([(80, 300), (100, 320), (120, 300), (140, 320)], fill="black", width=5)
        a = np.asarray(image).astype(np.float32)
        result = Extra.handwriting_boxes(a, [], 800, 800, 0.5)
        self.assertTrue(result)
        self.assertGreater(result[0]["bbox"][1], 580)
        lines = [{"text": "字", "boxes": [[0.19, 0.74, 0.36, 0.82]]}]
        self.assertFalse(Extra.handwriting_boxes(a, lines, 800, 800, 0.5))

    def test_04_prints_signatures_and_identity_enter_review(self):
        result = dict(photos=[], prints=[dict(bbox=[100, 100, 220, 220])],
                      handwriting=[dict(bbox=[300, 400, 400, 440])],
                      signs=[dict(mask=[300, 500, 600, 520])])
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "ordinary.jpg"
            Image.new("RGB", (1000, 1400), "white").save(path)
            with patch.object(Seals, "detect", return_value=dict(scale=1, seals=[])), \
                 patch.object(Extra, "detect", return_value=result):
                found = Pixels.detect(path, [{"text": "居民身份证"}], 1, 500, 700)
                self.assertEqual({x["type"] for x in found}, {"FINGERPRINT_IMAGE", "SIGNATURE_IMAGE", "IDENTITY_IMAGE"})
                signature = next(x for x in found if x["type"] == "SIGNATURE_IMAGE" and x["bbox"][1] > 230)
                self.assertGreater(signature["bbox"][3], 270)  # 下伸笔画必须纳入遮盖。
                self.assertFalse(any(x["type"] == "IDENTITY_IMAGE" for x in Pixels.detect(path, [], 1, 500, 700)))
            with patch.object(Seals, "detect", return_value=dict(scale=1, seals=[dict(kind="red", bbox=[95, 95, 225, 225])])), \
                 patch.object(Extra, "detect", return_value=result):
                found = Pixels.detect(path, [], 1, 500, 700)
                self.assertFalse(any(x["type"] == "FINGERPRINT_IMAGE" for x in found))
            self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")

    def test_05_image_candidates_need_disposition_and_survive_rescan(self):
        item = Pixels.candidate("SEAL_IMAGE", [100, 100, 200, 200], 1, 600, 800)
        p = project([item])
        P._auto_disposition(p)
        self.assertEqual(P.evaluate_review(p)["pending_occurrences"], 1)
        self.assertFalse(P.evaluate_review(p)["can_export"])
        item["user_choice"] = "redact"
        self.assertTrue(P.evaluate_review(p)["can_export"])
        renewed = Pixels.candidate("SEAL_IMAGE", [100, 100, 200, 200], 1, 600, 800)
        P._preserve_review_decisions([renewed], [], p)
        self.assertEqual(renewed["user_choice"], "redact")
        item["user_choice"] = "keep"
        self.assertEqual(P.evaluate_review(p)["states"][0]["state"], "keep")

    def test_06_failure_requires_current_manual_review(self):
        p = project(pixel_detection="failed", pixel_error="synthetic error")
        ev = P.evaluate_review(p)
        self.assertFalse(ev["can_export"])
        self.assertEqual(ev["hard_issues"][0]["code"], "PDF_IMAGE_DETECTION_FAILED")
        p["pages"][0].update(manual_confirm=True, reviewed=True, reviewed_content_revision=0)
        self.assertTrue(P.evaluate_review(p)["can_export"])
        p["pages"][0]["content_revision"] += 1
        self.assertFalse(P.evaluate_review(p)["can_export"])
        with self.assertRaisesRegex(ValueError, "坐标"):
            Pixels.candidate("SEAL_IMAGE", [0, float("nan"), 10, 10], 1, 600, 800)

    def test_07_cross_page_year_requires_a_matching_case_body(self):
        pages = [dict(page=1, width=600, height=800, pixel_case_edges={"bottom": [["（2025）", [50, 760, 100, 780]]]}),
                 dict(page=2, width=600, height=800, pixel_case_edges={"top": [["浙0109破申155号", [50, 20, 250, 40]]]})]
        self.assertEqual(len(Pixels.cross_page_cases(pages, D.LITIGATION_CASE_PATTERN)), 2)
        pages[1]["pixel_case_edges"]["top"][0][0] = "年度第三号"
        self.assertFalse(Pixels.cross_page_cases(pages, D.LITIGATION_CASE_PATTERN))

    def test_08_review_ui_script_and_runtime_dependency_gate(self):
        html = UI.page("abcdef123456").decode("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "review.js"
            path.write_text(script)
            run = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("整块遮盖", html)
        self.assertIn("imageReviewed", html)
        spec = importlib.util.spec_from_file_location("runtime_lou", ROOT / "runtime.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        with patch.object(runtime.subprocess, "run") as run:
            runtime._environment_valid(Path(sys.executable), ROOT / "core", False)
        self.assertIn("numpy", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
