"""DSH adapter: private environment and the unchanged R99 local service."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "serve"])
    parser.add_argument("--data-root", required=True)
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    package = Path(__file__).resolve().parent
    core = package / "core"
    if args.action == "prepare":
        environment = root / "environment"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        check = "import docx,pdfplumber,pypdf,reportlab,pypdfium2,PIL,regex; from lxml import etree"
        valid = python.exists() and subprocess.run(
            [str(python), "-c", check], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20).returncode == 0
        if not valid:
            # Rebuild only our environment after an interrupted or incompatible setup.
            subprocess.run([sys.executable, "-m", "venv", "--clear", str(environment)], check=True)
            subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                            "--timeout", "20", "--retries", "1", "-r",
                            str(core / "scripts/requirements.txt"),
                            "-c", str(package / "constraints.txt"),
                            "reportlab<5; python_version < '3.10'"], check=True)
            subprocess.run([str(python), "-c", check], check=True, timeout=20)
        return
    os.environ["CONTRACT_DESENSITIZER_RULES_DIR"] = str(root / "rules")
    sys.path.insert(0, str(core / "scripts"))
    import contract_app_server as server
    import presentation
    presentation.install(server)
    server.D.enforce_offline = lambda: None  # Same loopback startup behavior as R99 main().
    httpd = server.start_app_server("127.0.0.1", 0, str(root / "sessions"))
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
