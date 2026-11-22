"""Parse the supported formula subset and validate dependencies without evaluating."""
import re

from lxml import etree as XML

from contract_xlsx import ADDRESS, WorkbookError, tag


TOKEN = re.compile(
    r"\s*(?:(?P<quoted>'(?:[^']|'')+'!)|(?P<sheet>[\w.]+!)|"
    r'(?P<string>"(?:[^"]|"")*")|(?P<ref>\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6})(?![\w.])|'
    r"(?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)|"
    r"(?P<name>[A-Za-z_][\w.]*)|(?P<op>[()+*/^%:&,-]))"
)
SEPARATORS = re.compile(r"[ \t,./:;\-·，、；：]*")
PRECEDENCE = {"&": 1, "+": 2, "-": 2, "*": 3, "/": 3, "^": 4}


def coordinates(address):
    match = re.fullmatch(r"([A-Z]+)([0-9]+)", address)
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise ValueError("引用位置超出工作表范围")
    return column, row


def column_label(number):
    label = ""
    while number:
        number, letter = divmod(number - 1, 26)
        label = chr(65 + letter) + label
    return label


class Formula:
    def __init__(self, text, sheet, names):
        if len(text) > 4096:
            raise ValueError("公式过长")
        self.tokens = []
        self.current_sheet = sheet
        self.names = names
        pos = 0
        text = text.rstrip()
        while pos < len(text):
            match = TOKEN.match(text, pos)
            if not match:
                raise ValueError("含暂不支持的公式语法")
            self.tokens.append((match.lastgroup, match[match.lastgroup]))
            if len(self.tokens) > 120:
                raise ValueError("公式复杂度超过支持范围")
            pos = match.end()
        self.pos = 0
        self.expanded = len(self.tokens)
        self.tree = self.expression()
        if self.peek()[0]:
            raise ValueError("含暂不支持的公式运算")

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ("", "")

    def pop(self):
        token = self.peek()
        self.pos += 1
        return token

    def expect(self, expected):
        if self.pop()[1] != expected:
            raise ValueError("公式结构无效")

    def reference(self, current_sheet):
        kind, value = self.pop()
        if kind in ("quoted", "sheet"):
            raw_sheet = value[1:-2].replace("''", "'") if kind == "quoted" else value[:-1]
            current_sheet = self.names.get(raw_sheet.casefold())
            kind, value = self.pop()
        address = value.replace("$", "").upper()
        if not current_sheet or kind != "ref" or not ADDRESS.fullmatch(address):
            raise ValueError("工作表或单元格引用无效")
        coordinates(address)
        return current_sheet, address

    def expression(self, minimum=1):
        left = self.operand()
        while self.peek()[1] == "%":
            self.pop()
            left = ("%", (left,))
        while PRECEDENCE.get(self.peek()[1], 0) >= minimum:
            operator = self.pop()[1]
            right = self.expression(PRECEDENCE[operator] + 1)
            left = ("join" if operator == "&" else operator, (left, right))
        return left

    def operand(self):
        kind, value = self.peek()
        if kind in ("ref", "quoted", "sheet"):
            return ("ref", self.reference(self.current_sheet))
        self.pop()
        if kind == "num":
            return ("number", value)
        if kind == "string":
            if not SEPARATORS.fullmatch(value[1:-1].replace('""', '"')):
                raise ValueError("公式内嵌文本需要先移出公式并作为单元格内容复核")
            return ("separator", ())
        if value in ("+", "-"):
            return (value, (self.operand(),))
        if value == "(":
            expression = self.expression()
            self.expect(")")
            return expression
        if kind == "name" and value.upper() == "SUM":
            return ("sum", self.sum_args())
        raise ValueError("仅支持同簿引用、基本算术、SUM 与文本连接")

    def sum_args(self):
        self.expect("(")
        args = []
        while True:
            item = self.expression()
            if self.peek()[1] == ":" and item[0] == "ref":
                self.pop()
                first_sheet, first = item[1]
                last_sheet, last = self.reference(first_sheet)
                x1, y1 = coordinates(first)
                x2, y2 = coordinates(last)
                if first_sheet != last_sheet or x2 < x1 or y2 < y1 or (x2-x1+1)*(y2-y1+1) > 10000:
                    raise ValueError("SUM 区间超出支持范围")
                self.expanded += (x2-x1+1)*(y2-y1+1)
                args.extend(("ref", (first_sheet, column_label(x) + str(y)))
                            for x in range(x1, x2+1) for y in range(y1, y2+1))
            else:
                args.append(item)
            if len(args) > 10000:
                raise ValueError("SUM 引用单元格过多")
            if self.peek()[1] != ",":
                self.expect(")")
                return tuple(args)
            self.pop()


def parse_formulas(workbook):
    names = {name.casefold(): name for name in workbook.sheet_names}
    formulas = {}
    expanded = 0
    for sheet, address, formula, _ in workbook.formulas:
        try:
            if formula.attrib or not formula.text:
                raise ValueError("暂不支持共享、动态数组或数据表公式")
            parsed = Formula(formula.text, sheet, names)
            expanded += parsed.expanded
            if expanded > 100000:
                raise ValueError("工作簿公式引用过多")
            formulas[sheet, address] = parsed.tree
        except (ValueError, RecursionError) as exc:
            raise WorkbookError(f"{sheet}!{address}：{exc}", cell=(sheet, address),
                                advice="在原工作簿中修改该公式或其引用，再重新上传。当前支持同簿引用、基本运算和 SUM 等有限公式。",
                                code="XLSX_FORMULA_UNSUPPORTED") from exc
    return formulas


def validate(workbook, changes, clear_cache=False, cache_checks=None, formulas=None):
    changed = {(workbook.fields[index].sheet, workbook.fields[index].location)
               for index in changes if workbook.fields[index].kind != "author"}
    if formulas is None:
        formulas = parse_formulas(workbook)
    known = {}

    def inspect(tree, visiting, location):
        kind, inputs = tree
        if kind == "ref":
            if inputs in formulas:
                return visit(inputs, visiting)
            cell = workbook.cells.get(inputs)
            cell_type = cell.get("t", "n") if cell is not None else "n"
            return ("number" if cell_type in {"n", "d", "b"} else "text",
                    inputs if inputs in changed else None)
        if kind in ("number", "separator"):
            return kind, None
        children = [inspect(child, visiting, location) for child in inputs]
        affected = next((modified for _, modified in children if modified is not None), None)
        if kind != "join":
            if affected:
                amount = workbook.amount_cell(*affected)
                advice = ("如果要隐藏这笔金额，请将该格改用演示金额。若要保留原金额，撤销该格的文字替换后再导出。"
                          if amount else "定位后撤销该格的文字替换，或在原工作簿中调整输入与公式，再重新上传。")
                raise WorkbookError(
                    f"{'!'.join(location)} 的数值计算引用了 {'!'.join(affected)}。把该输入替换成文字会破坏计算。",
                    cell=affected, advice=advice, code="XLSX_AMOUNT_USE_DEMO" if amount else "XLSX_FORMULA_TEXT_INPUT")
            if any(output != "number" for output, _ in children):
                raise ValueError("数值公式引用文本或未知值，无法核对计算语义")
            return "number", None
        return "text", affected

    def visit(location, visiting):
        if location not in known:
            if location in visiting or len(visiting) >= 64:
                raise ValueError("公式循环引用或依赖链过长")
            visiting.add(location)
            known[location] = inspect(formulas[location], visiting, location)
            visiting.remove(location)
        return known[location]

    for location in formulas:
        try:
            visit(location, set())
        except WorkbookError:
            raise
        except (ValueError, RecursionError) as exc:
            raise WorkbookError(f"{'!'.join(location)}：{exc}", cell=location,
                                advice="核对该公式和引用单元格；修改原工作簿后重新上传。",
                                code="XLSX_FORMULA_INPUT_INVALID") from exc
    if cache_checks:
        field_map = {(field.sheet, field.location): field for field in workbook.fields}
        modified = {(workbook.fields[index].sheet, workbook.fields[index].location): replacement
                    for index, replacement in changes.items()}
        for location, secrets in cache_checks.items():
            seen, refs, pending = {location}, set(), [formulas[location]]
            while pending:
                kind, inputs = pending.pop()
                if kind == "ref":
                    if inputs not in formulas:
                        refs.add(inputs)
                    elif inputs not in seen:
                        seen.add(inputs)
                        pending.append(formulas[inputs])
                elif kind not in {"number", "separator"}:
                    pending.extend(inputs)
            originals = {ref: field_map[ref].text for ref in refs if ref in field_map}
            for secret in secrets:
                if (not any(secret in value and ref in modified
                            for ref, value in originals.items()) or
                    any(secret in modified.get(ref, value) for ref, value in originals.items())):
                    raise WorkbookError(
                        f"{'!'.join(location)}：旧公式结果含敏感信息，当前选择无法确认重算后会消除这些信息。",
                        cell=location, advice="定位该公式，核对它引用的源单元格，将对应原文纳入脱敏后重试。",
                        code="XLSX_FORMULA_CACHE_RESIDUE")
    if clear_cache:
        clear_formula_cache(workbook)


def clear_formula_cache(workbook):
    if workbook.formulas:
        for sheet, address, _, cached in workbook.formulas:
            if cached is not None:
                workbook.cells[sheet, address].remove(cached)
        workbook.remove_part("xl/calcChain.xml")
        root = workbook.parts["xl/workbook.xml"]
        calc = root.find(tag("calcPr"))
        if calc is None:
            calc = XML.SubElement(root, tag("calcPr"))
        calc.set("calcMode", "auto")
        calc.set("fullCalcOnLoad", "1")
        calc.set("forceFullCalc", "1")
