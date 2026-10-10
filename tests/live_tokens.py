"""Opt-in live MCP token checks; normal tests never execute gameplay."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from elingpt.client import BridgeClient, BridgeError
from elingpt.mcp import McpServer
from elingpt.observation import _size, apply_observation, decision_view, ui_decision_view


class LiveTokenChecks:
    def __init__(self, client: BridgeClient, counter: Callable[[str], int] | None = None):
        self.client = client
        self.server = McpServer(client)
        self.server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        self.current: dict[str, Any] | None = None
        self.counter = counter
        self.sequence = 0

    def record(self, name: str, **details: Any) -> None:
        print(json.dumps({"check": name, **details}, ensure_ascii=False), flush=True)

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        response = self.server.handle({"jsonrpc": "2.0", "id": self.sequence, "method": "tools/call",
                                       "params": {"name": name, "arguments": args or {}}})
        if response is None or "error" in response:
            raise BridgeError("live_mcp_error", str(response))
        result = response["result"]
        value = json.loads(result["content"][0]["text"])
        if result["isError"]:
            raise BridgeError(value["error"]["code"], value["error"]["message"])
        observation = value.get("observation", value)
        if "state" in observation or observation.get("delta"):
            self.current = apply_observation(self.current, observation)
        return value

    def measure(self, name: str, value: dict[str, Any]) -> None:
        details = {"bytes": _size(value)}
        if self.counter is not None:
            details["reference_tokens"] = self.counter(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        self.record(name, **details)

    def verify_native(self) -> dict[str, Any]:
        for _ in range(5):
            raw = self.client.call("observe")
            if self.current is not None and raw["revision"] == self.current["revision"]:
                actual = {key: value for key, value in self.current.items()
                          if key not in {"view_version", "observation_id"}}
                if actual == decision_view(raw):
                    return raw
            self.call("elin_observe")
        raise BridgeError("live_state_mismatch", "Decoded MCP state did not match a stable native observation.")

    def read_only(self) -> None:
        baseline = self.call("elin_observe", {"mode": "full"})
        raw = self.verify_native()
        self.measure("full_observe", baseline)
        self.measure("unpacked_decision_state", decision_view(raw))
        unchanged = self.call("elin_observe")
        self.verify_native()
        self.measure("unchanged_observe", unchanged)
        if raw["status"] == "playing" and raw["ready"]:
            assert unchanged.get("delta"), "Stable observe returned a full snapshot"
            assert _size(unchanged) < _size(baseline) * 0.1, "Unchanged response exceeded 10% of the baseline"
            assert _size(baseline) < _size(decision_view(raw)) * 0.75, "Full-state packing saved less than 25%"
        recovered = self.call("elin_observe", {"since": "deliberately-missing-baseline"})
        assert "delta" not in recovered, "Missing cursor did not return a new full baseline"
        self.verify_native()
        self.record("lossless_native_state_and_baseline_recovery", status="passed",
                    terrain_cells=len(self.current["state"].get("terrain", {}).get("cells", [])),
                    inventory_items=len(self.current["state"].get("inventory", [])))

    def mutate(self, name: str, **args: Any) -> dict[str, Any]:
        assert self.current is not None
        result = self.call(name, {"session_id": self.current["session_id"],
                                 "expected_revision": self.current["revision"], **args})
        deadline = time.monotonic() + 45
        while result.get("action", {}).get("status") in {"accepted", "running"}:
            if time.monotonic() >= deadline:
                raise BridgeError("live_timeout", "Accepted action is still running; it was not resubmitted.")
            result = self.call("elin_action_status", {"action_id": result["action"]["action_id"]})
        action = result.get("action", {})
        if action.get("status") not in {"completed", "awaiting_ui"}:
            raise BridgeError("live_action_failed", str(action))
        self.verify_native()
        return result

    @staticmethod
    def inventory_open(raw: dict[str, Any]) -> bool:
        return any(node.get("visible") and "LayerInventoryFloatMain" in node.get("path", "")
                   for node in raw["state"]["ui"]["nodes"])

    def guard(self, save_id: str, *, target: dict[str, int] | None = None) -> dict[str, Any]:
        assert self.current is not None
        observation, state = self.current, self.current["state"]
        if (observation["status"] != "playing" or state.get("session", {}).get("save_id") != save_id
                or state.get("zone", {}).get("name") != "Meadow" or state.get("zone", {}).get("region")
                or observation.get("active_action") or state["ui"].get("drag_active")
                or state["ui"].get("input_busy")):
            raise BridgeError("scope_guard", "Requires the named disposable save, idle in Meadow with no carried UI item.")
        player = state["player"]
        if player["hp"] != player["max_hp"] or player["stamina"] < 5:
            raise BridgeError("scope_guard", "Live movement requires full HP and at least five stamina.")
        if any(entity.get("hostile") for entity in state["entities"]):
            raise BridgeError("scope_guard", "A visible hostile prevents the live exercise.")
        if target is not None:
            if not observation["ready"]:
                raise BridgeError("scope_guard", "World movement is not ready.")
            terrain = state["terrain"]
            tile = next((dict(zip(terrain["columns"], row)) for row in terrain["cells"]
                         if row[:2] == [target["x"], target["z"]]), None)
            if tile is None or tile["blocked"] or tile["water"] or any(
                    entity.get("kind") == "character" and entity["position"] == target for entity in state["entities"]):
                raise BridgeError("scope_guard", "The selected adjacent tile is not clear, dry, visible local terrain.")
        return state

    def ui_pages(self) -> None:
        nodes: list[dict[str, Any]] = []
        args: dict[str, Any] = {"max_bytes": 4096}
        for page_number in range(64):
            result = self.call("elin_ui_observe", args)
            self.measure(f"inventory_ui_page_{page_number + 1}", result)
            page = result["ui"]
            assert page["format"] == "decision_nodes"
            assert _size(result) <= 4096 or page.get("budget_exceeded"), "Unreported UI page overflow"
            nodes += page["nodes"]
            if page["next_offset"] is None:
                raw = self.client.call("ui_observe")
                assert raw["revision"] == result["revision"], "UI changed during the page traversal"
                full = ui_decision_view(raw, max_bytes=8 * 1024 * 1024)["ui"]
                assert nodes == full["nodes"], "Paged UI omitted, duplicated or changed controls/text"
                assert len({node["id"] for node in nodes}) == len(nodes)
                self.record("inventory_ui_paging", status="passed", pages=page_number + 1,
                            nodes=len(nodes), native_truncated=full.get("truncated"))
                return
            args.update(offset=page["next_offset"], page_id=page["page_id"])
        raise BridgeError("live_page_limit", "UI did not finish within 64 bounded pages.")

    def exercise(self, save_id: str, dx: int, dz: int) -> None:
        self.guard(save_id)
        was_open = self.inventory_open(self.verify_native())
        self.call("elin_claim", {"name": "ElinGPT token verification"})
        self.call("elin_observe", {"mode": "full"})
        state = self.guard(save_id)
        original = copy.deepcopy(state)
        position = state["player"]["position"]
        target = {"x": position["x"] + dx, "z": position["z"] + dz}
        self.guard(save_id, target=target)
        outbound = self.mutate("elin_act", kind="step", dx=dx, dz=dz)
        self.measure("step_out", outbound)
        assert self.current["state"]["player"]["position"] == target, "Outward step did not arrive"
        self.guard(save_id, target=position)
        returned = self.mutate("elin_act", kind="step", dx=-dx, dz=-dz)
        self.measure("step_back", returned)
        assert self.current["state"]["player"]["position"] == position, "Return step did not restore position"
        self.guard(save_id)
        if was_open:
            closed = self.mutate("elin_ui_act", operation="key", key="i")
            self.measure("inventory_close", closed)
            assert not self.inventory_open(self.verify_native()), "Native inventory did not close"
        opened = self.mutate("elin_act", kind="open_ui", menu="inventory")
        self.measure("inventory_open", opened)
        assert self.inventory_open(self.verify_native()), "Native inventory did not open"
        self.ui_pages()
        if not was_open:
            closed = self.mutate("elin_ui_act", operation="key", key="i")
            self.measure("inventory_close", closed)
        assert self.inventory_open(self.verify_native()) == was_open, "Inventory window state was not restored"
        final = self.guard(save_id)
        assert final["player"]["position"] == original["player"]["position"]
        assert final["player"]["hp"] == original["player"]["hp"]
        assert final["inventory"] == original["inventory"], "Inventory changed during the bounded exercise"
        self.record("bounded_gameplay", status="passed", save_id=save_id, position=position,
                    hp=final["player"]["hp"], turns=final["turn"] - original["turn"],
                    inventory_unchanged=True, inventory_window_restored=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only live MCP checks, with optional bounded Meadow gameplay")
    parser.add_argument("--connection", type=Path)
    parser.add_argument("--exercise-save", metavar="SAVE_ID",
                        help="Consent to two adjacent steps and opening/closing inventory in this loaded disposable save")
    parser.add_argument("--dx", type=int, choices=(-1, 0, 1), default=-1)
    parser.add_argument("--dz", type=int, choices=(-1, 0, 1), default=0)
    parser.add_argument("--reference-tokenizer", help="Optional installed tiktoken encoding, e.g. o200k_base")
    args = parser.parse_args()
    if args.dx == args.dz == 0:
        parser.error("Select a nonzero adjacent step.")
    counter = None
    if args.reference_tokenizer:
        try:
            import tiktoken
        except ImportError:
            parser.error("Reference token counting requires the optional tiktoken package.")
        encoding = tiktoken.get_encoding(args.reference_tokenizer)
        counter = lambda text: len(encoding.encode(text, disallowed_special=()))
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        with BridgeClient(args.connection) as client:
            checks = LiveTokenChecks(client, counter)
            checks.read_only()
            if args.exercise_save:
                checks.exercise(args.exercise_save, args.dx, args.dz)
        return 0
    except (BridgeError, AssertionError, OSError) as error:
        print(f"Live token check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
