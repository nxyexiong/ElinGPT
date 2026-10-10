"""Explicitly opt-in live checks. Never launched by the normal test suite."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from elingpt.client import BridgeClient, BridgeError


class LiveChecks:
    def __init__(self, client: BridgeClient):
        self.client = client
        self.results: list[dict[str, Any]] = []

    @staticmethod
    def check_tree(ui: dict[str, Any]) -> None:
        assert ui["format"] == "gameobject_tree"
        assert len(ui["roots"]) == len(set(ui["roots"])), "Duplicate UI roots"
        ids = [node["id"] for node in ui["nodes"]]
        assert len(ids) == len(set(ids)), "Duplicate UI node IDs"
        seen: dict[str, dict[str, Any]] = {}
        for node in ui["nodes"]:
            parent = node["parent_id"]
            if parent is None:
                assert node["id"] in ui["roots"] and node["depth"] == 0
            else:
                assert parent in seen, "UI child appears without its parent"
                assert node["depth"] == seen[parent]["depth"] + 1
            if node["sensitive"]:
                assert "value" not in node and node["text"] is None and not node["actions"]
            if not node["visible"] or not node["interactable"]:
                assert not node["actions"], "Unavailable node advertises actions"
            seen[node["id"]] = node

    def observe(self) -> dict[str, Any]:
        state = self.client.call("observe")
        self.check_tree(state["state"]["ui"])
        return state

    def record(self, name: str, **details: Any) -> None:
        result = {"check": name, "status": "passed", **details}
        self.results.append(result)
        print(json.dumps(result), flush=True)

    def ui_act(self, operation: str, node_id: str | None = None, **args: Any) -> dict[str, Any]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.observe()
            request = {"session_id": state["session_id"], "expected_revision": state["revision"],
                       "operation": operation, **args}
            if node_id is not None:
                request["node_id"] = node_id
            try:
                response = self.client.call("ui_act", request)
            except BridgeError as error:
                if error.code not in {"stale_observation", "input_busy"}:
                    raise
                time.sleep(0.1)
                continue
            action = response.get("action")
            while action and not response.get("quitting"):
                if action["status"] == "completed":
                    break
                if action["status"] in {"interrupted", "rejected", "error"}:
                    raise BridgeError("live_action_failed", action.get("message", action["status"]))
                if time.monotonic() >= deadline:
                    raise BridgeError("live_timeout", "Accepted UI action did not complete; do not resubmit it.")
                time.sleep(0.1)
                response = self.client.call("action_status", {"action_id": action["action_id"]})
                action = response["action"]
            time.sleep(0.35)
            return response
        raise BridgeError("live_timeout", "UI state did not accept the operation within eight seconds.")

    def node(self, node_id: str) -> dict[str, Any]:
        return next(node for node in self.observe()["state"]["ui"]["nodes"] if node["id"] == node_id)

    def read_only(self) -> None:
        info = self.client.call("ping")
        assert "capture" not in info["capabilities"]
        state = self.observe()
        self.record("text_only_tree", nodes=len(state["state"]["ui"]["nodes"]),
                    truncated=state["state"]["ui"]["truncated"])
        root = state["state"]["ui"]["roots"][0]
        subtree = self.client.call("ui_observe", {"root_id": root, "depth": 2, "limit": 32})["ui"]
        self.check_tree(subtree)
        assert subtree["roots"] == [root] and subtree["nodes"][0]["id"] == root
        assert len(subtree["nodes"]) <= 32
        self.record("bounded_subtree", nodes=len(subtree["nodes"]))
        inactive = self.client.call("ui_observe", {"root_id": root, "depth": 4, "limit": 128,
                                                   "include_inactive": True})["ui"]
        self.check_tree(inactive)
        self.record("inactive_subtree", nodes=len(inactive["nodes"]))

    def settings_controls(self) -> None:
        state = self.observe()
        if state["status"] != "title":
            raise BridgeError("scope_guard", "Settings checks require the title screen, not a loaded save.")
        self.client.call("claim", {"name": "ElinGPT opt-in live checks"})
        self.client.start_heartbeat()
        state = self.observe()
        close = [node for node in state["state"]["ui"]["nodes"]
                 if node["label"] == "button close" and "click" in node["actions"]]
        if close:
            self.ui_act("click", close[0]["id"])
        state = self.observe()
        config = [node for node in state["state"]["ui"]["nodes"]
                  if node["label"] == "Config" and "click" in node["actions"]]
        if config:
            self.ui_act("click", config[0]["id"])
        state = self.observe()
        sliders = [node for node in state["state"]["ui"]["nodes"]
                   if node["role"] == "slider" and node.get("min") == 0 and node.get("max") == 1
                   and "set_value" in node["actions"] and "drag" in node["actions"]]
        if not sliders:
            raise BridgeError("fixture_missing", "Open the native settings page containing a 0..1 slider.")
        slider = sliders[0]
        original = slider["value"]
        test_value = 0.97 if abs(original - 0.97) > 0.01 else 0.93
        try:
            self.ui_act("focus", slider["id"])
            assert self.observe()["state"]["ui"]["focus_id"] == slider["id"]
            self.record("focus")
            self.ui_act("set_value", slider["id"], value=test_value)
            assert abs(self.node(slider["id"])["value"] - test_value) < 0.0001
            self.record("slider_value")
            self.ui_act("drag", slider["id"], target_id=slider["id"])
            assert abs(self.node(slider["id"])["value"] - test_value) > 0.0001
            self.record("slider_drag")
        finally:
            self.ui_act("set_value", slider["id"], value=original)
            assert abs(self.node(slider["id"])["value"] - original) < 0.0001
            self.record("slider_restored")
        state = self.observe()
        scroll = next(node for node in state["state"]["ui"]["nodes"]
                      if node["role"] == "scroll_view" and "scroll" in node["actions"])
        before = scroll["scroll_position"]["y"]
        if min(abs(before), abs(1 - before)) > 0.0001:
            raise BridgeError("fixture_missing", "Settings scroll must start at an endpoint for reversible checks.")
        direction = -3 if before > 0.5 else 3
        try:
            self.ui_act("scroll", scroll["id"], dx=0, dy=direction)
            assert abs(self.node(scroll["id"])["scroll_position"]["y"] - before) > 0.00001
            self.record("scroll")
            state = self.observe()
            dropdown = next(node for node in state["state"]["ui"]["nodes"]
                            if node["role"] == "dropdown" and "set_value" in node["actions"]
                            and len(node.get("options", [])) > 1)
            original = dropdown["value"]
            try:
                self.ui_act("set_value", dropdown["id"], value=0 if original != 0 else 1)
                assert self.node(dropdown["id"])["value"] != original
                self.record("dropdown_value")
            finally:
                self.ui_act("set_value", dropdown["id"], value=original)
                assert self.node(dropdown["id"])["value"] == original
                self.record("dropdown_restored")
        finally:
            self.ui_act("scroll", scroll["id"], dx=0, dy=-direction)
            assert abs(self.node(scroll["id"])["scroll_position"]["y"] - before) < 0.0001
            self.record("scroll_restored")
        self.ui_act("key", key="escape")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.observe()
            if any(node["label"] == "Config" and "click" in node["actions"]
                   for node in state["state"]["ui"]["nodes"]):
                break
            time.sleep(0.2)
        else:
            raise BridgeError("live_timeout", "Escape did not restore the native title menu.")
        self.record("native_settings_exit")

    def inventory_controls(self, save_id: str) -> None:
        state = self.observe()
        if state["status"] != "playing" or state["state"].get("session", {}).get("save_id") != save_id:
            raise BridgeError("scope_guard", "Load the explicitly named disposable save before inventory checks.")
        if state["state"]["ui"]["drag_active"]:
            raise BridgeError("scope_guard", "Finish the existing native carry before running inventory checks.")
        original = copy.deepcopy(state["state"]["inventory"])
        original_turn = state["state"]["turn"]
        original_position = state["state"]["player"]["position"]
        if sorted(item["position"]["x"] for item in original) != [-1, 0, 1, 2, 3, 4]:
            raise BridgeError("fixture_missing", "Expected the disposable fixture with five items and an empty sixth slot.")
        item = next(item for item in original if item["position"] == {"x": 0, "z": 0})
        if item["count"] != 1:
            raise BridgeError("fixture_missing", "The first inventory item must not be a stack.")

        def signature(items: list[dict[str, Any]]) -> list[str]:
            return sorted(json.dumps(item, sort_keys=True) for item in items)

        def slots() -> list[dict[str, Any]]:
            return [node for node in self.observe()["state"]["ui"]["nodes"]
                    if "LayerInventoryFloatMain" in node["path"] and "ButtonGridDrag" in node["components"]
                    and "click" in node["actions"]]

        self.client.call("claim", {"name": "ElinGPT opt-in inventory checks"})
        self.client.start_heartbeat()
        was_open = bool(slots())
        source = target = None
        try:
            if not was_open:
                self.ui_act("key", key="i")
            grids = slots()
            if len(grids) < 6:
                raise BridgeError("fixture_missing", "The native main inventory grid is not available.")
            source, target = grids[0]["id"], grids[5]["id"]
            self.ui_act("click", source)
            assert self.observe()["state"]["ui"]["drag_active"], "Item pickup did not start a native carry"
            self.record("native_item_pickup")
            self.ui_act("click", target)
            state = self.observe()
            assert not state["state"]["ui"]["drag_active"], "Placement left the item attached to the pointer"
            expected = copy.deepcopy(original)
            next(entry for entry in expected if entry["handle"] == item["handle"])["position"]["x"] = 5
            assert signature(state["state"]["inventory"]) == signature(expected)
            self.record("native_item_placement")
            self.ui_act("click", target)
            assert self.observe()["state"]["ui"]["drag_active"]
            self.ui_act("right_click", source)
            state = self.observe()
            assert not state["state"]["ui"]["drag_active"], "Right-click did not cancel the native carry"
            assert signature(state["state"]["inventory"]) == signature(expected)
            self.record("native_carry_cancel")
        finally:
            if self.observe()["state"]["ui"]["drag_active"]:
                self.ui_act("key", key="escape")
                assert not self.observe()["state"]["ui"]["drag_active"], "Native carry cancellation failed"
            current = self.observe()["state"]["inventory"]
            moved = next((entry for entry in current if entry["handle"] == item["handle"]), None)
            if moved is not None and moved["position"] == {"x": 5, "z": 0} and source and target:
                self.ui_act("click", target)
                self.ui_act("click", source)
            assert not self.observe()["state"]["ui"]["drag_active"]
            restored = self.observe()["state"]
            assert signature(restored["inventory"]) == signature(original), "Inventory restoration failed"
            assert restored["turn"] == original_turn and restored["player"]["position"] == original_position
            self.record("inventory_restored")
            if not was_open and slots():
                self.ui_act("key", key="i")
                assert not slots(), "Inventory did not return to its closed state"


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in live ElinGPT smoke checks (no automatic game launch)")
    parser.add_argument("--connection", type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--settings-actions", action="store_true",
                         help="Consent to temporary settings-control changes, restored in finally blocks")
    actions.add_argument("--inventory-save", metavar="SAVE_ID",
                         help="Consent to a reversible inventory transfer in this already-loaded disposable save")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        with BridgeClient(args.connection) as client:
            checks = LiveChecks(client)
            checks.read_only()
            if args.settings_actions:
                checks.settings_controls()
            elif args.inventory_save:
                checks.inventory_controls(args.inventory_save)
        return 0
    except (BridgeError, AssertionError, StopIteration, OSError) as error:
        print(f"Live check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
