"""Small decision views; full game and UI data remain available over the bridge."""

from __future__ import annotations

import json
import copy
from typing import Any

DECISION_BYTES = 16000
MIN_UI_BYTES = 4000


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _ui_priority(node: dict[str, Any]) -> int:
    path = node.get("path", "")
    if path.startswith(("UI\\Layers\\", "UI\\ToolTips\\")):
        return 0
    if path.startswith("UI\\Layers(Float)\\"):
        return 1
    return 2


def ui_decision_view(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value.get("ui"), dict):
        return value
    summary = decision_view({"state": {"ui": value["ui"]}})["state"]["ui"]
    return {**value, "ui": summary}


def decision_view(value: dict[str, Any]) -> dict[str, Any]:
    if "observation" in value:
        return {**value, "observation": decision_view(value["observation"])}
    if not isinstance(value.get("state"), dict):
        return value
    state = dict(value["state"])
    ui = state.get("ui")
    if isinstance(ui, dict) and isinstance(ui.get("nodes"), list):
        nodes = []
        priorities = {}
        action_sets: list[list[str]] = []
        for node in ui["nodes"]:
            if not node.get("visible", True) or node.get("sensitive", False):
                continue
            actions = node.get("actions", [])
            if not node.get("text") and not any(action != "invoke_event" for action in actions):
                continue
            item = {key: node[key] for key in
                    ("id", "parent_id", "name", "role", "label", "text", "actions",
                     "value", "min", "max", "whole_numbers", "read_only", "options", "scroll_position")
                    if key in node}
            if item.get("text") == item.get("label"):
                item.pop("text", None)
            if item.get("name") == item.get("label"):
                item.pop("name", None)
            if not item.get("text"):
                item.pop("text", None)
            if not actions:
                item.pop("actions", None)
            else:
                if actions not in action_sets:
                    action_sets.append(actions)
                item.pop("actions", None)
                item["action_set"] = action_sets.index(actions)
            nodes.append(item)
            priorities[item["id"]] = (
                _ui_priority(node),
                not bool(node.get("text")),
                not any(action not in {"invoke_event", "drag", "scroll"} for action in actions),
            )
        state["ui"] = {key: ui[key] for key in
                       ("active", "roots", "truncated", "focus_id", "input_busy", "drag_active")
                       if key in ui}
        state["ui"].update(
            format="decision_nodes",
            nodes=nodes,
            action_sets=action_sets,
            tree_node_count=len(ui["nodes"]),
            detail="node.action_set indexes action_sets. Parent IDs refer to the full tree. "
                   "If summary_omitted>0 or truncated, query ui_observe(root_id,depth,limit) before assuming a control is absent.",
        )
    tiles = state.pop("tiles", None)
    if tiles is not None:
        columns = ["x", "z", "floor", "block", "blocked", "water"]
        columns += sorted({key for tile in tiles for key in tile} - set(columns))
        state["terrain"] = {"columns": columns, "cells": [[tile.get(key) for key in columns] for tile in tiles]}
    result = {**value, "state": state}
    summary = state.get("ui")
    if isinstance(summary, dict) and summary.get("format") == "decision_nodes":
        summary["summary_omitted"] = 0
        if _size(result) > DECISION_BYTES:
            nodes = summary["nodes"]
            summary["nodes"] = []
            summary["summary_omitted"] = len(nodes)
            # Lossless terrain/inventory must not starve the current UI decision.
            budget = max(MIN_UI_BYTES, DECISION_BYTES - _size(result) - 128)
            ranked = sorted(enumerate(nodes), key=lambda pair: (
                *priorities[pair[1]["id"]],
                pair[0],
            ))
            chosen = []
            for index, node in ranked:
                cost = _size(node) + 1
                if cost <= budget:
                    chosen.append((index, node))
                    budget -= cost
            summary["nodes"] = [node for _, node in sorted(chosen)]
            summary["summary_omitted"] = len(nodes) - len(chosen)
    return result


class ObservationStream:
    """Explicit per-MCP-session deltas; observe always returns a fresh full baseline."""

    def __init__(self) -> None:
        self.previous: dict[str, Any] | None = None

    def present(self, value: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        if isinstance(value.get("observation"), dict):
            return {**value, "observation": self.present(value["observation"], full=full)}
        if not isinstance(value.get("state"), dict):
            return value
        current = decision_view(value)
        before = self.previous
        self.previous = copy.deepcopy(current)
        if (full or before is None or before.get("session_id") != current.get("session_id")
                or before.get("generation") != current.get("generation")):
            return current
        old_state, state = before["state"], current["state"]
        result = {key: value for key, value in current.items() if key != "state"}
        result.update(delta=True, base_revision=before["revision"],
                      state_delta={key: value for key, value in state.items()
                                   if key != "ui" and (key not in old_state or old_state[key] != value)},
                      state_removed=[key for key in old_state if key not in state])
        terrain, old_terrain = state.get("terrain"), old_state.get("terrain")
        if (isinstance(terrain, dict) and isinstance(old_terrain, dict) and terrain != old_terrain
                and terrain.get("columns") == old_terrain.get("columns")):
            old_cells = {tuple(cell[:2]): cell for cell in old_terrain["cells"]}
            cells = {tuple(cell[:2]): cell for cell in terrain["cells"]}
            terrain_delta = {
                "columns": terrain["columns"],
                "upsert": [cell for key, cell in cells.items() if old_cells.get(key) != cell],
                "removed": [list(key) for key in old_cells if key not in cells],
            }
            reconstructed_order = [key for key in old_cells if key in cells]
            reconstructed_order += [key for key in cells if key not in old_cells]
            if reconstructed_order != list(cells):
                terrain_delta["order"] = [list(key) for key in cells]
            if _size(terrain_delta) < _size(terrain):
                result["terrain_delta"] = terrain_delta
                result["state_delta"].pop("terrain", None)
        ui, old_ui = state.get("ui"), old_state.get("ui")
        if isinstance(ui, dict) and isinstance(old_ui, dict) and "nodes" in ui and "nodes" in old_ui:
            old_nodes = {node["id"]: node for node in old_ui["nodes"]}
            ids = [node["id"] for node in ui["nodes"]]
            result["ui_delta"] = {
                "meta": {key: value for key, value in ui.items() if key != "nodes"},
                "upsert": [node for node in ui["nodes"] if old_nodes.get(node["id"]) != node],
                "removed": [key for key in old_nodes if key not in ids],
            }
            if ids != [node["id"] for node in old_ui["nodes"]]:
                result["ui_delta"]["order"] = ids
        elif ui != old_ui:
            result["state_delta"]["ui"] = ui
        return result if _size(result) < _size(current) else current
