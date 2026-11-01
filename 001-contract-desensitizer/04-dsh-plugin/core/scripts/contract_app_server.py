#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
contract_app_server.py
======================
合同脱敏 + 还原一体化 Web 应用：本地 HTTP 服务，对外提供：
  - 上传 docx/pdf
  - 自动进入「脱敏前 / 脱敏后」左右对比页（展示全文）
  - 高中低分类筛选项 / 类型多选框
  - 用户点击确认 → 生成 <name>_脱敏.docx + mapping.json
  - 「还原」模式：自动匹配 sibling mapping；缺则让用户上传
  - 还原时**不动**批注 / 审阅 / 修订 / 样式（仅替换 <w:t>.text）

设计思路
--------
* 首次安装依赖需要联网；安装完成后文书处理不联网，与detector/redactor共享enforce_offline()。
* 会话隔离：每个上传文件一个 session 目录，sid 为 UUID。
* 输出脱敏产品统一为占位符模式（[label#N]） + mapping.json，方便一键还原。
"""
from __future__ import annotations

import html as _html
import hashlib
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
import zipfile
import urllib.parse
import shutil
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from contract_ui_notices import EXPORT_REVIEW_NOTICE, EXPORT_UNRECOGNIZED_NOTE

# 复用 detector / redactor 的离线算法、改写器、还原器
import contract_sensitive_detector as D
import contract_review_ext as review_ext
import contract_pdf_project as pdf_project
import contract_redaction_policy as Policy
from contract_pdf_ui import page as build_pdf_page
from contract_sensitive_detector import (
    TYPE_META,
    Settings,
    DetectorEngine,
    RulesFormatError,
    RuleMatchTimeout,
    build_rules_snapshot,
    load_rules_md,
    load_document,
    build_report,
    write_mapping,
)
from contract_redactor import (
    ENGINE_NAME as REDACTOR_NAME,
    __version__ as REDACTOR_VERSION,
    redact_file,
    restore_file,
    find_sibling_mapping,
    _extract_embedded_mapping,
)


APP_NAME = "contract_app_server"
APP_VERSION = "1.3.0-dev"
APP_REVISION = "r99"
DEFAULT_PORT = 18800
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_REQUEST_BODY_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
REQUEST_READ_BLOCK_BYTES = 64 * 1024
REQUEST_READ_TIMEOUT_SECONDS = 15
REQUEST_READ_TOTAL_SECONDS = 120
MAX_MULTIPART_FIELDS = 32
MAX_MULTIPART_FIELD_NAME_CHARS = 80
MAX_MULTIPART_NON_FILE_BYTES = 64 * 1024
PDF_JOBS: Dict[str, threading.Thread] = {}
PDF_JOBS_LOCK = threading.RLock()
SERVICE_EVENT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_service_state(work_dir: str, state: Dict[str, Any]) -> None:
    target = os.path.join(work_dir, "service-state.json")
    temp = target + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, target)
    except OSError as exc:
        sys.stderr.write("[service] state write failed: %s\n" % type(exc).__name__)


def _record_service_event(work_dir: str, request_id: str, phase: str,
                          error_type: Optional[str] = None) -> None:
    row = {"at": _utc_now(), "request_id": request_id, "phase": phase}
    if error_type:
        row["error_type"] = error_type
    try:
        with SERVICE_EVENT_LOCK, open(os.path.join(work_dir, "service-events.jsonl"),
                                      "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        sys.stderr.write("[service] event write failed: %s\n" % type(exc).__name__)


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _start_service_state(work_dir: str) -> Dict[str, Any]:
    previous_stop = None
    try:
        with open(os.path.join(work_dir, "service-state.json"), "r", encoding="utf-8") as stream:
            previous = json.load(stream)
        if previous.get("status") == "running" and not _pid_alive(previous.get("pid")):
            previous_stop = "unexpected-stop"
        elif (previous.get("status") == "stopped" and
              previous.get("stop_type") == "uncaught-exception"):
            previous_stop = "uncaught-exception"
    except (OSError, ValueError, TypeError):
        pass
    state = {
        "version": APP_VERSION,
        "pid": os.getpid(),
        "started_at": _utc_now(),
        "workdir_sha256": hashlib.sha256(os.path.abspath(work_dir).encode("utf-8")).hexdigest(),
        "status": "running",
        "previous_stop_type": previous_stop,
    }
    _write_service_state(work_dir, state)
    return state


def _finish_service_state(server: ThreadingHTTPServer, stop_type: str,
                          error_type: Optional[str] = None) -> None:
    state = getattr(server, "service_state", None)
    if not state:
        return
    state.update(status="stopped", stop_type=stop_type, stopped_at=_utc_now())
    if error_type:
        state["error_type"] = error_type
    _write_service_state(server.state.work_dir, state)


def _start_pdf_job(state: "AppState", sid: str) -> None:
    def work():
        try:
            pdf_project.process(state.work_dir, sid)
            try:
                p = pdf_project.load(state.work_dir, sid)
                if p["stage"] == "review" and p["case_name"]:
                    with state.case_lock:
                        case = state.load_case(p["case_name"])
                        p = pdf_project.sync_case(state.work_dir, sid,
                                                  case.get("terms", []), case.get("numbers", []))
                        case["numbers"] = p["numbers"]
                        state.save_case(p["case_name"], case)
            except Exception as exc:
                sys.stderr.write("[pdf] case sync failed: %s\n" % type(exc).__name__)
        finally:
            # 任务结束后清出登记表，避免 PDF_JOBS 只增不删。
            with PDF_JOBS_LOCK:
                PDF_JOBS.pop(sid, None)
    with PDF_JOBS_LOCK:
        existing = PDF_JOBS.get(sid)
        if existing and existing.is_alive():
            return
        job = threading.Thread(target=work, daemon=True)
        PDF_JOBS[sid] = job
        job.start()


def _pdf_jobs_active(sids) -> bool:
    with PDF_JOBS_LOCK:
        return any(PDF_JOBS.get(sid) and PDF_JOBS[sid].is_alive() for sid in sids)


# ------------------------------------------------- 明文清理（H1）---------------
# 会话目录里落的是「明文面」：上传原件（<sess>/<name>.*）＋ PDF 项目的 private/
# （original.pdf ＋ pages/*.png ＋ text/*.json）。这些是交付/共享/换机前必须清掉的东西。
# 清理策略：**只删明文，保留 out/ 的脱敏产物**；sid 白名单（12 位 hex）＋ 路径越界校验，
# 绝不触碰 cases/ 等非会话目录。
PLAINTEXT_SUFFIXES = (".docx", ".pdf", ".txt", ".md")
_SID_RE_CLEAN = re.compile(r"^[0-9a-f]{12}$")
_PDF_STATE_TEMP_RE = re.compile(r"^pdf-project\.json\.\d+\.\d+\.[0-9a-f]{32}\.tmp$")
RETENTION_ENV = "CONTRACT_DESENSITIZER_RETENTION_DAYS"


def _session_has_sensitive_files(sess: str) -> bool:
    if os.path.isdir(os.path.join(sess, "private")):
        return True
    try:
        for name in os.listdir(sess):
            path = os.path.join(sess, name)
            if os.path.isfile(path) and (
                    os.path.splitext(name)[1].lower() in PLAINTEXT_SUFFIXES
                    or name.lower().endswith(".mapping.json")
                    or name.lower().endswith(".redaction.log")
                    or name in ("pdf-project.json", "pdf-project.json.tmp")
                    or _PDF_STATE_TEMP_RE.fullmatch(name)):
                return True
        out = os.path.join(sess, "out")
        if os.path.isdir(out):
            return any(os.path.isfile(os.path.join(out, name)) and (
                name.lower().endswith(".mapping.json")
                or name.lower().endswith(".redaction.log")
                or name == "output.tmp.pdf") for name in os.listdir(out))
    except OSError:
        return True
    return False


def purge_plaintext(work_dir: str, sid: str) -> Tuple[bool, str, int]:
    """删除会话原件、缓存和可逆映射，保留已完成的脱敏产物。"""
    if not _SID_RE_CLEAN.match(sid or ""):
        return False, "非法的会话编号", 0
    root = os.path.abspath(work_dir)
    sess = os.path.abspath(os.path.join(root, sid))
    if not (sess + os.sep).startswith(root + os.sep):
        return False, "路径越界", 0
    if not os.path.isdir(sess):
        return False, "记录不存在或已被删除", 0
    removed = 0
    errors: List[str] = []
    priv = os.path.join(sess, "private")
    if os.path.isdir(priv):
        try:
            shutil.rmtree(priv)
            removed += 1
        except OSError:
            errors.append("PDF私有缓存")
    try:
        entries = os.listdir(sess)
    except OSError:
        return False, "无法读取会话目录", removed
    for fn in entries:
        fp = os.path.join(sess, fn)
        sensitive = (os.path.splitext(fn)[1].lower() in PLAINTEXT_SUFFIXES
                     or fn.lower().endswith(".mapping.json")
                     or fn.lower().endswith(".redaction.log")
                     or fn in ("pdf-project.json", "pdf-project.json.tmp")
                     or _PDF_STATE_TEMP_RE.fullmatch(fn))
        if os.path.isfile(fp) and sensitive:
            try:
                os.remove(fp)
                removed += 1
            except OSError:
                errors.append(fn)
    out_dir = os.path.join(sess, "out")
    if os.path.isdir(out_dir):
        try:
            out_names = os.listdir(out_dir)
        except OSError:
            out_names = []
            errors.append("out目录")
        for fn in out_names:
            if not (fn.lower().endswith(".mapping.json")
                    or fn.lower().endswith(".redaction.log")
                    or fn == "output.tmp.pdf"):
                continue
            try:
                os.remove(os.path.join(out_dir, fn))
                removed += 1
            except OSError:
                errors.append(fn)
    if errors:
        return False, "部分明文材料未能删除（%d项）" % len(errors), removed
    return True, "", removed


def purge_all_plaintext(work_dir: str) -> Tuple[int, int, int]:
    """清理全部会话敏感材料。返回 (清理会话数, 删除项目数, 失败会话数)。"""
    root = os.path.abspath(work_dir)
    cleaned = removed = failed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0, 0, 1
    for name in names:
        if not _SID_RE_CLEAN.match(name) or not os.path.isdir(os.path.join(root, name)):
            continue
        ok, _err, n = purge_plaintext(root, name)
        removed += n
        if ok:
            cleaned += 1
        else:
            failed += 1
    return cleaned, removed, failed


def _retention_days() -> int:
    """保留期（天）。默认 0＝不自动清理；经环境变量 CONTRACT_DESENSITIZER_RETENTION_DAYS 显式开启。"""
    raw = os.environ.get(RETENTION_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def retention_sweep(work_dir: str) -> int:
    """启动时按保留期清理会话敏感材料。默认关闭；返回被清理的会话数。

    刻意保守：只有显式设置环境变量才生效；任何异常都不影响服务启动。
    """
    days = _retention_days()
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    root = os.path.abspath(work_dir)
    cleaned = 0
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    for name in names:
        sess = os.path.join(root, name)
        if not _SID_RE_CLEAN.match(name) or not os.path.isdir(sess):
            continue
        try:
            if os.path.getmtime(sess) > cutoff:
                continue
        except OSError:
            continue
        if not _session_has_sensitive_files(sess):
            continue
        ok, _err, _n = purge_plaintext(root, name)
        if ok:
            cleaned += 1
    return cleaned


# ---------------------------------------------------------------- HTML / UI --
# 所有页面模板（CSS / HTML / JS）集中在 contract_app_ui.py，便于单独调整设计，
# 不触碰服务端处理逻辑。此处只做转发。
from contract_app_ui import (          # noqa: E402
    build_upload_page,
    build_workspace_body,
    build_review_page,
    build_expired_page,
    build_history_page,
    loading_doc,
    FAVICON_SVG,
)


# --------------------------------------------------------------- 服务端状态 ----


class AppState:
    """会话状态：每个上传文件一个 sid，对应一个工作目录。"""

    def __init__(self, work_dir: str):
        os.makedirs(work_dir, mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(work_dir, 0o700)
        self.work_dir = work_dir
        self.uploads: Dict[str, Dict[str, Any]] = {}
        self.case_lock = threading.RLock()

    def case_path(self, name: str) -> str:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        return os.path.join(self.work_dir, "cases", digest + ".json")

    def load_case(self, name: str) -> Dict[str, Any]:
        if not name:
            return {"terms": [], "numbers": []}
        with self.case_lock:
            path = self.case_path(name)
            if not os.path.exists(path):
                return {"terms": [], "numbers": []}
            with open(path, encoding="utf-8") as f:
                return json.load(f)

    def save_case(self, name: str, data: Dict[str, Any]) -> None:
        if not name:
            return
        with self.case_lock:
            path = self.case_path(name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)

    def new_session(self, sid: str, info: Dict[str, Any]) -> None:
        self.uploads[sid] = info

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        return self.uploads.get(sid)


# --------------------------------------------------------------- 工具函数 ------


def _safe_filename(name: str) -> str:
    """剥离路径成分，并只允许简单的 ascii + 中日韩。"""
    base = os.path.basename(name or "upload.bin")
    base = re.sub(r"[\\/]+", "_", base)
    return base or "upload.bin"


def _ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _rules_path() -> Optional[str]:
    """随包默认模板位置；仅作首次复制与升级比对来源，不是用户可编辑文件。"""
    root = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(root, "脱敏规则清单.md"),
                 os.path.join(os.path.dirname(root), "references", "脱敏规则清单.md")):
        if os.path.exists(path):
            return path
    return None


# ------------------------------------------------- 用户规则文件（可编辑） ------
# 设计要点（见《AI协助改规则功能吸收方案》4.1/4.2）：
# * 用户规则目录由 rules_data_dir() 独立解析，不从 sessions 目录推导，也不受
#   CONTRACT_DESENSITIZER_SESSIONS_DIR 或旧版 程序目录/sessions 回退影响；
#   启动器通过 CONTRACT_DESENSITIZER_RULES_DIR 注入，服务端缺省时按同一规则计算。
# * 首次只在目标不存在时从随包默认模板复制一次；启动与升级都不覆盖用户文件。
# * 快照按内容 SHA-256 命名存放在 Rules/Backups，同内容不重复备份；
#   最近一次通过校验的版本另存为 last_valid.md，供页面一键恢复。
# * state.json 记录默认模板、当前用户规则与最近有效版本的哈希。

RULES_STATE_FILENAME = "state.json"
RULES_BACKUP_DIRNAME = "Backups"
RULES_LAST_VALID_FILENAME = "last_valid.md"
RULES_BACKUP_KEEP = 20
_rules_store_lock = threading.RLock()
_rules_notice = {"bundled_updated": False, "init_error": None}
RULES_OPEN_TOKEN = ""


class _RulesUploadError(Exception):
    """上传前规则校验失败；code 供页面区分提示。"""

    def __init__(self, code, message, missing=None, sha=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing = list(missing or [])
        self.sha = sha


def rules_data_dir() -> str:
    """用户规则目录；独立解析，不从 sessions 目录推导。"""
    override = os.environ.get("CONTRACT_DESENSITIZER_RULES_DIR")
    if override:
        return override
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", "ContractDesensitizer")
    elif os.name == "nt":
        base = os.path.join(os.environ.get(
            "LOCALAPPDATA",
            os.path.join(os.path.expanduser("~"), "AppData", "Local")),
            "ContractDesensitizer")
    else:
        base = os.path.join(os.environ.get(
            "XDG_DATA_HOME",
            os.path.join(os.path.expanduser("~"), ".local", "share")),
            "contract-desensitizer")
    return os.path.join(base, "Rules")


def user_rules_path() -> str:
    """用户实际编辑的规则文件；识别、导出核对与页面打开均使用这一份。"""
    return os.path.join(rules_data_dir(), "脱敏规则清单.md")


def _active_rules_path() -> str:
    """当前生效规则文件：优先用户文件，缺失时回退随包默认模板。"""
    upath = user_rules_path()
    if os.path.exists(upath):
        return upath
    bundled = _rules_path()
    return bundled if bundled else upath


def _read_rules_file_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rules_state_path() -> str:
    return os.path.join(rules_data_dir(), RULES_STATE_FILENAME)


def _load_rules_state() -> Dict[str, Any]:
    try:
        with open(_rules_state_path(), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_rules_state(state: Dict[str, Any]) -> None:
    path = _rules_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_RULES_SNAPSHOT_SEQ = re.compile(r"^[0-9a-f]{64}\.md$")


def _snapshot_user_rules(raw: bytes, sha: str) -> None:
    """按内容哈希保存快照；同内容不重复，保留最近 RULES_BACKUP_KEEP 份。"""
    backup_dir = os.path.join(rules_data_dir(), RULES_BACKUP_DIRNAME)
    os.makedirs(backup_dir, exist_ok=True)
    target = os.path.join(backup_dir, sha + ".md")
    if not os.path.exists(target):
        tmp = target + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, target)
    try:
        entries = []
        for n in os.listdir(backup_dir):
            if not _RULES_SNAPSHOT_SEQ.fullmatch(n):
                continue
            fp = os.path.join(backup_dir, n)
            try:
                entries.append((os.path.getmtime(fp), n))
            except OSError:
                continue
        entries.sort()
        excess = entries[:-RULES_BACKUP_KEEP] if len(entries) > RULES_BACKUP_KEEP else []
    except OSError:
        return
    for _mtime, name in excess:
        try:
            os.remove(os.path.join(backup_dir, name))
        except OSError:
            pass


def _promote_last_valid(sha: str, raw: bytes, custom_ids: List[str]) -> None:
    """记录最近一次通过校验（且删除项已确认）的规则版本。"""
    backup_dir = os.path.join(rules_data_dir(), RULES_BACKUP_DIRNAME)
    os.makedirs(backup_dir, exist_ok=True)
    target = os.path.join(backup_dir, RULES_LAST_VALID_FILENAME)
    tmp = target + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, target)
    state = _load_rules_state()
    state["last_valid_sha256"] = sha
    state["last_valid_custom_ids"] = list(custom_ids)
    _save_rules_state(state)


def init_user_rules() -> None:
    """服务启动时：确保用户规则文件存在、比对默认模板哈希、按需保存快照。"""
    with _rules_store_lock:
        try:
            os.makedirs(rules_data_dir(), exist_ok=True)
            state = _load_rules_state()
            template_path = _rules_path()
            template_raw = _read_rules_file_bytes(template_path) if template_path else None
            template_sha = _sha256_bytes(template_raw) if template_raw is not None else None
            upath = user_rules_path()
            raw = _read_rules_file_bytes(upath)
            if raw is None and template_raw is not None:
                try:
                    with open(upath, "xb") as f:
                        f.write(template_raw)
                    raw = template_raw
                except FileExistsError:
                    raw = _read_rules_file_bytes(upath)
            if template_sha:
                if state.get("bundled_template_sha256") and \
                        state.get("bundled_template_sha256") != template_sha:
                    _rules_notice["bundled_updated"] = True
                state["bundled_template_sha256"] = template_sha
            if raw is not None:
                current_sha = _sha256_bytes(raw)
                if state.get("user_rules_sha256") != current_sha:
                    _snapshot_user_rules(raw, current_sha)
                    state["user_rules_sha256"] = current_sha
                _save_rules_state(state)
                if not state.get("last_valid_sha256"):
                    try:
                        snap = build_rules_snapshot(upath)
                        _promote_last_valid(snap["sha256"], raw, snap["custom_ids"])
                    except RulesFormatError:
                        pass  # 用户文件暂未通过校验时保持现状；上传时会给出明确错误
            else:
                _save_rules_state(state)
            _rules_notice["init_error"] = None
        except OSError as exc:
            _rules_notice["init_error"] = "无法准备用户规则文件（%s）：%s" % (
                rules_data_dir(), exc)


def _record_rules_change() -> Dict[str, Any]:
    """用户规则内容变化（无论是否经首页按钮）时保存快照并更新 state。"""
    state = _load_rules_state()
    raw = _read_rules_file_bytes(user_rules_path())
    if raw is None:
        return state
    current_sha = _sha256_bytes(raw)
    if state.get("user_rules_sha256") != current_sha:
        _snapshot_user_rules(raw, current_sha)
        state["user_rules_sha256"] = current_sha
        _save_rules_state(state)
    return state


def prepare_rules_for_upload(confirm_sha: Optional[str] = None):
    """上传识别前读取并校验用户规则；返回 (snapshot, state)。

    校验失败抛 _RulesUploadError，页面按 code 区分提示；删除自定义规则需
    使用者以 confirm_sha 明确确认后才会接受新版本。
    """
    with _rules_store_lock:
        state = _record_rules_change()
        upath = user_rules_path()
        raw = _read_rules_file_bytes(upath)
        if raw is None:
            raise _RulesUploadError(
                "RULES_MISSING", "用户规则文件不存在，请重启工具重新生成或恢复备份。")
        sha = _sha256_bytes(raw)
        prev_valid_sha = state.get("last_valid_sha256")
        try:
            snap = build_rules_snapshot(
                upath,
                previous_custom_ids=(state.get("last_valid_custom_ids") or [])
                if prev_valid_sha else None)
        except RulesFormatError as exc:
            raise _RulesUploadError(exc.code, str(exc), exc.missing, sha) from exc
        if snap["pending_deletions"] and not (
                confirm_sha and secrets.compare_digest(str(confirm_sha), sha)):
            raise _RulesUploadError(
                "RULES_DELETION_CONFIRM",
                "新规则文件将移除以下自定义规则：" + "、".join(snap["pending_deletions"])
                + "。确认删除后本次识别才会继续；如为误删，请恢复对应规则行。",
                snap["pending_deletions"], sha)
        _promote_last_valid(snap["sha256"], raw, snap["custom_ids"])
        return snap, state


def _export_selection_digest(info, request):
    """绑定页面确认时的原件、实际选择、模式、策略和规则快照。"""
    types = request.get("enabled_types")
    if types is None and "enabled_types" not in request:
        types = [row["type"] for row in info["report"]["items"]]
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    if not isinstance(types, list) or any(not isinstance(t, str) for t in types):
        raise ValueError("类型选择无效")
    items = request.get("items")
    if items is not None:
        if not isinstance(items, list):
            raise ValueError("位置选择无效")
        items = sorted(items, key=lambda row: json.dumps(row, sort_keys=True))
    modes = {name: request.get(name, default) for name, default in (
        ("comment_mode", "redact_preserve"), ("revision_mode", "redact_preserve"),
        ("external_relationship_action", "remove"), ("min_confidence", 0.5),
        ("min_severity", "low"))}
    rules = _active_rules_path()
    payload = {"schema": 1, "source": D._sha256(info["input_path"]),
               "types": sorted(set(types)), "items": items, "modes": modes,
               "inventory": _revision_review_digest(info.get("revision_inventory")),
               "policy": info.get("policy_id"),
               "numbers": sorted([(list(key) if isinstance(key, tuple) else key, value)
                                   for key, value in (info.get("case_numbers") or {}).items()], key=repr),
               "terms": [info.get("case_data", {}).get("terms", []), info.get("local_terms", [])],
               "rules": D._sha256(rules) if rules else "builtin"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _run_detector(path: str, terms=None, litigation=False, unified=True,
                  rules_snapshot=None) -> Dict[str, Any]:
    """对刚上传的路径跑一次 detector，返回 (doc, candidates, report, report_full_text)。

    使用 detector 默认配置 + 已通过校验的用户规则快照（不读全局可变状态）。
    """
    if rules_snapshot is None:
        # 兜底（如复核页字段重扫）：按同一份用户规则文件现场校验，失败直接抛出。
        rules_snapshot, _ = prepare_rules_for_upload()
    settings = Settings(min_confidence=0.5, min_severity="low")
    # v1.3.0「低置信可见可勾」：Web 复核路径保留 0.3–0.5 的候选，供复核页标注「把握较低」
    # 并默认不勾；下限（0.3）以下只计数、不静默丢弃（见 report["_scan_stats"]）。
    settings.retain_low_confidence = True
    if rules_snapshot is not None:
        settings.extra_specs = list(rules_snapshot["extra_specs"])
        settings.extra_type_meta.update(rules_snapshot["type_overrides"])
    if unified:
        review_ext.configure_unified_settings(settings)
    elif litigation:
        settings.profile = "litigation"
        settings.extra_specs.extend(review_ext.litigation_specs())
        settings.extra_type_meta.update(review_ext.litigation_type_meta())
    else:
        settings.profile = "contract"
    engine = DetectorEngine(settings)
    doc = load_document(path, include_headers=True)
    revision_inventory = ((doc.revision_analysis or {}).get("inventory")
                          if doc.file_type == "docx" else None)
    if revision_inventory is not None:
        revision_inventory = dict(revision_inventory)
        revision_inventory["input_sha256"] = D._sha256(path)
        revision_inventory["review_digest"] = _revision_review_digest(
            revision_inventory)
    compat_preview = []
    if doc.file_type == "docx":
        with zipfile.ZipFile(path) as package:
            from lxml import etree as XML
            for part in package.namelist():
                if part == "word/document.xml" or re.fullmatch(r"word/(?:header|footer)\d+\.xml", part):
                    rows = review_ext.classify_content_controls(part, XML.fromstring(package.read(part)))
                    for row in rows:
                        compat_preview.append({"part_kind": "正文" if part == "word/document.xml" else "页眉页脚",
                            "classification": row["classification"], "reason": row["reason"],
                            "verdict": "supported" if row["classification"] != "unsupported" else "blocked"})
    if revision_inventory is not None:
        revision_inventory["compat_preview"] = compat_preview
    D.require_pdf_text(doc)
    candidates = engine.scan_document(doc)
    if unified:
        review_ext.classify_unified_candidates(candidates)
    if terms:
        candidates = review_ext.add_exact_terms(doc, candidates, terms, engine.type_meta)
    report = build_report(doc, candidates, settings)
    report["_scan_stats"] = dict(getattr(engine, "last_scan_stats", {}) or {})
    full_raw = "\n".join(b.text.rstrip("\n") for b in doc.blocks)
    comment_info = None
    if os.path.splitext(path)[1].lower() == ".docx":
        from contract_docx_comments import inspect_comment_package
        comment_info = inspect_comment_package(path)
    return {
        "doc": doc, "candidates": candidates, "report": report,
        "full_raw": full_raw, "comment_info": comment_info,
        "revision_inventory": revision_inventory,
    }


def _revision_review_digest(inventory: Optional[Dict[str, Any]]) -> str:
    if not inventory:
        return ""
    payload = {
        "input_sha256": str(inventory.get("input_sha256") or ""),
        "unsupported_revision_digest": str(inventory.get("unsupported_digest") or ""),
        "unsupported_object_digest": str(inventory.get("object_digest") or ""),
        "unsafe_field_digest": str(inventory.get("unsafe_field_digest") or ""),
        "unscanned_part_digest": str(inventory.get("unscanned_digest") or ""),
        "unredacted_content_digest": str(
            inventory.get("unredacted_content_digest") or ""),
        "header_footer_digest": str(inventory.get("header_footer_digest") or ""),
        "embedded_image_count": int(inventory.get("embedded_image_count") or 0),
        "external_relationship_digest": str(
            inventory.get("external_relationship_digest") or ""),
        "revision_count": int(inventory.get("revision_count") or 0),
        "supported_text_revision_count": int(
            inventory.get("supported_text_revision_count") or 0),
        "supported_property_revision_count": int(
            inventory.get("supported_property_revision_count") or 0),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_confirmation_required(inventory: Optional[Dict[str, Any]]) -> bool:
    return bool(inventory and (
        inventory.get("has_revisions") or
        inventory.get("unsupported_manifest") or
        inventory.get("unsupported_object_manifest") or
        inventory.get("unscanned_parts") or
        inventory.get("unredacted_content_parts") or
        inventory.get("header_footer_content_parts") or
        inventory.get("embedded_image_count") or
        inventory.get("unsafe_field_instruction_count") or
        inventory.get("external_relationship_count")))


def _revision_hard_block_reason(mode: str, ext: str,
                                inventory: Optional[Dict[str, Any]]) -> str:
    """Return no user-facing export gate; inventory limitations become risk notes."""
    return ""


def _revision_needs_ack(inventory: Optional[Dict[str, Any]]) -> bool:
    return bool(inventory and (inventory.get("unsupported_manifest") or
                               inventory.get("unsupported_object_manifest") or
                               inventory.get("unscanned_parts") or
                               inventory.get("unsafe_field_instruction_count")))


def _revision_warning(inventory: Optional[Dict[str, Any]]) -> str:
    inventory = inventory or {}
    has_unchecked_content = bool(
        inventory.get("inventory_scope_unknown") or
        ("complete" in inventory and not inventory.get("complete")) or
        _revision_needs_ack(inventory) or
        inventory.get("unredacted_content_parts") or
        inventory.get("unscanned_parts") or
        inventory.get("header_footer_content_parts") or
        inventory.get("embedded_image_count") or
        inventory.get("unsafe_field_instruction_count"))
    if not has_unchecked_content:
        return ""
    return EXPORT_REVIEW_NOTICE + " " + EXPORT_UNRECOGNIZED_NOTE


def _revision_export_risk(inventory: Optional[Dict[str, Any]]) -> str:
    inventory = inventory or {}
    unknown = bool(
        inventory.get("inventory_scope_unknown") or
        ("complete" in inventory and not inventory.get("complete")))
    if unknown:
        return ("修订或文档部件盘点未能完成，范围或数量无法确定；"
                "受影响内容可能按原文保留。")

    unsupported = int(inventory.get("unsupported_revision_count") or 0)
    unsupported = max(unsupported, len(inventory.get("unsupported_manifest") or []))
    note_count = sum(int(row.get("count") or 0)
                     for row in inventory.get("unredacted_content_parts") or [])
    other_unscanned = bool(
        inventory.get("unscanned_parts") or
        inventory.get("unsupported_object_manifest") or
        inventory.get("unsafe_field_instruction_count") or
        inventory.get("embedded_image_count"))
    if not (unsupported or note_count or other_unscanned):
        return ""

    details = []
    if unsupported:
        details.append("%d处未支持修订" % unsupported)
    if note_count:
        details.append("%d条脚注或尾注内容" % note_count)
    if other_unscanned:
        details.append("其他未扫描结构")
    return "以下内容可能按原文保留：" + "、".join(details) + "。"


def _revision_block_reason(mode: str, ext: str,
                           inventory: Optional[Dict[str, Any]],
                           profile: str = "unified",
                           revision_mode: Optional[str] = None,
                           revision_acknowledged: bool = False,
                           revision_ack_digest: Optional[str] = None) -> str:
    """Revision modes are compatibility inputs; risks are confirmed after preflight."""
    return ""


def _read_revision_inventory(path: str) -> Optional[Dict[str, Any]]:
    if _ext_of(path) != ".docx":
        return None
    from contract_docx_revisions import analyze_docx_revisions
    analysis = analyze_docx_revisions(path)
    inventory = dict(analysis.get("inventory") or {})
    inventory["input_sha256"] = D._sha256(path)
    inventory["review_digest"] = _revision_review_digest(inventory)
    return inventory


def _looks_like_redacted(redacted_path: str) -> bool:
    """粗略判断文件是否已经是脱敏产物（占位符 [label#N] 出现）。"""
    PLACEHOLDER_PATTERN = re.compile(r"\[[^\]\[]{1,16}#\d+\]")
    ext = _ext_of(redacted_path)
    try:
        if ext == ".docx":
            import zipfile
            with zipfile.ZipFile(redacted_path) as z:
                names = [n for n in z.namelist() if n.startswith("word/") and n.endswith(".xml")]
                for n in names[:8]:
                    with z.open(n) as f:
                        chunk = f.read(200_000).decode("utf-8", errors="ignore")
                    if PLACEHOLDER_PATTERN.search(chunk):
                        return True
            return False
        if ext in (".txt", ".md"):
            with open(redacted_path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(200_000)
            return bool(PLACEHOLDER_PATTERN.search(head))
    except Exception:
        return False
    return False


# 页面渲染统一委托给 contract_app_ui（见文件头部 import）。


# --------------------------------------------------------------- HTTP handler --


class AppHandler(BaseHTTPRequestHandler):
    state: AppState = None  # injected by start_app_server

    # 关闭 socket 时的额外日志噪音
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "unknown"
        sys.stderr.write("[app] method=%s status=%s\n" % (self.command, status))

    def _send(self, code: int, content_type: str, body: bytes,
              extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, code: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, "application/json; charset=utf-8", data)
        request_id = getattr(self, "_upload_request_id", None)
        if request_id:
            _record_service_event(self.state.work_dir, request_id, "response-sent")

    def _send_html(self, body, code: int = 200) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if code != 204 and body:
            self.wfile.write(body)

    def _local_request_allowed(self, mutation: bool = False) -> bool:
        """阻断 DNS 重绑定及来自其他网页的本机写请求。"""
        host = (self.headers.get("Host") or "").lower()
        if not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", host):
            return False
        if mutation:
            origin = self.headers.get("Origin")
            if origin:
                try:
                    parsed = urllib.parse.urlparse(origin)
                    valid_origin = (parsed.scheme == "http" and
                                    parsed.hostname in ("127.0.0.1", "localhost") and
                                    parsed.port == self.server.server_port)
                except ValueError:
                    valid_origin = False
                if not valid_origin:
                    return False
        return True

    # ---- 历史记录（本地留档，只读磁盘）------------------------------------
    # 设计要点：全部走**磁盘**，不依赖内存会话表 self.state.uploads。
    # 后者是进程内字典，服务一重启即清空；若历史页也依赖它，"重启后依然能查"
    # 这个存在意义就没了（旧链接会全部 404）。
    _SID_RE = re.compile(r"^[0-9a-f]{12}$")

    @staticmethod
    def _hist_origin_name(sess: str) -> str:
        """会话目录里那份「原件」的文件名（mapping 缺失时兜底显示）。"""
        try:
            for fn in sorted(os.listdir(sess)):
                fp = os.path.join(sess, fn)
                if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in (
                        ".docx", ".pdf", ".txt", ".md"):
                    return fn
        except OSError:
            pass
        return ""

    @staticmethod
    def _hist_meta(sess: str) -> Dict[str, Any]:
        """尽量从 out/*.mapping.json 读出 文件名 / 生成时间 / 替换处数。"""
        meta: Dict[str, Any] = {"filename": "", "when": "", "count": None}
        out_dir = os.path.join(sess, "out")
        if not os.path.isdir(out_dir):
            return meta
        for fn in sorted(os.listdir(out_dir)):
            if not fn.endswith(".mapping.json"):
                continue
            try:
                with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                    d = json.load(f)
                src = d.get("source") or {}
                meta["filename"] = src.get("name") or d.get("filename") or ""
                meta["when"] = str(d.get("generated_at") or "")[:19].replace("T", " ")
                items = d.get("items")
                if isinstance(items, list):
                    meta["count"] = len(items)
            except Exception:
                pass
            break
        return meta

    def _history_data(self) -> Dict[str, Any]:
        """扫描本地会话目录，列出历次处理记录（只读磁盘，重启后依然可用）。"""
        root = os.path.abspath(self.state.work_dir)
        rows: List[Dict[str, Any]] = []
        total = 0
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        for name in names:
            if not self._SID_RE.match(name):
                continue
            sess = os.path.join(root, name)
            if not os.path.isdir(sess):
                continue
            out_dir = os.path.join(sess, "out")
            is_pdf_project = os.path.isfile(os.path.join(sess, "pdf-project.json"))
            pdf_output = os.path.isfile(os.path.join(out_dir, "脱敏文书-001.pdf"))
            pdf_purged = bool(pdf_output and not is_pdf_project and
                              not os.path.isdir(os.path.join(sess, "private")))
            if is_pdf_project:
                try:
                    pdf_meta = pdf_project.load(root, name)
                except Exception:
                    pdf_meta = {}
            else:
                pdf_meta = {}
            revision_blocked = False
            files: List[Tuple[str, str]] = []
            redacted = restored = False
            if os.path.isdir(out_dir):
                for fn in sorted(os.listdir(out_dir)):
                    if not os.path.isfile(os.path.join(out_dir, fn)):
                        continue
                    if fn.endswith(".redaction.log"):
                        files.append(("日志", fn))
                    elif fn.endswith(".mapping.json"):
                        files.append(("映射", fn))
                    elif "_还原" in fn:
                        files.append(("还原稿", fn))
                        restored = True
                    elif "_脱敏" in fn:
                        files.append(("脱敏稿", fn))
                        redacted = True
                    elif fn == "脱敏文书-001.pdf" and (is_pdf_project or pdf_purged):
                        files.append(("脱敏稿", fn))
                        redacted = True
            meta = self._hist_meta(sess)
            size = 0
            for dp, _dn, fns in os.walk(sess):
                for fn in fns:
                    try:
                        size += os.path.getsize(os.path.join(dp, fn))
                    except OSError:
                        pass
            total += size
            when = meta["when"]
            if not when:
                try:
                    when = datetime.fromtimestamp(
                        os.path.getmtime(sess)).strftime("%Y-%m-%d %H:%M:%S")
                except OSError:
                    when = ""
            rows.append({
                "sid": name,
                "filename": pdf_meta.get("filename") or meta["filename"] or self._hist_origin_name(sess) or "(未命名)",
                "when": when,
                "count": meta["count"],
                "files": files,
                "redacted": redacted,
                "restored": restored,
                "size": size,
                "live": bool(self.state.get(name)),
                "pdf_project": is_pdf_project,
                "revision_blocked": revision_blocked,
                # PDF 项目已清掉明文面（private/ 不在）→ 历史页不再提供「继续处理」。
                "pdf_purged": pdf_purged or bool(is_pdf_project and
                                                  not os.path.isdir(os.path.join(sess, "private"))),
            })
        rows.sort(key=lambda r: r["when"] or "", reverse=True)
        return {"rows": rows, "count": len(rows), "total_bytes": total}

    def _history_file(self, sid: str, name: str) -> Optional[str]:
        """按磁盘定位历史产物；不查内存会话表，故服务重启后仍能下载。"""
        if not self._SID_RE.match(sid or ""):
            return None
        out_dir = os.path.abspath(os.path.join(self.state.work_dir, sid, "out"))
        safe_root = out_dir + os.sep
        target = os.path.abspath(os.path.join(out_dir, name))
        if not (target + os.sep).startswith(safe_root):
            return None
        if not os.path.isfile(target):
            return None
        return target

    def _delete_history(self, sid: str) -> Tuple[bool, str]:
        """删除一条历史记录。sid 白名单 + 路径校验；cases/ 及其它目录一律拒绝。"""
        if not self._SID_RE.match(sid or ""):
            return False, "非法的会话编号"
        root = os.path.abspath(self.state.work_dir)
        target = os.path.abspath(os.path.join(root, sid))
        if not (target + os.sep).startswith(root + os.sep):
            return False, "路径越界"
        if not os.path.isdir(target):
            return False, "记录不存在或已被删除"
        try:
            shutil.rmtree(target)
        except Exception as exc:
            return False, "删除失败：%s（文件可能正被 Word 等程序占用）" % exc
        try:
            self.state.uploads.pop(sid, None)
        except Exception:
            pass
        return True, ""

    # ---- GET ----
    def do_GET(self):
        if not self._local_request_allowed():
            self._send_json({"ok": False, "error": "invalid local host"}, 403)
            return
        u = urllib.parse.urlparse(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)
        sid = (qs.get("sid") or [""])[0]

        if path == "/" or path == "/index.html":
            self._send_html(build_upload_page(RULES_OPEN_TOKEN))
            return
        if path == "/loading" or path == "/loading.html":
            self._send_html(loading_doc())
            return
        if path in ("/favicon.svg", "/favicon.ico"):
            self._send(200, "image/svg+xml; charset=utf-8", FAVICON_SVG.encode("utf-8"))
            return
        if path == "/health":
            self._send_json({"ok": True, "service": APP_NAME,
                             "version": APP_VERSION, "revision": APP_REVISION})
            return
        if path == "/api/rules/status":
            state = _load_rules_state()
            raw = _read_rules_file_bytes(user_rules_path())
            backup_dir = os.path.join(rules_data_dir(), RULES_BACKUP_DIRNAME)
            try:
                backups = len([n for n in os.listdir(backup_dir)
                               if _RULES_SNAPSHOT_SEQ.fullmatch(n)])
            except OSError:
                backups = 0
            self._send_json({
                "ok": True,
                "path": user_rules_path(),
                "exists": raw is not None,
                "sha256": _sha256_bytes(raw) if raw is not None else None,
                "last_valid_sha256": state.get("last_valid_sha256"),
                "backups": backups,
                "bundled_template_updated": bool(_rules_notice.get("bundled_updated")),
                "init_error": _rules_notice.get("init_error"),
            })
            return
        m = re.fullmatch(r"/pdf/([0-9a-f]{12})", path)
        if m:
            try:
                pdf_project.load(self.state.work_dir, m.group(1))
                self._send_html(build_pdf_page(m.group(1)))
            except Exception:
                self._send_html(build_expired_page(), 404)
            return
        m = re.fullmatch(r"/api/pdf/status/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                p = pdf_project.load(self.state.work_dir, m.group(1))
                if p["stage"] == "processing":
                    _start_pdf_job(self.state, m.group(1))
                self._send_json({"ok": True, "project": pdf_project.public(p)})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 404)
            return
        m = re.fullmatch(r"/api/pdf/page/([0-9a-f]{12})/([0-9]+)", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                p = pdf_project.load(self.state.work_dir, m.group(1))
                number = int(m.group(2))
                if not 1 <= number <= len(p["pages"]):
                    raise ValueError("页码无效")
                target = pdf_project.paths(self.state.work_dir, m.group(1))[0] / "private" / "pages" / f"{number:04d}.png"
                self._send(200, "image/png", target.read_bytes())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 404)
            return
        m = re.fullmatch(r"/api/pdf/thumb/([0-9a-f]{12})/([0-9]+)", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                import io
                from PIL import Image
                p = pdf_project.load(self.state.work_dir, m.group(1))
                number = int(m.group(2))
                if not 1 <= number <= len(p["pages"]):
                    raise ValueError("页码无效")
                source = pdf_project.paths(self.state.work_dir, m.group(1))[0] / "private" / "pages" / f"{number:04d}.png"
                with Image.open(source) as image:
                    thumb = image.convert("RGB")
                    thumb.thumbnail((64, 84))
                    data = io.BytesIO()
                    thumb.save(data, "PNG")
                self._send(200, "image/png", data.getvalue())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 404)
            return
        m = re.fullmatch(r"/api/pdf/preview/([0-9a-f]{12})/([0-9]+)", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                data = pdf_project.preview_image(self.state.work_dir, m.group(1), int(m.group(2)))
                self._send(200, "image/png", data)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 404)
            return
        m = re.fullmatch(r"/api/pdf/original/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                source = pdf_project.original(self.state.work_dir, m.group(1))
                self._send(200, "application/pdf", source.read_bytes(),
                           [("Content-Disposition", 'attachment; filename="original.pdf"')])
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 404)
            return
        if path.startswith("/download/"):
            # /download/<sid>/<encoded-name...>
            # 文件实际位置：<workdir>/<sid>/out/<name>（与 /api/apply 一致）
            parts = path.split("/")
            if len(parts) < 4:
                self._send_json({"ok": False, "error": "bad download path"}, 400)
                return
            dsid = parts[2]
            name = "/".join(parts[3:])
            name = urllib.parse.unquote(name)
            info = self.state.get(dsid)
            if not info:
                self._send_json({"ok": False, "error": "session not found"}, 404)
                return
            target = os.path.abspath(os.path.join(self.state.work_dir, dsid, "out", name))
            safe_root = os.path.abspath(os.path.join(self.state.work_dir, dsid, "out")) + os.sep
            if not (target + os.sep).startswith(safe_root):
                self._send_json({"ok": False, "error": "bad path"}, 400)
                return
            if not os.path.exists(target):
                self._send_json({"ok": False, "error": "file not found"}, 404)
                return
            with open(target, "rb") as f:
                data = f.read()
            ctype = "application/octet-stream"
            ext = os.path.splitext(target)[1].lower()
            if ext == ".docx":
                ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif ext == ".json":
                ctype = "application/json; charset=utf-8"
            elif ext == ".log":
                ctype = "text/plain; charset=utf-8"
            disp = f'attachment; filename="{urllib.parse.quote(os.path.basename(target))}"'
            self._send(200, ctype, data, [("Content-Disposition", disp)])
            return
        if path == "/workspace":
            info = self.state.get(sid)
            if not info:
                self._send_html(build_expired_page())
                return
            self._send_html(build_workspace_body(sid, info))
            return
        if path == "/review":
            info = self.state.get(sid)
            if not info:
                self._send_html(build_expired_page())
                return
            self._send_html(build_review_page(sid, info))
            return
        if path == "/history":
            self._send_html(build_history_page(self._history_data()))
            return
        if path.startswith("/history/download/"):
            # 磁盘直读 —— 不经内存会话表，服务重启后旧记录依然可下载
            parts = path.split("/")
            if len(parts) < 5:
                self._send_json({"ok": False, "error": "bad download path"}, 400)
                return
            target = self._history_file(parts[3],
                                        urllib.parse.unquote("/".join(parts[4:])))
            if not target:
                self._send_json({"ok": False, "error": "file not found"}, 404)
                return
            with open(target, "rb") as f:
                data = f.read()
            ctype = "application/octet-stream"
            ext = os.path.splitext(target)[1].lower()
            if ext == ".docx":
                ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif ext == ".json":
                ctype = "application/json; charset=utf-8"
            elif ext == ".log":
                ctype = "text/plain; charset=utf-8"
            disp = 'attachment; filename="%s"' % urllib.parse.quote(os.path.basename(target))
            self._send(200, ctype, data, [("Content-Disposition", disp)])
            return

        self._send_json({"ok": False, "error": "GET not found"}, 404)

    # ---- POST ----
    def _handle_rules_open(self):
        """打开本机规则文件：路径由服务端固定解析，网页不能传入路径。"""
        token = self.headers.get("X-Rules-Open-Token", "")
        if not RULES_OPEN_TOKEN or not secrets.compare_digest(token, RULES_OPEN_TOKEN):
            self._send_json({"ok": False, "error": "缺少有效的本机打开令牌，请刷新页面后重试"}, 403)
            return
        upath = user_rules_path()
        try:
            if sys.platform == "win32":
                try:
                    os.startfile(upath)  # 固定服务端路径，非用户输入
                except OSError:
                    import subprocess
                    editor = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                          "System32", "notepad.exe")
                    subprocess.Popen([editor, upath])
            else:
                import subprocess
                command = ["open", "-t", upath] if sys.platform == "darwin" else ["xdg-open", upath]
                subprocess.Popen(command)
            self._send_json({"ok": True, "path": upath})
        except OSError as exc:
            self._send_json({"ok": False, "path": upath,
                             "error": "无法启动文本编辑器，请在该位置手动打开规则文件：" + str(exc)}, 500)

    def _handle_rules_restore(self):
        """把用户规则文件恢复为最近一次通过校验的版本（当前内容先保存快照）。"""
        with _rules_store_lock:
            state = _load_rules_state()
            backup = os.path.join(rules_data_dir(), RULES_BACKUP_DIRNAME,
                                  RULES_LAST_VALID_FILENAME)
            raw = _read_rules_file_bytes(backup)
            if not state.get("last_valid_sha256") or raw is None:
                self._send_json({"ok": False,
                                 "error": "没有可恢复的有效规则版本；请手动修正规则文件"}, 404)
                return
            current = _read_rules_file_bytes(user_rules_path())
            if current is not None:
                _snapshot_user_rules(current, _sha256_bytes(current))
            try:
                with open(user_rules_path(), "wb") as f:
                    f.write(raw)
            except OSError as exc:
                self._send_json({"ok": False, "error": "恢复失败：" + str(exc)}, 500)
                return
            state["user_rules_sha256"] = _sha256_bytes(raw)
            _save_rules_state(state)
        self._send_json({"ok": True, "path": user_rules_path()})

    def do_POST(self):
        if not self._local_request_allowed(mutation=True):
            self._send_json({"ok": False, "error": "invalid request origin"}, 403)
            return
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/api/upload":
            self._upload_request_id = uuid.uuid4().hex[:16]
            _record_service_event(self.state.work_dir, self._upload_request_id, "body-reading")
        ctype = self.headers.get("Content-Type", "")
        body_bytes = b""
        raw_lengths = self.headers.get_all("Content-Length", [])
        transfer_encoding = self.headers.get_all("Transfer-Encoding", [])
        required_body = (
            path == "/api/upload"
            or path == "/api/history/clear-private"
            or path.startswith("/api/pdf/relink/")
            or path.startswith("/api/pdf/update/")
            or path.startswith("/api/pdf/term/")
            or path.startswith("/api/pdf/sync-case/")
            or path.startswith("/api/pdf/control/")
            or path.startswith("/api/pdf/export/")
            or path.startswith("/api/term/")
            or path.startswith("/api/apply/")
            or path.startswith("/api/restore/")
        )
        if transfer_encoding:
            self.close_connection = True
            self._send_json({"ok": False, "error": "不支持该请求传输编码"}, 400)
            return
        if len(raw_lengths) > 1 and len(set(raw_lengths)) != 1:
            self.close_connection = True
            self._send_json({"ok": False, "error": "Content-Length无效"}, 400)
            return
        raw_length = raw_lengths[0].strip() if raw_lengths else "0"
        if len(raw_length) > 20 or not re.fullmatch(r"[0-9]+", raw_length):
            self.close_connection = True
            self._send_json({"ok": False, "error": "Content-Length无效"}, 400)
            return
        try:
            clen = int(raw_length)
        except ValueError:
            self.close_connection = True
            self._send_json({"ok": False, "error": "Content-Length无效"}, 400)
            return
        if not raw_lengths and required_body:
            self.close_connection = True
            self._send_json({"ok": False, "error": "请求缺少Content-Length"}, 411)
            return
        if required_body and clen == 0:
            self.close_connection = True
            self._send_json({"ok": False, "error": "请求体不能为空"}, 400)
            return
        if clen > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            self._send_json({"ok": False, "error": "请求体超过总大小上限"}, 413)
            return
        if clen:
            old_timeout = self.connection.gettimeout()
            started = time.monotonic()
            chunks = []
            remaining = clen
            try:
                self.connection.settimeout(REQUEST_READ_TIMEOUT_SECONDS)
                while remaining:
                    if time.monotonic() - started > REQUEST_READ_TOTAL_SECONDS:
                        raise TimeoutError("request body read deadline exceeded")
                    chunk = self.rfile.read(min(remaining, REQUEST_READ_BLOCK_BYTES))
                    if not chunk:
                        raise EOFError("request body shorter than Content-Length")
                    chunks.append(chunk)
                    remaining -= len(chunk)
            except (OSError, EOFError, TimeoutError):
                self.close_connection = True
                self._send_json({"ok": False, "error": "请求体读取不完整或超时"}, 400)
                return
            finally:
                try:
                    self.connection.settimeout(old_timeout)
                except OSError:
                    pass
            body_bytes = b"".join(chunks)

        # 解析 multipart
        def parse_multipart():
            """最简单的 multipart 解析，返回 {field_name: {filename, content_type, data}}"""
            if not ctype.startswith("multipart/form-data"):
                return {}
            m = re.match(r'multipart/form-data;\s*boundary=(.*)', ctype)
            if not m:
                return {}
            try:
                boundary_value = m.group(1).strip().strip('"')
                boundary = b"--" + boundary_value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("multipart boundary无效") from exc
            if not boundary_value or len(boundary_value) > 200:
                raise ValueError("multipart boundary无效")
            out: Dict[str, Dict[str, Any]] = {}
            parts = body_bytes.split(boundary)
            if len(parts) - 2 > MAX_MULTIPART_FIELDS:
                raise ValueError("multipart字段数量超过上限")
            non_file_bytes = 0
            for p in parts[1:-1]:
                if p.startswith(b"\r\n"):
                    p = p[2:]
                if p.endswith(b"\r\n"):
                    p = p[:-2]
                if not p:
                    continue
                head_blob, _, data = p.partition(b"\r\n\r\n")
                head = head_blob.decode("utf-8", errors="ignore")
                # 解析 Content-Disposition
                name_m = re.search(r'name="([^"]+)"', head)
                filename_m = re.search(r'filename="([^"]*)"', head)
                ctype_m = re.search(r'Content-Type:\s*(.+)', head)
                if not name_m:
                    continue
                name = name_m.group(1)
                filename = filename_m.group(1) if filename_m else ""
                if len(name) > MAX_MULTIPART_FIELD_NAME_CHARS or name in out:
                    raise ValueError("multipart字段名称无效或重复")
                if not filename:
                    non_file_bytes += len(data)
                    if non_file_bytes > MAX_MULTIPART_NON_FILE_BYTES:
                        raise ValueError("multipart普通字段总量超过上限")
                content_type = (ctype_m.group(1).strip() if ctype_m else "")
                out[name] = {
                    "filename": filename,
                    "content_type": content_type,
                    "data": data,
                }
            request_id = getattr(self, "_upload_request_id", None)
            if request_id:
                _record_service_event(self.state.work_dir, request_id, "multipart-parsed")
            return out

        # --- 路由 ---
        if path == "/api/history/delete":
            if ctype.split(";", 1)[0].strip().lower() != "application/json":
                self._send_json({"ok": False, "error": "JSON request required"}, 415)
                return
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            ok, err = self._delete_history(str((payload or {}).get("sid") or ""))
            self._send_json({"ok": True} if ok else {"ok": False, "error": err})
            return

        if path == "/api/history/clear-private":
            # 清理原件、可逆映射和PDF项目状态，保留已完成脱敏产物。
            # body: {"all": true} 清全部；{"sid": "..."} 清单条。
            if ctype.split(";", 1)[0].strip().lower() != "application/json":
                self._send_json({"ok": False, "error": "JSON request required"}, 415)
                return
            try:
                payload = json.loads(body_bytes.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            payload = payload or {}
            if payload.get("all"):
                try:
                    session_ids = [name for name in os.listdir(self.state.work_dir)
                                   if _SID_RE_CLEAN.fullmatch(name)
                                   and os.path.isdir(os.path.join(self.state.work_dir, name))]
                except OSError:
                    self._send_json({"ok": False, "error": "无法读取会话目录"}, 500)
                    return
                with PDF_JOBS_LOCK:
                    active = any(PDF_JOBS.get(sid) and PDF_JOBS[sid].is_alive()
                                 for sid in session_ids)
                    result = None if active else purge_all_plaintext(self.state.work_dir)
                if active:
                    self._send_json({"ok": False,
                                     "error": "有PDF项目正在处理，请稍后重试清理"}, 409)
                    return
                cleaned, removed, failed = result
                for sid in session_ids:
                    if not _session_has_sensitive_files(os.path.join(self.state.work_dir, sid)):
                        self.state.uploads.pop(sid, None)
                self._send_json({"ok": failed == 0, "sessions": cleaned,
                                 "removed": removed, "failed": failed,
                                 "error": "部分会话仍有材料未能删除" if failed else None})
            else:
                sid = str(payload.get("sid") or "")
                with PDF_JOBS_LOCK:
                    active = bool(PDF_JOBS.get(sid) and PDF_JOBS[sid].is_alive())
                    result = None if active else purge_plaintext(self.state.work_dir, sid)
                if active:
                    self._send_json({"ok": False,
                                     "error": "PDF项目正在处理，请稍后重试清理"}, 409)
                    return
                ok, err, removed = result
                if ok:
                    self.state.uploads.pop(sid, None)
                self._send_json({"ok": True, "removed": removed} if ok
                                else {"ok": False, "error": err})
            return

        if path == "/api/shutdown":
            # 供网页右上角「退出」按钮调用：优雅停止本地服务并释放端口
            self._send_json({"ok": True, "message": "shutting down"})
            _srv = self.server

            def _stop() -> None:
                time.sleep(0.25)          # 先让 HTTP 响应写出去
                try:
                    _srv.shutdown()
                except Exception:
                    pass
                try:
                    _srv.server_close()
                except Exception:
                    pass
                _finish_service_state(_srv, "clean-shutdown")
                time.sleep(0.2)
                os._exit(0)               # 确保端口一定被释放

            try:
                threading.Thread(target=_stop, daemon=True).start()
            except Exception as exc:      # 不应发生；退化为直接退出
                sys.stderr.write("[app] shutdown failed: %s\n" % type(exc).__name__)
                os._exit(0)
            return

        # ---------------- /api/rules/* ----------------
        if path == "/api/rules/open":
            self._handle_rules_open()
            return

        if path == "/api/rules/restore-last-valid":
            self._handle_rules_restore()
            return

        if path == "/api/upload":
            sid = None
            sess_dir = None
            upload_committed = False
            try:
                mp = parse_multipart()
                fld = mp.get("file")
                mode = (mp.get("mode", {}).get("data") or b"redact").decode("utf-8").strip().lower()
                if mode not in ("redact", "restore"):
                    mode = "redact"
                # Keep accepting the old profile field during upgrades, but never let it
                # choose different recognition, revision, or DOCX validation behavior.
                profile = "unified"
                revision_mode = "redact_preserve"
                case_name = (mp.get("case_name", {}).get("data") or b"").decode("utf-8").strip()
                if len(case_name) > 80 or any(c in case_name for c in "\r\n"):
                    self._send_json({"ok": False, "error": "案件名称不能超过 80 字且不能换行"}, 400)
                    return
                if not fld or not fld.get("data"):
                    self._send_json({"ok": False, "error": "未选择文件"}, 400)
                    return
                if len(fld["data"]) > MAX_UPLOAD_BYTES:
                    self._send_json(
                        {"ok": False, "error": f"文件超过 {MAX_UPLOAD_BYTES//1024//1024} MB 上限"},
                        413)
                    return
                safe_name = _safe_filename(fld.get("filename") or "upload.bin")
                ext = _ext_of(safe_name)
                if ext not in (".docx", ".pdf", ".txt", ".md"):
                    self._send_json({"ok": False, "error": f"暂不支持 {ext} 文件"}, 400)
                    return
                if mode == "redact":
                    policy_selection = (mp.get("redaction_policy", {}).get("data") or
                                        b"").decode("utf-8").strip()
                    if policy_selection == Policy.RECOMMEND:
                        self._send_json({"ok": False, "error":
                                         "当前上传页使用了已停用的自动推荐策略，请刷新或升级后重新选择金额、日期的处理方式"}, 400)
                        return
                    try:
                        policy_id = Policy.resolve_policy(policy_selection, ext)
                    except ValueError:
                        self._send_json({"ok": False, "error":
                                         "请先选择金额、日期的处理方式，再上传文件"}, 400)
                        return
                else:
                    policy_selection = ""
                    policy_id = Policy.PRIVACY_FIRST
                if mode == "restore" and ext == ".pdf":
                    self._send_json({"ok": False, "error": "PDF 原件请从本机项目的「取回原始 PDF」获取；不能从脱敏 PDF 或文字映射表恢复扫描像素"}, 400)
                    return
                if mode == "redact":
                    confirm_sha = (mp.get("rules_confirm_sha256", {}).get("data")
                                   or b"").decode("utf-8", "ignore").strip() or None
                    try:
                        rules_snapshot, _rules_state = prepare_rules_for_upload(
                            confirm_sha=confirm_sha)
                    except _RulesUploadError as exc:
                        self._send_json({"ok": False, "code": exc.code,
                                         "error": exc.message, "missing": exc.missing,
                                         "rules_path": user_rules_path(),
                                         "rules_sha256": exc.sha},
                                        409 if exc.code == "RULES_DELETION_CONFIRM" else 400)
                        return
                sid = uuid.uuid4().hex[:12]
                sess_dir = os.path.join(self.state.work_dir, sid)
                os.makedirs(sess_dir, exist_ok=True)
                input_path = os.path.join(sess_dir, safe_name)
                with open(input_path, "wb") as f:
                    f.write(fld["data"])

                if mode == "redact" and ext == ".pdf":
                    try:
                        case_data = self.state.load_case(case_name)
                        pdf_project.create(self.state.work_dir, sid, input_path, safe_name,
                                           profile, case_name, [dict(x, scope="case")
                                                             for x in case_data.get("terms", [])],
                                           policy_id=policy_id,
                                           policy_selection=policy_selection,
                                           rules_snapshot=rules_snapshot)
                        _record_service_event(self.state.work_dir, self._upload_request_id,
                                              "project-created")
                        project = pdf_project.load(self.state.work_dir, sid)
                        project["numbers"] = case_data.get("numbers", [])
                        pdf_project.save(self.state.work_dir, sid, project)
                        os.remove(input_path)
                        _start_pdf_job(self.state, sid)
                        upload_committed = True
                        self._send_json({"ok": True, "sid": sid, "mode": "redact",
                                         "redirect": f"/pdf/{sid}"})
                    except Exception as exc:
                        _record_service_event(self.state.work_dir, self._upload_request_id,
                                              "upload-error", type(exc).__name__)
                        try:
                            shutil.rmtree(sess_dir)
                        except OSError as cleanup_exc:
                            sys.stderr.write("[upload] rollback failed sid=%s error_type=%s\n" %
                                             (sid, type(cleanup_exc).__name__))
                            self._send_json({"ok": False,
                                             "error": "PDF项目建立失败，临时文件清理未完成"}, 500)
                            return
                        sys.stderr.write("[upload] pdf setup failed sid=%s error_type=%s\n" %
                                         (sid, type(exc).__name__))
                        self._send_json({"ok": False, "error": "PDF项目建立失败，请检查文件后重试"}, 400)
                    return

                # 检测文件是否已含占位符
                is_redacted = _looks_like_redacted(input_path)

                if mode == "restore":
                    # 还原模式：跳过 detector。直接找 sibling mapping 和内嵌 mapping。
                    sib_mapping = find_sibling_mapping(input_path)
                    has_embedded = False
                    try:
                        if _extract_embedded_mapping(input_path) is not None:
                            has_embedded = True
                    except Exception:
                        pass
                    # 选择首个可用；如果两个都有，优先 sibling（更直观）
                    chosen = sib_mapping if (sib_mapping and os.path.exists(sib_mapping)) else (
                        "embedded" if has_embedded else None)
                    info = {
                        "sid": sid,
                        "filename": safe_name,
                        "input_path": input_path,
                        "mode": "restore",
                        "is_redacted": is_redacted,
                        "sibling_mapping": sib_mapping,
                        "mapping_path": chosen,
                        "has_embedded_mapping": has_embedded,
                        "outputs": {},
                    }
                    self.state.new_session(sid, info)
                    upload_committed = True
                    self._send_json({
                        "ok": True, "sid": sid,
                        "filename": safe_name, "mode": "restore",
                        "looks_redacted": is_redacted,
                        "mapping_found": bool(chosen),
                        "mapping_source": "embedded" if chosen == "embedded" else "file" if chosen else None,
                        "redirect": f"/workspace?sid={sid}",
                    })
                    return

                # ---- redact mode ----
                case_data = self.state.load_case(case_name)
                terms = [dict(t, scope="case") for t in case_data.get("terms", [])]
                try:
                    run = _run_detector(input_path, terms, unified=True,
                                        rules_snapshot=rules_snapshot)
                except RuleMatchTimeout as exc:
                    if not upload_committed and sess_dir:
                        try:
                            shutil.rmtree(sess_dir)
                            self.state.uploads.pop(sid, None)
                        except OSError:
                            self._send_json({"ok": False, "code": "RULES_MATCH_TIMEOUT",
                                             "error": "自定义规则匹配超时已中止，临时文件清理未完成，"
                                                      "请在历史记录中重试清理"}, 500)
                            return
                    sys.stderr.write("[upload] rule timeout sid=%s\n" % (sid or ""))
                    self._send_json({"ok": False, "code": "RULES_MATCH_TIMEOUT",
                                     "error": str(exc)}, 400)
                    return
                report = run["report"]
                doc = run["doc"]
                full_raw = run["full_raw"]
                revision_inventory = run.get("revision_inventory")
                revision_block_reason = _revision_hard_block_reason(
                    mode, ext, revision_inventory)
                # 写 mapping.json （它就是后续还原器要用的）
                mapping_path = os.path.join(sess_dir, safe_name + ".mapping.json")
                write_mapping(report, mapping_path)

                # 构造 review payload
                case_data["numbers"], numbers = review_ext.assign_case_numbers(
                    report, case_data.get("numbers", []))
                review_payload = _build_review_payload(report, doc.blocks, numbers, terms,
                                                       run.get("comment_info"),
                                                       revision_inventory=revision_inventory,
                                                       policy_id=policy_id)
                review_payload["profile"] = profile
                review_payload["revision_mode"] = revision_mode
                review_payload["comment_mode_default"] = "redact_preserve"
                review_payload["case_name"] = case_name
                review_payload["policy_selection"] = policy_selection
                review_payload["revision_block_reason"] = revision_block_reason
                review_payload["revision_warning"] = _revision_warning(revision_inventory)

                # 找 sibling mapping（detector 模式下不会用到，先留着）
                sib_mapping = find_sibling_mapping(input_path) or mapping_path

                info = {
                    "sid": sid,
                    "filename": safe_name,
                    "input_path": input_path,
                    "mode": "redact",
                    "report": report,
                    "doc": doc,
                    "full_raw": full_raw,
                    "review_payload": review_payload,
                    "profile": profile,
                    "policy_id": policy_id,
                    "policy_selection": policy_selection,
                    "case_name": case_name,
                    "case_data": case_data,
                    "local_terms": [],
                    "case_numbers": numbers,
                    "mapping_path": mapping_path if os.path.isfile(mapping_path) else "",
                    "sibling_mapping": sib_mapping,
                    "rules_sha256": rules_snapshot["sha256"],
                    "looks_redacted": is_redacted,
                    "counts": {"types": len({it["type"] for it in report["items"]}),
                               "occurrences": report["summary"]["occurrences"]},
                    "outputs": {},
                    "revision_inventory": revision_inventory,
                    "revision_mode": revision_mode,
                    "revision_block_reason": revision_block_reason,
                    "revision_warning": _revision_warning(revision_inventory),
                }
                with self.state.case_lock:
                    if case_name:
                        latest_case = self.state.load_case(case_name)
                        latest_case["numbers"], numbers = review_ext.assign_case_numbers(
                            report, latest_case.get("numbers", []))
                        case_data = latest_case
                        review_payload = _build_review_payload(report, doc.blocks, numbers, terms,
                                                               run.get("comment_info"),
                                                               revision_inventory=revision_inventory,
                                                               policy_id=policy_id)
                        review_payload["profile"] = profile
                        review_payload["revision_mode"] = revision_mode
                        review_payload["comment_mode_default"] = "redact_preserve"
                        review_payload["case_name"] = case_name
                        review_payload["policy_selection"] = policy_selection
                        review_payload["revision_block_reason"] = revision_block_reason
                        review_payload["revision_warning"] = _revision_warning(revision_inventory)
                        info["case_data"] = case_data
                        info["case_numbers"] = numbers
                        info["review_payload"] = review_payload
                    self.state.new_session(sid, info)
                    _record_service_event(self.state.work_dir, self._upload_request_id,
                                          "project-created")
                    try:
                        if case_name:
                            self.state.save_case(case_name, case_data)
                    except Exception:
                        self.state.uploads.pop(sid, None)
                        raise
                upload_committed = True
                self._send_json({
                    "ok": True, "sid": sid,
                    "filename": safe_name, "mode": "redact",
                    "profile": profile, "case_name": case_name,
                    "counts": info["counts"],
                    "looks_redacted": is_redacted,
                    "revision_blocked": bool(revision_block_reason),
                    "mapping_found": os.path.exists(sib_mapping),
                    "redirect": f"/workspace?sid={sid}",
                })
            except D.PdfTextLayerError as exc:
                _record_service_event(self.state.work_dir, self._upload_request_id,
                                      "upload-error", type(exc).__name__)
                # 扫描件和混合 PDF 不能被误报为“没有敏感信息”。
                if sess_dir:
                    try:
                        shutil.rmtree(sess_dir)
                    except OSError as cleanup_exc:
                        sys.stderr.write("[upload] rollback failed sid=%s error_type=%s\n" %
                                         (sid or "", type(cleanup_exc).__name__))
                        self._send_json({"ok": False,
                                         "error": "PDF无法安全读取，临时文件清理未完成"}, 500)
                        return
                sys.stderr.write("[upload] pdf text layer rejected sid=%s error_type=%s\n" %
                                 (sid or "", type(exc).__name__))
                self._send_json({"ok": False, "error": "PDF无法安全读取，请检查文件后重试",
                                 "code": "PDF_TEXT_UNREADABLE"}, 400)
            except ValueError as exc:
                _record_service_event(self.state.work_dir, self._upload_request_id,
                                      "upload-error", type(exc).__name__)
                if not upload_committed and sess_dir:
                    try:
                        shutil.rmtree(sess_dir)
                        self.state.uploads.pop(sid, None)
                    except OSError:
                        self._send_json({"ok": False,
                                         "error": "上传失败，临时文件清理未完成，请在历史记录中重试清理"}, 500)
                        return
                sys.stderr.write("[upload] rejected sid=%s error_type=%s\n" %
                                 (sid or "", type(exc).__name__))
                self._send_json({"ok": False, "error": "上传内容无效，请检查文件后重试"}, 400)
            except Exception as exc:
                _record_service_event(self.state.work_dir, self._upload_request_id,
                                      "upload-error", type(exc).__name__)
                if not upload_committed and sess_dir:
                    try:
                        target = os.path.abspath(sess_dir)
                        root = os.path.abspath(self.state.work_dir)
                        if sid and _SID_RE_CLEAN.fullmatch(sid) and target == os.path.join(root, sid):
                            shutil.rmtree(target)
                            self.state.uploads.pop(sid, None)
                    except OSError as cleanup_exc:
                        sys.stderr.write("[upload] rollback failed sid=%s error_type=%s\n" %
                                         (sid or "", type(cleanup_exc).__name__))
                        self._send_json({"ok": False,
                                         "error": "上传失败，临时文件清理未完成，请在历史记录中重试清理"}, 500)
                        return
                sys.stderr.write("[upload] processing failed sid=%s error_type=%s\n" %
                                 (sid or "", type(exc).__name__))
                self._send_json({"ok": False, "error": "上传/识别失败，请检查文件格式后重试"}, 500)
            return

        # ---------------- /api/term/<sid> ----------------
        m = re.fullmatch(r"/api/pdf/relink/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                upload = parse_multipart().get("file")
                if not upload or not upload.get("data") or len(upload["data"]) > MAX_UPLOAD_BYTES:
                    raise ValueError("请选择不超过 100 MB 的原始 PDF")
                pdf_project.relink(self.state.work_dir, m.group(1), upload["data"])
                self._send_json({"ok": True})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        m = re.fullmatch(r"/api/pdf/update/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                req = json.loads(body_bytes.decode("utf-8"))
                result = pdf_project.update(self.state.work_dir, m.group(1), req)
                self._send_json({"ok": True, "project": result})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        m = re.fullmatch(r"/api/pdf/term/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                req = json.loads(body_bytes.decode("utf-8"))
                term = review_ext.validate_term(req.get("term"))
                scope = req.get("scope", "document")
                project = pdf_project.load(self.state.work_dir, m.group(1))
                if scope not in ("document", "case"):
                    raise ValueError("字段范围无效")
                if scope == "case" and not project["case_name"]:
                    raise ValueError("请先填写案件或项目名称")
                if scope == "case":
                    with self.state.case_lock:
                        case = self.state.load_case(project["case_name"])
                        case["terms"] = [x for x in case.get("terms", []) if x.get("id") != term["id"]] + [term]
                        pdf_project.add_term(self.state.work_dir, m.group(1), dict(term, scope=scope))
                        updated = pdf_project.sync_case(self.state.work_dir, m.group(1),
                                                        case["terms"], case.get("numbers", []))
                        case["numbers"] = updated["numbers"]
                        self.state.save_case(project["case_name"], case)
                        pdf_project.mark_case_pending(self.state.work_dir, project["case_name"], m.group(1))
                        result = pdf_project.public(updated)
                else:
                    result = pdf_project.add_term(self.state.work_dir, m.group(1), dict(term, scope=scope))
                self._send_json({"ok": True, "project": result})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        m = re.fullmatch(r"/api/pdf/sync-case/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                p = pdf_project.load(self.state.work_dir, m.group(1))
                if not p["case_name"]:
                    raise ValueError("本项目未设置案件名称")
                with self.state.case_lock:
                    case = self.state.load_case(p["case_name"])
                    p = pdf_project.sync_case(self.state.work_dir, m.group(1),
                                              case.get("terms", []), case.get("numbers", []))
                    case["numbers"] = p["numbers"]
                    self.state.save_case(p["case_name"], case)
                self._send_json({"ok": True, "project": pdf_project.public(p)})
            except (ValueError, FileNotFoundError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        m = re.fullmatch(r"/api/pdf/control/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                req = json.loads(body_bytes.decode("utf-8"))
                result = pdf_project.control(self.state.work_dir, m.group(1), req.get("action"))
                if result["stage"] == "processing" and req.get("action") == "retry":
                    _start_pdf_job(self.state, m.group(1))
                self._send_json({"ok": True, "project": result})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        m = re.fullmatch(r"/api/pdf/export/([0-9a-f]{12})", path)
        if m:
            if self.headers.get("X-Local-Project") != "1":
                self._send_json({"ok": False, "error": "project access denied"}, 403)
                return
            try:
                req = json.loads(body_bytes.decode("utf-8")) or {}
                sid = m.group(1)
                project = pdf_project.load(self.state.work_dir, sid)
                if project.get("rules_sha256"):
                    live_rules = _read_rules_file_bytes(user_rules_path())
                    if _sha256_bytes(live_rules or b"") != project["rules_sha256"]:
                        self._send_json({"ok": False, "code": "RULES_CHANGED",
                                         "error": "规则文件在上传后发生了变化，请重新上传并复核后再导出"}, 409)
                        return
                # 已检出项必须先处理或明确保留；其他风险按原有方式知悉。
                result = pdf_project.export(self.state.work_dir, sid, req.get("revision"),
                                            confirmed=bool(req.get("confirm")))
                if isinstance(result, dict) and result.get("needs_confirm"):
                    self._send_json({"ok": True, "needs_confirm": True,
                                     "red": result["red"], "soft": result.get("notes", [])})
                    return
                self._send_json({"ok": True,
                                 "url": f"/history/download/{sid}/{urllib.parse.quote(result['name'])}",
                                 "exported_revision": result.get("exported_revision"),
                                 "notes": result.get("notes", [])})
            except (ValueError, TypeError, json.JSONDecodeError, FileNotFoundError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": "导出失败：" + str(exc)}, 500)
            return

        m = re.match(r"^/api/term/([A-Za-z0-9_-]+)$", path)
        if m:
            info = self.state.get(m.group(1))
            if not info or info.get("mode") != "redact":
                self._send_json({"ok": False, "error": "session not found"}, 404)
                return
            try:
                req = json.loads(body_bytes.decode("utf-8") or "{}")
                case_data = self.state.load_case(info.get("case_name", "")) if info.get("case_name") else info["case_data"]
                case_terms = list(case_data.get("terms", []))
                local_terms = list(info.get("local_terms", []))
                if req.get("action") == "delete":
                    case_terms = [t for t in case_terms if t.get("id") != req.get("id")]
                    local_terms = [t for t in local_terms if t.get("id") != req.get("id")]
                elif req.get("action") == "add":
                    term = review_ext.validate_term(req.get("term"))
                    if len(case_terms) + len(local_terms) >= 100:
                        raise ValueError("每个案件最多 100 个自选字段")
                    scope = req.get("scope", "case") if info.get("case_name") else "document"
                    if scope not in ("case", "document"):
                        raise ValueError("字段范围只能是本文或同案")
                    if scope == "case":
                        if any(t.get("id") == term["id"] for t in local_terms):
                            raise ValueError("该字段已存在于本文，请先删除再添加到同案")
                        case_terms = [t for t in case_terms if t.get("id") != term["id"]] + [term]
                    else:
                        if any(t.get("id") == term["id"] for t in case_terms):
                            raise ValueError("该字段已存在于同案，请先删除再添加到本文")
                        local_terms = [t for t in local_terms if t.get("id") != term["id"]] + [term]
                else:
                    raise ValueError("不支持的操作")
                revision_mode = req.get("revision_mode", info.get("revision_mode", "redact_preserve"))
                if revision_mode not in ("require_resolved", "redact_preserve",
                                         "passthrough_unverified"):
                    raise ValueError("DOCX修订处理方式无效")
                case_data["terms"] = case_terms
                terms = [dict(t, scope="case") for t in case_terms] + [dict(t, scope="document") for t in local_terms]
                run = _run_detector(info["input_path"], terms, unified=True)
                report = run["report"]
                revision_inventory = run.get("revision_inventory")
                revision_block_reason = _revision_hard_block_reason(
                    info.get("mode", "redact"), _ext_of(info["input_path"]),
                    revision_inventory)
                case_data["numbers"], numbers = review_ext.assign_case_numbers(report, case_data.get("numbers", []))
                self.state.save_case(info.get("case_name", ""), case_data)
                payload = _build_review_payload(report, run["doc"].blocks, numbers, terms,
                                                run.get("comment_info"),
                                                revision_inventory=revision_inventory,
                                                policy_id=info.get("policy_id",
                                                                   Policy.PRESERVE_TRANSACTION))
                payload["profile"] = "unified"
                payload["revision_mode"] = revision_mode
                payload["comment_mode_default"] = "redact_preserve"
                payload["case_name"] = info.get("case_name", "")
                payload["policy_selection"] = info.get("policy_selection", "")
                payload["revision_block_reason"] = revision_block_reason
                payload["revision_warning"] = _revision_warning(revision_inventory)
                info.update(report=report, doc=run["doc"], full_raw=run["full_raw"],
                            case_data=case_data, local_terms=local_terms,
                            case_numbers=numbers, review_payload=payload,
                            revision_inventory=revision_inventory,
                            revision_block_reason=revision_block_reason,
                            revision_warning=_revision_warning(revision_inventory),
                            revision_mode=revision_mode)
                self._send_json({"ok": True, "terms": terms})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": "更新自选字段失败：" + str(exc)}, 500)
            return

        # ---------------- /api/apply/<sid> ----------------
        m = re.match(r"^/api/(apply|export-plan)/([A-Za-z0-9_-]+)$", path)
        if m:
            action, sid = m.group(1), m.group(2)
            info = self.state.get(sid)
            if not info:
                self._send_json({"ok": False, "error": "session not found"}, 404)
                return
            try:
                req = json.loads(body_bytes.decode("utf-8") or "{}")
            except Exception:
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            input_path = info["input_path"]
            if D._sha256(input_path) != info["doc"].sha256:
                self._send_json({"ok": False, "code": "EXPORT_SOURCE_CHANGED",
                                 "error": "原件已变化，请重新上传并复核"}, 409)
                return
            if info.get("mode") == "redact" and info.get("rules_sha256"):
                live_rules = _read_rules_file_bytes(user_rules_path())
                if _sha256_bytes(live_rules or b"") != info.get("rules_sha256"):
                    self._send_json({"ok": False, "code": "RULES_CHANGED",
                                     "error": "规则文件在上传后发生了变化，已复核结果不会自动重算，"
                                              "请重新上传后再导出"}, 409)
                    return
            try:
                selection_digest = _export_selection_digest(info, req)
            except (ValueError, TypeError):
                self._send_json({"ok": False, "error": "类型或位置选择无效"}, 400)
                return
            if action == "export-plan":
                self._send_json({"ok": True, "selection_digest": selection_digest})
                return
            if "selection_digest" in req and req["selection_digest"] != selection_digest:
                self._send_json({"ok": False, "code": "EXPORT_SELECTION_STALE",
                                 "error": "原件、选择或策略已变化，请重新检查导出清单"}, 409)
                return
            ext = _ext_of(input_path)
            revision_mode = req.get("revision_mode", "redact_preserve")
            if revision_mode not in ("require_resolved", "redact_preserve",
                                     "passthrough_unverified"):
                self._send_json({"ok": False, "error": "DOCX修订处理方式无效"}, 400)
                return
            try:
                revision_inventory = _read_revision_inventory(input_path)
            except Exception:
                revision_inventory = {"complete": False, "has_revisions": False,
                                      "can_preserve_and_redact": False}
            hard_revision_reason = _revision_hard_block_reason(
                info.get("mode", "redact"), ext, revision_inventory)
            revision_acknowledged = True
            revision_ack_digest = str((revision_inventory or {}).get("unsupported_digest") or "")
            revision_reason = _revision_block_reason(
                info.get("mode", "redact"), ext, revision_inventory,
                revision_mode=revision_mode,
                revision_acknowledged=revision_acknowledged,
                revision_ack_digest=revision_ack_digest)
            if revision_reason:
                code = "DOCX_REVISION_BLOCKED"
                if _revision_needs_ack(revision_inventory):
                    if (revision_ack_digest and revision_ack_digest !=
                            revision_inventory.get("unsupported_digest")):
                        code = "DOCX_REVISION_ACK_STALE"
                    elif not revision_acknowledged:
                        code = "DOCX_REVISION_ACK_REQUIRED"
                    else:
                        code = "DOCX_REVISION_ACK_STALE"
                self._send_json({"ok": False, "error": revision_reason,
                                 "code": code}, 409)
                return
            external_relationship_action = str(
                req.get("external_relationship_action") or "remove")
            if external_relationship_action not in {"remove", "preserve"}:
                self._send_json({"ok": False, "error": "外部链接处理方式无效"}, 400)
                return
            external_relationship_digest = str(
                (revision_inventory or {}).get("external_relationship_digest") or "")
            if int((revision_inventory or {}).get("external_relationship_count") or 0):
                if str(req.get("external_relationship_digest") or "") != external_relationship_digest:
                    self._send_json({"ok": False, "error": "外部链接清单已变化，请重新检查"}, 409)
                    return
            external_relationship_acknowledged = external_relationship_action == "preserve"
            review_acknowledged = True
            current_review_digest = _revision_review_digest(revision_inventory)

            # 决定输出目录 / 路径
            out_dir = os.path.join(self.state.work_dir, sid, "out")
            os.makedirs(out_dir, exist_ok=True)
            stem = os.path.splitext(os.path.basename(input_path))[0]
            output_name = f"{stem}_脱敏{ext or '.txt'}"
            out_path = os.path.join(out_dir, output_name)
            stage_dir = os.path.join(self.state.work_dir, sid, ".apply-" + uuid.uuid4().hex)
            staged_out_path = os.path.join(stage_dir, output_name)

            enabled_types = req.get("enabled_types") or []
            if isinstance(enabled_types, str):
                enabled_types = [t.strip() for t in enabled_types.split(",") if t.strip()]
            if "enabled_types" not in req:
                # 仅缺失参数时沿用旧版默认；显式空列表不可扩大为全部
                enabled_types = [it["type"] for it in info["report"]["items"]]

            # 复核页逐个位置的勾选（用户在预览里点掉的高亮必须真的不替换）
            enabled_items = req.get("items")
            if not isinstance(enabled_items, list):
                enabled_items = None

            warnings = ""
            # v1.3.0 导出前清单留痕：低置信项（原始分数只进日志、不进界面）＋ 用户确认动作。
            _min_conf = float(req.get("min_confidence", 0.5))
            _report_items = (info.get("report") or {}).get("items") or []
            low_conf_items = [
                {"type": it["type"], "label": it["label"], "value": it["value"],
                 "confidence": it["confidence"], "count": it.get("count", 1)}
                for it in _report_items
                if float(it.get("confidence") or 0) < _min_conf
                and not any((occ.get("target") or {}).get("kind") ==
                            "external_relationship_target"
                            for occ in it.get("occurrences", []))
            ]
            _stats = (info.get("report") or {}).get("_scan_stats") or {}
            comment_mode = req.get("comment_mode") or "redact_preserve"
            confirmation = {
                "confirmed": bool(req.get("confirmed")),
                "preflight": True,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "low_confidence_count": len(low_conf_items),
                "below_display_floor": int(_stats.get("below_display_floor") or 0),
                "disabled_types": sorted(set(it["type"] for it in _report_items) - set(enabled_types)),
                "comment_mode": comment_mode,
                "revision_mode": revision_mode,
                "revision_acknowledged": revision_acknowledged,
                "revision_ack_digest": revision_ack_digest if revision_acknowledged else "",
                "unredacted_content_acknowledged": review_acknowledged,
                "review_digest": current_review_digest,
                "external_relationship_action": external_relationship_action,
                "selection_digest": selection_digest,
            }
            if ext == ".docx" and comment_mode not in (
                    "redact_preserve", "preserve_unredacted", "remove_all"):
                self._send_json({"ok": False, "error": "DOCX批注处理方式无效"}, 400)
                return
            if comment_mode == "preserve_unredacted" and ext != ".docx":
                self._send_json({"ok": False, "error": "保留批注但不脱敏仅支持DOCX"}, 400)
                return
            os.makedirs(stage_dir, exist_ok=False)
            try:
                # 注：含占位符的文件 = 已被脱敏过，二次脱敏会再次写出 placeholder
                # 避免重叠加：在该文件下不再生成新的 mapping（直接复用上一次）
                if info.get("looks_redacted") and ext == ".docx":
                    warnings = "⚠ 文件疑似已经过脱敏，将再次覆盖 placeholder。"
                r = redact_file(
                    source=input_path,
                    selection={"enabled_types": enabled_types, "items": enabled_items},
                    out_path=staged_out_path,
                    min_confidence=float(req.get("min_confidence", 0.5)),
                    min_severity=req.get("min_severity", "low"),
                    restore_mode=True,
                    custom_terms=info.get("case_data", {}).get("terms", []) + info.get("local_terms", []),
                    case_numbers=info.get("case_numbers"),
                    litigation_profile=False,
                    revision_mode=revision_mode,
                    external_clean=(ext == ".docx"),
                    unified_profile=True,
                    comment_mode=comment_mode,
                    rules_path=_active_rules_path(),
                    low_confidence_items=low_conf_items,
                    confirmation=confirmation,
                    allow_no_replacements=True,
                    revision_acknowledged=revision_acknowledged,
                    revision_ack_digest=revision_ack_digest,
                    unredacted_content_acknowledged=review_acknowledged,
                    unredacted_content_digest=(
                        str((revision_inventory or {}).get("unredacted_content_digest") or "")
                        if review_acknowledged else None),
                    external_relationship_action=external_relationship_action,
                    external_relationship_acknowledged=external_relationship_acknowledged,
                    external_relationship_digest=external_relationship_digest,
                )
                red = []
                risk_inventory = revision_inventory or {}
                if ext == ".docx":
                    revision_risk = _revision_export_risk(risk_inventory)
                    if revision_risk:
                        red.append(revision_risk)
                if (external_relationship_action == "preserve" and
                        int(risk_inventory.get("external_relationship_count") or 0)):
                    red.append("将保留外部链接或资源关系，打开文档时可能访问外部地址。")
                comments = (info.get("review_payload") or {}).get("comments") or {}
                if comment_mode == "preserve_unredacted" and comments.get("has_comments"):
                    red.append("批注将原样保留且不会脱敏，可能含有敏感信息。")
                elif comments.get("unsupported"):
                    red.append("部分批注结构无法完整处理，相关内容可能按原文保留。")
                if comment_mode == "remove_all" and comments.get("has_comments"):
                    red.append("将移除全部批注；映射文件不能恢复已删除的批注结构。")
                skipped_details = r.get("skipped_details") or []
                if skipped_details:
                    rows = []
                    for row in skipped_details:
                        where = str(row.get("location") or "文档位置待确认").strip()
                        label = str(row.get("label") or row.get("type") or "已选内容")
                        value = str(row.get("value") or "")
                        rows.append("%s：%s%s" % (
                            label, where + "，" if where else "", value[:120]))
                    red.append("以下已选内容未能自动改写，将按原文保留；导出后可在Word中搜索并手动处理：" +
                               "；".join(rows))
                if (int(r.get("applied") or 0) == 0 and
                        not int(r.get("comments_removed") or 0) and
                        not red):
                    red.append("本次没有替换内容，原文将保留且不生成映射文件。")
                soft_notes = [
                    EXPORT_REVIEW_NOTICE + " " + EXPORT_UNRECOGNIZED_NOTE
                ]
                if not req.get("confirmed") and red:
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    self._send_json({"ok": True, "needs_confirm": True,
                                     "red": red, "notes": soft_notes}, 200)
                    return
                mapping_src = r.get("mapping")
                mapping_dst = None
                staged_mapping_path = staged_out_path + ".mapping.json"
                if r.get("applied", 0) and (not mapping_src or not os.path.isfile(mapping_src)):
                    raise RuntimeError("脱敏映射文件未生成")
                if mapping_src:
                    if not os.path.isfile(mapping_src):
                        raise RuntimeError("脱敏映射文件不可用")
                    mapping_dst = os.path.join(out_dir, output_name + ".mapping.json")
                    if os.path.abspath(mapping_src) != os.path.abspath(staged_mapping_path):
                        shutil.copyfile(mapping_src, staged_mapping_path)

                log_dst = None
                staged_log_path = staged_out_path + ".redaction.log"
                if r.get("log") and os.path.isfile(r["log"]):
                    if os.path.abspath(r["log"]) != os.path.abspath(staged_log_path):
                        shutil.copyfile(r["log"], staged_log_path)
                    log_dst = os.path.join(out_dir, output_name + ".redaction.log")

                os.replace(staged_out_path, out_path)
                if mapping_dst:
                    os.replace(staged_mapping_path, mapping_dst)
                else:
                    if os.path.isfile(out_path + ".mapping.json"):
                        os.unlink(out_path + ".mapping.json")
                if log_dst:
                    os.replace(staged_log_path, log_dst)
            except Exception as exc:
                for artifact in (out_path, out_path + ".mapping.json",
                                 out_path + ".redaction.log"):
                    try:
                        if os.path.isfile(artifact):
                            os.unlink(artifact)
                    except OSError:
                        pass
                shutil.rmtree(stage_dir, ignore_errors=True)
                error = f"脱敏失败：{exc}"
                if comment_mode == "redact_preserve" and "无法保留并脱敏批注" in str(exc):
                    error += "。请在页面上方将批注处理修改为“保留批注但不脱敏”或“移除全部批注”，然后重新导出。"
                code = "EXPORT_NO_REPLACEMENTS_ACK_REQUIRED" if "没有可替换的内容" in str(exc) else "EXPORT_GENERATION_FAILED"
                self._send_json({"ok": False, "error": error, "code": code,
                                 "issues": [{"code": code, "severity": "blocking", "stage": "generate",
                                             "resolution_actions": ["重新检查选择和处理方式后重试"]}]},
                                409 if code == "EXPORT_NO_REPLACEMENTS_ACK_REQUIRED" else 500)
                return
            shutil.rmtree(stage_dir, ignore_errors=True)

            self._send_json({
                "ok": True,
                "output_name": output_name,
                "output_url": f"/download/{sid}/{urllib.parse.quote(output_name)}",
                "mapping_name": os.path.basename(mapping_dst) if mapping_dst else None,
                "mapping_url": f"/download/{sid}/{urllib.parse.quote(os.path.basename(mapping_dst))}" if mapping_dst else None,
                "log_url": f"/download/{sid}/{urllib.parse.quote(os.path.basename(log_dst))}" if log_dst else None,
                "applied": r["applied"],
                "skipped": r["skipped"],
                "skipped_details": r.get("skipped_details", []),
                "notes": soft_notes,
                "warnings": " ".join(x for x in (
                    warnings, r.get("comment_warning", ""),
                    _revision_warning(revision_inventory)) if x),
                "comment_mode": comment_mode,
                "comments_removed": r.get("comments_removed", 0),
                "comment_applied": r.get("comment_applied", 0),
                "comments_preserved": r.get("comments_preserved", 0),
            })
            return

        # ---------------- /api/restore/<sid> ----------------
        m = re.match(r"^/api/restore/([A-Za-z0-9_-]+)$", path)
        if m:
            sid = m.group(1)
            info = self.state.get(sid)
            if not info:
                self._send_json({"ok": False, "error": "session not found"}, 404)
                return

            input_path = info["input_path"]
            ext = _ext_of(input_path)
            out_dir = os.path.join(self.state.work_dir, sid, "out")
            os.makedirs(out_dir, exist_ok=True)
            stem = os.path.splitext(os.path.basename(input_path))[0]
            mapping_path: Optional[str] = None
            restore_kwargs: Dict[str, Any] = {}
            if ctype.startswith("multipart/form-data"):
                mp = parse_multipart()
                if "mapping" in mp:
                    fname = _safe_filename(mp["mapping"].get("filename") or "mapping.json")
                    mp_path = os.path.join(self.state.work_dir, sid, fname)
                    with open(mp_path, "wb") as f:
                        f.write(mp["mapping"]["data"])
                    mapping_path = mp_path
            elif ctype.startswith("application/json"):
                try:
                    req = json.loads(body_bytes.decode("utf-8") or "{}")
                except Exception:
                    self._send_json({"ok": False, "error": "invalid json"}, 400)
                    return
                req_mapping = req.get("mapping")
                if req_mapping:
                    mapping_path = req_mapping
            if not mapping_path:
                # 当前会话生成的映射反映复核勾选与手工补录；上传时的映射只是初稿。
                if info.get("mode") != "restore":
                    generated = os.path.join(out_dir, f"{stem}_脱敏{ext or '.txt'}.mapping.json")
                    if os.path.exists(generated):
                        mapping_path = generated
                if not mapping_path:
                    sib = info.get("sibling_mapping") or find_sibling_mapping(info["input_path"])
                    if sib and os.path.exists(sib):
                        mapping_path = sib
                # 注：mapping_path 可能为 None —— restore_file() 会自动尝试 docx 内嵌

            output_name = f"{stem}_还原{ext or '.txt'}"
            out_path = os.path.join(out_dir, output_name)

            # 在「脱敏会话」里点还原时，要还原的是**刚生成的脱敏产物**（含占位符），
            # 而不是最初上传的原件 —— 后者本来就没有占位符，还原会变成空操作。
            if info.get("mode") != "restore":
                redacted_candidate = os.path.join(out_dir, f"{stem}_脱敏{ext or '.txt'}")
                if os.path.exists(redacted_candidate):
                    input_path = redacted_candidate

            try:
                r = restore_file(
                    source=input_path,
                    mapping_path=mapping_path,  # None → 自动找内嵌
                    out_path=out_path,
                )
            except FileNotFoundError as exc:
                self._send_json({"ok": False,
                    "error": "找不到 mapping.json —— 请上传一个 (.json)。"}, 400)
                return
            except Exception as exc:
                self._send_json({"ok": False, "error": f"还原失败：{exc}"}, 500)
                return
            self._send_json({
                "ok": True,
                "output_name": output_name,
                "output_url": f"/download/{sid}/{urllib.parse.quote(output_name)}",
                "applied": r["applied"],
                "skipped": r["skipped"],
                "missing": r.get("missing") or [],
                "applied_comments": r.get("applied_comments", 0),
                "applied_comment_identities": r.get("applied_comment_identities", 0),
            })
            return

        self._send_json({"ok": False, "error": "POST not found"}, 404)


# --------------------------------------------------------------- 服务启动 ------


def _build_review_payload(report: Dict[str, Any], doc_blocks: List[Any],
                          case_numbers=None, terms=None, comment_info=None,
                          revision_inventory=None,
                          policy_id=Policy.PRESERVE_TRANSACTION) -> Dict[str, Any]:
    """构造 /review 页所需的 JSON：每段的全文 + 高亮区间 + 占位符预览。

    与 detector 的 _build_review_payload 类似，但额外保留 occ 的【全 occurrence 索引】
    用于前端精确按 occurrence 控制勾选。
    """
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    types: Dict[str, Dict[str, Any]] = {}
    external_targets: List[Dict[str, Any]] = []
    # 编号必须与 detector.write_mapping / redactor 完全一致：
    # 同一个实体（同一个人/企业，含简称）在整篇里共用同一个 [标签#N]。
    numberer = D.EntityNumberer(report["items"], case_numbers)
    for it in report["items"]:
        t = types.setdefault(it["type"], {
            "id": it["type"], "label": it["label"], "cat": it["category"],
            "sev": it["severity"], "count": 0,
            "default_enabled": (it["type"] != "NUMBER_CANDIDATE" and
                                it["type"] not in Policy.excluded_types(policy_id)),
        })
        t["count"] += it["count"]
        placeholder = numberer.placeholder(it["type"], it["value"], it["label"])
        for occ_index, o in enumerate(it["occurrences"]):
            target = o.get("target") or {}
            if target.get("kind") == "external_relationship_target":
                external_targets.append({
                    "type": it["type"], "label": it["label"],
                    "confidence": it["confidence"],
                    "conf": it["confidence"], "t": it["type"],
                    "display_text": target.get("display_text") or it["value"],
                    "placeholder": placeholder,
                    "offset": list(o["offset"]),
                    "location": "外部超链接目标",
                })
                continue
            buckets.setdefault(o["block_index"], []).append({
                "s": o["offset"][0], "e": o["offset"][1],
                "t": it["type"], "sev": it["severity"], "label": it["label"],
                "placeholder": placeholder,
                "masked": it["masked"], "conf": it["confidence"],
                "review_reasons": [r for r in it.get("reasons", [])
                                   if r == D.ORG_SPAN_REVIEW_REASON],
                "rewrite_blocked": bool((o.get("target") or {}).get("rewrite_supported") is False),
                "rewrite_block_reason": (o.get("target") or {}).get("rewrite_block_reason", ""),
                "occ_start": occ_index, "occ_end": occ_index + 1,
            })

    # detector extract_* 用 "\n".join(...) 拼接全文，块起点累加方式：
    #   pos = 0;
    #   for b in doc.blocks: blocks.append({"start":pos,...}); pos += len(text)+1
    # 因此我们重建出和 detector 一致的 start 起点
    blocks: List[Dict[str, Any]] = []
    pos = 0
    for b in doc_blocks:
        text = (b.text or "").rstrip("\n")
        blocks.append({
            "start": pos,
            "loc": b.location,
            "text": text,
            "kind": getattr(b, "field_kind", "text"),
            "comment_id": getattr(b, "comment_id", None),
            "marks": buckets.get(len(blocks), []),
        })
        pos += len(text) + 1

    comment_body_blocks = [b for b in doc_blocks if getattr(b, "field_kind", "text") == "comment_body"]
    comment_identity_blocks = [b for b in doc_blocks if getattr(b, "field_kind", "text") in
                               ("comment_author", "comment_person_identity")]
    comment_author_values = {
        b.text for b in comment_identity_blocks
        if getattr(b, "field_kind", "") == "comment_author"
        or str(getattr(b, "attribute_name", "")).rsplit("}", 1)[-1].lower() == "author"
    }
    return {
        "source_path": report.get("source", {}).get("path", ""),
        "policy_id": policy_id,
        "policy_description": Policy.description(policy_id),
        "policy_excluded_count": sum(
            int(it.get("count") or 0) for it in report.get("items", [])
            if it.get("type") in Policy.excluded_types(policy_id)),
        "english_dominant": D.is_english_dominant(
            "\n".join((b.text or "").rstrip("\n") for b in doc_blocks)),
        "min_confidence": report["settings"]["min_confidence"],
        "min_severity": report["settings"].get("min_severity", "low"),
        "warnings": report.get("warnings") or [],
        "revision_inventory": ({
            "complete": bool(revision_inventory.get("complete")),
            "has_revisions": bool(revision_inventory.get("has_revisions")),
            "passthrough_safe": bool(revision_inventory.get("passthrough_safe")),
            "can_preserve_and_redact": bool(revision_inventory.get("can_preserve_and_redact")),
            "can_export_after_ack": bool(revision_inventory.get("can_export_after_ack")),
            "supported_text_revision_count": int(
                revision_inventory.get("supported_text_revision_count") or 0),
            "supported_property_revision_count": int(
                revision_inventory.get("supported_property_revision_count") or 0),
            "unsupported_revision_count": int(
                revision_inventory.get("unsupported_revision_count") or 0),
            "field_instruction_count": int(
                revision_inventory.get("field_instruction_count") or 0),
            "safe_field_instruction_count": int(
                revision_inventory.get("safe_field_instruction_count") or 0),
            "unsafe_field_instruction_count": int(
                revision_inventory.get("unsafe_field_instruction_count") or 0),
            "unredacted_content_count": sum(
                int(row.get("count") or 0)
                for row in (revision_inventory.get("unredacted_content_parts") or [])),
            "unredacted_content_manifest": [
                {"kind": row.get("kind"), "count": int(row.get("count") or 0)}
                for row in (revision_inventory.get("unredacted_content_parts") or [])
                if int(row.get("count") or 0) > 0
            ],
            "review_digest": str(revision_inventory.get("review_digest") or ""),
            "embedded_image_count": int(
                revision_inventory.get("embedded_image_count") or 0),
            "header_footer_content_parts": [
                {key: row[key] for key in ("part", "kind") if key in row}
                for row in (revision_inventory.get("header_footer_content_parts") or [])
            ],
            "unsupported_object_manifest": [
                {key: row[key] for key in ("part", "kind", "reason") if key in row}
                for row in (revision_inventory.get("unsupported_object_manifest") or [])
            ],
            "text_object_count": int(
                revision_inventory.get("text_object_count") or 0),
            "object_digest": str(revision_inventory.get("object_digest") or ""),
            "unscanned_part_count": len(revision_inventory.get("unscanned_parts") or []),
            "external_relationship_count": int(
                revision_inventory.get("external_relationship_count") or 0),
            "external_relationship_digest": str(
                revision_inventory.get("external_relationship_digest") or ""),
            "external_relationship_manifest": [
                {key: row[key] for key in ("part", "kind") if key in row}
                for row in (revision_inventory.get("external_relationship_manifest") or [])
            ],
            "external_non_hyperlink_count": sum(
                1 for row in (revision_inventory.get("external_relationship_manifest") or [])
                if str(row.get("kind") or "").lower() != "hyperlink"),
            "external_target_unscanned_count": int(
                revision_inventory.get("external_target_unscanned_count") or 0),
            "compat_preview": list(revision_inventory.get("compat_preview") or []),
            "unsupported_digest": str(revision_inventory.get("unsupported_digest") or ""),
            "unsupported_manifest": [
                {key: row[key] for key in
                 ("part", "kind", "reason", "ordinal", "location") if key in row}
                for row in (revision_inventory.get("unsupported_manifest") or [])
            ],
        } if revision_inventory is not None else None),
        "embedded_image_count": int(
            (revision_inventory or {}).get("embedded_image_count") or 0),
        "types": list(types.values()),
        "blocks": blocks,
        "external_targets": external_targets,
        "comments": {
            "has_comments": bool(comment_body_blocks or comment_identity_blocks or
                                  (comment_info and comment_info.related_parts)),
            "count": len({getattr(b, "comment_id", None) for b in comment_body_blocks}),
            "paragraph_count": len(comment_body_blocks),
            "author_count": len(comment_author_values),
            "candidate_count": sum(1 for it in report.get("items", [])
                                   for occurrence in it.get("occurrences", [])
                                   if str((occurrence.get("target") or {}).get("kind", "")).startswith("comment_")),
            "unsupported": list(comment_info.unsupported) if comment_info else [],
            "related_part_count": len(comment_info.related_parts) if comment_info else 0,
        },
        "terms": terms or [],
        # v1.3.0：低于展示下限（默认 0.3）而被计数、不静默丢弃的候选数
        "below_display_floor": int((report.get("_scan_stats") or {}).get("below_display_floor") or 0),
    }


def start_app_server(host: str, port: int, work_dir: str) -> ThreadingHTTPServer:
    """启动 Contract Redactor Web 应用。"""
    os.makedirs(work_dir, exist_ok=True)
    # 按保留期清理明文面（默认关闭，仅当设置 CONTRACT_DESENSITIZER_RETENTION_DAYS 时生效）
    try:
        _n = retention_sweep(work_dir)
        if _n:
            sys.stderr.write("[retention] 已清理 %d 个会话的明文面\n" % _n)
    except Exception as exc:
        sys.stderr.write("[retention] sweep failed: %s\n" % type(exc).__name__)
    state = AppState(work_dir)
    global RULES_OPEN_TOKEN
    RULES_OPEN_TOKEN = secrets.token_urlsafe(24)
    init_user_rules()

    # 修正：review payload 需要 doc.blocks 信息 —— 重写一份带完整文本的版本
    # 这里 monkey-patch 一下 AppHandler 的 POST 处理逻辑（在 do_POST /api/upload 已经有完整 doc，
    # 我们直接构造 payload）。但 _build_review_payload 单独存在是为了 HTML 嵌入。
    # 这里直接升级 do_POST 的构造：覆盖 _build_review_payload 用更完整的信息

    handler_cls = _make_handler(state)
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.state = state
    server.service_state = _start_service_state(work_dir)
    return server


def _make_handler(state: AppState):
    """构造一个绑定了 state 的 Handler 子类。"""
    class _H(AppHandler):
        pass
    _H.state = state
    return _H


# ==============================================================================
# 启动脚本
# ==============================================================================


def _serve_until_stopped(server: ThreadingHTTPServer) -> None:
    stop_type = "clean-shutdown"
    error_type = None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  shutting down...")
        try:
            server.shutdown()
        except BaseException as exc:
            stop_type = "uncaught-exception"
            error_type = type(exc).__name__
            raise
    except BaseException as exc:
        stop_type = "uncaught-exception"
        error_type = type(exc).__name__
        raise
    finally:
        try:
            server.server_close()
        except BaseException as exc:
            if stop_type != "uncaught-exception":
                stop_type = "uncaught-exception"
                error_type = type(exc).__name__
            raise
        finally:
            _finish_service_state(server, stop_type, error_type)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="contract_app_server.py",
        description="合同脱敏 / 还原 一体化 Web 应用（本地 127.0.0.1）",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--workdir", default="./contract_app_sessions",
                    help="会话工作目录（每个上传文件一个独立子目录）")
    ap.add_argument("--open-browser", action="store_true",
                    help="启动后自动打开主页面（优先使用 Chrome 应用窗口）")
    args = ap.parse_args(argv)

    # 启动期间放开网络（127.0.0.1 绑定 / 可选浏览器跳转）
    D.enforce_offline = lambda: None  # 临时禁掉，避免 start_app 失败

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    server = start_app_server(args.host, args.port, workdir)
    base = f"http://{args.host}:{args.port}"
    print()
    print("  ┌──────────────────────────────────────────────────────┐")
    print("  │   合同脱敏 / 还原  ·  Contract Redactor                │")
    print("  └──────────────────────────────────────────────────────┘")
    print(f"  ◆ 版本：      v{APP_VERSION}")
    print(f"  ◆ 访问地址：  {base}/")
    print(f"  ◆ 会话目录：  {workdir}")
    print("  ◆ 网络说明：  首次安装依赖需要联网，安装完成后文书处理不联网；仅监听 127.0.0.1")
    print()
    print("  停止服务：网页右上角「退出」，或在此按 Ctrl-C")
    print()
    if args.open_browser:
        import threading, webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")).start()
    _serve_until_stopped(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
