#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
contract_redactor.py
====================
合同脱敏改写器 —— 读取 detection 报告 + 用户的选择（或 --types），
对 .docx / .pdf / .txt 文档执行**真正**的脱敏改写，输出新文件。

设计目标
--------
1. **运行期不联网**：首次安装依赖需要联网；安装完成后与detector共享默认开启的enforce_offline()。
2. **不损坏版式**：docx 走「run 级回填」，只在文本 run 中做字符替换；
   pdf 走「bbox 白块」，不破坏底图；txt 走「原文 diff 替换」。
3. **可审计**：输出文件旁附一份 *.redaction.log（哪些位置、用何种规则、
   替换前后值），便于事后追溯与人工复核。
4. **可对接复核页**：读取 detector 的 selection.json（用户在 HTML 上
   按高中低分类筛选后下载的文件），也可 --types X,Y 直接指定。

命令行示例
----------
    # 1) 使用 detector 输出的 .selection.json
    python contract_redactor.py 合同.docx --selection 合同.selection.json

    # 2) 直接指定类型
    python contract_redactor.py 合同.pdf --types ID_CARD,PHONE_MOBILE,PERSON_NAME

    # 3) 与 detector 配合一气呵成
    python contract_sensitive_detector.py 合同.docx --format html,selection
    python contract_redactor.py 合同.docx --selection 合同.selection.json

    # 4) 起一个本地 127.0.0.1 服务，让复核页一键调用
    python contract_redactor.py --serve --port 8765

输出
----
    <原文件名>_脱敏.<原扩展名>        脱敏后的文档
    <原文件名>_脱敏.redaction.log     改写明细（JSON Lines）
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import shutil
import socket
import tempfile
import sys
import time
import uuid
from datetime import datetime
import threading
import html as _html
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 复用 detector 的纯离线算法、校验位、类型元数据、文档解析、识别引擎。
import contract_sensitive_detector as D
import contract_review_ext as review_ext
from contract_sensitive_detector import (
    TYPE_META,
    Block,
    Document,
    DetectorEngine,
    Settings,
    extract_docx,
    extract_pdf,
    extract_text_file,
    is_placeholder,
    load_allowlist,
    load_rules_md,
    mask_value,
)

# --------------------------------------------------------------------- 常量 --

__version__ = "1.3.0-dev"
ENGINE_NAME = "contract_redactor"


# --------------------------------------------------------------------- 数据结构


@dataclass
class RedactOp:
    """一次改写动作：在 [start, end) 区间内用 replacement 替换原文。"""
    start: int                  # 原始全文偏移（来自 detection 报告）
    end: int
    type_id: str
    label: str
    severity: str
    confidence: float
    value: str                  # 该次出现的真实原文
    replacement: str            # 替换后
    location: str = ""
    page: Optional[int] = None
    bbox: Optional[List[float]] = None
    entity_value: str = ""      # 所属实体的代表值（同一实体编号一致，用它查号）
    block_index: int = -1
    local_start: int = -1
    local_end: int = -1
    target: Optional[Dict[str, Any]] = None
    overlap_types: List[str] = field(default_factory=list)
    overlap_labels: List[str] = field(default_factory=list)
    overlap_masks: List[str] = field(default_factory=list)
    overlap_originals: List[str] = field(default_factory=list)
    masked_value: str = ""
    source_op: Optional["RedactOp"] = field(default=None, repr=False, compare=False)


def _skipped_operation_detail(op: RedactOp) -> Dict[str, Any]:
    op = op.source_op or op
    return {"type": op.type_id, "label": op.label, "value": op.value,
            "location": op.location, "block_index": op.block_index}


def _discard_skipped_operations(operations: List[RedactOp],
                                mapping_records: List[Dict[str, Any]],
                                skipped_ops: Sequence[RedactOp]) -> Tuple[List[RedactOp], List[Dict[str, Any]]]:
    skipped_ids = set()
    skipped_keys = set()
    for candidate in skipped_ops:
        op = candidate.source_op or candidate
        skipped_ids.add(id(op))
        skipped_keys.add((op.start, op.end, op.type_id, op.value))
    if not skipped_keys:
        return operations, mapping_records
    operations = [op for op in operations if id(op) not in skipped_ids]
    mapping_records = [row for row in mapping_records
                       if (row.get("start"), row.get("end"), row.get("type"),
                           row.get("original")) not in skipped_keys]
    return operations, mapping_records


# --------------------------------------------------------------------- 文档重写器


class BaseRedactor:
    """改写器接口。每种格式一个子类。"""

    file_type: str = ""

    def __init__(self, source: str, full_text: str, ops: List[RedactOp]):
        self.source = source
        self.full_text = full_text
        self.ops = sorted(ops, key=lambda o: o.start, reverse=True)  # 从后往前改
        # 因检测/改写两侧段落文本口径不一致而被 fail-safe 跳过的段落（宁漏不错）
        self.unstable_paragraphs: List[Dict[str, Any]] = []

    # 子类必须实现
    def rewrite(self, dst: str) -> Dict[str, Any]:
        raise NotImplementedError

    # 通用工具：把 ops 应用到一段纯文本（txt/解析后内容）
    def apply_to_text(self, text: str) -> Tuple[str, int, int]:
        applied, skipped = 0, 0
        out = text
        # 由后向前避免偏移变化
        for op in sorted(self.ops, key=lambda o: o.start, reverse=True):
            seg = text[op.start:op.end]
            if seg != op.value:
                # 漂移防御：若当前文本不再匹配，跳过
                skipped += 1
                continue
            out = out[:op.start] + op.replacement + out[op.end:]
            applied += 1
        return out, applied, skipped


# ---------------------------------------------- DOCX 改写（run 级 XML 回填）


class DocxRedactor(BaseRedactor):
    """docx 改写：在段落级别替换文本，保留原有 run 的样式。

    算法
    ----
    1. detector 把每个段落当成一个 block，block 文本 = 该段落所有
       `<w:t>` 节点的 text 拼接（去掉 \\n）。
    2. 本改写器按相同顺序遍历段落（含页眉页脚），
       对每个段落：
         a) 收集所有 `<w:t>`，拼成 para_text；
         b) 校验 para_text == block_text；漂移则跳过；
         c) 对每个 op，把它映射到若干 `<w:t>` 上的字符区间，
            再把 replacement 按比例切分到这些 `<w:t>` 中替换。

    跨多个 `<w:t>` 的 op（如一个手机号被拆在两个 run 里）会被切分成
    「第一段 + 后续段」形式写入。replacement 按各段原字符数比例切。
    """
    file_type = "docx"

    W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def rewrite(self, dst: str) -> Dict[str, Any]:
        # 拷贝原文件到 dst，再就地修改
        shutil.copyfile(self.source, dst)

        try:
            from docx import Document as DocxDocument  # type: ignore
            from docx.oxml.ns import qn
        except ImportError as exc:
            raise RuntimeError("改写 docx 需要 python-docx") from exc

        doc = DocxDocument(dst)
        # 收集所有段落（含正文 + 页眉页脚），按 detector 的 block 顺序对齐
        detector_doc = D.extract_docx(self.source, include_headers=True)
        block_texts = [b.text.rstrip("\n") for b in detector_doc.blocks]

        # 在 self.full_text 中定位每个 block 的精确 [start, end)
        full = self.full_text
        block_spans: List[Tuple[int, int]] = []
        cursor = 0
        for txt in block_texts:
            i = full.find(txt, cursor)
            if i < 0:
                i = full.find(txt)
                if i < 0:
                    # 漂移：用启发式估算位置（粗略即可，跳过）
                    block_spans.append((-1, -1))
                    continue
            block_spans.append((i, i + len(txt)))
            cursor = i + len(txt)

        # 把 ops 按 block 分类，并转换成本 block 内偏移
        ops_by_block: Dict[int, List[RedactOp]] = {}
        skipped: List[RedactOp] = []
        for op in self.ops:
            placed = False
            for bi, (bs, be) in enumerate(block_spans):
                if bs < 0:
                    continue
                if op.start >= bs and op.end <= be:
                    op_local = RedactOp(
                        start=op.start - bs,
                        end=op.end - bs,
                        type_id=op.type_id, label=op.label,
                        severity=op.severity, confidence=op.confidence,
                        value=op.value, replacement=op.replacement,
                        location=op.location, page=op.page, bbox=op.bbox,
                        block_index=op.block_index, target=op.target,
                        source_op=op,
                    )
                    ops_by_block.setdefault(bi, []).append(op_local)
                    placed = True
                    break
            if not placed:
                skipped.append(op)

        # 段落元素列表（与 detector 的 block 顺序对齐）
        # detector 在 extract_docx 里**跳过空段落**，所以这里也要跳过，
        # 否则 paragraph index 会对不上 detector 的 block index。
        # 判空口径与检测侧完全一致（docx_para_text）。
        para_seq = [paragraph for _kind, _location, paragraph
                    in D.docx_para_targets(doc)]

        applied: List[RedactOp] = []
        for bi, ops in ops_by_block.items():
            if bi >= len(para_seq):
                skipped.extend(ops)
                continue
            ok, paragraph_skipped = self._replace_in_paragraph(
                para_seq[bi], block_texts[bi], ops)
            applied.extend(ok)
            skipped.extend(paragraph_skipped)

        # 段落级 _replace_in_paragraph 已经写回 .text，最后保存
        doc.save(dst)
        warnings: List[Dict[str, Any]] = []
        if self.unstable_paragraphs:
            warnings.append({
                "code": "PARAGRAPH_TEXT_MISMATCH",
                "count": len(self.unstable_paragraphs),
                "detail": "检测与改写两侧段落文本口径不一致，已跳过这些段落（宁漏不错）",
                "paragraphs": self.unstable_paragraphs[:10],
            })
        return {
            "applied": len(applied),
            "skipped": len(skipped) + (len(self.ops) - len(applied) - len(skipped)),
            "warnings": warnings,
            "skipped_ops": [op.source_op or op for op in skipped],
        }

    # ---- 段落内字符级回填 ----
    def _replace_in_paragraph(self, para_elem, block_text: str,
                              ops: List[RedactOp]) -> Tuple[List[RedactOp], List[RedactOp]]:
        """在段落 para_elem 上把 ops 应用进去（按字符 offset）。"""
        from contract_docx_revisions import iter_story_content_nodes
        ts = [node for node in iter_story_content_nodes(para_elem)
              if node.tag == self.W_NS + "t"]
        if not ts:
            return [], list(ops)

        # 段落纯文本：与检测侧**同一口径**（tab→空格、br→换行）。
        para_text_full, units = D.docx_para_units(para_elem)
        para_text = para_text_full.rstrip("\n")

        # fail-safe（R6）：两侧口径必须一致。历史缺陷正是「检测折算 tab、改写忽略 tab」，
        # 使替换区间整体右移——明文残留（如手机号前 5 位）且正文被破坏，比漏检更危险。
        # 一旦不一致就整段跳过：漏检可见、可人工补录。
        if para_text != block_text:
            self.unstable_paragraphs.append(
                {"paragraph_text": para_text[:80], "detector_text": block_text[:80]})
            return [], list(ops)

        applied: List[RedactOp] = []
        skipped: List[RedactOp] = []
        # 从后往前处理，避免前面的 offset 偏移影响后面的判断
        ops_sorted = sorted(ops, key=lambda o: o.start, reverse=True)
        # 先把每个 <w:t> 的字符切成 list，便于字符级切片
        pieces: List[List[str]] = [list(t.text or "") for t in ts]

        for op in ops_sorted:
            if op.start < 0 or op.end > len(para_text):
                skipped.append(op)
                continue
            # [start, end) 切成同一 <w:t> 内的连续段；tab/br 保留位不参与替换，
            # 因此制表符、换行、填空下划线的数量与位置保持不变。
            segments: List[Tuple[int, int, int]] = _segments_in_units(units, op.start, op.end)
            total_len = sum(r - l for _, l, r in segments)
            n_seg = len(segments)
            if total_len == 0 or n_seg == 0:
                skipped.append(op)
                continue
            if n_seg == 1:
                parts = [op.replacement]
            else:
                seg_lens = [r - l for _, l, r in segments]
                parts = self._split_replacement(op.replacement, seg_lens)

            # 写入各 <w:t>
            for (ti, l, r), part in zip(segments, parts):
                pieces[ti][l:r] = list(part)
            applied.append(op)

        # 写回 <w:t>.text
        for t, piece in zip(ts, pieces):
            t.text = "".join(piece)
        return applied, skipped

    @staticmethod
    def _split_replacement(rep: str, seg_lens: List[int]) -> List[str]:
        """把 rep 按 seg_lens 长度比例切分成多段。"""
        total = sum(seg_lens)
        if total == 0 or len(seg_lens) == 1:
            return [rep]
        # 每段字符数（按比例，首末段至少 1）
        n = len(rep)
        if n <= len(seg_lens):
            # replacement 比段数还短：每段 1 个，最后一段吃剩下的
            parts = [rep[i] if i < n else "" for i in range(len(seg_lens))]
            return parts
        # 正常按比例切
        sizes = [max(1, round(n * L / total)) for L in seg_lens]
        # 修正：总和与 n 的差异
        diff = n - sum(sizes)
        i = 0
        while diff != 0 and sizes:
            if diff > 0:
                sizes[i % len(sizes)] += 1
                diff -= 1
            elif sizes[i % len(sizes)] > 1:
                sizes[i % len(sizes)] -= 1
                diff += 1
            i += 1
            if i > 100:
                break
        # 按 sizes 切片
        out: List[str] = []
        cur = 0
        for s in sizes[:-1]:
            out.append(rep[cur:cur + s])
            cur += s
        out.append(rep[cur:])
        return out


# ---------------------------------------------- PDF 改写（bbox 白块 + 可选文字层）


class PdfRedactor(BaseRedactor):
    """pdf 改写：对含命中的页面**栅格化**为图片，在图片上画白块，
    再用 reportlab 把图片贴回 PDF。

    为什么栅格化？
    --------------
    法律 / 财务场景要求 PDF 脱敏后**绝对**不能从文本层恢复原数据。
    简单地「在原内容流上盖白块」会让原文本仍保留在 PDF 文本层里，
    复制粘贴就能泄密。这里采用「命中页 → 渲染成位图 → 画白块 →
    重新合成 PDF」的路径，输出 PDF 不含原文本，安全级别最高。
    """
    file_type = "pdf"

    def rewrite(self, dst: str) -> Dict[str, Any]:
        try:
            import pypdfium2 as pdfium  # type: ignore
            from PIL import Image, ImageDraw  # type: ignore
            from reportlab.pdfgen import canvas  # type: ignore
            from pypdf import PdfReader, PdfWriter  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PDF 栅格化需要 pypdfium2 + Pillow + reportlab") from exc

        # 1. 把所有 op 按 page 分类
        ops_by_page: Dict[int, List[RedactOp]] = {}
        for op in self.ops:
            if not op.bbox or op.page is None:
                continue
            ops_by_page.setdefault(op.page, []).append(op)

        applied = sum(len(v) for v in ops_by_page.values())
        skipped_ops = [op for op in self.ops if not op.bbox or op.page is None]
        skipped = len(skipped_ops)

        # 2. 没有命中 → 直接拷贝原文件
        if not ops_by_page:
            shutil.copyfile(self.source, dst)
            return {"applied": 0, "skipped": skipped,
                    "skipped_ops": skipped_ops}

        # 3. 用 pypdfium2 把每一页渲染成 PIL Image（命中页用 200 dpi 保证清晰）
        pdf = pdfium.PdfDocument(self.source)
        new_pages_bytes: List[bytes] = []
        for i, page in enumerate(pdf, 1):
            page_w = float(page.get_width())
            page_h = float(page.get_height())
            ops = ops_by_page.get(i, [])
            if not ops:
                # 未命中页：从原 PDF 直接抓内容（保留原文本层，搜索性最好）
                new_pages_bytes.append(self._copy_page_bytes(self.source, i - 1))
                continue
            # 命中页 → 栅格化 → 画白块
            pil_img = page.render(scale=200 / 72).to_pil()  # 200 dpi
            draw = ImageDraw.Draw(pil_img)
            scale_x = pil_img.width / page_w
            scale_y = pil_img.height / page_h
            for op in ops:
                x0, y0, x1, y1 = op.bbox  # type: ignore[misc]
                # bbox 来自 pdfplumber，原点在左上 → PIL 也在左上
                px0 = x0 * scale_x
                py0 = y0 * scale_y
                px1 = x1 * scale_x
                py1 = y1 * scale_y
                draw.rectangle([px0, py0, px1, py1], fill=(255, 255, 255),
                               outline=(220, 220, 220))
                # 写掩码文字（便于复核）
                try:
                    from PIL import ImageFont
                    f_h = max(8, int((py1 - py0) * 0.55))
                    font = ImageFont.load_default(size=f_h) if hasattr(ImageFont, "load_default") else None
                    txt = op.replacement[:24]
                    if font:
                        draw.text((px0 + 2, py0 + (py1 - py0 - f_h) / 2),
                                  txt, fill=(120, 120, 120), font=font)
                    else:
                        draw.text((px0 + 2, py0 + 2), txt, fill=(120, 120, 120))
                except Exception:
                    pass
            # 把 PIL Image 编码为 PNG bytes
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            new_pages_bytes.append(buf.getvalue())

        # 4. 用 reportlab 把每页（位图或原页）合成到新 PDF
        tmp_path = dst + ".tmp.pdf"
        c = canvas.Canvas(tmp_path)
        for i, page in enumerate(pdf, 1):
            page_w = float(page.get_width())
            page_h = float(page.get_height())
            c.setPageSize((page_w, page_h))
            if i in ops_by_page:
                # 命中页：贴位图（占满整页）
                from reportlab.lib.utils import ImageReader  # type: ignore
                img_reader = ImageReader(io.BytesIO(new_pages_bytes[i - 1]))
                c.drawImage(img_reader, 0, 0, width=page_w, height=page_h)
            else:
                # 未命中页：把原页内容贴过来（用 pypdf 提取原页 → 转 image 贴）
                # 简化处理：原页也栅格化（牺牲原页的搜索性换取代码简单）
                pil_img = page.render(scale=200 / 72).to_pil()
                buf = io.BytesIO(); pil_img.save(buf, format="PNG")
                from reportlab.lib.utils import ImageReader
                img_reader = ImageReader(io.BytesIO(buf.getvalue()))
                c.drawImage(img_reader, 0, 0, width=page_w, height=page_h)
            c.showPage()
        c.save()
        # 5. 改名
        shutil.move(tmp_path, dst)
        return {"applied": applied, "skipped": skipped,
                "skipped_ops": skipped_ops}

    def _copy_page_bytes(self, src_path: str, page_index: int) -> bytes:
        """从原 PDF 抽取第 page_index 页的原始内容流（bytes）。"""
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(src_path)
        page = reader.pages[page_index]
        # 通过 reportlab 直接复制页面内容需要更复杂的处理；
        # 简化：返回 None，调用方走栅格化分支
        return b""


# ---------------------------------------------- TXT 改写（直接字符串替换）


class TextRedactor(BaseRedactor):
    """txt/md/纯文本改写：直接按 char-offset 做字符串切片替换。"""
    file_type = "txt"

    def rewrite(self, dst: str) -> Dict[str, Any]:
        # 优先用 detector 给出的 full_text（已对齐 offset）；仅当不可用时才读源文件
        text = self.full_text
        if not text:
            with open(self.source, "r", encoding="utf-8") as f:
                text = f.read()
        new_content, applied, skipped = self.apply_to_text(text)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(new_content)
        skipped_ops = [op for op in self.ops
                       if not (0 <= op.start <= op.end <= len(text)) or
                       text[op.start:op.end] != op.value]
        return {"applied": applied, "skipped": skipped,
                "skipped_ops": skipped_ops}


# --------------------------------------------------- 把 detector 输出转成 ops --


def build_ops_from_report(report: Dict[str, Any], enabled_types: set,
                          enabled_items: Optional[List[Dict[str, Any]]] = None,
                          return_skipped: bool = False):
    """从 detector 的 .sensitive.json 报告 + 用户勾选的类型集合构造 ops。

    enabled_items：复核页里被逐个保留的「位置」清单，元素形如
        {"type": "PERSON_NAME", "offset": [start, end]}
    start/end 是该次出现在全文里的**绝对偏移**（复核页 mark 的 s/e）。
    注意不能用「实体内部的 occurrence 序号」做键 —— 每个实体的序号都从 0 重新开始，
    会把所有实体的第一处误判为同一处（踩过的坑）。
    传 None 表示不按位置过滤（命令行 --types 的用法）。
    """
    keep: Optional[set] = None
    if enabled_items is not None:
        keep = set()
        for mi in enabled_items:
            t = mi.get("type")
            off = mi.get("offset")
            if not t or not isinstance(off, (list, tuple)) or len(off) != 2:
                continue
            keep.add((t, int(off[0]), int(off[1])))

    ops: List[RedactOp] = []
    skipped: List[RedactOp] = []
    for it in report["items"]:
        if it["type"] not in enabled_types:
            continue
        replacement = mask_value(it["value"], it["type"])
        for o in it["occurrences"]:
            if keep is not None and (it["type"], o["offset"][0], o["offset"][1]) not in keep:
                continue
            occurrence_target = copy.deepcopy(o.get("target"))
            if occurrence_target and occurrence_target.get("rewrite_supported") is False:
                if not return_skipped:
                    raise ValueError(occurrence_target.get("rewrite_block_reason") or
                                     "该修订目标无法安全定点脱敏")
                skipped.append(RedactOp(
                    start=o["offset"][0], end=o["offset"][1],
                    type_id=it["type"], label=it["label"],
                    severity=it["severity"], confidence=it["confidence"],
                    value=o.get("text") or it["value"], replacement=replacement,
                    location=o.get("location", ""), page=o.get("page"),
                    bbox=o.get("bbox"), entity_value=it["value"],
                    block_index=o.get("block_index", -1),
                    local_start=(o.get("local_offset") or [-1, -1])[0],
                    local_end=(o.get("local_offset") or [-1, -1])[1],
                    target=occurrence_target))
                continue
            ops.append(RedactOp(
                start=o["offset"][0],
                end=o["offset"][1],
                type_id=it["type"],
                label=it["label"],
                severity=it["severity"],
                confidence=it["confidence"],
                # 真实字面文本：同一实体的不同写法（全称/简称、带不含空格）各自还原
                value=o.get("text") or it["value"],
                replacement=replacement,
                location=o.get("location", ""),
                page=o.get("page"),
                bbox=o.get("bbox"),
                entity_value=it["value"],
                block_index=o.get("block_index", -1),
                local_start=(o.get("local_offset") or [-1, -1])[0],
                local_end=(o.get("local_offset") or [-1, -1])[1],
                target=occurrence_target,
            ))

    if keep and not ops and not skipped:
        raise ValueError("勾选的位置与当前文档不匹配，请重新上传并复核")

    full_text = report.get("_full_text", "")
    return (ops, full_text, skipped) if return_skipped else (ops, full_text)


def _docx_physical_positions(op: RedactOp) -> Optional[Tuple[Tuple[str, str, str, int], ...]]:
    target = op.target or {}
    part = str(target.get("part", ""))
    positions = set()
    for segment in target.get("physical_segments") or []:
        path = str(segment.get("node_path", ""))
        kind = str(segment.get("node_kind", ""))
        start, end = int(segment.get("start", -1)), int(segment.get("end", -1))
        if not part or not path or not kind or start < 0 or end <= start:
            return None
        positions.update((part, path, kind, offset) for offset in range(start, end))
    return tuple(sorted(positions)) if positions else None


def _docx_operation_positions(op: RedactOp) -> Optional[Tuple[Tuple[str, str, str, int], ...]]:
    physical = _docx_physical_positions(op)
    if physical:
        return physical
    target_kind = str((op.target or {}).get("kind", ""))
    if target_kind in {"external_relationship_target", "vml_textpath"}:
        start, end = op.local_start, op.local_end
        if start < 0 or end <= start:
            return None
        block = "@target:%r" % (_docx_operation_scope(op),)
    else:
        start, end = op.start, op.end
        if op.block_index < 0 or start < 0 or end <= start:
            return None
        block = "@block:%d" % op.block_index
    return tuple((block, "#text", "text", offset)
                 for offset in range(start, end))


def _docx_operation_scope(op: RedactOp) -> Tuple[Any, ...]:
    target = op.target or {}
    kind = str(target.get("kind", "text"))
    keys = ("part", "paragraph_index", "revision_kind", "revision_id",
            "revision_ordinal", "revision_view", "revision_scope", "attribute",
            "comment_id", "element_index", "relationship_id", "node_path")
    identity = tuple((key, str(target.get(key, ""))) for key in keys
                     if key in target)
    if not target and op.block_index >= 0:
        return ("block", op.block_index)
    return (kind, identity, op.block_index)


def _merge_physical_segments(members: Sequence[RedactOp]) -> List[Dict[str, Any]]:
    order: List[Tuple[str, str, str]] = []
    offsets: Dict[Tuple[str, str, str], set] = {}
    for member in sorted(members, key=lambda op: (op.start, op.end)):
        for segment in (member.target or {}).get("physical_segments") or []:
            key = (str((member.target or {}).get("part", "")),
                   str(segment.get("node_path", "")),
                   str(segment.get("node_kind", "")))
            if key not in offsets:
                order.append(key)
                offsets[key] = set()
            offsets[key].update(range(int(segment.get("start", -1)),
                                      int(segment.get("end", -1))))
    merged: List[Dict[str, Any]] = []
    for part, path, kind in order:
        values = sorted(value for value in offsets[(part, path, kind)] if value >= 0)
        if not values:
            continue
        start = previous = values[0]
        for value in values[1:]:
            if value != previous + 1:
                merged.append({"part": part, "node_path": path, "node_kind": kind,
                               "start": start, "end": previous + 1})
                start = value
            previous = value
        merged.append({"part": part, "node_path": path, "node_kind": kind,
                       "start": start, "end": previous + 1})
    return merged


def _coalesce_exact_docx_targets(operations: List[RedactOp],
                                 restore_mode: bool) -> List[RedactOp]:
    """Coalesce same-position categories and safe textual interval overlaps."""
    rows = []
    for index, op in enumerate(operations):
        positions = _docx_operation_positions(op)
        rows.append({"index": index, "op": op, "scope": _docx_operation_scope(op),
                     "positions": set(positions or ())})

    clusters: List[List[Dict[str, Any]]] = []
    for row in rows:
        if not row["positions"]:
            clusters.append([row])
            continue
        pending = [row]
        changed = True
        while changed:
            changed = False
            for cluster in list(clusters):
                if any(member["scope"] == pending[0]["scope"] and
                       member["positions"] & item["positions"]
                       for member in cluster for item in pending):
                    pending.extend(cluster)
                    clusters.remove(cluster)
                    changed = True
        clusters.append(pending)

    merged_ops: List[RedactOp] = []
    for cluster in clusters:
        members = [row["op"] for row in cluster]
        positions = [row["positions"] for row in cluster]
        if len(members) == 1:
            merged_ops.append(members[0])
            continue
        unique: List[RedactOp] = []
        seen = set()
        for member in members:
            identity = (member.type_id, member.replacement, member.start,
                        member.end, member.value)
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(member)
        if len(unique) == 1:
            merged_ops.append(unique[0])
            continue

        ordered = sorted(unique, key=lambda op: (
            -int(getattr(D.TYPE_META.get(op.type_id), "priority", 0)),
            op.start, op.end, op.type_id))
        exact = all(row["positions"] == positions[0] for row in cluster)
        same_value = all(op.value == ordered[0].value for op in unique)
        same_range = all(op.start == ordered[0].start and
                         op.end == ordered[0].end for op in unique)
        text_kinds = {"text", "revision_visible_text", "revision_rejected_text",
                      "revision_deleted_text", "comment_body",
                      "external_relationship_target", "vml_textpath"}
        if not exact:
            if any(str((op.target or {}).get("kind", "text")) not in text_kinds
                   for op in unique):
                raise ValueError("DOCX写回目标存在无法安全合并的部分重叠")
            target_kind = str((unique[0].target or {}).get("kind", "text"))
            if target_kind in {"external_relationship_target", "vml_textpath"}:
                spans = [(op.local_start, op.local_end) for op in unique]
                if any(start < 0 or end <= start or
                       (op.start - start) != (op.end - end)
                       for op, (start, end) in zip(unique, spans)):
                    raise ValueError("DOCX特殊目标的文字范围与物理范围不一致")
            else:
                spans = [(op.start, op.end) for op in unique]
            union_start = min(start for start, _ in spans)
            union_end = max(end for _, end in spans)
            union_chars: List[Optional[str]] = [None] * (union_end - union_start)
            logical_overlap = False
            for op, (start, end) in zip(unique, spans):
                if end - start != len(op.value):
                    raise ValueError("DOCX部分重叠候选范围与原文长度不一致")
                for offset, char in enumerate(op.value, start=start - union_start):
                    existing = union_chars[offset]
                    if existing is not None and existing != char:
                        raise ValueError("DOCX部分重叠候选原文不一致，已停止导出")
                    if existing is not None:
                        logical_overlap = True
                    union_chars[offset] = char
            if not logical_overlap:
                raise ValueError("DOCX物理定位与文字范围不一致，已停止导出")
            if any(char is None for char in union_chars):
                raise ValueError("DOCX部分重叠目标无法组成连续原文")
            union_value = "".join(char for char in union_chars if char is not None)
        else:
            if not same_value or not same_range:
                raise ValueError("DOCX完全重合目标对应原文不一致，已停止导出")
            union_value = ordered[0].value
            union_end = ordered[0].end

        exact_range = exact and same_range
        leader = (ordered[0] if exact_range else max(
            unique, key=lambda op: (op.end - op.start,
                                    int(getattr(D.TYPE_META.get(op.type_id), "priority", 0)))))
        merged = copy.deepcopy(leader)
        merged.start = min(op.start for op in unique)
        merged.end = max(op.end for op in unique)
        merged.local_start = min((op.local_start for op in unique if op.local_start >= 0),
                                 default=leader.local_start)
        merged.local_end = max((op.local_end for op in unique if op.local_end >= 0),
                               default=leader.local_end)
        merged.value = union_value
        merged.overlap_types = list(dict.fromkeys(op.type_id for op in ordered))
        merged.overlap_labels = list(dict.fromkeys(op.label for op in ordered))
        merged.overlap_masks = list(dict.fromkeys(
            op.masked_value or op.replacement for op in ordered))
        merged.overlap_originals = list(dict.fromkeys(op.value for op in ordered))
        merged.masked_value = ("".join(op.masked_value or op.replacement for op in ordered)
                               if exact_range else
                               (leader.masked_value or leader.replacement))
        merged.replacement = ("".join(op.replacement for op in ordered)
                              if restore_mode and exact_range else leader.replacement)
        if any((op.target or {}).get("physical_segments") for op in unique):
            merged.target = copy.deepcopy(leader.target or {})
            merged.target["physical_segments"] = _merge_physical_segments(unique)
        if merged.target and merged.local_start >= 0 and merged.local_end > merged.local_start:
            merged.target["local_offset"] = [merged.local_start, merged.local_end]
        merged_ops.append(merged)

    claimed: Dict[Tuple[str, str, str, int], int] = {}
    for op in merged_ops:
        positions = _docx_operation_positions(op)
        if not positions:
            continue
        for position in positions:
            previous = claimed.get(position)
            if previous is not None and previous != id(op):
                raise ValueError("DOCX写回目标存在部分重叠，已停止导出")
            claimed[position] = id(op)
    return sorted(merged_ops, key=lambda op: (op.start, op.end, op.type_id))


def load_selection(path: str) -> Dict[str, Any]:
    """读取 detector 复核页导出的 selection.json。

    兼容两种形态：
      - detector v1 直接的 {"source":..., "enabled_types":[...]}
      - detector 复核页的 selection（已封装）
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_report(report_path: str) -> Dict[str, Any]:
    """读取 detector 的 *.sensitive.json（包含每条 occurrence 的 offset/bbox/value）。"""
    with open(report_path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------- 主流程：检测 → 选择 → 改写


def _expected_plain_story_text(source, full_text, operations,
                               return_skipped=False):
    """预先计算普通文字的物理段落结果，允许明确保留的同值位置。"""
    from docx import Document as DocxDocument
    from contract_docx_revisions import iter_story_content_nodes
    document = DocxDocument(source)
    paragraphs = list(D.docx_para_targets(document))
    roots = {part.element: str(part.partname).lstrip("/")
             for part in document.part.package.parts if hasattr(part, "element")}
    manifest, cursor, consumed, skipped = [], 0, set(), []
    for _, _, paragraph in paragraphs:
        text, units = D.docx_para_units(paragraph)
        normalized = text.rstrip("\n")
        start = full_text.find(normalized, cursor)
        if start < 0:
            raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：正文段落无法与扫描对齐")
        cursor = start + len(normalized)
        nodes = [n for n in iter_story_content_nodes(paragraph) if n.tag == DocxRedactor.W_NS + "t"]
        pieces = [list(n.text or "") for n in nodes]
        local = [(i, op) for i, op in enumerate(operations)
                 if start <= op.start < op.end <= start + len(normalized)]
        last = len(normalized)
        for index, op in sorted(local, key=lambda pair: pair[1].start, reverse=True):
            left, right = op.start - start, op.end - start
            if right > last or normalized[left:right] != op.value:
                skipped.append(op)
                continue
            last = left
            segments = _segments_in_units(units, left, right)
            if not segments:
                skipped.append(op)
                continue
            replacements = DocxRedactor._split_replacement(op.replacement, [b-a for _, a, b in segments])
            for (ni, a, b), replacement in zip(segments, replacements):
                pieces[ni][a:b] = list(replacement)
            consumed.add(index)
        expected, ni = [], 0
        for node in iter_story_content_nodes(paragraph):
            if node.tag == DocxRedactor.W_NS + "t":
                expected.append("".join(pieces[ni])); ni += 1
            elif node.tag == DocxRedactor.W_NS + "tab": expected.append(" ")
            elif node.tag in (DocxRedactor.W_NS + "br", DocxRedactor.W_NS + "cr"): expected.append("\n")
        root = paragraph.getroottree().getroot()
        if root not in roots:
            raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：正文部件无法定位")
        node, path = paragraph, []
        while node is not root:
            container = node.getparent(); path.append(container.index(node)); node = container
        manifest.append({"part": roots[root], "path": list(reversed(path)),
                         "sha256": hashlib.sha256("".join(expected).encode("utf-8")).hexdigest()})
    skipped_ids = {id(op) for op in skipped}
    skipped.extend(op for index, op in enumerate(operations)
                   if index not in consumed and id(op) not in skipped_ids)
    return (manifest, skipped) if return_skipped else manifest


def redact_file(source: str,
                selection: Optional[Dict[str, Any]] = None,
                types: Optional[Sequence[str]] = None,
                out_path: Optional[str] = None,
                rules_path: Optional[str] = None,
                min_confidence: float = 0.5,
                min_severity: str = "low",
                allowlist: Optional[Sequence[str]] = None,
                write_log: bool = True,
                restore_mode: bool = False,
                custom_terms: Optional[List[Dict[str, Any]]] = None,
                case_numbers: Optional[Dict[Tuple[str, str], int]] = None,
                external_clean: bool = False,
                litigation_profile: bool = False,
                unified_profile: bool = False,
                revision_mode: Optional[str] = None,
                comment_mode: Optional[str] = None,
                low_confidence_items: Optional[List[Dict[str, Any]]] = None,
                confirmation: Optional[Dict[str, Any]] = None,
                allow_no_replacements: bool = False,
                revision_acknowledged: bool = False,
                revision_ack_digest: Optional[str] = None,
                unredacted_content_acknowledged: bool = False,
                unredacted_content_digest: Optional[str] = None,
                external_relationship_action: str = "remove",
                external_relationship_acknowledged: bool = False,
                external_relationship_digest: Optional[str] = None,
                ) -> Dict[str, Any]:
    """对单个文件执行「检测 → 选择 → 改写」的端到端流程。

    selection 与 types 至少要有一个。

    restore_mode=True 时
    ------------------
    改写后的 docx / pdf 把所有替换项写成 [label#N] 占位符，并**额外**在同目录
    输出 <stem>.mapping.json，之后可用 restore_file() 一次性还原回原文
    （占位符 ↔ 原文一一对应）。当源是 .pdf 时，占位符会落在栅格化后的图片层上，
    所以 **PDF 模式仅占位生成 mapping，不实际写「占位 docx」**；可还原的始终是 docx。
    """
    if not selection and not types and not allow_no_replacements:
        raise ValueError("必须提供 --selection 或 --types")

    ext = os.path.splitext(source)[1].lower()
    if comment_mode is None:
        comment_mode = "redact_preserve"
    if ext == ".docx" and comment_mode not in (
            "redact_preserve", "preserve_unredacted", "remove_all"):
        raise ValueError("DOCX批注处理方式无效")
    comment_info = None
    if ext == ".docx":
        from contract_docx_comments import inspect_comment_package
        comment_info = inspect_comment_package(source)

    # 1) 同步 detector 的规则与设置
    extra_specs: List = []
    if rules_path:
        extra_specs = load_rules_md(rules_path)
    if litigation_profile:
        extra_specs.extend(review_ext.litigation_specs())

    settings = Settings(min_confidence=min_confidence, min_severity=min_severity)
    settings.extra_specs = extra_specs
    if unified_profile:
        review_ext.configure_unified_settings(settings)
    elif litigation_profile:
        settings.extra_specs.extend(review_ext.litigation_specs())
        settings.extra_type_meta.update(review_ext.litigation_type_meta())
    if allowlist:
        settings.allowlist = set(allowlist)
    # v1.3.0「低置信可见可勾」：只有复核页路径（selection 带逐个位置白名单 items）才保留
    # min_confidence 以下的候选 —— 否则用户在复核页勾选的「把握较低」项会因低于门槛
    # 而在重扫时消失，导致「勾选的位置与当前文档不匹配」。真正是否替换仍由 items 白名单决定。
    if selection and selection.get("items") is not None:
        settings.retain_low_confidence = True

    # 2) 用 detector 跑一次（保证报告与 detector 完全对齐）
    engine = DetectorEngine(settings)
    doc = _load_doc(source)
    revision_analysis = doc.revision_analysis or {}
    revision_inventory = revision_analysis.get("inventory") or {}
    has_revisions = bool(ext == ".docx" and revision_inventory.get("has_revisions"))
    unknown_revision_passthrough = bool(
        ext == ".docx" and has_revisions and
        (revision_analysis.get("inventory_scope_unknown") or
         revision_inventory.get("inventory_scope_unknown")))
    unknown_revision_digest = None
    if unknown_revision_passthrough:
        from contract_docx_revisions import revision_passthrough_digest
        unknown_revision_digest = revision_passthrough_digest(source)
    unsupported_revisions = revision_analysis.get("unsupported_manifest") or []
    supported_revision_count = (
        int(revision_analysis.get("supported_text_revision_count") or 0) +
        int(revision_analysis.get("supported_property_revision_count") or 0))
    current_unsupported_digest = str(revision_inventory.get("unsupported_digest") or "")
    unredacted_content_parts = revision_inventory.get("unredacted_content_parts") or []
    note_part_manifest = revision_inventory.get("note_part_manifest") or []
    unredacted_content_count = sum(
        int(row.get("count") or 0) for row in unredacted_content_parts)
    current_unredacted_content_digest = str(
        revision_inventory.get("unredacted_content_digest") or "")
    if revision_mode is None:
        revision_mode = "redact_preserve"
    if revision_mode not in ("require_resolved", "redact_preserve",
                             "passthrough_unverified"):
        raise ValueError("DOCX修订处理方式无效")
    passthrough_unverified = bool(
        revision_mode == "passthrough_unverified" and
        revision_analysis.get("passthrough_safe"))
    # 此门只对**命令行**路径有效。Web 界面里 redact+pdf 一律转「PDF 逐页核对」项目
    # （contract_app_server 的 /api/upload 提前 return），restore+pdf 也被拒；故本行在
    # Web 路径上不可达，属 CLI 专用防线。
    D.require_pdf_text(doc)
    candidates = engine.scan_document(doc)
    if unified_profile:
        review_ext.classify_unified_candidates(candidates)
    if custom_terms:
        candidates = review_ext.add_exact_terms(doc, candidates, custom_terms)

    # 把 allowlist 过滤也加进去
    if settings.allowlist:
        candidates = [c for c in candidates if c.value not in settings.allowlist]

    full_text = "\n".join(b.text.rstrip("\n") for b in doc.blocks)
    report = D.build_report(doc, candidates, settings)
    report["_full_text"] = full_text

    # 3) 选择
    if selection:
        enabled_types = set(selection.get("enabled_types", []))
        # 复核页会带上逐个位置的白名单；命令行用法没有这个键 → None（不过滤）
        sel_items = selection.get("items")
    else:
        enabled_types = set(types or [])
        sel_items = None

    # 4) 构造 ops
    ops, full_text, unsupported_target_ops = build_ops_from_report(
        report, enabled_types, sel_items, return_skipped=True)
    if ext == ".docx" and has_revisions:
        from contract_docx_revisions import split_shared_revision_view_operations
        ops = split_shared_revision_view_operations(ops)
    external_target_ops = [op for op in ops if op.target and
                           op.target.get("kind") == "external_relationship_target"]
    vml_target_ops = [op for op in ops if op.target and
                      op.target.get("kind") == "vml_textpath"]
    ops = [op for op in ops if not (op.target and
                                    op.target.get("kind") in {
                                        "external_relationship_target", "vml_textpath"})]
    if external_relationship_action != "preserve":
        external_target_ops = []
    if ext == ".docx" and comment_mode in ("remove_all", "preserve_unredacted"):
        ops = [op for op in ops if not (op.target and str(op.target.get("kind", "")).startswith("comment_"))]
    confirmed_unsupported_only = bool(
        has_revisions or revision_analysis.get("unsupported_object_manifest") or
        revision_analysis.get("unsafe_field_manifest") or passthrough_unverified)
    confirmed_unredacted_content_only = bool(unredacted_content_count)
    confirmed_external_cleanup_only = bool(
        external_clean and int(revision_inventory.get("external_relationship_count") or 0) > 0 and
        (external_relationship_action == "remove" or
         (external_relationship_action == "preserve" and
          external_relationship_acknowledged)))
    if external_clean and not allow_no_replacements and not (ops or external_target_ops or vml_target_ops) and not confirmed_unsupported_only and not confirmed_unredacted_content_only and not confirmed_external_cleanup_only and not (
            comment_mode in ("remove_all", "preserve_unredacted") and
            comment_info and comment_info.related_parts):
        raise ValueError("没有可替换的内容，已停止外发导出")

    if (allow_no_replacements and external_clean and
            not (confirmation or {}).get("confirmed") and
            not (confirmation or {}).get("preflight")):
        raise ValueError("请明确确认无替换导出，原有敏感文字可能保留")

    # 4.b) restore_mode：把所有 replacement 换成 [label#N]，并累计 mapping
    #      编号规则：同一个实体（同一个人/企业，含简称）在整篇里**共用同一个编号**，
    #      例如「张三」出现 10 次全部是 [自然人姓名#1]。编号由 detector 的
    #      EntityNumberer 统一分配，保证与复核页、mapping.json 三处完全一致。
    mapping_records: List[Dict[str, Any]] = []
    if restore_mode:
        numberer = D.EntityNumberer(report.get("items") or [], case_numbers)
        new_ops: List[RedactOp] = []
        for op in ops:
            # 查号用的是「实体代表值」而非本次出现的字面文本，
            # 保证同一实体的全称/简称拿到同一个编号。
            placeholder = numberer.placeholder(
                op.type_id, op.entity_value or op.value, op.label)
            new_ops.append(RedactOp(
                start=op.start, end=op.end, type_id=op.type_id,
                label=op.label, severity=op.severity,
                confidence=op.confidence, value=op.value,
                replacement=placeholder, location=op.location,
                page=op.page, bbox=op.bbox,
                entity_value=op.entity_value, block_index=op.block_index,
                local_start=op.local_start, local_end=op.local_end,
                target=copy.deepcopy(op.target),
                overlap_types=[op.type_id], overlap_labels=[op.label],
                overlap_masks=[op.replacement], masked_value=op.replacement,
            ))
        ops = new_ops
        for op in external_target_ops:
            op.replacement = numberer.placeholder(
                op.type_id, op.entity_value or op.value, op.label)
        for op in vml_target_ops:
            op.replacement = numberer.placeholder(
                op.type_id, op.entity_value or op.value, op.label)

    if ext == ".docx":
        ops = _coalesce_exact_docx_targets(ops, restore_mode=restore_mode)
        external_target_ops = _coalesce_exact_docx_targets(
            external_target_ops, restore_mode=restore_mode)
        vml_target_ops = _coalesce_exact_docx_targets(
            vml_target_ops, restore_mode=restore_mode)
    if restore_mode:
        for op in ops:
            aliases = op.overlap_types or [op.type_id]
            labels = op.overlap_labels or [op.label]
            record = {
                "type": op.type_id,
                "label": op.label,
                "severity": op.severity,
                "original": op.value,
                "entity_value": op.entity_value or op.value,
                "placeholder": op.replacement,
                "masked": op.masked_value or op.replacement,
                "start": op.start,
                "end": op.end,
                "page": op.page,
                "location": op.location,
                "block_index": op.block_index,
                "bbox": op.bbox,
                "confidence": op.confidence,
                **({"target": copy.deepcopy(op.target)} if ext == ".docx" and op.target else {}),
                **({"types": aliases, "labels": labels}
                   if len(aliases) > 1 else {}),
                **({"overlap_masks": op.overlap_masks}
                   if len(op.overlap_masks) > 1 else {}),
                **({"overlap_originals": op.overlap_originals}
                   if len(op.overlap_originals) > 1 else {}),
                **({"original_initials": op.target.get("original_initials", "")}
                   if op.target and op.target.get("kind") == "comment_author" else {}),
            }
            mapping_records.append(record)

    # 5) 选改写器
    comment_applied = 0
    comments_removed = 0
    comments_preserved = 0
    pre_skipped_ops: List[RedactOp] = list(unsupported_target_ops)
    if ext in (".docx",):
        comment_ops = [op for op in ops if op.target and str(op.target.get("kind", "")).startswith("comment_")]
        revision_ops = [op for op in ops if op.target and
                        str(op.target.get("kind", "")).startswith("revision_")]
        regular_ops = [op for op in ops if op not in comment_ops and op not in revision_ops]
        if has_revisions:
            # Revision author and rejected-view blocks are synthetic scan blocks;
            # route ordinary text targets by their XML paragraph coordinates too.
            paragraph_ops = [op for op in regular_ops
                             if op.target and op.target.get("kind") == "text"]
            inventory_unknown = bool(revision_analysis.get("inventory_scope_unknown"))
            if inventory_unknown:
                pre_skipped_ops.extend(
                    op for op in revision_ops
                    if str((op.target or {}).get("kind", "")).startswith("revision_"))
                revision_ops = [op for op in revision_ops
                                if not str((op.target or {}).get("kind", "")).startswith("revision_")]
            else:
                revision_ops.extend(paragraph_ops)
                regular_ops = [op for op in regular_ops if op not in paragraph_ops]
        if pre_skipped_ops:
            ops, mapping_records = _discard_skipped_operations(
                ops, mapping_records, pre_skipped_ops)
            skipped_ids = {id(op.source_op or op) for op in pre_skipped_ops}
            regular_ops = [op for op in regular_ops if id(op) not in skipped_ids]
            revision_ops = [op for op in revision_ops if id(op) not in skipped_ids]
        if external_clean and not has_revisions:
            expected_story_text, story_skipped_ops = _expected_plain_story_text(
                source, full_text, regular_ops, return_skipped=True)
            if story_skipped_ops:
                pre_skipped_ops.extend(story_skipped_ops)
                skipped_ids = {id(op) for op in story_skipped_ops}
                regular_ops = [op for op in regular_ops if id(op) not in skipped_ids]
                ops = [op for op in ops if id(op) not in skipped_ids]
                ops, mapping_records = _discard_skipped_operations(
                    ops, mapping_records, story_skipped_ops)
        redactor = DocxRedactor(source, full_text, regular_ops)
    elif ext == ".pdf":
        redactor = PdfRedactor(source, full_text, ops)
    elif ext in (".txt", ".md", ".markdown", ""):
        redactor = TextRedactor(source, full_text, ops)
    else:
        raise RuntimeError(f"不支持的文件格式：{ext}")

    # 6) 决定输出路径
    if not out_path:
        stem = os.path.splitext(os.path.basename(source))[0]
        out_path = os.path.join(os.path.dirname(source) or ".",
                                f"{stem}_脱敏{ext or '.txt'}")

    stage_expected_digest = current_unsupported_digest
    if ext != ".docx" or has_revisions or not external_clean:
        expected_story_text = None

    # 7) 改写
    result = redactor.rewrite(out_path)
    writer_skipped_ops = list(result.pop("skipped_ops", []) or [])
    if writer_skipped_ops:
        writer_skipped_ops = [op.source_op or op for op in writer_skipped_ops]
        ops, mapping_records = _discard_skipped_operations(
            ops, mapping_records, writer_skipped_ops)
        result.setdefault("skipped_details", []).extend(
            _skipped_operation_detail(op) for op in writer_skipped_ops)
        if ext == ".docx" and not has_revisions and external_clean:
            regular_ops = [op for op in regular_ops
                           if op not in writer_skipped_ops]
            expected_story_text = _expected_plain_story_text(
                source, full_text, regular_ops)
    if pre_skipped_ops:
        result["skipped"] += len(pre_skipped_ops)
        result.setdefault("skipped_details", []).extend(
            _skipped_operation_detail(op) for op in pre_skipped_ops)
    if ext == ".docx" and passthrough_unverified:
        review_ext.restore_unscanned_parts(
            source, out_path, revision_inventory.get("unscanned_parts"))
    if ext == ".docx" and has_revisions:
        if revision_analysis.get("has_revisions") and revision_ops:
            from contract_docx_revisions import rewrite_revision_occurrences
            stage_fd, revision_stage = tempfile.mkstemp(
                suffix=".docx", dir=os.path.dirname(os.path.abspath(out_path)))
            os.close(stage_fd)
            try:
                revision_result = rewrite_revision_occurrences(
                    out_path, revision_stage, revision_ops,
                    allow_unsupported_revisions=bool(unsupported_revisions),
                    expected_unsupported_digest=(revision_ack_digest
                                                 if revision_acknowledged else None),
                    allow_incomplete_passthrough=passthrough_unverified)
                os.replace(revision_stage, out_path)
                result["applied"] += revision_result["applied"]
                stage_expected_digest = revision_result["expected_unsupported_digest"]
                revision_skipped = revision_result.get("skipped_ops") or []
                if revision_skipped:
                    result["skipped"] += len(revision_skipped)
                    result.setdefault("skipped_details", []).extend(
                        _skipped_operation_detail(op) for op in revision_skipped)
                    ops, mapping_records = _discard_skipped_operations(
                        ops, mapping_records, revision_skipped)
                    revision_skipped_ids = {id(op.source_op or op)
                                             for op in revision_skipped}
                    revision_ops = [op for op in revision_ops
                                    if id(op) not in revision_skipped_ids]
            except Exception:
                if os.path.exists(revision_stage):
                    os.unlink(revision_stage)
                if os.path.exists(out_path):
                    os.unlink(out_path)
                raise
    if ext == ".docx":
        try:
            if comment_mode == "redact_preserve":
                from contract_docx_comments import rewrite_comment_occurrences
                try:
                    comment_result = rewrite_comment_occurrences(out_path, comment_ops)
                    comment_applied = comment_result["applied"]
                    result["applied"] += comment_result["applied"]
                    result["skipped"] += comment_result["skipped"]
                    comment_skipped = comment_result.get("skipped_ops") or []
                except Exception:
                    from contract_docx_comments import preserve_comment_package
                    preserve_comment_package(source, out_path)
                    comment_skipped = list(comment_ops)
                    result["skipped"] += len(comment_skipped)
                    comments_preserved = comment_info.comment_count if comment_info else 0
                if comment_skipped:
                    result.setdefault("skipped_details", []).extend(
                        _skipped_operation_detail(op) for op in comment_skipped)
                    ops, mapping_records = _discard_skipped_operations(
                        ops, mapping_records, comment_skipped)
            elif comment_mode == "remove_all":
                from contract_docx_comments import remove_comment_package
                remove_comment_package(out_path)
                comments_removed = comment_info.comment_count if comment_info else 0
            else:
                from contract_docx_comments import preserve_comment_package
                preserve_comment_package(source, out_path)
                comments_preserved = comment_info.comment_count if comment_info else 0
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise
    if restore_mode and ext == ".docx":
        for record, operation in zip(mapping_records, ops):
            if operation.target:
                record["target"] = copy.deepcopy(operation.target)
    if ext == ".docx" and vml_target_ops:
        try:
            from contract_docx_revisions import rewrite_vml_text_targets
            vml_result = rewrite_vml_text_targets(out_path, vml_target_ops)
            if vml_result.get("applied") != len(vml_target_ops):
                raise ValueError("VML文字路径替换数量与已选项不一致")
            result["applied"] += vml_result["applied"]
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise
    if external_clean:
        try:
            if ext != ".docx":
                raise ValueError("外发干净副本目前仅支持 Word .docx")
            selected_originals = list(dict.fromkeys(
                op.value for op in (ops + external_target_ops + vml_target_ops)
                if op.value))
            review_ext.clean_docx(
                out_path, selected_originals, comment_mode=comment_mode,
                preserve_revisions=has_revisions,
                allow_unsupported_revisions=bool(unsupported_revisions),
                expected_unsupported_digest=(stage_expected_digest
                                             if unsupported_revisions else None),
                allow_unsupported_objects=bool(
                    revision_analysis.get("unsupported_object_manifest")),
                expected_object_digest=revision_analysis.get("object_digest"),
                external_relationship_action=external_relationship_action,
                external_relationship_acknowledged=(
                    external_relationship_action == "preserve"),
                expected_external_relationship_digest=external_relationship_digest,
                allow_unverified_passthrough=bool(
                    revision_mode == "passthrough_unverified" and
                    revision_analysis.get("passthrough_safe")),
                expected_unscanned_digest=revision_inventory.get("unscanned_digest"),
                allow_unsafe_fields=bool(
                    revision_analysis.get("unsafe_field_manifest")),
                expected_unsafe_field_digest=revision_inventory.get(
                    "unsafe_field_digest"),
                allow_unredacted_content=bool(unredacted_content_count),
                expected_unredacted_content_digest=current_unredacted_content_digest,
                allow_unknown_revision_passthrough=unknown_revision_passthrough,
                expected_revision_passthrough_digest=unknown_revision_digest,
                expected_story_text=expected_story_text)
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise

    if ext == ".docx" and external_target_ops:
        try:
            from contract_docx_revisions import rewrite_external_hyperlink_targets
            target_result = rewrite_external_hyperlink_targets(
                out_path, external_target_ops)
            result["applied"] += target_result["applied"]
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise

    if (ext == ".docx" and
            (has_revisions or revision_analysis.get("unsupported_object_manifest") or
             int(revision_analysis.get("text_object_count") or 0) or
             revision_analysis.get("unsafe_field_manifest") or
             note_part_manifest or
             int(revision_inventory.get("external_relationship_count") or 0) or
             passthrough_unverified)):
        try:
            if unknown_revision_passthrough:
                from contract_docx_revisions import revision_passthrough_digest
                if revision_passthrough_digest(out_path) != unknown_revision_digest:
                    raise ValueError("最终DOCX改变了盘点异常时保留的修订内容")
                output_analysis = None
                output_inventory = {}
            else:
                from contract_docx_revisions import analyze_docx_revisions
                output_analysis = analyze_docx_revisions(out_path)
                output_inventory = output_analysis.get("inventory") or {}
            if unknown_revision_passthrough:
                pass
            else:
                before_records = sorted((row.get("part"), row.get("kind"), row.get("count"))
                                       for row in revision_inventory.get("records", []))
                after_records = sorted((row.get("part"), row.get("kind"), row.get("count"))
                                      for row in output_inventory.get("records", []))
                structure_changed = (
                    output_inventory.get("unsupported_digest") != stage_expected_digest)
                if ((not output_analysis.get("complete") and not (
                        passthrough_unverified and output_analysis.get("passthrough_safe"))) or
                        (has_revisions and not output_analysis.get("has_revisions")) or
                        before_records != after_records or
                        (unsupported_revisions and structure_changed) or
                        (int(revision_analysis.get("text_object_count") or 0) and
                         output_analysis.get("object_digest") !=
                         revision_analysis.get("object_digest")) or
                        (revision_analysis.get("unsafe_field_manifest") and
                         output_inventory.get("unsafe_field_digest") !=
                         revision_inventory.get("unsafe_field_digest")) or
                        (external_relationship_action == "remove" and
                         int(output_inventory.get("external_relationship_count") or 0) != 0) or
                        (passthrough_unverified and
                         output_inventory.get("unscanned_digest") !=
                         revision_inventory.get("unscanned_digest")) or
                        (note_part_manifest and
                         output_inventory.get("note_structure_digest") !=
                         revision_inventory.get("note_structure_digest")) or
                        (not unsupported_revisions and not passthrough_unverified and
                         not output_analysis.get("can_preserve_and_redact"))):
                    raise ValueError("最终DOCX修订结构与输入不一致，已停止发布")
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise

    if restore_mode and ext == ".docx" and has_revisions:
        from contract_docx_revisions import capture_output_targets
        try:
            capture_output_targets(out_path, mapping_records)
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise

    # 7.b) restore_mode：单独写 mapping.json；脱敏件不得携带原文映射。
    mapping_path: Optional[str] = None
    embedded_ok = False
    if restore_mode and (mapping_records or external_target_ops or vml_target_ops):
        mapping_path = out_path + ".mapping.json"
        sha = ""
        try:
            sha = D._sha256(source)
        except Exception:
            pass
        external_target_records = []
        for op in external_target_ops:
            target = op.target or {}
            external_target_records.append({
                "type": op.type_id, "label": op.label,
                "kind": "hyperlink", "part": target.get("part", ""),
                "relationship_id": target.get("relationship_id", ""),
                "attribute": "Target", "original": op.value,
                "placeholder": op.replacement,
                "written": target.get("uri_written", ""),
                "order": int(target.get("target_order", 0)),
                "source_target_sha256": target.get("source_target_sha256", ""),
                "output_target_sha256": target.get("output_target_sha256", ""),
                "source_span": [op.local_start, op.local_end],
                **({"types": op.overlap_types, "labels": op.overlap_labels}
                   if len(op.overlap_types) > 1 else {}),
                **({"overlap_masks": op.overlap_masks}
                   if len(op.overlap_masks) > 1 else {}),
                **({"overlap_originals": op.overlap_originals}
                   if len(op.overlap_originals) > 1 else {}),
            })
        vml_target_records = []
        for op in vml_target_ops:
            target = op.target or {}
            vml_target_records.append({
                "type": op.type_id, "label": op.label,
                "kind": "vml_textpath", "part": target.get("part", ""),
                "node_path": target.get("node_path", ""),
                "attribute": "string", "original": op.value,
                "placeholder": op.replacement,
                "written": target.get("written", ""),
                "order": int(target.get("output_order", 0)),
                "source_target_sha256": target.get("source_target_sha256", ""),
                "output_target_sha256": target.get("output_target_sha256", ""),
                "structure_sha256": target.get("structure_sha256", ""),
                "output_span": list(target.get("output_span") or []),
                **({"types": op.overlap_types, "labels": op.overlap_labels}
                   if len(op.overlap_types) > 1 else {}),
                **({"overlap_masks": op.overlap_masks}
                   if len(op.overlap_masks) > 1 else {}),
                **({"overlap_originals": op.overlap_originals}
                   if len(op.overlap_originals) > 1 else {}),
            })
        mapping_payload = {
            "version": 2 if ext == ".docx" else 1,
            "schema": "contract-mapping/v2" if ext == ".docx" else "contract-mapping/v1",
            "engine": {"name": ENGINE_NAME, "version": __version__, "mode": "offline-restore"},
            "source": {"path": os.path.abspath(source), "sha256": sha,
                       "name": os.path.basename(source), "type": ext.lstrip(".")},
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **({"output": {
                "sha256": D._sha256(out_path),
                "restore_compatibility_supported": bool(
                    has_revisions and all(
                        isinstance(row.get("target"), dict) and
                        (str(row["target"].get("kind", "")) == "text" or
                         str(row["target"].get("kind", "")).startswith(("revision_", "comment_"))) and
                        bool(row["target"].get("output_text_sha256")) and
                        bool(row["target"].get("output_structure_fingerprint"))
                        for row in mapping_records)),
            }} if has_revisions else {}),
            "entities": _summarize_entities(mapping_records),
            "items": sorted(mapping_records, key=lambda r: (r.get("start", 0), r.get("end", 0))),
            **({"external_targets": external_target_records}
               if external_target_records else {}),
            **({"vml_targets": vml_target_records}
               if vml_target_records else {}),
        }
        mapping_dir = os.path.dirname(os.path.abspath(mapping_path))
        mapping_fd, mapping_stage = tempfile.mkstemp(prefix=".mapping-", suffix=".tmp",
                                                      dir=mapping_dir)
        try:
            with os.fdopen(mapping_fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(mapping_payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(mapping_stage, mapping_path)
        except Exception:
            if os.path.exists(mapping_stage):
                os.unlink(mapping_stage)
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise

    # 8) 写审计日志
    log_path = None
    if write_log:
        log_path = out_path + ".redaction.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "source": os.path.abspath(source),
                "output": os.path.abspath(out_path),
                "enabled_types": sorted(enabled_types),
                "min_confidence": min_confidence,
                "min_severity": min_severity,
                "restore_mode": bool(restore_mode),
                "comment_mode": comment_mode if ext == ".docx" else None,
                "applied": result["applied"],
                "skipped": result["skipped"],
                "mapping": os.path.abspath(mapping_path) if mapping_path else None,
                "items": [
                    {"type": op.type_id, "label": op.label, "value": op.value,
                     "replacement": op.replacement, "location": op.location,
                     "page": op.page, "bbox": op.bbox, "confidence": op.confidence}
                    for op in ops
                ],
                # v1.3.0：导出前清单留痕（低置信项的原始分数只进日志、不进界面）
                "low_confidence": low_confidence_items or [],
                "review_confirmation": confirmation or None,
            }, ensure_ascii=False, indent=2))
    return {
        "output": out_path,
        "applied": result["applied"],
        "skipped": result["skipped"],
        "log": log_path,
        "mapping": mapping_path,
        "mapping_embedded": embedded_ok,
        "restore_mode": bool(restore_mode),
        "comment_mode": comment_mode if ext == ".docx" else None,
        "comment_applied": comment_applied,
        "comments_removed": comments_removed,
        "comments_preserved": comments_preserved,
        "comment_warning": (
            (("当前无法识别并脱敏批注内容，请人工核对批注内的敏感信息。"
              if comment_info.unsupported else
              "您已选择保留批注但不脱敏，请人工核对批注内的敏感信息。") +
             "批注已保留，未脱敏。")
            if ext == ".docx" and comment_mode == "preserve_unredacted" and
            comment_info and comment_info.related_parts else
            ("批注结构无法完整解析；相关批注将按原文保留，请人工核对。"
             if ext == ".docx" and comment_mode == "redact_preserve" and
             comment_info and comment_info.unsupported else "")),
        "skipped_details": result.get("skipped_details", []),
    }


def _load_doc(path: str) -> Document:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".pdf":
        return extract_pdf(path)
    return extract_text_file(path)


# --------------------------------------------------- 本地 HTTP 服务（供复核页调用）


def start_serve(host: str, port: int, doc_root: str) -> ThreadingHTTPServer:
    """启动一个 127.0.0.1 本地服务，让复核页可一键调用 redactor。

    POST /apply  body = {"source":..., "enabled_types":[...]}
    返 {"ok": True, "output":..., "applied":..., "skipped":..., "download":...}
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            sys.stderr.write("[serve] " + (format % args) + "\n")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                req = json.loads(body) if body else {}
            except Exception as e:
                self._json(400, {"ok": False, "error": "invalid json: " + str(e)})
                return
            if self.path == "/apply":
                self._apply(req)
            elif self.path == "/health":
                self._json(200, {"ok": True, "service": ENGINE_NAME,
                                 "version": __version__})
            else:
                self._json(404, {"ok": False, "error": "unknown path"})

        def _apply(self, req: Dict[str, Any]):
            src = req.get("source")
            if not src or not os.path.exists(src):
                self._json(400, {"ok": False, "error": f"源文件不存在：{src}"})
                return
            try:
                r = redact_file(
                    source=src,
                    selection={"enabled_types": req.get("enabled_types", [])},
                    min_confidence=float(req.get("min_confidence", 0.5)),
                    min_severity=req.get("min_severity", "low"),
                    out_path=os.path.join(doc_root, _out_name(src)),
                )
                out = r["output"]
                self._json(200, {
                    "ok": True,
                    "output": os.path.basename(out),
                    "applied": r["applied"],
                    "skipped": r["skipped"],
                    "download": "file://" + os.path.abspath(out),
                })
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"ok": True, "service": ENGINE_NAME,
                                 "version": __version__})
                return
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    f"{ENGINE_NAME} v{__version__}\nPOST /apply  执行脱敏\nGET/POST /health 心跳\n".encode())
            else:
                self._json(404, {"ok": False, "error": "GET not supported"})

        def _json(self, code: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer((host, port), Handler)
    return server


def _out_name(src: str) -> str:
    base = os.path.basename(src)
    stem, ext = os.path.splitext(base)
    return f"{stem}_脱敏{ext or '.txt'}"


def _restore_name(src: str) -> str:
    """还原产物的命名:把 <stem>_脱敏.docx → <stem>_还原.docx"""
    base = os.path.basename(src)
    stem, ext = os.path.splitext(base)
    if stem.endswith("_脱敏"):
        stem = stem[:-3]
    return f"{stem}_还原{ext or '.txt'}"


def placeholder_value(label: str, counter: int) -> str:
    """生成与 detector write_mapping 完全一致的占位符。

    形如 "[自然人姓名#1]"。格式只在 detector.placeholder_text 定义一次，
    这里直接委托，避免两处各写一份格式字符串而走样。
    """
    return D.placeholder_text(label, counter)


def _summarize_entities(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (type, 占位符) 汇总「这份合同一共涉及几种主体、各自出现几次」。"""
    agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in records:
        ph = r.get("placeholder") or ""
        parsed = D.parse_placeholder(ph)
        if not parsed:
            continue
        key = (r.get("type", ""), ph)
        item = agg.get(key)
        if item is None:
            agg[key] = {
                "type": r.get("type", ""),
                "label": r.get("label", ""),
                "number": parsed[1],
                "placeholder": ph,
                "original": r.get("original", ""),
                "occurrences": 1,
            }
        else:
            item["occurrences"] += 1
    return sorted(agg.values(), key=lambda x: (x["type"], x["number"]))


# ---------------------------------------------- DOCX 占位符回填（还原） --------


def _split_replacement_static(rep: str, seg_lens: List[int]) -> List[str]:
    """把 rep 按 seg_lens 长度比例切分成多段（与 DocxRedactor 共享）。"""
    total = sum(seg_lens)
    if total == 0 or len(seg_lens) == 1:
        return [rep]
    n = len(rep)
    if n <= len(seg_lens):
        return [rep[i] if i < n else "" for i in range(len(seg_lens))]
    sizes = [max(1, round(n * L / total)) for L in seg_lens]
    diff = n - sum(sizes)
    i = 0
    while diff != 0 and sizes:
        if diff > 0:
            sizes[i % len(sizes)] += 1
            diff -= 1
        elif sizes[i % len(sizes)] > 1:
            sizes[i % len(sizes)] -= 1
            diff += 1
        i += 1
        if i > 100:
            break
    out: List[str] = []
    cur = 0
    for s in sizes[:-1]:
        out.append(rep[cur:cur + s])
        cur += s
    out.append(rep[cur:])
    return out


def _segments_in_units(units: List[Tuple], s: int, e: int) -> List[Tuple[int, int, int]]:
    """把文本区间 [s, e) 切成若干「同一 <w:t> 内的连续字符段」。

    ``units`` 由 ``docx_para_units`` 给出，与段落文本逐字对齐。
    ('keep', ...) 是 tab/br 折算出的不可写保留位：它会**中断**当前段，
    但自身不参与替换——替换后制表符/换行数量与位置保持不变。
    """
    segments: List[Tuple[int, int, int]] = []
    cur_ti = None
    seg_l = seg_r = 0
    for g in range(s, e):
        u = units[g]
        if u[0] != "t":
            if cur_ti is not None:
                segments.append((cur_ti, seg_l, seg_r + 1))
                cur_ti = None
            continue
        ti, j = u[1], u[2]
        if cur_ti is None:
            cur_ti, seg_l, seg_r = ti, j, j
        elif ti == cur_ti and j == seg_r + 1:
            seg_r = j
        else:
            segments.append((cur_ti, seg_l, seg_r + 1))
            cur_ti, seg_l, seg_r = ti, j, j
    if cur_ti is not None:
        segments.append((cur_ti, seg_l, seg_r + 1))
    return segments


def _replace_in_paragraph_with_map(para_elem, pattern: "re.Pattern[str]",
                                    seqs: Dict[str, List[str]],
                                    cursor: Dict[str, int], W_NS: str) -> int:
    """在段落上对所有 placeholder 应用替换；保留 run 级别样式（粗体/字体等）。

    与 DocxRedactor._replace_in_paragraph 同思路：从后往前扫，命中后切分 run。

    ``seqs``：占位符 → 该占位符按文档顺序对应的原文列表。
    同一个实体共用一个占位符时（如全称「北京星海智能科技有限公司」与其简称
    「星海智能」都是 [组织机构名称#1]），该列表会有多个**不同**的原文，
    因此必须按出现顺序逐个取用；``cursor`` 跨段落累计已用到的下标。
    """
    from contract_docx_revisions import iter_story_content_nodes
    ts = [node for node in iter_story_content_nodes(para_elem)
          if node.tag == W_NS + "t"]
    if not ts:
        return 0
    # 口径须与检测侧一致（tab→空格、br→换行），否则还原时定位不到占位符。
    para_text_full, units = D.docx_para_units(para_elem)
    para_text = para_text_full.rstrip("\n")
    if not pattern.search(para_text):
        return 0

    pieces: List[List[str]] = [list(t.text or "") for t in ts]
    matches = list(pattern.finditer(para_text))
    applied = 0

    # 先按「文档正序」取出每个占位符本次应还原成的原文，再倒序改写，
    # 这样既保证与出现顺序对齐，又不影响从后往前切分 run 的既有逻辑。
    repls: List[str] = []
    for m in matches:
        seq = seqs.get(m.group(0)) or [""]
        k = cursor.get(m.group(0), 0)
        repls.append(seq[k] if k < len(seq) else seq[-1])
        cursor[m.group(0)] = k + 1

    for m, repl in reversed(list(zip(matches, repls))):
        s, e = m.start(), m.end()

        segments = _segments_in_units(units, s, e)

        if len(segments) == 1:
            parts = [repl]
        else:
            seg_lens = [r - l for _, l, r in segments]
            parts = _split_replacement_static(repl, seg_lens)

        for (ti, l, r), part in zip(segments, parts):
            pieces[ti][l:r] = list(part)
        applied += 1

    for t, piece in zip(ts, pieces):
        t.text = "".join(piece)
    return applied


class DocxRestorer:
    """docx 还原器：用 mapping.json 把 [label#N] 占位符回填成原文。

    关键不变式
    ----------
    * 只修改 `<w:t>.text`，不修改 `<w:comment>`、`<w:ins>`、`<w:del>` 等节点。
      python-docx 的 save() 也只会序列化我们触碰过的元素，其他部件原样保留。
    * 批注 (`comments.xml`) 通过 run-id 关联文本段；当我们切分 run 来写大段文本，
      run-id 会重新分配。原本的内容锚定不会跨段移动，所以批注位置不变。
    * 修订标记（tracked changes）位于 run 外层 (`<w:ins>/<w:del>` 包住 `<w:r>`)，
      我们在 run 内做字符级回填，修订语义不变。
    * 段落合并/拆分不会发生 —— 只在一个段落内做替换。

    限制
    ----------
    * PDF 还原不支持：栅格化后文本层已清空，无法程序化回填（必须 OCR）。
    """

    W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def __init__(self, source: str, mapping):
        """mapping 支持两种形态：

        * ``{placeholder: original}``            —— 占位符与原文一一对应（常规情况）
        * ``{placeholder: [original, ...]}``     —— 同一占位符对应多个原文（全称/简称同号）
        """
        self.source = source
        seqs: Dict[str, List[str]] = {}
        for ph, val in dict(mapping).items():
            if isinstance(val, (list, tuple)):
                seqs[ph] = [str(x) for x in val] or [""]
            else:
                seqs[ph] = [str(val)]
        self.seqs = seqs
        # 兼容旧接口：单值映射（取该占位符的最后一个原文）
        self.mapping = {ph: v[-1] for ph, v in seqs.items()}

    def restore(self, dst: str) -> Dict[str, Any]:
        try:
            from docx import Document as DocxDocument  # type: ignore
            from docx.oxml.ns import qn
        except ImportError as exc:
            raise RuntimeError("还原 docx 需要 python-docx") from exc

        shutil.copyfile(self.source, dst)
        doc = DocxDocument(dst)

        if not self.seqs:
            return {"applied": 0, "skipped": 0, "missing": list()}

        # 按 placeholder 长度倒序，避免子串误伤
        items = sorted(self.seqs.items(), key=lambda kv: -len(kv[0]))
        pattern = re.compile("|".join(re.escape(ph) for ph, _ in items))

        # 收集所有段落（正文 + 页眉页脚）；口径与检测/改写一致
        para_seq = [paragraph for _kind, _location, paragraph
                    in D.docx_para_targets(doc)]

        applied = 0
        # 替换前先采集完整文本，便于统计「文档中没有的占位符」
        before_text_parts: List[str] = []
        before_text_parts.extend(D.docx_para_text(p) for p in para_seq)
        before_text = "\n".join(before_text_parts)

        cursor: Dict[str, int] = {}
        for p in para_seq:
            applied += _replace_in_paragraph_with_map(
                p, pattern, self.seqs, cursor, self.W_NS)

        missing = [ph for ph in self.seqs if ph not in before_text]

        doc.save(dst)
        return {"applied": applied, "skipped": 0, "missing": missing}


class TextRestorer:
    """txt/md 还原器：用 mapping.json 把 [label#N] 占位符回填成原文。

    关键不变式
    ----------
    * 纯文本、按 char-offset 做字符串切片替换，不解析文档结构（与 DocxRestorer 相对）。
    * **占位符计数严格核对**：某占位符在文档中的出现次数**不得多于** mapping 提供的
      原文条数。否则会需要「循环取模（n % len(seq)）」回填旧值，制造看似成功、实则
      内容错误的还原产物 —— 此时直接报错中止，禁止静默回填。
      （文档中出现次数**少于** mapping 条数时，未用到的占位符计入 ``missing``，
      与 DocxRestorer 口径一致。）
    * 单次正则扫描 + 按文档出现顺序消费映射；替换时**从后往前**应用，避免 offset
      漂移，也避免刚还原出的原文被再次当作占位符处理。

    限制
    ----------
    * 换行：文本模式读取会规范化换行，``newline=""`` 只能保留读取后的换行字符，
      **不能恢复原件 CRLF/LF 约定**（如需原件级还原，须在脱敏阶段另存换行约定）。
    * 空行：若待还原文本本身已由块级重建丢失空行，还原无法凭空补回。
    """

    def __init__(self, source: str, mapping):
        """mapping 支持 ``{placeholder: original}`` 与 ``{placeholder: [original, ...]}``
        两种形态，与 DocxRestorer 保持一致。"""
        self.source = source
        seqs: Dict[str, List[str]] = {}
        for ph, val in dict(mapping).items():
            if isinstance(val, (list, tuple)):
                seqs[ph] = [str(x) for x in val] or [""]
            else:
                seqs[ph] = [str(val)]
        self.seqs = seqs
        # 兼容旧接口：单值映射（取该占位符的最后一个原文）
        self.mapping = {ph: v[-1] for ph, v in seqs.items()}

    def restore(self, dst: str) -> Dict[str, Any]:
        with open(self.source, "r", encoding="utf-8") as f:
            text = f.read()

        if not self.seqs:
            with open(dst, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            return {"applied": 0, "skipped": 0, "missing": list()}

        # 按 placeholder 长度倒序，避免子串误伤（长占位符优先匹配）
        items = sorted(self.seqs.items(), key=lambda kv: -len(kv[0]))
        pattern = re.compile("|".join(re.escape(ph) for ph, _ in items))

        # 1) 单次扫描，收集文档内所有占位符（按出现顺序）
        matches = [(m.start(), m.group(0)) for m in pattern.finditer(text)]

        # 2) 计数核对：文档出现次数不得多于 mapping 条数（否则需取模，禁止）
        doc_counts: Dict[str, int] = {}
        for _s, ph in matches:
            doc_counts[ph] = doc_counts.get(ph, 0) + 1
        surplus = [
            (ph, doc_counts.get(ph, 0), len(seq))
            for ph, seq in self.seqs.items()
            if doc_counts.get(ph, 0) > len(seq)
        ]
        if surplus:
            detail = "；".join(
                f"{ph} 文档 {n} 处 / mapping {m} 条" for ph, n, m in surplus)
            raise RuntimeError(
                "占位符数量与 mapping 不一致，为避免错误还原已中止：" + detail)

        # 3) 按文档正序取原文，再从后往前替换（offset 不漂移）
        cursor: Dict[str, int] = {}
        assigned: List[Tuple[int, str, str]] = []
        for start, ph in matches:
            seq = self.seqs[ph]
            k = cursor.get(ph, 0)
            assigned.append((start, ph, seq[k]))
            cursor[ph] = k + 1

        for start, ph, orig in reversed(assigned):
            text = text[:start] + orig + text[start + len(ph):]

        with open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(text)

        missing = [ph for ph in self.seqs if doc_counts.get(ph, 0) == 0]
        return {"applied": len(matches), "skipped": 0, "missing": missing}


# --------------------------------------------------- 还原主流程 ----------------


def load_mapping(path: str) -> Dict[str, str]:
    """读取 detector 的 .mapping.json，返回 {placeholder: original} 字典。

    接受 contract-mapping/v1 schema。**同一实体（含全称/简称）共用一个 placeholder**，
    此时这里只保留其中一个原文；需要精确还原请用 `load_mapping_sequences()`。
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    schema = payload.get("schema", "")
    if schema and not schema.startswith("contract-mapping/"):
        raise RuntimeError(f"mapping.json 的 schema 不识别：{schema}")
    out: Dict[str, str] = {}
    for it in payload.get("items", []):
        ph = it.get("placeholder")
        orig = it.get("original")
        if ph and orig is not None:
            out[ph] = orig
    return out


def load_mapping_sequences(path: str) -> Dict[str, List[str]]:
    """返回 ``{placeholder: [原文, 原文, ...]}``，按**在文档中出现的先后**排序。

    同一个实体（如「北京星海智能科技有限公司」与其简称「星海智能」）共用一个
    占位符 ``[组织机构名称#1]``，但该占位符在不同位置要还原成不同的字面文本。
    还原器按文档顺序逐次取用该列表，就能精确还原。
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    schema = payload.get("schema", "")
    if schema and not schema.startswith("contract-mapping/"):
        raise RuntimeError(f"mapping.json 的 schema 不识别：{schema}")

    rows: List[Tuple[int, int, str, str]] = []
    for idx, it in enumerate(payload.get("items", [])):
        ph = it.get("placeholder")
        orig = it.get("original")
        if not ph or orig is None:
            continue
        start = it.get("start")
        if not isinstance(start, int):
            start = idx
        block = it.get("block_index")
        rows.append((start, block if isinstance(block, int) else idx, ph, orig))
    rows.sort(key=lambda r: (r[0], r[1]))

    out: Dict[str, List[str]] = {}
    for _start, _blk, ph, orig in rows:
        out.setdefault(ph, []).append(orig)
    return out


def find_sibling_mapping(redacted_path: str) -> Optional[str]:
    """在同一目录下寻找与 redacted_path 配对的 mapping.json。

    规则（按优先级）：
      - 嵌入版本：见 `_extract_embedded_mapping`（docx 内部 customXml）
      - <name>.mapping.json                （与原文件同名）
      - <stem>.mapping.json                （去后缀后）
      - <name>_mapping.json                （兜底）
    注意：嵌入版只对 .docx 有效，返回的是 mapping 内容而非路径，
    但 restore_file() 会把内容先写到临时文件再读取。
    """
    folder = os.path.dirname(os.path.abspath(redacted_path))
    base = os.path.basename(redacted_path)
    stem, ext = os.path.splitext(base)
    candidates = [
        os.path.join(folder, f"{stem}{ext}.mapping.json"),
        os.path.join(folder, f"{stem}.mapping.json"),
        os.path.join(folder, f"{base}.mapping.json"),
        os.path.join(folder, f"{stem}_mapping.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _extract_embedded_mapping(docx_path: str) -> Optional[Dict[str, Any]]:
    """从 docx zip 内的 customXml 抽出我们嵌进去的 mapping.json 内容。"""
    try:
        import zipfile
        with zipfile.ZipFile(docx_path) as z:
            for name in z.namelist():
                if name.endswith(".mapping.json") or name == "customXml/mapping.json":
                    with z.open(name) as f:
                        return json.loads(f.read().decode("utf-8"))
    except Exception:
        return None
    return None


def embed_mapping_in_docx(docx_path: str, mapping: Dict[str, Any]) -> bool:
    """把 mapping 内容写进 docx zip 的 customXml/mapping.json。

    同时在 [Content_Types].xml 加 override、word/_rels/document.xml.rels 加一个
    relationship，确保 WPS / Word 不会报「未声明的内容类型」警告。

    返回 True 表示成功，False 表示失败（已容错：不影响脱敏产物本身）。
    """
    import zipfile

    target_part = "customXml/mapping.json"
    rel_id = "rIdMapping1"
    ctype = "application/json"
    rels_target = "customXml/mapping.json"

    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            names = zin.namelist()
            payload_data = json.dumps(mapping, ensure_ascii=False, indent=2).encode("utf-8")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
                for n in names:
                    if n == target_part:
                        continue
                    data = zin.read(n)
                    if n == "[Content_Types].xml":
                        s = data.decode("utf-8")
                        # 仅当 override 不存在时追加
                        if f'PartName="/{target_part}"' not in s:
                            injection = f'<Override PartName="/{target_part}" ContentType="{ctype}"/>'
                            s = s.replace("</Types>", injection + "</Types>")
                        data = s.encode("utf-8")
                    elif n == "word/_rels/document.xml.rels":
                        s = data.decode("utf-8")
                        if f'Target="{rels_target}"' not in s and f'Id="{rel_id}"' not in s:
                            injection = (
                                f'<Relationship Id="{rel_id}" '
                                f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml" '
                                f'Target="../{rels_target}"/>'
                            )
                            s = s.replace("</Relationships>", injection + "</Relationships>")
                        data = s.encode("utf-8")
                    zout.writestr(n, data)
                # 写我们的 customXml 部件
                zout.writestr(target_part, payload_data)
        with open(docx_path, "wb") as f:
            f.write(buf.getvalue())
        return True
    except Exception as e:
        sys.stderr.write(f"[embed_mapping_in_docx] 失败: {e}\n")
        return False


def restore_file(source: str,
                 mapping_path: Optional[str] = None,
                 out_path: Optional[str] = None,
                 write_log: bool = True) -> Dict[str, Any]:
    """把脱敏后的文件还原回原始文件（docx / txt / md）。

    source: 脱敏后文件路径（docx / txt / md；pdf 已被栅格化，不支持）
    mapping_path: mapping.json 路径；
                  缺省时按 (1) docx 内嵌 customXml、(2) 同目录兄弟文件 顺序查找
    """
    if not os.path.exists(source):
        raise FileNotFoundError(source)

    ext = os.path.splitext(source)[1].lower()
    if ext == ".pdf":
        raise RuntimeError(
            "PDF 已被栅格化（文本层已清），无法程序化还原。"
            "如需还原，请使用 docx 版合同，或使用「脱敏（占位符模式）」+ 保留 docx 源文件。"
        )
    if ext not in (".docx", ".txt", ".md", ".markdown"):
        raise RuntimeError(
            f"暂不支持 {ext} 文件的还原；目前支持 .docx / .txt / .md")

    # 1) 优先看 docx 内部嵌的 mapping
    if not mapping_path:
        embedded = _extract_embedded_mapping(source)
        if embedded:
            tmp = source + ".embedded-mapping.json"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(embedded, f, ensure_ascii=False, indent=2)
                mapping_path = tmp
            except Exception:
                mapping_path = None
    # 2) 否则按 sibling 查找
    if not mapping_path:
        mapping_path = find_sibling_mapping(source)
    if not mapping_path or not os.path.exists(mapping_path):
        raise FileNotFoundError(
            f"找不到 mapping.json；请把 mapping 文件路径传入或放在 {source} 同目录。"
        )

    with open(mapping_path, "r", encoding="utf-8") as f:
        mapping_payload = json.load(f)
    mapping_schema = mapping_payload.get("schema", "")
    mapping_items = mapping_payload.get("items", [])
    external_target_items = mapping_payload.get("external_targets", [])
    vml_target_items = mapping_payload.get("vml_targets", [])
    if mapping_schema and not mapping_schema.startswith("contract-mapping/"):
        raise RuntimeError("mapping.json 的 schema 不识别：%s" % mapping_schema)
    is_v2_docx = ext == ".docx" and mapping_schema == "contract-mapping/v2"
    if not out_path:
        out_path = os.path.join(os.path.dirname(source) or ".",
                                _restore_name(source))
    targeted_restore = False
    strict_items: List[Dict[str, Any]] = []
    exact_output = False
    if is_v2_docx:
        comment_items = [row for row in mapping_items
                         if str((row.get("target") or {}).get("kind", "")).startswith("comment_")]
        output_info = mapping_payload.get("output") or {}
        revision_items = [row for row in mapping_items
                          if str((row.get("target") or {}).get("kind", "")).startswith("revision_")]
        if output_info.get("sha256"):
            exact_output = D._sha256(source) == str(output_info.get("sha256"))
            if not exact_output and not output_info.get("restore_compatibility_supported"):
                raise RuntimeError("脱敏DOCX已变化，mapping不支持结构兼容还原")
            strict_kinds = {"text", "revision_visible_text", "revision_rejected_text",
                            "revision_deleted_text", "revision_author"}
            strict_items = [row for row in mapping_items
                            if (row.get("target") or {}).get("kind") in strict_kinds]
            if len(strict_items) + len(comment_items) != len(mapping_items):
                raise RuntimeError("新mapping含未支持的DOCX目标，严格还原已中止")
            for row in strict_items:
                target = row.get("target") or {}
                if not target.get("output_text_sha256") or not target.get("output_structure_fingerprint"):
                    raise RuntimeError("新mapping缺少目标输出校验信息，严格还原已中止")
            targeted_restore = True
            regular_items = strict_items
            mapping: Dict[str, List[str]] = {}
            for row in sorted(strict_items, key=lambda item: (item.get("start", 0),
                                                               item.get("block_index", 0))):
                if row.get("placeholder") and row.get("original") is not None:
                    mapping.setdefault(row["placeholder"], []).append(str(row["original"]))
        else:
            if revision_items:
                raise RuntimeError("修订mapping缺少输出校验信息，严格还原已中止")
            regular_items = [row for row in mapping_items
                             if not str((row.get("target") or {}).get("kind", "")).startswith("comment_")]
            rows = sorted(enumerate(regular_items),
                          key=lambda pair: (pair[1].get("start", pair[0]), pair[1].get("block_index", pair[0])))
            mapping = {}
            for _index, row in rows:
                if row.get("placeholder") and row.get("original") is not None:
                    mapping.setdefault(row["placeholder"], []).append(str(row["original"]))
            source_doc = extract_docx(source)
            expected = {placeholder: len(values) for placeholder, values in mapping.items()}
            actual: Dict[str, int] = {}
            for block in source_doc.blocks:
                if block.field_kind != "text":
                    continue
                for placeholder in expected:
                    actual[placeholder] = actual.get(placeholder, 0) + block.text.count(placeholder)
            if any(actual.get(placeholder, 0) != count for placeholder, count in expected.items()):
                raise RuntimeError("mapping v2与DOCX中的占位符数量不一致，严格还原已中止")
    else:
        mapping = load_mapping_sequences(mapping_path)
        comment_items = []
    if not mapping and not (is_v2_docx and (comment_items or targeted_restore or
                                             external_target_items or vml_target_items)):
        raise RuntimeError(f"mapping.json 内无占位符项：{mapping_path}")

    if targeted_restore:
        shutil.copyfile(source, out_path)
        try:
            from contract_docx_revisions import restore_revision_occurrences
            target_result = restore_revision_occurrences(
                out_path, strict_items, compatible=not exact_output)
            if target_result.get("applied") != len(strict_items):
                raise RuntimeError("DOCX目标还原数量与mapping不一致")
            result = {"applied": int(target_result.get("applied", 0)),
                      "skipped": 0, "missing": []}
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise
    else:
        if ext in (".txt", ".md", ".markdown"):
            restorer = TextRestorer(source, mapping)
        else:
            restorer = DocxRestorer(source, mapping)
        result = restorer.restore(out_path)
    if is_v2_docx and external_target_items:
        try:
            from contract_docx_revisions import restore_external_hyperlink_targets
            external_result = restore_external_hyperlink_targets(
                out_path, external_target_items)
            if external_result.get("applied") != len(external_target_items):
                raise RuntimeError("外部超链接目标还原数量与mapping不一致")
            result["applied"] = int(result.get("applied", 0)) + int(
                external_result.get("applied", 0))
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise
    if is_v2_docx and vml_target_items:
        try:
            from contract_docx_revisions import restore_vml_text_targets
            vml_result = restore_vml_text_targets(out_path, vml_target_items)
            if vml_result.get("applied") != len(vml_target_items):
                raise RuntimeError("VML文字路径还原数量与mapping不一致")
            result["applied"] = int(result.get("applied", 0)) + int(
                vml_result.get("applied", 0))
        except Exception:
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise
    if is_v2_docx:
        if result.get("missing"):
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise RuntimeError("mapping v2与DOCX中的占位符不匹配，严格还原已中止")
        from contract_docx_comments import restore_comment_occurrences
        comment_result = restore_comment_occurrences(
            out_path, comment_items, strict=targeted_restore)
        if comment_result.get("mismatches"):
            if os.path.exists(out_path):
                os.unlink(out_path)
            raise RuntimeError("批注严格还原失败：" + "；".join(comment_result["mismatches"][:3]))
        result["applied"] = int(result.get("applied", 0)) + int(comment_result.get("applied", 0))
        result["applied_comments"] = comment_result.get("applied_comments", 0)
        result["applied_comment_identities"] = comment_result.get("applied_comment_identities", 0)

    log_path = None
    if write_log:
        log_path = out_path + ".restore.log"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump({
                "source": os.path.abspath(source),
                "output": os.path.abspath(out_path),
                "mapping": os.path.abspath(mapping_path),
                "mapping_source": "embedded" if mapping_path.endswith(".embedded-mapping.json") else "external",
                "applied": result["applied"],
                "missing_placeholders": result.get("missing", []),
                "items": [
                    {"placeholder": ph, "originals": orig}
                    for ph, orig in mapping.items()
                ],
            }, f, ensure_ascii=False, indent=2)

    return {
        "output": out_path,
        "applied": result["applied"],
        "skipped": result["skipped"],
        "missing": result.get("missing", []),
        "applied_comments": result.get("applied_comments", 0),
        "applied_comment_identities": result.get("applied_comment_identities", 0),
        "log": log_path,
    }


# ---------------------------------------------- 占位符模式脱敏 （嵌入主 redact_file） ------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="contract_redactor.py",
        description="合同脱敏改写器：把识别结果真正写到文件里（docx/pdf/txt）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python contract_redactor.py 合同.docx --selection 合同.selection.json
  python contract_redactor.py 合同.pdf --types ID_CARD,PHONE_MOBILE,PERSON_NAME
  python contract_redactor.py 合同.docx --types ID_CARD --restore-mode -o 合同_脱敏.docx
  python contract_redactor.py 合同_脱敏.docx --restore --mapping 合同_脱敏.docx.mapping.json
  python contract_redactor.py --serve --port 8765   # 让复核页一键调用
  python contract_redactor.py --app --port 8765     # 起完整的上传/复核/还原 Web 应用
""")
    ap.add_argument("input", nargs="?", help="待脱敏 / 待还原的文件（docx/pdf/txt）")
    ap.add_argument("--selection", help="detector 复核页导出的 selection.json")
    ap.add_argument("--report", help="detector 输出的 .sensitive.json（默认自动找同目录同名报告）")
    ap.add_argument("--types", help="直接指定要脱敏的类型，逗号分隔")
    ap.add_argument("--comments", choices=["redact-preserve", "preserve-unredacted", "remove-all"],
                    help="DOCX批注处理方式；默认保留并脱敏，诉讼外发建议显式指定 remove-all")
    ap.add_argument("--rules", help="脱敏规则清单 Markdown")
    ap.add_argument("--allowlist", help="白名单文件")
    ap.add_argument("--out", help="输出文件路径（默认原目录 <原名>_脱敏.<ext> 或 _还原.<ext>）")
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--min-severity", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--no-log", action="store_true", help="不写 .redaction.log")
    ap.add_argument("--allow-network", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="改写器内置自检")
    ap.add_argument("--serve", action="store_true", help="起 127.0.0.1 本地服务（兼容旧版）")
    ap.add_argument("--app", action="store_true", help="起 上传 / 复核 / 还原 Web 应用")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--doc-root", default=".", help="serve 模式下输出文件所在目录")
    ap.add_argument("--app-workdir", default=None,
                    help="应用模式下的会话工作目录（默认 ./contract_app_sessions）")
    ap.add_argument("--restore-mode", action="store_true",
                    help="脱敏时使用 [label#N] 占位符，并写出 mapping.json 以便还原")
    ap.add_argument("--restore", action="store_true",
                    help="把已脱敏的 docx 通过 mapping.json 还原回原文")
    ap.add_argument("--mapping", help="还原模式下使用的 mapping.json（默认自动按文件名匹配）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    if not args.allow_network:
        # 服务 / 应用模式需要绑定 TCP 端口，自动开闸；其它情况仍默认离线
        if not (args.serve or args.app):
            D.enforce_offline()

    if args.selftest:
        return redactor_selftest()

    if args.app:
        workdir = args.app_workdir or os.path.abspath("./contract_app_sessions")
        srv = start_app_server(args.host, args.port, workdir)
        sys.stderr.write(
            f"[app] Contract Redactor 应用模式 → http://{args.host}:{args.port}\n"
            f"[app] 会话目录：{workdir}\n"
            "[app] 浏览器若没自动打开，请手动访问上方的链接。Ctrl-C 退出。\n"
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("[app] shutting down...\n")
        return 0

    if args.serve:
        doc_root = os.path.abspath(args.doc_root)
        os.makedirs(doc_root, exist_ok=True)
        srv = start_serve(args.host, args.port, doc_root)
        sys.stderr.write(
            f"[serve] {ENGINE_NAME} v{__version__} listening on "
            f"http://{args.host}:{args.port}  (output dir: {doc_root})\n"
            "[serve] 在复核页点击「确认并执行脱敏改写」即可调用。Ctrl-C 退出。\n"
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("[serve] shutting down...\n")
        return 0

    if not args.input:
        ap.print_help()
        return 2

    # --- 还原分支 ---
    if args.restore:
        try:
            r = restore_file(
                source=args.input,
                mapping_path=args.mapping,
                out_path=args.out,
            )
        except Exception as e:
            print(f"[错误] 还原失败：{e}", file=sys.stderr)
            return 1
        print(f"\n■ 还原完成 → {r['output']}")
        print(f"   替换 {r['applied']} 处")
        if r.get("missing"):
            print(f"   ⚠ {len(r['missing'])} 个占位符未在文档中找到（mapping 比文档更全）")
        if r["log"]:
            print(f"   审计日志 → {r['log']}")
        return 0

    allowlist = load_allowlist(args.allowlist)

    selection = None
    if args.selection:
        selection = load_selection(args.selection)

    types = None
    if args.types:
        types = [t.strip() for t in args.types.split(",") if t.strip()]

    try:
        r = redact_file(
            source=args.input,
            selection=selection,
            types=types,
            out_path=args.out,
            rules_path=args.rules,
            min_confidence=args.min_confidence,
            min_severity=args.min_severity,
            allowlist=list(allowlist),
            write_log=not args.no_log,
            restore_mode=args.restore_mode,
            comment_mode=args.comments.replace("-", "_") if args.comments else None,
        )
    except Exception as e:
        print(f"[错误] 改写失败：{e}", file=sys.stderr)
        return 1

    print(f"\n■ 脱敏完成 → {r['output']}")
    print(f"   替换 {r['applied']} 处，跳过 {r['skipped']} 处")
    if r["log"]:
        print(f"   审计日志 → {r['log']}")
    if r.get("mapping"):
        print(f"   映射文件 → {r['mapping']}  （占位符 ↔ 原文，支持 --restore 还原）")
    return 0


def redactor_selftest() -> int:
    """内置自检：构造一个临时 txt + 几个 ops，跑 apply_to_text 验证。"""
    text = "甲方张三，手机 13812345678，身份证 440305199001012347，住北京市海淀区中关村南大街5号。\n" \
           "金额：人民币 1,280,000.00 元。\n"
    ops = [
        RedactOp(start=text.index("张三"), end=text.index("张三") + 2,
                 type_id="PERSON_NAME", label="姓名", severity="high", confidence=0.9,
                 value="张三", replacement="张**", location="L1"),
        RedactOp(start=text.index("13812345678"), end=text.index("13812345678") + 11,
                 type_id="PHONE_MOBILE", label="手机", severity="high", confidence=0.95,
                 value="13812345678", replacement="138****5678", location="L1"),
        RedactOp(start=text.index("440305199001012347"),
                 end=text.index("440305199001012347") + 18,
                 type_id="ID_CARD", label="身份证", severity="high", confidence=0.99,
                 value="440305199001012347", replacement="440305********2347", location="L1"),
    ]
    red = TextRedactor("<selftest>", text, ops)
    out, applied, skipped = red.apply_to_text(text)
    fails = []
    if "张**" not in out: fails.append("姓名未脱敏")
    if "138****5678" not in out: fails.append("手机号未脱敏")
    if "440305********2347" not in out: fails.append("身份证未脱敏")
    if applied != 3: fails.append(f"applied={applied} (应为 3)")
    print("== 改写器自检 ==")
    if fails:
        for f in fails: print("  [FAIL]", f)
        return 1
    print("  [PASS] 文本改写正确（applied=3, skipped=0）")
    print(f"  原文 → 脱敏后：\n  {out.strip()[:120]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
