from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evidence import utc_now


SCHEMA_VERSION = 1


def empty_workspace() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "pins": [], "history": []}


def load_workspace(path: str | Path) -> tuple[dict[str, Any], str | None]:
    source = Path(path)
    if not source.exists():
        return empty_workspace(), None
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
        if value.get("schema_version") != SCHEMA_VERSION or not isinstance(value.get("pins"), list) or not isinstance(value.get("history"), list):
            raise ValueError("unsupported workspace schema")
        return value, None
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return empty_workspace(), f"Workspace recovery reset invalid state: {type(exc).__name__}"


def save_workspace(path: str | Path, workspace: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(workspace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def record_screen(workspace: dict[str, Any], result: dict[str, Any], *, pin_organisations: list[str] | None = None) -> dict[str, Any]:
    updated = json.loads(json.dumps(workspace))
    updated.setdefault("history", []).append({
        "created_at": utc_now(),
        "query": result.get("query"),
        "plan": result.get("plan"),
        "result_count": result.get("result_count"),
        "result_organisation_numbers": [item.get("organisation_number") for item in result.get("results", [])],
    })
    updated["history"] = updated["history"][-100:]
    pins = set(updated.setdefault("pins", []))
    pins.update(pin_organisations or [])
    updated["pins"] = sorted(pins)
    return updated
