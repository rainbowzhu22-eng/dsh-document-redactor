"""Local image candidates, adapted from 楼军有's 2026-11-12 contribution.

Detectors only propose regions. The PDF review engine owns confirmation and export.
No source-file writes, filename heuristics, OCR subprocesses or separate environment.
"""
import hashlib
import math

LABELS = {"SEAL_IMAGE": "印章", "PHOTO_IMAGE": "证件照片",
          "SIGNATURE_IMAGE": "签名／手写字迹", "FINGERPRINT_IMAGE": "红手印",
          "IDENTITY_IMAGE": "身份证", "CASE_FRAGMENT_IMAGE": "跨页案号片段"}


def case_edges(lines, width, height):
    import contract_pixel_extra as extra
    edges = {"top": [], "bottom": []}
    for line in lines:
        text = str(line.get("text", "")).strip()
        if not text or len(text) > 48:
            continue
        box = extra.line_bbox(line, width, height)
        if not box:
            continue
        if box[1] < height * 0.20:
            edges["top"].append([text, box])
        if box[3] > height * 0.75:
            edges["bottom"].append([text, box])
    return edges


def cross_page_cases(pages, pattern):
    import re
    output = []
    for first, second in zip(pages, pages[1:]):
        if second["page"] != first["page"] + 1:
            continue
        for year, year_box in first.get("pixel_case_edges", {}).get("bottom", []):
            if not re.fullmatch(r"[（(]\s*(?:19|20)\d{2}\s*[)）]", year):
                continue
            for body, body_box in second.get("pixel_case_edges", {}).get("top", []):
                if re.fullmatch(pattern, year + body):
                    for state, box in ((first, year_box), (second, body_box)):
                        output.append(candidate("CASE_FRAGMENT_IMAGE", box, state["page"], state["width"], state["height"]))
    return list({item["id"]: item for item in output}.values())


def _box(raw, width, height):
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("图像候选坐标无效")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in raw):
        raise ValueError("图像候选坐标无效")
    x0, y0, x1, y1 = raw
    result = [max(0, x0 - 3), max(0, y0 - 3), min(width, x1 + 3), min(height, y1 + 3)]
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError("图像候选坐标越界")
    return result


def candidate(kind, raw, page, width, height):
    box = _box(raw, width, height)
    label = LABELS[kind]
    ident = hashlib.sha256(repr((kind, page, box)).encode()).hexdigest()[:20]
    value = ("身份证页候选（整页遮盖；需要保留其他内容时请手工框选）"
             if kind == "IDENTITY_IMAGE" else label + "候选（请核对完整范围）")
    return {"id": "pixel-" + ident, "page": page, "type": kind, "label": label,
            "value": value, "placeholder": "[" + label + "]",
            "bbox": box, "bboxes": [box], "geometry": "region", "replaceable": False,
            "source": "pixel", "confidence": 0.7, "entity_confidence": 0.7,
            "ocr_confidence": None, "machine_selected": False, "selected": False,
            "selection_reason": "image-region-needs-confirmation"}


def detect(path, lines, page, width, height):
    # Imports are lazy so a missing dependency becomes an explicit page error.
    import contract_pixel_seals as seals
    import contract_pixel_extra as extra
    from PIL import Image
    with Image.open(path) as image:
        pixel_width, pixel_height = image.size
    sx, sy = width / pixel_width, height / pixel_height
    found = seals.detect(str(path))
    scale = found["scale"]
    regions = [("SEAL_IMAGE", [v / scale for v in item["bbox"]])
               for item in found["seals"]]
    result = extra.detect(str(path), pixel_width, pixel_height, lines)
    for key, kind in (("photos", "PHOTO_IMAGE"), ("prints", "FINGERPRINT_IMAGE"),
                      ("handwriting", "SIGNATURE_IMAGE")):
        for item in result[key]:
            box = item["bbox"]
            if key == "prints" and any(
                    seal_kind == "SEAL_IMAGE" and
                    max(0, min(box[2], seal_box[2]) - max(box[0], seal_box[0])) *
                    max(0, min(box[3], seal_box[3]) - max(box[1], seal_box[1])) >=
                    0.80 * (box[2] - box[0]) * (box[3] - box[1])
                    for seal_kind, seal_box in regions):
                continue
            regions.append((kind, box))
    for item in result["signs"]:
        x0, y0, x1, y1 = item["mask"]
        padding = 1.3 * max(1, y1 - y0)
        regions.append(("SIGNATURE_IMAGE", [x0, max(0, y0 - padding), x1,
                                            min(pixel_height, y1 + padding)]))
    # Identity documents are recognized from content, never from the filename.
    text = " ".join(str(line.get("text", "")) for line in lines)
    identity = ("居民身份证" in text or
                ("公民身份号码" in text and sum(word in text for word in ("姓名", "出生", "住址", "民族")) >= 2))
    if identity:
        # A whole-card/page candidate is safer than promising field-only precision.
        # Users can adjust it with the existing manual-region tool before export.
        regions.append(("IDENTITY_IMAGE", [0, 0, pixel_width, pixel_height]))
    output, seen = [], set()
    for kind, box in regions:
        normalized = [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy]
        item = candidate(kind, normalized, page, width, height)
        if item["id"] not in seen:
            output.append(item)
            seen.add(item["id"])
    return output
