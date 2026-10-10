"""MCP and model tool definitions share the same strict input contract."""

from __future__ import annotations

from typing import Any
import math

from .client import BridgeError


def string(description: str, *, maximum: int = 256) -> dict[str, Any]:
    return {"type": "string", "description": description, "minLength": 1, "maxLength": maximum}


REVISION = {
    "session_id": string("Current observe.session_id."),
    "expected_revision": {"type": "integer", "minimum": 0,
                          "description": "Current observe.revision; stale actions are rejected."},
}
DEFINITIONS: list[tuple[str, str, dict[str, Any], list[str]]] = [
    ("ping", "Read bridge capabilities, built-in gameplay guide (axes/UI/world basics), and connector metrics.", {}, []),
    ("observe", "Read compact state, then deltas. Reuse action observations; use mode=full after losing the baseline. All valid local terrain is preserved.",
     {"mode": {"type": "string", "enum": ["auto", "full"]},
      "since": string("Optional last observation_id; mismatch returns a full baseline.")}, []),
    ("claim", "Acquire exclusive control at a game, title-menu or native death-dialog decision boundary. Death retains UI control; user input/F9/disconnect releases it.",
     {"name": string("Agent label shown to the spectator.", maximum=60)}, ["name"]),
    ("release", "Cancel mod-owned activity and release control.", {}, []),
    ("inspect", "Inspect a current visible-entity or top-level inventory handle. Hidden stats are never returned.",
     {"handle": string("Entity handle from observe.")}, ["handle"]),
    ("available_actions", "Discover short-lived normal context actions for a visible local tile.",
     {**REVISION, "x": {"type": "integer", "minimum": 0, "description": "Map x; increases east."},
      "z": {"type": "integer", "minimum": 0, "description": "Map z; increases north, not south."}},
     ["session_id", "expected_revision", "x", "z"]),
    ("act", "One normal action; waits up to 10s and returns fresh state/delta. Poll if still running; never resubmit. rotate_held changes orientation only; rediscover placement actions afterward.",
     {**REVISION, "kind": {"type": "string", "enum": ["wait", "step", "context", "rotate_held", "open_ui"]},
      "dx": {"type": "integer", "minimum": -1, "maximum": 1, "description": "+1 east, -1 west (local screen down-right/up-left)."},
      "dz": {"type": "integer", "minimum": -1, "maximum": 1, "description": "+1 north, -1 south (local screen up-right/down-left)."},
      "action_handle": string("ID from available_actions."),
      "menu": {"type": "string", "enum": ["inventory", "character", "abilities", "journal"]},
      "reason": string("Short public action rationale (not private chain-of-thought).", maximum=180)},
     ["session_id", "expected_revision", "kind"]),
    ("action_status", "Wait up to 10s for this accepted action's decision boundary; return status and observation changes. Never resubmit a running action.",
     {"action_id": string("ID returned by act or ui_act.")}, ["action_id"]),
    ("ui_observe", "Read a page of exact visible UI text/controls (default). Continue with next_offset and page_id. view=tree explicitly requests bounded debug metadata. Query subtrees if truncated.",
     {"root_id": string("Optional node ID for a subtree."),
      "depth": {"type": "integer", "minimum": 0, "maximum": 32},
      "limit": {"type": "integer", "minimum": 1, "maximum": 4096},
      "include_inactive": {"type": "boolean"},
      "view": {"type": "string", "enum": ["tree", "decisions"]},
      "offset": {"type": "integer", "minimum": 0, "maximum": 4096},
      "page_id": string("Page identity from the same UI query; required for offset>0."),
      "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 65536}}, []),
    ("ui_act", "Operate an advertised node using Unity events or an in-game key. MCP waits for queued input and returns fresh state. In decision views, node.action_set indexes ui.action_sets. Inventory uses click-to-carry/click-to-place; ui.drag_active reports a carried item. No OS input injection.",
     {**REVISION,
      "operation": {"type": "string", "enum": ["click", "right_click", "hover", "focus", "submit", "cancel",
          "set_text", "end_edit", "set_value", "scroll", "drag", "invoke_event", "key"]},
      "node_id": string("UI node ID from the tree. Required except for key."),
      "target_id": string("Visible drop target for drag."),
      "event_id": string("Advertised UnityEvent ID for invoke_event."),
      "key": {"type": "string", "enum": ["enter", "escape", "tab", "space", "up", "down", "left", "right",
          "page_up", "page_down", "home", "end"] + list("abcdefghijklmnopqrstuvwxyz0123456789")},
      "value": {"type": ["string", "number", "integer", "boolean"], "maxLength": 4096},
      "dx": {"type": "number", "minimum": -10, "maximum": 10},
      "dy": {"type": "number", "minimum": -10, "maximum": 10}},
     ["session_id", "expected_revision", "operation"]),
    ("ui_choose", "Compatibility alias for generic click on an advertised UI node.",
     {**REVISION, "choice_id": string("UI node ID from observe.state.ui.nodes or ui_observe.")},
     ["session_id", "expected_revision", "choice_id"]),
    ("ui_set_text", "Compatibility alias for set_text on a generic UI input node. Submit/end-edit separately.",
     {**REVISION, "field_id": string("Editable text_input node ID from the UI tree."),
      "value": {"type": "string", "minLength": 0, "maxLength": 256}},
     ["session_id", "expected_revision", "field_id", "value"]),
    ("session_command", "Native save, pause menu, return-to-title or quit. Return/quit in-game keep the normal save confirmation. While dead, use the native death/revival UI instead.",
     {**REVISION, "command": {"type": "string", "enum": ["save", "return_to_title", "quit", "pause_menu"]}},
     ["session_id", "expected_revision", "command"]),
    ("cancel", "Cancel only current mod-owned activity without releasing the lease.", {}, []),
]
ALL_TOOLS = [{
    "name": f"elin_{name}", "description": description,
    "inputSchema": {"type": "object", "properties": properties,
                    "required": required, "additionalProperties": False},
    "annotations": {"readOnlyHint": name in {"ping", "observe", "inspect", "available_actions",
                                           "action_status", "ui_observe"},
                    "openWorldHint": False},
} for name, description, properties, required in DEFINITIONS]
TOOLS = [tool for tool in ALL_TOOLS if tool["name"] not in {"elin_ui_choose", "elin_ui_set_text"}]
TOOL_MAP = {tool["name"]: tool for tool in ALL_TOOLS}


def bridge_arguments(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items()
            if key not in {"view", "mode", "since", "offset", "page_id", "max_bytes"}}


def validate(name: str, args: Any) -> str:
    if name not in TOOL_MAP:
        raise BridgeError("unknown_tool", f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise BridgeError("invalid_argument", "Tool arguments must be an object.")
    schema = TOOL_MAP[name]["inputSchema"]
    properties = schema["properties"]
    if set(args) - set(properties):
        raise BridgeError("invalid_argument", "Unknown argument(s): " + ", ".join(sorted(set(args) - set(properties))))
    for key in schema["required"]:
        if key not in args:
            raise BridgeError("invalid_argument", f"Missing argument: {key}")
    for key, value in args.items():
        rule = properties[key]
        if rule["type"] == "string":
            if (not isinstance(value, str) or not rule.get("minLength", 1) <= len(value) <= rule.get("maxLength", 256)
                    or "\0" in value):
                raise BridgeError("invalid_argument", f"{key} must be a bounded nonempty string.")
        elif rule["type"] == "integer":
            if type(value) is not int or not rule.get("minimum", -(2**63)) <= value <= rule.get("maximum", 2**63 - 1):
                raise BridgeError("invalid_argument", f"{key} must be an integer in range.")
        elif rule["type"] == "number":
            if type(value) not in {int, float} or not math.isfinite(value) or not rule.get("minimum", -1e6) <= value <= rule.get("maximum", 1e6):
                raise BridgeError("invalid_argument", f"{key} must be a finite number in range.")
        elif rule["type"] == "boolean":
            if type(value) is not bool:
                raise BridgeError("invalid_argument", f"{key} must be boolean.")
        elif isinstance(rule["type"], list):
            if type(value) not in {str, int, float, bool} or (
                    isinstance(value, str) and (len(value) > rule.get("maxLength", 4096) or "\0" in value)) or (
                    type(value) in {int, float} and not math.isfinite(value)):
                raise BridgeError("invalid_argument", f"{key} must be a bounded text/boolean/finite numeric value.")
        if "enum" in rule and value not in rule["enum"]:
            raise BridgeError("invalid_argument", f"{key} is not a supported value.")
    if name == "elin_act":
        required = {"step": ["dx", "dz"], "context": ["action_handle"], "open_ui": ["menu"]}.get(args["kind"], [])
        for key in required:
            if key not in args:
                raise BridgeError("invalid_argument", f"{args['kind']} requires {key}.")
        if args["kind"] == "step" and args["dx"] == args["dz"] == 0:
            raise BridgeError("invalid_argument", "Use wait instead of a zero-length step.")
    if name == "elin_ui_act":
        operation = args["operation"]
        required = ["key"] if operation == "key" else ["node_id"]
        required += {"drag": ["target_id"], "invoke_event": ["event_id"],
                     "set_text": ["value"], "set_value": ["value"], "scroll": ["dx", "dy"]}.get(operation, [])
        for key in required:
            if key not in args:
                raise BridgeError("invalid_argument", f"{operation} requires {key}.")
        if operation == "set_text" and not isinstance(args["value"], str):
            raise BridgeError("invalid_argument", "set_text requires a string value.")
    if name == "elin_ui_observe":
        if args.get("view") == "tree" and any(key in args for key in ("offset", "page_id", "max_bytes")):
            raise BridgeError("invalid_argument", "Paging and max_bytes apply only to the decisions view.")
        if args.get("offset", 0) and "page_id" not in args:
            raise BridgeError("invalid_argument", "UI continuation requires page_id.")
    return name.removeprefix("elin_")
