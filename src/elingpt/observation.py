"""Lossless wire encodings, bounded UI pages and recoverable observation deltas."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from .client import BridgeError

VIEW_VERSION = 2
VIEW_INSTRUCTIONS = """View v2: first observe returns a compact full baseline; later observations/action results
are deltas. Reuse action results; observe(mode="full") after losing context. Optional since=observation_id
requests recovery if the connector's baseline differs. Apply deltas only to matching base_id/base_revision:
replace changed top-level fields, state_delta fields, and remove state_removed/meta_removed keys.
terrain_delta upserts rows by (x,z); removed coordinates leave the window, not the world.
collections_delta upserts/removes inventory/entities by handle, ground_objects by (x,z), messages by index.
ui_delta updates only changed meta fields, removes meta_removed fields, and upserts/removes nodes by id;
retain node order unless order is supplied. Canonical terrain/collections are sorted by their keys.
Full terrain palette_grid: rows[z-offset][x-offset] indexes palette; null means outside the valid map.
Palette values follow columns excluding x,z and defaults; origin+offset gives coordinates.
Table records merge defaults with columns zipped to each row; missing [row,column] pairs mean absent keys.
All valid local tiles and wall variants remain represented; entity/action visibility restrictions remain.
UI node.action_set indexes action_sets. UI is paged: query ui_observe(offset=next_offset,page_id=page_id);
stale_ui_page requires offset=0. truncated means query a smaller subtree. view="tree" is explicit debug
metadata, not the default. Never infer that omitted controls or off-window tiles do not exist."""
DECISION_BYTES = 24000
UI_BYTES = 4000
COLLECTION_KEYS = {
    "inventory": ("handle",), "entities": ("handle",),
    "ground_objects": ("x", "z"), "messages": ("index",),
}
WIRE_KEYS = {"delta", "base_id", "base_revision", "state_delta", "state_removed",
             "terrain_delta", "collections_delta", "ui_delta", "meta_removed", "budget"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(value: Any) -> int:
    return len(_json(value).encode("utf-8"))


def _same(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(item, right[key]) for key, item in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _key(item: dict[str, Any], fields: tuple[str, ...]) -> Any:
    return item[fields[0]] if len(fields) == 1 else tuple(item[field] for field in fields)


def _indexed(items: Any, fields: tuple[str, ...]) -> dict[Any, dict[str, Any]] | None:
    if not isinstance(items, list) or any(
            not isinstance(item, dict) or any(field not in item for field in fields) for item in items):
        return None
    result = {_key(item, fields): item for item in items}
    return result if len(result) == len(items) else None


def pack_records(items: list[dict[str, Any]]) -> Any:
    if not items:
        return []
    columns = list(dict.fromkeys(key for item in items for key in item))
    defaults = {key: items[0][key] for key in columns if key in items[0]
                and all(key in item and _same(item[key], items[0][key]) for item in items)}
    columns = [key for key in columns if key not in defaults]
    table = {"format": "table", "defaults": defaults, "columns": columns,
             "rows": [[item.get(key) for key in columns] for item in items]}
    missing = [[i, j] for i, item in enumerate(items) for j, key in enumerate(columns) if key not in item]
    if missing:
        table["missing"] = missing
    return table if _size(table) < _size(items) else items


def unpack_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return copy.deepcopy(value)
    if not isinstance(value, dict) or value.get("format") != "table":
        raise BridgeError("invalid_observation", "Expected records or a table.")
    items = [{**value["defaults"], **dict(zip(value["columns"], row))} for row in value["rows"]]
    for i, j in value.get("missing", []):
        items[i].pop(value["columns"][j])
    return items


def pack_terrain(terrain: dict[str, Any]) -> dict[str, Any]:
    columns, cells = terrain["columns"], terrain["cells"]
    if not cells:
        return terrain
    positions = {(cell[0], cell[1]) for cell in cells}
    if len(positions) != len(cells):
        raise BridgeError("invalid_observation", "Duplicate terrain coordinates.")
    x0, z0 = min(x for x, z in positions), min(z for x, z in positions)
    width = max(x for x, z in positions) - x0 + 1
    height = max(z for x, z in positions) - z0 + 1
    if width * height > 4096 or width * height > len(cells) * 2:
        return terrain
    defaults = {key: cells[0][i] for i, key in enumerate(columns) if i >= 2
                and all(_same(row[i], cells[0][i]) for row in cells)}
    variable = [i for i, key in enumerate(columns) if i >= 2 and key not in defaults]
    palette: list[list[Any]] = []
    codes: dict[str, int] = {}
    rows: list[list[int | None]] = [[None] * width for _ in range(height)]
    for cell in cells:
        values = [cell[i] for i in variable]
        key = _json(values)
        if key not in codes:
            codes[key] = len(palette)
            palette.append(values)
        rows[cell[1] - z0][cell[0] - x0] = codes[key]
    packed = {"format": "palette_grid", "origin": [x0, z0], "size": [width, height],
              "columns": columns, "defaults": defaults, "palette": palette, "rows": rows}
    return packed if _size(packed) < _size(terrain) else terrain


def unpack_terrain(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("format") != "palette_grid":
        return copy.deepcopy(value)
    columns, defaults = value["columns"], value["defaults"]
    variable = [key for key in columns[2:] if key not in defaults]
    x0, z0 = value["origin"]
    width, height = value["size"]
    cells = []
    for x in range(width):
        for z in range(height):
            code = value["rows"][z][x]
            if code is None:
                continue
            item = {**defaults, **dict(zip(variable, value["palette"][code])), "x": x0 + x, "z": z0 + z}
            cells.append([item[key] for key in columns])
    return {"columns": columns, "cells": cells}


def _ui_priority(node: dict[str, Any]) -> int:
    path = node.get("path", "")
    if path.startswith(("UI\\Layers\\", "UI\\ToolTips\\", "UI\\ContextMenu\\")):
        return 0
    return 1 if path.startswith("UI\\Layers(Float)\\") else 2


def ui_decision_view(value: dict[str, Any], *, offset: int = 0, page_id: str | None = None,
                     max_bytes: int = 8000) -> dict[str, Any]:
    ui = value.get("ui")
    if not isinstance(ui, dict):
        return value
    if ui.get("format") == "decision_nodes":
        return value
    nodes = []
    action_sets: list[list[str]] = []
    ranked = sorted(enumerate(ui.get("nodes", [])), key=lambda pair: (_ui_priority(pair[1]), pair[0]))
    for _, node in ranked:
        if not node.get("visible", True) or node.get("sensitive", False):
            continue
        actions = node.get("actions", [])
        if not node.get("text") and not any(action != "invoke_event" for action in actions):
            continue
        item = {key: node[key] for key in
                ("id", "parent_id", "name", "role", "label", "text", "value", "min", "max",
                 "whole_numbers", "read_only", "options", "scroll_position") if key in node}
        if item.get("name") == item.get("label"):
            item.pop("name", None)
        if not item.get("text") or item.get("text") == item.get("label"):
            item.pop("text", None)
        if actions:
            if actions not in action_sets:
                action_sets.append(actions)
            item["action_set"] = action_sets.index(actions)
        nodes.append(item)
    meta = {key: ui[key] for key in
            ("active", "roots", "truncated", "focus_id", "input_busy", "drag_active", "pauses_game", "processing")
            if key in ui}
    meta.update(format="decision_nodes", action_sets=action_sets, tree_node_count=len(ui.get("nodes", [])))
    identity = hashlib.sha256(_json([value.get("revision"), meta, nodes]).encode("utf-8")).hexdigest()[:16]
    if (page_id is not None and page_id != identity) or (offset and page_id is None):
        raise BridgeError("stale_ui_page", "UI page changed or has no baseline; query offset=0 again.")
    if offset > len(nodes):
        raise BridgeError("invalid_argument", "UI offset exceeds the number of decision nodes.")
    summary = {**meta, "page_id": identity, "offset": offset, "total_nodes": len(nodes), "nodes": []}
    result = {**value, "ui": summary}
    remaining = max_bytes - _size(result) - 128
    for node in nodes[offset:]:
        cost = _size(node) + 1
        if summary["nodes"] and cost > remaining:
            break
        # An indivisible long dialogue must remain readable, not silently disappear.
        summary["nodes"].append(node)
        remaining -= cost
    end = offset + len(summary["nodes"])
    summary["next_offset"] = end if end < len(nodes) else None
    summary["summary_omitted"] = len(nodes) - len(summary["nodes"])
    if _size(result) > max_bytes:
        summary["budget_exceeded"] = True
    return result


def decision_view(value: dict[str, Any]) -> dict[str, Any]:
    """Canonical decision state; transport packing is deliberately separate."""
    if isinstance(value.get("observation"), dict):
        return {**value, "observation": decision_view(value["observation"])}
    if not isinstance(value.get("state"), dict):
        return value
    state = copy.deepcopy(value["state"])
    if isinstance(state.get("ui"), dict):
        state["ui"] = ui_decision_view(
            {"revision": value.get("revision"), "ui": state["ui"]}, max_bytes=UI_BYTES)["ui"]
    tiles = state.pop("tiles", None)
    if tiles is not None:
        columns = ["x", "z", "floor", "block", "blocked", "water"]
        columns += sorted({key for tile in tiles for key in tile} - set(columns))
        state["terrain"] = {"columns": columns,
                            "cells": [[tile.get(key) for key in columns]
                                      for tile in sorted(tiles, key=lambda tile: (tile["x"], tile["z"]))]}
    for name, fields in COLLECTION_KEYS.items():
        indexed = _indexed(state.get(name), fields)
        if indexed is not None:
            state[name] = [indexed[key] for key in sorted(indexed)]
    return {**value, "state": state}


def compact_observation(value: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value.get("observation"), dict):
        return {**value, "observation": compact_observation(value["observation"])}
    if not isinstance(value.get("state"), dict):
        return value
    result = copy.deepcopy(value)
    state = result["state"]
    if "terrain" in state:
        state["terrain"] = pack_terrain(state["terrain"])
    for name in COLLECTION_KEYS:
        if isinstance(state.get(name), list):
            state[name] = pack_records(state[name])
    result["view_version"] = VIEW_VERSION
    if _size(result) > DECISION_BYTES:
        result["budget"] = {"target_bytes": DECISION_BYTES, "exceeded": True,
                            "reason": "Lossless state or indivisible UI text; no terrain was dropped."}
    return result


def expand_observation(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result.pop("budget", None)
    state = result.get("state", {})
    if "terrain" in state:
        state["terrain"] = unpack_terrain(state["terrain"])
    for name in COLLECTION_KEYS:
        if name in state:
            state[name] = unpack_records(state[name])
    return result


def apply_observation(previous: dict[str, Any] | None, value: dict[str, Any]) -> dict[str, Any]:
    """Reference decoder. A missing/mismatched baseline is an error, never a guess."""
    if not value.get("delta"):
        return expand_observation(value)
    if (previous is None or previous.get("observation_id") != value.get("base_id")
            or previous.get("revision") != value.get("base_revision")):
        raise BridgeError("missing_baseline", "Request observe(mode='full') before applying this delta.")
    result = copy.deepcopy(previous)
    result.update({key: item for key, item in value.items() if key not in WIRE_KEYS})
    for key in value.get("meta_removed", []):
        result.pop(key, None)
    state = result["state"]
    replacements = expand_observation({"state": value.get("state_delta", {})})["state"]
    state.update(replacements)
    for key in value.get("state_removed", []):
        state.pop(key, None)
    if "terrain_delta" in value:
        patch = value["terrain_delta"]
        terrain = state["terrain"]
        if terrain["columns"] != patch["columns"]:
            raise BridgeError("missing_baseline", "Terrain schema differs from the delta baseline.")
        cells = {tuple(row[:2]): row for row in terrain["cells"]}
        for key in patch.get("removed", []):
            cells.pop(tuple(key))
        for row in patch.get("upsert", []):
            cells[tuple(row[:2])] = row
        terrain["cells"] = [cells[key] for key in sorted(cells)]
    for name, patch in value.get("collections_delta", {}).items():
        fields = COLLECTION_KEYS[name]
        items = _indexed(state[name], fields)
        if items is None:
            raise BridgeError("missing_baseline", "Collection has no stable identities.")
        for key in patch.get("removed", []):
            items.pop(tuple(key) if isinstance(key, list) else key)
        for item in unpack_records(patch.get("upsert", [])):
            items[_key(item, fields)] = item
        state[name] = [items[key] for key in sorted(items)]
    if "ui_delta" in value:
        patch, ui = value["ui_delta"], state["ui"]
        ui.update(patch.get("meta", {}))
        for key in patch.get("meta_removed", []):
            ui.pop(key, None)
        nodes = {node["id"]: node for node in ui["nodes"]}
        for key in patch.get("removed", []):
            nodes.pop(key)
        for node in patch.get("upsert", []):
            nodes[node["id"]] = node
        ui["nodes"] = [nodes[key] for key in patch.get("order", list(nodes))]
    return result


class ObservationStream:
    def __init__(self) -> None:
        self.previous: dict[str, Any] | None = None
        self.identifier = uuid.uuid4().hex[:8]
        self.sequence = 0

    def present(self, value: dict[str, Any], *, full: bool = False,
                since: str | None = None) -> dict[str, Any]:
        if isinstance(value.get("observation"), dict):
            return {**value, "observation": self.present(value["observation"], full=full, since=since)}
        if not isinstance(value.get("state"), dict):
            return value
        current = decision_view(value)
        self.sequence += 1
        current.update(view_version=VIEW_VERSION, observation_id=f"{self.identifier}:{self.sequence}")
        before = self.previous
        self.previous = copy.deepcopy(current)
        packed = compact_observation(current)
        if (full or before is None or (since is not None and since != before["observation_id"])
                or before.get("session_id") != current.get("session_id")
                or before.get("generation") != current.get("generation")):
            return packed
        old_state, state = before["state"], current["state"]
        result = {key: item for key, item in current.items()
                  if key != "state" and (key == "revision" or key not in before or not _same(before[key], item))}
        result.update(delta=True, base_id=before["observation_id"], base_revision=before["revision"])
        removed = [key for key in before if key not in current and key != "state"]
        if removed:
            result["meta_removed"] = removed
        replacements = {key: item for key, item in state.items()
                        if key not in old_state or not _same(old_state[key], item)}
        removed = [key for key in old_state if key not in state]
        if removed:
            result["state_removed"] = removed
        terrain, old_terrain = state.get("terrain"), old_state.get("terrain")
        if ("terrain" in replacements and isinstance(terrain, dict) and isinstance(old_terrain, dict)
                and terrain["columns"] == old_terrain["columns"]):
            old_cells = {tuple(row[:2]): row for row in old_terrain["cells"]}
            cells = {tuple(row[:2]): row for row in terrain["cells"]}
            patch = {"columns": terrain["columns"],
                     "upsert": [row for key, row in cells.items() if not _same(old_cells.get(key), row)],
                     "removed": [list(key) for key in old_cells if key not in cells]}
            if _size(patch) < _size(pack_terrain(terrain)):
                result["terrain_delta"] = patch
                replacements.pop("terrain")
        for name, fields in COLLECTION_KEYS.items():
            if name not in replacements:
                continue
            old_items, items = _indexed(old_state.get(name), fields), _indexed(state[name], fields)
            if old_items is None or items is None:
                continue
            patch = {"upsert": pack_records([item for key, item in items.items() if not _same(old_items.get(key), item)]),
                     "removed": [key for key in old_items if key not in items]}
            if _size(patch) < _size(pack_records(state[name])):
                result.setdefault("collections_delta", {})[name] = patch
                replacements.pop(name)
        ui, old_ui = state.get("ui"), old_state.get("ui")
        if "ui" in replacements and isinstance(ui, dict) and isinstance(old_ui, dict):
            old_nodes = {node["id"]: node for node in old_ui["nodes"]}
            ids = [node["id"] for node in ui["nodes"]]
            patch = {}
            meta = {key: item for key, item in ui.items()
                    if key != "nodes" and (key not in old_ui or not _same(old_ui[key], item))}
            if meta:
                patch["meta"] = meta
            removed = [key for key in old_ui if key not in ui and key != "nodes"]
            if removed:
                patch["meta_removed"] = removed
            upsert = [node for node in ui["nodes"] if not _same(old_nodes.get(node["id"]), node)]
            if upsert:
                patch["upsert"] = upsert
            removed = [key for key in old_nodes if key not in ids]
            if removed:
                patch["removed"] = removed
            reconstructed = [key for key in old_nodes if key in ids]
            reconstructed += [node["id"] for node in upsert if node["id"] not in old_nodes]
            if reconstructed != ids:
                patch["order"] = ids
            if _size(patch) < _size(ui):
                result["ui_delta"] = patch
                replacements.pop("ui")
        if replacements:
            result["state_delta"] = compact_observation({"state": replacements})["state"]
        return result if _size(result) < _size(packed) else packed
