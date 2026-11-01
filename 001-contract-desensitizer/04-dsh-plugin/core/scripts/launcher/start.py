#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法律文书脱敏助手 · 跨平台启动器（Windows / macOS / Linux 通用）

用法：
    python start.py                 启动本地服务并自动打开浏览器
    python start.py --foreground    前台运行，日志留在当前终端，Ctrl-C 结束

行为：
    1. 找到带依赖的 Python；没有就自动建 .venv 并安装 requirements.txt（仅首次）
    2. 检查 18800 端口；仅对确认属于本工具的旧服务请求优雅退出
    3. 启动 contract_app_server.py（安装完成后文书处理不联网，只监听 127.0.0.1）
    4. 健康检查通过后打开 http://127.0.0.1:18800/

只依赖标准库，不需要 bash / bat / vbs。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("REDACTOR_PORT", "18800"))
APP_URL = f"http://127.0.0.1:{PORT}/"
APP_VERSION = "1.3.0-dev"
APP_REVISION = "r99"
REQUIRED = ("docx", "pdfplumber", "pypdf", "reportlab", "pypdfium2", "PIL", "regex")
WINDOWS_REQUIRED = (
    "rapidocr", "onnxruntime", "winrt.windows.foundation",
    "winrt.windows.foundation.collections", "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr", "winrt.windows.storage",
    "winrt.windows.storage.streams",
)
MIN_PY = (3, 9)


def here() -> Path:
    return Path(__file__).resolve().parent


def project_dir() -> Path:
    return here().parent


def sessions_dir(proj: Path) -> Path:
    configured = os.environ.get("CONTRACT_DESENSITIZER_SESSIONS_DIR")
    if configured:
        return Path(configured).expanduser()
    legacy = proj / "sessions"
    if legacy.is_dir() and any(p.is_dir() and len(p.name) == 12 for p in legacy.iterdir()):
        return legacy  # preserve existing v1.2.3 local history without copying private files
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ContractDesensitizer/sessions"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return base / "ContractDesensitizer/sessions"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "contract-desensitizer/sessions"


def rules_data_dir() -> Path:
    """用户规则目录；独立于 sessions 目录解析，不受旧版目录回退影响。"""
    configured = os.environ.get("CONTRACT_DESENSITIZER_RULES_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support/ContractDesensitizer"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ContractDesensitizer"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "contract-desensitizer"
    return base / "Rules"


def has_deps(python: Path) -> bool:
    required = REQUIRED + (WINDOWS_REQUIRED if os.name == "nt" else ())
    code = "import " + ", ".join(required)
    try:
        return subprocess.run([str(python), "-c", code],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def pick_python(proj: Path) -> Path | None:
    venv_py = proj / ".venv" / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")
    candidates = [venv_py, Path(sys.executable)]
    found = os.environ.get("PYTHON")
    if found:
        candidates.append(Path(found))
    for c in candidates:
        if c.exists() and has_deps(c):
            return c
    return None


def bootstrap(proj: Path) -> Path:
    """建虚拟环境并装依赖，返回可用的 python 路径。"""
    if sys.version_info < MIN_PY:
        raise SystemExit(f"✗ 需要 Python {MIN_PY[0]}.{MIN_PY[1]}+，当前是 "
                         f"{sys.version_info.major}.{sys.version_info.minor}")
    venv = proj / ".venv"
    print("· 首次运行：创建虚拟环境…")
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")
    print("· 安装依赖（约 1~2 分钟，仅此一次）…")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                   check=False)
    subprocess.run([str(py), "-m", "pip", "install", "--quiet",
                    "-r", str(proj / "requirements.txt")], check=True)
    return py


def port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def service_health() -> dict | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(APP_URL + "health", timeout=1) as response:
            data = json.loads(response.read(4096))
        if isinstance(data, dict) and data.get("service") == "contract_app_server":
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _is_current_service(health: dict | None) -> bool:
    return bool(health and health.get("version") == APP_VERSION and
                health.get("revision") == APP_REVISION)


def request_shutdown() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(APP_URL + "api/shutdown", data=b"", method="POST")
    opener.open(request, timeout=2).close()


def prepare_port() -> bool:
    """返回 True 表示同版本服务已运行；不按端口盲杀其他进程。"""
    current = service_health()
    if _is_current_service(current):
        return True
    if current:
        try:
            request_shutdown()
        except OSError as exc:
            raise SystemExit(f"旧版服务未能正常退出：{exc}") from exc
        for _ in range(30):
            if not port_open():
                return False
            time.sleep(0.2)
        raise SystemExit("旧版服务未释放端口 18800，请在网页中退出后重试。")
    if port_open():
        raise SystemExit("端口 18800 已被其他程序占用，未结束该程序。请先释放端口或联系管理员。")
    return False


def wait_ready(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        health = service_health()
        if _is_current_service(health):
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="法律文书脱敏助手启动器")
    ap.add_argument("--foreground", action="store_true", help="前台运行，Ctrl-C 结束")
    ap.add_argument("--stop", action="store_true", help="只停止本工具服务，不结束占用端口的其他程序")
    args = ap.parse_args()

    if args.stop:
        if service_health():
            request_shutdown()
            print("已请求停止法律文书脱敏助手服务。")
        elif port_open():
            raise SystemExit("端口由其他程序占用，未结束该程序。")
        else:
            print("法律文书脱敏助手服务未运行。")
        return 0

    proj = project_dir()
    py = pick_python(proj)
    if py is None:
        py = bootstrap(proj)

    if prepare_port():
        print(f"✓ 已有 v{APP_VERSION} 服务运行：{APP_URL}")
        webbrowser.open(APP_URL)
        return 0
    workdir = sessions_dir(proj)
    workdir.mkdir(parents=True, exist_ok=True)
    rules_dir = rules_data_dir()
    rules_dir.mkdir(parents=True, exist_ok=True)

    print(f"· 项目目录：{proj}")
    print(f"· 规则目录：{rules_dir}")
    print(f"· Python   ：{py}")
    print(f"· 启动服务（安装完成后文书处理不联网，仅监听 127.0.0.1:{PORT}）…")

    child_env = dict(os.environ, CONTRACT_DESENSITIZER_RULES_DIR=str(rules_dir))
    cmd = [str(py), str(proj / "contract_app_server.py"),
           "--host", "127.0.0.1", "--port", str(PORT),
           "--workdir", str(workdir)]
    if args.foreground:
        print("前台运行模式：当前窗口承载本地脱敏服务，使用期间请保持窗口开启；关闭窗口会中断网页连接。按 Ctrl+C 正常停止。")
        proc = subprocess.Popen(cmd, cwd=str(proj), env=child_env)
    else:
        log = workdir / "server.log"
        log_f = open(log, "ab")
        popen_kwargs = dict(cwd=str(proj), stdout=log_f,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=child_env)
        if os.name == "nt":
            # Windows .bat 退出后服务仍需独立运行。
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
        else:
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        finally:
            log_f.close()

    if not wait_ready():
        proc.terminate()
        print(f"✗ 服务启动失败，日志：{workdir / 'server.log'}", file=sys.stderr)
        return 1

    print(f"✓ 服务已就绪：{APP_URL}")
    webbrowser.open(APP_URL)
    if args.foreground:
        print("前台服务正在此窗口运行。关闭窗口会停止服务；按 Ctrl+C 结束。")
    else:
        print("服务已在后台运行，可以关闭启动器窗口。停止服务：网页右上角「退出」，或运行 start.py --stop。")

    if args.foreground:
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
