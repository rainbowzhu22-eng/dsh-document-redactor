"""Persistent, local PDF review projects. All outward PDFs are rebuilt from pixels."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import contract_sensitive_detector as D
import contract_review_ext as R
import contract_redaction_policy as Policy

SCALE = 2.0
PDF_BLOCK_VERSION = 7
OCR_COLUMN_GAP = 18.0
LOCKS = {}
# These thresholds describe separate decisions. Equal initial values do not
# make entity confidence, OCR confidence, and geometry interchangeable.
ENTITY_AUTO_SELECT_THRESHOLD = 0.50
ENTITY_REVIEW_THRESHOLD = 0.50
OCR_AUTO_SELECT_THRESHOLD = 0.70
OCR_REVIEW_THRESHOLD = 0.70
OCR_BOX_THRESHOLD = 0.70
OCR_LINE_ACCEPT_THRESHOLD = 0.35
ENTITY_DISPLAY_MIN_CONFIDENCE = 0.30


def _lock(sid):
    return LOCKS.setdefault(sid, threading.RLock())


def _is_transient_windows_file_lock(exc):
    return os.name == "nt" and getattr(exc, "winerror", None) in (5, 32, 33)


def _atomic(path, data):
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:
                if not _is_transient_windows_file_lock(exc) or attempt == 4:
                    raise
                time.sleep(0.02 * (2 ** attempt))
    finally:
        tmp.unlink(missing_ok=True)


def paths(root, sid):
    if not re.fullmatch(r"[0-9a-f]{12}", sid):
        raise ValueError("无效项目编号")
    base = Path(root) / sid
    return base, base / "pdf-project.json"


def load(root, sid):
    base, manifest = paths(root, sid)
    for attempt in range(5):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            break
        except OSError as exc:
            if not _is_transient_windows_file_lock(exc) or attempt == 4:
                raise
            time.sleep(0.02 * (2 ** attempt))
    if data.get("sid") != sid or data.get("kind") != "pdf-v1.3":
        raise ValueError("项目数据无效")
    data.setdefault("policy_id", Policy.PRESERVE_TRANSACTION)
    data.setdefault("policy_selection", data["policy_id"])
    data.setdefault("unlocated_candidates", [])
    _normalize_review(data)
    return data


def save(root, sid, project):
    _, manifest = paths(root, sid)
    with _lock(sid):
        _atomic(manifest, project)


def original(root, sid, project=None):
    project = project or load(root, sid)
    path = paths(root, sid)[0] / "private" / "original.pdf"
    if not path.is_file():
        raise FileNotFoundError("原始 PDF 不存在，请重新关联")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != project["sha256"]:
        raise ValueError("原始 PDF 校验失败，文件可能已被替换")
    return path


def relink(root, sid, content):
    with _lock(sid):
        project = load(root, sid)
        digest = hashlib.sha256(content).hexdigest()
        if digest != project["sha256"]:
            raise ValueError("重新关联失败：所选文件与原件 SHA-256 不一致")
        target = paths(root, sid)[0] / "private" / "original.pdf"
        temp = target.with_suffix(".pdf.tmp")
        temp.write_bytes(content)
        os.replace(temp, target)
        return project


def _count_link_annotations(path):
    """Count PDF /Link annotations without reading or returning their targets."""
    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=False)
    count = 0
    for page in reader.pages:
        for reference in page.get("/Annots", []) or []:
            annotation = reference.get_object()
            if str(annotation.get("/Subtype", "")) == "/Link":
                count += 1
    return count


def create(root, sid, source, filename, profile="contract", case_name="", terms=None,
           policy_id=Policy.PRESERVE_TRANSACTION, policy_selection=None,
           rules_snapshot=None):
    base, _ = paths(root, sid)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = base / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        # POSIX：收紧为仅本人可读写。Windows 上 os.chmod 只切只读位、不设 ACL，
        # 为避免「0o700 已生效」的错觉，这里显式跳过；Windows 侧依赖
        # %LOCALAPPDATA% 的默认 ACL（本人 + Administrators），不是本代码保证的。
        os.chmod(base, 0o700)
        os.chmod(private, 0o700)
    target = private / "original.pdf"
    shutil.copyfile(source, target)
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    link_annotation_count = _count_link_annotations(target)
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(target))
    if len(pdf) < 1:
        raise ValueError("PDF 没有页面")
    pages = []
    for i in range(len(pdf)):
        page = pdf[i]
        w, h = page.get_size()
        pages.append({"page": i + 1, "width": w, "height": h, "stage": "pending",
                      "reviewed": False, "revision": 0, "error": "", "ocr": "pending",
                      "ocr_supplement": "pending", "block_version": 0,
                      "manual_confirm": False, "dropped_lines": 0,
                      "ocr_line_stats": _empty_ocr_line_stats()})
        page.close()
    pdf.close()
    project = {"kind": "pdf-v1.3", "sid": sid, "filename": filename,
               "sha256": sha, "profile": profile, "case_name": case_name,
               "policy_id": policy_id, "policy_selection": policy_selection or policy_id,
               "link_annotation_count": link_annotation_count,
               "stage": "processing", "done": 0, "cancel": False, "revision": 0,
               "pages": pages, "candidates": [], "regions": [], "terms": terms or [],
               "numbers": [], "scan_stats": {}, "case_rules_pending": False,
               "output": "", "error": "", "undo": []}
    if rules_snapshot is not None:
        # 保存本次上传使用的规则；重试、加词和服务重启均使用同一快照。
        rules_bytes = rules_snapshot["text"].encode("utf-8")
        (private / "rules.md").write_bytes(rules_bytes)
        project["rules_sha256"] = rules_snapshot["sha256"]
        project["rules_snapshot_sha256"] = hashlib.sha256(rules_bytes).hexdigest()
    save(root, sid, project)
    return project


def _ocr(image_path):
    """Return text lines with per-character coordinates in rendered pixels."""
    if platform.system() == "Darwin":
        helper = Path(__file__).with_name("pdf_vision_ocr")
        if not helper.is_file():
            raise RuntimeError("本机识别组件未安装；请手工框选并逐页核对")
        proc = subprocess.run([str(helper), str(image_path)], capture_output=True,
                              text=True, timeout=90, check=False)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "本机文字识别失败")
        return json.loads(proc.stdout)
    if platform.system() == "Windows":
        # Windows native OCR is available only when the matching language pack is present.
        from contract_pdf_win_ocr import recognize
        return recognize(str(image_path))
    raise RuntimeError("当前系统没有本机 OCR；可手工框选")


def _render(pdf, page_index, path):
    page = pdf[page_index]
    bitmap = page.render(scale=SCALE, rotation=0)
    image = bitmap.to_pil().convert("RGB")
    image.save(path, "PNG")
    page.close()
    return image


def _empty_ocr_line_stats():
    return {"empty_text": 0, "box_count_mismatch": 0, "invalid_confidence": 0,
            "low_confidence": 0, "confidence_unavailable": 0,
            "incomplete_geometry": 0}


def _ocr_blocks(lines, page_no, width, height, stats=None):
    blocks = []
    stats = stats if stats is not None else {}
    for key in _empty_ocr_line_stats():
        stats.setdefault(key, 0)
    for line in lines:
        txt = str(line.get("text") or "")
        boxes = line.get("boxes") or []
        if not txt:
            stats["empty_text"] += 1
            continue
        if len(boxes) != len(txt):
            stats["box_count_mismatch"] += 1
            continue
        raw_confidence = line.get("confidence")
        if raw_confidence is None:
            confidence = None
            stats["confidence_unavailable"] += 1
        else:
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                confidence = math.nan
            if not math.isfinite(confidence):
                stats["invalid_confidence"] += 1
                continue
            if confidence < OCR_LINE_ACCEPT_THRESHOLD:
                stats["low_confidence"] += 1
                continue
        word_boxes = []
        incomplete_geometry = False
        for i, b in enumerate(boxes):
            # 空格没有字形框是正常表示，不计入坐标不完整。
            if txt[i].isspace():
                continue
            if not b or len(b) != 4:
                incomplete_geometry = True
                continue
            try:
                x0, y0, x1, y1 = (float(v) for v in b)
            except (TypeError, ValueError):
                incomplete_geometry = True
                continue
            if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
                incomplete_geometry = True
                continue
            # OCR returns top-left normalized image coordinates.
            word_boxes.append(D.WordBox(i, i + 1, x0 * width, y0 * height,
                                        x1 * width, y1 * height))
        block = D.Block(txt, block_type="page", location=f"第 {page_no} 页 OCR",
                        page=page_no, word_boxes=word_boxes)
        block.ocr_confidence = confidence
        block.ocr_confidences = [confidence] * len(txt)
        block.ocr_geometry = line.get("geometry", "unknown")
        # 逐字符定位标签，与文本索引一一对应；空格处沿用行级标签。
        block.ocr_geometries = [line.get("geometry", "unknown")] * len(txt)
        if incomplete_geometry:
            stats["incomplete_geometry"] += 1
        blocks.append(block)
    return blocks


def _join_ocr_lines(blocks, page_no):
    if not blocks:
        return None
    parts = []
    word_boxes = []
    confidences = []
    geoms = []
    pos = 0
    exact_geometry = True
    for line_index, block in enumerate(blocks):
        if line_index:
            parts.append("\n")
            pos += 1
            confidences.append(1.0)
            geoms.append("none")
        exact_geometry = exact_geometry and getattr(block, "ocr_geometry", None) == "character"
        line_boxes = {box.start: box for box in block.word_boxes
                      if box.end == box.start + 1}
        line_geoms = getattr(block, "ocr_geometries", None)
        line_label = getattr(block, "ocr_geometry", None) or "unknown"
        previous_box = None
        for index, char in enumerate(block.text):
            box = line_boxes.get(index)
            if (box and previous_box and not char.isspace() and
                    not block.text[index - 1].isspace() and
                    box.x0 - previous_box.x1 > OCR_COLUMN_GAP):
                parts.append(D._PDF_CELL_BOUNDARY)
                pos += 1
                confidences.append(1.0)
                geoms.append("none")
            parts.append(char)
            confidence_values = getattr(block, "ocr_confidences", None)
            if confidence_values and index < len(confidence_values):
                confidence = confidence_values[index]
            else:
                confidence = getattr(block, "ocr_confidence", None)
            confidences.append(None if confidence is None else float(confidence))
            # 逐字符定位标签，避免一行粗框拖累同块其他精确行。
            if line_geoms is not None and index < len(line_geoms):
                geoms.append(line_geoms[index] or "unknown")
            else:
                geoms.append(line_label)
            if box:
                word_boxes.append(D.WordBox(pos, pos + 1, box.x0, box.top,
                                            box.x1, box.bottom))
                if not char.isspace():
                    previous_box = box
            pos += 1
    merged = D.Block("".join(parts), block_type="page",
                     location=f"第 {page_no} 页 OCR", page=page_no,
                     word_boxes=word_boxes)
    merged.ocr_confidence = None
    merged.ocr_confidences = confidences
    merged.ocr_geometry = "character" if exact_geometry else "unknown"
    merged.ocr_geometries = geoms if len(geoms) == len(merged.text) else None
    return merged


def _ocr_wrapped_address_blocks(blocks, page_no):
    rows = []
    for block in blocks:
        boxes = [box for box in block.word_boxes if box.end > box.start]
        if not boxes:
            continue
        rows.append({
            "block": block,
            "x0": min(box.x0 for box in boxes),
            "top": min(box.top for box in boxes),
        })

    wrapped = []
    for left in rows:
        prefix = left["block"].text.rstrip()
        if not D.ADDR_WRAP_PREFIX_RE.search(prefix):
            continue
        matches = []
        for right in rows:
            if right["top"] <= left["top"]:
                continue
            if right["top"] - left["top"] > 22.0:
                continue
            x_delta = abs(right["x0"] - left["x0"])
            if x_delta > 18.0 or not D.ADDR_WRAP_SUFFIX_RE.match(right["block"].text.strip()):
                continue
            matches.append((x_delta, right["top"] - left["top"], right["block"]))
        if matches:
            right_block = min(matches, key=lambda item: (item[0], item[1]))[2]
            joined = _join_ocr_lines([left["block"], right_block], page_no)
            if joined:
                joined.location += " 地址换行"
                wrapped.append(joined)
    return wrapped


def _rules_snapshot(root, sid, project):
    if not project.get("rules_sha256"):
        return None  # 旧项目保持原识别结果与内置规则，不追溯套用新规则。
    path = paths(root, sid)[0] / "private" / "rules.md"
    if hashlib.sha256(path.read_bytes()).hexdigest() != project.get("rules_snapshot_sha256"):
        raise ValueError("PDF项目的规则快照已变化，请重新上传并复核")
    return D.build_rules_snapshot(str(path))


def _settings(litigation=False, rules_snapshot=None):
    settings = D.Settings(min_confidence=ENTITY_AUTO_SELECT_THRESHOLD, min_severity="low",
                         retain_low_confidence=True,
                         display_min_confidence=ENTITY_DISPLAY_MIN_CONFIDENCE)
    if rules_snapshot is not None:
        settings.extra_specs = list(rules_snapshot["extra_specs"])
        settings.extra_type_meta.update(rules_snapshot["type_overrides"])
    return R.configure_unified_settings(settings)


def _score_value(item, key, fallback=None):
    value = item.get(key)
    if value is None:
        value = fallback
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _entity_confidence(item):
    if item.get("score_semantics") == "legacy-composite" and item.get("entity_confidence") is None:
        return None
    return _score_value(item, "entity_confidence", item.get("confidence"))


def _ocr_confidence(item):
    if item.get("source") != "ocr":
        return None
    return _score_value(item, "ocr_confidence")


def _block_ocr_confidence(block, start, end):
    values = getattr(block, "ocr_confidences", None)
    matched = [values[index]
               for index in range(start, min(end, len(block.text)))
               if values and index < len(values) and not block.text[index].isspace()]
    if matched:
        if any(value is None for value in matched):
            return None
        try:
            confidence = min(float(value) for value in matched)
        except (TypeError, ValueError):
            return None
        return confidence if math.isfinite(confidence) else None
    return _score_value({"score": getattr(block, "ocr_confidence", None)}, "score")


def _score_review_reasons(item):
    reasons = []
    entity = _entity_confidence(item)
    ocr = _ocr_confidence(item)
    if item.get("score_semantics") == "legacy-composite" and entity is None:
        reasons.append("legacy_composite_score")
    if entity is not None and entity < ENTITY_REVIEW_THRESHOLD:
        reasons.append("low_entity_confidence")
    if ocr is not None and ocr < OCR_REVIEW_THRESHOLD:
        reasons.append("low_ocr_confidence")
    if (ocr is None and item.get("source") == "ocr" and
            item.get("score_semantics") == "split-v1"):
        reasons.append("ocr_confidence_unavailable")
    return reasons


def _candidate_id(item, occ):
    raw = f"{item['type']}|{item['value']}|{occ['page']}|{occ['block_index']}|{occ['local_offset']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _precise_text_range(block, start, end):
    # 按当前范围的逐字符定位标签判断，不用整块标记一票否决。
    geoms = getattr(block, "ocr_geometries", None)
    if geoms is None and getattr(block, "ocr_geometry", None) != "character":
        return False
    seen = set()
    for i in range(start, min(end, len(block.text))):
        if block.text[i].isspace():
            continue
        if geoms is not None and (i >= len(geoms) or geoms[i] != "character"):
            return False
        matches = [w for w in block.word_boxes if w.start <= i < w.end]
        if len(matches) != 1 or matches[0].start != i or matches[0].end != i + 1:
            return False
        box = (matches[0].x0, matches[0].top, matches[0].x1, matches[0].bottom)
        if box in seen:
            return False
        seen.add(box)
    return bool(seen)


def _candidate_replaceable(item):
    return item.get("geometry") == "character" and item.get("replaceable") is True


def _candidate_requires_manual_box(item):
    confidence = _ocr_confidence(item)
    return confidence is not None and confidence < OCR_BOX_THRESHOLD


def _candidate_direct_replaceable(item):
    return _candidate_replaceable(item) and not _candidate_requires_manual_box(item)


def _candidate_render_plan(item, page):
    """统一渲染计划：有真实页面框即可绘制，分数只影响处理模式与提示，
有效框不排除出渲染。replace=精确改字；box-replace=按真实框
    擦写代号、放不下则纯遮盖；none=缺少有效坐标，留人工。"""
    boxes = item.get("bboxes") or ([item.get("bbox")] if item.get("bbox") else [])
    if not page or not boxes or any(not _valid_review_box(box, page) for box in boxes):
        return {"mode": "none", "boxes": []}
    mode = "replace" if _candidate_direct_replaceable(item) else "box-replace"
    return {"mode": mode, "boxes": [list(box) for box in boxes]}


def _page_render_items(project, page_no):
    """同一项目快照下，预览与导出共用的逐页实际应用项。"""
    pages = {x.get("page"): x for x in project.get("pages", [])}
    candidates = [x for x in project.get("candidates", [])
                  if x.get("page") == page_no and x.get("selected") and
                  _candidate_render_plan(x, pages.get(page_no))["mode"] != "none"]
    regions = [x for x in project.get("regions", []) if x.get("page") == page_no]
    return candidates, regions


def _auto_disposition(project):
    """机器默认处置：能改字的已默认替换；有真实框但不能直改字的
    （OCR分低于门槛、词/行级粗定位、跨行分段）自动按框替换，代号放不下则遮盖。
    低实体分、编号候选与无坐标命中不自动处理；整体可撤销。"""
    pages = {x.get("page"): x for x in project.get("pages", [])}
    applied = set(project.get("auto_applied_ids") or [])
    snapshot = None
    for item in project.get("candidates", []):
        if item.get("id") in applied:
            continue
        if (item.get("selected") or item.get("resolution") in ("keep", "manual-covered") or
                item.get("policy_excluded") or item.get("type") == "NUMBER_CANDIDATE"):
            continue
        entity = _entity_confidence(item)
        if entity is None or entity < ENTITY_AUTO_SELECT_THRESHOLD:
            continue
        plan = _candidate_render_plan(item, pages.get(item.get("page")))
        if plan["mode"] != "box-replace":
            continue
        if snapshot is None:
            snapshot = {"candidates": json.loads(json.dumps(project["candidates"])),
                        "regions": json.loads(json.dumps(project["regions"])),
                        "unlocated_candidates": json.loads(json.dumps(
                            project.get("unlocated_candidates", []))),
                        "reviewed": [x["reviewed"] for x in project["pages"]],
                        "coverage_links": json.loads(json.dumps(project.get("coverage_links", [])))}
        reason = ("ocr_low" if _candidate_requires_manual_box(item)
                  else "multiline" if len(plan["boxes"]) > 1 else "coarse_geometry")
        item["selected"] = True
        item["auto_applied"] = reason
        applied.add(item["id"])
    if snapshot is not None:
        project.setdefault("undo", []).append(snapshot)
        project["undo"] = project["undo"][-10:]
    project["auto_applied_ids"] = sorted(applied)
    project["auto_disposition_done"] = True


def _candidate_review_reasons(item):
    reasons = _score_review_reasons(item)
    if _candidate_requires_manual_box(item):
        reasons.append("manual_box_required")
    failure = item.get("failure_reason")
    if failure:
        geometry_reason = {
            "page-unknown": "missing_geometry",
            "missing-bbox": "missing_geometry",
            "missing-character-geometry": "missing_geometry",
            "invalid-page-bbox": "invalid_geometry",
        }.get(failure)
        if geometry_reason:
            reasons.append(geometry_reason)
    elif not _candidate_replaceable(item):
        boxes = item.get("bboxes") or ([item.get("bbox")] if item.get("bbox") else [])
        if not boxes or item.get("geometry") == "character":
            reasons.append("missing_geometry")
        else:
            reasons.append("coarse_geometry")
    return list(dict.fromkeys(reasons))


def _local_geometry(block, start, end):
    """当前出现范围的局部定位标签；旧缓存无逐字符标签时回退整块标记。"""
    geoms = getattr(block, "ocr_geometries", None)
    block_label = getattr(block, "ocr_geometry", None) or "unknown"
    if not geoms:
        return block_label
    labels = [geoms[i] for i in range(start, min(end, len(block.text), len(geoms)))
              if not block.text[i].isspace()]
    if not labels:
        return block_label
    if all(label == "character" for label in labels):
        return "character"
    for preferred in ("word", "line"):
        if all(label in (preferred, "none") for label in labels):
            return preferred
    return "unknown"


def _range_bboxes(block, start, end, fallback, replaceable):
    # 无论能否精确改字，都优先采用范围内真实字符/词框去重后按行保留；
    # 只有确实没有真实框时才回退到整体 fallback，不伪造字符级精度。
    boxes = [box for box in block.word_boxes
             if box.end > start and box.start < end and
             box.start < len(block.text) and not block.text[box.start].isspace()]
    deduped, seen_coords = [], set()
    for box in boxes:
        key = (round(box.x0, 3), round(box.top, 3), round(box.x1, 3), round(box.bottom, 3))
        if key in seen_coords:
            continue
        seen_coords.add(key)
        deduped.append(box)
    boxes = deduped
    if not boxes:
        return [list(fallback)] if fallback else []
    heights = sorted(box.bottom - box.top for box in boxes)
    tolerance = max(1.5, heights[len(heights) // 2] * 0.55)
    rows = []
    for box in sorted(boxes, key=lambda item: ((item.top + item.bottom) / 2, item.x0)):
        center = (box.top + box.bottom) / 2
        row = next((group for group in rows
                    if abs(center - group["center"]) <= tolerance), None)
        if row is None:
            row = {"center": center, "boxes": []}
            rows.append(row)
        row["boxes"].append(box)
        row["center"] = sum((item.top + item.bottom) / 2 for item in row["boxes"]) / len(row["boxes"])
    return [[min(box.x0 for box in row["boxes"]),
             min(box.top for box in row["boxes"]),
             max(box.x1 for box in row["boxes"]),
             max(box.bottom for box in row["boxes"])]
            for row in rows]


def _candidate_value_key(value, type_id=None):
    value = str(value or "")
    if type_id in ("AMOUNT", "MONEY"):
        value = value.translate(str.maketrans({"，": ",", "．": "."}))
    return re.sub(r"\s+", "", value).casefold()


def _boxes_overlap(left, right):
    for a in left or []:
        for b in right or []:
            intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
                0.0, min(a[3], b[3]) - max(a[1], b[1]))
            smaller = min((a[2] - a[0]) * (a[3] - a[1]),
                          (b[2] - b[0]) * (b[3] - b[1]))
            if smaller > 0 and intersection / smaller >= 0.45:
                return True
    return False


def _unlocated_reason(doc, occurrence, limits):
    page_no = occurrence.get("page")
    if page_no is None:
        return "page-unknown"
    bbox = occurrence.get("bbox")
    if not bbox:
        return "missing-bbox"
    if (len(bbox) != 4 or
            not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox) or
            not (0 <= bbox[0] < bbox[2] and 0 <= bbox[1] < bbox[3])):
        return "invalid-page-bbox"
    size = limits.get(page_no) if limits else None
    if limits and (size is None or
                   not (0 <= bbox[0] < bbox[2] <= size[0] and
                        0 <= bbox[1] < bbox[3] <= size[1])):
        return "invalid-page-bbox"
    block_index = occurrence.get("block_index")
    if not isinstance(block_index, int) or not 0 <= block_index < len(doc.blocks):
        return "missing-character-geometry"
    block = doc.blocks[block_index]
    start, end = occurrence.get("local_offset") or (0, 0)
    if any(not any(w.start <= i < w.end for w in block.word_boxes)
           for i in range(max(0, start), min(end, len(block.text)))
           if not block.text[i].isspace()):
        return "missing-character-geometry"
    return ""


def _occurrence_identity(type_id, occurrence):
    block_index = occurrence.get("block_index")
    local_offset = occurrence.get("local_offset")
    if (isinstance(block_index, int) and isinstance(local_offset, (list, tuple)) and
            len(local_offset) == 2 and all(isinstance(value, int) for value in local_offset)):
        return type_id, occurrence.get("page"), block_index, local_offset[0], local_offset[1]
    return None


def _unlocated_candidate(doc, item, occurrence, identity, index, reason, numberer,
                         policy_excluded=False):
    block_index = occurrence.get("block_index")
    block = (doc.blocks[block_index] if isinstance(block_index, int) and
             0 <= block_index < len(doc.blocks) else None)
    start, end = occurrence.get("local_offset") or (0, 0)
    if block is not None:
        start = max(0, min(len(block.text), start))
        end = max(start, min(len(block.text), end))
        value = (occurrence.get("text") or block.text[start:end] or item["value"])
        context = block.text[max(0, start - 70):min(len(block.text), end + 70)]
        source = "ocr" if "OCR" in block.location else "text"
        ocr_confidence = _block_ocr_confidence(block, start, end) if source == "ocr" else None
    else:
        value = occurrence.get("text") or item["value"]
        context = occurrence.get("context") or ""
        source = "text"
        ocr_confidence = None
    location_key = repr(identity) if identity is not None else "%s|%s|%s|%s" % (
        item["type"], occurrence.get("page"), occurrence.get("local_offset"), index)
    candidate = {
        "id": hashlib.sha256(location_key.encode("utf-8")).hexdigest()[:20],
        "page": occurrence.get("page"), "type": item["type"],
        "label": item["label"], "value": value,
        "placeholder": numberer.placeholder(item["type"], item["value"], item["label"]),
        "confidence": float(item["confidence"]),
        "entity_confidence": float(item["confidence"]),
        "ocr_confidence": ocr_confidence, "score_semantics": "split-v1",
        "requires_manual_box": (ocr_confidence is not None and
                                 ocr_confidence < OCR_BOX_THRESHOLD),
        "source": source,
        "block_index": block_index, "local_offset": list(occurrence.get("local_offset") or []),
        "context": context, "failure_reason": reason,
        "status": "keep" if policy_excluded else "pending",
        "policy_excluded": policy_excluded,
        "resolution": "policy-excluded" if policy_excluded else "",
    }
    candidate["review_reasons"] = _candidate_review_reasons(candidate)
    return candidate


def _suggestions(doc, profile, terms, stored_numbers=None, pages=None,
                 policy_id=Policy.PRESERVE_TRANSACTION, include_stats=False,
                 rules_snapshot=None):
    settings = _settings(profile == "litigation", rules_snapshot)
    engine = D.DetectorEngine(settings)
    candidates = engine.scan_document(doc)
    scan_stats = dict(engine.last_scan_stats)
    R.classify_unified_candidates(candidates)
    if terms:
        candidates = R.add_exact_terms(doc, candidates, terms)
    report = D.build_report(doc, candidates, settings)
    numbers, overrides = R.assign_case_numbers(report, stored_numbers or [])
    numberer = D.EntityNumberer(report["items"], overrides)
    out = []
    unlocated_by_location = {}
    unlocated_without_location = []
    located_occurrences = set()
    limits = {p["page"]: (p["width"], p["height"]) for p in (pages or [])}
    for item in report["items"]:
        for occurrence_index, occ in enumerate(item["occurrences"]):
            identity = _occurrence_identity(item["type"], occ)
            bbox = occ.get("bbox")
            reason = _unlocated_reason(doc, occ, limits)
            if reason:
                if identity is None or identity not in located_occurrences:
                    candidate = _unlocated_candidate(
                        doc, item, occ, identity, occurrence_index, reason, numberer,
                        item["type"] in Policy.excluded_types(policy_id))
                    if identity is None:
                        unlocated_without_location.append(candidate)
                    else:
                        unlocated_by_location.setdefault(identity, candidate)
                continue
            block = doc.blocks[occ["block_index"]]
            start, end = occ["local_offset"]
            is_ocr = "OCR" in block.location
            geometry = _local_geometry(block, start, end)
            replaceable = _precise_text_range(block, start, end)
            block_confidence = (_block_ocr_confidence(block, start, end)
                                if is_ocr else None)
            value = (occ.get("text") or item["value"]).replace("\r\n", " ").replace("\n", " ")
            if item["type"] == "PERSON_NAME" and not re.search(r"[A-Za-z]", item["value"]):
                value = re.sub(r"\s+", "", item["value"])
            bboxes = _range_bboxes(block, start, end, bbox, replaceable)
            entity_confidence = float(item["confidence"])
            ocr_confidence = block_confidence if is_ocr else None
            excluded = item["type"] in Policy.excluded_types(policy_id)
            auto_selected = (not excluded and
                             item["type"] != "NUMBER_CANDIDATE" and replaceable and
                             entity_confidence >= ENTITY_AUTO_SELECT_THRESHOLD and
                             (not is_ocr or (ocr_confidence is not None and
                                             ocr_confidence >= OCR_AUTO_SELECT_THRESHOLD)))
            if excluded:
                selection_reason = "policy-excluded"
            elif item["type"] == "NUMBER_CANDIDATE":
                selection_reason = "number-candidate"
            elif not replaceable:
                selection_reason = "unreplaceable-geometry"
            elif entity_confidence < ENTITY_AUTO_SELECT_THRESHOLD:
                selection_reason = "entity-confidence-below-auto-select-threshold"
            elif is_ocr and ocr_confidence is None:
                selection_reason = "ocr-confidence-unavailable"
            elif is_ocr and ocr_confidence < OCR_AUTO_SELECT_THRESHOLD:
                selection_reason = "ocr-confidence-below-box-threshold"
            else:
                selection_reason = "selected-by-default"
            candidate = {"id": _candidate_id(item, occ), "page": occ["page"],
                        "type": item["type"], "label": item["label"],
                        "value": value,
                        "placeholder": numberer.placeholder(item["type"], item["value"], item["label"]),
                        "bbox": bbox, "bboxes": bboxes, "replaceable": replaceable,
                        "selected": auto_selected,
                        "policy_excluded": excluded,
                        "source": "ocr" if is_ocr else "text",
                        "confidence": entity_confidence,
                        "entity_confidence": entity_confidence,
                        "ocr_confidence": ocr_confidence,
                        "score_semantics": "split-v1",
                        "requires_manual_box": _candidate_requires_manual_box({
                            "source": "ocr" if is_ocr else "text",
                            "ocr_confidence": ocr_confidence}),
                        "selection_reason": selection_reason,
                        "geometry": geometry}
            candidate["review_reasons"] = _candidate_review_reasons(candidate)
            duplicate = any(
                existing["page"] == candidate["page"] and
                existing["type"] == candidate["type"] and
                _candidate_value_key(existing["value"], existing["type"]) == _candidate_value_key(candidate["value"], candidate["type"]) and
                _boxes_overlap(existing.get("bboxes", [existing.get("bbox")]),
                               candidate.get("bboxes", [candidate.get("bbox")]))
                for existing in out
            )
            if not duplicate:
                out.append(candidate)
            if identity is not None:
                located_occurrences.add(identity)
                unlocated_by_location.pop(identity, None)
    unlocated = list(unlocated_by_location.values()) + unlocated_without_location
    if include_stats:
        return out, numbers, unlocated, scan_stats
    return out, numbers, unlocated


def _serialize_blocks(blocks):
    return [{"text": b.text, "block_type": b.block_type, "location": b.location,
             "page": b.page, "ocr_confidence": getattr(b, "ocr_confidence", None),
             "ocr_confidences": getattr(b, "ocr_confidences", None),
             "ocr_geometry": getattr(b, "ocr_geometry", None),
             "ocr_geometries": getattr(b, "ocr_geometries", None),
             "word_boxes": [[w.start, w.end, w.x0, w.top, w.x1, w.bottom]
                            for w in b.word_boxes]} for b in blocks]


def _deserialize_blocks(raw):
    out = []
    for x in raw:
        block = D.Block(x["text"], x["block_type"], x["location"], x["page"],
                        [D.WordBox(*w) for w in x["word_boxes"]])
        if x.get("ocr_confidence") is not None:
            block.ocr_confidence = x["ocr_confidence"]
        if x.get("ocr_confidences") is not None:
            block.ocr_confidences = x["ocr_confidences"]
        if x.get("ocr_geometry") is not None:
            block.ocr_geometry = x["ocr_geometry"]
        # 旧缓存（v6 及以前）没有逐字符标签：保留旧精度标签，不推断为精确。
        geoms = x.get("ocr_geometries")
        if isinstance(geoms, list) and len(geoms) == len(x["text"]):
            block.ocr_geometries = geoms
        out.append(block)
    return out


def _blocks_file(root, sid, page_no):
    folder = paths(root, sid)[0] / "private" / "text"
    folder.mkdir(exist_ok=True)
    return folder / f"{page_no:04d}.json"


def process(root, sid):
    """Background page rendering/OCR; checkpoints survive restart/cancel/failure."""
    with _lock(sid):
        project = load(root, sid)
        project["cancel"] = False
        project["stage"] = "processing"
        project["error"] = ""
        save(root, sid, project)
    try:
        source = original(root, sid)
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(source))
        pdf.init_forms()
        base = paths(root, sid)[0]
        previews = base / "private" / "pages"
        previews.mkdir(exist_ok=True)
        extracted = D.extract_pdf(str(source))
        by_page = {}
        for block in extracted.blocks:
            by_page.setdefault(block.page, []).append(block)
        blocks = []
        for i in range(len(pdf)):
            with _lock(sid):
                project = load(root, sid)
                if project["cancel"]:
                    project["stage"] = "cancelled"
                    save(root, sid, project)
                    return
            page_no = i + 1
            img_path = previews / f"{page_no:04d}.png"
            cache = _blocks_file(root, sid, page_no)
            page_line_stats = _empty_ocr_line_stats()
            if (project["pages"][i]["stage"] == "ready" and
                    project["pages"][i].get("block_version") == PDF_BLOCK_VERSION and
                    img_path.is_file() and cache.is_file()):
                blocks.extend(_deserialize_blocks(json.loads(cache.read_text(encoding="utf-8"))))
                continue
            image = _render(pdf, i, img_path)
            text_blocks = by_page.get(page_no, [])
            # OCR supplements digital text pages because their text order can differ from visible layout.
            page_state = project["pages"][i]
            reliable = (not page_state.get("force_ocr") and
                        any(b.word_boxes and len(b.text.strip()) >= 10 for b in text_blocks))
            lines = []
            if reliable:
                page_blocks = list(text_blocks)
                page_state["ocr"] = "text-layer"
                page_state["error"] = ""
                try:
                    lines = _ocr(img_path)
                    ocr_blocks = _ocr_blocks(lines, page_no, image.width / SCALE,
                                             image.height / SCALE, page_line_stats)
                    merged = _join_ocr_lines(ocr_blocks, page_no)
                    if merged and merged.text.strip():
                        page_blocks.append(merged)
                        page_state["ocr_supplement"] = "complete"
                    else:
                        page_state["ocr_supplement"] = "empty"
                    page_blocks.extend(_ocr_wrapped_address_blocks(ocr_blocks, page_no))
                    page_state["ocr_line_stats"] = page_line_stats
                    page_state["dropped_lines"] = sum(page_line_stats[key] for key in
                        ("empty_text", "box_count_mismatch", "invalid_confidence", "low_confidence"))
                    page_state["supplement_error"] = ""
                except Exception as exc:
                    page_state["ocr_supplement"] = "failed"
                    page_state["supplement_error"] = str(exc)[:240]
                    page_state["dropped_lines"] = 0
                    page_state["ocr_line_stats"] = page_line_stats
            else:
                try:
                    lines = _ocr(img_path)
                    ocr_blocks = _ocr_blocks(lines, page_no, image.width / SCALE,
                                             image.height / SCALE, page_line_stats)
                    merged = _join_ocr_lines(ocr_blocks, page_no)
                    page_blocks = [merged] if merged and merged.text.strip() else []
                    page_blocks.extend(_ocr_wrapped_address_blocks(ocr_blocks, page_no))
                    page_state["ocr_line_stats"] = page_line_stats
                    page_state["dropped_lines"] = sum(page_line_stats[key] for key in
                        ("empty_text", "box_count_mismatch", "invalid_confidence", "low_confidence"))
                    page_state["ocr"] = "complete" if page_blocks else "empty"
                    page_state["ocr_supplement"] = "not-needed"
                    page_state["error"] = ""
                except Exception as exc:
                    page_blocks = []
                    page_state["ocr"] = "failed"
                    page_state["ocr_supplement"] = "not-needed"
                    page_state["error"] = str(exc)[:240]
                    page_state["dropped_lines"] = 0
                    page_state["ocr_line_stats"] = page_line_stats
            # 诊断只保存在本机会话私有目录，供逐位置核查，不写入通用日志。
            _atomic(cache.with_suffix(".ocr.json"), {"lines": lines})
            page_state["ocr_retried_lines"] = sum(bool(x.get("retry_status")) for x in lines)
            page_state["ocr_improved_lines"] = sum(x.get("retry_status") == "improved" for x in lines)
            blocks.extend(page_blocks)
            _atomic(cache, _serialize_blocks(page_blocks))
            page_state["block_version"] = PDF_BLOCK_VERSION
            # 重新处理一页即视为「此前的人工确认作废」，须重新确认。
            page_state["manual_confirm"] = False
            page_state["stage"] = "ready"
            with _lock(sid):
                latest = load(root, sid)
                latest["pages"][i] = page_state
                latest["done"] = sum(x["stage"] == "ready" for x in latest["pages"])
                save(root, sid, latest)
        pdf.close()
        doc = D.Document(source=str(source), file_type="pdf", blocks=blocks,
                         sha256=project["sha256"])
        with _lock(sid):
            project = load(root, sid)
            candidates, numbers, unlocated, scan_stats = _suggestions(
                doc, project["profile"], project["terms"], project.get("numbers"),
                project["pages"], project.get("policy_id", Policy.PRESERVE_TRANSACTION),
                include_stats=True, rules_snapshot=_rules_snapshot(root, sid, project))
            _preserve_review_decisions(candidates, unlocated, project)
            project["candidates"], project["numbers"], project["unlocated_candidates"] = candidates, numbers, unlocated
            _auto_disposition(project)
            project["scan_stats"] = scan_stats
            project["english_dominant"] = D.is_english_dominant(doc.text)
            project["stage"] = "review"
            save(root, sid, project)
    except Exception as exc:
        with _lock(sid):
            project = load(root, sid)
            project["stage"] = "failed"
            project["error"] = str(exc)[:300]
            save(root, sid, project)


def public(project):
    """API view is local-only, but avoid exposing private paths."""
    result = {k: v for k, v in project.items() if k not in ("sha256", "numbers", "undo")}
    result["review_evaluation"] = evaluate_review(project)
    return result


def _invalidate_output(root, sid, project):
    # A changed review must not leave an outdated outward copy in History.
    (paths(root, sid)[0] / "out" / "脱敏文书-001.pdf").unlink(missing_ok=True)
    project["output"] = ""
    if project["stage"] == "exported":
        project["stage"] = "review"


def _preserve_review_decisions(candidates, unlocated, prior):
    """仅对身份未变的出现位置保留操作决定；重识别页应先移除旧候选。"""
    old = {x["id"]: x for x in prior.get("candidates", [])}
    def position_key(item):
        boxes = item.get("bboxes") or ([item["bbox"]] if item.get("bbox") else [])
        return (item.get("page"), item.get("type"), item.get("source"),
                _candidate_value_key(item.get("value"), item.get("type")),
                tuple(tuple(round(v, 3) for v in box) for box in boxes))
    positions = {}
    for previous in old.values():
        if previous.get("bbox") or previous.get("bboxes"):
            positions.setdefault(position_key(previous), []).append(previous)
    for item in candidates:
        previous = old.get(item["id"])
        if (previous is not None and (item.get("bbox") or item.get("bboxes")) and
                position_key(previous) != position_key(item)):
            previous = None
        if previous is None:
            matches = positions.get(position_key(item), [])
            if len(matches) == 1:
                previous = matches[0]
        if previous is not None and previous.get("resolution") in ("keep", "redact"):
            if previous["resolution"] == "keep":
                item["selected"] = False
                item["resolution"] = "keep"
            else:
                # 选中状态对 box-replace 同样有效，重扫后按原样保留。
                item["selected"] = True
                item["resolution"] = "redact"
    old_unlocated = {x["id"]: x for x in prior.get("unlocated_candidates", [])}
    for item in unlocated:
        previous = old_unlocated.get(item["id"], {})
        for key in ("status", "region_id"):
            if key in previous:
                item[key] = previous[key]


def add_term(root, sid, term):
    scope = term.get("scope", "document") if isinstance(term, dict) else "document"
    term = dict(R.validate_term(term), scope=scope)
    with _lock(sid):
        p = load(root, sid)
        if p["stage"] not in ("review", "exported"):
            raise ValueError("请等待页面处理完成")
        terms = [x for x in p["terms"] if x["id"] != term["id"]] + [term]
        if len(terms) > 100:
            raise ValueError("每份文书最多 100 个自选字段")
        doc = _cached_document(root, sid, p)
        new, numbers, unlocated, scan_stats = _suggestions(
            doc, p["profile"], terms, p.get("numbers"), p["pages"], p.get("policy_id"),
            include_stats=True, rules_snapshot=_rules_snapshot(root, sid, p))
        _preserve_review_decisions(new, unlocated, p)
        p["terms"] = terms
        p["candidates"] = new
        p["unlocated_candidates"] = unlocated
        p["numbers"] = numbers
        p["scan_stats"] = scan_stats
        _auto_disposition(p)
        for page in p["pages"]:
            page["reviewed"] = False
            page["revision"] += 1
        p["revision"] += 1
        _normalize_review(p)
        _invalidate_output(root, sid, p)
        save(root, sid, p)
        return public(p)


def _cached_document(root, sid, project):
    blocks = []
    for page in project["pages"]:
        cache = _blocks_file(root, sid, page["page"])
        blocks.extend(_deserialize_blocks(json.loads(cache.read_text(encoding="utf-8"))))
    return D.Document(source=str(original(root, sid, project)), file_type="pdf",
                      blocks=blocks, sha256=project["sha256"])


def sync_case(root, sid, terms, numbers):
    with _lock(sid):
        p = load(root, sid)
        if p["stage"] not in ("review", "exported"):
            return p
        doc = _cached_document(root, sid, p)
        merged = [dict(x, scope="case") for x in terms]
        merged += [x for x in p["terms"] if x.get("scope") != "case"]
        candidates, rows, unlocated, scan_stats = _suggestions(
            doc, p["profile"], merged, numbers, p["pages"], p.get("policy_id"),
            include_stats=True, rules_snapshot=_rules_snapshot(root, sid, p))
        _preserve_review_decisions(candidates, unlocated, p)
        changed = (p["terms"] != merged or p["candidates"] != candidates or
                   p.get("unlocated_candidates", []) != unlocated)
        p["terms"] = merged
        p["candidates"] = candidates
        p["unlocated_candidates"] = unlocated
        p["numbers"] = rows
        p["scan_stats"] = scan_stats
        _auto_disposition(p)
        p["case_rules_pending"] = False
        if changed:
            for page in p["pages"]:
                page["reviewed"] = False
                page["revision"] += 1
            p["revision"] += 1
            _invalidate_output(root, sid, p)
        save(root, sid, p)
        return p


def mark_case_pending(root, case_name, except_sid):
    for candidate in Path(root).iterdir():
        sid = candidate.name
        if sid == except_sid or not re.fullmatch(r"[0-9a-f]{12}", sid):
            continue
        if not (candidate / "pdf-project.json").is_file():
            continue
        with _lock(sid):
            try:
                p = load(root, sid)
                if p["case_name"] == case_name:
                    p["case_rules_pending"] = True
                    save(root, sid, p)
            except Exception:
                continue


def control(root, sid, action):
    with _lock(sid):
        p = load(root, sid)
        if action == "cancel" and p["stage"] == "processing":
            p["cancel"] = True
        elif action == "retry" and p["stage"] in ("failed", "cancelled"):
            p["cancel"] = False
            p["stage"] = "processing"
            p["error"] = ""
        else:
            raise ValueError("当前状态不能执行此操作")
        save(root, sid, p)
        return public(p)


def update(root, sid, req):
    with _lock(sid):
        p = load(root, sid)
        # 仅扫描运行中禁止编辑；失败/取消后仍可调整已就绪页。
        if p["stage"] == "processing":
            raise ValueError("请等待页面处理完成")
        if req.get("revision") != p["revision"]:
            raise ValueError("页面已更新，请刷新后再操作")
        action = req.get("action")
        page_no = req.get("page")
        if not isinstance(page_no, int) or not 1 <= page_no <= len(p["pages"]):
            raise ValueError("页码无效")
        page = p["pages"][page_no - 1]
        snapshot = None
        if action in ("candidate", "type-toggle", "region-add", "region-update",
                      "region-delete", "unlocated-keep", "coverage-bind", "region-from-candidates", "candidates-keep"):
            snapshot = {"candidates": json.loads(json.dumps(p["candidates"])),
                        "regions": json.loads(json.dumps(p["regions"])),
                        "unlocated_candidates": json.loads(json.dumps(
                            p.get("unlocated_candidates", []))),
                        "reviewed": [x["reviewed"] for x in p["pages"]],
                        "coverage_links": json.loads(json.dumps(p.get("coverage_links", [])))}
        if action == "undo":
            history = p.get("undo", [])
            if not history:
                raise ValueError("没有可撤销的更改")
            prior = history.pop()
            p["candidates"] = prior["candidates"]
            p["regions"] = prior["regions"]
            p["coverage_links"] = prior.get("coverage_links", [])
            p["unlocated_candidates"] = prior.get("unlocated_candidates", [])
            for state, reviewed in zip(p["pages"], prior["reviewed"]):
                state["reviewed"] = reviewed
        elif action == "review":
            page["reviewed"] = True
            # 识别失败的页必须由用户显式勾选「我已检查该页预览，确认无未处理的敏感信息」才放行导出。
            if req.get("manual_confirm") is True:
                page["manual_confirm"] = True
        elif action == "candidate":
            item = next((x for x in p["candidates"] if x["id"] == req.get("id") and x["page"] == page_no), None)
            if item is None or not isinstance(req.get("selected"), bool):
                raise ValueError("替换项无效")
            if req["selected"] and _candidate_render_plan(item, page)["mode"] == "none":
                raise ValueError("该项缺少有效坐标，请在原页框选遮盖或选择保留")
            item["selected"] = req["selected"]
            item["resolution"] = "redact" if req["selected"] else "keep"
            item.pop("auto_applied", None)
            page["reviewed"] = False
        elif action == "type-toggle":
            label = req.get("label")
            items = [x for x in p["candidates"] if x["page"] == page_no and x["label"] == label]
            if not items or not isinstance(label, str) or not isinstance(req.get("selected"), bool):
                raise ValueError("类型开关无效")
            # 逐项按渲染能力应用；缺有效坐标的项保持未选，不整组拒绝。
            pages_by_no = {x.get("page"): x for x in p["pages"]}
            for item in items:
                if req["selected"] and _candidate_render_plan(item, pages_by_no.get(page_no))["mode"] == "none":
                    continue
                item["selected"] = req["selected"]
                if req["selected"]:
                    item["resolution"] = "redact"
                else:
                    item.pop("resolution", None)
                item.pop("auto_applied", None)
            page["reviewed"] = False
        elif action == "coverage-bind":
            bind_coverage(p, req.get("candidate_refs"), req.get("region_ids"), page_no, req.get("coverage_confirmed"))
            page["reviewed"] = False
        elif action == "candidates-keep":
            refs = req.get("candidate_refs")
            if not isinstance(refs, list) or not refs or len(refs) > 500:
                raise ValueError("请选择需要保留原文的位置")
            items = [(ref, _review_candidate(p, ref)) for ref in refs]
            if any(item.get("page") not in (None, page_no) for _, item in items):
                raise ValueError("请仅选择当前页的位置")
            for ref, item in items:
                if ref["collection"] == "candidates":
                    item.update(selected=False, resolution="keep")
                else:
                    item["status"] = "keep"
                p["coverage_links"] = [link for link in p.get("coverage_links", []) if link["candidate_ref"] != ref]
            page["reviewed"] = False
        elif action == "region-from-candidates":
            refs = req.get("candidate_refs")
            if not isinstance(refs, list) or not refs or len(refs) > 500:
                raise ValueError("请选择本页需要遮盖的候选")
            prepared, seen = [], set()
            for ref in refs:
                item = _review_candidate(p, ref)
                if item.get("page") != page_no:
                    raise ValueError("请仅选择本页已有坐标的候选")
                boxes = item.get("bboxes") or ([item["bbox"]] if item.get("bbox") else [])
                if not boxes or any(not _valid_review_box(b, page) for b in boxes):
                    raise ValueError("候选坐标缺失或越界，请手工定位")
                for box in boxes:
                    if tuple(box) not in seen:
                        prepared.append({"id": uuid.uuid4().hex[:16], "page": page_no, "bbox": list(box),
                                         "requires_confirmation": True, "candidate_ref": dict(ref)})
                        seen.add(tuple(box))
            p["regions"].extend(prepared)
            # 一键遮盖即绑定覆盖关系，无需二次确认按钮。
            region_ids = [r["id"] for r in prepared]
            if region_ids:
                bind_coverage(p, refs, region_ids, page_no, True)
            page["reviewed"] = False
        elif action == "unlocated-keep":
            item = next((x for x in p.get("unlocated_candidates", [])
                         if x["id"] == req.get("id")), None)
            if item is None or item.get("status", "pending") != "pending":
                raise ValueError("待定位项无效或已处理")
            item["status"] = "keep"
            page["reviewed"] = False
        elif action in ("region-add", "region-update"):
            box = req.get("bbox")
            if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box):
                raise ValueError("遮盖坐标无效")
            x0, y0, x1, y1 = box
            if not (0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]):
                raise ValueError("遮盖区域超出页面")
            if action == "region-add":
                region = {"id": uuid.uuid4().hex[:16], "page": page_no, "bbox": box}
                resolves_id = req.get("resolves_id")
                if resolves_id:
                    candidate = next((x for x in p.get("unlocated_candidates", [])
                                      if x["id"] == resolves_id), None)
                    if (candidate is None or candidate.get("status", "pending") != "pending" or
                            (candidate.get("page") is not None and
                             candidate["page"] != page_no)):
                        raise ValueError("待定位项无效，请刷新后重试")
                    region["resolves_id"] = candidate["id"]
                    candidate["status"] = "manual-covered"
                    candidate["region_id"] = region["id"]
                p["regions"].append(region)
                if req.get("candidate_ref"):
                    bind_coverage(p, [req["candidate_ref"]], [region["id"]], page_no, True)
                if resolves_id:
                    bind_coverage(p, [{"collection": "unlocated_candidates", "id": resolves_id}],
                                  [region["id"]], page_no, True)
            else:
                item = next((x for x in p["regions"] if x["id"] == req.get("id") and x["page"] == page_no), None)
                if item is None:
                    raise ValueError("遮盖区域不存在")
                item["bbox"] = box
                # 几何改变会使旧关联失效；未知位置不能自动续认已覆盖。
            page["reviewed"] = False
        elif action == "region-delete":
            before = len(p["regions"])
            deleted = [x for x in p["regions"]
                       if x["id"] == req.get("id") and x["page"] == page_no]
            p["regions"] = [x for x in p["regions"] if not (x["id"] == req.get("id") and x["page"] == page_no)]
            if before == len(p["regions"]):
                raise ValueError("遮盖区域不存在")
            for region in deleted:
                resolves_id = region.get("resolves_id")
                if resolves_id:
                    candidate = next((x for x in p.get("unlocated_candidates", [])
                                      if x["id"] == resolves_id), None)
                    if candidate:
                        candidate["status"] = "pending"
                        candidate.pop("region_id", None)
            page["reviewed"] = False
        elif action == "retry-page":
            page["stage"] = "pending"
            page["ocr"] = "pending"
            page["ocr_supplement"] = "pending"
            page["force_ocr"] = True
            page["error"] = ""
            page["reviewed"] = False
            _blocks_file(root, sid, page_no).unlink(missing_ok=True)
            p["candidates"] = [x for x in p["candidates"] if x["page"] != page_no]
            p["unlocated_candidates"] = [x for x in p.get("unlocated_candidates", [])
                                         if x.get("page") not in (None, page_no)]
            p["undo"] = []
            p["stage"] = "processing"
            p["done"] = sum(x["stage"] == "ready" for x in p["pages"])
        else:
            raise ValueError("不支持的操作")
        if snapshot is not None:
            p.setdefault("undo", []).append(snapshot)
            p["undo"] = p["undo"][-10:]
        if action not in ("review", "undo"):
            page["content_revision"] = page.get("content_revision", 0) + 1
        if action == "review":
            page["reviewed_content_revision"] = page.get("content_revision", 0)
        page["revision"] += 1
        p["revision"] += 1
        _normalize_review(p)
        _invalidate_output(root, sid, p)
        save(root, sid, p)
        return public(p)


def _clean_box(box, width, height, pad=3):
    x0, y0, x1, y1 = box
    return (max(0, int((x0 - pad) * SCALE)), max(0, int((y0 - pad) * SCALE)),
            min(width, int(math.ceil((x1 + pad) * SCALE))),
            min(height, int(math.ceil((y1 + pad) * SCALE))))


def _font(size=22):
    from PIL import ImageFont
    options = ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
               str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"),
               str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simsun.ttc"),
               "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"]
    for path in options:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    raise RuntimeError("没有可用的中文字体，无法安全绘制代号")


def _layout_placeholder(draw, label, box):
    """最小八点字号；短框缩为类型字母和编号，仍放不下则只遮盖。"""
    x0, y0, x1, y1 = box
    width, height = max(0, x1 - x0 - 4), max(0, y1 - y0 - 2)
    match = re.fullmatch(r"\[([^#]+)#(\d+)\]", label)
    choices = [label]
    if match:
        aliases = {"组织机构名称": "机构", "自然人姓名": "姓名", "身份证号": "证件",
                   "联系电话": "电话", "电子邮箱": "邮箱", "银行账号": "账号"}
        choices.append(f"[{aliases.get(match.group(1), match.group(1)[:2])}#{match.group(2)}]")
        codes = {"组织机构名称": "O", "自然人姓名": "P", "电子邮箱": "E",
                 "联系电话": "T", "国际电话号码": "T", "银行账号": "B"}
        if match.group(1) in codes:
            choices.append(f"{codes[match.group(1)]}{match.group(2)}")
    for candidate in dict.fromkeys(choices):
        for size in range(min(22, int(height)), 15, -1):
            font = _font(size)
            bounds = draw.textbbox((0, 0), candidate, font=font)
            if bounds[2] - bounds[0] <= width and bounds[3] - bounds[1] <= height:
                return candidate, font, (x0 + 2 - bounds[0], y0 + 1 - bounds[1])
    return None


def _redact_image(image, candidates, regions):
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(image)
    # Erase all selected text before drawing any replacement. Overlapping hits stay erased.
    replacements = []
    for item in candidates:
        source_boxes = item.get("bboxes") or [item["bbox"]]
        boxes = [_clean_box(box, image.width, image.height) for box in source_boxes]
        for box in boxes:
            draw.rectangle(box, fill="white")
        if boxes:
            replacements.append((max(boxes, key=lambda box: (box[2] - box[0]) *
                                     (box[3] - box[1])), item))
    for box, item in replacements:
        x0, y0, x1, y1 = box
        layout = _layout_placeholder(draw, item["placeholder"], box)
        if layout:
            label, font, position = layout
            draw.text(position, label, fill="black", font=font)
        else:
            draw.rectangle(box, fill="black")
    for item in regions:
        draw.rectangle(_clean_box(item["bbox"], image.width, image.height, pad=0), fill="black")
    return image


def preview_image(root, sid, page_no):
    """Render the same replacement routine as export over the stored page pixels."""
    from PIL import Image
    import io
    p = load(root, sid)
    if not 1 <= page_no <= len(p["pages"]):
        raise ValueError("页码无效")
    page = paths(root, sid)[0] / "private" / "pages" / f"{page_no:04d}.png"
    with Image.open(page) as source:
        image = source.convert("RGB")
    render_candidates, render_regions = _page_render_items(p, page_no)
    _redact_image(image, render_candidates, render_regions)
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


def _valid_review_box(box, page):
    return (isinstance(box, (list, tuple)) and len(box) == 4 and
            all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box) and
            0 <= box[0] < box[2] <= page.get("width", 0) and
            0 <= box[1] < box[3] <= page.get("height", 0))


def rectangle_union_covers(target, regions, render_geometry):
    """按实际绘制像素求矩形并集，不以总外接框冒充完整覆盖。"""
    if not _valid_review_box(target, render_geometry):
        return False
    width, height = round(render_geometry["width"] * SCALE), round(render_geometry["height"] * SCALE)
    def pixels(box):
        x0, y0, x1, y1 = _clean_box(box, width, height, pad=0)
        return x0, y0, min(width, x1 + 1), min(height, y1 + 1)
    tx0, ty0, tx1, ty1 = pixels(target)
    rects = []
    for box in regions:
        if _valid_review_box(box, render_geometry):
            x0, y0, x1, y1 = pixels(box)
            rects.append((max(x0, tx0), max(y0, ty0), min(x1, tx1), min(y1, ty1)))
    rects = [r for r in rects if r[0] < r[2] and r[1] < r[3]]
    cuts = sorted({tx0, tx1} | {r[i] for r in rects for i in (0, 2)})
    for left, right in zip(cuts, cuts[1:]):
        top = ty0
        for y0, y1 in sorted((r[1], r[3]) for r in rects if r[0] <= left and r[2] >= right):
            if y0 > top:
                return False
            top = max(top, y1)
        if top < ty1:
            return False
    return bool(rects)


def _coverage_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _candidate_snapshot(item):
    return _coverage_hash({k: item.get(k) for k in
                           ("id", "type", "page", "value", "bbox", "bboxes", "source", "geometry")})


def _review_candidate(project, ref):
    if not isinstance(ref, dict) or ref.get("collection") not in ("candidates", "unlocated_candidates"):
        raise ValueError("候选引用无效")
    item = next((x for x in project.get(ref["collection"], []) if x.get("id") == ref.get("id")), None)
    if item is None:
        raise ValueError("候选已变化，请刷新")
    return item


def _link_valid(project, link):
    try:
        item = _review_candidate(project, link["candidate_ref"])
        page = next(x for x in project.get("pages", []) if x.get("page") == link.get("page"))
        regions = [next(x for x in project.get("regions", []) if x.get("id") == rid) for rid in link["region_ids"]]
        if (not regions or link.get("mode") not in ("manual", "auto") or item.get("page") not in (None, page["page"]) or
                any(r.get("page") != page["page"] or not _valid_review_box(r.get("bbox"), page) for r in regions) or
                link.get("candidate_snapshot_sha256") != _candidate_snapshot(item) or
                link.get("region_geometry_sha256") != _coverage_hash([(r["id"], r["bbox"]) for r in regions])):
            return False
        if link["mode"] == "auto":
            boxes = item.get("bboxes") or ([item["bbox"]] if item.get("bbox") else [])
            return bool(boxes) and all(rectangle_union_covers(b, [r["bbox"] for r in regions], page) for b in boxes)
        return True
    except (KeyError, TypeError, ValueError, StopIteration):
        return False


def bind_coverage(project, candidate_refs, region_ids, page, confirmed):
    if confirmed is not True or not isinstance(candidate_refs, list) or not candidate_refs or not region_ids:
        raise ValueError("请查看预览并确认所选区域已完整遮盖对应敏感文字")
    state = next((x for x in project["pages"] if x["page"] == page), None)
    regions = [next((x for x in project["regions"] if x["id"] == rid), None) for rid in region_ids]
    if state is None or any(r is None or r["page"] != page or not _valid_review_box(r.get("bbox"), state) for r in regions):
        raise ValueError("请选择当前页有效的已有遮盖框")
    prepared = []
    for ref in candidate_refs:
        item = _review_candidate(project, ref)
        if item.get("page") not in (None, page):
            raise ValueError("不能关联其他页的敏感位置")
        prepared.append((item, {"candidate_ref": dict(ref), "page": page, "region_ids": list(region_ids),
            "mode": "manual", "candidate_snapshot_sha256": _candidate_snapshot(item),
            "region_geometry_sha256": _coverage_hash([(r["id"], r["bbox"]) for r in regions]),
            "confirmed_revision": project["revision"], "validator_version": 1}))
    for item, link in prepared:
        project["coverage_links"] = [old for old in project.get("coverage_links", [])
                                      if old.get("candidate_ref") != link["candidate_ref"]] + [link]
        if link["candidate_ref"]["collection"] == "candidates":
            item.update(selected=False, resolution="manual-covered")
        else:
            item.update(status="manual-covered")
    project["review_schema"] = 2


def _normalize_review(project):
    project.setdefault("coverage_links", [])
    for collection in ("candidates", "unlocated_candidates"):
        for item in project.get(collection, []):
            if item.get("entity_confidence") is not None:
                item["confidence"] = item["entity_confidence"]
                item["score_semantics"] = "split-v1"
            elif (item.get("score_semantics") != "legacy-composite" and
                  item.get("source") != "ocr"):
                item["entity_confidence"] = item.get("confidence")
                item["score_semantics"] = "split-v1"
            else:
                item["score_semantics"] = "legacy-composite"
            if (item.get("score_semantics") == "split-v1" and
                    item.get("selection_reason") ==
                    "ocr-confidence-below-auto-select-threshold" and
                    item.get("resolution") not in ("keep", "redact", "manual-covered")):
                entity = _entity_confidence(item)
                ocr = _ocr_confidence(item)
                excluded = (item.get("policy_excluded") or
                            item.get("type") == "NUMBER_CANDIDATE")
                selected = (not excluded and _candidate_replaceable(item) and
                            entity is not None and entity >= ENTITY_AUTO_SELECT_THRESHOLD and
                            (item.get("source") != "ocr" or
                             (ocr is not None and ocr >= OCR_AUTO_SELECT_THRESHOLD)))
                item["selected"] = selected
                if selected:
                    item["selection_reason"] = "selected-by-default"
                elif entity is not None and entity < ENTITY_AUTO_SELECT_THRESHOLD:
                    item["selection_reason"] = "entity-confidence-below-auto-select-threshold"
            item["requires_manual_box"] = _candidate_requires_manual_box(item)
            item["review_reasons"] = _candidate_review_reasons(item)
    if project.get("review_schema") == 2:
        valid = [link for link in project["coverage_links"] if _link_valid(project, link)]
        project["coverage_links"] = valid
        linked = {(x["candidate_ref"]["collection"], x["candidate_ref"]["id"]) for x in valid}
        for collection in ("candidates", "unlocated_candidates"):
            for item in project.get(collection, []):
                if (collection, item.get("id")) not in linked:
                    if collection == "unlocated_candidates" and item.get("status") == "manual-covered":
                        item["status"] = "pending"
                        item.pop("region_id", None)
                    if collection == "candidates" and item.get("resolution") == "manual-covered":
                        item.pop("resolution", None)
        return
    for item in project.get("unlocated_candidates", []):
        if item.get("status") != "manual-covered":
            continue
        region = next((r for r in project.get("regions", []) if r.get("id") == item.get("region_id") and
                       r.get("resolves_id") == item.get("id")), None)
        if region:
            try:
                bind_coverage(project, [{"collection": "unlocated_candidates", "id": item["id"]}],
                              [region["id"]], region["page"], True)
            except ValueError:
                pass
    project["review_schema"] = 2


def evaluate_review(project):
    """同一快照求值：已检出的未处置项必须处理或明确保留，不能知悉后跳过。"""
    pages = {x.get("page"): x for x in project.get("pages", [])}
    valid_links = [x for x in project.get("coverage_links", []) if _link_valid(project, x)]
    linked = {(x["candidate_ref"]["collection"], x["candidate_ref"]["id"]) for x in valid_links}
    confirmed_regions = {rid for x in valid_links for rid in x["region_ids"]}
    states, pending = [], []
    policy_excluded = Policy.excluded_types(project.get("policy_id", Policy.PRESERVE_TRANSACTION))
    for collection in ("candidates", "unlocated_candidates"):
        for item in project.get(collection, []):
            reason = _candidate_review_reasons(item)
            plan_mode = "none"
            if collection == "candidates":
                plan_mode = _candidate_render_plan(item, pages.get(item.get("page")))["mode"]
            if item.get("resolution") == "keep" or item.get("status") == "keep":
                status = "keep"
            elif item.get("policy_excluded", item.get("type") in policy_excluded):
                status = "policy-excluded"
            elif (collection, item.get("id")) in linked:
                status = "manual-covered"
            elif collection == "candidates" and item.get("selected") and plan_mode != "none":
                # 选中且按真实框可绘制即为已处置；box-replace 同样是机器完成。
                status = "redact" if plan_mode == "replace" else "redact-box"
            else:
                boxes = item.get("bboxes") or ([item["bbox"]] if item.get("bbox") else [])
                page = pages.get(item.get("page"))
                regions = [r["bbox"] for r in project.get("regions", []) if r.get("page") == item.get("page") and
                           (not r.get("requires_confirmation") or r["id"] in confirmed_regions)]
                covered = bool(boxes and page) and all(rectangle_union_covers(b, regions, page) for b in boxes)
                if collection == "candidates" and covered:
                    status = "covered"
                else:
                    status = "pending"
                    if not boxes:
                        reason.append("missing_geometry")
                    elif page is None or any(not _valid_review_box(b, page) for b in boxes):
                        reason.append("invalid_geometry")
                    pending.append(item)
            states.append({"candidate_ref": {"collection": collection, "id": item.get("id")},
                           "page": item.get("page"), "state": status,
                           "render_mode": plan_mode if status in ("redact", "redact-box") else None,
                           "can_auto_redact": collection == "candidates" and plan_mode != "none",
                           "auto_applied": item.get("auto_applied") or None,
                           "reason_codes": list(dict.fromkeys(reason))})
    unreviewed = [x.get("page") for x in project.get("pages", []) if not x.get("reviewed")]
    red = []
    if project.get("stage") == "processing":
        red.append({"code": "PDF_STAGE_PROCESSING", "action": "扫描仍在进行，请等待完成后再导出"})
    failed_pages = [x.get("page") for x in project.get("pages", [])
                    if x.get("ocr") == "failed" and not x.get("manual_confirm")]
    if failed_pages:
        red.append({"code": "PDF_OCR_FAILED", "pages": failed_pages,
                    "action": "第 %s 页识别失败，未识别内容将按原文导出" %
                    "、".join(str(n) for n in failed_pages)})
    if project.get("stage") in ("failed", "cancelled"):
        red.append({"code": "PDF_SCAN_STOPPED",
                    "action": "扫描已停止，未完成识别的页面将按原文导出"})
    if pending:
        _pages = sorted({x.get("page") for x in pending if x.get("page") is not None})
        _note = ("（第" + "、".join(str(n) for n in _pages) + "页）" if _pages else "")
        red.append({"code": "PDF_DISPOSITION_REQUIRED", "count": len(pending),
                    "action": "有 %d 处敏感内容尚未处理%s，请先处理或明确保留后再导出" %
                    (len(pending), _note)})
    applied_any = (any(x.get("selected") for x in project.get("candidates", [])) or
                   project.get("regions"))
    has_findings = bool(project.get("candidates") or project.get("unlocated_candidates"))
    if has_findings and not applied_any:
        red.append({"code": "PDF_ZERO_APPLIED",
                    "action": "本次未应用任何替换或遮盖，导出稿将与原文基本一致"})
    return {"states": states, "pending": pending, "unreviewed": unreviewed,
            "reviewed_pages": len(pages) - len(unreviewed), "total_pages": len(pages),
            "pending_occurrences": len(pending), "revision": project.get("revision"),
            "hard_issues": [x for x in red if x["code"] == "PDF_DISPOSITION_REQUIRED"],
            "soft_ack_required": False,
            "red_flags": red,
            "can_export": project.get("stage") != "processing" and not pending,
            "ready_for_export": project.get("stage") != "processing" and not pending,
            "next_actions": [x["action"] for x in red]}


def _pending_sensitive(project):
    return evaluate_review(project)["pending"]


def _ocr_line_summary(page):
    stats = page.get("ocr_line_stats")
    if not isinstance(stats, dict):
        count = int(page.get("dropped_lines") or 0)
        return ("OCR行未纳入原因未记录（旧项目%d行），请人工检查" % count) if count else ""
    labels = (("empty_text", "文字为空"),
              ("box_count_mismatch", "字符与坐标框数量不符"),
              ("invalid_confidence", "OCR分数无效"),
              ("confidence_unavailable", "OCR分未提供"),
              ("low_confidence", "OCR分低于%.2f" % OCR_LINE_ACCEPT_THRESHOLD))
    rejected = ["%s：%d行" % (label, int(stats.get(key) or 0))
                for key, label in labels if int(stats.get(key) or 0) > 0]
    incomplete = int(stats.get("incomplete_geometry") or 0)
    if incomplete:
        rejected.append("坐标不完整：%d行" % incomplete)
    return "、".join(rejected)


def export_report(root, sid):
    with _lock(sid):
        return _export_report_project(load(root, sid))


def _export_report_project(p):
    """导出前检查。返回 {'hard': [], 'red': [...], 'soft': [...]}（中文条目）。

    硬拦（hard）：检出内容未处置；扫描运行中由 export() 直接拒绝。
    红档（red）：其余风险在导出前一次知悉，不得绕过硬拦。
    软提示（soft）：非阻断状态区摘要；导出区不出现坐标/几何诊断与低分提醒（要求4）。
    """
    hard, red, soft = [], [], []
    policy_excluded = Policy.excluded_types(
        p.get("policy_id", Policy.PRESERVE_TRANSACTION))
    evaluation = evaluate_review(p)
    hard.extend(flag["action"] for flag in evaluation["hard_issues"])
    red.extend(flag["action"] for flag in evaluation["red_flags"]
               if flag["code"] not in ("PDF_STAGE_PROCESSING", "PDF_DISPOSITION_REQUIRED"))
    soft.append(
        "自动识别无法穷尽文书中的全部敏感字段，识别结果也可能存在偏差；未识别到的内容将按原文保留。外发前请人工核对导出的文件。")
    soft.append(
        "PDF中的批注及批注人名称可能作为页面内容保留；工具不能单独扫描并脱敏批注人信息，请逐页核对。")
    link_count = int(p.get("link_annotation_count") or 0)
    if link_count:
        soft.append("本次导出将移除 %d 个可点击链接；页面印出的可见网址仍须脱敏或遮盖。" % link_count)
    empty = [x["page"] for x in p["pages"] if x.get("ocr") == "empty"]
    if empty:
        soft.append("第 %s 页未识别到文字，请确认确无敏感信息"
                    % "、".join(str(n) for n in empty))
    supplement_incomplete = [x for x in p["pages"]
                             if x.get("ocr_supplement") in ("failed", "empty")]
    if supplement_incomplete:
        page_labels = "、".join(str(x["page"]) for x in supplement_incomplete)
        soft.append("第 %s 页未取得补充OCR结果，请人工检查" % page_labels)
    default_off = [x for x in p["candidates"]
                   if not x.get("selected") and
                   x.get("policy_excluded", x.get("type") in policy_excluded)]
    default_off_count = len(default_off) + sum(
        1 for x in p.get("unlocated_candidates", []) if x.get("policy_excluded"))
    if default_off_count:
        soft.append("%s策略下金额、日期共 %d 条默认保留" %
                    (Policy.label(p.get("policy_id")), default_off_count))
    below_display = int((p.get("scan_stats") or {}).get("below_display_floor") or 0)
    if below_display:
        by_page = (p.get("scan_stats") or {}).get("below_display_floor_by_page") or []
        page_labels = ["第%d页%d条" % (int(row["page"]), int(row.get("count") or 0))
                       for row in by_page if row.get("page") is not None and
                       int(row.get("count") or 0) > 0]
        location_summary = "、".join(page_labels)
        suffix = "（%s）" % location_summary if location_summary else ""
        soft.append("另有 %d 条低置信命中未列入候选清单%s，请检查对应页面" %
                    (below_display, suffix))
    states = evaluation.get("states") or []
    state_count = lambda state: sum(1 for row in states if row.get("state") == state)
    replaced = state_count("redact")
    boxed = state_count("redact-box")
    covered = state_count("covered") + state_count("manual-covered")
    preserved = (state_count("keep") + state_count("policy-excluded") +
                 state_count("optional"))
    applied_states = {"redact", "redact-box", "covered", "manual-covered"}
    auto_processed = sum(1 for row in states
                         if row.get("auto_applied") and
                         row.get("state") in {"redact", "redact-box"})
    soft.append("本次处置：替换 %d 处、按框处理 %d 处、人工遮盖 %d 处；机器自动处理 %d 处；另有 %d 处候选保留原文。"
                % (replaced, boxed, covered, auto_processed, preserved))
    soft.append("导出为像素稿，不含文本层与映射文件，不能用工具还原")
    return {"hard": hard, "red": red, "soft": soft}


def export(root, sid, revision=None, confirmed=False):
    with _lock(sid):
        p = load(root, sid)
        _rules_snapshot(root, sid, p)
        # 在同一项目锁内检查，风险知悉不能越过待处置项。
        if p["stage"] == "processing":
            raise ValueError("扫描仍在进行，请等待完成后再导出")
        _report = _export_report_project(p)
        if _report["hard"]:
            raise ValueError("；".join(_report["hard"]))
        if _report["red"] and not confirmed:
            # 一次知悉仅适用于识别失败页、扫描停止和零处置等其余风险。
            return {"needs_confirm": True, "red": _report["red"], "notes": _report["soft"]}
        pages_by_no = {x.get("page"): x for x in p["pages"]}
        render_candidates = [x for x in p["candidates"]
                             if x.get("selected") and
                             _candidate_render_plan(x, pages_by_no.get(x.get("page")))["mode"] != "none"]
        source = original(root, sid, p)
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(source))
        pdf.init_forms()
        from PIL import Image
        images = []
        for i in range(len(pdf)):
            page = pdf[i]
            img = page.render(scale=SCALE).to_pil().convert("RGB")
            page.close()
            images.append(_redact_image(img,
                [x for x in render_candidates if x["page"] == i + 1],
                [x for x in p["regions"] if x["page"] == i + 1]))
        pdf.close()
        outdir = paths(root, sid)[0] / "out"
        outdir.mkdir(exist_ok=True)
        name = "脱敏文书-001.pdf"
        temp = outdir / "output.tmp.pdf"
        images[0].save(temp, "PDF", save_all=True, append_images=images[1:],
                       resolution=72 * SCALE, title="脱敏文书", author="")
        # PDF pages are newly encoded images; no source object, metadata or hidden text survives.
        if _count_link_annotations(temp):
            temp.unlink(missing_ok=True)
            raise ValueError("PDF重建后仍检测到可点击链接")
        os.replace(temp, outdir / name)
        p["output"] = name
        exported_revision = p["revision"]
        p["last_export"] = {"revision": exported_revision, "pages": len(p["pages"])}
        if p["stage"] in ("review", "exported"):
            p["stage"] = "exported"
        save(root, sid, p)
        return {"name": name, "exported_revision": exported_revision, "notes": _report["soft"]}
