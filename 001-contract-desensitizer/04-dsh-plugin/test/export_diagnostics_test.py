"""Selected export failures exercised through the local app's request handlers."""
from email.message import Message
import errno
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
sys.path.insert(0, str(CORE / "scripts"))

import contract_app_server as app
import contract_pdf_project as pdf
import content_controls_test as controls
from lxml import etree


class ExportDiagnostics(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="redaction-export-errors-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def request(self, path, body=None, info=None, *, navigate=False):
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        handler = object.__new__(app.AppHandler)
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = "localhost"
        handler.headers["X-Local-Project"] = "1"
        if body is not None:
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(raw))
        if navigate:
            handler.headers["Sec-Fetch-Mode"] = "navigate"
        handler.rfile = io.BytesIO(raw)
        handler.connection = SimpleNamespace(gettimeout=lambda: None, settimeout=lambda _: None)
        handler.state = SimpleNamespace(work_dir=str(self.root), get=lambda _: info)
        handler._send = lambda status, kind, data, **_: response.append((status, kind, data))
        handler._send_html = lambda html, status: response.append((status, "text/html", html))
        response = []
        (handler.do_POST if body is not None else handler.do_GET)()
        status, content_type, data = response[0]
        return status, data if content_type == "text/html" else json.loads(data)

    def test_word_header_content_control_reports_part_and_preserves_existing_export(self):
        fixture = controls.ContentControls()
        fixture.work = self.root
        source, part, root = fixture.fixture(location="header")
        etree.SubElement(root.find(".//" + controls.W + "sdtPr"), controls.W + "dataBinding")
        fixture.rewrite_part(source, part, root)
        original = source.read_bytes()
        output = self.root / "example" / "out" / f"{source.stem}_脱敏.docx"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"previous validated export")
        info = {"input_path": str(source), "mode": "redact", "doc": app.D.load_document(str(source)),
                "report": {"items": []}, "local_terms": [{"label": "测试字段", "values": [controls.TOKEN]}]}
        with patch.object(app, "_active_rules_path", return_value=None):
            status, response = self.request("/api/apply/example", {}, info)
        self.assertEqual((status, response["code"]), (409, "DOCX_CONTENT_CONTROL_UNSUPPORTED"))
        self.assertEqual(response["location"],
                         {"part": part, "section": "页眉", "kind": "内容控件", "ordinal": 1})
        self.assertIn("副本", response["advice"])
        self.assertNotIn(controls.TOKEN, json.dumps(response, ensure_ascii=False))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(output.read_bytes(), b"previous validated export")

    def test_text_and_markdown_distinguish_invalid_input_from_failed_write(self):
        for extension in ("txt", "md"):
            with self.subTest(extension=extension):
                source = self.root / f"source.{extension}"
                source.write_text("测试文书", encoding="utf-8")
                info = {"input_path": str(source), "doc": app.D.load_document(str(source)),
                        "report": {"items": []}}
                status, invalid = self.request("/api/export-plan/example", [], info)
                self.assertEqual((status, invalid["code"]), (400, "EXPORT_REQUEST_INVALID"))
                self.assertEqual(invalid["stage"], "导出前检查")
                with patch.object(app.os, "makedirs", side_effect=OSError(errno.ENOSPC, "no space")):
                    status, failed = self.request("/api/apply/example", {}, info)
                self.assertEqual((status, failed["code"]), (500, "EXPORT_WRITE_FAILED"))
                self.assertIn("空间不足", failed["error"])

    def test_pdf_scan_blocker_passes_page_to_the_review(self):
        project = {"stage": "review", "rules_sha256": app._sha256_bytes(b"")}
        report = {"hard": ["图像检测未完成"], "hard_issues": [{
            "code": "PDF_IMAGE_DETECTION_FAILED", "pages": [2, 5], "action": "重试图像检测。"}]}
        with patch.object(pdf, "load", return_value=project), patch.object(pdf, "_rules_snapshot"), \
                patch.object(app, "_read_rules_file_bytes", return_value=b""), \
                patch.object(pdf, "_export_report_project", return_value=report):
            status, result = self.request("/api/pdf/export/abcdef012345", {"confirm": True})
        self.assertEqual((status, result["code"]), (400, "PDF_DISPOSITION_REQUIRED"))
        self.assertEqual(result["location"], {"pages": [2, 5]})
        self.assertIn("重试图像检测", result["advice"])

    def test_restore_and_browser_download_describe_recovery(self):
        status, restore = self.request("/api/restore/example", {})
        self.assertEqual((status, restore["code"], restore["stage"]),
                         (404, "EXPORT_SESSION_EXPIRED", "还原文书"))
        for route in ("/download", "/history/download"):
            with self.subTest(route=route):
                url = f"{route}/example/missing.docx"
                status, download = self.request(url, info={"mode": "redact"})
                self.assertEqual((status, download["code"]), (404, "EXPORT_DOWNLOAD_MISSING"))
                status, page = self.request(url, info={"mode": "redact"}, navigate=True)
                self.assertEqual(status, 404)
                self.assertIn("打开历史项目", page)
                self.assertIn("EXPORT_DOWNLOAD_MISSING", page)
