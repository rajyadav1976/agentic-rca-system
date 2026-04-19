
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import json
import traceback
import signal
from plc_rca_tools import *  # Import all @Tool-decorated functions


# Build tool registry from @Tool-decorated functions
TOOL_REGISTRY = {fn._tool_name: fn for fn in globals().values() if hasattr(fn, '_tool_name')}
print("Registered tools:", list(TOOL_REGISTRY.keys()), file=sys.stderr)
sys.stderr.flush()

def handle_signal(signum, frame):
    print(f"MCP server received signal {signum}, shutting down.", file=sys.stderr)
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

print("MCP server started", file=sys.stderr)
sys.stderr.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        req = json.loads(line)
        method = req.get("method")
        params = req.get("params", {})
        req_id = req.get("id")
        if method not in TOOL_REGISTRY:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}
            }
        else:
            try:
                result = TOOL_REGISTRY[method](**params)
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result
                }
            except Exception as e:
                print(f"Error in tool '{method}': {e}", file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)
                sys.stderr.flush()
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32000,
                        "message": str(e),
                        "data": traceback.format_exc()
                    }
                }
    except Exception as e:
        # Fatal parse error
        resp = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32700,
                "message": f"Parse error: {str(e)}",
                "data": traceback.format_exc()
            }
        }
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
