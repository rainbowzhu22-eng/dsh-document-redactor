"""Windows OCR: use RapidOCR; try Windows OCR only on RapidOCR import errors."""
from __future__ import annotations

import asyncio
import os
import threading
import tempfile
from pathlib import Path
from PIL import Image

_ENGINE = None

# M2（v1.3.0）：本机 OCR 超时保护秒数。macOS 的 pdf_vision_ocr 走 subprocess 有 90s 超时，
# Windows 两条路径都没有超时 —— 个别页面可能把整批处理卡死。此处统一加超时，
# 超时按「本页识别失败」处理（走 H2 的失败页路径，由用户在核对页手工确认/框选）。
OCR_TIMEOUT_SEC = float(os.environ.get("CONTRACT_PDF_OCR_TIMEOUT", "90"))


def _rect(quad):
    xs = [float(point[0]) for point in quad]
    ys = [float(point[1]) for point in quad]
    return min(xs), min(ys), max(xs), max(ys)


def _word_rows(raw):
    if not raw:
        return []
    out = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 3 and isinstance(item[0], str):
            out.append(item)
        elif isinstance(item, (list, tuple)):
            out.extend(_word_rows(item))
    return out


def _rapid_once(path):
    global _ENGINE
    import rapidocr
    from rapidocr import RapidOCR
    if _ENGINE is None:
        model_dir = Path(rapidocr.__file__).resolve().parent / "models"
        expected = ("PP-OCRv6_det_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                    "PP-OCRv6_rec_small.onnx")
        if any(not (model_dir / name).is_file() for name in expected):
            raise RuntimeError("本机 OCR 模型文件不完整；已停止自动识别，请手工框选")
        font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        font = next((str(font_dir / name) for name in
                     ("msyh.ttc", "simsun.ttc", "arial.ttf")
                     if (font_dir / name).is_file()), None)
        if font is None:
            raise RuntimeError("缺少本机 OCR 字体，请手工框选")
        _ENGINE = RapidOCR(params={"Global.font_path": font})
    result = _ENGINE(path, return_word_box=True, return_single_char_box=True)
    if result.boxes is None:
        return []
    with Image.open(path) as image:
        width, height = image.size
    words = _word_rows(result.word_results)
    lines = []
    for quad, text, score in zip(result.boxes, result.txts, result.scores):
        x0, y0, x1, y1 = _rect(quad)
        matched = []
        for word in words:
            try:
                wx0, wy0, wx1, wy1 = _rect(word[2])
            except (TypeError, ValueError, IndexError):
                continue
            cx, cy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
            if x0 - 3 <= cx <= x1 + 3 and y0 - 3 <= cy <= y1 + 3:
                matched.append((wx0, wy0, wx1, wy1, word[0]))
        matched.sort(key=lambda row: (row[0], row[1]))
        boxes = []
        cursor = 0
        reliable = True
        for char in text:
            if char.isspace():
                boxes.append(None)
                continue
            while cursor < len(matched) and not matched[cursor][4]:
                cursor += 1
            if cursor >= len(matched) or matched[cursor][4][0] != char:
                reliable = False
                break
            wx0, wy0, wx1, wy1, label = matched[cursor]
            # A multi-character label has only one box. Never fan that box out
            # across several text characters, even when its prefix happens to match.
            if len(label) != 1:
                reliable = False
                break
            boxes.append([wx0 / width, wy0 / height, wx1 / width, wy1 / height])
            cursor += 1
        if (cursor < len(matched) and any(row[4] for row in matched[cursor:])):
            reliable = False
        if not reliable or len(boxes) != len(text):
            line_box = [x0 / width, y0 / height, x1 / width, y1 / height]
            boxes = [line_box if not c.isspace() else None for c in text]
        lines.append({"text": text, "boxes": boxes,
                      "confidence": float(score),
                      "geometry": "character" if reliable else "line"})
    return lines


def _rapid(path):
    """对低分行局部放大重试，保持原图坐标；每页最多重试六行。"""
    lines = _rapid_once(path)
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    retried = 0
    with tempfile.TemporaryDirectory(prefix="contract-ocr-crop-") as temp:
        for index, line in enumerate(lines):
            if float(line.get("confidence", 0)) >= 0.70:
                continue
            boxes = [b for b in line.get("boxes", []) if b]
            if not boxes or retried >= 6:
                line["retry_status"] = "unavailable"
                continue
            retried += 1
            top = max(0, int(min(b[1] for b in boxes) * height) - 10)
            bottom = min(height, int(max(b[3] for b in boxes) * height) + 11)
            if bottom <= top:
                continue
            crop_path = Path(temp) / f"{index}.png"
            image.crop((0, top, width, bottom)).resize(
                (width * 2, (bottom - top) * 2)).save(crop_path)
            try:
                candidates = _rapid_once(str(crop_path))
                center = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2
                candidates = [row for row in candidates if row.get("boxes") and
                    abs((min(b[1] for b in row["boxes"] if b) +
                         max(b[3] for b in row["boxes"] if b)) / 2 *
                        (bottom - top) + top - center * height) < (bottom - top) / 2]
                best = max(candidates, key=lambda row: row["confidence"], default=None)
                if (best and best.get("geometry") == "character" and
                        best["confidence"] >= 0.70 and
                        best["confidence"] > float(line["confidence"]) + 0.05):
                    best["boxes"] = [
                        [b[0], (top + b[1] * (bottom - top)) / height,
                         b[2], (top + b[3] * (bottom - top)) / height] if b else None
                        for b in best["boxes"]]
                    best["retry_status"] = "improved"
                    best["initial_confidence"] = line["confidence"]
                    lines[index] = best
                else:
                    line["retry_status"] = "not-improved"
            except Exception:
                # 重试失败保留原识别和低分状态，不把明文或异常写入通用日志。
                line["retry_status"] = "failed"
    return lines


async def _winrt(path):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import StorageFile, FileAccessMode
    file = await StorageFile.get_file_from_path_async(os.fspath(path))
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows 缺少本机 OCR 语言包")
    result = await engine.recognize_async(bitmap)
    width, height = bitmap.pixel_width, bitmap.pixel_height
    lines = []
    for line in result.lines:
        text = ""
        boxes = []
        for word in line.words:
            if text:
                text += " "
                boxes.append(None)
            rect = word.bounding_rect
            box = [rect.x / width, rect.y / height,
                   (rect.x + rect.width) / width, (rect.y + rect.height) / height]
            text += word.text
            boxes.extend([box] * len(word.text))
        if text:
            lines.append({"text": text, "boxes": boxes,
                          "confidence": None, "geometry": "word"})
    return lines


def _recognize_inner(path):
    try:
        return _rapid(path)
    except ImportError:
        try:
            return asyncio.run(_winrt(path))
        except (ImportError, RuntimeError) as exc:
            raise RuntimeError("本机 OCR 不可用，请手工框选并逐页核对") from exc


def recognize(path):
    """同步识别入口，带超时保护（M2）。

    超时后不等待后台线程（daemon），直接按「本页识别失败」抛出 —— 由上游把该页置为
    failed，用户在核对页人工确认或手工框选，而不是整批卡死。
    """
    box = {}

    def _worker():
        try:
            box["lines"] = _recognize_inner(path)
        except BaseException as exc:          # noqa: BLE001 — 需把异常带回主线程
            box["error"] = exc

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(OCR_TIMEOUT_SEC)
    if th.is_alive():
        raise RuntimeError("本机 OCR 超时（>%ds），请手工框选并逐页核对" % int(OCR_TIMEOUT_SEC))
    if "error" in box:
        raise box["error"]
    return box.get("lines") or []
