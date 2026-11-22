import io
import json
from pathlib import Path
import re
import sys
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core/scripts"))
from contract_app_server import _xlsx_suggestions
from contract_xlsx import Workbook, WorkbookError, MAIN, DOC_REL, PKG_REL, CONTENT, tag
from contract_xlsx_amounts import AmountPlan
from contract_xlsx_formula import clear_formula_cache, validate
from contract_xlsx_ui import page


def fixture(formula=None, external=False):
    def worksheet(rows, extra=""):
        return (f'<worksheet xmlns="{MAIN}"><cols><col min="4" max="4" hidden="1" width="10"/></cols>'
                f'<sheetData>{rows}</sheetData>{extra}</worksheet>')

    def rels(*links):
        xml = "".join(
            f'<Relationship Id="rId{i}" Target="{target}" '
            f'Type="{PKG_REL if kind.startswith("metadata/") else DOC_REL}/{kind}"/>'
            for i, (target, kind) in enumerate(links, 1))
        if external and links[0][1] == "comments":
            xml += (f'<Relationship Id="rId9" Target="https://example.invalid/test.xlsx" '
                    f'Type="{DOC_REL}/externalLinkPath" TargetMode="External"/>')
        return f'<Relationships xmlns="{PKG_REL}">{xml}</Relationships>'

    parts = {
        "_rels/.rels": rels(("xl/workbook.xml", "officeDocument"), ("docProps/core.xml", "metadata/core-properties")),
        "docProps/core.xml": '<coreProperties xmlns="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"><lastModifiedBy>PRIVATE_AUTHOR</lastModifiedBy></coreProperties>',
        "xl/workbook.xml": (f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheets>'
                           '<sheet name="明细" sheetId="1" r:id="rId1"/>'
                           '<sheet name="汇总" state="hidden" sheetId="2" r:id="rId2"/>'
                           '</sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": rels(("worksheets/sheet1.xml", "worksheet"), ("worksheets/sheet2.xml", "worksheet"),
                                          ("sharedStrings.xml", "sharedStrings"), ("styles.xml", "styles")),
        "xl/worksheets/sheet1.xml": worksheet(
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>125</v></c><c r="C1" s="1"><v>45000</v></c></row>'
            '<row r="2" hidden="1"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>50</v></c></row>',
            '<mergeCells count="1"><mergeCell ref="D3:E3"/></mergeCells>'),
        "xl/worksheets/_rels/sheet1.xml.rels": rels(("../comments1.xml", "comments")),
        "xl/comments1.xml": (f'<comments xmlns="{MAIN}"><authors><author>PRIVATE_AUTHOR</author></authors>'
                             '<commentList><comment ref="A1" authorId="0"><text><t>电话 13800138000</t></text>'
                             '</comment></commentList></comments>'),
        "xl/worksheets/sheet2.xml": worksheet(
            '<row r="1"><c r="A1" t="str"><f>' + formula +
            '</f><v>13800138000</v></c><c r="B1"><f>SUM(明细!B1:B2)</f><v>175</v></c></row>'
            if formula else '<row r="1"><c r="A1" t="inlineStr"><is><t>保留正文</t></is></c></row>'),
        "xl/styles.xml": (f'<styleSheet xmlns="{MAIN}"><cellXfs count="2"><xf numFmtId="0"/>'
                          '<xf numFmtId="14"/></cellXfs></styleSheet>'),
        "xl/sharedStrings.xml": (f'<sst xmlns="{MAIN}"><si><r><t>13800</t></r><r><t>138000</t></r></si>'
                                 '<si><t>0012</t></si><si><t>OLD_UNUSED_TEXT</t></si></sst>'),
    }
    types = {"xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
             "xl/worksheets/sheet1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
             "xl/worksheets/sheet2.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"}
    parts["[Content_Types].xml"] = (f'<Types xmlns="{CONTENT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>' +
        "".join(f'<Override PartName="/{part}" ContentType="{kind}"/>' for part, kind in types.items()) + '</Types>')
    out = io.BytesIO()
    with ZipFile(out, "w") as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return out.getvalue()


def field_id(book, sheet, location):
    return next(i for i, item in enumerate(book.fields)
                if item.sheet == sheet and item.location == location)


class SpreadsheetCandidate(unittest.TestCase):
    def test_regular_workbook_redacts_shared_strings_comments_and_props(self):
        raw = fixture()
        book = Workbook(raw)
        self.assertEqual(book.hidden_sheets, ["汇总"])
        self.assertEqual(book.fields[field_id(book, "明细", "A2")].text, "0012")
        export = book.export({str(field_id(book, "明细", "A1")): "=plain text",
                              str(field_id(book, "明细", "A1 批注")): "已编辑",
                              str(field_id(book, "明细", "批注作者 1")): "作者"}, True)
        reopened = Workbook(export)
        self.assertEqual(reopened.fields[field_id(reopened, "明细", "A1")].text, "=plain text")
        self.assertFalse(reopened.formulas)
        self.assertEqual(reopened.cells["明细", "C1"].get("s"), "1")
        sheet = reopened.parts["xl/worksheets/sheet1.xml"]
        self.assertEqual(sheet.find(tag("mergeCells"))[0].get("ref"), "D3:E3")
        self.assertEqual(sheet.find(tag("sheetData"))[1].get("hidden"), "1")
        self.assertEqual(sheet.find(tag("cols"))[0].get("hidden"), "1")
        with ZipFile(io.BytesIO(export)) as zip_file:
            xml = b"".join(zip_file.read(path) for path in zip_file.namelist())
        for secret in (b"13800138000", b"PRIVATE_AUTHOR", b"OLD_UNUSED_TEXT"):
            self.assertNotIn(secret, xml)
        self.assertEqual(Workbook(raw).fields[0].text, "13800138000")

    def test_review_has_date_policy_and_candidate_suggestions(self):
        book = Workbook(fixture())
        snapshot = {"extra_specs": [], "type_overrides": {}}
        hidden, _ = _xlsx_suggestions(book, snapshot, "privacy_first", "fictional.xlsx")
        kept, _ = _xlsx_suggestions(book, snapshot, "preserve_transaction", "fictional.xlsx")
        self.assertEqual(hidden[field_id(book, "明细", "C1")], "[日期]")
        self.assertNotIn(field_id(book, "明细", "C1"), kept)
        self.assertIn(field_id(book, "明细", "A1 批注"), kept)
        payload = book.review(kept, "preserve_transaction")
        payload.update(filename="</script><script>alert(1)</script>", policyLabel="保留")
        review_page = page("123456789abc", payload).decode()
        stored = re.search(r'id="payload">(.*?)</script>', review_page, re.S)[1]
        self.assertNotIn("<script>", stored)
        self.assertEqual(json.loads(stored)["filename"], payload["filename"])

    def test_preserves_supported_formulas_and_clears_caches(self):
        book = Workbook(fixture("明细!A1"))
        validate(book, {})
        replacement = str(field_id(book, "明细", "A1"))
        out = book.export({replacement: "[电话]"}, True, lambda w, e: validate(
            w, e, clear_cache=True, cache_checks={("汇总", "A1"): {"13800138000"}}))
        loaded = Workbook(out)
        self.assertEqual([f.text for _, _, f, _ in loaded.formulas], ["明细!A1", "SUM(明细!B1:B2)"])
        self.assertTrue(all(cached is None for _, _, _, cached in loaded.formulas))
        self.assertEqual(loaded.parts["xl/workbook.xml"].find(tag("calcPr")).get("fullCalcOnLoad"), "1")

    def test_blocks_numeric_dependency_change_and_unreviewed_cache(self):
        workbook = Workbook(fixture("明细!A1"))
        with self.assertRaisesRegex(WorkbookError, "汇总!B1.*数值计算.*明细!B2") as error:
            validate(workbook, {field_id(workbook, "明细", "B2"): "[金额]"})
        details = error.exception.response()
        self.assertEqual(details["cell"], {"sheet": "明细", "address": "B2"})
        self.assertEqual(details["code"], "XLSX_AMOUNT_USE_DEMO")
        self.assertIn("撤销该格的文字替换", details["advice"])
        with self.assertRaisesRegex(WorkbookError, "汇总!A1.*敏感信息"):
            validate(workbook, {}, clear_cache=True, cache_checks={("汇总", "A1"): {"13800138000"}})

    def test_blocks_external_links_and_locates_unhandled_formula(self):
        with self.assertRaisesRegex(WorkbookError, "外部链接"):
            Workbook(fixture(external=True))
        for formula in ('IF(明细!A1="张三",1,0)', "'[other.xlsx]名单'!A1", '"13800138000"', "A1"):
            with self.subTest(formula=formula), self.assertRaisesRegex(WorkbookError, "汇总!A1"):
                validate(Workbook(fixture(formula)), {})

    def test_amount_preview_synthesizes_numeric_dependencies_and_masks_format(self):
        workbook = Workbook(fixture("明细!A1"))
        plan = AmountPlan(workbook, [["汇总", "B1"]])
        self.assertEqual(plan.preview()["inputs"], [("明细", "B1"), ("明细", "B2")])
        self.assertEqual(plan.preview()["formulas"], [("汇总", "B1")])

        def apply(book, edits):
            validate(book, edits, formulas=plan.formulas)
            plan.apply(edits)
            clear_formula_cache(book)

        exported = Workbook(workbook.export({}, True, apply))
        self.assertEqual(exported.cells["明细", "B1"].get("t"), None)
        self.assertNotEqual(exported.cells["明细", "B1"].findtext(tag("v")), "125")
        self.assertNotEqual(exported.cells["明细", "B2"].findtext(tag("v")), "50")
        self.assertEqual(exported.cells["汇总", "B1"].findtext(tag("f")), "SUM(明细!B1:B2)")
        self.assertIsNone(exported.cells["汇总", "B1"].find(tag("v")))
        self.assertEqual(exported.cells["明细", "C1"].findtext(tag("v")), "45000")
        self.assertIn("已脱敏", exported.parts["xl/styles.xml"].find(tag("numFmts"))[0].get("formatCode"))

    def test_amount_preview_rejects_date_targets_and_embedded_constants(self):
        workbook = Workbook(fixture("SUM(明细!B1:B2)+9000"))
        with self.assertRaisesRegex(WorkbookError, "无法替换"):
            AmountPlan(workbook, [["明细", "C1"]])
        with self.assertRaisesRegex(WorkbookError, "汇总!A1.*公式内的数字") as error:
            AmountPlan(workbook, [["明细", "B1"]])
        self.assertEqual(error.exception.response()["cell"], {"sheet": "汇总", "address": "A1"})
        self.assertIn("移到可复核的输入格", error.exception.response()["advice"])

    def test_amount_export_blocks_text_conflicts_and_original_amount_copies(self):
        workbook = Workbook(fixture("明细!A1"))
        plan = AmountPlan(workbook, [["汇总", "B1"]])
        with self.assertRaisesRegex(WorkbookError, "明细!B1.*文字替换"):
            plan.apply({field_id(workbook, "明细", "B1"): "[金额]"})
        with self.assertRaisesRegex(WorkbookError, "明细!A1 批注.*原金额"):
            plan.apply({field_id(workbook, "明细", "A1 批注"): "旧基本工资为125元"})

    def test_requires_review_and_valid_edits(self):
        workbook = Workbook(fixture())
        with self.assertRaisesRegex(WorkbookError, "请先确认"):
            workbook.export({}, False)
        with self.assertRaisesRegex(WorkbookError, "修改内容无效"):
            workbook.export({"40001": "invalid"}, True)


if __name__ == "__main__":
    unittest.main()
