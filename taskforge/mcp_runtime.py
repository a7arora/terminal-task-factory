"""Small, read-only MCP tool runtime used by Stage B."""
from __future__ import annotations

import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any


READ_ONLY_GITHUB_TOOL_ALLOWLIST = {
    "search_repositories",
    "get_file_contents",
    "get_commit",
}


@dataclass(frozen=True)
class ToolBinding:
    alias: str
    server_key: str
    server_tool_name: str
    description: str
    input_schema: dict[str, Any]


def _field(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _expand_env(value: str) -> str:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in os.environ:
            raise ValueError(f"MCP config references missing environment variable {key}")
        return os.environ[key]

    return pattern.sub(replace, value)


def load_mcp_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    for required in ("reference", "github"):
        if required not in config:
            raise ValueError(f"MCP config needs a {required!r} server entry")
        item = config[required]
        if not isinstance(item.get("command"), str) or not item["command"]:
            raise ValueError(f"MCP server {required!r} needs a command")
        if not isinstance(item.get("args", []), list):
            raise ValueError(f"MCP server {required!r} args must be a list")
    allowed = config["github"].get("allowed_tools")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("github.allowed_tools must explicitly list read-only tool names")
    return config


class MCPRuntime:
    """Owns reference/GitHub stdio MCP sessions and exposes a safe tool subset."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sessions: dict[str, Any] = {}
        self.bindings: dict[str, ToolBinding] = {}
        self.server_tools: dict[str, dict[str, Any]] = {}
        self._stack: AsyncExitStack | None = None
        self.reference_scope: tuple[str, str, str] | None = None

    async def __aenter__(self) -> "MCPRuntime":
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError(
                "Stage B MCP support is optional; install with "
                "`pip install -e '.[stage-b]'`"
            ) from exc

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for server_key in ("reference", "github"):
            server_config = self.config[server_key]
            env = {
                key: _expand_env(value)
                for key, value in server_config.get("env", {}).items()
            }
            params = StdioServerParameters(
                command=server_config["command"],
                args=server_config.get("args", []),
                env=env or None,
            )
            read_stream, write_stream = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            self.sessions[server_key] = session
            listed = await session.list_tools()
            tools = list(_field(listed, "tools", default=[]))
            self.server_tools[server_key] = {str(_field(tool, "name")): tool for tool in tools}

        self._bind_github_tools()
        self._bind_reference_tools()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, traceback)
            self._stack = None

    def _bind_github_tools(self) -> None:
        configured = self.config["github"].get("allowed_tools", [])
        unsupported = sorted(set(configured) - READ_ONLY_GITHUB_TOOL_ALLOWLIST)
        if unsupported:
            raise ValueError(f"GitHub tools are outside Stage B's read-only allowlist: {unsupported}")
        available = self.server_tools["github"]
        missing = sorted(set(configured) - set(available))
        if missing:
            raise ValueError(f"configured GitHub MCP tools are not available: {missing}")
        for index, name in enumerate(configured, start=1):
            tool = available[name]
            annotations = _field(tool, "annotations", default=None)
            read_only_hint = _field(annotations, "readOnlyHint", "read_only_hint", default=None)
            if read_only_hint is False:
                raise ValueError(f"GitHub MCP tool {name!r} is explicitly marked as mutating")
            schema = _field(tool, "inputSchema", "input_schema", default={"type": "object", "properties": {}})
            description = str(_field(tool, "description", default="") or "")
            alias = f"gh_{index}_{re.sub(r'[^A-Za-z0-9_]', '_', name)[:42]}"
            self.bindings[alias] = ToolBinding(
                alias=alias,
                server_key="github",
                server_tool_name=name,
                description=f"Read-only GitHub MCP tool `{name}`. {description}".strip(),
                input_schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            )

    def set_reference_scope(self, dataset_version: str, category: str, exclude_subcategory: str) -> None:
        self.reference_scope = (dataset_version, category, exclude_subcategory)

    def _bind_reference_tools(self) -> None:
        available = self.server_tools["reference"]
        expected = {
            "list_reference_tasks": {
                "name": "ref_list_reference_tasks",
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "description": "List subcategories and short titles of existing examples in the sampled broad category.",
            },
            "get_reference_task": {
                "name": "ref_get_reference_task",
                "schema": {"type": "object", "properties": {"subcategory": {"type": "string"}, "title": {"type": "string"}}, "required": ["subcategory", "title"], "additionalProperties": False},
                "description": "Fetch the full visible description for one listed example by its subcategory and title.",
            },
        }
        for server_name, spec in expected.items():
            if server_name not in available:
                raise ValueError(f"reference MCP server is missing required tool {server_name!r}")
            self.bindings[spec["name"]] = ToolBinding(
                alias=spec["name"], server_key="reference", server_tool_name=server_name,
                description=spec["description"], input_schema=spec["schema"],
            )

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": binding.alias,
                "description": binding.description,
                "input_schema": binding.input_schema,
            }
            for binding in self.bindings.values()
        ]

    def describe_binding(self, alias: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return the MCP destination and effective arguments for an attempted model tool call."""
        binding = self.bindings.get(alias)
        if binding is None:
            return {"tool_alias": alias, "arguments": arguments}
        actual_arguments = arguments
        if binding.server_key == "reference" and self.reference_scope is not None:
            dataset_version, category, exclude_subcategory = self.reference_scope
            actual_arguments = {
                **arguments,
                "dataset_version": dataset_version,
                "category": category,
                "exclude_subcategory": exclude_subcategory,
            }
        return {
            "tool_alias": alias,
            "server": binding.server_key,
            "tool": binding.server_tool_name,
            "arguments": actual_arguments,
        }

    async def novelty_reference_tasks(self, topic: Any, dataset_version: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch all visible descriptions for the exact sampled subcategory for the isolated novelty judge."""
        session = self.sessions["reference"]
        tool_name = "list_novelty_reference_tasks"
        if tool_name not in self.server_tools["reference"]:
            raise ValueError(f"reference MCP server is missing required tool {tool_name!r}")
        args = {"dataset_version": dataset_version, "category": topic.category, "subcategory": topic.subcategory}
        result = await _with_timeout(
            session.call_tool(tool_name, arguments=args),
            float(self.config["reference"].get("timeout_seconds", 60)),
        )
        if _field(result, "isError", "is_error", default=False):
            raise RuntimeError(f"novelty reference MCP call failed: {_content_text(result)}")
        payload = _structured_payload(result)
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            raise ValueError("novelty reference MCP must return a tasks array")
        for task in tasks:
            if task.get("category") != topic.category or task.get("subcategory") != topic.subcategory:
                raise ValueError(f"novelty reference {task.get('task_id', '<unknown>')} is outside the exact topic")
            if not task.get("task_id") or not task.get("description"):
                raise ValueError("novelty reference tasks need task_id and description")
            task["description"] = re.sub(
                r"terminal[\s-]*bench(?:mark)?(?:\s+4(?:\.0(?:\.0)?)?)?",
                "reference benchmark",
                str(task["description"]),
                flags=re.IGNORECASE,
            )
        metadata = payload.get("source", {})
        return tasks, metadata if isinstance(metadata, dict) else {}

    async def call_github(self, alias: str, arguments: dict[str, Any], timeout_seconds: float) -> tuple[str, dict[str, Any]]:
        binding = self.bindings.get(alias)
        if binding is None:
            raise ValueError(f"model requested a tool that is not allowed: {alias}")
        actual_arguments = arguments
        if binding.server_key == "reference":
            if self.reference_scope is None:
                raise RuntimeError("reference MCP scope has not been set")
            dataset_version, category, exclude_subcategory = self.reference_scope
            actual_arguments = {**arguments, "dataset_version": dataset_version, "category": category, "exclude_subcategory": exclude_subcategory}
        result = await _with_timeout(
            self.sessions[binding.server_key].call_tool(binding.server_tool_name, arguments=actual_arguments),
            timeout_seconds,
        )
        is_error = bool(_field(result, "isError", "is_error", default=False))
        text = _content_text(result)
        structured = _field(result, "structuredContent", "structured_content", default=None)
        if structured is not None:
            text = json.dumps(structured, ensure_ascii=False)
        if binding.server_key == "reference" and binding.server_tool_name == "get_reference_task" and not is_error:
            card = json.loads(text)
            card["description"] = re.sub(
                r"terminal[\s-]*bench(?:mark)?(?:\s+4(?:\.0(?:\.0)?)?)?",
                "reference benchmark",
                str(card.get("description", "")),
                flags=re.IGNORECASE,
            )
            text = json.dumps(card, ensure_ascii=False)
        return text, {"tool": binding.server_tool_name, "arguments": actual_arguments, "is_error": is_error, "server": binding.server_key}


async def _with_timeout(awaitable: Any, timeout_seconds: float) -> Any:
    import asyncio

    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


def _content_text(result: Any) -> str:
    content = _field(result, "content", default=[]) or []
    pieces: list[str] = []
    for block in content:
        if _field(block, "type") == "text":
            pieces.append(str(_field(block, "text", default="")))
    if not pieces:
        structured = _field(result, "structuredContent", "structured_content", default=None)
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)
    return "\n".join(pieces)


def _structured_payload(result: Any) -> Any:
    structured = _field(result, "structuredContent", "structured_content", default=None)
    if structured is not None:
        return structured
    text = _content_text(result).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("reference MCP must return structured JSON; free-form text is not accepted") from exc
