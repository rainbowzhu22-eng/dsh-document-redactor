"""Review and replace linked amounts with independent numeric examples."""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, DecimalException
import hashlib
import json
import re
from secrets import randbelow

from lxml import etree as XML

from contract_xlsx import WorkbookError, tag
from contract_xlsx_formula import parse_formulas


NUMBER_IN_TEXT = re.compile(
    r"(?<![0-9.])-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?(?![0-9.])"
)
MAX_AMOUNT = Decimal("100000000000")


def number(value):
    parsed = Decimal(value)
    if not parsed.is_finite() or abs(parsed) > MAX_AMOUNT:
        raise ValueError("金额超出支持范围")
    return parsed


def nodes(tree):
    kind, args = tree
    yield tree
    if kind not in {"ref", "number", "separator"}:
        for child in args:
            yield from nodes(child)


class AmountPlan:
    def __init__(self, workbook, selected):
        self.book = workbook
        if not isinstance(selected, list) or not 1 <= len(selected) <= 4000:
            raise WorkbookError("请至少选择一个要隐藏的金额单元格，每次最多 4,000 个。",
                                advice="选择金额列或单元格后点击「分析金额范围」。",
                                code="XLSX_AMOUNT_SELECTION_REQUIRED")
        targets = set()
        for row in selected:
            if not isinstance(row, list) or len(row) != 2 or not all(isinstance(value, str) for value in row):
                raise WorkbookError("无法识别金额选区。", advice="返回复核页重新选择金额范围。")
            key = tuple(row)
            if not workbook.amount_cell(*key):
                location = key if key in workbook.cells else None
                raise WorkbookError("金额选择中包含无法替换的单元格。",
                                    cell=location, advice="仅选择数值金额或金额公式。去掉日期、百分比和文本格后重新分析。",
                                    code="XLSX_AMOUNT_SELECTION_INVALID")
            targets.add(key)
        self.formulas = parse_formulas(workbook)
        refs = {key: {args for kind, args in nodes(tree) if kind == "ref"}
                for key, tree in self.formulas.items()}
        users = defaultdict(set)
        for formula, inputs in refs.items():
            for source in inputs:
                users[source].add(formula)
        self.inputs, self.results = set(), set()
        seen, pending = set(), list(targets)
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            if key in self.formulas:
                label = "!".join(key)
                kinds = {kind for kind, _ in nodes(self.formulas[key])}
                if "number" in kinds:
                    raise WorkbookError(f"{label}：公式内的数字可能包含原金额。",
                                        cell=key, advice="把公式内的数字移到可复核的输入格，再重新上传并选择金额范围。",
                                        code="XLSX_AMOUNT_FORMULA_LITERAL")
                if kinds - {"ref", "+", "-", "*", "sum"}:
                    raise WorkbookError(f"{label}：联动公式使用了演示金额模式暂未支持的运算。",
                                        cell=key, advice="调整原工作簿后重新上传。此模式支持同簿引用、加减乘和 SUM。",
                                        code="XLSX_AMOUNT_FORMULA_UNSUPPORTED")
                self.results.add(key)
                pending.extend(refs[key])
            elif workbook.amount_cell(*key):
                value = workbook.cells[key].findtext(tag("v"))
                if value is not None:
                    self.inputs.add(key)
                elif key in targets:
                    raise WorkbookError(f"{'!'.join(key)}：所选金额单元格为空")
            elif key in workbook.cells:
                item = workbook.cells[key]
                # A percentage or numeric Excel date is a numeric factor, not pay.
                if item.get("t", "n") != "n" or item.get("s") not in workbook.percent_styles | workbook.date_styles:
                    raise WorkbookError(f"{'!'.join(key)}：联动公式引用的输入不是可识别的数值。",
                                        cell=key, advice="在原工作簿中核对该格是否为数字或百分比，修正后重新上传。",
                                        code="XLSX_AMOUNT_INPUT_INVALID")
                continue
            pending.extend(users[key])
        if not self.inputs or len(self.inputs) + len(self.results) > 10000:
            raise WorkbookError("无法确认金额输入，或联动范围过大，请拆分工作簿")
        if "xl/styles.xml" not in self.book.parts:
            raise WorkbookError("缺少工作簿样式，无法设置金额显示")
        manifest = [sorted(targets), sorted(self.inputs), sorted(self.results)]
        self.digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False).encode()).hexdigest()

    def preview(self):
        return {"id": self.digest, "inputs": sorted(self.inputs), "formulas": sorted(self.results)}

    def values(self, replacements):
        cache = dict(replacements)

        def visit(key, depth):
            if key in cache:
                return cache[key]
            if depth > 64:
                raise ValueError("金额公式依赖过深或循环引用")
            if key in self.formulas:
                value = evaluate(self.formulas[key], depth + 1)
            else:
                cell = self.book.cells.get(key)
                raw = cell.findtext(tag("v")) if cell is not None else None
                value = number(raw) if raw is not None else Decimal(0)
            cache[key] = number(value)
            return cache[key]

        def evaluate(tree, depth):
            kind, args = tree
            if kind == "ref":
                return visit(args, depth)
            values = [evaluate(item, depth) for item in args]
            if kind == "sum":
                return sum(values, Decimal(0))
            if kind == "+":
                return sum(values)
            if kind == "-":
                return values[0] - values[1] if len(values) == 2 else -values[0]
            return values[0] * values[1]

        for key in sorted(self.inputs | self.results):
            try:
                visit(key, 0)
            except (ValueError, DecimalException, RecursionError) as exc:
                raise WorkbookError(f"{'!'.join(key)}：金额结果无法安全重算。",
                                    cell=key, advice="检查相关数值格式、循环引用和运算范围，修正原工作簿后重新上传。",
                                    code="XLSX_AMOUNT_RECALC_FAILED") from exc
        return cache

    def apply(self, changes):
        original = self.values({})
        protected = {original[key] for key in self.inputs | self.results} - {Decimal(0)}
        for sheet, address, _, cached in self.book.formulas:
            if (sheet, address) in self.results and cached is not None and cached.text:
                try:
                    cached_value = number(cached.text)
                except (ValueError, DecimalException):
                    continue
                if cached_value:
                    protected.add(cached_value)

        def find_residue(value, location, cell=None):
            for match in NUMBER_IN_TEXT.finditer(value):
                try:
                    amount = number(match[0].replace(",", ""))
                except (ValueError, DecimalException):
                    continue
                if amount in protected:
                    raise WorkbookError(f"{location}：仍有与原金额一致的值。",
                                        cell=cell, advice="核对是否还有金额副本。将相关数值格加入金额范围，或对文本备注中的金额做文字替换。",
                                        code="XLSX_AMOUNT_COPY_FOUND")

        for index, field in enumerate(self.book.fields):
            key = field.sheet, field.location
            if key in self.inputs:
                if index in changes:
                    raise WorkbookError(f"{'!'.join(key)}：同一格同时设置了文字替换和演示金额。",
                                        cell=key, advice="点击「撤销文字替换」，保留已确认的演示金额范围，再重新导出。",
                                        code="XLSX_AMOUNT_EDIT_CONFLICT")
                continue
            if (field.kind == "date" or
                    (key in self.book.cells and self.book.cells[key].get("s") in self.book.percent_styles)):
                continue
            find_residue(changes.get(index, field.text), "!".join(key), key)
        for name in self.book.sheet_names:
            find_residue(name, f"工作表名称「{name}」")
        for style in self.book.parts["xl/styles.xml"].iter(tag("numFmt")):
            for literal in re.findall(r'"([^"]+)"', style.get("formatCode", "")):
                find_residue(literal, "单元格自定义格式")
        for key, tree in self.formulas.items():
            for kind, value in nodes(tree):
                if kind == "number":
                    try:
                        parsed = number(value)
                    except (ValueError, DecimalException):
                        continue
                    if parsed in protected:
                        raise WorkbookError(f"{'!'.join(key)}：公式中仍有与原金额一致的数值。",
                                            cell=key, advice="在原工作簿中把这笔金额移到输入格，再重新上传并加入金额范围。",
                                            code="XLSX_AMOUNT_FORMULA_RESIDUE")

        # Use ranges independent of the source amounts. Pay headers keep the
        # demonstration credible while all underlying values remain synthetic.
        labels = {(field.sheet, field.location): field.text for field in self.book.fields}
        ordered = sorted(self.inputs)
        for _ in range(8):
            generated = {}
            for sheet, address in ordered:
                column = address.rstrip("0123456789")
                header = labels.get((sheet, column + "1"), "")
                low, high = (9000, 15000) if ("基本工资" in header or "底薪" in header) else (100, 600)
                generated[sheet, address] = Decimal(low * 100 + randbelow((high - low) * 100)) / 100
            verified = self.values(generated)
            if all(verified[key] not in protected for key in self.inputs | self.results):
                break
        else:
            raise WorkbookError("未能生成与原金额区分的演示数据，请重试")
        for key, replacement in generated.items():
            self.book.cells[key].find(tag("v")).text = format(replacement, "f")
        self.mask_formats()

    def mask_formats(self):
        styles = self.book.parts["xl/styles.xml"]
        formats = styles.find(tag("cellXfs"))
        if formats is None or not len(formats):
            raise WorkbookError("工作簿缺少可保留的单元格样式")
        custom = styles.find(tag("numFmts"))
        if custom is None:
            custom = XML.Element(tag("numFmts"))
            styles.insert(0, custom)
        used = {node.get("numFmtId") for node in custom}
        number_id = next((str(i) for i in range(164, 65536) if str(i) not in used), None)
        if number_id is None:
            raise WorkbookError("自定义数字格式已达到上限")
        XML.SubElement(custom, tag("numFmt"), numFmtId=number_id,
                       formatCode='"已脱敏";"已脱敏";"已脱敏";"已脱敏"')
        custom.set("count", str(len(custom)))
        source_count = len(formats)
        mapping = {}
        for key in sorted(self.inputs | self.results):
            cell = self.book.cells[key]
            current = int(cell.get("s", "0"))
            if not 0 <= current < source_count:
                raise WorkbookError(f"{'!'.join(key)}：单元格样式索引无效")
            if current not in mapping:
                style = deepcopy(formats[current])
                style.set("numFmtId", number_id)
                style.set("applyNumberFormat", "1")
                mapping[current] = str(len(formats))
                formats.append(style)
            cell.set("s", mapping[current])
        formats.set("count", str(len(formats)))
