"""Local read-only MCP wrapper for an authorized Terminal-Bench task-card export."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("task-reference-catalog")


def _catalog() -> dict[str, Any]:
    path = os.environ.get("TASKFORGE_REFERENCE_CATALOG")
    if not path:
        raise RuntimeError("set TASKFORGE_REFERENCE_CATALOG to an authorized task-card JSON export")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("dataset_version") != "terminal-bench/terminal-bench@4.0.0":
        raise ValueError("reference catalog dataset_version must be terminal-bench/terminal-bench@4.0.0")
    if not isinstance(data.get("tasks"), list):
        raise ValueError("reference catalog needs a tasks array")
    seen_ids: set[str] = set()
    for index, task in enumerate(data["tasks"]):
        if not isinstance(task, dict):
            raise ValueError(f"reference catalog task {index} must be an object")
        for field in ("task_id", "category", "subcategory", "description"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise ValueError(f"reference catalog task {index} needs a non-empty string {field!r}")
        if task["task_id"] in seen_ids:
            raise ValueError(f"reference catalog has duplicate task_id {task['task_id']!r}")
        seen_ids.add(task["task_id"])
    return data


def _eligible_tasks(dataset_version: str, category: str, exclude_subcategory: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = _catalog()
    if dataset_version != data["dataset_version"]:
        raise ValueError(f"catalog only contains {data['dataset_version']}")
    matches = [
        task
        for task in data["tasks"]
        if task.get("category") == category and task.get("subcategory") != exclude_subcategory
    ]
    return data, matches


@mcp.tool()
def list_reference_tasks(dataset_version: str, category: str, exclude_subcategory: str) -> dict[str, Any]:
    """List compact task titles and IDs in a category, excluding the sampled subcategory. Use get_reference_task for full descriptions."""
    data, matches = _eligible_tasks(dataset_version, category, exclude_subcategory)
    return {
        "source": {
            "dataset_version": data["dataset_version"],
            "source_uri": data.get("source_uri", "local authorized export"),
            "access_policy": data.get("access_policy", "user-supplied authorized export"),
            "matched_task_count": len(matches),
        },
        "tasks": [
            {
                "subcategory": task["subcategory"],
                "title": task.get("title") or task["task_id"].replace("-", " ").title(),
            }
            for task in matches
        ],
    }


@mcp.tool()
def get_reference_task(title: str, subcategory: str, dataset_version: str, category: str, exclude_subcategory: str) -> dict[str, Any]:
    """Fetch one full visible description by its listed title and subcategory, within the host-enforced allowed reference pool."""
    data, matches = _eligible_tasks(dataset_version, category, exclude_subcategory)
    titled_matches = [
        item for item in matches
        if item["subcategory"] == subcategory
        and (item.get("title") or item["task_id"].replace("-", " ").title()) == title
    ]
    if not titled_matches:
        raise ValueError("title and subcategory are not in the allowed reference set")
    if len(titled_matches) > 1:
        raise ValueError("that title is ambiguous within the subcategory; choose a more distinctive listed title")
    task = titled_matches[0]
    return {
        "task_id": task["task_id"],
        "category": task["category"],
        "subcategory": task["subcategory"],
        "description": task["description"],
    }


@mcp.tool()
def list_novelty_reference_tasks(dataset_version: str, category: str, subcategory: str) -> dict[str, Any]:
    """Return visible task descriptions for novelty review in exactly one category and subcategory."""
    data = _catalog()
    if dataset_version != data["dataset_version"]:
        raise ValueError(f"catalog only contains {data['dataset_version']}")
    tasks = [
        {"task_id": task["task_id"], "category": task["category"], "subcategory": task["subcategory"], "description": task["description"]}
        for task in data["tasks"]
        if task.get("category") == category and task.get("subcategory") == subcategory
    ]
    return {"source": {"dataset_version": data["dataset_version"], "source_uri": data.get("source_uri", "local catalog")}, "tasks": tasks}


if __name__ == "__main__":
    mcp.run()
