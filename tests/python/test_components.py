from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from elingpt.client import BridgeClient, BridgeError
from elingpt.mcp import McpServer
from elingpt.runner import ModelClient, run, wait_for_action
from elingpt.tools import TOOLS, validate

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "ElinGPT.Core.Tests" / "bin" / "Release" / "net9.0" / "ElinGPT.Core.Tests.dll"


class StubClient:
    def __init__(self):
        self.calls = []
        self.has_control = False
        self.closed = False
        self.revision = 1
        self.status = "playing"
        self.ui = {"active": False, "format": "gameobject_tree", "roots": ["canvas"],
                   "nodes": [{"id": "canvas", "parent_id": None, "role": "canvas", "actions": []}]}
        self.action_status = "completed"

    def start_heartbeat(self):
        pass

    def close(self):
        self.closed = True
        self.has_control = False

    def call(self, method, args=None):
        self.calls.append((method, args))
        if method == "claim":
            self.has_control = True
        if method == "observe" or method == "claim":
            return {"session_id": "s", "revision": self.revision, "status": self.status,
                    "state": {"ui": self.ui}}
        if method in {"act", "ui_act", "ui_choose"}:
            return {"action": {"action_id": "a"}}
        if method == "ui_observe":
            return {"revision": self.revision, "ui": self.ui}
        if method == "action_status":
            return {"action": {"status": self.action_status}, "observation": {}}
        return {}


class ToolTests(unittest.TestCase):
    def test_native_held_rotation_action(self):
        args = {"session_id": "s", "expected_revision": 1, "kind": "rotate_held"}
        self.assertEqual(validate("elin_act", args), "act")
        with self.assertRaises(BridgeError):
            validate("elin_act", {**args, "direction": -1})

    def test_tool_names_and_required_action_fields(self):
        self.assertEqual(len(TOOLS), 14)
        self.assertNotIn("elin_capture", {tool["name"] for tool in TOOLS})
        self.assertEqual(validate("elin_observe", {}), "observe")
        good = {"session_id": "s", "expected_revision": 1, "kind": "step", "dx": 1, "dz": 0}
        self.assertEqual(validate("elin_act", good), "act")
        for bad in [{**good, "dx": True}, {**good, "dx": 2},
                    {**good, "dx": 0}, {**good, "extra": 1},
                    {"session_id": "s", "expected_revision": 1, "kind": "context"}]:
            with self.subTest(bad=bad), self.assertRaises(BridgeError):
                validate("elin_act", bad)

    def test_unknown_and_non_object(self):
        with self.assertRaises(BridgeError):
            validate("exec", {})
        with self.assertRaises(BridgeError):
            validate("elin_observe", [])

    def test_native_session_and_text_field_inputs(self):
        base = {"session_id": "s", "expected_revision": 1}
        self.assertEqual(validate("elin_session_command", {**base, "command": "save"}), "session_command")
        self.assertEqual(validate("elin_ui_set_text", {**base, "field_id": "name", "value": ""}), "ui_set_text")
        with self.assertRaises(BridgeError):
            validate("elin_session_command", {**base, "command": "delete_save"})
        with self.assertRaises(BridgeError):
            validate("elin_ui_set_text", {**base, "field_id": "name", "value": "x" * 257})

    def test_generic_tree_query_and_node_operations(self):
        base = {"session_id": "s", "expected_revision": 1}
        self.assertEqual(validate("elin_ui_observe", {"root_id": "canvas", "depth": 8, "limit": 256,
                                                    "include_inactive": True}), "ui_observe")
        self.assertEqual(validate("elin_ui_observe", {"view": "decisions"}), "ui_observe")
        with self.assertRaises(BridgeError):
            validate("elin_ui_observe", {"view": "screenshot"})
        for args in [{"operation": "click", "node_id": "button"},
                     {"operation": "key", "key": "enter"},
                     {"operation": "key", "key": "i"},
                     {"operation": "set_text", "node_id": "input", "value": ""},
                     {"operation": "set_value", "node_id": "toggle", "value": True},
                     {"operation": "scroll", "node_id": "list", "dx": 0, "dy": -1},
                     {"operation": "drag", "node_id": "item", "target_id": "slot"}]:
            self.assertEqual(validate("elin_ui_act", {**base, **args}), "ui_act")
        for args in [{"operation": "click"}, {"operation": "key", "key": "console"},
                     {"operation": "set_text", "node_id": "input", "value": {}},
                     {"operation": "scroll", "node_id": "list", "dx": 0, "dy": float("inf")}]:
            with self.assertRaises(BridgeError):
                validate("elin_ui_act", {**base, **args})


class McpTests(unittest.TestCase):
    def setUp(self):
        self.client = StubClient()
        self.server = McpServer(self.client)

    def initialize(self):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-03-26"}})

    def test_initialization_without_game_connection(self):
        result = self.initialize()
        self.assertEqual(result["result"]["protocolVersion"], "2025-03-26")
        self.assertFalse(self.client.calls)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_require_initialization(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(response["error"]["code"], -32000)

    def test_mutation_waits_and_returns_decision_boundary(self):
        self.initialize()
        result = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "elin_ui_act", "arguments": {"session_id": "s", "expected_revision": 1,
                                              "operation": "key", "key": "enter"}}})["result"]
        value = json.loads(result["content"][0]["text"])
        self.assertEqual(value["action"]["status"], "completed")
        self.assertEqual([name for name, _ in self.client.calls], ["ui_act", "action_status"])

    def test_text_only_content_and_tool_errors(self):
        self.initialize()
        for name in ["elin_observe", "elin_ui_observe", "unknown", "elin_capture"]:
            result = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": name, "arguments": {}}})["result"]
            if name in {"unknown", "elin_capture"}:
                self.assertTrue(result["isError"])
            else:
                self.assertFalse(result["isError"])
                self.assertEqual(result["content"][0]["type"], "text")

    def test_ui_decisions_are_opt_in_and_never_sent_as_game_arguments(self):
        self.initialize()
        self.client.ui["nodes"].append({"id": "button", "label": "Accept", "actions": ["click"]})
        for view in ("tree", "decisions"):
            result = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "elin_ui_observe", "arguments": {"view": view, "root_id": "canvas"}}})["result"]
            self.assertFalse(result["isError"])
            value = json.loads(result["content"][0]["text"])
            self.assertEqual(value["ui"]["format"], "decision_nodes" if view == "decisions" else "gameobject_tree")
            self.assertEqual(self.client.calls[-1], ("ui_observe", {"root_id": "canvas"}))
        self.assertEqual(self.client.ui["format"], "gameobject_tree")

    def test_stdio_output_and_eof_cleanup(self):
        source = io.BytesIO(b'{broken}\n{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        output = io.StringIO()
        self.server.run(source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertTrue(self.client.closed)

    def test_invalid_rpc_shapes(self):
        for request in [[], {}, {"jsonrpc": "2.0", "id": True, "method": "ping"}]:
            self.assertEqual(self.server.handle(request)["error"]["code"], -32600)


class RunnerTests(unittest.TestCase):
    def test_remote_privacy_and_url_guards(self):
        with self.assertRaises(BridgeError):
            ModelClient("https://example.invalid/v1", "model", "KEY", False)
        with self.assertRaises(BridgeError):
            ModelClient("http://example.invalid/v1", "model", "KEY", True)
        with self.assertRaises(BridgeError):
            ModelClient("https://secret@example.invalid/v1", "model", "KEY", True)
        client = ModelClient("http://127.0.0.1:1234/v1", "model", "KEY", False)
        self.assertEqual(client.url, "http://127.0.0.1:1234/v1/chat/completions")

    def test_normal_bounded_run_and_no_extra_mutation(self):
        class Model:
            def complete(self, messages, **kwargs):
                action = {"session_id": "s", "expected_revision": 1, "kind": "wait"}
                return {"role": "assistant", "content": "Wait once.",
                        "tool_calls": [{"id": "1", "type": "function", "function": {
                            "name": "elin_act", "arguments": json.dumps(action)}},
                                       {"id": "2", "type": "function", "function": {
                                           "name": "elin_act", "arguments": json.dumps(action)}}]}
        client = StubClient()
        with patch("sys.stdout", new=io.StringIO()):
            run(client, Model(), "Wait", max_steps=1)
        self.assertEqual(sum(method == "act" for method, _ in client.calls), 1)
        self.assertTrue(client.closed)

    def test_generic_ui_does_not_need_screen_whitelist(self):
        class Model:
            def complete(self, messages, **kwargs):
                assert "CustomUnknownPanel" in messages[-1]["content"]
                return {"role": "assistant", "content": "Observed the generic tree."}
        client = StubClient()
        client.ui["nodes"].append({"id": "custom", "name": "CustomUnknownPanel", "parent_id": "canvas",
                                   "role": "button", "actions": ["click"]})
        with patch("sys.stdout", new=io.StringIO()):
            run(client, Model(), "Walk")
        self.assertTrue(client.closed)

    def test_runner_supports_the_same_compact_ui_query(self):
        class Model:
            calls = 0

            def complete(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"role": "assistant", "tool_calls": [{"id": "1", "type": "function", "function": {
                        "name": "elin_ui_observe", "arguments": '{"view":"decisions","root_id":"canvas"}'}}]}
                result = json.loads(next(message["content"] for message in messages if message["role"] == "tool"))
                assert result["ui"]["format"] == "decision_nodes"
                return {"role": "assistant", "content": "Read the compact UI."}

        client = StubClient()
        with patch("sys.stdout", new=io.StringIO()):
            run(client, Model(), "Read UI", max_steps=2)
        self.assertIn(("ui_observe", {"root_id": "canvas"}), client.calls)

    def test_runner_continues_through_native_death_ui(self):
        client = StubClient()
        client.status = "dead"
        client.ui["active"] = True
        client.ui["nodes"].append({"id": "last-words", "parent_id": "canvas", "role": "text_input",
                                   "label": "Last words", "actions": ["set_text", "submit"]})
        test = self

        class Model:
            calls = 0

            def complete(self, messages, **kwargs):
                self.calls += 1
                observation = json.loads(messages[-1]["content"].split("\n", 1)[1])
                if self.calls == 1:
                    test.assertEqual(observation["status"], "dead")
                    test.assertIn("Last words", messages[-1]["content"])
                    return {"role": "assistant", "tool_calls": [{"id": "1", "type": "function", "function": {
                        "name": "elin_ui_act",
                        "arguments": '{"operation":"key","key":"enter","session_id":"s","expected_revision":1}'}}]}
                test.assertEqual(observation["status"], "playing")
                return {"role": "assistant", "content": "Revived through the native UI."}

        native_call = client.call

        def call(method, args=None):
            result = native_call(method, args)
            if method == "ui_act":
                client.status = "playing"
                client.ui["active"] = False
                client.revision += 1
            return result

        with patch.object(client, "call", side_effect=call), patch("sys.stdout", new=io.StringIO()):
            run(client, Model(), "Continue after death", max_steps=2)
        self.assertIn(("ui_act", {"operation": "key", "key": "enter", "session_id": "s",
                                 "expected_revision": 1}), client.calls)
        self.assertEqual(sum(method == "claim" for method, _ in client.calls), 1)
        self.assertTrue(client.closed)

    def test_runner_waits_for_death_transition_without_issuing_input(self):
        client = StubClient()
        client.status = "dead"

        class Model:
            def complete(self, *args, **kwargs):
                raise AssertionError("No model decision before the native death dialog is ready")

        with patch("elingpt.runner.time.sleep") as sleep, patch("sys.stdout", new=io.StringIO()):
            run(client, Model(), "Continue after death", max_steps=2)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(any(method in {"act", "ui_act"} for method, _ in client.calls))
        self.assertTrue(client.closed)

    def test_awaiting_ui_is_not_repeated_action(self):
        client = StubClient()
        client.action_status = "awaiting_ui"
        result = wait_for_action(client, "a", float("inf"))
        self.assertEqual(result["action"]["status"], "awaiting_ui")
        self.assertEqual(len(client.calls), 1)

    def test_pending_ui_input_waits_for_quit_acknowledgement(self):
        client = StubClient()
        with patch.object(client, "call", side_effect=[
            {"action": {"status": "awaiting_ui"}, "observation": {"state": {"ui": {"input_busy": True}}}},
            {"action": {"status": "completed"}, "quitting": True},
        ]) as call, patch("elingpt.runner.time.sleep"):
            result = wait_for_action(client, "a", float("inf"))
        self.assertTrue(result["quitting"])
        self.assertEqual(call.call_count, 2)

    def test_mcp_poll_budget_does_not_cancel_running_work(self):
        client = StubClient()
        client.action_status = "running"
        result = wait_for_action(client, "a", 0, cancel_on_timeout=False)
        self.assertEqual(result["action"]["status"], "running")
        self.assertEqual([name for name, _ in client.calls], ["action_status"])

    def test_transcript_open_error_still_releases(self):
        client = StubClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.jsonl"
            path.write_text("")
            with self.assertRaises(FileExistsError):
                run(client, object(), "Wait", transcript=path)
        self.assertTrue(client.closed)

    def test_malformed_model_tool_is_explicit_and_releases(self):
        class Model:
            def complete(self, *args, **kwargs):
                return {"role": "assistant", "tool_calls": [{"id": "1", "function": []}]}
        client = StubClient()
        with self.assertRaisesRegex(BridgeError, "Tool function"):
            run(client, Model(), "Wait")
        self.assertTrue(client.closed)


class LoopbackIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE.is_file():
            raise RuntimeError("Build the .NET test fixture first: scripts\\check.ps1")
        cls.process = subprocess.Popen(["dotnet", str(FIXTURE), "--mock-server"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
        descriptor = cls.process.stdout.readline()
        config = json.loads(descriptor)
        cls.temp = tempfile.TemporaryDirectory()
        cls.descriptor = Path(cls.temp.name) / "connection.json"
        cls.descriptor.write_text(json.dumps(config), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.process.communicate("\n", timeout=10)
        if cls.process.returncode != 0:
            raise RuntimeError("Mock bridge failed")
        cls.temp.cleanup()

    def test_real_client_against_actual_csharp_transport(self):
        with BridgeClient(self.descriptor) as client:
            self.assertEqual(client.call("ping")["schema_version"], "1")
            state = client.call("claim", {"name": "Integration agent"})
            args = {"session_id": state["session_id"], "expected_revision": state["revision"], "kind": "wait"}
            action = client.call("act", args, request_id="one-action")
            replay = client.call("act", args, request_id="one-action")
            self.assertEqual(action, replay)
            result = wait_for_action(client, action["action"]["action_id"], time.monotonic() + 5)
            self.assertEqual(result["action"]["status"], "completed")
            self.assertTrue(client.call("heartbeat")["owns_control"])

    def test_explicit_read_recovers_without_replaying_or_claiming(self):
        with BridgeClient(self.descriptor) as client:
            client.call("observe")
            client._drop()
            client.failure = BridgeError("outcome_unknown", "Simulated lost connection")
            with self.assertRaisesRegex(BridgeError, "outcome_unknown"):
                client.call("claim", {"name": "must not auto-reclaim"})
            state = client.call("observe")
            self.assertIn("session_id", state)
            self.assertIsNone(client.failure)
            self.assertFalse(client.has_control)
            client._drop()
            client.failure = BridgeError("game_closed", "Simulated acknowledged quit")
            client._stop.set()
            self.assertIn("session_id", client.call("ping"))
            self.assertFalse(client._stop.is_set())
    def test_actual_stdio_mcp_process_and_no_game_mutations(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "elin_observe", "arguments": {}}},
        ]
        process = subprocess.run([os.sys.executable, "-m", "elingpt", "--connection", str(self.descriptor), "mcp"],
                                 input="".join(json.dumps(request) + "\n" for request in requests),
                                 text=True, encoding="utf-8", capture_output=True, timeout=15,
                                 env={**os.environ, "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(len(responses[1]["result"]["tools"]), 14)
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertIn("Test\u300c\u03bb\u300d", process.stdout)

    def test_wrong_token_fails_explicitly(self):
        config = json.loads(self.descriptor.read_text())
        config["token"] = "A" * 43 + "="
        path = Path(self.temp.name) / "bad.json"
        path.write_text(json.dumps(config))
        with BridgeClient(path) as client, self.assertRaisesRegex(BridgeError, "unauthorized"):
            client.call("ping")

    def test_remote_descriptor_rejected(self):
        config = json.loads(self.descriptor.read_text())
        config["host"] = "example.invalid"
        path = Path(self.temp.name) / "remote.json"
        path.write_text(json.dumps(config))
        with BridgeClient(path) as client, self.assertRaisesRegex(BridgeError, "loopback"):
            client.call("ping")

    def test_full_runner_through_mock_model_and_actual_bridge(self):
        payloads = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payloads.append(payload)
                observation = json.loads(payload["messages"][-1]["content"].split("\n", 1)[1])
                args = {"session_id": observation["session_id"], "expected_revision": observation["revision"], "kind": "wait"}
                message = {"role": "assistant", "content": "Waiting one normal turn.",
                           "tool_calls": [{"id": "mock-call", "type": "function", "function": {
                               "name": "elin_act", "arguments": json.dumps(args)}}]}
                body = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            model = ModelClient(f"http://127.0.0.1:{server.server_port}/v1", "fake-model",
                                "ELINGPT_TEST_NO_KEY", False)
            with BridgeClient(self.descriptor) as client, patch("sys.stdout", new=io.StringIO()):
                run(client, model, "Wait once", max_steps=1, max_seconds=10)
            self.assertEqual(len(payloads), 1)
            self.assertFalse(payloads[0]["parallel_tool_calls"])
            self.assertEqual(payloads[0]["model"], "fake-model")
            with BridgeClient(self.descriptor) as observer:
                self.assertIsNone(observer.call("observe")["controller"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
