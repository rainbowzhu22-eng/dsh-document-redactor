"""Exact user terms, case numbering, and fail-closed DOCX export for external review."""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import tempfile
import zipfile
from lxml import etree as ET

import contract_sensitive_detector as D


def validate_term(raw):
    if not isinstance(raw, dict):
        raise ValueError("自选字段格式无效")
    label = str(raw.get("label", "")).strip()
    values = raw.get("values", [])
    if not 1 <= len(label) <= 12 or any(c in label for c in "[]#<>\n\r"):
        raise ValueError("字段名称须为 1–12 个字符，且不能包含括号或 #")
    if not isinstance(values, list) or not 1 <= len(values) <= 12:
        raise ValueError("每个字段须填写原文，可另加至多 11 个别名")
    clean = []
    for value in values:
        if not isinstance(value, str) or not 2 <= len(value.strip()) <= 120 or "\n" in value:
            raise ValueError("原文及别名须为 2–120 个字符，且不能跨段")
        value = value.strip()
        if D.is_placeholder(value):
            raise ValueError("不能把占位符作为自选原文")
        if value not in clean:
            clean.append(value)
    return {"id": hashlib.sha256((label + "\0" + clean[0]).encode()).hexdigest()[:16],
            "label": label, "values": clean}


def _custom_type(label):
    for tid, meta in D.TYPE_META.items():
        if meta.label == label:
            return tid
    return "USER_" + hashlib.sha256(label.encode()).hexdigest()[:12].upper()


COURT_REGION = r"[\u4e00-\u9fff]{2,8}(?:特别行政区|自治区|自治州|地区|省|市|盟|区|县)"
COURT_ORDINARY = rf"{COURT_REGION}(?:第[一二三四五六七八九十\d]+)?(?:高级|中级|基层)?人民法院"
COURT_SPECIAL = rf"{COURT_REGION}(?:知识产权|互联网|海事|铁路运输)法院"
COURT_START_BOUNDARY = r"(?:^|(?<=[,.;，。；：、\s])|(?<=所在地)|(?<=住所地)|(?<=管辖地)|(?<=向)|(?<=由)|(?<=位于)|(?<=为)"
COURT_PATTERN = rf"{COURT_START_BOUNDARY})(最高人民法院|中国人民解放军军事法院|{COURT_ORDINARY}|{COURT_SPECIAL}|{COURT_REGION}军事法院)"

# ---- 英语 / 境外案号、法院与仲裁机构（英语争议解决字段）----
# 专名法院走 COURT_NAME；可泛指词（High Court 等）只走标题行或 Court: 标签；
# 仲裁机构不是法院，走 ORG_NAME。不把英文编号加成 CASE_NO 再交给中文分类器。
EN_CASE_NO_SPECS = (
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?<![A-Za-z0-9])(?:\d{1,3}:\d{2}|\d{2})-(?:cv|cr|bk|mj|ad)-\d{4,7}(?:-[A-Za-z]{2,5})?(?![0-9A-Za-z])",
        0.88, source="统一规则", label="诉讼案号候选",
        anti_keywords=("version", "revision", "http", "www"),
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?i)\b(?:case|docket|civil\s+action|index|cause|claim|arbitration)\s*"
        r"(?:no\.?|number)\s*[:：]?\s*"
        r"(?P<no>(?=[A-Z0-9]*[0-9])[A-Z0-9][A-Z0-9:/\-]{3,39})",
        0.86, group=1, source="统一规则", label="诉讼案号候选",
        anti_keywords=("purchase", "invoice", "room", "version"),
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?i)\bclaim\s*(?:no\.?|number)\s*[:：]?\s*"
        r"([A-Z]{1,4}\d{2}[A-Z]\d{5}|[A-Z]{2}-\d{4}-\d{6})",
        0.90, group=1, source="统一规则", label="诉讼案号候选",
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?<![A-Za-z0-9])(?:HCA|HCMP|HCCT|HCLA|HCVI|CACV|FACV|DCCJ|HCB|HCCW|HCAL|HCPI|HCMA)"
        r"\s+\d{1,6}/(?:19|20)\d{2}(?![0-9])",
        0.90, source="统一规则", label="诉讼案号候选",
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?<!\w)\[(?:19|20)\d{2}\]\s+"
        r"(?:EWHC|EWCA(?:\s+(?:Civ|Crim))?|UKSC|UKHL|HKCFI|HKCA|HKCFA|HCAL|SGCA|SGHC|SICC|FACV)"
        r"\s+\d{1,6}(?:\s*\([A-Za-z]{2,12}\))?",
        0.90, source="统一规则", label="诉讼案号候选",
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?i)\bSIAC\s+(?:arb(?:itration)?\s*)?no\.?\s*[:：]?\s*\d{1,6}\s+of\s+(?:19|20)\d{2}",
        0.90, source="统一规则", label="诉讼案号候选",
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?i)\bHKIAC(?:\s+administered\s+arbitration)?\s*(?:no\.?|number|/)[\s:：]*([A-Z]?\d{3,8})",
        0.86, group=1, source="统一规则", label="诉讼案号候选",
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?i)\b(?:LCIA|ICC)\s+case\s+no\.?\s*[:：]?\s*([A-Z0-9][A-Z0-9/\-]{2,20})",
        0.88, group=1, source="统一规则", label="诉讼案号候选",
    ),
    D.RegexSpec(
        "CASE_NUMBER",
        r"(?i)\bICSID\s+(?:case\s+)?(?:no\.?|number)?\s*[:：]?\s*(ARB/(?:\d{2}|(?:19|20)\d{2})/\d{1,4})",
        0.92, group=1, source="统一规则", label="诉讼案号候选",
    ),
    D.RegexSpec(
        "NUMBER_CANDIDATE",
        r"(?i)\b(?:award|ruling|procedural\s+order)\s+no\.?\s*[:：]?\s*\d{1,4}\b",
        0.50, source="统一规则", label="编号候选，类别待核对",
    ),
    D.RegexSpec(
        "NUMBER_CANDIDATE",
        r"(?i)\b(?:matter\s+no\.?|our\s+ref\.?|your\s+ref\.?|file\s+(?:no\.?|number)|client\s+ref\.?)"
        r"\s*[:：]?\s*([A-Z0-9][A-Z0-9/\-]{2,24})",
        0.50, group=1, source="统一规则", label="编号候选，类别待核对",
    ),
)

# 专名法院全称；High Court / Court of Appeal 等可泛指词不在此列。
EN_COURT_PATTERN = (
    r"(?i)(?<![A-Za-z])("
    r"united\s+states\s+district\s+court\s+for\s+the\s+[a-z\s]{3,60}|"
    r"united\s+states\s+court\s+of\s+appeals\s+for\s+the\s+[a-z\s]{3,60}|"
    r"supreme\s+court\s+of\s+the\s+united\s+states|"
    r"supreme\s+court\s+of\s+the\s+united\s+kingdom|"
    r"high\s+court\s+of\s+justice"
    r"(?:\s*,\s*(?:king's|queen's)\s+bench\s+division)?|"
    r"court\s+of\s+final\s+appeal(?:\s+of\s+hong\s+kong)?|"
    r"hong\s+kong\s+high\s+court|"
    r"hong\s+kong\s+court\s+of\s+appeal|"
    r"singapore\s+high\s+court|"
    r"singapore\s+international\s+commercial\s+court"
    r")(?!['a-z])(?!\s*(?:street|road|avenue|ave\.?|st\.?|rd\.?|lane|ln\.?|drive|dr\.?))"
    r"(?!\s*(?:held|has|is|did|will|should|would|may|might|sits|ordered|found|noted)\b)"
)

# 仲裁机构不是法院，走 ORG_NAME。
EN_ARB_ORG_PATTERN = (
    r"(?i)(?<![A-Za-z])("
    r"hong\s+kong\s+international\s+arbitration\s+centre|"
    r"singapore\s+international\s+arbitration\s+centre|"
    r"london\s+court\s+of\s+international\s+arbitration|"
    r"icc\s+international\s+court\s+of\s+arbitration|"
    r"international\s+court\s+of\s+arbitration(?:\s+of\s+the\s+icc)?|"
    r"china\s+international\s+economic\s+and\s+trade\s+arbitration\s+commission|"
    r"beijing\s+arbitration\s+commission|"
    r"shenzhen\s+court\s+of\s+international\s+arbitration|"
    r"shanghai\s+international\s+arbitration\s+center|"
    r"china\s+maritime\s+arbitration\s+commission|"
    r"international\s+centre\s+for\s+settlement\s+of\s+investment\s+disputes|"
    r"arbitration\s+institute\s+of\s+the\s+stockholm\s+chamber\s+of\s+commerce"
    r")(?!['a-z])(?!\s*(?:street|road|avenue))"
)

# High Court / Court of Appeal 等可泛指词：仅标题行 IN THE ... COURT。
EN_COURT_HEADING_PATTERN = (
    r"(?im)^\s*(?:IN\s+THE\s+"
    r"(?:High\s+Court(?:\s+of\s+Justice)?|Court\s+of\s+Appeal|"
    r"Commercial\s+Court|Supreme\s+Court|"
    r"United\s+States\s+District\s+Court|"
    r"Court\s+of\s+First\s+Instance)"
    r"[^\n]{0,60})\s*$"
)

# Court: / Court Name: / Tribunal: 标签值 → COURT_NAME。
EN_COURT_LABEL_PATTERN = (
    r"(?i)\b(?:court\s+name|court|tribunal)\s*[:：=]\s*"
    r"(?!presiding\b|sole\b|co-?arbitrators?\b|arbitrators?\b|umpire\b)"
    r"(?![A-Za-z'’&.\- ]{0,80}\b(?:arbitration|arbitral)\b)"
    r"([A-Z][A-Za-z'’&.\- ]{3,80})"
    r"(?=\s*$|[;,\n])"
)


def litigation_type_meta():
    return {
        "CASE_NUMBER": D.TypeMeta("CASE_NUMBER", "案号", "诉讼信息", "high", priority=80),
        "COURT_NAME": D.TypeMeta("COURT_NAME", "法院名称", "诉讼信息", "high", priority=80),
    }


def litigation_specs():
    """Additional suggestions for litigation review; each remains user-selectable."""
    return [
        D.RegexSpec("CASE_NUMBER", r"[（(]\d{4}[）)][\u4e00-\u9fff]{1,8}\d{0,8}(?:民|刑|行|执|知|破|赔|特|申)[\u4e00-\u9fff]{0,4}\d{1,9}号", 0.94,
                    source="诉讼规则", label="案号"),
        D.RegexSpec("COURT_NAME", COURT_PATTERN, 0.84, group=1,
                    source="诉讼规则", label="法院名称"),
        *EN_CASE_NO_SPECS,
        D.RegexSpec("COURT_NAME", EN_COURT_PATTERN, 0.84,
                    source="诉讼规则", label="法院名称"),
        D.RegexSpec("COURT_NAME", EN_COURT_HEADING_PATTERN, 0.80,
                    source="诉讼规则", label="法院名称"),
        D.RegexSpec("COURT_NAME", EN_COURT_LABEL_PATTERN, 0.80, group=1,
                    source="诉讼规则", label="法院名称"),
        D.RegexSpec("ORG_NAME", EN_ARB_ORG_PATTERN, 0.82,
                    source="诉讼规则", label="组织机构名称"),
    ]


_CASE_NUMBER_PATTERN = re.compile(
    r"[（(](?:19|20)\d{2}[）)][\u4e00-\u9fff]{1,8}\d{0,8}"
    r"(?:民|刑|行|执|知|破|赔|特|申)[\u4e00-\u9fff]{0,4}\d{1,9}号")
_ARBITRATION_CASE_NUMBER_PATTERN = re.compile(
    r"[（(](?:19|20)\d{2}[）)][\u4e00-\u9fff]{0,10}?"
    r"(?:仲裁|贸仲)[\u4e00-\u9fff]{0,8}?(?:字第|第)\d{1,6}号"
    r"|(?:深国仲|北仲|京仲|沪仲|贸仲|中国贸仲|"
    r"中国国际经济贸易仲裁委员会|北京仲裁委员会|上海仲裁委员会|"
    r"深圳国际仲裁院|[\u4e00-\u9fff]{2,8}仲裁委员会|[\u4e00-\u9fff]{2,8}仲裁院)"
    r"[\u4e00-\u9fff]{0,4}[〔\[]\s*(?:19|20)\d{2}\s*[〕\]]"
    r"\s*[\u4e00-\u9fff]{0,6}?(?:受|字)?第?\d{1,6}号")
_CASE_NUMBER_PATTERN = re.compile(
    _CASE_NUMBER_PATTERN.pattern + r"|" + _ARBITRATION_CASE_NUMBER_PATTERN.pattern)
_ORG_DOCUMENT_NUMBER_PATTERN = re.compile(
    r"[\u4e00-\u9fff]{2,14}(?:〔|\[)(?:19|20)\d{2}(?:〕|\])\d{1,6}号")


def unified_type_meta():
    return {
        **litigation_type_meta(),
        "CASE_NUMBER": D.TypeMeta("CASE_NUMBER", "诉讼案号候选", "诉讼信息", "high", priority=80),
        "COURT_NAME": D.TypeMeta("COURT_NAME", "法院名称", "诉讼信息", "high", priority=80),
        "ORG_DOCUMENT_NO": D.TypeMeta("ORG_DOCUMENT_NO", "公文/机构编号候选", "编号信息", "medium", priority=70),
        "NUMBER_CANDIDATE": D.TypeMeta("NUMBER_CANDIDATE", "编号候选，类别待核对", "编号信息", "low", priority=60),
    }


def configure_unified_settings(settings):
    """Enable the same optional legal candidates for every supported document format."""
    settings.profile = "unified"
    settings.extra_specs.extend([
        D.RegexSpec(
            "CASE_NUMBER",
            r"[（(](?:19|20)\d{2}[）)][\u4e00-\u9fff]{1,8}\d{0,8}"
            r"(?:民|刑|行|执|知|破|赔|特|申)[\u4e00-\u9fff]{0,4}\d{1,9}号",
            0.94, source="统一规则", label="诉讼案号候选"),
        D.RegexSpec("CASE_NUMBER", _ARBITRATION_CASE_NUMBER_PATTERN.pattern, 0.94,
                    source="统一规则", label="诉讼案号候选"),
        D.RegexSpec(
            "ORG_DOCUMENT_NO",
            r"[\u4e00-\u9fff]{2,14}(?:〔\s*(?:19|20)\d{2}\s*〕|"
            r"\[\s*(?:19|20)\d{2}\s*\])\s*\d{1,6}号",
            0.86, source="统一规则", label="公文/机构编号候选"),
        D.RegexSpec("COURT_NAME", COURT_PATTERN, 0.84, group=1,
                    source="统一规则", label="法院名称"),
        *EN_CASE_NO_SPECS,
        D.RegexSpec("COURT_NAME", EN_COURT_PATTERN, 0.84,
                    source="统一规则", label="法院名称"),
        D.RegexSpec("COURT_NAME", EN_COURT_HEADING_PATTERN, 0.80,
                    source="统一规则", label="法院名称"),
        D.RegexSpec("COURT_NAME", EN_COURT_LABEL_PATTERN, 0.80, group=1,
                    source="统一规则", label="法院名称"),
        D.RegexSpec("ORG_NAME", EN_ARB_ORG_PATTERN, 0.82,
                    source="统一规则", label="组织机构名称"),
    ])
    settings.extra_type_meta.update(unified_type_meta())
    return settings


def classify_case_number(value):
    """Classify an existing CASE_NO hit by shape, never by assumed source."""
    compact = re.sub(r"\s+", "", str(value or ""))
    case_match = bool(_CASE_NUMBER_PATTERN.fullmatch(compact))
    org_match = bool(_ORG_DOCUMENT_NUMBER_PATTERN.fullmatch(compact))
    if case_match and not org_match:
        return "CASE_NUMBER"
    if org_match and not case_match:
        return "ORG_DOCUMENT_NO"
    return "NUMBER_CANDIDATE"


def classify_unified_candidates(candidates):
    """Relabel generic CASE_NO hits while preserving exact occurrence locations."""
    metadata = unified_type_meta()
    for candidate in candidates:
        if candidate.type_id != "CASE_NO":
            continue
        type_id = classify_case_number(candidate.value)
        meta = metadata[type_id]
        candidate.type_id = type_id
        candidate.label = meta.label
        candidate.category = meta.category
        candidate.severity = meta.severity
        candidate.reasons.append("分类仅依据编号格式，不代表已确认其来源")
    return candidates


def add_exact_terms(doc, candidates, terms, type_meta=None):
    """User literals take precedence over automatic guesses at the same position."""
    full = "\n".join(b.text.rstrip("\n") for b in doc.blocks)
    spans = []
    pos = 0
    for block in doc.blocks:
        value = block.text.rstrip("\n")
        spans.append((pos, value, block))
        pos += len(value) + 1
    occupied = []
    manual = {}
    for term in sorted(terms, key=lambda t: max((len(v) for v in t.get("values", [])), default=0), reverse=True):
        item = validate_term(term)
        label, canonical = item["label"], item["values"][0]
        tid = _custom_type(label)
        if type_meta:
            for known_id, meta in type_meta.items():
                if meta.label == label:
                    tid = known_id
                    break
        candidate = D.Candidate(0, tid, label, "用户自选", "high", canonical,
                                "[已脱敏]", 0.99, ["来源：用户自选", "用户指定的原文或别名"])
        for bi, (start, block_text, block) in enumerate(spans):
            for literal in sorted(item["values"], key=len, reverse=True):
                for match in re.finditer(re.escape(literal), block_text, re.I if literal.isascii() else 0):
                    ls, le = match.span()
                    if literal.isascii() and ((ls and block_text[ls - 1].isalnum()) or
                                              (le < len(block_text) and block_text[le].isalnum())):
                        continue
                    s, e = start + ls, start + le
                    if any(s < oe and os_ < e for os_, oe in occupied):
                        continue
                    occupied.append((s, e))
                    candidate.occurrences.append(D.Occurrence(
                        page=block.page, location=block.location, block_type=block.block_type,
                        start=s, end=e, context=full[max(0, s - 25):min(len(full), e + 25)],
                        text=full[s:e], block_index=bi, local_start=ls, local_end=le,
                        bbox=block.bbox_for_range(ls, le)))
        if candidate.occurrences:
            manual[(tid, canonical)] = candidate
    kept = []
    for candidate in candidates:
        candidate.occurrences = [o for o in candidate.occurrences
                                 if not any(o.start < e and s < o.end for s, e in occupied)]
        if candidate.occurrences:
            kept.append(candidate)
    result = list(manual.values()) + kept
    for i, candidate in enumerate(result, 1):
        candidate.cid = i
    return result


def assign_case_numbers(report, stored):
    """Keep the same exact entity number across documents in one named case."""
    numbers = {}
    for row in stored:
        stored_type = row["type"]
        if stored_type == "CASE_NO":
            stored_type = classify_case_number(row.get("value", ""))
        numbers[(stored_type, row["value"])] = row["number"]
    next_by_type = {}
    for (tid, _), n in numbers.items():
        next_by_type[tid] = max(next_by_type.get(tid, 0), n)
    local = D.build_entity_number_map(report["items"])
    groups = {}
    for item in report["items"]:
        tid, value = item["type"], item["value"]
        local_n = local[(tid, D._norm_key(value))]
        group = groups.setdefault((tid, local_n), [])
        group.append(item)
    ordered = sorted(groups.items(), key=lambda pair: min(
        o["offset"][0] for item in pair[1] for o in item["occurrences"]))
    for (tid, _local_n), items in ordered:
        existing = [numbers[(tid, it["value"])] for it in items if (tid, it["value"]) in numbers]
        if not existing and tid == "ORG_NAME":
            related = {n for (stored_type, stored_value), n in numbers.items()
                       if stored_type == tid and any(D._org_same_entity(stored_value, it["value"]) for it in items)}
            if len(related) == 1:
                existing = list(related)
        if existing:
            number = existing[0]
        else:
            next_by_type[tid] = next_by_type.get(tid, 0) + 1
            number = next_by_type[tid]
        for item in items:
            numbers[(tid, item["value"])] = number
    rows = [{"type": t, "value": v, "number": n} for (t, v), n in numbers.items()]
    return rows, numbers


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_V = "{urn:schemas-microsoft-com:vml}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_COMMENT_TAGS = {_W + name for name in ("commentReference", "commentRangeStart", "commentRangeEnd")}
_BAD_TAGS = {"ins", "del", "delText", "moveFrom", "moveTo", "moveFromRangeStart", "moveToRangeStart",
             "vanish", "webHidden", "specVanish",
             "footnoteReference", "endnoteReference", "altChunk", "object", "drawing", "pict",
             "fldSimple", "sdt", "hyperlink", "customXml", "smartTag",
             "bookmarkStart", "bookmarkEnd"}
_ALLOWED_WORD = re.compile(r"word/(?:document|header\d+|footer\d+|styles|numbering|settings|fontTable|webSettings)\.xml$|word/theme/theme\d+\.xml$")
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def _relationship_part(owner):
    folder, leaf = posixpath.split(owner)
    return posixpath.join(folder, "_rels", leaf + ".rels")


def _resolve_part(owner, target):
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))
    if resolved == ".." or resolved.startswith("../") or "\\" in target:
        raise ValueError("DOCX关系目标路径无效")
    return resolved


def _relationship_owner(rel_name):
    if rel_name == "_rels/.rels":
        return ""
    match = re.fullmatch(r"(.*?)/_rels/([^/]+)\.rels", rel_name)
    if not match:
        raise ValueError("DOCX关系部件名称无效")
    return posixpath.join(match.group(1), match.group(2))


def _preserved_part_closure(source, names, start_parts):
    parts = set(start_parts)
    relationship_parts = set()
    pending = list(start_parts)
    while pending:
        owner = pending.pop()
        rel_name = _relationship_part(owner)
        if rel_name not in names:
            continue
        relationship_parts.add(rel_name)
        root = ET.fromstring(source.read(rel_name))
        for rel in root:
            if rel.tag != _REL_NS + "Relationship":
                continue
            if rel.get("TargetMode", "").lower() == "external":
                continue
            target = _resolve_part(owner, rel.get("Target", ""))
            if target not in names:
                raise ValueError("未支持修订关联部件缺失")
            if target not in parts:
                parts.add(target)
                pending.append(target)
    return parts, relationship_parts


def _unsupported_relationship_closure(source, names, manifest, roots,
                                     extra_roots_by_part=None):
    dependency_parts = set()
    relationship_parts = set()
    for part, root in roots.items():
        unsupported_roots = _manifest_nodes(root, part, manifest)
        unsupported_roots.update((extra_roots_by_part or {}).get(part, ()))
        referenced_ids = {
            value
            for node in unsupported_roots
            for element in node.iter()
            for attr, value in element.attrib.items()
            if attr.startswith(_R)
        }
        if not referenced_ids:
            continue
        rel_name = _relationship_part(part)
        if rel_name not in names:
            raise ValueError("未支持修订引用的关系部件缺失")
        rel_root = ET.fromstring(source.read(rel_name))
        relations = {rel.get("Id"): rel for rel in rel_root
                     if rel.tag == _REL + "Relationship"}
        relationship_parts.add(rel_name)
        for rid in referenced_ids:
            rel = relations.get(rid)
            if rel is None:
                raise ValueError("未支持修订引用的关系不存在")
            if rel.get("TargetMode", "").lower() == "external":
                continue
            target = _resolve_part(part, rel.get("Target", ""))
            if target not in names:
                raise ValueError("未支持修订引用的部件缺失")
            dependency_parts.add(target)

    transitive_parts, transitive_relationships = _preserved_part_closure(
        source, names, dependency_parts) if dependency_parts else (set(), set())
    return (dependency_parts | transitive_parts,
            relationship_parts | transitive_relationships)


def _manifest_nodes(root, part, manifest):
    expected = {(row["kind"], int(row["ordinal"]))
                for row in manifest if row.get("part") == part}
    counts = {}
    found = set()
    nodes = set()
    for node in root.iter():
        if not isinstance(node.tag, str) or not node.tag.startswith(_W):
            continue
        local = node.tag[len(_W):]
        ordinal = counts.get(local, 0)
        counts[local] = ordinal + 1
        if (local, ordinal) in expected:
            found.add((local, ordinal))
            nodes.add(node)
    if found != expected:
        raise ValueError("未支持修订定位发生变化，已停止导出")
    return nodes


def _manifest_object_roots(root, part, manifest):
    object_rows = [row for row in manifest
                   if row.get("part") == part and
                   str(row.get("reason") or "").startswith("兼容文本框")]
    anchors = _manifest_nodes(root, part, object_rows) if object_rows else set()
    return {
        next((ancestor for ancestor in anchor.iterancestors()
              if ancestor.tag == _MC + "AlternateContent"), anchor)
        for anchor in anchors
    }


def _inside_nodes(element, roots):
    current = element
    while current is not None:
        if current in roots:
            return True
        current = current.getparent()
    return False


def _story_image_parts(source, names, stories, unsupported_manifest=()):
    image_parts = set()
    relationship_parts = set()
    for part, root in stories:
        unsupported_roots = _manifest_nodes(root, part, unsupported_manifest)
        unsupported_roots.update(
            _manifest_object_roots(root, part, unsupported_manifest))
        rel_name = _relationship_part(part)
        if rel_name not in names:
            relations = {}
            external_ids = set()
        else:
            rel_root = ET.fromstring(source.read(rel_name))
            relations = {}
            external_ids = set()
            for rel in rel_root:
                if rel.tag != _REL + "Relationship" or not rel.get("Type", "").endswith("/image"):
                    continue
                rid = rel.get("Id", "")
                if not rid:
                    raise ValueError("DOCX图片关系无效")
                if rel.get("TargetMode", "").lower() == "external":
                    external_ids.add(rid)
                    continue
                target = _resolve_part(part, rel.get("Target", ""))
                if target not in names or not target.startswith("word/media/"):
                    raise ValueError("DOCX图片目标缺失或不在媒体目录")
                relations[rid] = target

        used_ids = set()
        for node in root.iter():
            if _inside_nodes(node, unsupported_roots):
                continue
            if not isinstance(node.tag, str):
                continue
            if node.tag == _A + "blip":
                if node.get(_R + "link"):
                    continue
                rid = node.get(_R + "embed", "")
                if rid in external_ids:
                    continue
                if not rid or rid not in relations:
                    raise ValueError("DOCX图片未关联可保留的内嵌图片")
                used_ids.add(rid)
            elif node.tag == _V + "imagedata":
                rid = node.get(_R + "id", "")
                if rid in external_ids:
                    continue
                if not rid or rid not in relations:
                    raise ValueError("DOCX图片未关联可保留的内嵌图片")
                used_ids.add(rid)

        for drawable in list(root.iter(_W + "drawing")) + list(root.iter(_W + "pict")):
            if _inside_nodes(drawable, unsupported_roots):
                continue
            references = [node for node in drawable.iter()
                          if node.tag in {_A + "blip", _V + "imagedata"}]
            has_textbox = any(node.tag == _W + "txbxContent" for node in drawable.iter())
            if has_textbox:
                if references:
                    raise ValueError("DOCX绘图对象同时包含文本框和图片，无法安全保留")
                continue
            if not references:
                if drawable.tag == _W + "pict" and _is_plain_vml_watermark(drawable):
                    continue
                raise ValueError("DOCX含无法安全保留的复杂绘图对象")
            if drawable.tag == _W + "drawing":
                graphic_uris = [node.get("uri", "") for node in drawable.iter()
                                if isinstance(node.tag, str) and
                                node.tag.rsplit("}", 1)[-1] == "graphicData"]
                if not graphic_uris or any(not uri.endswith("/picture") for uri in graphic_uris):
                    raise ValueError("DOCX含无法安全保留的非图片绘图对象")
            elif any(isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "textbox"
                     for node in drawable.iter()):
                raise ValueError("DOCX图片对象包含未支持的文本框")

        if used_ids:
            relationship_parts.add(rel_name)
            image_parts.update(relations[rid] for rid in used_ids)
    return image_parts, relationship_parts


def _is_plain_vml_watermark(drawable):
    text_paths = [node for node in drawable.iter(_V + "textpath")
                  if node.get("string")]
    if len(text_paths) != 1:
        return False
    try:
        from contract_docx_revisions import _vml_textpath_shape
        return _vml_textpath_shape(text_paths[0]) is not None
    except Exception:
        return False


def _ooxml_toggle_value(element):
    value = element.get(_W + "val")
    if value is None:
        return True
    value = value.lower()
    if value in {"0", "false", "off"}:
        return False
    if value in {"1", "true", "on"}:
        return True
    raise ValueError("隐藏属性开关值无效")


def _ordinary_format_node(node):
    # Only formatting vocabulary is accepted; an arbitrary Word node under rPr
    # is not evidence that its content is ordinary formatting.
    formats = {
        "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps",
        "strike", "dstrike", "outline", "shadow", "emboss", "imprint",
        "noProof", "snapToGrid", "vanish", "webHidden", "specVanish", "color",
        "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u",
        "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em",
        "lang", "eastAsianLayout", "oMath", "pStyle", "keepNext", "keepLines",
        "pageBreakBefore", "framePr", "widowControl", "numPr", "ilvl", "numId",
        "suppressLineNumbers", "pBdr", "top", "left", "bottom", "right",
        "between", "bar", "tabs", "tab", "suppressAutoHyphens", "kinsoku",
        "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN",
        "bidi", "adjustRightInd", "ind", "contextualSpacing", "mirrorIndents",
        "suppressOverlap", "jc", "textDirection", "textAlignment",
        "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
    }
    return (isinstance(node.tag, str) and node.tag.startswith(_W) and
            node.tag[len(_W):] in formats and
            any(ancestor.tag in {_W + "rPr", _W + "pPr"} for ancestor in node.iterancestors()) and
            not (node.text or "").strip() and
            not any(key.startswith(_R) for key in node.attrib))


def _simple_content_controls(root, ignored_controls=()):
    safe = set()
    ignored = set(ignored_controls)
    allowed_properties = {
        _W + "alias", _W + "tag", _W + "id", _W + "lock", _W + "text",
    }
    allowed_content_tags = {
        _W + name for name in (
            "sdtContent", "p", "pPr", "r", "rPr", "t", "tab", "br", "cr",
            "proofErr", "bookmarkStart", "bookmarkEnd",
        )
    }
    for control in root.iter(_W + "sdt"):
        if control in ignored:
            continue
        properties = control.find(_W + "sdtPr")
        content = control.find(_W + "sdtContent")
        if (properties is None or content is None or
                any(child.tag not in {_W + "sdtPr", _W + "sdtContent", _W + "sdtEndPr"}
                    for child in control) or
                any(child.tag not in allowed_properties for child in properties) or
                any(node.tag not in allowed_content_tags and not _ordinary_format_node(node)
                    for node in content.iter()) or
                any(node.tag == _W + "sdt" for node in content.iter()) or
                any(key.startswith(_R) for node in content.iter()
                    for key in node.attrib)):
            continue
        safe.add(control)
    return safe


def classify_content_controls(part, root):
    """逐控件给出兼容结论，不能因另一个问题控件抹去安全控件分类。"""
    footer_nodes = _safe_page_footer_nodes(root) if re.fullmatch(r"word/footer\d+\.xml", part) else set()
    safe_page = {node for node in root.iter(_W + "sdt") if node in footer_nodes}
    safe_text = _simple_content_controls(root, ignored_controls=safe_page)
    rows = []
    for index, node in enumerate(root.iter(_W + "sdt")):
        kind = "safe_page" if node in safe_page else "safe_text" if node in safe_text else "unsupported"
        rows.append({"ordinal": index, "classification": kind,
                     "reason": "安全页码或普通文字控件" if kind != "unsupported" else "绑定、关系或未支持结构需处理"})
    return rows


def _content_control_structure_digest(root, additional_controls=()):
    additional = set(additional_controls)
    controls = _simple_content_controls(root, ignored_controls=additional)
    if not controls and not additional:
        return hashlib.sha256(b"[]").hexdigest()
    records = []
    for control in root.iter(_W + "sdt"):
        if control not in controls and control not in additional:
            return ""
        clone = ET.fromstring(ET.tostring(control))
        for node in clone.iter():
            if node.tag in {_W + "t", _W + "delText"}:
                node.text = None
        records.append(ET.tostring(clone, method="c14n", exclusive=True,
                                   with_comments=False).decode("utf-8"))
    return hashlib.sha256("\n".join(records).encode("utf-8")).hexdigest()


def _safe_page_footer_nodes(root):
    from contract_docx_revisions import _field_instruction_inventory
    safe_nodes = set()
    allowed = {_W + name for name in (
        "sdt", "sdtPr", "sdtContent", "sdtEndPr", "alias", "tag", "id", "lock", "text",
        "docPartObj", "docPartGallery", "docPartUnique", "p", "pPr", "r", "rPr", "t", "tab", "br", "cr",
        "proofErr", "bookmarkStart", "bookmarkEnd", "fldChar", "instrText", "fldSimple")}
    for control in root.iter(_W + "sdt"):
        if control.find(_W + "sdtContent") is None:
            continue
        if any(node.tag not in allowed and not _ordinary_format_node(node) for node in control.iter()):
            continue
        if any(key.startswith(_R) for node in control.iter() for key in node.attrib):
            continue
        fields = _field_instruction_inventory("footer", control)
        if not fields or any(not row["safe"] or row["field_type"] not in
                             {"PAGE", "NUMPAGES", "SECTIONPAGES"} for row in fields):
            continue
        safe_nodes.update(control.iter())
    return safe_nodes



def _prepare_residual_values(originals):
    unique_originals = tuple(dict.fromkeys(
        value for value in originals if isinstance(value, str) and value))
    attribute_originals = tuple(
        value for value in unique_originals
        if not (value.isdigit() and len(value) < 6))
    return unique_originals, attribute_originals


def _compile_residual_pattern(values):
    if not values:
        return None
    return re.compile("|".join(re.escape(value) for value in values))


def _is_residual_visible_text_node(element):
    # DOCX revision targets are verified by their physical XML paths and output hashes.
    # Searching their split replacement fragments globally causes unrelated text and
    # structural values to look like unredacted matches.
    return element.tag == _A + "t"


def _external_relationship_manifest(source, names):
    records = []
    for rel_name in sorted(name for name in names if name.lower().endswith(".rels")):
        try:
            root = ET.fromstring(source.read(rel_name))
        except Exception as exc:
            raise ValueError("DOCX关系部件无法安全检查") from exc
        if root.tag != _REL + "Relationships":
            raise ValueError("DOCX关系部件结构无效")
        relationship_ids = set()
        for rel in root:
            if rel.tag != _REL + "Relationship":
                raise ValueError("DOCX关系部件含无效节点")
            relationship_id = rel.get("Id", "")
            target = rel.get("Target", "")
            rel_type = rel.get("Type", "")
            if (not relationship_id or relationship_id in relationship_ids or
                    not target or not rel_type):
                raise ValueError("DOCX关系部件含不完整或重复关系")
            relationship_ids.add(relationship_id)
            if rel.get("TargetMode", "").lower() != "external":
                continue
            records.append({
                "part": rel_name,
                "id": relationship_id,
                "kind": rel_type.rsplit("/", 1)[-1],
                "target_sha256": hashlib.sha256(
                    target.encode("utf-8")).hexdigest(),
            })
    records.sort(key=lambda row: (
        row["part"], row["kind"], row["id"], row["target_sha256"]))
    digest = hashlib.sha256(json.dumps(
        records, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return records, digest


def _remove_relationship_references(root, relationship_id):
    removed = 0

    def keep_tail(parent, index, tail, last_child=None):
        if not tail:
            return
        if last_child is not None:
            last_child.tail = (last_child.tail or "") + tail
        elif index:
            previous = parent[index - 1]
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail

    for parent in list(root.iter()):
        for child in list(parent):
            if not any(key.startswith(_R) and key.rsplit("}", 1)[-1] in {
                    "id", "link", "embed"} and value == relationship_id
                    for key, value in child.attrib.items()):
                continue
            index = parent.index(child)
            tail = child.tail
            if child.tag == _W + "hyperlink":
                children = list(child)
                parent.remove(child)
                for offset, nested in enumerate(children):
                    parent.insert(index + offset, nested)
                keep_tail(parent, index, tail, children[-1] if children else None)
            else:
                parent.remove(child)
                keep_tail(parent, index, tail)
            removed += 1
    if any(key.startswith(_R) and key.rsplit("}", 1)[-1] in {
            "id", "link", "embed"} and value == relationship_id
            for node in root.iter() for key, value in node.attrib.items()):
        raise ValueError("移除外部关系后仍存在悬空引用")
    return removed


def restore_unscanned_parts(source_path, output_path, unscanned_parts):
    """Restore unreadable noncritical members byte-for-byte after python-docx saves."""
    rows = [row for row in (unscanned_parts or [])
            if isinstance(row, dict) and isinstance(row.get("part"), str)]
    if not rows:
        return
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(
        os.path.abspath(output_path)))
    os.close(fd)
    try:
        with zipfile.ZipFile(source_path, "r") as source, \
                zipfile.ZipFile(output_path, "r") as output:
            source_infos = {item.filename: item for item in source.infolist()
                            if not item.is_dir()}
            output_infos = {item.filename: item for item in output.infolist()
                            if not item.is_dir()}
            source_names = [item.filename for item in source.infolist()
                            if not item.is_dir()]
            output_names = [item.filename for item in output.infolist()
                            if not item.is_dir()]
            if (len(source_names) != len(source_infos) or
                    len(output_names) != len(output_infos)):
                raise ValueError("DOCX含重复部件名称，不能安全透传未解析部件")
            entries = {name: (info, output.read(name))
                       for name, info in output_infos.items()}
            source_types = ET.fromstring(source.read("[Content_Types].xml"))
            output_types = ET.fromstring(entries["[Content_Types].xml"][1])
            source_overrides = {
                node.get("PartName", ""): node for node in source_types
                if node.tag == _CT_NS + "Override"}
            source_defaults = {
                node.get("Extension", "").lower(): node for node in source_types
                if node.tag == _CT_NS + "Default"}
            output_overrides = {
                node.get("PartName", "") for node in output_types
                if node.tag == _CT_NS + "Override"}
            output_defaults = {
                node.get("Extension", "").lower() for node in output_types
                if node.tag == _CT_NS + "Default"}

            for row in rows:
                name = row["part"]
                if name not in source_infos:
                    raise ValueError("未解析部件已从源DOCX中消失")
                raw = source.read(name)
                if (len(raw) != int(row.get("size", -1)) or
                        hashlib.sha256(raw).hexdigest() != row.get("sha256")):
                    raise ValueError("未解析部件与确认时摘要不一致")
                if name not in output_infos or entries[name][1] != raw:
                    entries[name] = (source_infos[name], raw)

                part_name = "/" + name
                if part_name in source_overrides:
                    if part_name not in output_overrides:
                        output_types.append(ET.fromstring(ET.tostring(
                            source_overrides[part_name])))
                        output_overrides.add(part_name)
                else:
                    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    if extension not in output_defaults:
                        default = source_defaults.get(extension)
                        if default is None:
                            raise ValueError("未解析部件缺少内容类型声明")
                        output_types.append(ET.fromstring(ET.tostring(default)))
                        output_defaults.add(extension)

            entries["[Content_Types].xml"] = (
                output_infos["[Content_Types].xml"],
                ET.tostring(output_types, encoding="utf-8", xml_declaration=True))
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as rebuilt:
            for name, (info, data) in entries.items():
                rebuilt.writestr(info, data)
        os.replace(tmp, output_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _verify_story_text(root, part, manifest):
    """验证内部预期段落清单；外部调用不提供清单时继续严格残留检查。"""
    rows = [row for row in manifest if row["part"] == part]
    verified = set()
    for row in rows:
        paragraph = root
        try:
            for index in row["path"]:
                paragraph = paragraph[index]
        except (IndexError, TypeError):
            raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：预期段落位置变化")
        if paragraph.tag != _W + "p":
            raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：预期段落位置变化")
        digest = hashlib.sha256(D.docx_para_text(paragraph).encode("utf-8")).hexdigest()
        if digest != row["sha256"] or paragraph in verified:
            raise ValueError("DOCX_EXPECTED_EDIT_CONFLICT：已选改写或保留文字与预期不一致")
        verified.add(paragraph)
    return verified


def clean_docx(path, originals, comment_mode, preserve_revisions=False,
               allow_unsupported_revisions=False,
               expected_unsupported_digest=None,
               allow_unsupported_objects=False,
               expected_object_digest=None,
               external_relationship_action="remove",
               external_relationship_acknowledged=False,
               expected_external_relationship_digest=None,
               allow_unverified_passthrough=False,
               expected_unscanned_digest=None,
               allow_unsafe_fields=False,
               expected_unsafe_field_digest=None,
               allow_unredacted_content=False,
               expected_unredacted_content_digest=None,
               allow_unknown_revision_passthrough=False,
               expected_revision_passthrough_digest=None,
               expected_story_text=None):
    """Export a plain OOXML subset; reject content we cannot confidently inspect."""
    from docx import Document
    from contract_docx_comments import inspect_comment_package
    from contract_docx_revisions import (
        REVISION_TAGS, _classify_field_code, _note_content_inventory,
        revision_passthrough_digest,
    )

    originals, attribute_originals = _prepare_residual_values(originals)
    residual_pattern = _compile_residual_pattern(originals)
    attribute_pattern = _compile_residual_pattern(attribute_originals)
    if comment_mode not in ("redact_preserve", "preserve_unredacted", "remove_all"):
        raise ValueError("DOCX批注处理方式无效")
    allowed_revision_tags = {
        "ins", "del", "delText", "moveFrom", "moveTo",
        "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
    }
    revision_analysis = None
    revision_inventory = {}
    unsupported_manifest = []
    supported_property_manifest = []
    unsafe_field_manifest = []
    unsupported_digest = ""
    object_manifest = []
    object_digest = ""
    object_roots_by_part = {}
    unknown_revision_passthrough = bool(
        allow_unknown_revision_passthrough and
        expected_revision_passthrough_digest)
    if (preserve_revisions or allow_unsupported_objects or
            allow_unverified_passthrough or allow_unsafe_fields or
            allow_unredacted_content or expected_object_digest is not None):
        from contract_docx_revisions import analyze_docx_revisions
        if unknown_revision_passthrough:
            revision_analysis = {
                "complete": False, "has_revisions": True,
                "inventory_scope_unknown": True,
                "unsupported_manifest": [],
                "unsupported_object_manifest": [],
                "unsafe_field_manifest": [],
                "inventory": {"complete": False, "has_revisions": True,
                              "inventory_scope_unknown": True},
            }
        else:
            revision_analysis = analyze_docx_revisions(path)
        revision_inventory = revision_analysis.get("inventory") or {}
        unsupported_manifest = revision_analysis.get("unsupported_manifest") or []
        object_manifest = revision_analysis.get("unsupported_object_manifest") or []
        object_digest = str(revision_analysis.get("object_digest") or "")
        object_roots_by_part = {}
        supported_property_manifest = revision_analysis.get("supported_property_manifest") or []
        unsupported_digest = str(revision_inventory.get("unsupported_digest") or "")
        if ((not revision_analysis.get("complete") and not (
                unknown_revision_passthrough or
                allow_unverified_passthrough and revision_analysis.get("passthrough_safe"))) or
                (not revision_analysis.get("has_revisions") and not object_manifest and
                 not revision_analysis.get("unsafe_field_manifest") and
                 not revision_inventory.get("unredacted_content_parts") and
                 expected_object_digest is None)):
            raise ValueError("修订结构未通过完整盘点，不能保留导出")
        if unsupported_manifest:
            if (not allow_unsupported_revisions or
                    (not revision_analysis.get("can_export_after_ack") and not (
                        allow_unverified_passthrough and
                        revision_analysis.get("passthrough_safe"))) or
                    unsupported_digest != expected_unsupported_digest):
                raise ValueError("未支持修订未经确认或结构已变化，不能保留导出")
        elif (not unknown_revision_passthrough and
              not revision_analysis.get("can_preserve_and_redact") and not (
                allow_unverified_passthrough and
                revision_analysis.get("passthrough_safe"))):
            raise ValueError("修订结构未通过受支持性复核，不能保留导出")
        if object_manifest and (
                not allow_unsupported_objects or object_digest != expected_object_digest):
            raise ValueError("未支持的文本框或绘图形状内容未经确认或结构已变化，不能保留导出")
        if (expected_object_digest is not None and
                object_digest != expected_object_digest):
            raise ValueError("文本框对象结构与复核时不一致，不能保留导出")
        unsafe_field_manifest = revision_analysis.get("unsafe_field_manifest") or []
        unsafe_field_digest = str(revision_inventory.get("unsafe_field_digest") or "")
        if unsafe_field_manifest and (
                not allow_unsafe_fields or
                unsafe_field_digest != expected_unsafe_field_digest):
            raise ValueError("未支持字段指令未经确认或结构已变化，不能保留导出")
        if allow_unverified_passthrough and str(
                revision_inventory.get("unscanned_digest") or "") != expected_unscanned_digest:
            raise ValueError("未扫描部件摘要已变化，请重新确认")

    with zipfile.ZipFile(path) as source:
        names = set(source.namelist())
        if external_relationship_action not in {"remove", "preserve"}:
            raise ValueError("外部关系处理方式无效")
        external_manifest, external_digest = _external_relationship_manifest(source, names)
        external_ids_by_part = {}
        for row in external_manifest:
            external_ids_by_part.setdefault(row["part"], set()).add(row["id"])
        if (expected_external_relationship_digest and
                external_digest != expected_external_relationship_digest):
            raise ValueError("外部关系清单已变化，请重新检查")
        if (external_manifest and external_relationship_action == "preserve" and
                (not external_relationship_acknowledged or
                 external_digest != expected_external_relationship_digest)):
            raise ValueError("保留外部链接前须单独确认远端访问和未扫描内容风险")
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise ValueError("DOCX 结构不完整")
        if sum(i.file_size for i in source.infolist()) > 150 * 1024 * 1024:
            raise ValueError("DOCX 解压后过大，不能安全导出")
        note_inventory = _note_content_inventory(source, names)
        note_parts = {row["part"] for row in note_inventory["parts"]}
        unredacted_content_parts = {
            row["part"] for row in note_inventory["parts"]
            if int(row.get("count") or 0) > 0}
        if unredacted_content_parts and (
                not allow_unredacted_content or
                str(expected_unredacted_content_digest or "") != note_inventory["digest"]):
            raise ValueError("脚注或尾注内容未自动扫描和脱敏，请完成Word人工审核确认后再导出")
        comment_info = inspect_comment_package(path)
        # Unsupported comment details remain in the package and are reported
        # for one-time export confirmation by the application layer.
        allowed = {n for n in names if _ALLOWED_WORD.fullmatch(n)}
        allowed.update(_relationship_part(owner) for owner in list(allowed)
                       if _relationship_part(owner) in names)
        if note_parts:
            note_dependencies, note_relationship_parts = _preserved_part_closure(
                source, names, note_parts)
            allowed.update(note_dependencies)
            allowed.update(note_relationship_parts)
        unsupported_parts = {row.get("part") for row in unsupported_manifest
                             if row.get("part") and row.get("part") not in allowed}
        if unsupported_parts - names:
            raise ValueError("未支持修订所在DOCX部件缺失")
        preserved_parts, revision_relationship_parts = _preserved_part_closure(
            source, names, unsupported_parts) if unsupported_parts else (set(), set())
        allowed.update(preserved_parts)
        allowed.update(revision_relationship_parts)
        unscanned_passthrough_parts = set(preserved_parts)
        if allow_unverified_passthrough:
            raw_parts = {row.get("part") for row in revision_inventory.get(
                "unscanned_parts", []) if row.get("part") in names}
            raw_dependencies, raw_relationships = _preserved_part_closure(
                source, names, raw_parts) if raw_parts else (set(), set())
            allowed.update(raw_parts | raw_dependencies | raw_relationships)
            unscanned_passthrough_parts.update(raw_parts | raw_dependencies)
            revision_relationship_parts.update(raw_relationships)
        story_names = [n for n in allowed if n == "word/document.xml" or
                       re.fullmatch(r"word/(?:header|footer)\d+\.xml", n)]
        revision_tag_names = set(REVISION_TAGS)
        revision_tag_qnames = {_W + local for local in revision_tag_names}
        unknown_revision_parts = set()
        unknown_revision_roots_by_part = {}
        if unknown_revision_passthrough:
            for part in names:
                if (not part.lower().startswith("word/") or
                        not part.lower().endswith(".xml") or
                        part.lower().endswith(".rels")):
                    continue
                revision_root = ET.fromstring(source.read(part))
                if any(node.tag in revision_tag_qnames for node in revision_root.iter()):
                    unknown_revision_parts.add(part)
        story_roots = [(name, ET.fromstring(source.read(name))) for name in story_names]
        if unknown_revision_passthrough:
            for part, revision_root in story_roots:
                if part not in unknown_revision_parts:
                    continue
                roots = set()
                for node in revision_root.iter():
                    if node.tag not in revision_tag_qnames:
                        continue
                    paragraph = next((ancestor for ancestor in node.iterancestors()
                                      if ancestor.tag == _W + "p"), None)
                    roots.add(paragraph if paragraph is not None else node)
                unknown_revision_roots_by_part[part] = roots
        content_control_digests = {}
        for part, root in story_roots:
            controls = list(root.iter(_W + "sdt"))
            footer_fields = (_safe_page_footer_nodes(root)
                             if re.fullmatch(r"word/footer\d+\.xml", part) else set())
            safe_footer_controls = {control for control in controls
                                    if control in footer_fields}
            safe_controls = _simple_content_controls(
                root, ignored_controls=safe_footer_controls)
            if controls and len(safe_controls) + len(safe_footer_controls) != len(controls):
                raise ValueError("DOCX含未绑定或包含不受支持内容的内容控件")
            if controls:
                content_control_digests[part] = _content_control_structure_digest(
                    root, additional_controls=safe_footer_controls)
        for part, root in story_roots:
            object_rows = [row for row in unsupported_manifest
                           if row.get("part") == part and
                           str(row.get("reason") or "").startswith("兼容文本框")]
            if not object_rows:
                continue
            for anchor in _manifest_nodes(root, part, object_rows):
                logical_root = next((ancestor for ancestor in anchor.iterancestors()
                                     if ancestor.tag == _MC + "AlternateContent"), anchor)
                object_roots_by_part.setdefault(part, set()).add(logical_root)
        dependency_roots = dict(story_roots)
        for part in {row.get("part") for row in unsupported_manifest}:
            if (part in allowed and part not in dependency_roots and
                    isinstance(part, str) and part.endswith(".xml")):
                dependency_roots[part] = ET.fromstring(source.read(part))
        unsupported_dependency_parts, unsupported_relationship_parts = (
            _unsupported_relationship_closure(
                source, names, unsupported_manifest, dependency_roots,
                extra_roots_by_part=object_roots_by_part)
            if unsupported_manifest or object_roots_by_part else (set(), set()))
        allowed.update(unsupported_dependency_parts)
        allowed.update(unsupported_relationship_parts)
        unscanned_passthrough_parts.update(unsupported_dependency_parts)
        image_parts, image_relationship_parts = _story_image_parts(
            source, names, story_roots, unsupported_manifest)
        allowed.update(image_parts)
        allowed.update(image_relationship_parts)
        if comment_mode == "redact_preserve":
            allowed.update(comment_info.related_parts & names)
        comment_passthrough = set()
        comment_relationships = set()
        if comment_mode == "preserve_unredacted":
            from contract_docx_comments import comment_package_closure
            comment_parts, comment_relationships, relation_errors, outgoing = comment_package_closure(source)
            if relation_errors:
                raise ValueError("无法完整保留批注关系：" + "; ".join(relation_errors[:3]))
            comment_passthrough = (comment_parts | comment_relationships) & names
            allowed.update(comment_passthrough)
        allowed.add("[Content_Types].xml")
        if "_rels/.rels" in names:
            allowed.add("_rels/.rels")
        if "word/_rels/document.xml.rels" in names:
            allowed.add("word/_rels/document.xml.rels")
        payload = {}
        note_ids = note_inventory["note_ids"]
        for name in allowed:
            data = source.read(name)
            if (name in comment_passthrough or name in unscanned_passthrough_parts or
                    name in note_parts or
                    (unknown_revision_passthrough and
                     name in unknown_revision_parts and name not in story_names)):
                payload[name] = data
                continue
            if name.startswith("word/") and name.endswith(".xml"):
                root = ET.fromstring(data)
                verified_paragraphs = (_verify_story_text(root, name, expected_story_text)
                                       if expected_story_text is not None else set())
                unsupported_roots = _manifest_nodes(
                    root, name, unsupported_manifest) if unsupported_manifest else set()
                unsupported_roots.update(
                    _manifest_object_roots(root, name, unsupported_manifest))
                supported_property_roots = _manifest_nodes(
                    root, name, supported_property_manifest) if supported_property_manifest else set()
                if name in ("word/styles.xml", "word/settings.xml"):
                    for element in root.iter():
                        if (element.tag in {_W + "vanish", _W + "webHidden", _W + "specVanish"} and
                                _ooxml_toggle_value(element)):
                            if not _inside_nodes(element, unsupported_roots):
                                raise ValueError("文档样式或设置含隐藏文字，需先在 Word 中处理")
                        if element.tag in {_W + "rPrChange", _W + "pPrChange"} and not (
                                preserve_revisions and
                                _inside_nodes(element, supported_property_roots | unsupported_roots)):
                            raise ValueError("文档样式或设置含未确认的属性修订，需先在 Word 中处理")
                if name == "word/document.xml" or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
                    if comment_mode == "remove_all":
                        for parent in root.iter():
                            for child in list(parent):
                                if child.tag in _COMMENT_TAGS:
                                    parent.remove(child)
                    footer_fields = (_safe_page_footer_nodes(root)
                                     if re.fullmatch(r"word/footer\d+\.xml", name) else set())
                    # Safe page fields do not exempt nearby selected sensitive text from verification.
                    for text_node in footer_fields:
                        if (text_node.tag == _W + "t" and attribute_pattern and
                                attribute_pattern.search(text_node.text or "") and
                                not any(p in verified_paragraphs for p in text_node.iterancestors())):
                            raise ValueError("页码内容控件中仍含已选原文，请核对改写结果")
                    content_controls = list(root.iter(_W + "sdt"))
                    safe_footer_controls = {control for control in content_controls
                                            if control in footer_fields}
                    safe_content_controls = _simple_content_controls(
                        root, ignored_controls=safe_footer_controls)
                    if (content_controls and
                            len(safe_content_controls) + len(safe_footer_controls) !=
                            len(content_controls)):
                        raise ValueError("DOCX含未绑定或包含不受支持内容的内容控件")
                    story_images = {node for drawable in
                                    list(root.iter(_W + "drawing")) + list(root.iter(_W + "pict"))
                                    for node in (drawable,)}
                    bad = []
                    for element in root.iter():
                        if not isinstance(element.tag, str):
                            continue
                        if (unknown_revision_passthrough and
                                _inside_nodes(element,
                                              unknown_revision_roots_by_part.get(name, set()))):
                            continue
                        local = element.tag.rsplit("}", 1)[-1]
                        if (local == "hyperlink" and
                                element.get(_R + "id") in external_ids_by_part.get(
                                    _relationship_part(name), set())):
                            continue
                        if element.tag.startswith(_MC):
                            if not (any(_inside_nodes(root_node, {element})
                                        for root_node in unsupported_roots) or
                                    _inside_nodes(element, unsupported_roots)):
                                bad.append(local)
                        elif element.tag.startswith(_W) and local in _BAD_TAGS:
                            if ((preserve_revisions or allow_unsupported_objects or
                                 allow_unverified_passthrough) and
                                    _inside_nodes(element, unsupported_roots)):
                                continue
                            if ((preserve_revisions or allow_unverified_passthrough) and
                                    local in allowed_revision_tags):
                                continue
                            if (name == "word/document.xml" and local == "footnoteReference" and
                                    "footnote" in note_ids and
                                    element.get(_W + "id") in note_ids["footnote"]):
                                continue
                            if (name == "word/document.xml" and local == "endnoteReference" and
                                    "endnote" in note_ids and
                                    element.get(_W + "id") in note_ids["endnote"]):
                                continue
                            if local in {"bookmarkStart", "bookmarkEnd"}:
                                continue
                            if local == "sdt" and element in safe_content_controls:
                                continue
                            if (local in {"vanish", "webHidden", "specVanish"} and
                                    any(ancestor.tag == _W + "r"
                                        for ancestor in element.iterancestors()) and
                                    any(node.tag == _W + "t"
                                        for ancestor in element.iterancestors()
                                        if ancestor.tag == _W + "r"
                                        for node in ancestor.iter())):
                                continue
                            if local in {"drawing", "pict"} and element in story_images:
                                continue
                            if local == "fldSimple":
                                safe_field, _reason = _classify_field_code(
                                    element.get(_W + "instr", ""))
                                if safe_field or (allow_unsafe_fields and
                                                  unsafe_field_manifest):
                                    continue
                            if local in {"sdt", "instrText", "fldSimple"} and element in footer_fields:
                                continue
                            bad.append(local)
                    if bad:
                        raise ValueError("文档含批注、修订、隐藏文字、图片或复杂对象（%s）；请在 Word 中处理后重试" % bad[0])
                    expected_control_digest = content_control_digests.get(name)
                    if expected_control_digest is not None and (
                            _content_control_structure_digest(
                                root, additional_controls=safe_footer_controls) !=
                            expected_control_digest):
                        raise ValueError("外发清理改变了内容控件结构")
                    for element in root.iter():
                        if (unknown_revision_passthrough and
                                _inside_nodes(element,
                                              unknown_revision_roots_by_part.get(name, set()))):
                            continue
                        if ((preserve_revisions or allow_unsupported_objects or
                             allow_unverified_passthrough) and
                                _inside_nodes(element, unsupported_roots)):
                            continue
                        if element.tag == _W + "instrText":
                            continue
                        if _is_residual_visible_text_node(element) and element.text:
                            if residual_pattern and residual_pattern.search(element.text):
                                raise ValueError("已选原文仍存在于非正文 XML 中")
                        if (attribute_pattern and not (
                                allow_unsafe_fields and
                                element.tag == _W + "fldSimple") and
                                any(attribute_pattern.search(attr)
                                    for attr in element.attrib.values())):
                            raise ValueError("已选原文仍存在于 XML 属性中")
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            payload[name] = data
        rel_names = ({"_rels/.rels", "word/_rels/document.xml.rels"} |
                     image_relationship_parts | revision_relationship_parts |
                     {name for name in allowed if name.lower().endswith(".rels")})
        for rel_name in rel_names:
            if rel_name not in payload:
                continue
            root = ET.fromstring(payload[rel_name])
            for rel in list(root):
                target = rel.get("Target", "")
                if rel.get("TargetMode", "").lower() == "external":
                    if external_relationship_action == "remove":
                        owner = _relationship_owner(rel_name)
                        if owner in payload:
                            if owner in unscanned_passthrough_parts:
                                raise ValueError("外部关系所在部件未解析，不能安全移除")
                            owner_root = ET.fromstring(payload[owner])
                            _remove_relationship_references(owner_root, rel.get("Id", ""))
                            payload[owner] = ET.tostring(
                                owner_root, encoding="utf-8", xml_declaration=True)
                        root.remove(rel)
                    continue
                resolved = _resolve_part(_relationship_owner(rel_name), target)
                if resolved not in allowed:
                    root.remove(rel)  # detached metadata/unsupported part is excluded from clean copy
            payload[rel_name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        root = ET.fromstring(payload["[Content_Types].xml"])
        for child in list(root):
            if child.tag == _CT_NS + "Override" and child.get("PartName", "").lstrip("/") not in allowed:
                root.remove(child)
        payload["[Content_Types].xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if unsupported_parts:
            content_types = ET.fromstring(payload["[Content_Types].xml"])
            overrides = {node.get("PartName", "").lstrip("/") for node in content_types
                         if node.tag == _CT_NS + "Override"}
            defaults = {node.get("Extension", "").lower() for node in content_types
                        if node.tag == _CT_NS + "Default"}
            for part in preserved_parts | unsupported_dependency_parts:
                extension = part.rsplit(".", 1)[-1].lower() if "." in part else ""
                if part not in overrides and extension not in defaults:
                    raise ValueError("未支持修订关联部件缺少内容类型声明")
        # Inspect visible XML, style/setting attributes, and every retained part.
        for name, data in payload.items():
            if (name == "word/document.xml" or
                    re.fullmatch(r"word/(?:header|footer)\d+\.xml", name) or
                    name in comment_passthrough):
                continue  # visible text is controlled by the user's per-occurrence review
            if (external_relationship_action == "preserve" and
                    name.lower().endswith(".rels")):
                # Preserved non-hyperlink targets are acknowledged but not scanned.
                rel_root = ET.fromstring(data)
                changed = False
                for rel in rel_root:
                    if rel.get("TargetMode", "").lower() == "external":
                        rel.attrib.pop("Target", None)
                        changed = True
                if changed:
                    data = ET.tostring(rel_root, encoding="utf-8", xml_declaration=True)
            decoded = data.decode("utf-8", "ignore")
            if (name not in unscanned_passthrough_parts and
                    name not in note_parts and name not in unknown_revision_parts and
                    attribute_pattern and
                    attribute_pattern.search(decoded)):
                raise ValueError("导出件仍含已选原文：%s" % name)
        fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(path))
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as target:
                for name, data in payload.items():
                    target.writestr(name, data)
            Document(tmp)  # fail before replacing the redacted result
            if expected_story_text is not None:
                with zipfile.ZipFile(tmp) as completed:
                    for part in {row["part"] for row in expected_story_text}:
                        _verify_story_text(ET.fromstring(completed.read(part)), part, expected_story_text)
            if unknown_revision_passthrough:
                if revision_passthrough_digest(tmp) != expected_revision_passthrough_digest:
                    raise ValueError("外发清理改变了盘点异常时保留的修订内容")
                if note_parts:
                    with zipfile.ZipFile(tmp, "r") as completed:
                        output_notes = _note_content_inventory(
                            completed, set(completed.namelist()))
                    if output_notes.get("structure_digest") != note_inventory.get("structure_digest"):
                        raise ValueError("外发清理改变了脚注或尾注编号或文字内容")
            elif (unsupported_manifest or object_manifest or expected_object_digest is not None or
                    allow_unverified_passthrough or note_parts):
                output_analysis = analyze_docx_revisions(tmp)
                output_inventory = output_analysis.get("inventory") or {}
                passthrough_digest_matches = (
                    output_inventory.get("unscanned_digest") == expected_unscanned_digest)
                if (not output_analysis.get("complete") and not (
                        allow_unverified_passthrough and
                        output_analysis.get("passthrough_safe") and
                        passthrough_digest_matches)):
                    raise ValueError("外发清理后的DOCX无法完整盘点")
                if output_inventory.get("unsupported_digest") != unsupported_digest:
                    raise ValueError("外发清理改变了已确认保留的修订结构")
                if (object_manifest and
                        output_analysis.get("object_digest") != object_digest):
                    raise ValueError("外发清理改变了已确认保留的文本框对象")
                if (expected_object_digest is not None and
                        output_analysis.get("object_digest") != object_digest):
                    raise ValueError("外发清理改变了文本框对象结构")
                if (allow_unverified_passthrough and
                        not passthrough_digest_matches):
                    raise ValueError("外发清理改变了未扫描部件")
                if (note_parts and
                        output_inventory.get("note_structure_digest") !=
                        note_inventory.get("structure_digest")):
                    raise ValueError("外发清理改变了脚注或尾注编号或文字内容")
            # Windows cannot replace a path while its source ZipFile handle is still open.
            source.close()
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
