#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DOCX 批注部件的只读提取、定点改写、还原和删除。"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import tempfile
import zipfile
from lxml import etree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{" + W_NS + "}"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "{" + REL_NS + "}"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CT = "{" + CT_NS + "}"
COMMENT_MARKERS = {W + "commentReference", W + "commentRangeStart", W + "commentRangeEnd"}
COMMENT_PART_RE = re.compile(r"(?:^|/)comments(?:[A-Za-z0-9_-]*)\.xml$", re.I)
KNOWN_COMMENT_PARTS = {
    "word/comments.xml", "word/commentsextended.xml", "word/commentsids.xml",
    "word/commentsextensible.xml", "word/people.xml",
}
IDENTITY_ATTRS = {"author", "authorid", "displayname", "userid", "email", "name"}
UNSUPPORTED_TAGS = {
    "ins", "del", "delText", "moveFrom", "moveTo", "drawing", "pict", "object",
    "altChunk", "fldSimple", "instrText", "fldChar", "vanish", "webHidden",
    "specVanish", "rPrChange", "pPrChange",
}


@dataclass
class CommentParagraphRef:
    part_name: str
    comment_id: str
    paragraph_index: int
    text: str


@dataclass
class CommentIdentityRef:
    part_name: str
    value: str
    field_kind: str
    attribute_name: str
    element_index: Optional[int] = None
    element_path: Optional[Tuple[int, ...]] = None
    comment_id: Optional[str] = None
    initials: str = ""
    location: str = ""


@dataclass
class CommentPackageInfo:
    paragraphs: List[CommentParagraphRef] = field(default_factory=list)
    identities: List[CommentIdentityRef] = field(default_factory=list)
    related_parts: set = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)

    @property
    def comment_count(self) -> int:
        return len({(p.part_name, p.comment_id) for p in self.paragraphs})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _owner_from_rels(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return ""
    marker = "/_rels/"
    if marker not in rels_name:
        return ""
    folder, leaf = rels_name.split(marker, 1)
    return posixpath.join(folder, leaf[:-5]) if leaf.endswith(".rels") else ""


def _element_path(root, element) -> Tuple[int, ...]:
    path = []
    current = element
    while current is not root:
        parent = current.getparent()
        if parent is None:
            return ()
        path.append(list(parent).index(current))
        current = parent
    return tuple(reversed(path))


def _at_element_path(root, path: Sequence[int]):
    current = root
    for index in path:
        children = list(current)
        if index < 0 or index >= len(children):
            return None
        current = children[index]
    return current


def _resolve_target(owner: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))


def _relationships(zf: zipfile.ZipFile) -> Tuple[Dict[str, List[Tuple[str, str, bool]]], List[str]]:
    outgoing: Dict[str, List[Tuple[str, str, bool]]] = {}
    errors: List[str] = []
    for name in zf.namelist():
        if not name.endswith(".rels"):
            continue
        try:
            root = ET.fromstring(zf.read(name))
        except Exception:
            errors.append(name + "关系无法解析")
            continue
        owner = _owner_from_rels(name)
        rows = []
        for rel in root:
            if _local(rel.tag) != "relationship":
                continue
            target = rel.get("Target", "")
            external = rel.get("TargetMode", "").lower() == "external"
            rows.append((rel.get("Type", ""), _resolve_target(owner, target), external))
        outgoing[owner] = rows
    return outgoing, errors


def _discover_related(zf: zipfile.ZipFile) -> Tuple[set, List[str], Dict[str, List[Tuple[str, str, bool]]]]:
    names = set(zf.namelist())
    outgoing, errors = _relationships(zf)
    related = {name for name in names if COMMENT_PART_RE.search(name) or name.lower().endswith("/people.xml")}
    for owner, rows in outgoing.items():
        for rel_type, target, _external in rows:
            if "comment" in rel_type.lower() or rel_type.lower().endswith("/people"):
                if target in names:
                    related.add(target)
                elif target:
                    errors.append("批注关系目标不存在：" + target)
    return related, errors, outgoing


def _text_units(paragraph) -> Tuple[str, List[Tuple[Any, ...]]]:
    text: List[str] = []
    units: List[Tuple[Any, ...]] = []
    text_index = -1
    for node in paragraph.iter():
        local = _local(node.tag)
        if local == "t":
            text_index += 1
            value = node.text or ""
            for char_index, char in enumerate(value):
                text.append(char)
                units.append(("t", text_index, char_index))
        elif local == "tab":
            text.append(" ")
            units.append(("keep", "tab"))
        elif local in ("br", "cr"):
            text.append("\n")
            units.append(("keep", "br"))
    return "".join(text), units


def _comment_elements(root) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for elem in root.iter():
        if _local(elem.tag) != "comment":
            continue
        comment_id = next((v for k, v in elem.attrib.items() if _local(k) == "id"), None)
        if comment_id is not None:
            out[str(comment_id)] = elem
    return out


def inspect_comment_package(docx_path: str) -> CommentPackageInfo:
    info = CommentPackageInfo()
    try:
        with zipfile.ZipFile(docx_path) as zf:
            related, errors, outgoing = _discover_related(zf)
            names = set(zf.namelist())
            info.related_parts = related
            info.unsupported.extend(errors)
            if len(names) != len(zf.namelist()):
                info.unsupported.append("DOCX ZIP含重复成员")
            for part in sorted(related):
                if part not in names:
                    continue
                if part.lower() not in KNOWN_COMMENT_PARTS:
                    info.unsupported.append("未知批注部件：" + part)
                    continue
                try:
                    root = ET.fromstring(zf.read(part))
                except Exception:
                    info.unsupported.append("批注部件无法解析：" + part)
                    continue
                if part == "word/comments.xml":
                    seen = set()
                    for comment in root.iter():
                        if _local(comment.tag) != "comment":
                            continue
                        comment_id = next((v for k, v in comment.attrib.items() if _local(k) == "id"), None)
                        if comment_id is None or str(comment_id) in seen:
                            info.unsupported.append("批注ID缺失或重复")
                            continue
                        seen.add(str(comment_id))
                        author_attr = next((k for k in comment.attrib if _local(k) == "author"), None)
                        author = comment.get(author_attr, "") if author_attr else ""
                        initials_attr = next((k for k in comment.attrib if _local(k) == "initials"), None)
                        initials = comment.get(initials_attr, "") if initials_attr else ""
                        if author:
                            info.identities.append(CommentIdentityRef(
                                part_name=part, value=author, field_kind="comment_author",
                                attribute_name=author_attr or "author", comment_id=str(comment_id),
                                initials=initials, location="批注#%s插入人" % comment_id))
                        paras = [e for e in comment.iter() if _local(e.tag) == "p"]
                        for pi, paragraph in enumerate(paras):
                            text, _units = _text_units(paragraph)
                            info.paragraphs.append(CommentParagraphRef(part, str(comment_id), pi, text))
                        if not paras:
                            info.paragraphs.append(CommentParagraphRef(part, str(comment_id), 0, ""))
                        for elem in comment.iter():
                            if _local(elem.tag) in {t.lower() for t in UNSUPPORTED_TAGS}:
                                info.unsupported.append("批注含不支持结构：" + _local(elem.tag))
                elif part == "word/people.xml":
                    people = [e for e in root.iter() if _local(e.tag) == "person"]
                    for ei, person in enumerate(people):
                        for attr, value in person.attrib.items():
                            if _local(attr) in IDENTITY_ATTRS and value.strip():
                                info.identities.append(CommentIdentityRef(
                                    part_name=part, value=value, field_kind="comment_person_identity",
                                    attribute_name=attr, element_index=ei,
                                    element_path=(),
                                    location="现代批注人员资料第%d项" % (ei + 1)))
                        for child in person.iter():
                            if child is person:
                                continue
                            for attr, value in child.attrib.items():
                                if _local(attr) in IDENTITY_ATTRS and value.strip():
                                    info.identities.append(CommentIdentityRef(
                                        part_name=part, value=value, field_kind="comment_person_identity",
                                        attribute_name=attr, element_index=ei,
                                        element_path=_element_path(person, child),
                                        location="现代批注人员资料第%d项" % (ei + 1)))
                if any(_local(elem.tag) in {t.lower() for t in UNSUPPORTED_TAGS}
                       for elem in root.iter()):
                    info.unsupported.append("批注部件含不支持结构：" + part)
                owner = part
                for rel_type, _target, external in outgoing.get(owner, []):
                    if external:
                        info.unsupported.append("批注部件含外部关系：" + part)
                    if "image" in rel_type.lower() or "oleobject" in rel_type.lower():
                        info.unsupported.append("批注部件含图片或嵌入对象：" + part)
    except (OSError, zipfile.BadZipFile) as exc:
        info.unsupported.append("DOCX批注包读取失败：" + str(exc))
    info.unsupported = list(dict.fromkeys(info.unsupported))
    if info.unsupported:
        info.warnings.append("检测到无法安全处理的批注结构，保留批注导出将停止")
    return info


def extract_comment_blocks(info: CommentPackageInfo) -> List[Any]:
    from contract_sensitive_detector import Block

    blocks = []
    for ref in info.paragraphs:
        blocks.append(Block(
            text=ref.text, block_type="comment",
            location="批注#%s第%d段" % (ref.comment_id, ref.paragraph_index + 1),
            part_name=ref.part_name, part_block_index=ref.paragraph_index,
            comment_id=ref.comment_id, field_kind="comment_body"))
    for ref in info.identities:
        linked_comment_initials = {}
        if ref.field_kind == "comment_person_identity" and _local(ref.attribute_name) == "author":
            linked_comment_initials = {
                comment.comment_id: comment.initials
                for comment in info.identities
                if comment.field_kind == "comment_author" and comment.value == ref.value
            }
        blocks.append(Block(
            text=ref.value, block_type="comment_author", location=ref.location,
            part_name=ref.part_name, comment_id=ref.comment_id,
            field_kind=ref.field_kind, attribute_name=ref.attribute_name,
            element_index=ref.element_index,
            element_path=ref.element_path,
            linked_comment_initials=linked_comment_initials,
            original_initials=ref.initials,
            part_block_index=ref.element_index if ref.element_index is not None else -1))
    return blocks


def _comment(root, comment_id: str):
    return _comment_elements(root).get(str(comment_id))


def _attr_key(element, name: str):
    if name in element.attrib:
        return name
    return next((key for key in element.attrib if _local(key) == _local(name)), None)


def _paragraph(comment, index: int):
    paras = [e for e in comment.iter() if _local(e.tag) == "p"]
    return paras[index] if 0 <= index < len(paras) else None


def _segments(units: Sequence[Tuple[Any, ...]], start: int, end: int) -> List[Tuple[int, int, int]]:
    segments: List[Tuple[int, int, int]] = []
    current = None
    for absolute in range(start, end):
        if absolute >= len(units):
            return []
        unit = units[absolute]
        if unit[0] != "t":
            return []
        ti, ci = unit[1], unit[2]
        if current and current[0] == ti and current[2] == ci:
            current[2] += 1
        else:
            current = [ti, ci, ci + 1]
            segments.append(current)
    return [(int(ti), int(left), int(right)) for ti, left, right in segments]


def _split_replacement(value: str, lengths: Sequence[int]) -> List[str]:
    if len(lengths) <= 1:
        return [value]
    if len(value) <= len(lengths):
        return [value[i:i + 1] if i < len(value) else "" for i in range(len(lengths))]
    total = sum(lengths) or len(lengths)
    sizes = [max(1, round(len(value) * length / total)) for length in lengths]
    while sum(sizes) > len(value):
        i = max((j for j, size in enumerate(sizes) if size > 1), key=lambda j: sizes[j], default=-1)
        if i < 0:
            break
        sizes[i] -= 1
    while sum(sizes) < len(value):
        sizes[sizes.index(min(sizes))] += 1
    out, cursor = [], 0
    for size in sizes:
        out.append(value[cursor:cursor + size])
        cursor += size
    return out


def _write_paragraph_ops(paragraph, operations: Sequence[Any],
                         return_applied_ops: bool = False):
    text, units = _text_units(paragraph)
    texts = [e for e in paragraph.iter() if _local(e.tag) == "t"]
    pieces = [list(e.text or "") for e in texts]
    applied = []
    for op in sorted(operations, key=lambda item: item.local_start, reverse=True):
        start, end = op.local_start, op.local_end
        if start < 0 or end > len(text) or end <= start or text[start:end] != op.value:
            continue
        segs = _segments(units, start, end)
        if not segs:
            continue
        replacements = _split_replacement(op.replacement, [right - left for _ti, left, right in segs])
        for (ti, left, right), replacement in zip(segs, replacements):
            pieces[ti][left:right] = list(replacement)
        applied.append(op)
    for elem, value in zip(texts, pieces):
        elem.text = "".join(value)
    return applied if return_applied_ops else len(applied)


def _serialize(root) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _structure_fingerprint(element) -> str:
    rows = []
    for node in element.iter():
        if not isinstance(node.tag, str):
            continue
        attrs = [(key, value) for key, value in node.attrib.items()
                 if _local(key) not in IDENTITY_ATTRS]
        attrs.sort()
        rows.append(node.tag + "\x1f" + repr(attrs))
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _atomic_rewrite(path: str, replacements: Dict[str, bytes], removed: Optional[set] = None,
                    validate_xml: bool = True) -> None:
    removed = removed or set()
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=directory)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as target:
            infos = source.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ValueError("DOCX ZIP含重复成员")
            written = set()
            for info in infos:
                if info.filename in removed:
                    continue
                target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
                written.add(info.filename)
            for name, data in replacements.items():
                if name not in removed and name not in written:
                    target.writestr(name, data)
        with zipfile.ZipFile(tmp) as check:
            if check.testzip() is not None:
                raise ValueError("DOCX ZIP完整性校验失败")
            for name in replacements:
                if (not validate_xml or name in removed or
                        not name.lower().endswith((".xml", ".rels"))):
                    continue
                ET.fromstring(check.read(name))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def comment_package_closure(zf: zipfile.ZipFile):
    """Return comment parts and their recursively related package parts."""
    names = set(zf.namelist())
    related, errors, outgoing = _discover_related(zf)
    parts = set(related)
    relationship_parts = set()
    queue = list(related)
    while queue:
        owner = queue.pop()
        rel_name = _relationship_name_for_part(owner)
        if rel_name in names:
            relationship_parts.add(rel_name)
        for _rel_type, target, external in outgoing.get(owner, []):
            if external:
                continue
            if target and target not in names:
                errors.append("批注关系目标不存在：" + target)
                continue
            if target in names and target not in parts:
                parts.add(target)
                queue.append(target)
    return parts, relationship_parts, errors, outgoing


def preserve_comment_package(source_path: str, docx_path: str) -> Dict[str, int]:
    """Restore the source comment package verbatim after python-docx saves the body."""
    replacements: Dict[str, bytes] = {}
    with zipfile.ZipFile(source_path) as source, zipfile.ZipFile(docx_path) as target:
        names = set(source.namelist())
        parts, relationship_parts, errors, outgoing = comment_package_closure(source)
        if errors:
            raise ValueError("无法完整枚举批注关系：" + "；".join(errors[:3]))
        target_names = set(target.namelist())
        replacements.update({name: source.read(name) for name in parts | relationship_parts})

        source_document_rels = "word/_rels/document.xml.rels"
        if source_document_rels in names:
            source_root = ET.fromstring(source.read(source_document_rels))
            if source_document_rels in target_names:
                target_root = ET.fromstring(target.read(source_document_rels))
            else:
                target_root = ET.Element(REL + "Relationships")
            source_comment_rels = []
            for rel in source_root:
                if _local(rel.tag) != "relationship":
                    continue
                resolved = _resolve_target("word/document.xml", rel.get("Target", ""))
                rel_type = rel.get("Type", "").lower()
                if resolved in parts or "comment" in rel_type or rel_type.endswith("/people"):
                    source_comment_rels.append(rel)
            source_ids = {rel.get("Id") for rel in source_comment_rels}
            for rel in list(target_root):
                if rel.get("Id") in source_ids:
                    target_root.remove(rel)
            target_root.extend(source_comment_rels)
            replacements[source_document_rels] = _serialize(target_root)

        content_types_name = "[Content_Types].xml"
        if content_types_name in names:
            source_ct = ET.fromstring(source.read(content_types_name))
            target_ct = (ET.fromstring(target.read(content_types_name))
                         if content_types_name in target_names
                         else ET.Element(CT + "Types"))
            part_names = {"/" + part for part in parts}
            extensions = {posixpath.splitext(part)[1].lstrip(".").lower()
                          for part in parts if posixpath.splitext(part)[1]}
            for item in source_ct:
                if _local(item.tag) == "override" and item.get("PartName") in part_names:
                    for old in list(target_ct):
                        if (_local(old.tag) == "override" and
                                old.get("PartName") == item.get("PartName")):
                            target_ct.remove(old)
                    target_ct.append(item)
                elif (_local(item.tag) == "default" and
                      item.get("Extension", "").lower() in extensions):
                    ext = item.get("Extension", "").lower()
                    for old in list(target_ct):
                        if (_local(old.tag) == "default" and
                                old.get("Extension", "").lower() == ext):
                            target_ct.remove(old)
                    target_ct.append(item)
            replacements[content_types_name] = _serialize(target_ct)

    _atomic_rewrite(docx_path, replacements, validate_xml=False)
    with zipfile.ZipFile(docx_path) as check:
        missing = (set(replacements) - set(check.namelist()))
        if missing:
            raise ValueError("批注部件保留校验失败")
        for name in replacements:
            if name.endswith((".xml", ".rels")):
                if name in ("word/comments.xml",) or COMMENT_PART_RE.search(name):
                    continue
                ET.fromstring(check.read(name))
    return {"parts_preserved": len(parts), "relationships_preserved": len(relationship_parts)}


def rewrite_comment_occurrences(docx_path: str, operations: Sequence[Any]) -> Dict[str, Any]:
    if not operations:
        return {"applied": 0, "skipped": 0,
                "applied_ops": [], "skipped_ops": []}
    info = inspect_comment_package(docx_path)
    if info.unsupported:
        raise ValueError("无法保留并脱敏批注：" + "；".join(info.unsupported[:3]))
    by_part: Dict[str, List[Any]] = {}
    for op in operations:
        target = op.target or {}
        part = target.get("part")
        if not part or part not in info.related_parts:
            raise ValueError("批注操作目标无效")
        by_part.setdefault(part, []).append(op)
    replacements: Dict[str, bytes] = {}
    applied = skipped = 0
    applied_operation_ids = set()
    person_updates = []
    with zipfile.ZipFile(docx_path) as zf:
        for part, ops in by_part.items():
            root = ET.fromstring(zf.read(part))
            part_applied = 0
            body_groups: Dict[Tuple[str, int], List[Any]] = {}
            for op in ops:
                target = op.target or {}
                kind = target.get("kind")
                if kind == "comment_body":
                    key = (str(target.get("comment_id", "")), int(target.get("paragraph_index", -1)))
                    body_groups.setdefault(key, []).append(op)
                elif kind == "comment_author":
                    comment = _comment(root, target.get("comment_id", ""))
                    key = _attr_key(comment, target.get("attribute", "author")) if comment is not None else None
                    if key and comment.get(key, "") == op.value:
                        target["source_text_sha256"] = hashlib.sha256(op.value.encode("utf-8")).hexdigest()
                        target["original_initials"] = next((v for k, v in comment.attrib.items()
                                                               if _local(k) == "initials"), "")
                        comment.set(key, op.replacement)
                        target["output_text_sha256"] = hashlib.sha256(op.replacement.encode("utf-8")).hexdigest()
                        initials_key = next((k for k in comment.attrib if _local(k) == "initials"), None)
                        if initials_key:
                            comment.set(initials_key, "")
                        target["output_structure_fingerprint"] = _structure_fingerprint(comment)
                        part_applied += 1
                        applied_operation_ids.add(id(op))
                        person_updates.append((op.value, op.replacement))
                elif kind == "comment_person_identity":
                    people = [e for e in root.iter() if _local(e.tag) == "person"]
                    index = int(target.get("element_index", -1))
                    person = people[index] if 0 <= index < len(people) else None
                    element = _at_element_path(person, target.get("element_path", [])) if person is not None else None
                    key = _attr_key(element, target.get("attribute", "")) if element is not None else None
                    current = element.get(key, "") if key and element is not None else ""
                    start, end = op.local_start, op.local_end
                    if (key and element is not None and 0 <= start < end <= len(current)
                            and current[start:end] == op.value):
                        target["source_text_sha256"] = hashlib.sha256(op.value.encode("utf-8")).hexdigest()
                        target["source_attribute_sha256"] = hashlib.sha256(current.encode("utf-8")).hexdigest()
                        element.set(key, current[:start] + op.replacement + current[end:])
                        target["output_text_sha256"] = hashlib.sha256(op.replacement.encode("utf-8")).hexdigest()
                        target["output_attribute_sha256"] = hashlib.sha256(element.get(key, "").encode("utf-8")).hexdigest()
                        target["output_structure_fingerprint"] = _structure_fingerprint(element)
                        part_applied += 1
                        applied_operation_ids.add(id(op))
                        person_updates.append((op.value, op.replacement))
            for (comment_id, paragraph_index), body_ops in body_groups.items():
                comment = _comment(root, comment_id)
                para = _paragraph(comment, paragraph_index) if comment is not None else None
                if para is not None:
                    before_text, _units = _text_units(para)
                    applied_body_ops = _write_paragraph_ops(
                        para, body_ops, return_applied_ops=True)
                    count = len(applied_body_ops)
                    applied_operation_ids.update(id(op) for op in applied_body_ops)
                    after_text, _units = _text_units(para)
                    if count:
                        for op in applied_body_ops:
                            op.target.update(
                                source_text_sha256=hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
                                output_text_sha256=hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
                                output_structure_fingerprint=_structure_fingerprint(para))
                    part_applied += count
            applied += part_applied
            skipped += len(ops) - part_applied
            replacements[part] = _serialize(root)
        for part in info.related_parts:
            if part not in replacements and part in zf.namelist():
                try:
                    root = ET.fromstring(zf.read(part))
                except Exception:
                    continue
            else:
                root = ET.fromstring(replacements[part]) if part in replacements else None
            if root is None:
                continue
            changed = False
            for elem in root.iter():
                for attr, value in list(elem.attrib.items()):
                    for original, replacement in person_updates:
                        if value == original:
                            elem.set(attr, replacement)
                            if _local(attr) == "author" and _local(elem.tag) == "comment":
                                initials_key = next((k for k in elem.attrib if _local(k) == "initials"), None)
                                if initials_key:
                                    elem.set(initials_key, "")
                            changed = True
                            break
            if changed:
                replacements[part] = _serialize(root)
    _atomic_rewrite(docx_path, replacements)
    skipped_ops = [op for op in operations if id(op) not in applied_operation_ids]
    return {"applied": applied, "skipped": skipped,
            "applied_ops": [op for op in operations
                            if id(op) in applied_operation_ids],
            "skipped_ops": skipped_ops}


def restore_comment_occurrences(docx_path: str, records: Sequence[Dict[str, Any]],
                               strict: bool = True) -> Dict[str, Any]:
    comments = [item for item in records if (item.get("target") or {}).get("kind", "").startswith("comment_")]
    if not comments:
        return {"applied": 0, "applied_comments": 0, "applied_comment_identities": 0,
                "missing": [], "mismatches": []}
    info = inspect_comment_package(docx_path)
    if info.unsupported:
        return {"applied": 0, "applied_comments": 0, "applied_comment_identities": 0,
                "missing": [], "mismatches": ["批注结构无法安全解析"]}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in comments:
        target = item.get("target") or {}
        part = target.get("part")
        if not part or part not in info.related_parts:
            return {"applied": 0, "applied_comments": 0, "applied_comment_identities": 0,
                    "missing": [], "mismatches": ["批注目标部件不存在"]}
        grouped.setdefault(part, []).append(item)
    replacements: Dict[str, bytes] = {}
    mismatches: List[str] = []
    applied_body = applied_identity = 0
    identity_updates = []
    with zipfile.ZipFile(docx_path) as zf:
        for part, items in grouped.items():
            root = ET.fromstring(zf.read(part))
            body_groups: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
            identity_items = []
            for item in items:
                target = item["target"]
                if target["kind"] == "comment_body":
                    key = (part, str(target.get("comment_id", "")),
                           int(target.get("paragraph_index", -1)))
                    body_groups.setdefault(key, []).append(item)
                else:
                    identity_items.append(item)
            for (_part, comment_id, pi), group in body_groups.items():
                comment = _comment(root, comment_id)
                para = _paragraph(comment, pi) if comment is not None else None
                text, units = _text_units(para) if para is not None else ("", [])
                expected_hashes = {str((row.get("target") or {}).get("output_text_sha256"))
                                   for row in group
                                   if (row.get("target") or {}).get("output_text_sha256")}
                expected_structures = {str((row.get("target") or {}).get("output_structure_fingerprint"))
                                       for row in group
                                       if (row.get("target") or {}).get("output_structure_fingerprint")}
                if (strict and (len(expected_hashes) > 1 or len(expected_structures) > 1 or
                        (expected_hashes and hashlib.sha256(text.encode("utf-8")).hexdigest() not in expected_hashes) or
                        (expected_structures and _structure_fingerprint(para) not in expected_structures))):
                    mismatches.append("批注#%s第%d段结构或输出哈希不匹配" % (comment_id, pi + 1))
                    continue
                ops = []
                by_placeholder: Dict[str, List[Dict[str, Any]]] = {}
                for item in group:
                    by_placeholder.setdefault(str(item.get("placeholder", "")), []).append(item)
                group_invalid = False
                for placeholder, placeholder_items in by_placeholder.items():
                    starts = [m.start() for m in re.finditer(re.escape(placeholder), text)]
                    if not placeholder or len(starts) != len(placeholder_items):
                        group_invalid = True
                        break
                    placeholder_items.sort(key=lambda row: (
                        (row.get("target") or {}).get("local_offset", [0])[0],
                        row.get("start", 0)))
                    for start, item in zip(starts, placeholder_items):
                        ops.append(type("RestoreOp", (), {
                            "local_start": start, "local_end": start + len(placeholder),
                            "value": placeholder, "replacement": item["original"],
                        })())
                if group_invalid:
                    mismatches.append("批注#%s第%d段占位符数量不一致" % (comment_id, pi + 1))
                    continue
                count = _write_paragraph_ops(para, ops)
                if count != len(group):
                    mismatches.append("批注#%s第%d段占位符无法完整还原" % (comment_id, pi + 1))
                else:
                    applied_body += count
            for item in identity_items:
                target = item["target"]
                if target["kind"] == "comment_author":
                    element = _comment(root, target.get("comment_id", ""))
                    key = _attr_key(element, target.get("attribute", "author")) if element is not None else None
                else:
                    people = [e for e in root.iter() if _local(e.tag) == "person"]
                    index = int(target.get("element_index", -1))
                    person = people[index] if 0 <= index < len(people) else None
                    element = _at_element_path(person, target.get("element_path", [])) if person is not None else None
                    key = _attr_key(element, target.get("attribute", "")) if element is not None else None
                expected_hash = target.get("output_text_sha256")
                expected_structure = target.get("output_structure_fingerprint")
                value = element.get(key, "") if key and element is not None else ""
                local_offset = target.get("local_offset") or [0, 0]
                start, end = int(local_offset[0]), int(local_offset[1])
                placeholder = item.get("placeholder")
                is_person_substring = target.get("kind") == "comment_person_identity"
                target_value_matches = (value[start:start + len(placeholder)] == placeholder
                                        if is_person_substring else value == placeholder)
                attribute_hash = target.get("output_attribute_sha256")
                if (not key or not target_value_matches or
                        (strict and expected_hash and hashlib.sha256(placeholder.encode("utf-8")).hexdigest() != expected_hash) or
                        (strict and attribute_hash and hashlib.sha256(value.encode("utf-8")).hexdigest() != attribute_hash) or
                        (strict and expected_structure and _structure_fingerprint(element) != expected_structure)):
                    mismatches.append("批注插入人目标与脱敏映射不匹配")
                    continue
                if is_person_substring:
                    element.set(key, value[:start] + item.get("original", "") + value[start + len(placeholder):])
                else:
                    element.set(key, item.get("original", ""))
                identity_updates.append((item.get("placeholder", ""), item.get("original", ""),
                                         target.get("linked_comment_initials", {})))
                if target["kind"] == "comment_author":
                    initials_key = next((k for k in element.attrib if _local(k) == "initials"), None)
                    if initials_key:
                        element.set(initials_key, target.get("original_initials", ""))
                applied_identity += 1
            replacements[part] = _serialize(root)
        if not mismatches:
            for part in info.related_parts:
                if part not in replacements and part in zf.namelist():
                    try:
                        root = ET.fromstring(zf.read(part))
                    except Exception:
                        continue
                else:
                    root = ET.fromstring(replacements[part]) if part in replacements else None
                if root is None:
                    continue
                changed = False
                for elem in root.iter():
                    for attr, value in list(elem.attrib.items()):
                        for placeholder, original, linked_initials in identity_updates:
                            if value == placeholder:
                                elem.set(attr, original)
                                if _local(attr) == "author" and _local(elem.tag) == "comment":
                                    comment_id = next((v for k, v in elem.attrib.items() if _local(k) == "id"), None)
                                    if comment_id is not None and str(comment_id) in linked_initials:
                                        initials_key = next((k for k in elem.attrib if _local(k) == "initials"), None)
                                        if initials_key:
                                            elem.set(initials_key, linked_initials[str(comment_id)])
                                changed = True
                                break
                if changed:
                    replacements[part] = _serialize(root)
    if mismatches:
        return {"applied": 0, "applied_comments": 0, "applied_comment_identities": 0,
                "missing": [], "mismatches": mismatches}
    _atomic_rewrite(docx_path, replacements)
    return {"applied": applied_body + applied_identity, "applied_comments": applied_body,
            "applied_comment_identities": applied_identity, "missing": [], "mismatches": []}


def _relationship_name_for_part(part: str) -> str:
    folder, leaf = posixpath.split(part)
    return posixpath.join(folder, "_rels", leaf + ".rels")


def remove_comment_package(docx_path: str) -> Dict[str, int]:
    replacements: Dict[str, bytes] = {}
    removed = set()
    with zipfile.ZipFile(docx_path) as zf:
        names = set(zf.namelist())
        related, errors, outgoing = _discover_related(zf)
        removed.update(related)
        if errors:
            raise ValueError("无法完整枚举批注关系：" + "；".join(errors[:3]))
        incoming: Dict[str, set] = {}
        for owner, rows in outgoing.items():
            for _rel_type, target, external in rows:
                if not external and target:
                    incoming.setdefault(target, set()).add(owner)
        queue = list(removed)
        while queue:
            owner = queue.pop()
            rel_name = _relationship_name_for_part(owner)
            removed.add(rel_name)
            for _rel_type, target, external in outgoing.get(owner, []):
                if (not external and target in names and target not in removed
                        and incoming.get(target, set()).issubset(removed)):
                    removed.add(target)
                    queue.append(target)
        for name in names:
            if name in ("word/document.xml",) or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
                root = ET.fromstring(zf.read(name))
                for parent in root.iter():
                    for child in list(parent):
                        if child.tag in COMMENT_MARKERS:
                            parent.remove(child)
                replacements[name] = _serialize(root)
            elif name.endswith(".rels"):
                root = ET.fromstring(zf.read(name))
                owner = _owner_from_rels(name)
                changed = False
                for rel in list(root):
                    target = _resolve_target(owner, rel.get("Target", ""))
                    if target in removed or "comment" in rel.get("Type", "").lower() or rel.get("Type", "").lower().endswith("/people"):
                        root.remove(rel)
                        changed = True
                if changed:
                    replacements[name] = _serialize(root)
            elif name == "[Content_Types].xml":
                root = ET.fromstring(zf.read(name))
                changed = False
                for child in list(root):
                    part = child.get("PartName", "").lstrip("/")
                    if part in removed or COMMENT_PART_RE.search(part) or part.lower().endswith("/people.xml"):
                        root.remove(child)
                        changed = True
                if changed:
                    replacements[name] = _serialize(root)
        count = len(related)
    _atomic_rewrite(docx_path, replacements, removed)
    with zipfile.ZipFile(docx_path) as check:
        remaining = set(check.namelist())
        if any(COMMENT_PART_RE.search(name) or name.lower().endswith("/people.xml") for name in remaining):
            raise ValueError("批注部件未能完整删除")
        for name in remaining:
            if name == "[Content_Types].xml" or name.endswith(".rels") or name in ("word/document.xml",) or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
                try:
                    root = ET.fromstring(check.read(name))
                except Exception:
                    continue
                if name.endswith(".rels") and any("comment" in e.get("Type", "").lower() or e.get("Type", "").lower().endswith("/people") for e in root):
                    raise ValueError("批注关系未能完整删除")
                if name in ("word/document.xml",) or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
                    if any(elem.tag in COMMENT_MARKERS for elem in root.iter()):
                        raise ValueError("批注锚点未能完整删除")
    return {"removed_parts": count}
