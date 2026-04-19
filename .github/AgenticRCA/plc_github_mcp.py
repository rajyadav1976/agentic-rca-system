"""
plc_github_mcp.py - MCP tool registration and Tool decorator

This module provides the @Tool decorator for registering tool functions
with the MCP server. It maintains a global registry of tools and exposes
utility functions for tool discovery and invocation.
"""

import functools
import inspect
import threading

# Global registry for all tools
_TOOL_REGISTRY = {}
_TOOL_REGISTRY_LOCK = threading.Lock()

def Tool(name=None, description=None):
    """
    Decorator to register a function as an MCP tool.
    Args:
        name (str): Name of the tool (defaults to function name)
        description (str): Description of the tool
    """
    def decorator(func):
        tool_name = name or func.__name__
        tool_desc = description or func.__doc__ or ""
        with _TOOL_REGISTRY_LOCK:
            _TOOL_REGISTRY[tool_name] = {
                "function": func,
                "description": tool_desc,
                "signature": inspect.signature(func)
            }
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        wrapper._tool_name = tool_name
        wrapper._tool_description = tool_desc
        return wrapper
    return decorator

def get_tool_registry():
    """Return the global tool registry (read-only)."""
    with _TOOL_REGISTRY_LOCK:
        return dict(_TOOL_REGISTRY)

def get_tool(name):
    """Get a registered tool by name."""
    with _TOOL_REGISTRY_LOCK:
        return _TOOL_REGISTRY.get(name)

def list_tools():
    """List all registered tool names."""
    with _TOOL_REGISTRY_LOCK:
        return list(_TOOL_REGISTRY.keys())

# For MCP server: import this file and call get_tool_registry() to discover all tools.

# --- FastMCP: Minimal, ready-to-use MCP client for subprocess JSON-RPC communication ---
import subprocess
import threading
import json
import queue
import uuid
import sys
import time

class FastMCP:
    """
    FastMCP: Minimal client for communicating with an MCP server subprocess via JSON-RPC over stdin/stdout.
    Usage:
        mcp = FastMCP(cmd=["python", "/tmp/rca/mcp_server.py"])
        result = mcp.call("tool_name", {"arg1": val1, ...})
    """
    def __init__(self, provider=None, cmd=None, timeout=60):
        import threading
        import sys
        self.provider = provider
        self.cmd = cmd or [sys.executable, "/tmp/rca/mcp_server.py"]
        self.timeout = timeout
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=True,
            )
        except Exception as e:
            print(f"[FastMCP] Failed to start MCP server subprocess: {e}", file=sys.stderr)
            raise
        self._lock = threading.Lock()
        self._responses = {}
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        # Start a thread to log stderr in real time
        self._stderr_thread = threading.Thread(target=self._log_stderr, daemon=True)
        self._stderr_thread.start()

    def _log_stderr(self):
        """Log MCP server stderr in real time for debugging."""
        while True:
            if self._proc.stderr is None:
                break
            line = self._proc.stderr.readline()
            if not line:
                break
            print(f"[MCP-STDERR] {line.rstrip()}", file=sys.stderr)

    def _reader(self):
        while True:
            line = self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
                msg_id = msg.get("id")
                if msg_id:
                    with self._lock:
                        self._responses[msg_id] = msg
            except Exception:
                continue

    def call(self, method, params=None):
        """
        Call a tool method on the MCP server.
        Args:
            method (str): Tool name
            params (dict): Arguments for the tool
        Returns:
            Result from the MCP server (dict or value)
        """
        msg_id = str(uuid.uuid4())
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params or {},
        }
        line = json.dumps(request) + "\n"
        with self._lock:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        # Wait for response
        start = time.time()
        while time.time() - start < self.timeout:
            with self._lock:
                if msg_id in self._responses:
                    resp = self._responses.pop(msg_id)
                    if "result" in resp:
                        return resp["result"]
                    elif "error" in resp:
                        raise RuntimeError(f"MCP error: {resp['error']}")
            time.sleep(0.05)
        raise TimeoutError(f"No response from MCP server for method {method}")

    def close(self):
        if self._proc:
            self._proc.terminate()
            self._proc = None
