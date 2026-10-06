"""Minimal Model Context Protocol server over stdio (JSON-RPC 2.0, one message per line).

Written against the public MCP specification; no third-party packages needed.
Only the `tools` capability is offered.
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


class ToolError(Exception):
    """Raised by a tool to report a problem the agent can act on."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[..., Any]
    read_only: bool = True
    annotations: dict = field(default_factory=dict)


@dataclass
class Image:
    """An image result, sent to the client as an image content block."""
    data: bytes
    mime: str = "image/png"


@dataclass
class Server:
    name: str
    version: str
    instructions: str = ""
    tools: dict[str, Tool] = field(default_factory=dict)

    def tool(self, name: str, description: str, properties: dict | None = None,
             required: list[str] | None = None, read_only: bool = True, annotations: dict | None = None):
        def deco(fn):
            schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
            if required:
                schema["required"] = required
            self.tools[name] = Tool(name, description, schema, fn, read_only, dict(annotations or {}))
            return fn
        return deco

    # ---------------------------------------------------------------- protocol
    def _handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        rid = msg.get("id")
        is_request = "id" in msg and method is not None
        if method is None:
            return None                      # a response to something we never asked: ignore

        def ok(result: dict) -> dict:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def err(code: int, message: str) -> dict:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

        if method == "initialize":
            wanted = (msg.get("params") or {}).get("protocolVersion")
            version = wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
            if self.instructions:
                result["instructions"] = self.instructions
            return ok(result)
        if method == "ping":
            return ok({})
        if method == "tools/list":
            tools = []
            for t in self.tools.values():
                tools.append({
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.schema,
                    "annotations": {"readOnlyHint": t.read_only, "openWorldHint": False, **t.annotations},
                })
            return ok({"tools": tools})
        if method == "tools/call":
            params = msg.get("params") or {}
            tool = self.tools.get(params.get("name", ""))
            if tool is None:
                return err(-32602, f"unknown tool: {params.get('name')}")
            args = params.get("arguments") or {}
            try:
                out = tool.fn(**args)
                return ok({"content": _to_content(out), "isError": False})
            except ToolError as e:
                return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
            except TypeError as e:
                return ok({"content": [{"type": "text", "text": f"bad arguments for {tool.name}: {e}"}], "isError": True})
            except Exception as e:  # a bug on our side: say so instead of dying
                traceback.print_exc(file=sys.stderr)
                return ok({"content": [{"type": "text", "text": f"internal error in {tool.name}: {type(e).__name__}: {e}"}],
                           "isError": True})
        if not is_request:
            return None                      # notifications (initialized, cancelled, ...) need no answer
        return err(-32601, f"method not found: {method}")

    def serve(self) -> None:
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        while True:
            line = stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
                stdout.write(json.dumps(reply).encode("utf-8") + b"\n")
                stdout.flush()
                continue
            batch = msg if isinstance(msg, list) else [msg]
            replies = [r for r in (self._handle(m) for m in batch if isinstance(m, dict)) if r is not None]
            if not replies:
                continue
            payload = replies if isinstance(msg, list) else replies[0]
            stdout.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


def _to_content(out: Any) -> list[dict]:
    """Turns whatever a tool returned into MCP content blocks."""
    import base64

    items = out if isinstance(out, (list, tuple)) else [out]
    content: list[dict] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, Image):
            content.append({"type": "image", "data": base64.b64encode(item.data).decode("ascii"), "mimeType": item.mime})
        elif isinstance(item, str):
            content.append({"type": "text", "text": item})
        else:
            content.append({"type": "text", "text": json.dumps(item, ensure_ascii=False, indent=1)})
    if not content:
        content.append({"type": "text", "text": "(no output)"})
    return content
