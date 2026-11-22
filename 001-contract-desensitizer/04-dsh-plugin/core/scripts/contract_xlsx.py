"""Local XLSX inspection and export for supported SpreadsheetML workbooks."""
from copy import deepcopy
from dataclasses import dataclass
import io
from pathlib import PurePosixPath
import posixpath
import re
import zipfile

from lxml import etree as XML


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PART = re.compile(
    r"(?:\[Content_Types\]\.xml|_rels/\.rels|docProps/(?:core|app)\.xml|"
    r"xl/(?:workbook|styles|sharedStrings|calcChain)\.xml|"
    r"xl/_rels/workbook\.xml\.rels|xl/theme/theme\d+\.xml|"
    r"xl/(?:worksheets/sheet\d+|comments\d+|comments/comment\d+)\.xml|"
    r"xl/worksheets/_rels/sheet\d+\.xml\.rels|xl/drawings/[^/]+\.vml)"
)
ADDRESS = re.compile(r"[A-Z]{1,3}[1-9][0-9]{0,6}")
SHEET_NODES = {
    "sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData",
    "sheetProtection", "mergeCells", "printOptions", "pageMargins", "pageSetup",
    "rowBreaks", "colBreaks", "legacyDrawing",
}
WORKBOOK_NODES = {
    "fileVersion", "fileSharing", "workbookPr", "workbookProtection", "bookViews",
    "sheets", "definedNames", "calcPr",
}


class WorkbookError(ValueError):
    def __init__(self, message, *, cell=None, advice="", code=""):
        super().__init__(message)
        self.cell = cell
        self.advice = advice
        self.code = code

    def response(self):
        payload = {"ok": False, "error": str(self)}
        if self.cell:
            payload["cell"] = {"sheet": self.cell[0], "address": self.cell[1]}
        if self.advice:
            payload["advice"] = self.advice
        if self.code:
            payload["code"] = self.code
        return payload


def tag(name):
    return f"{{{MAIN}}}{name}"


def field_tag(namespace, name):
    return f"{{{namespace}}}{name}"


def read_parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) > 256 or sum(entry.file_size for entry in entries) > 64 * 1024**2:
            raise WorkbookError("工作簿展开后超过处理上限")
        parts = {}
        for entry in entries:
            name = entry.filename
            if name in parts or not PART.fullmatch(name) or entry.flag_bits & 1:
                raise WorkbookError(f"无法处理工作簿部件：{name}")
            parser = XML.XMLParser(resolve_entities=False, no_network=True,
                                   remove_comments=True, remove_pis=True)
            try:
                element = XML.fromstring(archive.read(entry), parser)
            except XML.XMLSyntaxError as exc:
                raise WorkbookError(f"工作簿部件无法读取：{name}") from exc
            if element.getroottree().docinfo.doctype:
                raise WorkbookError(f"工作簿部件包含不支持的声明：{name}")
            parts[name] = element
    if not {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels"} <= parts.keys():
        raise WorkbookError("工作簿缺少必需部件")
    return parts


def relationships(parts):
    result = {}
    for name, element in parts.items():
        if not name.endswith(".rels"):
            continue
        base = "" if name == "_rels/.rels" else name.split("/_rels/")[0]
        if name != "_rels/.rels":
            owner = posixpath.join(base, PurePosixPath(name).name.removesuffix(".rels"))
            if owner not in parts:
                raise WorkbookError(f"工作簿包含无主关系：{name}")
        links = {}
        for item in element:
            rid, target = item.get("Id"), item.get("Target", "")
            if item.tag != field_tag(PKG_REL, "Relationship") or not rid or rid in links:
                raise WorkbookError(f"工作簿关系无效：{name}")
            if item.get("TargetMode") not in (None, "Internal") or ":" in target:
                raise WorkbookError(f"工作簿包含外部链接：{name}")
            normalized = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                             else posixpath.join(base, target))
            if normalized not in parts or set(PurePosixPath(normalized).parts) & {"..", "_rels"}:
                raise WorkbookError(f"工作簿关系指向无法处理的部件：{name}")
            relation = item.get("Type", "")
            known = False
            if name == "_rels/.rels":
                known = (relation, normalized) in {
                    (DOC_REL + "/officeDocument", "xl/workbook.xml"),
                    (PKG_REL + "/metadata/core-properties", "docProps/core.xml"),
                    (DOC_REL + "/extended-properties", "docProps/app.xml"),
                }
            elif name == "xl/_rels/workbook.xml.rels":
                for kind, target in {
                    "worksheet": r"xl/worksheets/sheet\d+\.xml",
                    "styles": r"xl/styles\.xml", "sharedStrings": r"xl/sharedStrings\.xml",
                    "theme": r"xl/theme/theme\d+\.xml", "calcChain": r"xl/calcChain\.xml",
                }.items():
                    if relation == DOC_REL + "/" + kind and re.fullmatch(target, normalized):
                        known = True
                        break
            else:
                known = ((relation == DOC_REL + "/comments" and
                          re.fullmatch(r"xl/(?:comments\d+|comments/comment\d+)\.xml", normalized))
                         or (relation == DOC_REL + "/vmlDrawing" and normalized.endswith(".vml")))
            if not known:
                raise WorkbookError(f"工作簿包含不支持的关系类型：{name}")
            links[rid] = normalized
        result[name] = links
    reachable = {"[Content_Types].xml", *result}
    for links in result.values():
        reachable.update(links.values())
    if parts.keys() - reachable:
        raise WorkbookError(f"无法处理未关联部件：{sorted(parts.keys() - reachable)[0]}")
    return result


def ensure_children(element, allowed, location):
    for node in element:
        if node.tag not in {tag(name) for name in allowed}:
            raise WorkbookError(f"{location} 包含无法处理的结构：{XML.QName(node).localname}")


def text_nodes(container):
    return container.xpath("./m:t | ./m:r/m:t", namespaces={"m": MAIN})


def plain_text(container):
    return "".join(node.text or "" for node in text_nodes(container))


@dataclass
class Field:
    sheet: str
    location: str
    text: str
    node: object
    kind: str


class Workbook:
    def __init__(self, data):
        self.parts = read_parts(data)
        self.links = relationships(self.parts)
        self.fields = []
        self.formulas = []
        self.cells = {}
        self.sheet_names = []
        self.hidden_sheets = []
        self.date_styles = self.number_styles(
            set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59)))
        self.amount_styles = self.number_styles({5, 6, 7, 8, 41, 42, 43, 44})
        self.percent_styles = self.number_styles({9, 10}, r"0(?:\.0+)?%")
        self.date_styles |= self.number_styles(set(), r"(?:yyyy|yy)[-/]mm[-/]dd")
        self.read_workbook()

    def number_styles(self, number_formats, custom_pattern=None):
        styles = self.parts.get("xl/styles.xml")
        if styles is None:
            return set()
        formats = styles.find(tag("cellXfs"))
        if formats is None:
            return set()
        ids = {str(value) for value in number_formats}
        if custom_pattern:
            ids.update(style.get("numFmtId") for style in styles.iter(tag("numFmt"))
                       if re.fullmatch(custom_pattern, style.get("formatCode", ""), re.I))
        return {str(index) for index, style in enumerate(formats)
                if style.get("numFmtId") in ids}

    def amount_cell(self, sheet, address):
        cell = self.cells.get((sheet, address))
        return (cell is not None and cell.get("t", "n") == "n" and
                cell.get("s") not in self.date_styles | self.percent_styles)

    def read_workbook(self):
        workbook = self.parts["xl/workbook.xml"]
        if workbook.tag != tag("workbook"):
            raise WorkbookError("工作簿 XML 类型无效")
        ensure_children(workbook, WORKBOOK_NODES, "工作簿")
        names = workbook.find(tag("definedNames"))
        if names is not None and len(names):
            raise WorkbookError("工作簿包含暂未支持的命名范围")
        for props in workbook.findall(tag("workbookPr")):
            props.attrib.pop("codeName", None)
        for share in workbook.findall(tag("fileSharing")):
            workbook.remove(share)
        sheets = workbook.find(tag("sheets"))
        if sheets is None or not len(sheets):
            raise WorkbookError("工作簿没有工作表")
        known = set()
        strings = self.parts.get("xl/sharedStrings.xml")
        if strings is not None:
            ensure_children(strings, {"si"}, "共享字符串")
        sheet_parts = set()
        for row in sheets:
            name = row.get("name", "")
            part = self.links["xl/_rels/workbook.xml.rels"].get(row.get(field_tag(DOC_REL, "id")), "")
            if (not name or name.casefold() in known or part in sheet_parts or
                    not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", part)):
                raise WorkbookError("工作簿的工作表名称或关系无效")
            known.add(name.casefold())
            sheet_parts.add(part)
            self.sheet_names.append(name)
            if row.get("state") in {"hidden", "veryHidden"}:
                self.hidden_sheets.append(name)
            self.read_sheet(name, part, strings)
        if sheet_parts != {name for name in self.parts if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)}:
            raise WorkbookError("工作簿包含未复核的工作表部件")
        if not self.cells:
            raise WorkbookError("工作簿没有可复核的单元格")

    def read_sheet(self, name, part, strings):
        root = self.parts[part]
        if root.tag != tag("worksheet"):
            raise WorkbookError(f"{name} 的工作表类型无效")
        ensure_children(root, SHEET_NODES, name)
        for props in root.findall(tag("sheetPr")):
            props.attrib.pop("codeName", None)
        data = root.find(tag("sheetData"))
        if data is None:
            raise WorkbookError(f"{name} 缺少单元格数据")
        ensure_children(data, {"row"}, name)
        for row in data:
            ensure_children(row, {"c"}, name)
        for cell in data.iter(tag("c")):
            address = cell.get("r", "")
            if not ADDRESS.fullmatch(address) or (name, address) in self.cells:
                raise WorkbookError(f"{name} 有无效或重复的单元格位置")
            if len(self.cells) >= 40000:
                raise WorkbookError("单元格超过 40,000 个，请拆分工作簿")
            self.cells[name, address] = cell
            self.read_cell(name, address, cell, strings)
        rel = f"xl/worksheets/_rels/{PurePosixPath(part).name}.rels"
        for target in self.links.get(rel, {}).values():
            if re.fullmatch(r"xl/(?:comments\d+|comments/comment\d+)\.xml", target):
                self.read_comments(name, self.parts[target])
            elif target.endswith(".vml"):
                shapes = self.parts[target]
                if any((node.text or "").strip() for node in shapes.iter()
                       if XML.QName(node).localname in {"textbox", "div"}):
                    raise WorkbookError(f"{name} 的批注图形包含未支持的文字对象")
                if any(XML.QName(node).localname in {"imagedata", "image", "textpath"} for node in shapes.iter()):
                    raise WorkbookError(f"{name} 含不支持的批注图像或形状")
                if any(node.get("ObjectType") != "Note" for node in shapes.iter()
                       if XML.QName(node).localname == "ClientData"):
                    raise WorkbookError(f"{name} 含非批注绘图对象")
            else:
                raise WorkbookError(f"{name} 有无法处理的工作表关系")

    def read_cell(self, sheet, address, cell, strings):
        location = f"{sheet}!{address}"
        ensure_children(cell, {"f", "v", "is"}, location)
        if len({child.tag for child in cell}) != len(cell) or "cm" in cell.attrib or "vm" in cell.attrib:
            raise WorkbookError(f"{location} 有重复的值或隐藏单元格元数据")
        kind = cell.get("t", "n")
        value = cell.find(tag("v"))
        formula = cell.find(tag("f"))
        if formula is not None:
            if len(self.formulas) >= 2000:
                raise WorkbookError(f"{location}：工作簿公式超过 2,000 条")
            self.formulas.append((sheet, address, formula, value))
            return
        if kind == "s":
            index = value.text if value is not None and value.text else ""
            if not re.fullmatch(r"[0-9]{1,8}", index) or strings is None or int(index) >= len(strings):
                raise WorkbookError(f"{location} 的共享字符串引用无效")
            inline = deepcopy(strings[int(index)])
            inline.tag = tag("is")
            cell.remove(value)
            cell.append(inline)
            cell.set("t", "inlineStr")
            kind = "inlineStr"
        if kind == "inlineStr":
            inline = cell.find(tag("is"))
            if inline is None:
                raise WorkbookError(f"{location} 缺少文本内容")
            ensure_children(inline, {"t", "r"}, location)
            for run in inline.findall(tag("r")):
                ensure_children(run, {"rPr", "t"}, location)
            self.fields.append(Field(sheet, address, plain_text(inline), inline, "text"))
        elif kind in {"n", "d", "b", "e", "str"}:
            if value is not None and value.text:
                if kind == "d" or (kind == "n" and cell.get("s") in self.date_styles):
                    kind = "date"
                elif kind == "n" and cell.get("s") in self.amount_styles:
                    kind = "amount"
                self.fields.append(Field(sheet, address, value.text, cell, kind))
        else:
            raise WorkbookError(f"{location} 的单元格类型暂不支持")

    def read_comments(self, sheet, root):
        ensure_children(root, {"authors", "commentList"}, sheet)
        authors = root.find(tag("authors"))
        if authors is not None:
            for index, author in enumerate(authors):
                if author.tag != tag("author") or len(author):
                    raise WorkbookError(f"{sheet} 含无法处理的批注作者结构")
                # The author can be edited or replaced during review.
                self.fields.append(Field(sheet, f"批注作者 {index + 1}", author.text or "", author, "author"))
        for comment in root.iter(tag("comment")):
            addr = comment.get("ref", "")
            location = f"{sheet}!{addr}"
            ensure_children(comment, {"text"}, location)
            text = comment.find(tag("text"))
            if text is None or not ADDRESS.fullmatch(addr):
                raise WorkbookError(f"{location} 的批注位置无效")
            ensure_children(text, {"t", "r"}, location)
            for run in text.findall(tag("r")):
                ensure_children(run, {"rPr", "t"}, location)
            self.fields.append(Field(sheet, f"{addr} 批注", plain_text(text), text, "text"))

    def review(self, suggestions, policy):
        return {
            "policy": policy, "sheets": self.sheet_names, "hidden_sheets": self.hidden_sheets,
            "formula_count": len(self.formulas),
            "fields": [{"id": index, "sheet": f.sheet, "address": f.location,
                        "text": f.text, "suggestion": suggestions.get(index, ""),
                        "amountCell": self.amount_cell(f.sheet, f.location)}
                       for index, f in enumerate(self.fields) if f.text] + [
                           {"sheet": sheet, "address": f"{address} 公式",
                            "text": f"={formula.text}  ·  保存的结果："
                                    f"{cached.text if cached is not None and cached.text is not None else '无'}",
                            "readonly": True, "cell": address,
                            "amountCell": self.amount_cell(sheet, address)}
                           for sheet, address, formula, cached in self.formulas
                       ],
        }

    def remove_part(self, name):
        if self.parts.pop(name, None) is None:
            return
        for rel, links in self.links.items():
            for node in list(self.parts[rel]):
                if links.get(node.get("Id")) == name:
                    self.parts[rel].remove(node)
        content = self.parts["[Content_Types].xml"]
        for node in list(content):
            if node.tag == field_tag(CONTENT, "Override") and node.get("PartName") == "/" + name:
                content.remove(node)

    def export(self, edits, acknowledged, formula_policy=None):
        if not acknowledged:
            raise WorkbookError("请先确认已复核所有工作表（包括隐藏工作表）")
        if not isinstance(edits, dict):
            raise WorkbookError("修改内容无效")
        changes = {}
        for key, replacement in edits.items():
            if not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,6}", key):
                raise WorkbookError("单元格编号无效")
            index = int(key)
            if index >= len(self.fields) or not isinstance(replacement, str) or len(replacement) > 32767:
                raise WorkbookError("单元格修改内容无效")
            if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", replacement):
                raise WorkbookError("单元格修改含无效字符")
            if self.fields[index].text != replacement:
                changes[index] = replacement
        if self.formulas and formula_policy is None:
            sheet, address, _, _ = self.formulas[0]
            raise WorkbookError(f"{sheet}!{address}：当前路径没有启用公式处理")
        if formula_policy is not None:
            formula_policy(self, changes)
        for index, replacement in changes.items():
            self.replace(self.fields[index], replacement)
        self.remove_part("xl/sharedStrings.xml")
        self.remove_part("docProps/core.xml")
        self.remove_part("docProps/app.xml")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, part in self.parts.items():
                archive.writestr(name, XML.tostring(part, encoding="utf-8", xml_declaration=True))
        return out.getvalue()

    @staticmethod
    def replace(field, value):
        node = field.node
        if field.kind == "author":
            node.text = value
        elif field.kind == "text":
            nodes = text_nodes(node)
            first = nodes[0] if nodes else XML.SubElement(node, tag("t"))
            first.set(XML_SPACE, "preserve")
            first.text = value
            for other in nodes[1:]:
                other.text = ""
        else:
            original = node.find(tag("v"))
            node.remove(original)
            node.set("t", "inlineStr")
            text = XML.SubElement(XML.SubElement(node, tag("is")), tag("t"))
            text.set(XML_SPACE, "preserve")
            text.text = value
