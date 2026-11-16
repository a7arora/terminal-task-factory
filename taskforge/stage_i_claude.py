"""Stage I: generate an initial task package from a Stage G-cleared specification."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .stage_b import MODEL_ID
from .stage_f import _parse_json, _validate_spec

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "task_implementer.md"
ROLES = {"solver_visible", "reference_solution", "grader", "authoring"}
ROLE_DIRS = {
    "solver_visible": "workspace",
    "reference_solution": "reference",
    "grader": "grader",
    "authoring": "authoring",
}
MAX_FILE_BYTES = 2_000_000
MAX_PACKAGE_BYTES = 15_000_000
AGENT_MAX_TURNS = 20
AGENT_MAX_BUDGET_USD = 1.50
GLM_DEFAULT_SOFT_TARGET_USD = 1.50
GLM_DEFAULT_HARD_LIMIT_USD = 1.50


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _safe_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"unsafe Stage I file path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Stage I file path must be a clean relative path: {value!r}")
    return path


def _validate_approval(spec_path: Path, approval_status_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    status = json.loads(approval_status_path.read_text(encoding="utf-8"))
    allowed_statuses = {"spec_approved_by_reviser_loop", "spec_proceeding_after_review_limit"}
    if not isinstance(status, dict) or status.get("status") not in allowed_statuses:
        raise ValueError(
            "Stage I requires a Stage G status of 'spec_approved_by_reviser_loop' "
            "or 'spec_proceeding_after_review_limit'"
        )
    recorded_spec = status.get("task_spec")
    if not isinstance(recorded_spec, str) or not recorded_spec.strip():
        raise ValueError("Stage G status does not identify its task_spec")
    recorded_path = Path(recorded_spec)
    if not recorded_path.is_absolute():
        recorded_path = Path.cwd() / recorded_path
    if recorded_path.resolve() != spec_path.resolve():
        raise ValueError("the supplied spec path does not match the task_spec recorded in Stage G status")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("approved task spec must contain a JSON object")
    # Stage I does not have the accepted idea at hand, so it checks the spec's own structure
    # with its category/objective fields mirrored as the protected idea values.
    protected_idea = {key: spec.get(key) for key in ("category", "subcategory", "objective", "core_challenge", "expected_deliverable")}
    _validate_spec(spec, protected_idea)
    return spec, status


def _render_prompt(spec: dict[str, Any]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{{STAGE_G_SPEC_JSON}}", json.dumps(spec, indent=2, ensure_ascii=False))


def _load_stage_g_review_context(status: dict[str, Any]) -> dict[str, Any]:
    review_path = status.get("stage_g_review")
    if not isinstance(review_path, str) or not review_path.strip():
        return {"stage_g_status": status.get("status"), "review_rounds": []}
    path = Path(review_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    summary = json.loads(path.read_text(encoding="utf-8"))
    rounds = summary.get("rounds", []) if isinstance(summary, dict) else []
    review_rounds = []
    for item in rounds:
        if not isinstance(item, dict):
            continue
        review_rounds.append({
            key: item[key]
            for key in ("reviewer", "decision", "findings", "changes")
            if key in item
        })
    return {
        "stage_g_status": status.get("status"),
        "review_summary_status": summary.get("status") if isinstance(summary, dict) else None,
        "reviewers_used": summary.get("reviewers_used") if isinstance(summary, dict) else None,
        "review_rounds": review_rounds,
    }


def _validate_implementation(value: dict[str, Any]) -> list[dict[str, str]]:
    expected = {"decision", "summary", "blocking_questions", "implementation_notes", "files"}
    if set(value) != expected:
        raise ValueError(f"Stage I response must contain exactly: {sorted(expected)}")
    if not isinstance(value["decision"], str) or value["decision"] not in {"ready", "blocked"}:
        raise ValueError("Stage I decision must be 'ready' or 'blocked'")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ValueError("Stage I summary must be a non-empty string")
    for field in ("blocking_questions", "implementation_notes", "files"):
        if not isinstance(value[field], list):
            raise ValueError(f"Stage I {field} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value["blocking_questions"]):
        raise ValueError("Stage I blocking_questions must contain non-empty strings")
    if any(not isinstance(item, str) or not item.strip() for item in value["implementation_notes"]):
        raise ValueError("Stage I implementation_notes must contain non-empty strings")

    files: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    roles_present: set[str] = set()
    total_bytes = 0
    for item in value["files"]:
        if not isinstance(item, dict) or set(item) != {"role", "path", "purpose", "content"}:
            raise ValueError("each Stage I file needs role, path, purpose, and content")
        role = item["role"]
        if role not in ROLES:
            raise ValueError(f"invalid Stage I file role: {role!r}")
        relative = _safe_relative_path(item["path"])
        folded = (role, relative.as_posix().casefold())
        if folded in seen:
            raise ValueError(f"duplicate Stage I output path: {role}/{relative}")
        seen.add(folded)
        if not isinstance(item["purpose"], str) or not item["purpose"].strip():
            raise ValueError("Stage I file purpose must be a non-empty string")
        if not isinstance(item["content"], str) or not item["content"].strip():
            raise ValueError("Stage I file content must be non-empty text")
        content_bytes = len(item["content"].encode("utf-8"))
        if content_bytes > MAX_FILE_BYTES:
            raise ValueError(f"Stage I file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        total_bytes += content_bytes
        if total_bytes > MAX_PACKAGE_BYTES:
            raise ValueError(f"Stage I package exceeds {MAX_PACKAGE_BYTES} bytes")
        roles_present.add(role)
        files.append({key: item[key] for key in ("role", "path", "purpose", "content")})

    if value["decision"] == "blocked":
        if not value["blocking_questions"] or files:
            raise ValueError("a blocked Stage I result needs questions and must not contain files")
    else:
        if value["blocking_questions"]:
            raise ValueError("a ready Stage I result cannot have blocking questions")
        missing_roles = ROLES - roles_present
        if missing_roles:
            raise ValueError(f"ready Stage I result is missing file roles: {sorted(missing_roles)}")
        stage_j_manifests = [
            item for item in files
            if item["role"] == "authoring" and item["path"] == "stage-j.json"
        ]
        if len(stage_j_manifests) != 1:
            raise ValueError("ready Stage I result must include exactly one authoring/stage-j.json verification manifest")
        try:
            stage_j_manifest = json.loads(stage_j_manifests[0]["content"])
        except json.JSONDecodeError as exc:
            raise ValueError("authoring/stage-j.json must contain valid JSON") from exc
        if not isinstance(stage_j_manifest, dict) or stage_j_manifest.get("version") != 1:
            raise ValueError("authoring/stage-j.json must be a version 1 verification manifest")
        if not isinstance(stage_j_manifest.get("base_image"), str) or not stage_j_manifest["base_image"].strip():
            raise ValueError("authoring/stage-j.json must declare base_image")
        if not any(
            item["role"] == "authoring" and item["path"] == "image/requirements.in"
            for item in files
        ):
            raise ValueError("ready Stage I result must include authoring/image/requirements.in")
        rubrics = [
            item for item in files
            if item["role"] == "authoring" and item["path"] == "rubric.json"
        ]
        if len(rubrics) != 1:
            raise ValueError("ready Stage I result must include exactly one authoring/rubric.json")
        try:
            rubric = json.loads(rubrics[0]["content"])
        except json.JSONDecodeError as exc:
            raise ValueError("authoring/rubric.json must contain valid JSON") from exc
        if not isinstance(rubric, list) or not rubric:
            raise ValueError("authoring/rubric.json must be a non-empty ordered list")
        for index, item in enumerate(rubric):
            if not isinstance(item, dict) or set(item) != {"score", "criterion", "required", "tags"}:
                raise ValueError(f"rubric item {index} needs exactly score, criterion, required, and tags")
            score = item["score"]
            if not isinstance(score, int) or isinstance(score, bool) or score == 0:
                raise ValueError(f"rubric item {index} score must be a non-zero integer")
            if not isinstance(item["criterion"], str) or not item["criterion"].strip():
                raise ValueError(f"rubric item {index} criterion must be non-empty text")
            if not isinstance(item["required"], bool):
                raise ValueError(f"rubric item {index} required must be boolean")
            if not isinstance(item["tags"], list) or any(not isinstance(tag, str) or not tag.strip() for tag in item["tags"]):
                raise ValueError(f"rubric item {index} tags must be an array of non-empty strings")
        synthetic_manifest_files = [
            item for item in files
            if item["role"] == "solver_visible" and item["path"] == "synthetic/manifest.json"
        ]
        if len(synthetic_manifest_files) != 1:
            raise ValueError("ready Stage I result must include exactly one workspace/synthetic/manifest.json")
        try:
            synthetic_manifest = json.loads(synthetic_manifest_files[0]["content"])
        except json.JSONDecodeError as exc:
            raise ValueError("workspace/synthetic/manifest.json must contain valid JSON") from exc
        if not isinstance(synthetic_manifest, dict) or set(synthetic_manifest) != {"version", "files"} or synthetic_manifest.get("version") != 1 or not isinstance(synthetic_manifest.get("files"), list):
            raise ValueError("workspace/synthetic/manifest.json must have version 1 and a files array")
        listed: dict[str, str] = {}
        for index, entry in enumerate(synthetic_manifest["files"]):
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise ValueError(f"synthetic manifest files[{index}] must contain exactly path and sha256")
            relative = _safe_relative_path(entry["path"]).as_posix()
            digest = entry["sha256"]
            if relative == "manifest.json" or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"synthetic manifest files[{index}] has an invalid path or SHA-256")
            if relative in listed:
                raise ValueError(f"synthetic manifest repeats attachment path {relative!r}")
            listed[relative] = digest
        actual_synthetic = {
            item["path"][len("synthetic/"):]: hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
            for item in files
            if item["role"] == "solver_visible" and item["path"].startswith("synthetic/") and item["path"] != "synthetic/manifest.json"
        }
        if listed != actual_synthetic:
            raise ValueError("synthetic manifest paths/hashes must exactly match the supplied synthetic attachments")
    return files


def _collect_package_files(
    package_root: Path,
    purposes: dict[tuple[str, str], str],
) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    total_bytes = 0
    for role, role_dir in ROLE_DIRS.items():
        root = package_root / role_dir
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            content = path.read_text(encoding="utf-8")
            size = len(content.encode("utf-8"))
            if size > MAX_FILE_BYTES:
                raise ValueError(f"Stage I file exceeds {MAX_FILE_BYTES} bytes: {role}/{relative}")
            total_bytes += size
            if total_bytes > MAX_PACKAGE_BYTES:
                raise ValueError(f"Stage I package exceeds {MAX_PACKAGE_BYTES} bytes")
            files.append({
                "role": role,
                "path": relative,
                "purpose": purposes.get((role, relative), "Generated task-package file."),
                "content": content,
            })
    return files


def _write_synthetic_manifest(package_root: Path, purposes: dict[tuple[str, str], str]) -> None:
    synthetic_root = package_root / ROLE_DIRS["solver_visible"] / "synthetic"
    synthetic_root.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(item for item in synthetic_root.rglob("*") if item.is_file() and item.name != "manifest.json"):
        entries.append({
            "path": path.relative_to(synthetic_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    _write_json(synthetic_root / "manifest.json", {"version": 1, "files": entries})
    purposes[("solver_visible", "synthetic/manifest.json")] = (
        "Hash manifest for all solver-visible synthetic attachments."
    )


async def _run_agent_impl(
    prompt: str,
    package_root: Path,
    purposes: dict[tuple[str, str], str],
    on_progress: Any,
    *,
    glm_mode: bool = False,
    sandbox: Any = None,
    transcript_path: Path | None = None,
    budget_usd: float = AGENT_MAX_BUDGET_USD,
    max_turns: int = AGENT_MAX_TURNS,
    accounting_proxy: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
            tool,
        )
    except ImportError as exc:
        raise RuntimeError("Stage I requires `pip install -e '.[stage-i]'` for the Claude Agent SDK") from exc
    from .provider_config import GLM_CLAUDE_CODE_MODEL, glm_claude_code_env

    def budget_status() -> dict[str, Any]:
        if not glm_mode:
            return {"budget_usd": budget_usd, "tracking": "SDK budget only; live GLM estimate unavailable"}
        return accounting_proxy.meter.status()

    def add_budget_to_tool_result(result: dict[str, Any]) -> dict[str, Any]:
        if glm_mode:
            result.setdefault("content", []).append({
                "type": "text", "text": "Current budget status: " + json.dumps(budget_status())
            })
        return result

    def package_path(role_value: Any, relative_value: Any) -> tuple[str, str, Path]:
        if role_value not in ROLE_DIRS:
            raise ValueError(f"invalid Stage I file role: {role_value!r}")
        relative = _safe_relative_path(relative_value)
        role = str(role_value)
        relative_text = relative.as_posix()
        target = package_root / ROLE_DIRS[role]
        for part in relative.parts:
            target = target / part
        if package_root.resolve() not in target.resolve().parents:
            raise ValueError("Stage I tool path escapes the task package")
        return role, relative_text, target

    @tool("write_task_file", "Create or replace one UTF-8 file in a task-package role.", {
        "role": str, "path": str, "purpose": str, "content": str,
    })
    async def write_task_file(args: dict[str, Any]) -> dict[str, Any]:
        role, relative, target = package_path(args.get("role"), args.get("path"))
        purpose = args.get("purpose")
        content = args.get("content")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("purpose must be non-empty text")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be non-empty text")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
        current_total = sum(
            item.stat().st_size for item in package_root.rglob("*")
            if item.is_file() and item != target
        ) if package_root.exists() else 0
        if current_total + len(encoded) > MAX_PACKAGE_BYTES:
            raise ValueError(f"package exceeds {MAX_PACKAGE_BYTES} bytes")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        purposes[(role, relative)] = purpose.strip()
        on_progress(f"wrote {role}/{relative}")
        result = {"content": [{"type": "text", "text": f"Wrote {role}/{relative} ({len(encoded)} bytes)."}]}
        result = add_budget_to_tool_result(result)
        record_trace("tool_result", name="write_task_file", input=args, result=result)
        return result

    @tool("read_task_file", "Read one UTF-8 file from a task-package role.", {
        "role": str, "path": str,
    })
    async def read_task_file(args: dict[str, Any]) -> dict[str, Any]:
        role, relative, target = package_path(args.get("role"), args.get("path"))
        if not target.is_file():
            raise ValueError(f"file does not exist: {role}/{relative}")
        result = {"content": [{"type": "text", "text": target.read_text(encoding="utf-8")}]}
        result = add_budget_to_tool_result(result)
        record_trace("tool_result", name="read_task_file", input=args, result=result)
        return result

    @tool("list_task_files", "List files already created in the task package.", {})
    async def list_task_files(_: dict[str, Any]) -> dict[str, Any]:
        files = _collect_package_files(package_root, purposes)
        summary = [{"role": item["role"], "path": item["path"], "purpose": item["purpose"]} for item in files]
        result = {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]}
        result = add_budget_to_tool_result(result)
        record_trace("tool_result", name="list_task_files", input={}, result=result)
        return result

    @tool("delete_task_file", "Delete one file from a task-package role if it was created by mistake.", {
        "role": str, "path": str,
    })
    async def delete_task_file(args: dict[str, Any]) -> dict[str, Any]:
        role, relative, target = package_path(args.get("role"), args.get("path"))
        if target.is_file():
            target.unlink()
            purposes.pop((role, relative), None)
            on_progress(f"removed {role}/{relative}")
        result = {"content": [{"type": "text", "text": f"Removed {role}/{relative} if it existed."}]}
        result = add_budget_to_tool_result(result)
        record_trace("tool_result", name="delete_task_file", input=args, result=result)
        return result

    async def code_exec(args: dict[str, Any]) -> dict[str, Any]:
        if sandbox is None:
            return {"content": [{"type": "text", "text": "shell access is disabled"}], "isError": True}
        command = args.get("cmd")
        if not isinstance(command, str) or not command.strip():
            return {"content": [{"type": "text", "text": "cmd must be non-empty text"}], "isError": True}
        result = sandbox.execute(command, 600)
        on_progress(f"sandbox command completed (exit={result.get('returncode')})")
        tool_result = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        tool_result = add_budget_to_tool_result(tool_result)
        record_trace("tool_result", name="code_exec", input=args, result=result)
        return tool_result

    tools = [write_task_file, read_task_file, list_task_files, delete_task_file]
    if glm_mode:
        from claude_agent_sdk import tool

        @tool("code_exec", "Run bounded shell commands in the network-disabled authoring container.", {"cmd": str})
        async def code_exec_tool(args: dict[str, Any]) -> dict[str, Any]:
            return await code_exec(args)

        tools.append(code_exec_tool)

        @tool("budget_status", "Return the latest estimated GLM spend and remaining Stage I budget.", {})
        async def budget_status_tool(_: dict[str, Any]) -> dict[str, Any]:
            result = {"content": [{"type": "text", "text": json.dumps(budget_status())}]}
            record_trace("tool_result", name="budget_status", input={}, result=result)
            return result

        tools.append(budget_status_tool)

    server = create_sdk_mcp_server(
        name="taskforge_package",
        version="1.0.0",
        tools=tools,
    )
    allowed_tools = [
        "mcp__taskforge_package__write_task_file",
        "mcp__taskforge_package__read_task_file",
        "mcp__taskforge_package__list_task_files",
        "mcp__taskforge_package__delete_task_file",
    ]
    if glm_mode:
        allowed_tools.append("mcp__taskforge_package__code_exec")
        allowed_tools.append("mcp__taskforge_package__budget_status")
    system_prompt = (
        "You are Stage I, a decisive task-package designer and implementer. Produce a concrete, "
        "repairable benchmark package using defensible explicit design choices. Block only when no "
        "reasonable choice can produce a coherent and valid task. Record assumptions and triage risks. "
        "Use the taskforge_package tools to create, read, list, or remove package files. "
        + (f"The Stage I GLM budget is ${budget_usd:.2f}. Tool results include the latest estimated spent and remaining amounts, updated after each model response. Check budget_status when needed; reserve budget for required files and final validation, and prioritize completeness within the remaining amount. This is a token-price estimate, not the provider's exact bill. " if glm_mode else "")
        + ("Use code_exec only for bounded checks inside the provided offline Docker workspace. Never assume a check passed unless its output confirms it. " if glm_mode else "")
        + "Do not use network or other tools."
    )
    options = ClaudeAgentOptions(
        model=GLM_CLAUDE_CODE_MODEL if glm_mode else MODEL_ID,
        cwd=str(package_root),
        system_prompt=system_prompt,
        tools=[],
        mcp_servers={"taskforge_package": server},
        allowed_tools=allowed_tools,
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        max_turns=max_turns,
        # Claude Code's local price table does not know Z.ai's GLM rates. The
        # localhost proxy enforces the GLM budget from Z.ai's final usage events.
        max_budget_usd=None if glm_mode else budget_usd,
        env=glm_claude_code_env(proxy_url=accounting_proxy.base_url) if glm_mode else {},
        **({"effort": "high"} if not glm_mode else {}),
    )

    responses: list[dict[str, Any]] = []
    final_text = ""
    request_number = 0
    trace_lock = threading.Lock()
    live_log_path = transcript_path.with_name("agent-live.log") if transcript_path else None

    def live_text(event: str, fields: dict[str, Any]) -> str:
        stamp = datetime.now(timezone.utc).isoformat()
        if event == "model_prompt":
            return f"\n[{stamp}] PROMPT #{fields.get('request_number')}\n{fields.get('text', '')}\n"
        if event == "system_prompt":
            return f"\n[{stamp}] AGENT SYSTEM PROMPT\n{fields.get('text', '')}\n"
        if event == "model_reply":
            pieces = []
            for block in fields.get("content", []):
                if block.get("type") == "text":
                    pieces.append(block.get("text", ""))
                elif block.get("type") == "tool_call":
                    pieces.append("[TOOL CALL " + str(block.get("name")) + "]\n" + json.dumps(block.get("input", {}), indent=2, ensure_ascii=False))
            return f"\n[{stamp}] MODEL RESPONSE #{fields.get('request_number')}\n" + "\n".join(pieces) + "\n"
        if event == "tool_result":
            return f"\n[{stamp}] TOOL RESULT: {fields.get('name')}\n{json.dumps(fields.get('result'), indent=2, ensure_ascii=False, default=str)}\n"
        if event == "provider_usage":
            usage = fields.get("usage", {})
            return (f"\n[{stamp}] GLM RESPONSE COST #{fields.get('responses_with_usage')}\n"
                    f"input={usage.get('input_tokens', 0)} cached_input={usage.get('cache_read_input_tokens', 0)} "
                    f"output={usage.get('output_tokens', 0)}\n"
                    f"this response=${fields.get('response_estimated_cost_usd', 0):.6f}; "
                    f"cumulative=${fields.get('cumulative_estimated_spend_usd', 0):.6f}; "
                    f"remaining={fields.get('estimated_remaining_usd')}\n")
        if event == "model_result":
            return (f"\n[{stamp}] AGENT TURN COMPLETE #{fields.get('request_number')}\n"
                    f"prompt-response estimated cost=${fields.get('request_estimated_cost_usd')}\n"
                    f"cumulative accounting={fields.get('glm_accounting')}\n")
        return ""

    def record_trace(event: str, **fields: Any) -> None:
        record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        readable = live_text(event, fields)
        with trace_lock:
            if transcript_path is not None:
                transcript_path.parent.mkdir(parents=True, exist_ok=True)
                with transcript_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    stream.flush()
            if live_log_path is not None and readable:
                with live_log_path.open("a", encoding="utf-8") as stream:
                    stream.write(readable)
                    stream.flush()
        if readable:
            print(readable, flush=True)

    if glm_mode:
        accounting_proxy.meter.on_usage = lambda data: record_trace("provider_usage", **data)

    record_trace("system_prompt", text=system_prompt)
    async with ClaudeSDKClient(options=options) as client:
        current_prompt = prompt
        while True:
            request_number += 1
            prompt_start_spend = budget_status().get("estimated_spent_usd") if glm_mode else None
            record_trace("model_prompt", request_number=request_number, text=current_prompt)
            await client.query(current_prompt)
            text_parts: list[str] = []
            assistant_turn_usages: list[dict[str, Any]] = []
            result_message: Any = None
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    usage = getattr(message, "usage", None)
                    content: list[dict[str, Any]] = []
                    if isinstance(usage, dict):
                        assistant_turn_usages.append(usage)
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_input = getattr(block, "input", {}) or {}
                            content.append({"type": "tool_call", "name": getattr(block, "name", "unknown"), "input": tool_input})
                            suffix = f" {tool_input.get('role')}/{tool_input.get('path')}" if isinstance(tool_input, dict) and tool_input.get("path") else ""
                            on_progress(f"agent tool: {getattr(block, 'name', 'unknown')}{suffix}")
                        elif isinstance(block, TextBlock) and block.text.strip():
                            text_parts.append(block.text)
                            content.append({"type": "text", "text": block.text})
                    record_trace("model_reply", request_number=request_number, content=content, usage=usage)
                elif isinstance(message, ResultMessage):
                    result_message = message
                    current_spend = budget_status().get("estimated_spent_usd") if glm_mode else None
                    prompt_cost = (current_spend - prompt_start_spend
                                   if isinstance(current_spend, (int, float)) and isinstance(prompt_start_spend, (int, float))
                                   else current_spend if isinstance(current_spend, (int, float)) and prompt_start_spend is None
                                   else None)
                    record_trace(
                        "model_result", request_number=request_number,
                        result=getattr(message, "result", None),
                        usage=getattr(message, "usage", None),
                        total_cost_usd=getattr(message, "total_cost_usd", None),
                        num_turns=getattr(message, "num_turns", None),
                        is_error=getattr(message, "is_error", None),
                        errors=getattr(message, "errors", None),
                        glm_accounting=budget_status() if glm_mode else None,
                        request_estimated_cost_usd=prompt_cost,
                    )
            final_text = getattr(result_message, "result", None) or "\n".join(text_parts)
            responses.append({
                "num_turns": getattr(result_message, "num_turns", None),
                "total_cost_usd": getattr(result_message, "total_cost_usd", None),
                "terminal_reason": getattr(result_message, "terminal_reason", None),
                "is_error": getattr(result_message, "is_error", None),
                "session_id": getattr(result_message, "session_id", None),
                "usage": getattr(result_message, "usage", None),
                "assistant_turn_usages": assistant_turn_usages,
                "model_usage": getattr(result_message, "model_usage", None),
            })
            if result_message is not None and getattr(result_message, "is_error", False):
                errors = getattr(result_message, "errors", None) or []
                raise RuntimeError(f"Claude Agent SDK Stage I call failed: {errors or final_text}")
            if len(responses) == 1:
                try:
                    result = _parse_json(final_text)
                    if not isinstance(result, dict) or set(result) != {
                        "decision", "summary", "blocking_questions", "implementation_notes",
                    }:
                        raise ValueError("Stage I agent summary must contain decision, summary, blocking_questions, and implementation_notes")
                    if result["decision"] == "ready":
                        _write_synthetic_manifest(package_root, purposes)
                    files = _collect_package_files(package_root, purposes)
                    _validate_implementation({**result, "files": files})
                    return final_text, responses
                except (ValueError, json.JSONDecodeError) as validation_error:
                    if getattr(result_message, "terminal_reason", None) == "max_turns":
                        raise ValueError(f"Stage I reached its {max_turns}-turn limit before producing a valid package: {validation_error}") from validation_error
                    current_prompt = (
                        "The package failed TaskForge's deterministic validation. Repair the files using the "
                        "provided package tools, then return the required final JSON summary only. Make the "
                        "smallest changes needed.\n\nValidation error:\n" +
                        f"{type(validation_error).__name__}: {validation_error}\n\n" +
                        "Required response schema: {\"decision\":\"ready|blocked\",\"summary\":\"...\","
                        "\"blocking_questions\":[...],\"implementation_notes\":[...]}"
                    )
                    on_progress(f"deterministic validation failed; using the single repair cycle: {validation_error}")
                    continue
            try:
                result = _parse_json(final_text)
                if not isinstance(result, dict) or set(result) != {
                    "decision", "summary", "blocking_questions", "implementation_notes",
                }:
                    raise ValueError("Stage I agent summary must contain decision, summary, blocking_questions, and implementation_notes")
                if result["decision"] == "ready":
                    _write_synthetic_manifest(package_root, purposes)
                files = _collect_package_files(package_root, purposes)
                _validate_implementation({**result, "files": files})
                return final_text, responses
            except (ValueError, json.JSONDecodeError) as validation_error:
                raise ValueError(f"Stage I response failed validation after one repair cycle: {validation_error}") from validation_error


async def _run_agent(
    prompt: str, package_root: Path, purposes: dict[tuple[str, str], str], on_progress: Any,
    *, glm_mode: bool = False, sandbox: Any = None, transcript_path: Path | None = None,
    budget_usd: float = AGENT_MAX_BUDGET_USD, max_turns: int = AGENT_MAX_TURNS,
) -> tuple[str, list[dict[str, Any]]]:
    if not glm_mode:
        return await _run_agent_impl(
            prompt, package_root, purposes, on_progress, glm_mode=False, sandbox=sandbox,
            transcript_path=transcript_path, budget_usd=budget_usd, max_turns=max_turns,
        )
    from .glm_accounting_proxy import GLMAccountingProxy, GLMUsageMeter
    from .provider_config import glm_upstream_config
    upstream, api_key = glm_upstream_config()
    meter = GLMUsageMeter(max_cost_usd=budget_usd)
    with GLMAccountingProxy(upstream, api_key, meter) as proxy:
        text, responses = await _run_agent_impl(
            prompt, package_root, purposes, on_progress, glm_mode=True, sandbox=sandbox,
            transcript_path=transcript_path, budget_usd=budget_usd, max_turns=max_turns,
            accounting_proxy=proxy,
        )
    for response in responses:
        response["glm_accounting"] = meter.status()
    return text, responses


async def run_stage_i(
    spec_path: Path,
    approval_status_path: Path,
    output_dir: Path,
    *,
    provider: str = "claude",
) -> Path:
    """Generate a task package from a Stage G spec cleared for implementation; never execute generated code."""
    if output_dir.exists():
        raise FileExistsError(f"Stage I output already exists: {output_dir}")
    spec, approval_status = _validate_approval(spec_path, approval_status_path)
    glm_mode = provider == "claude-code-glm"
    if provider not in {"claude", "claude-code-glm"}:
        raise ValueError("Stage I SDK provider must be 'claude' or 'claude-code-glm'")
    if glm_mode:
        from .provider_config import glm_upstream_config
        glm_upstream_config()
        try:
            glm_target_usd = float(os.environ.get("GLM_STAGE_I_MAX_COST_USD", GLM_DEFAULT_SOFT_TARGET_USD))
            glm_budget_usd = float(os.environ.get("GLM_STAGE_I_HARD_MAX_COST_USD", GLM_DEFAULT_HARD_LIMIT_USD))
        except ValueError as exc:
            raise ValueError("GLM Stage I cost target and hard limit must be positive numbers") from exc
        if glm_target_usd <= 0 or glm_budget_usd < glm_target_usd:
            raise ValueError("GLM_STAGE_I_HARD_MAX_COST_USD must be at least the positive GLM_STAGE_I_MAX_COST_USD target")
        try:
            glm_max_turns = max(1, min(64, int(os.environ.get("GLM_STAGE_I_MAX_TURNS", 32))))
        except ValueError as exc:
            raise ValueError("GLM_STAGE_I_MAX_TURNS must be an integer from 1 to 64") from exc
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("set ANTHROPIC_API_KEY before running the Claude Stage I backup")
    else:
        glm_budget_usd = AGENT_MAX_BUDGET_USD
        glm_target_usd = AGENT_MAX_BUDGET_USD
        glm_max_turns = AGENT_MAX_TURNS
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Stage I requires `pip install -e '.[stage-i]'` for the Claude Agent SDK") from exc

    task_id = spec["task_id"]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", task_id):
        raise ValueError("approved task_id must be lowercase kebab-case")
    output_dir.mkdir(parents=True)
    review_context = _load_stage_g_review_context(approval_status)
    prompt = _render_prompt(spec).replace(
        "{{STAGE_G_REVIEW_JSON}}",
        json.dumps(review_context, indent=2, ensure_ascii=False),
    )
    (output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    package_root = output_dir / "task-package"
    package_root.mkdir(parents=True)
    sandbox = None
    if glm_mode:
        from .glm_adapter import DockerSolverSandbox
        sandbox = DockerSolverSandbox(
            "python:3.12-slim", None, package_root, output_dir / "sandbox",
            {"memory_mb": 2048, "cpus": 2, "pids_limit": 128},
        )
        sandbox.start()
    purposes: dict[tuple[str, str], str] = {}
    progress_log = output_dir / "agent-progress.log"

    def record_progress(message: str) -> None:
        line = f"[{datetime.now(timezone.utc).isoformat()}] Stage I {message}"
        print(line, flush=True)
        with progress_log.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    try:
        final_text, sdk_responses = await _run_agent(
            prompt, package_root, purposes, record_progress, glm_mode=glm_mode, sandbox=sandbox,
            transcript_path=output_dir / "agent-transcript.jsonl",
            budget_usd=glm_budget_usd, max_turns=glm_max_turns,
        )
    finally:
        if sandbox is not None:
            sandbox.stop()
    try:
        result = _parse_json(final_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Stage I agent did not return its required JSON summary: {exc}") from exc
    files = _collect_package_files(package_root, purposes)
    _validate_implementation({**result, "files": files})
    _write_json(output_dir / "agent-summary.json", result)
    (output_dir / "agent-final-response.txt").write_text(final_text, encoding="utf-8")
    metadata = {
        "stage": "I",
        "agent_sdk": "claude-agent-sdk",
        "model_id": "glm-5.3-flash" if glm_mode else MODEL_ID,
        "max_turns": glm_max_turns,
        "sdk_max_budget_usd": None if glm_mode else glm_budget_usd,
        "proxy_cost_limit_usd": glm_budget_usd if glm_mode else None,
        "soft_cost_target_usd": glm_target_usd,
        "hard_cost_limit_usd": glm_budget_usd,
        "effort": None,
        "tool_access": "TaskForge package tools and network-disabled Docker shell" if glm_mode else "TaskForge package file tools only; built-in tools disabled",
        "usage": sdk_responses[-1].get("usage") if sdk_responses else None,
        "assistant_turn_usage_count": sum(
            len(response.get("assistant_turn_usages") or []) for response in sdk_responses
        ),
        "usage_tracking_source": "zai_final_response_usage_via_local_proxy" if glm_mode else "result_message",
        "model_usage": sdk_responses[-1].get("model_usage") if sdk_responses else None,
        "sdk_reported_cost_usd_untrusted_for_glm": sdk_responses[-1].get("total_cost_usd") if glm_mode and sdk_responses else None,
        "estimated_glm_cost_usd": (sdk_responses[-1].get("glm_accounting", {}).get("estimated_spent_usd")
                                    if glm_mode and sdk_responses else None),
        "glm_input_usd_per_million": 0.15 if glm_mode else None,
        "glm_output_usd_per_million": 0.50 if glm_mode else None,
        "agent_responses": sdk_responses,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "artifact_type": "initial_task_implementation_unverified",
        "schema_repair_cycles_allowed": 1,
    }
    _write_json(output_dir / "implementation-review.json", {
        "decision": result["decision"],
        "summary": result["summary"],
        "blocking_questions": result["blocking_questions"],
        "implementation_notes": result["implementation_notes"],
    })
    _write_json(output_dir / "provenance.json", metadata)
    if result["decision"] == "blocked":
        _write_json(output_dir / "status.json", {
            "status": "blocked_for_spec_clarification",
            "stage_g_spec": str(spec_path),
            "approval_status": str(approval_status_path),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        return output_dir / "implementation-review.json"

    manifest_files: list[dict[str, str]] = []
    for item in files:
        relative = _safe_relative_path(item["path"])
        package_path = Path(ROLE_DIRS[item["role"]]).joinpath(*relative.parts)
        manifest_files.append({
            "role": item["role"],
            "path": package_path.as_posix(),
            "purpose": item["purpose"],
            "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
        })
    _write_json(output_dir / "task-package" / "manifest.json", {
        "task_id": task_id,
        "title": spec["title"],
        "category": spec["category"],
        "subcategory": spec["subcategory"],
        "source_spec": str(spec_path),
        "approval_status": approval_status.get("status"),
        "implementation_status": "generated_unverified",
        "files": manifest_files,
    })
    _write_json(output_dir / "status.json", {
        "status": "implementation_generated_unverified",
        "stage_g_spec": str(spec_path),
        "approval_status": str(approval_status_path),
        "task_package": str(output_dir / "task-package"),
        "next_stage": "Stage J verification and hardening",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return output_dir / "task-package" / "manifest.json"
