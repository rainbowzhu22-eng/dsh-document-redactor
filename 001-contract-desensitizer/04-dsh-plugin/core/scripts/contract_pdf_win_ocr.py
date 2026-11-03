"""Windows OCR: isolate native OCR imports in a disposable worker process."""
from __future__ import annotations

import atexit
import asyncio
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path
from PIL import Image

_ENGINE = None

# M2（v1.3.0）：本机 OCR 超时保护秒数。macOS 的 pdf_vision_ocr 走 subprocess 有 90s 超时，
# Windows 两条路径都没有超时 —— 个别页面可能把整批处理卡死。此处统一加超时，
# 超时按「本页识别失败」处理（走 H2 的失败页路径，由用户在核对页手工确认/框选）。
OCR_TIMEOUT_SEC = float(os.environ.get("CONTRACT_PDF_OCR_TIMEOUT", "90"))
OCR_START_TIMEOUT_SEC = float(os.environ.get("CONTRACT_PDF_OCR_START_TIMEOUT", "30"))
OCR_MAX_RESPONSE_BYTES = int(os.environ.get(
    "CONTRACT_PDF_OCR_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))


class OcrError(RuntimeError):
    """Base class for worker lifecycle and protocol failures."""


class OcrTimeout(OcrError):
    pass


class OcrCrashed(OcrError):
    pass


class OcrProtocolError(OcrError):
    pass


class OcrUnavailable(OcrError):
    pass


def _rect(quad):
    xs = [float(point[0]) for point in quad]
    ys = [float(point[1]) for point in quad]
    return min(xs), min(ys), max(xs), max(ys)


def _word_rows(raw):
    if not raw:
        return []
    out = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 3 and isinstance(item[0], str):
            out.append(item)
        elif isinstance(item, (list, tuple)):
            out.extend(_word_rows(item))
    return out


def _rapid_engine():
    global _ENGINE
    import rapidocr
    from rapidocr import RapidOCR
    if _ENGINE is None:
        model_dir = Path(rapidocr.__file__).resolve().parent / "models"
        expected = ("PP-OCRv6_det_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                    "PP-OCRv6_rec_small.onnx")
        if any(not (model_dir / name).is_file() for name in expected):
            raise RuntimeError("本机 OCR 模型文件不完整；已停止自动识别，请手工框选")
        font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        font = next((str(font_dir / name) for name in
                     ("msyh.ttc", "simsun.ttc", "arial.ttf")
                     if (font_dir / name).is_file()), None)
        if font is None:
            raise RuntimeError("缺少本机 OCR 字体，请手工框选")
        _ENGINE = RapidOCR(params={"Global.font_path": font})
    return _ENGINE


def _rapid_once(path):
    result = _rapid_engine()(path, return_word_box=True, return_single_char_box=True)
    if result.boxes is None:
        return []
    with Image.open(path) as image:
        width, height = image.size
    words = _word_rows(result.word_results)
    lines = []
    for quad, text, score in zip(result.boxes, result.txts, result.scores):
        x0, y0, x1, y1 = _rect(quad)
        matched = []
        for word in words:
            try:
                wx0, wy0, wx1, wy1 = _rect(word[2])
            except (TypeError, ValueError, IndexError):
                continue
            cx, cy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
            if x0 - 3 <= cx <= x1 + 3 and y0 - 3 <= cy <= y1 + 3:
                matched.append((wx0, wy0, wx1, wy1, word[0]))
        matched.sort(key=lambda row: (row[0], row[1]))
        boxes = []
        cursor = 0
        reliable = True
        for char in text:
            if char.isspace():
                boxes.append(None)
                continue
            while cursor < len(matched) and not matched[cursor][4]:
                cursor += 1
            if cursor >= len(matched) or matched[cursor][4][0] != char:
                reliable = False
                break
            wx0, wy0, wx1, wy1, label = matched[cursor]
            # A multi-character label has only one box. Never fan that box out
            # across several text characters, even when its prefix happens to match.
            if len(label) != 1:
                reliable = False
                break
            boxes.append([wx0 / width, wy0 / height, wx1 / width, wy1 / height])
            cursor += 1
        if (cursor < len(matched) and any(row[4] for row in matched[cursor:])):
            reliable = False
        if not reliable or len(boxes) != len(text):
            line_box = [x0 / width, y0 / height, x1 / width, y1 / height]
            boxes = [line_box if not c.isspace() else None for c in text]
        lines.append({"text": text, "boxes": boxes,
                      "confidence": float(score),
                      "geometry": "character" if reliable else "line"})
    return lines


def _rapid(path):
    """对低分行局部放大重试，保持原图坐标；每页最多重试六行。"""
    lines = _rapid_once(path)
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    retried = 0
    with tempfile.TemporaryDirectory(prefix="contract-ocr-crop-") as temp:
        for index, line in enumerate(lines):
            if float(line.get("confidence", 0)) >= 0.70:
                continue
            boxes = [b for b in line.get("boxes", []) if b]
            if not boxes or retried >= 6:
                line["retry_status"] = "unavailable"
                continue
            retried += 1
            top = max(0, int(min(b[1] for b in boxes) * height) - 10)
            bottom = min(height, int(max(b[3] for b in boxes) * height) + 11)
            if bottom <= top:
                continue
            crop_path = Path(temp) / f"{index}.png"
            image.crop((0, top, width, bottom)).resize(
                (width * 2, (bottom - top) * 2)).save(crop_path)
            try:
                candidates = _rapid_once(str(crop_path))
                center = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2
                candidates = [row for row in candidates if row.get("boxes") and
                    abs((min(b[1] for b in row["boxes"] if b) +
                         max(b[3] for b in row["boxes"] if b)) / 2 *
                        (bottom - top) + top - center * height) < (bottom - top) / 2]
                best = max(candidates, key=lambda row: row["confidence"], default=None)
                if (best and best.get("geometry") == "character" and
                        best["confidence"] >= 0.70 and
                        best["confidence"] > float(line["confidence"]) + 0.05):
                    best["boxes"] = [
                        [b[0], (top + b[1] * (bottom - top)) / height,
                         b[2], (top + b[3] * (bottom - top)) / height] if b else None
                        for b in best["boxes"]]
                    best["retry_status"] = "improved"
                    best["initial_confidence"] = line["confidence"]
                    lines[index] = best
                else:
                    line["retry_status"] = "not-improved"
            except Exception:
                # 重试失败保留原识别和低分状态，不把明文或异常写入通用日志。
                line["retry_status"] = "failed"
    return lines


async def _winrt(path):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import StorageFile, FileAccessMode
    file = await StorageFile.get_file_from_path_async(os.fspath(path))
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows 缺少本机 OCR 语言包")
    result = await engine.recognize_async(bitmap)
    width, height = bitmap.pixel_width, bitmap.pixel_height
    lines = []
    for line in result.lines:
        text = ""
        boxes = []
        for word in line.words:
            if text:
                text += " "
                boxes.append(None)
            rect = word.bounding_rect
            box = [rect.x / width, rect.y / height,
                   (rect.x + rect.width) / width, (rect.y + rect.height) / height]
            text += word.text
            boxes.extend([box] * len(word.text))
        if text:
            lines.append({"text": text, "boxes": boxes,
                          "confidence": None, "geometry": "word"})
    return lines


def _recognize_inner(path):
    try:
        return _rapid(path)
    except ImportError:
        try:
            return asyncio.run(_winrt(path))
        except (ImportError, RuntimeError) as exc:
            raise RuntimeError("本机 OCR 不可用，请手工框选并逐页核对") from exc


def prepare_backend():
    """Initialize the OCR backend inside the worker process."""
    try:
        _rapid_engine()
        return "rapidocr"
    except ImportError:
        from winrt.windows.graphics.imaging import BitmapDecoder  # noqa: F401
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
        from winrt.windows.storage import StorageFile  # noqa: F401
        return "windows-ocr"


class _JobBasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JobExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimit),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsKillJob:
    """Kill assigned workers when the owning service process loses this handle."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _JobExtendedLimit()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle, self.JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return
        self._kernel32 = kernel32
        self.handle = handle

    def assign(self, process):
        if not self.handle or not getattr(process, "_handle", None):
            return False
        return bool(self._kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(self.handle), ctypes.c_void_p(process._handle)))

    def close(self):
        if self.handle:
            self._kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


class OcrClient:
    """Serialize OCR requests through one restartable worker process."""

    def __init__(self, worker_cmd=None, start_timeout=OCR_START_TIMEOUT_SEC,
                 max_response_bytes=OCR_MAX_RESPONSE_BYTES):
        self.worker_cmd = worker_cmd
        self.start_timeout = float(start_timeout)
        self.max_response_bytes = int(max_response_bytes)
        self._lock = threading.RLock()
        self._process = None
        self._messages = None
        self._reader = None
        self._next_id = 0
        self._startup_failures = 0
        self._disabled = False
        self._stopped = False
        self._job = _WindowsKillJob()
        self._job_assigned = False

    def _command(self):
        if self.worker_cmd:
            return list(self.worker_cmd)
        return [sys.executable, str(Path(__file__).with_name("contract_pdf_ocr_worker.py"))]

    @staticmethod
    def _put(messages, item):
        try:
            messages.put_nowait(item)
        except queue.Full:
            pass

    def _read_messages(self, process, messages):
        stream = process.stdout
        try:
            while True:
                raw = stream.readline(self.max_response_bytes + 1)
                if not raw:
                    self._put(messages, ("error", "worker-exited"))
                    return
                if len(raw) > self.max_response_bytes or not raw.endswith(b"\n"):
                    self._put(messages, ("error", "response-too-large"))
                    return
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._put(messages, ("error", "invalid-json"))
                    return
                self._put(messages, ("message", value))
        except (OSError, ValueError):
            self._put(messages, ("error", "reader-failed"))

    def _terminate_locked(self):
        process = self._process
        reader = self._reader
        self._process = None
        self._messages = None
        self._reader = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if reader and reader is not threading.current_thread():
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def _startup_failed_locked(self, reason):
        self._terminate_locked()
        self._startup_failures += 1
        if self._startup_failures >= 3:
            self._disabled = True
        if self._disabled:
            raise OcrUnavailable("本机 OCR 连续启动失败，已停止自动识别，请手工框选")
        raise OcrUnavailable("本机 OCR 启动失败，请手工框选（%s）" % reason)

    def _wait_locked(self, timeout):
        try:
            kind, value = self._messages.get(timeout=max(0.01, timeout))
        except queue.Empty as exc:
            raise OcrTimeout("本机 OCR 超时，请手工框选并逐页核对") from exc
        if kind == "error":
            raise OcrProtocolError("本机 OCR 通信失败（%s）" % value)
        if not isinstance(value, dict):
            raise OcrProtocolError("本机 OCR 返回格式无效")
        return value

    def _start_locked(self):
        if self._stopped or self._disabled:
            raise OcrUnavailable("本机 OCR 当前不可用，请手工框选")
        if self._process is not None and self._process.poll() is None:
            return
        self._terminate_locked()
        messages = queue.Queue(maxsize=4)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = dict(os.environ, PYTHONUTF8="1")
        try:
            process = subprocess.Popen(
                self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=flags, env=env, bufsize=0)
        except OSError as exc:
            self._startup_failed_locked(type(exc).__name__)
        self._process = process
        self._messages = messages
        self._job_assigned = self._job.assign(process)
        reader = threading.Thread(
            target=self._read_messages, args=(process, messages),
            name="pdf-ocr-reader", daemon=True)
        self._reader = reader
        reader.start()
        try:
            ready = self._wait_locked(self.start_timeout)
        except OcrError as exc:
            self._startup_failed_locked(type(exc).__name__)
        if ready.get("ready") is not True:
            self._startup_failed_locked(str(ready.get("error") or "not-ready")[:80])
        self._startup_failures = 0

    def prewarm(self):
        with self._lock:
            self._start_locked()

    def recognize(self, path, timeout=OCR_TIMEOUT_SEC):
        with self._lock:
            self._start_locked()
            self._next_id += 1
            request_id = self._next_id
            payload = json.dumps({"id": request_id, "path": os.fspath(path)},
                                 ensure_ascii=False, separators=(",", ":")) + "\n"
            try:
                self._process.stdin.write(payload.encode("utf-8"))
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._terminate_locked()
                raise OcrCrashed("本机 OCR 进程已退出，请重试本页") from exc
            started = time.monotonic()
            try:
                response = self._wait_locked(float(timeout))
            except OcrTimeout:
                self._terminate_locked()
                raise OcrTimeout("本机 OCR 超时（>%ds），请手工框选并逐页核对" % int(timeout))
            except OcrError:
                self._terminate_locked()
                raise
            if response.get("id") != request_id:
                self._terminate_locked()
                raise OcrProtocolError("本机 OCR 响应编号不匹配")
            if time.monotonic() - started > float(timeout):
                self._terminate_locked()
                raise OcrTimeout("本机 OCR 超时，请手工框选并逐页核对")
            if response.get("ok") is not True:
                raise RuntimeError(str(response.get("error") or "本机 OCR 识别失败")[:240])
            lines = response.get("lines")
            if not isinstance(lines, list):
                self._terminate_locked()
                raise OcrProtocolError("本机 OCR 响应缺少识别结果")
            return lines

    def stop(self):
        self._stopped = True
        while True:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            if self._lock.acquire(timeout=0.1):
                break
        try:
            self._terminate_locked()
            self._job.close()
        finally:
            self._lock.release()


_CLIENT = None
_CLIENT_LOCK = threading.Lock()
_PREWARM_THREAD = None


def _client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = OcrClient()
        return _CLIENT


def recognize(path):
    return _client().recognize(path)


def prewarm_async():
    global _CLIENT, _PREWARM_THREAD
    with _CLIENT_LOCK:
        if _PREWARM_THREAD and _PREWARM_THREAD.is_alive():
            return _PREWARM_THREAD
        if _CLIENT is None:
            _CLIENT = OcrClient()
        client = _CLIENT

        def run():
            try:
                client.prewarm()
            except OcrError as exc:
                sys.stderr.write("[pdf-ocr] prewarm failed: %s\n" % type(exc).__name__)

        _PREWARM_THREAD = threading.Thread(
            target=run, name="pdf-ocr-prewarm", daemon=True)
        _PREWARM_THREAD.start()
        return _PREWARM_THREAD


def stop():
    global _CLIENT, _PREWARM_THREAD
    with _CLIENT_LOCK:
        client = _CLIENT
        _CLIENT = None
        _PREWARM_THREAD = None
    if client is not None:
        client.stop()


atexit.register(stop)
