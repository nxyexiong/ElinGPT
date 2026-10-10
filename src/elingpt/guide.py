"""Shared game basics, also embedded in the native bridge assembly."""

import json
from importlib.resources import files
from typing import Any


def gameplay_guide() -> dict[str, Any]:
    return json.loads(files("elingpt").joinpath("gameplay_guide.json").read_text(encoding="utf-8"))


def gameplay_instructions() -> str:
    guide = gameplay_guide()
    axes = guide["coordinates"]
    directions = ", ".join(f"{name}={axes[name]}" for name in ("north", "south", "east", "west"))
    return "\n".join([
        f"Elin gameplay basics v{guide['version']}. Map (dx,dz): {directions}.",
        axes["local_projection"], axes["region_projection"], axes["scope"],
        *guide["basics"],
    ])
