"""Private JSON-lines worker for Windows PDF OCR."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_REQUEST_BYTES = 64 * 1024


def _protocol_stream():
    duplicate = os.dup(sys.stdout.fileno())
    protocol = os.fdopen(duplicate, "w", encoding="utf-8", newline="\n", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return protocol


def _send(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def main():
    protocol = _protocol_stream()
    os.environ["CONTRACT_PDF_OCR_WORKER"] = "1"
    try:
        from contract_pdf_win_ocr import _recognize_inner, prepare_backend
        backend = prepare_backend()
    except BaseException as exc:  # noqa: BLE001 - startup failure must cross the process boundary
        _send(protocol, {"ready": False, "error": type(exc).__name__})
        return 2
    _send(protocol, {"ready": True, "backend": backend})
    while True:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            _send(protocol, {"id": None, "ok": False, "error": "请求过大"})
            return 3
        request = {}
        try:
            request = json.loads(raw.decode("utf-8"))
            request_id = request.get("id")
            path = request.get("path")
            if not isinstance(request_id, int) or not isinstance(path, str):
                raise ValueError("请求格式无效")
            if not Path(path).is_file():
                raise ValueError("待识别页面不存在")
            lines = _recognize_inner(path)
            _send(protocol, {"id": request_id, "ok": True, "lines": lines})
        except Exception as exc:  # noqa: BLE001 - return a bounded local error to the service
            _send(protocol, {"id": request.get("id") if isinstance(request, dict) else None,
                             "ok": False, "error": str(exc)[:240]})


if __name__ == "__main__":
    raise SystemExit(main())
