"""DSH adapter: private environment and the local document service."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

DOWNLOAD_SOURCES = {
    "default": None,
    "pypi": "https://pypi.org/simple",
    "tuna": "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
}


def _progress(stage):
    print(json.dumps({"type": "preparation", "stage": stage}), flush=True)


def _environment_valid(python, core, windows):
    if not python.is_file():
        return False
    check = "import docx,pdfplumber,pypdf,reportlab,pypdfium2,PIL,regex; from lxml import etree"
    if windows:
        check += ("; import importlib,onnxruntime; "
                  "[importlib.import_module('winrt.windows.' + m) for m in "
                  "('media.ocr','graphics.imaging','storage','foundation',"
                  "'foundation.collections','storage.streams')]; "
                  "import sys; sys.path.insert(0, " + repr(str(core / 'scripts')) + "); "
                  "from contract_pdf_win_ocr import _rapid_engine; _rapid_engine()")
    try:
        return subprocess.run([str(python), "-c", check], stdout=sys.stderr,
                              stderr=sys.stderr, timeout=120 if windows else 30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prepare_environment(root, package, source="default"):
    """Repair in the stable venv path; keep installed packages and pip's cache on failure."""
    if source not in DOWNLOAD_SOURCES:
        raise ValueError("INVALID_DOWNLOAD_SOURCE")
    core = package / "core"
    environment = root / "environment"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _progress("checking")
    if _environment_valid(python, core, os.name == "nt"):
        _progress("ready")
        return
    _progress("environment")
    # No --clear: interrupted installs retain their packages, cache and stable paths.
    # A valid existing environment returned above is never recreated.
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True,
                   stdout=sys.stderr, stderr=sys.stderr)
    subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True,
                   stdout=sys.stderr, stderr=sys.stderr)
    _progress("installing")
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check",
               "--no-input", "--timeout", "60", "--retries", "3", "-r",
               str(core / "scripts/requirements.txt"), "-c", str(package / "constraints.txt"),
               "reportlab<5; python_version < '3.10'"]
    if DOWNLOAD_SOURCES[source]:
        command.extend(["--index-url", DOWNLOAD_SOURCES[source]])
    subprocess.run(command, check=True, stdout=sys.stderr, stderr=sys.stderr)
    _progress("validating")
    if not _environment_valid(python, core, os.name == "nt"):
        raise RuntimeError("ENVIRONMENT_VALIDATION_FAILED")
    _progress("ready")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "serve"])
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--source", choices=tuple(DOWNLOAD_SOURCES), default="default")
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    package = Path(__file__).resolve().parent
    core = package / "core"
    if args.action == "prepare":
        prepare_environment(root, package, args.source)
        return
    os.environ["CONTRACT_DESENSITIZER_RULES_DIR"] = str(root / "rules")
    sys.path.insert(0, str(core / "scripts"))
    import contract_app_server as server
    import presentation
    presentation.install(server)
    server.D.enforce_offline = lambda: None  # Same loopback startup behavior as R99 main().
    httpd = server.start_app_server("127.0.0.1", 0, str(root / "sessions"))
    server._prewarm_pdf_ocr()
    # The private stdin pipe is held only by our owner. EOF also covers an owner
    # crash or direct termination, where plugin disposal cannot run.
    def stop_when_owner_exits():
        try:
            while sys.stdin.buffer.read(1):
                pass
        except (OSError, ValueError):
            pass
        httpd.shutdown()

    threading.Thread(target=stop_when_owner_exits, daemon=True).start()
    print(json.dumps({"type": "ready", "url": "http://127.0.0.1:%s/" % httpd.server_port,
                      "revision": server.APP_REVISION}), flush=True)
    server._serve_until_stopped(httpd)


if __name__ == "__main__":
    main()
