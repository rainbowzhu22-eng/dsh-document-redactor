"""Run the released document service with the shared presentation and local-only data."""
import functools
import json
import os
from pathlib import Path
import sys
import threading

root, package = map(Path, sys.argv[1:])
os.umask(0o077)
os.environ["CONTRACT_DESENSITIZER_RULES_DIR"] = str(root / "rules")
sys.path.insert(0, str(package))
sys.path.insert(0, str(package / "core/scripts"))
import contract_app_server as server
import presentation

presentation.DATA_AND_AI = presentation.DATA_AND_AI.replace("DSH", "WorkBuddy")
presentation.install(server)
server.redact_file = functools.partial(server.redact_file, write_log=False)
server.D.enforce_offline = lambda: None
httpd = server.start_app_server("127.0.0.1", 0, str(root / "sessions"))
server._prewarm_pdf_ocr()

def stop_when_owner_exits():
    try:
        while sys.stdin.buffer.read(1):
            pass
    finally:
        httpd.shutdown()

threading.Thread(target=stop_when_owner_exits, daemon=True).start()
print(json.dumps({"type": "ready", "url": f"http://127.0.0.1:{httpd.server_port}/",
                  "revision": server.APP_REVISION}), flush=True)
server._serve_until_stopped(httpd)
