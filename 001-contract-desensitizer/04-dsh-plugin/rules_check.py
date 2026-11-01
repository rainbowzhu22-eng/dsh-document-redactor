"""Check an Agent proposal against the unchanged R99 rule parser; never apply it."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'core' / 'scripts'))
import contract_sensitive_detector as detector

try:
    previous = detector.build_rules_snapshot(sys.argv[1])
    proposed = detector.build_rules_snapshot(
        sys.argv[2], previous_custom_ids=previous['custom_ids'])
    print(json.dumps({'ok': True, 'deleted': proposed['pending_deletions']}, ensure_ascii=False))
except Exception as error:
    print(json.dumps({'ok': False, 'message': str(error)[:1200]}, ensure_ascii=False))
    sys.exit(1)
