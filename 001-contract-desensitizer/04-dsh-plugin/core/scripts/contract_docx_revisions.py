#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读盘点 DOCX 包中的 Word 修订结构。"""
from __future__ import annotations

import posixpath
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
import urllib.parse
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from lxml import etree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{" + W_NS + "}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
VML = "{urn:schemas-microsoft-com:vml}"
OFFICE = "{urn:schemas-microsoft-com:office:office}"
OFFICE_WORD = "{urn:schemas-microsoft-com:office:word}"
OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MAX_UNPACKED_BYTES = 150 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_DISPLAY_OBSERVATIONS = 250
MAX_DISPLAY_TEXT_CHARS = 240
MAX_DISPLAY_AUTHORS = 100
CONTENT_TYPES = "{http://schemas.openxmlformats.org/package/2006/content-types}"
NOTE_CONTENT_TYPES = {
    "footnotes": ("footnotes", "footnote",
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"),
    "endnotes": ("endnotes", "endnote",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"),
}

REVISION_TAGS = {
    "ins": "新增文字修订",
    "del": "删除文字修订",
    "delText": "删除修订文字节点",
    "moveFrom": "移动前文字修订",
    "moveTo": "移动后文字修订",
    "moveFromRangeStart": "移动范围标记",
    "moveFromRangeEnd": "移动范围标记",
    "moveToRangeStart": "移动范围标记",
    "moveToRangeEnd": "移动范围标记",
    "customXmlInsRangeStart": "自定义XML插入范围标记",
    "customXmlInsRangeEnd": "自定义XML插入范围标记",
    "customXmlDelRangeStart": "自定义XML删除范围标记",
    "customXmlDelRangeEnd": "自定义XML删除范围标记",
    "customXmlMoveFromRangeStart": "自定义XML移动范围标记",
    "customXmlMoveFromRangeEnd": "自定义XML移动范围标记",
    "customXmlMoveToRangeStart": "自定义XML移动范围标记",
    "customXmlMoveToRangeEnd": "自定义XML移动范围标记",
    "rPrChange": "文字属性修订",
    "pPrChange": "段落属性修订",
    "sectPrChange": "分节属性修订",
    "tblPrChange": "表格属性修订",
    "tblGridChange": "表格网格修订",
    "trPrChange": "表格行属性修订",
    "tcPrChange": "表格单元格属性修订",
    "numberingChange": "编号属性修订",
    "cellIns": "单元格插入修订",
    "cellDel": "单元格删除修订",
    "cellMerge": "单元格合并修订",
    "conflictIns": "冲突插入修订（未支持）",
    "conflictDel": "冲突删除修订（未支持）",
}

TEXT_REVISION_TAGS = {"ins", "del", "moveFrom", "moveTo"}
PROPERTY_REVISION_TAGS = {
    "rPrChange": "rPr",
    "pPrChange": "pPr",
    "sectPrChange": "sectPr",
    "tblPrChange": "tblPr",
    "tblGridChange": "tblGrid",
    "trPrChange": "trPr",
    "tcPrChange": "tcPr",
}
W16DU = "{http://schemas.microsoft.com/office/word/2023/wordml/word16du}"
PARAGRAPH_MARK_REVISION_ATTRS = {
    W + "id", W + "author", W + "date", W16DU + "dateUtc",
}
SAFE_PAGE_FIELD = re.compile(
    r"^\s*(?:PAGE|NUMPAGES|SECTIONPAGES)"
    r"(?:\s+\\\*\s+(?:MERGEFORMAT|ARABIC|ROMAN))?\s*$",
    re.IGNORECASE,
)
SAFE_NUMBER_FORMAT_FIELD = re.compile(
    r"^\s*=\s*(?:[0-9]|[1-9][0-9])\s+\\\*\s+(?:GB[1-4]|ARABIC)\s*$",
    re.IGNORECASE,
)


def _is_paragraph_mark_revision(element, paragraph) -> bool:
    if element.tag not in {W + "ins", W + "del"} or paragraph is None:
        return False
    run_properties = element.getparent()
    paragraph_properties = (run_properties.getparent()
                            if run_properties is not None else None)
    if (run_properties is None or run_properties.tag != W + "rPr" or
            paragraph_properties is None or paragraph_properties.tag != W + "pPr" or
            paragraph_properties.getparent() is not paragraph):
        return False
    if len(element) or (element.text or "").strip():
        return False
    if any(attribute not in PARAGRAPH_MARK_REVISION_ATTRS
           for attribute in element.attrib):
        return False
    return bool(element.get(W + "id", ""))


def _classify_field_code(code: str) -> Tuple[bool, str]:
    normalized = " ".join(code.split())
    if not normalized:
        return False, "字段指令为空或无法重组"
    if re.fullmatch(r"FORMTEXT", normalized, re.IGNORECASE):
        return True, "标准表单文字字段"
    if SAFE_PAGE_FIELD.fullmatch(normalized):
        return True, "安全页码字段"
    if SAFE_NUMBER_FORMAT_FIELD.fullmatch(normalized):
        return True, "安全数字格式字段"
    return False, "字段指令类型未纳入安全范围"


def _field_instruction_inventory(part: str, root) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    paragraphs = list(root.iter(W + "p"))
    paragraph_indices = {paragraph: index for index, paragraph in enumerate(paragraphs)}
    seen_instruction_nodes: Set[Any] = set()
    stack: List[Dict[str, Any]] = []

    def add_record(node, code: str, safe: bool, reason: str) -> None:
        paragraph = _nearest_paragraph(node)
        normalized = " ".join(code.split())
        field_type = (normalized.split(" ", 1)[0].upper()
                      if normalized and not normalized.startswith("=") else "FORMULA")
        if not re.fullmatch(r"[A-Z]{1,24}", field_type):
            field_type = "OTHER"
        records.append({
            "part": part,
            "paragraph_index": paragraph_indices.get(paragraph, -1),
            "ordinal": len(records),
            "safe": safe,
            "reason": reason,
            "kind": "field",
            "field_type": field_type,
            "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        })

    for node in root.iter():
        if _inside_alternate_content(node):
            if stack and node.tag in {W + "fldSimple", W + "fldChar", W + "instrText"}:
                stack[-1]["contains_nested"] = True
            continue
        if node.tag == W + "fldSimple":
            if stack:
                stack[-1]["contains_nested"] = True
            code = node.get(W + "instr", "")
            safe, reason = _classify_field_code(code)
            nested_content = any(
                child is not node and child.tag in {
                    W + "fldSimple", W + "fldChar", W + "instrText",
                }
                for child in node.iter()
            )
            if nested_content:
                safe, reason = False, "字段包含嵌套结构"
            add_record(node, code, safe, reason)
            continue

        if node.tag == W + "fldChar":
            field_type = node.get(W + "fldCharType", "")
            if field_type == "begin":
                if stack:
                    stack[-1]["contains_nested"] = True
                nested_simple = any(ancestor.tag == W + "fldSimple"
                                    for ancestor in node.iterancestors())
                stack.append({
                    "node": node,
                    "parts": [],
                    "has_separator": False,
                    "invalid": nested_simple,
                    "contains_nested": False,
                })
            elif field_type == "separate":
                if stack and not stack[-1]["has_separator"]:
                    stack[-1]["has_separator"] = True
                elif stack:
                    stack[-1]["invalid"] = True
                else:
                    add_record(node, "", False, "字段显示分隔标记缺少起始标记")
            elif field_type == "end":
                if not stack:
                    add_record(node, "", False, "字段结束标记缺少起始标记")
                    continue
                state = stack.pop()
                code = " ".join("".join(state["parts"]).split())
                safe, reason = _classify_field_code(code)
                if state["contains_nested"]:
                    safe, reason = False, "字段包含嵌套结构"
                elif state["invalid"]:
                    safe, reason = False, "字段结构不符合受支持格式"
                elif not state["has_separator"] and not (safe and SAFE_PAGE_FIELD.fullmatch(code)):
                    safe, reason = False, "字段缺少显示结果分隔标记"
                add_record(state["node"], code, safe, reason)
            else:
                add_record(node, "", False, "字段标记类型无法识别")
            continue

        if node.tag == W + "instrText":
            if any(ancestor.tag == W + "fldSimple"
                   for ancestor in node.iterancestors()):
                continue
            if stack:
                seen_instruction_nodes.add(node)
                if not stack[-1]["has_separator"]:
                    stack[-1]["parts"].append(node.text or "")

    for state in stack:
        code = " ".join("".join(state["parts"]).split())
        add_record(state["node"], code, False, "字段缺少结束标记")
    for node in root.iter(W + "instrText"):
        if (not _inside_alternate_content(node) and
                node not in seen_instruction_nodes and
                not any(ancestor.tag == W + "fldSimple"
                        for ancestor in node.iterancestors())):
            add_record(node, node.text or "", False, "字段指令缺少完整字段边界")
    return records


def _empty_inventory() -> Dict[str, Any]:
    return {
        "complete": False,
        "has_revisions": False,
        "revision_count": 0,
        "author_count": 0,
        "parts_scanned": 0,
        "tracked_changes_enabled": False,
        "records": [],
        "observations": [],
        "observations_truncated": False,
        "authors": [],
        "authors_truncated": False,
        "can_export_after_ack": False,
        "supported_text_revision_count": 0,
        "supported_property_revision_count": 0,
        "unsupported_manifest": [],
        "unsupported_digest": "",
        "unsupported_revision_count": 0,
        "field_instruction_count": 0,
        "safe_field_instruction_count": 0,
        "unsafe_field_instruction_count": 0,
        "unsafe_field_manifest": [],
        "unsafe_field_digest": "",
        "embedded_image_count": 0,
        "text_object_count": 0,
        "header_footer_content_parts": [],
        "header_footer_digest": hashlib.sha256(b"[]").hexdigest(),
        "unsupported_object_manifest": [],
        "object_digest": "",
        "hidden_text_count": 0,
        "hidden_text_manifest": [],
        "unredacted_content_parts": [],
        "unredacted_content_digest": hashlib.sha256(b"[]").hexdigest(),
        "note_part_manifest": [],
        "note_structure_digest": hashlib.sha256(b"[]").hexdigest(),
        "unscanned_parts": [],
        "unscanned_digest": "",
        "passthrough_safe": False,
        "external_relationship_count": 0,
        "external_relationship_manifest": [],
        "external_relationship_digest": "",
        "external_relationships_valid": False,
        "errors": [],
    }


def _revision_node_ordinals(root) -> Dict[Any, int]:
    counts: Dict[str, int] = {}
    result: Dict[Any, int] = {}
    for node in root.iter():
        if not isinstance(node.tag, str) or not node.tag.startswith(W):
            continue
        local = node.tag[len(W):]
        result[node] = counts.get(local, 0)
        counts[local] = result[node] + 1
    return result


def _revision_location(element, paragraph_indices: Dict[Any, int]) -> str:
    paragraph = _nearest_paragraph(element)
    if paragraph is not None and paragraph in paragraph_indices:
        return "第" + str(paragraph_indices[paragraph] + 1) + "段"
    return "XML路径" + element.getroottree().getpath(element)


def _unsupported_record(part: str, element, reason: str,
                        ordinal: int, location: str) -> Dict[str, Any]:
    if reason.startswith("兼容文本框"):
        canonical = _object_canonical_bytes(element)
    else:
        canonical = ET.tostring(element, method="c14n", exclusive=True,
                                with_comments=False)
    return {
        "part": part,
        "kind": element.tag[len(W):],
        "reason": reason,
        "ordinal": ordinal,
        "location": location,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _revision_text(element) -> str:
    pieces = []
    for node in element.iter():
        if node is element or not isinstance(node.tag, str):
            continue
        if node.tag in {W + "t", W + "delText"} and node.text:
            pieces.append(node.text)
    return "".join(pieces)


def _revision_observation(element, part: str, label: str,
                          paragraph_indices: Dict[Any, int]) -> Dict[str, Any]:
    paragraph = element
    while paragraph is not None and paragraph.tag != W + "p":
        paragraph = paragraph.getparent()
    if paragraph is not None and paragraph in paragraph_indices:
        location = "段落" + str(paragraph_indices[paragraph] + 1)
    else:
        location = "XML路径" + element.getroottree().getpath(element)

    local = element.tag.split("}")[-1]
    text = _revision_text(element) if local in TEXT_REVISION_TAGS else ""
    return {
        "part": part,
        "kind": label,
        "location": location,
        "author": (element.get(W + "author") or "").strip(),
        "text": text[:MAX_DISPLAY_TEXT_CHARS],
        "text_truncated": len(text) > MAX_DISPLAY_TEXT_CHARS,
    }


def inspect_docx_revisions(path: str) -> Dict[str, Any]:
    """Read revision metadata and text without changing the source package."""
    result = _empty_inventory()
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if len(members) > MAX_MEMBERS:
                result["errors"].append("DOCX部件数量超过盘点上限")
                return result
            if len(names) != len(set(names)):
                result["errors"].append("DOCX含重复部件名")
                return result
            if sum(item.file_size for item in members) > MAX_UNPACKED_BYTES:
                result["errors"].append("DOCX解压体积超过盘点上限")
                return result

            parser = ET.XMLParser(resolve_entities=False, no_network=True,
                                  huge_tree=False, recover=False)
            kind_counts: Dict[str, Counter] = {}
            authors: Set[str] = set()
            observations: List[Dict[str, Any]] = []
            observations_truncated = False
            parsed_parts = set()
            unscanned_parts = []
            external_relationships = []
            external_relationships_valid = True
            hidden_text_parts: Dict[str, int] = {}
            for item in members:
                name = item.filename
                if item.is_dir() or not name.lower().endswith((".xml", ".vml", ".rels")):
                    continue
                try:
                    root = ET.fromstring(archive.read(name), parser=parser)
                except Exception:
                    result["errors"].append("无法解析XML部件：" + name)
                    raw = archive.read(name)
                    unscanned_parts.append({
                        "part": name,
                        "size": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    })
                    continue
                parsed_parts.add(name)
                if (name == "word/document.xml" or
                        re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)):
                    hidden_runs = set()
                    for marker in root.iter():
                        if marker.tag not in {W + "vanish", W + "webHidden", W + "specVanish"}:
                            continue
                        run = next((ancestor for ancestor in marker.iterancestors()
                                    if ancestor.tag == W + "r"), None)
                        if run is not None and any(node.tag in {W + "t", W + "delText"}
                                                   for node in run.iter()):
                            hidden_runs.add(run)
                    if hidden_runs:
                        hidden_text_parts[name] = len(hidden_runs)
                if name.lower().endswith(".rels"):
                    if root.tag != REL + "Relationships":
                        result["errors"].append("关系部件结构无效：" + name)
                        external_relationships_valid = False
                        continue
                    relationship_ids = set()
                    for rel in root:
                        if rel.tag != REL + "Relationship":
                            result["errors"].append("关系部件含无效节点：" + name)
                            external_relationships_valid = False
                            continue
                        relationship_id = rel.get("Id", "")
                        target = rel.get("Target", "")
                        rel_type = rel.get("Type", "")
                        if (not relationship_id or relationship_id in relationship_ids or
                                not target or not rel_type):
                            result["errors"].append("关系部件含不完整或重复关系：" + name)
                            external_relationships_valid = False
                            continue
                        relationship_ids.add(relationship_id)
                        if rel.get("TargetMode", "").lower() == "external":
                            external_relationships.append({
                                "part": name,
                                "id": relationship_id,
                                "kind": rel_type.rsplit("/", 1)[-1],
                                "target_sha256": hashlib.sha256(
                                    target.encode("utf-8")).hexdigest(),
                            })
                result["parts_scanned"] += 1
                paragraph_indices = {paragraph: index for index, paragraph in
                                     enumerate(root.iter(W + "p"))}
                for element in root.iter():
                    if not isinstance(element.tag, str) or not element.tag.startswith(W):
                        continue
                    local = element.tag[len(W):]
                    if name == "word/settings.xml" and local == "trackRevisions":
                        value = (element.get(W + "val") or "1").strip().lower()
                        if value not in {"0", "false", "off", "no"}:
                            result["tracked_changes_enabled"] = True
                    label = REVISION_TAGS.get(local)
                    if label is None and local.endswith("Change"):
                        label = "其他属性修订（未支持）"
                    if label is None:
                        continue
                    counts = kind_counts.setdefault(name, Counter())
                    counts[label] += 1
                    author = element.get(W + "author")
                    if author and author.strip():
                        authors.add(author.strip())
                    if label and local != "delText":
                        if len(observations) < MAX_DISPLAY_OBSERVATIONS:
                            observations.append(_revision_observation(
                                element, name, label, paragraph_indices))
                        else:
                            observations_truncated = True

            try:
                note_inventory = _note_content_inventory(archive, set(names))
            except (ValueError, ET.XMLSyntaxError) as exc:
                result["errors"].append("脚注或尾注结构无效：" + str(exc))
                note_inventory = {
                    "parts": [], "note_ids": {}, "digest": hashlib.sha256(b"[]").hexdigest(),
                    "structure_digest": hashlib.sha256(b"[]").hexdigest(),
                }
            result["note_part_manifest"] = note_inventory["parts"]
            result["unredacted_content_parts"] = [
                row for row in note_inventory["parts"] if int(row.get("count") or 0) > 0]
            result["unredacted_content_digest"] = note_inventory["digest"]
            result["note_structure_digest"] = note_inventory["structure_digest"]
            result["hidden_text_manifest"] = [
                {"part": part, "count": count}
                for part, count in sorted(hidden_text_parts.items())]
            result["hidden_text_count"] = sum(hidden_text_parts.values())

            records = []
            for part_name in sorted(kind_counts, key=posixpath.normcase):
                for label, count in sorted(kind_counts[part_name].items()):
                    records.append({"part": part_name, "kind": label, "count": count})
            result["records"] = records
            result["revision_count"] = sum(row["count"] for row in records)
            result["author_count"] = len(authors)
            result["authors"] = sorted(authors)[:MAX_DISPLAY_AUTHORS]
            result["authors_truncated"] = len(authors) > MAX_DISPLAY_AUTHORS
            result["observations"] = observations
            result["observations_truncated"] = observations_truncated
            result["has_revisions"] = result["revision_count"] > 0
            critical_parts = {
                "[Content_Types].xml", "_rels/.rels", "word/document.xml",
                "word/_rels/document.xml.rels",
            }
            unscanned_digest = hashlib.sha256(json.dumps(
                unscanned_parts, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            hard_parse_errors = [row for row in unscanned_parts
                                 if row["part"] in critical_parts]
            result["unscanned_parts"] = unscanned_parts
            result["unscanned_digest"] = unscanned_digest
            external_relationships.sort(key=lambda row: (
                row["part"], row["kind"], row["id"], row["target_sha256"]))
            result["external_relationship_count"] = len(external_relationships)
            result["external_relationship_manifest"] = external_relationships
            result["external_relationships_valid"] = external_relationships_valid
            result["external_relationship_digest"] = hashlib.sha256(json.dumps(
                external_relationships, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            unscanned_relationship_parts = any(
                row["part"].lower().endswith(".rels") for row in unscanned_parts)
            result["passthrough_safe"] = bool(
                unscanned_parts and not hard_parse_errors and
                critical_parts.issubset(parsed_parts) and
                not unscanned_relationship_parts and external_relationships_valid)
            result["complete"] = not result["errors"]
            return result
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        result["errors"].append("DOCX包无法安全读取")
        return result


REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "{" + REL_NS + "}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
REVISION_UNSUPPORTED_CONTENT = {
    "drawing", "pict", "object", "altChunk", "fldSimple", "instrText",
    "delInstrText", "fldChar", "vanish", "webHidden", "specVanish",
}
PROPERTY_CONTAINERS = {
    "pPr", "rPr", "tblPr", "tblGrid", "trPr", "tcPr", "sectPr",
    "numbering", "abstractNum", "num", "style", "styles",
}


def iter_story_content_nodes(paragraph, include_removed=False):
    """Yield one paragraph's content nodes, excluding properties and nested paragraphs."""
    def walk(node):
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if child.tag in {W + name for name in PROPERTY_CONTAINERS}:
                continue
            if child.tag == W + "p":
                continue
            if not include_removed and child.tag in {W + "del", W + "moveFrom"}:
                continue
            yield child
            yield from walk(child)

    yield from walk(paragraph)


def _relationship_part(owner: str) -> str:
    folder, leaf = posixpath.split(owner)
    return posixpath.join(folder, "_rels", leaf + ".rels")


def _resolve_part(owner: str, target: str) -> str:
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))
    if resolved == ".." or resolved.startswith("../") or "\\" in resolved:
        raise ValueError("DOCX关系目标越出包目录")
    return resolved


def _note_content_inventory(archive, names: Set[str]) -> Dict[str, Any]:
    """Inventory valid footnote/endnote bodies that are retained without auto-redaction."""
    rels_name = "word/_rels/document.xml.rels"
    document_name = "word/document.xml"
    content_types_name = "[Content_Types].xml"
    if rels_name not in names or document_name not in names or content_types_name not in names:
        if any(name in names for name in ("word/footnotes.xml", "word/endnotes.xml")):
            raise ValueError("注释部件缺少主文档关系或内容类型部件")
        return {
            "parts": [], "note_ids": {},
            "digest": hashlib.sha256(b"[]").hexdigest(),
            "structure_digest": hashlib.sha256(b"[]").hexdigest(),
        }

    document_root = _parse_xml(archive.read(document_name))
    relationships_root = _parse_xml(archive.read(rels_name))
    content_types_root = _parse_xml(archive.read(content_types_name))
    if relationships_root.tag != REL + "Relationships":
        raise ValueError("主文档关系部件结构无效")
    if content_types_root.tag != CONTENT_TYPES + "Types":
        raise ValueError("内容类型部件结构无效")

    overrides: Dict[str, List[str]] = {}
    for node in content_types_root:
        if node.tag == CONTENT_TYPES + "Override":
            overrides.setdefault(node.get("PartName", "").lstrip("/"), []).append(
                node.get("ContentType", ""))

    note_ids: Dict[str, Set[str]] = {}
    parts = []
    linked_parts = set()
    for rel in relationships_root:
        if rel.tag != REL + "Relationship":
            continue
        kind = rel.get("Type", "").rsplit("/", 1)[-1]
        if kind not in NOTE_CONTENT_TYPES:
            continue
        if rel.get("TargetMode", "").lower() == "external":
            raise ValueError("脚注或尾注关系不能指向外部目标")
        part = _resolve_part(document_name, rel.get("Target", ""))
        if part not in names or part in linked_parts:
            raise ValueError("脚注或尾注关系目标缺失或重复")
        linked_parts.add(part)
        root_name, local_name, expected_content_type = NOTE_CONTENT_TYPES[kind]
        note_root = _parse_xml(archive.read(part))
        if note_root.tag != W + root_name:
            raise ValueError("脚注或尾注XML根节点无效")
        if overrides.get(part) != [expected_content_type]:
            raise ValueError("脚注或尾注内容类型声明缺失或不匹配")

        notes = list(note_root.iter(W + local_name))
        ids = [node.get(W + "id", "") for node in notes]
        if not all(ids) or len(ids) != len(set(ids)):
            raise ValueError("脚注或尾注编号缺失或重复")
        note_ids[local_name] = set(ids)
        body_notes = [node for node in notes if node.get(W + "id") not in {"-1", "0"}]
        note_text_rows = []
        for note in body_notes:
            text = "".join(
                node.text or "" for node in note.iter()
                if node.tag in {W + "t", W + "delText", W + "instrText", W + "delInstrText"})
            note_text_rows.append({
                "id": note.get(W + "id"),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
        raw = archive.read(part)
        parts.append({
            "part": part,
            "kind": kind,
            "count": len(body_notes),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "ids_digest": hashlib.sha256(json.dumps(
                sorted(note_ids[local_name]), separators=(",", ":")).encode("utf-8")).hexdigest(),
            "text_digest": hashlib.sha256(json.dumps(
                note_text_rows, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
        })

    for standard_part in ("word/footnotes.xml", "word/endnotes.xml"):
        if standard_part in names and standard_part not in linked_parts:
            raise ValueError("脚注或尾注部件未关联到主文档")
    for local_name, reference_name in (("footnote", "footnoteReference"),
                                       ("endnote", "endnoteReference")):
        references = {node.get(W + "id", "") for node in
                      document_root.iter(W + reference_name)}
        missing = references - note_ids.get(local_name, set()) - {"-1", "0"}
        if missing:
            raise ValueError("正文中的脚注或尾注引用找不到对应内容")

    parts.sort(key=lambda row: (row["part"], row["kind"]))
    digest_rows = [{key: row[key] for key in ("part", "kind", "count", "sha256")}
                   for row in parts]
    structure_rows = [{key: row[key] for key in
                       ("part", "kind", "count", "ids_digest", "text_digest")}
                      for row in parts]
    return {
        "parts": parts,
        "note_ids": note_ids,
        "digest": hashlib.sha256(json.dumps(
            digest_rows, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest(),
        "structure_digest": hashlib.sha256(json.dumps(
            structure_rows, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest(),
    }


def _parse_xml(data: bytes):
    parser = ET.XMLParser(resolve_entities=False, load_dtd=False,
                          no_network=True, huge_tree=False, recover=False)
    root = ET.fromstring(data, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError("DOCX XML不允许包含DTD")
    return root


def _serialize(root) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def read_external_hyperlink_targets(path: str) -> List[Dict[str, Any]]:
    """Return external hyperlink targets for local candidate scanning only."""
    rows = []
    with zipfile.ZipFile(path, "r") as archive:
        for part in sorted(name for name in archive.namelist()
                           if name.lower().endswith(".rels")):
            try:
                root = _parse_xml(archive.read(part))
            except Exception as exc:
                raise ValueError("DOCX关系部件无法安全检查") from exc
            if root.tag != REL + "Relationships":
                raise ValueError("DOCX关系部件结构无效")
            seen = set()
            for rel in root:
                if rel.tag != REL + "Relationship":
                    raise ValueError("DOCX关系部件含无效节点")
                rid = rel.get("Id", "")
                if not rid or rid in seen:
                    raise ValueError("DOCX关系部件含不完整或重复关系")
                seen.add(rid)
                rel_type = rel.get("Type", "")
                target = rel.get("Target", "")
                if (rel.get("TargetMode", "").lower() == "external" and
                        rel_type.rsplit("/", 1)[-1].lower() == "hyperlink" and target):
                    rows.append({
                        "part": part,
                        "relationship_id": rid,
                        "target_sha256": hashlib.sha256(
                            target.encode("utf-8")).hexdigest(),
                        "target": target,
                    })
    return rows


def revision_passthrough_digest(path: str) -> str:
    """Hash revision-bearing paragraphs and standalone revision nodes."""
    revision_tags = {W + local for local in REVISION_TAGS}
    rows = []
    with zipfile.ZipFile(path, "r") as archive:
        for part in sorted(name for name in archive.namelist()
                           if name.lower().startswith("word/") and
                           name.lower().endswith(".xml") and
                           not name.lower().endswith(".rels")):
            root = _parse_xml(archive.read(part))
            preserved = set()
            for node in root.iter():
                if node.tag not in revision_tags:
                    continue
                paragraph = next((ancestor for ancestor in node.iterancestors()
                                  if ancestor.tag == W + "p"), None)
                preserved.add(paragraph if paragraph is not None else node)
            for node in preserved:
                rows.append((part, ET.tostring(node, method="c14n").decode("utf-8")))
    encoded = json.dumps(sorted(rows), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rewrite_external_hyperlink_targets(docx_path: str, operations: List[Any]) -> Dict[str, int]:
    """Apply selected target spans and attach strict restore hashes to operations."""
    if not operations:
        return {"applied": 0}
    grouped: Dict[Tuple[str, str], List[Any]] = {}
    for op in operations:
        target = op.target or {}
        if (target.get("kind") != "external_relationship_target" or
                target.get("attribute") != "Target"):
            raise ValueError("外部超链接目标类型无效")
        grouped.setdefault((str(target.get("part", "")),
                            str(target.get("relationship_id", ""))), []).append(op)

    replacements: Dict[str, bytes] = {}
    applied = 0
    with zipfile.ZipFile(docx_path, "r") as archive:
        names = set(archive.namelist())
        for (part, rid), ops in grouped.items():
            if not part.endswith(".rels") or part not in names or not rid:
                raise ValueError("外部超链接目标位置无效")
            root = _parse_xml(archive.read(part))
            matches = [rel for rel in root if rel.get("Id") == rid]
            if len(matches) != 1:
                raise ValueError("外部超链接关系缺失或不唯一")
            rel = matches[0]
            if (rel.get("TargetMode", "").lower() != "external" or
                    rel.get("Type", "").rsplit("/", 1)[-1].lower() != "hyperlink"):
                raise ValueError("外部超链接关系类型已变化")
            original = rel.get("Target", "")
            source_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
            expected_hashes = {str((op.target or {}).get("target_sha256") or "")
                               for op in ops}
            if expected_hashes != {source_hash}:
                raise ValueError("外部超链接目标已变化，请重新检查")
            ordered = sorted(ops, key=lambda op: (op.local_start, op.local_end))
            cursor = -1
            pieces = []
            for order, op in enumerate(ordered):
                start, end = int(op.local_start), int(op.local_end)
                if start < cursor or start < 0 or end <= start or end > len(original):
                    raise ValueError("外部超链接目标区间无效或重叠")
                if original[start:end] != op.value:
                    raise ValueError("外部超链接目标与扫描文本不一致")
                encoded = urllib.parse.quote(str(op.replacement), safe="-._~")
                if not encoded.isascii():
                    raise ValueError("外部超链接占位符不是URI安全文本")
                pieces.extend((original[cursor + 1:start], encoded))
                cursor = end - 1
                op.target.update(
                    attribute="Target", source_target_sha256=source_hash,
                    uri_written=encoded, target_order=order)
                applied += 1
            pieces.append(original[cursor + 1:])
            rewritten = "".join(pieces)
            rel.set("Target", rewritten)
            output_hash = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
            for op in ordered:
                op.target["output_target_sha256"] = output_hash
            replacements[part] = _serialize(root)

    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(os.path.abspath(docx_path)))
    os.close(fd)
    try:
        with zipfile.ZipFile(docx_path, "r") as source, \
                zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as output:
            for info in source.infolist():
                output.writestr(info, replacements.get(info.filename,
                                                       source.read(info.filename)))
        os.replace(tmp, docx_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"applied": applied}


def restore_external_hyperlink_targets(docx_path: str,
                                       records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Restore target spans only when relationship identity and both hashes match."""
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in records or []:
        if (not isinstance(row, dict) or row.get("attribute") != "Target" or
                row.get("kind") != "hyperlink"):
            raise RuntimeError("mapping外部超链接目标记录无效")
        grouped.setdefault((str(row.get("part", "")),
                            str(row.get("relationship_id", ""))), []).append(row)
    if not grouped:
        return {"applied": 0}

    replacements: Dict[str, bytes] = {}
    applied = 0
    with zipfile.ZipFile(docx_path, "r") as archive:
        names = set(archive.namelist())
        for (part, rid), rows in grouped.items():
            if not part.endswith(".rels") or part not in names or not rid:
                raise RuntimeError("mapping外部超链接目标部件无效")
            root = _parse_xml(archive.read(part))
            matches = [rel for rel in root if rel.get("Id") == rid]
            if len(matches) != 1:
                raise RuntimeError("mapping外部超链接关系缺失或不唯一")
            rel = matches[0]
            if (rel.get("TargetMode", "").lower() != "external" or
                    rel.get("Type", "").rsplit("/", 1)[-1].lower() != "hyperlink"):
                raise RuntimeError("mapping外部超链接关系类型已变化")
            current = rel.get("Target", "")
            output_hashes = {str(row.get("output_target_sha256") or "") for row in rows}
            source_hashes = {str(row.get("source_target_sha256") or "") for row in rows}
            if (len(output_hashes) != 1 or not next(iter(output_hashes)) or
                    hashlib.sha256(current.encode("utf-8")).hexdigest() != next(iter(output_hashes)) or
                    len(source_hashes) != 1 or not next(iter(source_hashes))):
                raise RuntimeError("外部超链接目标与mapping摘要不一致")
            restored = current
            for row in sorted(rows, key=lambda item: int(item.get("order", 0))):
                written = str(row.get("written") or "")
                original = str(row.get("original") or "")
                if not written or written not in restored:
                    raise RuntimeError("外部超链接占位符缺失，严格还原已中止")
                restored = restored.replace(written, original, 1)
                applied += 1
            if hashlib.sha256(restored.encode("utf-8")).hexdigest() != next(iter(source_hashes)):
                raise RuntimeError("外部超链接还原结果与源目标摘要不一致")
            rel.set("Target", restored)
            replacements[part] = _serialize(root)

    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(os.path.abspath(docx_path)))
    os.close(fd)
    try:
        with zipfile.ZipFile(docx_path, "r") as source, \
                zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as output:
            for info in source.infolist():
                output.writestr(info, replacements.get(info.filename,
                                                       source.read(info.filename)))
        os.replace(tmp, docx_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"applied": applied}


def _story_parts(names: Set[str], roots: Dict[str, Any]) -> Tuple[List[Tuple[str, str]], List[str]]:
    errors: List[str] = []
    root_rels = roots.get("_rels/.rels")
    if root_rels is None:
        return [], ["缺少包根关系部件"]
    document_part = ""
    for rel in root_rels:
        if rel.tag == REL + "Relationship" and rel.get("Type", "").endswith("/officeDocument"):
            try:
                document_part = _resolve_part("", rel.get("Target", ""))
            except ValueError:
                errors.append("主文档关系目标无效")
            break
    if not document_part or document_part not in names:
        return [], errors + ["无法定位DOCX主文档部件"]

    parts = [(document_part, "paragraph")]
    rels = roots.get(_relationship_part(document_part))
    if rels is None:
        return parts, errors + ["主文档关系部件无法读取"]
    seen = {document_part}
    for rel in rels:
        if rel.tag != REL + "Relationship":
            continue
        rel_type = rel.get("Type", "")
        if not (rel_type.endswith("/header") or rel_type.endswith("/footer")):
            continue
        if rel.get("TargetMode", "").lower() == "external":
            continue
        try:
            part = _resolve_part(document_part, rel.get("Target", ""))
        except ValueError:
            errors.append("页眉或页脚关系目标无效")
            continue
        if part not in names:
            errors.append("页眉或页脚关系目标缺失：" + part)
            continue
        if part not in roots:
            continue
        if part not in seen:
            parts.append((part, "header" if rel_type.endswith("/header") else "footer"))
            seen.add(part)
    return parts, errors


def _nearest_paragraph(element):
    current = element.getparent()
    while current is not None and current.tag != W + "p":
        current = current.getparent()
    return current


def _inside_properties(element, paragraph) -> bool:
    current = element.getparent()
    while current is not None and current is not paragraph:
        if isinstance(current.tag, str) and current.tag.startswith(W):
            if current.tag[len(W):] in PROPERTY_CONTAINERS:
                return True
        current = current.getparent()
    return False


def _revision_ancestor(element, paragraph=None):
    current = element.getparent()
    while current is not None and (paragraph is None or current is not paragraph):
        if isinstance(current.tag, str) and current.tag in {
                W + "ins", W + "del", W + "moveFrom", W + "moveTo"}:
            return current
        current = current.getparent()
    return None


def _fingerprint(element) -> str:
    rows: List[str] = []
    for node in element.iter():
        if not isinstance(node.tag, str):
            continue
        attrs = [(key, value) for key, value in node.attrib.items() if key != W + "author"]
        attrs.sort()
        rows.append(node.tag + "\x1f" + repr(attrs))
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _object_canonical_bytes(element) -> bytes:
    """Normalize links and visible textbox text for stable structure checks."""
    rows = []

    def add_text(value):
        if not value:
            return
        if rows and rows[-1][0] == "text":
            rows[-1][1] += value
        else:
            rows.append(["text", value])

    def walk(node):
        if not isinstance(node.tag, str):
            return
        attrs = sorted((key, value) for key, value in node.attrib.items()
                       if not key.startswith(OFFICE_REL) and
                       not (node.tag == VML + "textpath" and key == "string"))
        rows.append(["element", node.tag, attrs])
        if node.tag != W + "t":
            add_text(node.text)
        for child in node:
            relationship_attrs = [key for key in child.attrib
                                  if key.startswith(OFFICE_REL)]
            if child.tag == W + "hyperlink" and relationship_attrs:
                add_text(child.text)
                for nested in child:
                    walk(nested)
                    add_text(nested.tail)
                add_text(child.tail)
            elif relationship_attrs:
                add_text(child.tail)
            else:
                walk(child)
                add_text(child.tail)

    walk(element)
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _object_digest(element) -> str:
    return hashlib.sha256(_object_canonical_bytes(element)).hexdigest()


def _inside_alternate_content(element) -> bool:
    return any(ancestor.tag in {MC + "AlternateContent", MC + "Choice", MC + "Fallback"}
               for ancestor in element.iterancestors())


def _paragraph_id(paragraph) -> str:
    return paragraph.get(W14 + "paraId", "")


def _valid_property_revision(element, previous_properties_tag: str) -> bool:
    children = [child for child in element if isinstance(child.tag, str)]
    if len(children) != 1 or children[0].tag != W + previous_properties_tag:
        return False
    for node in element.iter():
        if not isinstance(node.tag, str) or not node.tag.startswith(W):
            return False
        if node is element:
            continue
        local = node.tag[len(W):]
        if (local in REVISION_UNSUPPORTED_CONTENT or local in TEXT_REVISION_TAGS or
                local in {"p", "moveFromRangeStart", "moveFromRangeEnd",
                          "moveToRangeStart", "moveToRangeEnd", "customXmlInsRangeStart",
                          "customXmlInsRangeEnd", "customXmlDelRangeStart",
                          "customXmlDelRangeEnd", "customXmlMoveFromRangeStart",
                          "customXmlMoveFromRangeEnd", "customXmlMoveToRangeStart",
                          "customXmlMoveToRangeEnd"} or local.endswith("Change")):
            return False
    return True


def _vml_shapetype_is_safe(shapetype, shape):
    if (shapetype is None or shapetype.tag != VML + "shapetype" or
            shapetype.getparent() != shape.getparent() or
            shape.get("type") != "#" + shapetype.get("id", "")):
        return False
    allowed_attrs = {"id", "coordsize", "coordorigin", "adj", "path", "spt",
                     OFFICE + "spt", "filled", "stroked", "style", "fillcolor",
                     "strokecolor"}
    if set(shapetype.attrib) - allowed_attrs:
        return False
    allowed_children = {
        VML + "formulas", VML + "path", VML + "textpath", VML + "handles",
        OFFICE + "lock",
    }
    if any(child.tag not in allowed_children for child in shapetype):
        return False
    if any((node.text or "").strip() for node in shapetype.iter()):
        return False
    for node in shapetype.iter():
        if node is shapetype:
            continue
        if node.tag == VML + "formulas":
            if (set(node.attrib) or any(child.tag != VML + "f" or len(child) or
                                        set(child.attrib) != {"eqn"}
                                        for child in node)):
                return False
        elif node.tag == VML + "path":
            if (len(node) or set(node.attrib) -
                    {"connectangles", "connectlocs", "connecttype", "textpathok",
                     OFFICE + "connectangles", OFFICE + "connectlocs",
                     OFFICE + "connecttype"}):
                return False
        elif node.tag == VML + "textpath":
            if (len(node) or "string" in node.attrib or
                    set(node.attrib) - {"on", "fitshape"}):
                return False
        elif node.tag == VML + "handles":
            if any(child.tag != VML + "h" or len(child) or
                   set(child.attrib) - {"position", "xrange"} for child in node):
                return False
        elif node.tag == OFFICE + "lock":
            if (len(node) or set(node.attrib) -
                    {"ext", VML + "ext", "shapetype", "text"}):
                return False
        elif node.tag == VML + "f":
            continue
        elif node.tag == VML + "h":
            continue
        else:
            return False
    return True


def _vml_textpath_shape(textpath):
    if textpath is None or textpath.tag != VML + "textpath":
        return None
    shape = next((ancestor for ancestor in textpath.iterancestors()
                  if ancestor.tag == VML + "shape"), None)
    pict = next((ancestor for ancestor in textpath.iterancestors()
                 if ancestor.tag == W + "pict"), None)
    paragraph = _nearest_paragraph(textpath)
    if (shape is None or pict is None or paragraph is None or
            _inside_alternate_content(textpath)):
        return None
    shapetypes = list(pict.findall(VML + "shapetype"))
    shapes = list(pict.findall(VML + "shape"))
    if (not textpath.get("string") or len(textpath.get("string", "")) > 4000 or
            textpath.getparent() != shape or shape.getparent() != pict or
            len(shapes) != 1 or shapes[0] != shape or len(shapetypes) > 1 or
            any(child not in (shape, *shapetypes) for child in pict)):
        return None
    textpaths_with_text = [node for node in pict.iter(VML + "textpath")
                           if node.get("string")]
    if (len(textpaths_with_text) != 1 or textpaths_with_text[0] != textpath or
            len(list(shape.iter(VML + "textpath"))) != 1 or
            len(list(pict.iter(VML + "shape"))) != 1 or
            len(list(pict.iter(W + "pict"))) != 1):
        return None
    if shapetypes and not _vml_shapetype_is_safe(shapetypes[0], shape):
        return None
    shape_attrs = {"id", "type", "style", "fillcolor", "strokecolor", "filled",
                   "stroked", "coordsize", "coordorigin", "adj", "spid", "allowincell",
                   OFFICE + "spid", OFFICE + "allowincell"}
    textpath_attrs = {"string", "on", "fitshape", "fitpath", "trim", "xscale", "style"}
    if (set(shape.attrib) - shape_attrs or
            set(textpath.attrib) - textpath_attrs):
        return None
    allowed_shape_children = {
        VML + "textpath", VML + "fill", VML + "wrap", OFFICE_WORD + "wrap",
    }
    if any(child.tag not in allowed_shape_children for child in shape):
        return None
    if (len(shape.findall(VML + "fill")) > 1 or
            len(shape.findall(VML + "wrap")) > 1 or
            len(textpath)):
        return None
    safe_child_attrs = {
        VML + "fill": {"opacity"},
        VML + "wrap": {"anchorx", "anchory"},
        OFFICE_WORD + "wrap": {"anchorx", "anchory"},
    }
    for child in shape:
        if (child.tag in safe_child_attrs and
                (set(child.attrib) - safe_child_attrs[child.tag] or
                 len(child) or (child.text or "").strip())):
            return None
    if any(not isinstance(node.tag, str) for node in pict.iter()):
        return None
    return shape


def _has_reviewable_story_content(root) -> bool:
    text_tags = {W + "t", W + "instrText", W + "delText", W + "delInstrText"}
    visual_tags = {W + "drawing", W + "pict", W + "object"}
    for node in root.iter():
        if node.tag in text_tags and (node.text or "").strip():
            return True
        if node.tag == VML + "textpath" and (node.get("string") or "").strip():
            return True
        if node.tag == W + "fldSimple" and (node.get(W + "instr") or "").strip():
            return True
        if node.tag in visual_tags:
            return True
    return False


def _vml_textpath_structure_digest(shape) -> str:
    clone = ET.fromstring(ET.tostring(shape))
    for node in clone.iter(VML + "textpath"):
        node.set("string", "")
    return hashlib.sha256(ET.tostring(
        clone, method="c14n", exclusive=True, with_comments=False)).hexdigest()


def _xpath_namespaces(root) -> Dict[str, str]:
    namespaces: Dict[str, str] = {}
    for node in root.iter():
        for prefix, uri in (node.nsmap or {}).items():
            if prefix:
                namespaces[prefix] = uri
    return namespaces


def _xpath_target(root, node_path: str):
    return root.xpath(node_path, namespaces=_xpath_namespaces(root))


def read_vml_text_targets(path: str) -> List[Dict[str, Any]]:
    """Read only simple VML text paths with a unique paragraph-local target."""
    rows: List[Dict[str, Any]] = []
    with zipfile.ZipFile(path, "r") as archive:
        names = set(archive.namelist())
        if len(names) > MAX_MEMBERS or sum(info.file_size for info in archive.infolist()) > MAX_UNPACKED_BYTES:
            raise ValueError("DOCX包超过VML文字路径检查上限")
        roots = {}
        for name in names:
            if name.lower().endswith((".xml", ".rels")):
                try:
                    roots[name] = _parse_xml(archive.read(name))
                except Exception:
                    continue
        stories, errors = _story_parts(names, roots)
        if errors:
            raise ValueError("DOCX故事部件无法安全检查VML文字路径")
        ordinal = 0
        for part, part_kind in stories:
            root = roots.get(part)
            if root is None:
                continue
            for textpath in root.iter(VML + "textpath"):
                shape = _vml_textpath_shape(textpath)
                if shape is None:
                    continue
                ordinal += 1
                value = textpath.get("string", "")
                paragraph = _nearest_paragraph(textpath)
                paragraph_index = (list(root.iter(W + "p")).index(paragraph)
                                   if paragraph is not None else -1)
                rows.append({
                    "part": part,
                    "part_kind": part_kind,
                    "paragraph_index": paragraph_index,
                    "ordinal": ordinal,
                    "node_path": root.getroottree().getpath(textpath),
                    "attribute": "string",
                    "value": value,
                    "target_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    "structure_sha256": _vml_textpath_structure_digest(shape),
                })
    return rows


def rewrite_vml_text_targets(docx_path: str, operations: List[Any]) -> Dict[str, int]:
    """Rewrite selected VML textpath spans while preserving shape structure."""
    if not operations:
        return {"applied": 0}
    grouped: Dict[Tuple[str, str], List[Any]] = {}
    for op in operations:
        target = op.target or {}
        if (target.get("kind") != "vml_textpath" or
                target.get("attribute") != "string"):
            raise ValueError("VML文字路径目标类型无效")
        grouped.setdefault((str(target.get("part", "")),
                            str(target.get("node_path", ""))), []).append(op)

    replacements: Dict[str, bytes] = {}
    applied = 0
    with zipfile.ZipFile(docx_path, "r") as archive:
        names = set(archive.namelist())
        roots: Dict[str, Any] = {}
        for part in {key[0] for key in grouped}:
            if part not in names or not part.lower().endswith((".xml", ".vml")):
                raise ValueError("VML文字路径部件无效")
            roots[part] = _parse_xml(archive.read(part))
        for (part, node_path), ops in grouped.items():
            root = roots[part]
            matches = _xpath_target(root, node_path)
            if len(matches) != 1 or _vml_textpath_shape(matches[0]) is None:
                raise ValueError("VML文字路径已变化或无法唯一定位")
            node = matches[0]
            current = node.get("string", "")
            structure_hash = _vml_textpath_structure_digest(
                next(ancestor for ancestor in node.iterancestors()
                     if ancestor.tag == VML + "shape"))
            source_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if ({str((op.target or {}).get("target_sha256") or "") for op in ops} !=
                    {source_hash} or
                    {str((op.target or {}).get("structure_sha256") or "")
                     for op in ops} != {structure_hash}):
                raise ValueError("VML文字路径内容或结构已变化，请重新检查")
            ordered = sorted(ops, key=lambda op: (op.local_start, op.local_end))
            cursor = 0
            output_cursor = 0
            pieces: List[str] = []
            for order, op in enumerate(ordered):
                start, end = int(op.local_start), int(op.local_end)
                if (start < cursor or start < 0 or end <= start or end > len(current) or
                        current[start:end] != op.value):
                    raise ValueError("VML文字路径区间无效、重叠或与扫描文本不一致")
                prefix = current[cursor:start]
                pieces.append(prefix)
                output_cursor += len(prefix)
                written = str(op.replacement)
                op.target.update(
                    attribute="string", source_target_sha256=source_hash,
                    output_order=order, output_span=[output_cursor,
                                                      output_cursor + len(written)],
                    written=written)
                pieces.append(written)
                output_cursor += len(written)
                cursor = end
                applied += 1
            pieces.append(current[cursor:])
            rewritten = "".join(pieces)
            node.set("string", rewritten)
            output_hash = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
            for op in ordered:
                op.target["output_target_sha256"] = output_hash
            if _vml_textpath_structure_digest(
                    next(ancestor for ancestor in node.iterancestors()
                         if ancestor.tag == VML + "shape")) != structure_hash:
                raise ValueError("VML文字路径替换改变了形状结构")
            replacements[part] = _serialize(root)

    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(os.path.abspath(docx_path)))
    os.close(fd)
    try:
        with zipfile.ZipFile(docx_path, "r") as source, \
                zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as output:
            for info in source.infolist():
                output.writestr(info, replacements.get(info.filename,
                                                       source.read(info.filename)))
        os.replace(tmp, docx_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"applied": applied}


def restore_vml_text_targets(docx_path: str,
                             records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Restore VML text only when target identity, structure and hashes match."""
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in records or []:
        if (not isinstance(row, dict) or row.get("attribute") != "string" or
                row.get("kind") != "vml_textpath"):
            raise RuntimeError("mapping VML文字路径记录无效")
        grouped.setdefault((str(row.get("part", "")),
                            str(row.get("node_path", ""))), []).append(row)
    if not grouped:
        return {"applied": 0}

    replacements: Dict[str, bytes] = {}
    applied = 0
    with zipfile.ZipFile(docx_path, "r") as archive:
        names = set(archive.namelist())
        roots: Dict[str, Any] = {}
        for part in {key[0] for key in grouped}:
            if part not in names or not part.lower().endswith((".xml", ".vml")):
                raise RuntimeError("mapping VML文字路径部件无效")
            roots[part] = _parse_xml(archive.read(part))
        for (part, node_path), rows in grouped.items():
            root = roots[part]
            matches = _xpath_target(root, node_path)
            if len(matches) != 1 or _vml_textpath_shape(matches[0]) is None:
                raise RuntimeError("mapping VML文字路径缺失或不唯一")
            node = matches[0]
            current = node.get("string", "")
            shape = next(ancestor for ancestor in node.iterancestors()
                         if ancestor.tag == VML + "shape")
            structure_hash = _vml_textpath_structure_digest(shape)
            output_hashes = {str(row.get("output_target_sha256") or "") for row in rows}
            source_hashes = {str(row.get("source_target_sha256") or "") for row in rows}
            structure_hashes = {str(row.get("structure_sha256") or "") for row in rows}
            if (len(output_hashes) != 1 or not next(iter(output_hashes)) or
                    hashlib.sha256(current.encode("utf-8")).hexdigest() != next(iter(output_hashes)) or
                    len(source_hashes) != 1 or not next(iter(source_hashes)) or
                    structure_hashes != {structure_hash}):
                raise RuntimeError("VML文字路径与mapping摘要不一致")
            restored = current
            for row in sorted(rows, key=lambda item: int(item.get("order", 0)), reverse=True):
                span = row.get("output_span") or []
                if len(span) != 2:
                    raise RuntimeError("mapping VML文字路径还原区间无效")
                start, end = int(span[0]), int(span[1])
                written = str(row.get("written") or "")
                original = str(row.get("original") or "")
                if (start < 0 or end < start or end > len(restored) or
                        restored[start:end] != written):
                    raise RuntimeError("VML占位符缺失或已变化，严格还原已中止")
                restored = restored[:start] + original + restored[end:]
                applied += 1
            if hashlib.sha256(restored.encode("utf-8")).hexdigest() != next(iter(source_hashes)):
                raise RuntimeError("VML文字路径还原结果与源摘要不一致")
            node.set("string", restored)
            replacements[part] = _serialize(root)

    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(os.path.abspath(docx_path)))
    os.close(fd)
    try:
        with zipfile.ZipFile(docx_path, "r") as source, \
                zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as output:
            for info in source.infolist():
                output.writestr(info, replacements.get(info.filename,
                                                       source.read(info.filename)))
        os.replace(tmp, docx_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"applied": applied}


def _paragraph_text_view(paragraph, part: str, paragraph_index: int):
    return _paragraph_revision_view(paragraph, part, paragraph_index, "accepted")


def _paragraph_revision_view(paragraph, part: str, paragraph_index: int,
                             view: str):
    if view not in {"accepted", "rejected"}:
        raise ValueError("DOCX修订视图类型无效")
    chars: List[str] = []
    units: List[Dict[str, Any]] = []
    for node in iter_story_content_nodes(
            paragraph, include_removed=(view == "rejected")):
        owner = _revision_ancestor(node, paragraph)
        deleted = owner is not None and owner.tag in {W + "del", W + "moveFrom"}
        inserted = owner is not None and owner.tag in {W + "ins", W + "moveTo"}
        if ((view == "accepted" and deleted) or
                (view == "rejected" and inserted)):
            continue
        text_tag = W + "delText" if deleted else W + "t"
        node_path = node.getroottree().getpath(node)
        if node.tag == text_tag:
            value = node.text or ""
            if owner is not None:
                ordinal = owner.getroottree().getpath(owner)
                owner_key = (owner.tag[len(W):], ordinal)
            else:
                owner_key = ("plain", part, paragraph_index)
            for offset, char in enumerate(value):
                chars.append(char)
                units.append({"node": node, "node_offset": offset,
                              "writable": True, "owner_key": owner_key,
                              "node_kind": "delText" if deleted else "t",
                              "node_path": node_path})
        elif node.tag == W + "tab":
            chars.append(" ")
            units.append({"node": node, "node_offset": None,
                          "writable": False, "owner_key": ("control", part, paragraph_index),
                          "node_kind": "tab", "node_path": node_path})
        elif node.tag in {W + "br", W + "cr"}:
            chars.append("\n")
            units.append({"node": node, "node_offset": None,
                          "writable": False, "owner_key": ("control", part, paragraph_index),
                          "node_kind": "control", "node_path": node_path})
    return "".join(chars), units


def _revision_text_view(container, kind: str):
    text_tag = W + ("t" if kind in {"ins", "moveTo"} else "delText")
    chars: List[str] = []
    units: List[Dict[str, Any]] = []
    for node in container.iter():
        if node is container or not isinstance(node.tag, str):
            continue
        if node.tag == W + "p":
            continue
        if node.tag == text_tag:
            value = node.text or ""
            for offset, char in enumerate(value):
                chars.append(char)
                units.append({"node": node, "node_offset": offset,
                              "writable": True, "owner_key": (kind, container),
                              "node_kind": "t" if kind in {"ins", "moveTo"} else "delText",
                              "node_path": node.getroottree().getpath(node)})
        elif node.tag == W + "tab":
            chars.append(" ")
            units.append({"node": node, "node_offset": None,
                          "writable": False, "owner_key": (kind, container),
                          "node_kind": "tab",
                          "node_path": node.getroottree().getpath(node)})
        elif node.tag in {W + "br", W + "cr"}:
            chars.append("\n")
            units.append({"node": node, "node_offset": None,
                          "writable": False, "owner_key": (kind, container),
                          "node_kind": "control",
                          "node_path": node.getroottree().getpath(node)})
    return "".join(chars), units


def analyze_docx_revisions(path: str) -> Dict[str, Any]:
    """Return a display inventory and supported text-revision views for one DOCX."""
    inventory = inspect_docx_revisions(path)
    result: Dict[str, Any] = {
        "inventory": inventory,
        "complete": bool(inventory.get("complete")),
        "has_revisions": bool(inventory.get("has_revisions")),
        "supported": False,
        "can_preserve_and_redact": False,
        "supported_text_revision_count": 0,
        "supported_property_revision_count": 0,
        "can_export_after_ack": False,
        "unsupported_manifest": [],
        "unsupported_digest": "",
        "unsupported_revision_count": 0,
        "unscanned_field_instruction_count": 0,
        "field_instruction_count": 0,
        "safe_field_instruction_count": 0,
        "unsafe_field_instruction_count": 0,
        "unsafe_field_manifest": [],
        "unsafe_field_digest": "",
        "embedded_image_count": 0,
        "text_object_count": 0,
        "unsupported_object_manifest": [],
        "unsupported_object_roots": [],
        "object_digest": "",
        "supported_property_manifest": [],
        "unsupported": [],
        "paragraphs": [],
        "deleted_blocks": [],
        "author_blocks": [],
        "property_author_blocks": [],
        "story_parts": [],
    }
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            roots: Dict[str, Any] = {}
            errors: List[str] = [
                error for error in (inventory.get("errors") or [])
                if not error.startswith("无法解析XML部件：")]
            for name in names:
                if not name.lower().endswith((".xml", ".vml", ".rels")):
                    continue
                try:
                    roots[name] = _parse_xml(archive.read(name))
                except Exception:
                    errors.append("无法解析XML部件：" + name)
            stories, story_errors = _story_parts(names, roots)
            errors.extend(story_errors)
            result["story_parts"] = [name for name, _kind in stories]
            header_footer_content_parts = [
                {"part": part, "kind": kind}
                for part, kind in stories
                if kind in {"header", "footer"} and
                _has_reviewable_story_content(roots[part])
            ]
            header_footer_digest = hashlib.sha256(json.dumps(
                header_footer_content_parts, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            field_records = [
                record
                for part, _kind in stories
                for record in _field_instruction_inventory(part, roots[part])
            ]
            unsafe_fields = [record for record in field_records if not record["safe"]]
            unsafe_field_digest = hashlib.sha256(json.dumps(
                unsafe_fields, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            embedded_image_count = 0
            for part, _kind in stories:
                for node in roots[part].iter():
                    if (node.tag not in {W + "drawing", W + "pict"} or
                            any(child.tag == W + "txbxContent"
                                for child in node.iter() if child is not node)):
                        continue
                    paths = list(node.iter(VML + "textpath")) if node.tag == W + "pict" else []
                    if len(paths) == 1 and _vml_textpath_shape(paths[0]) is not None:
                        continue
                    embedded_image_count += 1
            object_roots = []
            object_seen = set()
            for part, _kind in stories:
                for candidate in roots[part].iter():
                    has_textbox = any(child.tag == W + "txbxContent"
                                      for child in candidate.iter()
                                      if child is not candidate)
                    has_vml_textpath = any(child.tag == VML + "textpath"
                                           for child in candidate.iter()
                                           if child is not candidate)
                    if (candidate.tag not in {W + "drawing", W + "pict"} or
                            not (has_textbox or has_vml_textpath)):
                        continue
                    alternate = next((ancestor for ancestor in candidate.iterancestors()
                                      if ancestor.tag == MC + "AlternateContent"), None)
                    logical_root = alternate if alternate is not None else candidate
                    if logical_root not in object_seen:
                        object_seen.add(logical_root)
                        object_roots.append((part, logical_root))
            result.update(
                field_instruction_count=len(field_records),
                safe_field_instruction_count=len(field_records) - len(unsafe_fields),
                unsafe_field_instruction_count=len(unsafe_fields),
                unsafe_field_manifest=unsafe_fields,
                unsafe_field_digest=unsafe_field_digest,
                unscanned_field_instruction_count=len(unsafe_fields),
                embedded_image_count=embedded_image_count,
                text_object_count=len(object_roots),
                header_footer_content_parts=header_footer_content_parts,
                header_footer_digest=header_footer_digest,
            )
            result["inventory"].update(
                field_instruction_count=len(field_records),
                safe_field_instruction_count=len(field_records) - len(unsafe_fields),
                unsafe_field_instruction_count=len(unsafe_fields),
                unsafe_field_manifest=unsafe_fields,
                unsafe_field_digest=unsafe_field_digest,
                embedded_image_count=embedded_image_count,
                text_object_count=len(object_roots),
                header_footer_content_parts=header_footer_content_parts,
                header_footer_digest=header_footer_digest,
            )
            story_kind = dict(stories)
            supported_containers: List[Dict[str, Any]] = []
            property_revision_containers: List[Dict[str, Any]] = []
            unsupported: List[Dict[str, Any]] = []
            def mark_unsupported(part_name, element, reason):
                unsupported.append({"part": part_name,
                                    "kind": element.tag[len(W):],
                                    "reason": reason,
                                    "_element": element})

            unsupported_object_nodes = set()
            unsupported_object_records = []
            all_object_records = []
            for part, logical_root in object_roots:
                is_alternate = logical_root.tag == MC + "AlternateContent"
                if is_alternate:
                    ordered_object_nodes = [node for branch in logical_root
                                            if branch.tag in {MC + "Choice", MC + "Fallback"}
                                            for node in branch.iter()]
                else:
                    ordered_object_nodes = list(logical_root.iter())
                object_nodes = set(ordered_object_nodes)
                if is_alternate:
                    unsupported_object_nodes.update(object_nodes)
                object_anchors = [node for node in ordered_object_nodes
                                  if node.tag in {W + "drawing", W + "pict"} and
                                  any(child.tag in {W + "txbxContent", VML + "textpath"}
                                      for child in node.iter() if child is not node)]
                if is_alternate and object_anchors:
                    mark_unsupported(
                        part, object_anchors[0],
                        "兼容文本框：Word为适配不同版本保留的备用显示副本（例如VML与DrawingML各一份）；"
                        "工具无法确认所有副本都已同步脱敏，须人工核对")
                vml_paths = [node for node in logical_root.iter(VML + "textpath")
                             if node.get("string")]
                object_record = {
                    "part": part,
                    "kind": "vml_textpath" if vml_paths else "text_object",
                    "structure_sha256": _object_digest(logical_root),
                }
                all_object_records.append(object_record)
                if is_alternate:
                    unsupported_object_records.append(object_record)
                elif vml_paths and any(
                        _vml_textpath_shape(textpath) is None
                        for textpath in vml_paths):
                    unsupported_object_records.append({
                        **object_record,
                        "reason": "VML文字路径结构复杂或无法唯一定位",
                    })

            paragraph_indices: Dict[str, Dict[Any, int]] = {
                part: {paragraph: index for index, paragraph in
                       enumerate(roots[part].iter(W + "p"))}
                for part, _kind in stories
            }
            ordinals_by_part = {part: _revision_node_ordinals(root)
                                for part, root in roots.items()}
            container_ordinals: Dict[Tuple[str, int, str], int] = {}

            for part, root in roots.items():
                range_markers: Dict[Tuple[str, str], Dict[str, List[Any]]] = {}
                for marker in root.iter():
                    if not isinstance(marker.tag, str) or not marker.tag.startswith(W):
                        continue
                    marker_name = marker.tag[len(W):]
                    match = re.fullmatch(r"(customXml(?:Ins|Del|MoveFrom|MoveTo))Range(Start|End)", marker_name)
                    marker_id = marker.get(W + "id")
                    if match and marker_id:
                        range_markers.setdefault((match.group(1), marker_id), {"Start": [], "End": []})[match.group(2)].append(marker)
                validated_range_markers = {
                    node for pair in range_markers.values()
                    if len(pair["Start"]) == 1 and len(pair["End"]) == 1
                    for node in pair["Start"] + pair["End"]
                }
                for element in root.iter():
                    if element in unsupported_object_nodes:
                        continue
                    if not isinstance(element.tag, str) or not element.tag.startswith(W):
                        continue
                    local = element.tag[len(W):]
                    if element in validated_range_markers:
                        continue
                    if local == "delText":
                        parent_revision = _revision_ancestor(element)
                        parent_kind = (parent_revision.tag[len(W):]
                                       if parent_revision is not None else "")
                        if (parent_kind == "del" and part in story_kind and
                                _nearest_paragraph(parent_revision) is not None and
                                not _inside_properties(parent_revision,
                                                       _nearest_paragraph(parent_revision))):
                            continue
                        mark_unsupported(part, element,
                                         "删除文字节点不在受支持的w:del文本修订中")
                        continue

                    if local in {"ins", "del"}:
                        paragraph = _nearest_paragraph(element)
                        pi = (paragraph_indices.get(part, {}).get(paragraph)
                              if paragraph is not None else None)
                        ordinal = None
                        if pi is not None:
                            key = (part, pi, local)
                            ordinal = container_ordinals.get(key, 0)
                            container_ordinals[key] = ordinal + 1
                        if paragraph is not None and _inside_properties(element, paragraph):
                            if (part in story_kind and pi is not None and
                                    _is_paragraph_mark_revision(element, paragraph)):
                                global_ordinal = ordinals_by_part[part][element]
                                property_revision_containers.append({
                                    "part": part,
                                    "part_kind": story_kind[part],
                                    "paragraph_index": pi,
                                    "container": element,
                                    "kind": local,
                                    "revision_id": element.get(W + "id", ""),
                                    "revision_ordinal": global_ordinal,
                                    "author": element.get(W + "author", ""),
                                    "target": {
                                        "part": part,
                                        "paragraph_index": -1,
                                        "revision_scope": "part",
                                        "revision_kind": local,
                                        "revision_id": element.get(W + "id", ""),
                                        "revision_ordinal": global_ordinal,
                                        "revision_fingerprint": _fingerprint(element),
                                    },
                                })
                            else:
                                mark_unsupported(
                                    part, element,
                                    "属性区域中的文字修订结构不符合受支持的段落标记格式")
                            continue
                        if (part not in story_kind or paragraph is None or pi is None or
                                _revision_ancestor(element, paragraph) is not None):
                            mark_unsupported(part, element,
                                             "文字修订不在受支持的正文、页眉或页脚段落中")
                            continue
                        bad = None
                        expected_text = W + ("t" if local == "ins" else "delText")
                        for child in element.iter():
                            if child is element or not isinstance(child.tag, str):
                                continue
                            child_local = child.tag.split("}")[-1]
                            if child_local in REVISION_UNSUPPORTED_CONTENT:
                                bad = "包含不支持的字段、隐藏内容或对象"
                                break
                            if child.tag in {W + "p", W + "ins", W + "del", W + "moveFrom", W + "moveTo"}:
                                bad = "包含嵌套段落或嵌套修订"
                                break
                            if child.tag in {W + "t", W + "delText"} and child.tag != expected_text:
                                bad = "修订文字节点类型与修订容器不一致"
                                break
                        if bad:
                            mark_unsupported(part, element, bad)
                            continue
                        rid = element.get(W + "id", "")
                        text, units = _revision_text_view(element, local)
                        base_target = {
                            "part": part,
                            "paragraph_index": pi,
                            "paragraph_id": _paragraph_id(paragraph),
                            "paragraph_fingerprint": _fingerprint(paragraph),
                            "revision_kind": local,
                            "revision_id": rid,
                            "revision_ordinal": ordinal,
                            "revision_fingerprint": _fingerprint(element),
                        }
                        supported_containers.append({
                            "part": part, "part_kind": story_kind[part],
                            "paragraph_index": pi, "paragraph": paragraph,
                            "container": element, "kind": local,
                            "revision_id": rid, "revision_ordinal": ordinal,
                            "text": text, "units": units,
                            "author": element.get(W + "author", ""),
                            "target": base_target,
                        })
                        continue

                    if local in PROPERTY_REVISION_TAGS:
                        key = (part, -1, local)
                        ordinal = container_ordinals.get(key, 0)
                        container_ordinals[key] = ordinal + 1
                        if not _valid_property_revision(element, PROPERTY_REVISION_TAGS[local]):
                            mark_unsupported(
                                part, element, "属性修订的旧属性结构不符合受支持格式")
                            continue
                        property_revision_containers.append({
                            "part": part,
                            "container": element,
                            "kind": local,
                            "revision_id": element.get(W + "id", ""),
                            "revision_ordinal": ordinal,
                            "author": element.get(W + "author", ""),
                            "target": {
                                "part": part,
                                "paragraph_index": -1,
                                "revision_scope": "part",
                                "revision_kind": local,
                                "revision_id": element.get(W + "id", ""),
                                "revision_ordinal": ordinal,
                                "revision_fingerprint": _fingerprint(element),
                            },
                        })
                        continue

                    label = REVISION_TAGS.get(local)
                    if label is None and local.endswith("Change"):
                        label = "其他属性修订（未支持）"
                    if label and local not in {"delText"}:
                        mark_unsupported(part, element,
                                         "该修订结构不在当前支持范围内")

            # A deleted-text node is supported only if it belongs to a validated container.
            supported_del_nodes = {
                node
                for row in supported_containers
                if row["kind"] == "del"
                for node in row["container"].iter(W + "delText")
            }
            for part, root in roots.items():
                for node in root.iter(W + "delText"):
                    if node in unsupported_object_nodes:
                        continue
                    if node not in supported_del_nodes and not any(
                            item.get("part") == part and item.get("kind") == "delText"
                            for item in unsupported):
                        mark_unsupported(part, node, "删除文字未纳入支持范围")

            paragraphs: List[Dict[str, Any]] = []
            for part, part_kind in stories:
                root = roots.get(part)
                if root is None:
                    continue
                for pi, paragraph in enumerate(root.iter(W + "p")):
                    if _inside_alternate_content(paragraph):
                        continue
                    text, units = _paragraph_text_view(paragraph, part, pi)
                    paragraph_revisions = [row for row in supported_containers
                                           if row["part"] == part and row["paragraph_index"] == pi]
                    has_revision_markup = any(
                        isinstance(node.tag, str) and node.tag.startswith(W) and
                        (node.tag[len(W):] in REVISION_TAGS or
                         node.tag[len(W):].endswith("Change"))
                        for node in paragraph.iter())
                    has_unsupported_revision_markup = any(
                        item.get("part") == part and
                        (item.get("kind") in REVISION_TAGS or
                         str(item.get("kind") or "").endswith("Change")) and
                        _nearest_paragraph(item.get("_element")) is paragraph
                        for item in unsupported)
                    rejected_text = ""
                    rejected_units: List[Dict[str, Any]] = []
                    rejected_view_supported = bool(
                        paragraph_revisions and not has_unsupported_revision_markup)
                    if rejected_view_supported:
                        rejected_text, rejected_units = _paragraph_revision_view(
                            paragraph, part, pi, "rejected")
                    if not text and not paragraph_revisions and not has_revision_markup:
                        continue
                    location = ("正文" if part_kind == "paragraph" else
                                ("页眉" if part_kind == "header" else "页脚")) + "第" + str(pi + 1) + "段"
                    target = {
                        "kind": "visible_text",
                        "part": part,
                        "paragraph_index": pi,
                        "paragraph_id": _paragraph_id(paragraph),
                        "structure_fingerprint": _fingerprint(paragraph),
                    }
                    if (has_unsupported_revision_markup or
                            (inventory.get("passthrough_safe") and
                             has_revision_markup)):
                        target["rewrite_supported"] = False
                        target["rewrite_block_reason"] = (
                            "该段含未支持或未完整盘点的修订，工具不会自动改写此段；"
                            "请人工审核原样保留的内容。")
                    paragraphs.append({
                        "part": part, "part_kind": part_kind,
                        "paragraph_index": pi, "paragraph": paragraph,
                        "text": text, "units": units,
                        "location": location, "target": target,
                        "has_revision": bool(paragraph_revisions or has_revision_markup),
                        "rejected_text": rejected_text,
                        "rejected_units": rejected_units,
                        "rejected_view_supported": rejected_view_supported,
                    })

            deleted_blocks: List[Dict[str, Any]] = []
            author_blocks: List[Dict[str, Any]] = []
            for row in supported_containers:
                revision_target = dict(row["target"])
                if row["kind"] == "del" and row["text"]:
                    deleted_blocks.append({
                        "part": row["part"],
                        "paragraph_index": row["paragraph_index"],
                        "location": ("正文" if row["part_kind"] == "paragraph" else
                                     ("页眉" if row["part_kind"] == "header" else "页脚")) +
                                    "第" + str(row["paragraph_index"] + 1) + "段·删除修订#" +
                                    (row["revision_id"] or str(row["revision_ordinal"] + 1)),
                        "text": row["text"], "units": row["units"],
                        "target": {"kind": "revision_deleted_text", **revision_target,
                                   "segment_index": 0,
                                   "text_sha256": hashlib.sha256(
                                       row["text"].encode("utf-8")).hexdigest()},
                    })
                author = row["author"]
                if author.strip():
                    author_blocks.append({
                        "part": row["part"],
                        "paragraph_index": row["paragraph_index"],
                        "location": ("正文" if row["part_kind"] == "paragraph" else
                                     ("页眉" if row["part_kind"] == "header" else "页脚")) +
                                    "第" + str(row["paragraph_index"] + 1) + "段·修订人",
                        "text": author,
                        "target": {"kind": "revision_author", **revision_target,
                                   "attribute": W + "author"},
                    })

            property_author_blocks: List[Dict[str, Any]] = []
            for row in property_revision_containers:
                if row["author"].strip():
                    display_paragraph_index = int(row.get("paragraph_index", -1))
                    if display_paragraph_index >= 0 and row.get("part_kind"):
                        scope = {
                            "paragraph": "正文",
                            "header": "页眉",
                            "footer": "页脚",
                        }[row["part_kind"]]
                        location = "%s第%d段·段落标记修订人" % (
                            scope, display_paragraph_index + 1)
                    else:
                        location = "DOCX部件%s·%s修订人" % (
                            row["part"], row["kind"])
                    property_author_blocks.append({
                        "part": row["part"],
                        "paragraph_index": -1,
                        "location": location,
                        "text": row["author"],
                        "target": {"kind": "revision_author", **row["target"],
                                   "attribute": W + "author"},
                    })
            author_blocks.extend(property_author_blocks)

            paragraph_indices_by_part = {
                part: {paragraph: index for index, paragraph in
                       enumerate(root.iter(W + "p"))}
                for part, root in roots.items()
            }
            unsupported_by_node = {}
            for item in unsupported:
                key = (item["part"], item["kind"], item["reason"], item["_element"])
                unsupported_by_node[key] = item
            unsupported = list(unsupported_by_node.values())
            unsupported_manifest = []
            unsupported_rows = []
            for item in unsupported:
                row = {key: value for key, value in item.items() if key != "_element"}
                unsupported_rows.append(row)
                element = item["_element"]
                part = item["part"]
                ordinals = ordinals_by_part.get(part, {})
                if element not in ordinals:
                    continue
                unsupported_manifest.append(_unsupported_record(
                    part, element, item["reason"], ordinals[element],
                    _revision_location(element,
                                       paragraph_indices_by_part.get(part, {}))))
            unsupported_manifest.sort(
                key=lambda row: (row["part"], row["kind"], row["ordinal"], row["sha256"]))
            unsupported_digest = hashlib.sha256(json.dumps(
                unsupported_manifest, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            supported_property_manifest = [
                {"part": row["part"], "kind": row["kind"],
                 "ordinal": ordinals_by_part[row["part"]][row["container"]]}
                for row in property_revision_containers
            ]
            hard_parse_errors = [error for error in errors
                                 if not error.startswith("无法解析XML部件：") or
                                 error.rsplit("：", 1)[-1] in {
                                     "[Content_Types].xml", "_rels/.rels",
                                     "word/document.xml",
                                     "word/_rels/document.xml.rels"}]
            unscanned_relationship_parts = any(
                row.get("part", "").lower().endswith(".rels")
                for row in (inventory.get("unscanned_parts") or []))
            passthrough_safe = bool(
                inventory.get("passthrough_safe") and not story_errors and
                not unscanned_relationship_parts and
                inventory.get("external_relationships_valid"))
            can_export_after_ack = bool(
                len(unsupported_manifest) == len(unsupported_rows) and
                (not errors or (passthrough_safe and
                                not hard_parse_errors)))
            unsupported_digest = hashlib.sha256(json.dumps({
                "unsupported_manifest": [
                    row for row in unsupported_manifest
                    if not str(row.get("reason") or "").startswith("兼容文本框")],
                "unscanned_digest": inventory.get("unscanned_digest", ""),
            }, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            result.update(
                paragraphs=paragraphs,
                deleted_blocks=deleted_blocks,
                author_blocks=author_blocks,
                property_author_blocks=property_author_blocks,
                supported_text_revision_count=len(supported_containers),
                supported_property_revision_count=len(property_revision_containers),
                can_export_after_ack=can_export_after_ack,
                unsupported_manifest=unsupported_manifest,
                unsupported_digest=unsupported_digest,
                unsupported_revision_count=len(unsupported_manifest),
                has_revisions=bool(inventory.get("has_revisions") or
                                   inventory.get("unscanned_parts")),
                field_instruction_count=result["field_instruction_count"],
                safe_field_instruction_count=result["safe_field_instruction_count"],
                unsafe_field_instruction_count=result["unsafe_field_instruction_count"],
                unsafe_field_manifest=unsafe_fields,
                embedded_image_count=embedded_image_count,
                text_object_count=len(object_roots),
                unsupported_object_manifest=unsupported_object_records,
                unsupported_object_roots=[
                    {"part": part, "root": logical_root}
                    for part, logical_root in object_roots
                ],
                object_digest=hashlib.sha256(json.dumps(
                    all_object_records, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest(),
                supported_property_manifest=supported_property_manifest,
                unsupported=unsupported_rows + [{"part": "", "kind": "parse",
                                                  "reason": error} for error in errors],
                complete=not errors,
                passthrough_safe=passthrough_safe,
                unscanned_parts=list(inventory.get("unscanned_parts") or []),
                supported=not unsupported_rows and not errors,
                can_preserve_and_redact=not unsupported_rows and not errors,
            )
            result["inventory"]["supported"] = result["supported"]
            result["inventory"]["can_preserve_and_redact"] = result["can_preserve_and_redact"]
            result["inventory"]["can_export_after_ack"] = can_export_after_ack
            result["inventory"]["supported_text_revision_count"] = len(supported_containers)
            result["inventory"]["supported_property_revision_count"] = len(property_revision_containers)
            result["inventory"]["unsupported_manifest"] = unsupported_manifest
            result["inventory"]["unsupported_digest"] = unsupported_digest
            result["inventory"]["unsupported_revision_count"] = len(unsupported_manifest)
            result["inventory"]["has_revisions"] = result["has_revisions"]
            result["inventory"]["passthrough_safe"] = result["passthrough_safe"]
            result["inventory"]["unscanned_parts"] = result["unscanned_parts"]
            result["inventory"]["field_instruction_count"] = result["field_instruction_count"]
            result["inventory"]["safe_field_instruction_count"] = result["safe_field_instruction_count"]
            result["inventory"]["unsafe_field_instruction_count"] = result["unsafe_field_instruction_count"]
            result["inventory"]["unsafe_field_manifest"] = unsafe_fields
            result["inventory"]["unsafe_field_digest"] = unsafe_field_digest
            result["inventory"]["embedded_image_count"] = embedded_image_count
            result["inventory"]["text_object_count"] = len(object_roots)
            result["inventory"]["unsupported_object_manifest"] = unsupported_object_records
            result["inventory"]["object_digest"] = result["object_digest"]
            result["inventory"]["supported_property_manifest"] = supported_property_manifest
            result["inventory"]["unsupported"] = result["unsupported"]
            result["inventory"]["complete"] = result["complete"]
            result["inventory"]["passthrough_safe"] = passthrough_safe
            return result
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, KeyError):
        result["complete"] = False
        result["supported"] = False
        result["can_preserve_and_redact"] = False
        result["can_export_after_ack"] = False
        result["unsupported"] = [{"part": "", "kind": "package",
                                  "reason": "DOCX修订结构无法安全分析"}]
        result["inventory"]["complete"] = False
        result["inventory"]["can_export_after_ack"] = False
        result["inventory"]["errors"] = ["DOCX修订结构无法安全分析"]
        return result


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _range_segments(units: List[Dict[str, Any]], start: int, end: int):
    segments = []
    for index in range(start, end):
        unit = units[index]
        if not unit.get("writable") or unit.get("node_offset") is None:
            raise ValueError("选中修订文字含不可改写的控制字符")
        node = unit["node"]
        offset = int(unit["node_offset"])
        if segments and segments[-1][0] is node and segments[-1][2] == offset:
            segments[-1][2] += 1
        else:
            segments.append([node, offset, offset + 1])
    return segments


def _split_replacement(value: str, lengths: List[int]) -> List[str]:
    if len(lengths) <= 1:
        return [value]
    if len(value) <= len(lengths):
        return [value[i:i + 1] if i < len(value) else "" for i in range(len(lengths))]
    total = sum(lengths) or len(lengths)
    sizes = [max(1, round(len(value) * length / total)) for length in lengths]
    while sum(sizes) > len(value):
        index = max((i for i, size in enumerate(sizes) if size > 1),
                    key=lambda i: sizes[i], default=-1)
        if index < 0:
            break
        sizes[index] -= 1
    while sum(sizes) < len(value):
        sizes[sizes.index(min(sizes))] += 1
    result, cursor = [], 0
    for size in sizes:
        result.append(value[cursor:cursor + size])
        cursor += size
    return result


def _write_text_operations(text: str, units: List[Dict[str, Any]], operations: List[Any]) -> int:
    ordered = sorted(operations, key=lambda op: op.local_start)
    for previous, current in zip(ordered, ordered[1:]):
        if current.local_start < previous.local_end:
            raise ValueError("修订扫描结果存在重叠目标，已停止导出")
    pieces: Dict[Any, List[str]] = {}
    nodes: Dict[Any, Any] = {}
    for unit in units:
        node = unit["node"]
        if node not in pieces:
            nodes[node] = node
            pieces[node] = list(node.text or "")
    for op in sorted(operations, key=lambda item: item.local_start, reverse=True):
        start, end = op.local_start, op.local_end
        if (start < 0 or end <= start or end > len(text) or text[start:end] != op.value):
            raise ValueError("修订文本目标与原扫描结果不一致")
        segments = _range_segments(units, start, end)
        replacements = _split_replacement(
            op.replacement, [right - left for _node, left, right in segments])
        for (node, left, right), replacement in zip(segments, replacements):
            pieces[node][left:right] = list(replacement)
    for node, chars in pieces.items():
        node.text = "".join(chars)
        if node.text[:1].isspace() or node.text[-1:].isspace():
            node.set(XML_SPACE, "preserve")
    return len(operations)


def _write_combined_text_operations(
        view_groups: List[Tuple[str, List[Dict[str, Any]], List[Any]]]) -> int:
    """Apply edits from multiple paragraph views against their shared XML nodes."""
    original_by_node: Dict[Any, str] = {}
    edits_by_node: Dict[Any, List[Tuple[int, int, str, int, int]]] = {}
    output_segments_by_op: Dict[int, List[Optional[Dict[str, Any]]]] = {}
    applied = 0
    for text, units, operations in view_groups:
        for op in operations:
            start, end = int(op.local_start), int(op.local_end)
            if (start < 0 or end <= start or end > len(text) or
                    text[start:end] != op.value):
                raise ValueError("修订文本目标与原扫描结果不一致")
            segments = _range_segments(units, start, end)
            lengths = [right - left for _node, left, right in segments]
            replacements = _split_replacement(op.replacement, lengths)
            op_id = id(op)
            output_segments_by_op[op_id] = [None] * len(segments)
            source_segments = []
            for segment_index, ((node, left, right), replacement) in enumerate(
                    zip(segments, replacements)):
                original_by_node.setdefault(node, node.text or "")
                node_path = node.getroottree().getpath(node)
                node_kind = (node.tag[len(W):] if isinstance(node.tag, str) and
                             node.tag.startswith(W) else str(node.tag))
                source_segments.append({"node_path": node_path,
                                        "node_kind": node_kind,
                                        "start": left, "end": right})
                edits_by_node.setdefault(node, []).append(
                    (left, right, replacement, op_id, segment_index))
            if op.target is not None:
                op.target["physical_segments"] = source_segments
            applied += 1

    for node, original in original_by_node.items():
        edits = sorted(edits_by_node[node], key=lambda item: item[0])
        if any(current[0] < previous[1]
               for previous, current in zip(edits, edits[1:])):
            raise ValueError("修订扫描结果存在重叠目标，已停止导出")
        value = original
        shift = 0
        for left, right, replacement, op_id, segment_index in edits:
            output_start = left + shift
            output_segments_by_op[op_id][segment_index] = {
                "node_path": node.getroottree().getpath(node),
                "node_kind": (node.tag[len(W):] if isinstance(node.tag, str) and
                              node.tag.startswith(W) else str(node.tag)),
                "start": output_start,
                "end": output_start + len(replacement),
            }
            shift += len(replacement) - (right - left)
        for left, right, replacement, _op_id, _segment_index in reversed(edits):
            if right > len(original):
                raise ValueError("修订文字物理目标超出XML节点范围")
            value = value[:left] + replacement + value[right:]
        node.text = value
        if value[:1].isspace() or value[-1:].isspace():
            node.set(XML_SPACE, "preserve")
    for _text, _units, operations in view_groups:
        for op in operations:
            if op.target is not None:
                op.target["output_physical_segments"] = [
                    segment for segment in output_segments_by_op[id(op)]
                    if segment is not None]
    return applied


def _restore_physical_segments(root, rows: List[Dict[str, Any]]) -> int:
    edits_by_node: Dict[Any, List[Tuple[int, int, str, str]]] = {}
    restored_rows = 0
    for row in rows:
        target = row.get("target") or {}
        source_segments = target.get("physical_segments") or []
        output_segments = target.get("output_physical_segments") or []
        original = str(row.get("original", ""))
        placeholder = str(row.get("placeholder", ""))
        lengths = [int(segment.get("end", 0)) - int(segment.get("start", 0))
                   for segment in source_segments]
        if (not source_segments or len(source_segments) != len(output_segments) or
                sum(lengths) != len(original) or not placeholder):
            raise RuntimeError("DOCX mapping物理文字片段信息不完整")
        expected_parts = _split_replacement(placeholder, lengths)
        original_parts = _split_replacement(original, lengths)
        for segment, expected, replacement in zip(
                output_segments, expected_parts, original_parts):
            path = str(segment.get("node_path", ""))
            start = int(segment.get("start", -1))
            end = int(segment.get("end", -1))
            if not path or start < 0 or end < start or end - start != len(expected):
                raise RuntimeError("DOCX mapping物理输出位置无效")
            matches = root.getroottree().xpath(
                path, namespaces=_xpath_namespaces(root))
            if len(matches) != 1:
                raise RuntimeError("DOCX mapping物理输出节点缺失或不唯一")
            node = matches[0]
            value = node.text or ""
            if end > len(value) or value[start:end] != expected:
                raise RuntimeError("DOCX mapping物理输出内容与占位符不一致")
            edits_by_node.setdefault(node, []).append(
                (start, end, expected, replacement))
        restored_rows += 1

    for node, edits in edits_by_node.items():
        ordered = sorted(edits, key=lambda item: item[0])
        for previous, current in zip(ordered, ordered[1:]):
            if (current[0] < previous[1] or
                    (current[0] == previous[0] and
                     (current[0] == current[1] or previous[0] == previous[1]))):
                raise RuntimeError("DOCX mapping物理还原目标重叠")
        value = node.text or ""
        for start, end, _expected, replacement in reversed(ordered):
            value = value[:start] + replacement + value[end:]
        node.text = value
        if value[:1].isspace() or value[-1:].isspace():
            node.set(XML_SPACE, "preserve")
    return restored_rows


def _physical_restore_paths_resolve(root, rows: List[Dict[str, Any]]) -> bool:
    for row in rows:
        target = row.get("target") or {}
        for segment in target.get("output_physical_segments") or []:
            path = str(segment.get("node_path", ""))
            if not path or len(root.getroottree().xpath(
                    path, namespaces=_xpath_namespaces(root))) != 1:
                return False
    return True


def split_shared_revision_view_operations(operations: List[Any]) -> List[Any]:
    """Keep one accepted-view edit for shared text and isolate rejected-view deltas."""
    accepted_positions: Dict[Tuple[str, int, str], Set[Tuple[str, str, int]]] = {}
    for op in operations:
        target = op.target or {}
        if target.get("kind") != "revision_visible_text":
            continue
        key = (str(target.get("part", "")),
               int(target.get("paragraph_index", -1)), op.type_id)
        for segment in target.get("physical_segments") or []:
            path = str(segment.get("node_path", ""))
            node_kind = str(segment.get("node_kind", ""))
            for offset in range(int(segment.get("start", 0)),
                                int(segment.get("end", 0))):
                accepted_positions.setdefault(key, set()).add(
                    (path, node_kind, offset))

    result: List[Any] = []
    for op in operations:
        target = op.target or {}
        if target.get("kind") != "revision_rejected_text":
            result.append(op)
            continue
        key = (str(target.get("part", "")),
               int(target.get("paragraph_index", -1)), op.type_id)
        shared = accepted_positions.get(key) or set()
        positions: List[Tuple[str, str, int]] = []
        for segment in target.get("physical_segments") or []:
            path = str(segment.get("node_path", ""))
            node_kind = str(segment.get("node_kind", ""))
            positions.extend((path, node_kind, offset)
                             for offset in range(int(segment.get("start", 0)),
                                                 int(segment.get("end", 0))))
        if len(positions) != len(op.value) or not any(pos in shared for pos in positions):
            result.append(op)
            continue

        unique = [index for index, position in enumerate(positions)
                  if position not in shared]
        run_start = None
        runs: List[Tuple[int, int]] = []
        for index in unique:
            if run_start is None:
                run_start = index
            elif index != previous + 1:
                runs.append((run_start, previous + 1))
                run_start = index
            previous = index
        if run_start is not None:
            runs.append((run_start, previous + 1))

        for start, end in runs:
            fragment = copy.deepcopy(op)
            fragment.value = op.value[start:end]
            fragment.local_start = op.local_start + start
            fragment.local_end = op.local_start + end
            fragment.start = op.start + start
            fragment.end = op.start + end
            segments: List[Dict[str, Any]] = []
            for path, node_kind, offset in positions[start:end]:
                if (segments and segments[-1]["node_path"] == path and
                        segments[-1]["node_kind"] == node_kind and
                        segments[-1]["end"] == offset):
                    segments[-1]["end"] = offset + 1
                else:
                    segments.append({"node_path": path, "node_kind": node_kind,
                                     "start": offset, "end": offset + 1})
            fragment.target["physical_segments"] = segments
            fragment.target["local_offset"] = [fragment.local_start,
                                                fragment.local_end]
            result.append(fragment)
    return result


def _locate_revision(paragraph, target: Dict[str, Any]):
    kind = target.get("revision_kind")
    candidates = [node for node in paragraph.iter(W + str(kind))]
    ordinal = int(target.get("revision_ordinal", -1))
    if ordinal < 0 or ordinal >= len(candidates):
        return None
    element = candidates[ordinal]
    if (element.get(W + "id", "") != str(target.get("revision_id", "")) or
            _fingerprint(element) != target.get("revision_fingerprint")):
        return None
    return element


def _atomic_rewrite(path: str, replacements: Dict[str, bytes]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=directory)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "r") as source:
            infos = source.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ValueError("DOCX ZIP含重复部件名")
            if len(infos) > MAX_MEMBERS or sum(item.file_size for item in infos) > MAX_UNPACKED_BYTES:
                raise ValueError("DOCX包超过安全处理上限")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as target:
                for info in infos:
                    target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
        with zipfile.ZipFile(tmp, "r") as check:
            if check.testzip() is not None:
                raise ValueError("DOCX ZIP完整性校验失败")
            for name, data in replacements.items():
                if name.lower().endswith((".xml", ".rels")):
                    _parse_xml(check.read(name))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def capture_output_targets(docx_path: str, records: List[Dict[str, Any]]) -> None:
    """Attach final output hashes and structure fingerprints to targeted DOCX records."""
    paragraph_groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    revision_rows: List[Dict[str, Any]] = []
    for row in records:
        target = row.get("target") or {}
        kind = target.get("kind")
        if kind in {"text", "revision_visible_text", "revision_rejected_text"}:
            part = str(target.get("part", ""))
            paragraph_index = int(target.get("paragraph_index", -1))
            if not part or paragraph_index < 0:
                raise ValueError("正文mapping目标位置无效")
            paragraph_groups.setdefault((part, paragraph_index), []).append(row)
        elif kind in {"revision_deleted_text", "revision_author"}:
            revision_rows.append(row)
    if not paragraph_groups and not revision_rows:
        return

    parts = {part for part, _paragraph_index in paragraph_groups}
    parts.update(str((row.get("target") or {}).get("part", ""))
                 for row in revision_rows)
    with zipfile.ZipFile(docx_path, "r") as archive:
        names = set(archive.namelist())
        roots: Dict[str, Any] = {}
        for part in parts:
            if part not in names:
                raise ValueError("DOCX mapping目标部件缺失")
            roots[part] = _parse_xml(archive.read(part))
        for (part, paragraph_index), rows in paragraph_groups.items():
            paragraphs = list(roots[part].iter(W + "p"))
            if paragraph_index >= len(paragraphs):
                raise ValueError("正文mapping目标段落缺失")
            paragraph = paragraphs[paragraph_index]
            expected_id = str((rows[0].get("target") or {}).get("paragraph_id", ""))
            if expected_id and _paragraph_id(paragraph) != expected_id:
                raise ValueError("正文mapping目标段落标识已变化")
            fingerprint = _fingerprint(paragraph)
            for row in rows:
                target = row.get("target") or {}
                view = "rejected" if target.get("revision_view") == "rejected" else "accepted"
                text, _units = _paragraph_revision_view(
                    paragraph, part, paragraph_index, view)
                row["target"].update(output_text_sha256=_text_hash(text),
                                     output_structure_fingerprint=fingerprint)

        for row in revision_rows:
            target = row["target"]
            part = str(target.get("part", ""))
            paragraph_index = int(target.get("paragraph_index", -1))
            if target.get("revision_scope") == "part":
                element = _locate_revision(roots[part], target)
            else:
                paragraphs = list(roots[part].iter(W + "p"))
                if paragraph_index < 0 or paragraph_index >= len(paragraphs):
                    raise ValueError("修订mapping目标段落缺失")
                element = _locate_revision(paragraphs[paragraph_index], target)
            if element is None:
                raise ValueError("修订mapping目标结构已变化")
            if target.get("kind") == "revision_deleted_text":
                text, _units = _revision_text_view(element, "del")
            else:
                text = element.get(target.get("attribute", W + "author"), "")
            target.update(output_text_sha256=_text_hash(text),
                          output_structure_fingerprint=_fingerprint(element))


def build_expected_preserved_manifest(source_path, operations):
    """在隔离克隆精确应用已选修订人属性；其余内容仍按原始摘要保护。"""
    roots, seen = {}, set()
    with zipfile.ZipFile(source_path) as source:
        for op in operations:
            target = op.target or {}
            if target.get("kind") != "revision_author":
                continue
            part = target.get("part")
            if part not in source.namelist():
                raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：目标部件不存在")
            root = roots.setdefault(part, _parse_xml(source.read(part)))
            scope = root
            if target.get("revision_scope") != "part":
                paragraphs = list(root.iter(W + "p"))
                index = int(target.get("paragraph_index", -1))
                if not 0 <= index < len(paragraphs):
                    raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：目标段落不存在")
                scope = paragraphs[index]
            element = _locate_revision(scope, target)
            attribute = target.get("attribute", W + "author")
            if element is None or attribute != W + "author" or element.get(attribute) != op.value:
                raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：目标或旧值不一致")
            identity = (part, element.getroottree().getpath(element), attribute)
            if identity in seen:
                raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：同一属性重复操作")
            seen.add(identity)
            element.set(attribute, op.replacement)
        if not roots:
            return analyze_docx_revisions(source_path)
        with tempfile.TemporaryDirectory(prefix="expected-revision-") as temp:
            clone = os.path.join(temp, "expected.docx")
            with zipfile.ZipFile(clone, "w", zipfile.ZIP_DEFLATED) as target:
                for entry in source.infolist():
                    target.writestr(entry, _serialize(roots[entry.filename]) if entry.filename in roots
                                    else source.read(entry.filename))
            return analyze_docx_revisions(clone)


def rewrite_revision_occurrences(source_path: str, output_path: str,
                                operations: List[Any], *,
                                allow_unsupported_revisions: bool = False,
                                expected_unsupported_digest: Optional[str] = None,
                                allow_incomplete_passthrough: bool = False) -> Dict[str, Any]:
    """Rewrite supported targets while verifying acknowledged unsupported structures."""
    analysis = analyze_docx_revisions(source_path)
    inventory = analysis.get("inventory") or {}
    unsupported = analysis.get("unsupported_manifest") or []
    incomplete_passthrough = bool(
        not analysis.get("complete") and allow_incomplete_passthrough and
        analysis.get("passthrough_safe") and inventory.get("unscanned_parts"))
    if ((not analysis.get("complete") and not incomplete_passthrough) or
            not analysis.get("has_revisions")):
        raise ValueError("DOCX含无法安全处理的修订结构，已停止导出")
    source_digest = str(inventory.get("unsupported_digest") or "")
    if unsupported and not incomplete_passthrough:
        if (not allow_unsupported_revisions or
                not analysis.get("can_export_after_ack") or
                source_digest != expected_unsupported_digest):
            raise ValueError("未支持修订未经确认或结构已变化，已停止导出")
    elif not unsupported and not incomplete_passthrough and not analysis.get("can_preserve_and_redact"):
        raise ValueError("DOCX含无法安全处理的修订结构，已停止导出")

    skipped_ops: List[Any] = []
    if incomplete_passthrough:
        unscanned_parts = {
            str(row if isinstance(row, str) else row.get("part") or "")
            for row in inventory.get("unscanned_parts") or []
        }
        safe_operations = []
        for op in operations:
            target_part = str((op.target or {}).get("part") or "")
            if target_part and target_part in unscanned_parts:
                skipped_ops.append(op)
            else:
                safe_operations.append(op)
        operations = safe_operations

    expected_analysis = build_expected_preserved_manifest(source_path, operations)
    expected_inventory = expected_analysis.get("inventory") or {}
    expected_digest = expected_inventory.get("unsupported_digest")

    def validate_output():
        output_analysis = analyze_docx_revisions(output_path)
        output_inventory = output_analysis.get("inventory") or {}
        incomplete_preserved = bool(
            incomplete_passthrough and not output_analysis.get("complete") and
            output_analysis.get("passthrough_safe") and
            output_inventory.get("unscanned_digest") == inventory.get("unscanned_digest"))
        if ((not output_analysis.get("complete") and not incomplete_preserved) or
                output_inventory.get("unsupported_digest") != expected_digest):
            raise ValueError("修订改写改变了已确认保留的结构，已停止导出")

    if not operations:
        shutil.copyfile(source_path, output_path)
        try:
            validate_output()
        except Exception:
            if os.path.exists(output_path):
                os.unlink(output_path)
            raise
        return {"applied": 0, "expected_unsupported_digest": expected_digest,
                "skipped_ops": skipped_ops}

    grouped: Dict[Tuple[str, str, int, str, str, int], List[Any]] = {}
    for op in operations:
        target = op.target or {}
        kind = target.get("kind")
        if kind not in {"text", "revision_visible_text", "revision_rejected_text",
                        "revision_deleted_text", "revision_author"}:
            raise ValueError("修订改写目标类型无效")
        part = str(target.get("part", ""))
        paragraph_index = int(target.get("paragraph_index", -1))
        part_scope = target.get("revision_scope") == "part"
        if not part or (paragraph_index < 0 and not part_scope):
            raise ValueError("修订改写目标位置无效")
        if part_scope and kind != "revision_author":
            raise ValueError("部件级修订目标仅支持修订人属性")
        if target.get("rewrite_supported") is False:
            raise ValueError(target.get("rewrite_block_reason") or "修订目标无法安全改写")
        key = (part, kind, paragraph_index,
               str(target.get("revision_kind", "")),
               str(target.get("revision_id", "")),
               int(target.get("revision_ordinal", -1)))
        grouped.setdefault(key, []).append(op)

    replacements: Dict[str, bytes] = {}
    applied = 0
    expected_views: Dict[Tuple[str, int, str], str] = {}
    with zipfile.ZipFile(source_path, "r") as archive:
        names = set(archive.namelist())
        roots: Dict[str, Any] = {}
        for part, _kind, _pi, _revision_kind, _revision_id, _ordinal in grouped:
            if part not in names:
                raise ValueError("修订目标部件不存在")
            if part not in roots:
                roots[part] = _parse_xml(archive.read(part))

        paragraph_groups: Dict[Tuple[str, int], List[Tuple[str, List[Any]]]] = {}
        for (part, kind, paragraph_index, _revision_kind, _revision_id, _ordinal), ops in grouped.items():
            if kind in {"text", "revision_visible_text", "revision_rejected_text"}:
                paragraph_groups.setdefault((part, paragraph_index), []).append((kind, ops))

        for (part, paragraph_index), view_rows in paragraph_groups.items():
            paragraphs = list(roots[part].iter(W + "p"))
            if paragraph_index < 0 or paragraph_index >= len(paragraphs):
                raise ValueError("修订目标段落已变化")
            paragraph = paragraphs[paragraph_index]
            fingerprint = _fingerprint(paragraph)
            by_view: Dict[str, Dict[str, Any]] = {}
            for kind, ops in view_rows:
                target = ops[0].target
                if fingerprint != target.get("structure_fingerprint"):
                    raise ValueError("修订正文结构已变化")
                view = "rejected" if kind == "revision_rejected_text" else "accepted"
                text, units = _paragraph_revision_view(
                    paragraph, part, paragraph_index, view)
                source_hash = _text_hash(text)
                for op in ops:
                    digest = (op.target or {}).get("source_text_sha256")
                    if digest and digest != source_hash:
                        raise ValueError("修订视图与扫描时的原文不一致")
                view_row = by_view.setdefault(view, {
                    "text": text, "units": units, "ops": [], "source_hash": source_hash})
                if view_row["text"] != text or view_row["source_hash"] != source_hash:
                    raise ValueError("同一修订视图的扫描摘要不一致")
                view_row["ops"].extend(ops)

            applied += _write_combined_text_operations([
                (row["text"], row["units"], row["ops"])
                for row in by_view.values()])
            output_fingerprint = _fingerprint(paragraph)
            for view, row in by_view.items():
                output_text, _output_units = _paragraph_revision_view(
                    paragraph, part, paragraph_index, view)
                expected_views[(part, paragraph_index, view)] = output_text
                output_hash = _text_hash(output_text)
                for op in row["ops"]:
                    op.target.update(source_text_sha256=row["source_hash"],
                                     output_text_sha256=output_hash,
                                     output_structure_fingerprint=output_fingerprint,
                                     revision_view=view)

        for (part, kind, paragraph_index, _revision_kind, _revision_id, _ordinal), ops in grouped.items():
            if kind in {"text", "revision_visible_text", "revision_rejected_text"}:
                continue
            root = roots[part]
            target = ops[0].target
            if target.get("revision_scope") == "part":
                if kind != "revision_author":
                    raise ValueError("部件级修订目标仅支持修订人属性")
                paragraph = None
                element = _locate_revision(root, target)
                if element is None:
                    raise ValueError("属性修订人目标结构已变化")
            else:
                paragraphs = list(root.iter(W + "p"))
                if paragraph_index < 0 or paragraph_index >= len(paragraphs):
                    raise ValueError("修订目标段落已变化")
                paragraph = paragraphs[paragraph_index]
            if kind == "revision_deleted_text":
                element = _locate_revision(paragraph, target)
                if element is None:
                    raise ValueError("删除修订目标结构已变化")
                text, units = _revision_text_view(element, "del")
                source_hash = _text_hash(text)
                applied += _write_combined_text_operations([(text, units, ops)])
                output_text, _output_units = _revision_text_view(element, "del")
                output_hash = _text_hash(output_text)
                output_fingerprint = _fingerprint(element)
                for op in ops:
                    op.target.update(source_text_sha256=source_hash,
                                     output_text_sha256=output_hash,
                                     output_structure_fingerprint=output_fingerprint)
            else:
                if paragraph is not None:
                    element = _locate_revision(paragraph, target)
                attribute = target.get("attribute", W + "author")
                if element is None or element.get(attribute, "") != ops[0].value:
                    raise ValueError("修订人目标与原扫描结果不一致")
                if len(ops) != 1:
                    raise ValueError("修订人属性目标重复")
                element.set(attribute, ops[0].replacement)
                op = ops[0]
                op.target.update(source_text_sha256=_text_hash(op.value),
                                 output_text_sha256=_text_hash(op.replacement),
                                 output_structure_fingerprint=_fingerprint(element))
                applied += 1
        replacements.update({part: _serialize(root) for part, root in roots.items()})

    shutil.copyfile(source_path, output_path)
    try:
        _atomic_rewrite(output_path, replacements)
        # Re-read every changed story part before publishing the result.
        with zipfile.ZipFile(output_path, "r") as check:
            for part in replacements:
                root = _parse_xml(check.read(part))
                expected_for_part = [
                    (paragraph_index, view, expected_text)
                    for (expected_part, paragraph_index, view), expected_text
                    in expected_views.items() if expected_part == part]
                paragraphs = list(root.iter(W + "p"))
                for paragraph_index, view, expected_text in expected_for_part:
                    if paragraph_index >= len(paragraphs):
                        raise ValueError("最终DOCX修订视图目标段落缺失")
                    actual_text, _units = _paragraph_revision_view(
                        paragraphs[paragraph_index], part, paragraph_index, view)
                    if actual_text != expected_text:
                        raise ValueError("最终DOCX修订视图与目标写回结果不一致")
        validate_output()
        return {"applied": applied, "expected_unsupported_digest": expected_digest,
                "skipped_ops": skipped_ops}
    except Exception:
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise


def restore_revision_occurrences(docx_path: str, records: List[Dict[str, Any]],
                                 compatible: bool = True) -> Dict[str, Any]:
    """Restore targeted text and revision fields after strict output validation."""
    supported_kinds = {"text", "revision_visible_text", "revision_rejected_text",
                       "revision_deleted_text", "revision_author"}
    rows = [row for row in records
            if (row.get("target") or {}).get("kind") in supported_kinds]
    if not rows:
        return {"applied": 0, "mode": "none"}

    grouped: Dict[Tuple[str, str, int, str, str, int], List[Dict[str, Any]]] = {}
    for row in rows:
        target = row.get("target") or {}
        kind = target.get("kind")
        if kind not in supported_kinds:
            raise RuntimeError("DOCX mapping目标类型无效")
        part_scope = target.get("revision_scope") == "part"
        key = (str(target.get("part", "")), str(kind),
               int(target.get("paragraph_index", -1)),
               str(target.get("revision_kind", "")),
               str(target.get("revision_id", "")),
               int(target.get("revision_ordinal", -1)))
        if (not key[0] or
                (part_scope and (kind != "revision_author" or key[2] != -1)) or
                (not part_scope and key[2] < 0)):
            raise RuntimeError("DOCX mapping目标位置无效")
        grouped.setdefault(key, []).append(row)

    replacements: Dict[str, bytes] = {}
    prepared: List[Tuple[str, str, int, Any, List[Dict[str, Any]], str, List[Dict[str, Any]]]] = []
    with zipfile.ZipFile(docx_path, "r") as archive:
        names = set(archive.namelist())
        roots: Dict[str, Any] = {}
        for part, _kind, _pi, _revision_kind, _revision_id, _ordinal in grouped:
            if part not in names:
                raise RuntimeError("DOCX mapping目标部件缺失")
            if part not in roots:
                roots[part] = _parse_xml(archive.read(part))
        for (part, kind, paragraph_index, _revision_kind, _revision_id, _ordinal), items in grouped.items():
            root = roots[part]
            paragraphs = list(root.iter(W + "p"))
            target = items[0].get("target") or {}
            output_fingerprint = target.get("output_structure_fingerprint") or target.get("structure_fingerprint")
            output_hash = target.get("output_text_sha256")
            if not output_fingerprint or not output_hash:
                raise RuntimeError("DOCX mapping缺少目标输出校验信息")

            if kind in {"text", "revision_visible_text", "revision_rejected_text"}:
                view = "rejected" if target.get("revision_view") == "rejected" else "accepted"
                if compatible:
                    matches = []
                    expected_id = str(target.get("paragraph_id", ""))
                    for pi, candidate in enumerate(paragraphs):
                        if expected_id and _paragraph_id(candidate) != expected_id:
                            continue
                        candidate_text, candidate_units = _paragraph_revision_view(
                            candidate, part, pi, view)
                        if (_fingerprint(candidate) == output_fingerprint and
                                _text_hash(candidate_text) == output_hash):
                            matches.append((pi, candidate, candidate_text, candidate_units))
                    if len(matches) != 1:
                        raise RuntimeError("正文mapping目标缺失或不唯一")
                    paragraph_index, paragraph, text, units = matches[0]
                else:
                    if paragraph_index >= len(paragraphs):
                        raise RuntimeError("正文或修订目标段落已变化")
                    paragraph = paragraphs[paragraph_index]
                    text, units = _paragraph_revision_view(
                        paragraph, part, paragraph_index, view)
                    if (_fingerprint(paragraph) != output_fingerprint or
                            _text_hash(text) != output_hash):
                        raise RuntimeError("正文或修订目标与mapping不一致")
                element = paragraph
            else:
                part_scope = target.get("revision_scope") == "part"
                revision_kind = str(target.get("revision_kind", ""))
                if (kind == "revision_deleted_text" and revision_kind != "del") or (
                        kind == "revision_author" and revision_kind not in
                        (TEXT_REVISION_TAGS | set(PROPERTY_REVISION_TAGS))):
                    raise RuntimeError("DOCX mapping修订类型无效")
                if compatible:
                    matches = []
                    revision_id = str(target.get("revision_id", ""))
                    if part_scope:
                        revision_candidates = ((-1, candidate)
                                               for candidate in roots[part].iter(W + revision_kind))
                    else:
                        revision_candidates = (
                            (pi, candidate)
                            for pi, candidate_paragraph in enumerate(paragraphs)
                            for candidate in candidate_paragraph.iter(W + revision_kind))
                    for pi, candidate in revision_candidates:
                        if candidate.get(W + "id", "") != revision_id:
                            continue
                        if _fingerprint(candidate) != output_fingerprint:
                            continue
                        if kind == "revision_deleted_text":
                            candidate_text, candidate_units = _revision_text_view(candidate, "del")
                        else:
                            candidate_text = candidate.get(target.get("attribute", W + "author"), "")
                            candidate_units = []
                        if _text_hash(candidate_text) == output_hash:
                            matches.append((pi, candidate, candidate_text, candidate_units))
                    if len(matches) != 1:
                        raise RuntimeError("修订mapping目标缺失或不唯一")
                    paragraph_index, element, text, units = matches[0]
                else:
                    if part_scope:
                        element = _locate_revision(roots[part], target)
                    else:
                        if paragraph_index < 0 or paragraph_index >= len(paragraphs):
                            raise RuntimeError("修订目标已被接受、拒绝或删除")
                        element = _locate_revision(paragraphs[paragraph_index], target)
                    if element is None:
                        raise RuntimeError("修订容器已被接受、拒绝或删除")
                    if kind == "revision_deleted_text":
                        text, units = _revision_text_view(element, "del")
                    else:
                        text, units = element.get(target.get("attribute", W + "author"), ""), []
                    if (_fingerprint(element) != output_fingerprint or
                            _text_hash(text) != output_hash):
                        raise RuntimeError("修订目标结构或输出文本与mapping不一致")
            prepared.append((part, kind, paragraph_index, element, items, text, units))

    applied = 0
    paragraph_restore_groups: Dict[
        Tuple[str, int], List[Tuple[str, str, List[Dict[str, Any]], List[Any]]]] = {}
    physical_restore_rows: Dict[str, List[Dict[str, Any]]] = {}
    for part, kind, paragraph_index, element, items, text, units in prepared:
        if kind == "revision_author":
            if len(items) != 1:
                raise RuntimeError("修订人mapping目标重复")
            item = items[0]
            placeholder = str(item.get("placeholder", ""))
            if text != placeholder:
                raise RuntimeError("修订人占位符与mapping不匹配")
            element.set(item["target"].get("attribute", W + "author"), item.get("original", ""))
            applied += 1
            continue

        by_placeholder: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            placeholder = str(item.get("placeholder", ""))
            if not placeholder:
                raise RuntimeError("修订mapping缺少占位符")
            by_placeholder.setdefault(placeholder, []).append(item)
        ops = []
        for placeholder, group in by_placeholder.items():
            starts = [match.start() for match in re.finditer(re.escape(placeholder), text)]
            if len(starts) != len(group):
                raise RuntimeError("修订目标占位符数量与mapping不一致")
            group.sort(key=lambda row: (int((row.get("target") or {}).get("local_offset", [0])[0]),
                                        int(row.get("start", 0))))
            if (all((row.get("target") or {}).get("output_physical_segments")
                    for row in group) and
                    _physical_restore_paths_resolve(roots[part], group)):
                physical_restore_rows.setdefault(part, []).extend(group)
                continue
            for start, item in zip(starts, group):
                ops.append(type("RestoreOp", (), {
                    "local_start": start,
                    "local_end": start + len(placeholder),
                    "value": placeholder,
                    "replacement": str(item.get("original", "")),
                    "target": item.get("target") or {},
                })())
        if kind in {"text", "revision_visible_text", "revision_rejected_text"}:
            view = "rejected" if (items[0].get("target") or {}).get(
                "revision_view") == "rejected" else "accepted"
            paragraph_restore_groups.setdefault(
                (part, paragraph_index), []).append((view, text, units, ops))
        else:
            applied += _write_text_operations(text, units, ops)

    for part, rows in physical_restore_rows.items():
        applied += _restore_physical_segments(roots[part], rows)

    for (_part, _paragraph_index), groups in paragraph_restore_groups.items():
        applied += _write_combined_text_operations([
            (text, units, ops) for _view, text, units, ops in groups])

    replacements.update({part: _serialize(root) for part, root in roots.items()})
    _atomic_rewrite(docx_path, replacements)
    with zipfile.ZipFile(docx_path, "r") as check:
        for part in replacements:
            _parse_xml(check.read(part))
    return {"applied": applied,
            "mode": "structure_compatible" if compatible else "exact_hash"}
