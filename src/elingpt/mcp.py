"""Dependency-free JSON-RPC MCP stdio adapter; stdout is protocol-only."""

from __future__ import annotations

import json
import sys
import time
from typing import Any, BinaryIO, TextIO

from . import __version__
from .client import MAX_REQUEST, BridgeClient, BridgeError
from .guide import gameplay_guide, gameplay_instructions
from .observation import ObservationStream, VIEW_INSTRUCTIONS, VIEW_VERSION, ui_decision_view
from .runner import wait_for_action
from .telemetry import Metrics
from .tools import ALL_TOOLS, TOOLS, bridge_arguments, validate

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


class McpServer:
    def __init__(self, client: BridgeClient, *, metrics: TextIO | None = None, compat_tools: bool = False):
        self.client = client
        self.initialized = False
        self.observations = ObservationStream()
        self.metrics = Metrics(metrics)
        self.tools = ALL_TOOLS if compat_tools else TOOLS

    @staticmethod
    def error(identifier: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}

    def handle(self, request: Any) -> dict[str, Any] | None:
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or
                not isinstance(request.get("method"), str)):
            return self.error(None, -32600, "Invalid JSON-RPC request")
        method = request["method"]
        if "id" not in request:
            return None
        identifier = request["id"]
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            return self.error(None, -32600, "Request ID must be a string or integer")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.error(identifier, -32602, "Parameters must be an object")
        if method == "initialize":
            version = params.get("protocolVersion")
            self.initialized = True
            result = {"protocolVersion": version if version in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[-1],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "elingpt", "version": __version__},
                      "instructions": "Claim control explicitly. Use one action at a time; poll its ID only if "
                      "still running. Human input/F9 takes over; never automatically reclaim.\n" +
                      gameplay_instructions() + "\n" + VIEW_INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return self.error(identifier, -32000, "Initialize the MCP session first")
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            started = time.monotonic()
            try:
                if not isinstance(name, str):
                    raise BridgeError("invalid_argument", "Tool name must be a string.")
                bridge_method = validate(name, args)
                if bridge_method == "action_status":
                    value = wait_for_action(self.client, args["action_id"], time.monotonic() + 10,
                                            cancel_on_timeout=False)
                else:
                    value = self.client.call(bridge_method, bridge_arguments(args))
                if bridge_method == "ui_observe" and args.get("view", "decisions") == "decisions":
                    value = ui_decision_view(value, **{key: args[key] for key in
                        ("offset", "page_id", "max_bytes") if key in args})
                if bridge_method in {"act", "ui_act", "ui_choose"} and not value.get("quitting"):
                    value = wait_for_action(self.client, value["action"]["action_id"], time.monotonic() + 10,
                                            cancel_on_timeout=False)
                if bridge_method == "claim":
                    self.client.start_heartbeat()
                value = self.observations.present(value, full=args.get("mode") == "full", since=args.get("since"))
                if bridge_method == "ping":
                    value = {**value, "gameplay_guide": value.get("gameplay_guide", gameplay_guide()),
                             "connector": {"view_version": VIEW_VERSION,
                             "metrics": {key: dict(item) for key, item in self.metrics.tools.items()}}}
                self.metrics.record(name, value, elapsed_ms=round((time.monotonic() - started) * 1000))
                content = [{"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}]
                result = {"content": content, "isError": False}
            except BridgeError as error:
                value = {"error": {"code": error.code, "message": error.message}}
                self.metrics.record(name if isinstance(name, str) else "invalid_tool_call", value,
                                    elapsed_ms=round((time.monotonic() - started) * 1000))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False,
                           separators=(",", ":"))}], "isError": True}
        else:
            return self.error(identifier, -32601, f"Unknown method: {method}")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def run(self, source: BinaryIO | None = None, output: TextIO | None = None) -> None:
        source = source or sys.stdin.buffer
        output = output or sys.stdout
        self.client.start_heartbeat()
        try:
            while True:
                line = source.readline(MAX_REQUEST + 1)
                if not line:
                    return
                if len(line) > MAX_REQUEST:
                    print(json.dumps(self.error(None, -32600, "Request exceeds 64 KiB")), file=output, flush=True)
                    return
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeError):
                    response = self.error(None, -32700, "Invalid JSON")
                else:
                    response = self.handle(request)
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False), file=output, flush=True)
        finally:
            self.client.close()
