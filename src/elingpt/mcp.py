"""Dependency-free JSON-RPC MCP stdio adapter; stdout is protocol-only."""

from __future__ import annotations

import json
import sys
import time
from typing import Any, BinaryIO, TextIO

from . import __version__
from .client import MAX_REQUEST, BridgeClient, BridgeError
from .observation import ObservationStream, ui_decision_view
from .runner import wait_for_action
from .tools import TOOLS, validate

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


class McpServer:
    def __init__(self, client: BridgeClient):
        self.client = client
        self.initialized = False
        self.observations = ObservationStream()

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
                      "instructions": "Observe before acting. Claim control explicitly. Use one action at a time "
                      "and query its status. Mutations/status may return delta observations: apply state_delta as "
                      "top-level replacements, state_removed as deletions, ui_delta.meta as metadata, and UI upsert/"
                      "removed by node ID, based on base_revision. terrain_delta upserts rows keyed by their first "
                      "two columns (x,z), removes listed coordinates, and optionally supplies the complete coordinate "
                      "order; otherwise retain order and append new rows. Observe returns a full baseline whenever needed. "
                      "Terrain includes all valid tiles within player position +/- limits.radius, without character "
                      "line-of-sight filtering. Removed coordinates leave the observation window, not the world; "
                      "a cleared wall is an upsert with block=0. Entity visibility and action legality remain restricted. "
                      "A running action is not complete; poll its ID without resubmitting. "
                      "Human input/F9 takes over; never automatically reclaim control."}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return self.error(identifier, -32000, "Initialize the MCP session first")
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            try:
                if not isinstance(name, str):
                    raise BridgeError("invalid_argument", "Tool name must be a string.")
                bridge_method = validate(name, args)
                bridge_args = {key: value for key, value in args.items() if key != "view"}
                value = self.client.call(bridge_method, bridge_args)
                if bridge_method == "ui_observe" and args.get("view") == "decisions":
                    value = ui_decision_view(value)
                if bridge_method in {"act", "ui_act", "ui_choose"} and not value.get("quitting"):
                    value = wait_for_action(self.client, value["action"]["action_id"], time.monotonic() + 2,
                                            cancel_on_timeout=False)
                if bridge_method == "claim":
                    self.client.start_heartbeat()
                content = [{"type": "text", "text": json.dumps(self.observations.present(value, full=bridge_method == "observe"), ensure_ascii=False,
                                                               separators=(",", ":"))}]
                result = {"content": content, "isError": False}
            except BridgeError as error:
                result = {"content": [{"type": "text", "text": json.dumps(
                    {"error": {"code": error.code, "message": error.message}})}], "isError": True}
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
