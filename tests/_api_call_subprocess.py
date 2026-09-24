"""Makes exactly one API call from a freshly spawned process (see test_api_safety.py's
`call_over_socket`). A literal file, not an inline `-c` script or an f-string embedding "TROUPE_AGENT"
as text, so its own command line and source never accidentally match the peer-env check it's used
to exercise.

argv: <root> <method> <params-json>
stdout: one line of {"ok": true, "result": ...} or {"ok": false, "code": ..., "message": ...}
"""
import json
import sys
from pathlib import Path

from troupe.api_client import Client

root, method, params_json = sys.argv[1], sys.argv[2], sys.argv[3]
client = Client(Path(root))
try:
    outcome = {"ok": True, "result": client.call(method, json.loads(params_json))}
except Exception as exc:
    outcome = {"ok": False, "code": getattr(exc, "code", None), "message": str(exc)}
finally:
    client.close()
print(json.dumps(outcome))
