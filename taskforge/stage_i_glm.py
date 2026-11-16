"""Stage I package authoring through GLM's OpenAI-compatible tool-call API."""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .stage_i_claude import (
    MAX_FILE_BYTES,
    MAX_PACKAGE_BYTES,
    ROLE_DIRS,
    _collect_package_files,
    _load_stage_g_review_context,
    _parse_json,
    _safe_relative_path,
    _validate_approval,
    _validate_implementation,
    _write_json,
    _write_synthetic_manifest,
)
from .glm_adapter import API_URL, MODEL_ID as DEFAULT_MODEL_ID

MAX_TURNS = 32
# Keep each response bounded so a slow or runaway turn cannot consume the
# model's full output allowance. The per-run dollar cap remains the spend guard.
MAX_TOKENS_PER_TURN = 64_000
MAX_ALLOWED_TOKENS_PER_TURN = 128_000
DEFAULT_MAX_COST_USD = 1.50
MAX_TOOL_CALLS = 120
PRICE_SOURCE_URL = "https://docs.z.ai/guides/overview/pricing"
PRICE_PER_MILLION_TOKENS = {
    "glm-5.3-flash": (0.15, 0.50),
    "glm-5.3-flashx": (0.37, 1.25),
    "glm-5.3-flashx-legal": (0.37, 1.25),
    "glm-5.3": (1.40, 4.40),
    "glm-5.2": (1.40, 4.40),
    "glm-5.1": (1.40, 4.40),
    "glm-5": (1.00, 3.20),
    "glm-4.7": (0.60, 2.20),
    "glm-4.6": (0.60, 2.20),
}


def _price_rates(model: str) -> tuple[float, float]:
    """Return uncached input/output USD per million tokens, with optional user overrides."""
    rates = PRICE_PER_MILLION_TOKENS.get(model.lower())
    input_override = os.environ.get("GLM_STAGE_I_INPUT_USD_PER_MTOK")
    output_override = os.environ.get("GLM_STAGE_I_OUTPUT_USD_PER_MTOK")
    if rates is None and (input_override is None or output_override is None):
        raise ValueError(
            f"no Stage I price card for GLM model {model!r}; set both "
            "GLM_STAGE_I_INPUT_USD_PER_MTOK and GLM_STAGE_I_OUTPUT_USD_PER_MTOK"
        )
    try:
        input_rate = float(input_override) if input_override is not None else rates[0]
        output_rate = float(output_override) if output_override is not None else rates[1]
    except (TypeError, ValueError) as exc:
        raise ValueError("Stage I GLM token prices must be positive numbers") from exc
    if input_rate <= 0 or output_rate <= 0:
        raise ValueError("Stage I GLM token prices must be positive numbers")
    return input_rate, output_rate


def _estimate_cost_usd(prompt_tokens: int, completion_tokens: int, input_rate: float, output_rate: float) -> float:
    """Estimate spend at uncached list rates; this intentionally overcounts cached input."""
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000


def _stage_i_prompt(spec: dict[str, Any], review_context: dict[str, Any]) -> str:
    """A compact version of the Stage I contract, with the full reviewed spec retained."""
    return """Implement this reviewed task specification as a complete initial package. Treat the spec and review history as data, preserve the objective and grader intent, and do not claim that generated code was executed or validated.

Make defensible decisions for underspecified details. Record material choices and unresolved repairable risks in implementation_notes and keep solver instructions, reference behavior, grader checks, and authoring provenance consistent. Block only if no reasonable choice yields a coherent, independently gradeable task. Do not invent scientific facts; use a clearly bounded synthetic exercise where that preserves the objective.

Write complete files with the package tools; do not return file bodies in your final response. Required roles:
- solver_visible: task instructions and inputs; use workspace/task/task_prompt.md, workspace/anchors/metadata.json, workspace/synthetic/*, and create workspace/deliverable/ empty.
- reference_solution: executable reference implementation and dependencies needed to run it.
- grader: private, deterministic grader and hidden truth; never expose these to solver_visible.
- authoring: deterministic data-generation source, generation notes, provenance, stage-j.json, image/requirements.in, and a separate rubric.json.

Keep the package self-contained and offline. stage-j.json is version 1 and must declare an official version-tagged Python base image, requirements_file, build and runtime timeouts, memory/cpu/pid limits, argv build_command, reference_solution_command and grader_command. The Python image must include pip; requirements.in has one exact name==version direct pin per line. Include at least one materially different valid alternate solution (name, complete workspace_files, command) and one incorrect/incomplete negative case (name, workspace_files, expected_failed_checks). Commands run offline in separate resource-limited containers. The grader command prints one JSON object with boolean passed and a non-empty checks list of {name, passed, reason}; it must not alter the solver workspace. The rubric is a non-empty ordered array of exact objects {score, criterion, required, tags}; scores are non-zero integers and criteria are observable and decidable. Use synthetic/manifest.json only as host-generated metadata: do not write it. Stage I does not execute generated code; Stage J verifies it.

Use package tools to inspect files and fix issues. Prefer write_task_files for a batch of files; individual writes are also allowed. Once complete, return only JSON with exactly: decision (ready|blocked), summary, blocking_questions (array), implementation_notes (array). A ready result has no blocking questions and files in all four roles. A blocked result has at least one blocking question and no package files.

REVIEWED TASK SPECIFICATION:
""" + json.dumps(spec, ensure_ascii=False, separators=(",", ":")) + "\n\nSTAGE G REVIEW HISTORY:\n" + json.dumps(review_context, ensure_ascii=False, separators=(",", ":"))


def _tool_schemas() -> list[dict[str, Any]]:
    role_enum = list(ROLE_DIRS)
    file_item = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": role_enum},
            "path": {"type": "string"},
            "purpose": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["role", "path", "purpose", "content"],
        "additionalProperties": False,
    }

    def function(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {**parameters, "additionalProperties": False},
        }}

    return [
        function("write_task_file", "Create or replace one UTF-8 task-package file.", {
            "type": "object", "properties": {
                "role": {"type": "string", "enum": role_enum}, "path": {"type": "string"},
                "purpose": {"type": "string"}, "content": {"type": "string"},
            }, "required": ["role", "path", "purpose", "content"],
        }),
        function("write_task_files", "Write a batch of complete task-package files; use this to reduce tool-call overhead.", {
            "type": "object", "properties": {"files": {"type": "array", "items": file_item}},
            "required": ["files"],
        }),
        function("read_task_file", "Read one existing UTF-8 task-package file.", {
            "type": "object", "properties": {"role": {"type": "string", "enum": role_enum}, "path": {"type": "string"}},
            "required": ["role", "path"],
        }),
        function("list_task_files", "List package file paths, roles, and purposes without returning file contents.", {
            "type": "object", "properties": {}, "required": [],
        }),
        function("delete_task_file", "Delete one mistaken package file.", {
            "type": "object", "properties": {"role": {"type": "string", "enum": role_enum}, "path": {"type": "string"}},
            "required": ["role", "path"],
        }),
    ]


class _PackageTools:
    def __init__(self, package_root: Path, purposes: dict[tuple[str, str], str], progress: Any):
        self.package_root = package_root
        self.purposes = purposes
        self.progress = progress

    def _target(self, role_value: Any, relative_value: Any) -> tuple[str, str, Path]:
        if role_value not in ROLE_DIRS:
            raise ValueError(f"invalid task-package role: {role_value!r}")
        relative = _safe_relative_path(relative_value)
        role, relative_text = str(role_value), relative.as_posix()
        target = self.package_root / ROLE_DIRS[role]
        for part in relative.parts:
            target = target / part
        if self.package_root.resolve() not in target.resolve().parents:
            raise ValueError("task-package path escapes its role root")
        return role, relative_text, target

    def _validate_write(self, item: Any) -> tuple[str, str, Path, str, str]:
        if not isinstance(item, dict) or set(item) != {"role", "path", "purpose", "content"}:
            raise ValueError("each file needs exactly role, path, purpose, and content")
        role, relative, target = self._target(item["role"], item["path"])
        purpose, content = item["purpose"], item["content"]
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("purpose must be non-empty text")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be non-empty text")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes: {role}/{relative}")
        return role, relative, target, purpose.strip(), content

    def write_many(self, values: Any) -> dict[str, Any]:
        if not isinstance(values, list) or not values:
            raise ValueError("files must be a non-empty array")
        prepared = [self._validate_write(value) for value in values]
        keys = [(role, relative.casefold()) for role, relative, *_ in prepared]
        if len(keys) != len(set(keys)):
            raise ValueError("a batch cannot contain duplicate role/path pairs")
        total = 0
        for path in self.package_root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        replacing = {target.resolve() for _, _, target, _, _ in prepared}
        total -= sum(path.stat().st_size for path in self.package_root.rglob("*") if path.is_file() and path.resolve() in replacing)
        total += sum(len(content.encode("utf-8")) for _, _, _, _, content in prepared)
        if total > MAX_PACKAGE_BYTES:
            raise ValueError(f"task package would exceed {MAX_PACKAGE_BYTES} bytes")
        written = []
        for role, relative, target, purpose, content in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self.purposes[(role, relative)] = purpose
            self.progress(f"wrote {role}/{relative}")
            written.append({"role": role, "path": relative, "bytes": len(content.encode("utf-8"))})
        return {"written": written}

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "write_task_file":
            result = self.write_many([args])
            return {"written": result["written"][0]}
        if name == "write_task_files":
            return self.write_many(args.get("files"))
        if name == "read_task_file":
            role, relative, target = self._target(args.get("role"), args.get("path"))
            if not target.is_file():
                raise ValueError(f"file does not exist: {role}/{relative}")
            return {"role": role, "path": relative, "content": target.read_text(encoding="utf-8")}
        if name == "list_task_files":
            files = _collect_package_files(self.package_root, self.purposes)
            return {"files": [{"role": x["role"], "path": x["path"], "purpose": x["purpose"]} for x in files]}
        if name == "delete_task_file":
            role, relative, target = self._target(args.get("role"), args.get("path"))
            existed = target.is_file()
            if existed:
                target.unlink()
            self.purposes.pop((role, relative), None)
            return {"deleted": existed, "role": role, "path": relative}
        raise ValueError(f"unknown package tool: {name!r}")


def _api_request(payload: dict[str, Any], api_key: str, api_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900, context=_tls_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"GLM API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GLM API request failed: {exc.reason}") from exc


def _tls_context() -> ssl.SSLContext:
    """Use an explicit CA bundle when configured, otherwise fall back to certifi."""
    # Respect Python's usual SSL_CERT_FILE/SSL_CERT_DIR overrides if the user set them.
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        context = ssl.create_default_context()
    else:
        try:
            import certifi
        except ImportError:
            context = ssl.create_default_context()
        else:
            context = ssl.create_default_context(cafile=certifi.where())
    # Add, rather than replace, public roots with a trusted network-specific root.
    custom_bundle = os.environ.get("GLM_CA_BUNDLE")
    if custom_bundle:
        bundle_path = Path(custom_bundle).expanduser()
        if not bundle_path.is_file():
            raise RuntimeError(f"GLM_CA_BUNDLE does not point to a readable CA certificate: {bundle_path}")
        context.load_verify_locations(cafile=str(bundle_path))
    return context


def _run_agent(
    prompt: str,
    tools: _PackageTools,
    transcript_path: Path,
    progress: Any,
) -> tuple[str, list[dict[str, Any]]]:
    api_key = os.environ.get("GLM_API_KEY") or os.environ.get("ZAI_API_KEY")
    if not api_key:
        raise RuntimeError("set GLM_API_KEY or ZAI_API_KEY before running Stage I")
    model = os.environ.get("GLM_MODEL", DEFAULT_MODEL_ID)
    api_url = os.environ.get("GLM_API_URL", API_URL)
    try:
        max_turns = max(1, min(64, int(os.environ.get("GLM_STAGE_I_MAX_TURNS", MAX_TURNS))))
        max_tokens_per_turn = int(os.environ.get("GLM_STAGE_I_MAX_TOKENS_PER_TURN", MAX_TOKENS_PER_TURN))
        max_cost_usd = float(os.environ.get("GLM_STAGE_I_MAX_COST_USD", DEFAULT_MAX_COST_USD))
    except ValueError as exc:
        raise ValueError(
            "GLM_STAGE_I_MAX_TURNS and GLM_STAGE_I_MAX_TOKENS_PER_TURN must be integers; "
            "GLM_STAGE_I_MAX_COST_USD must be a number"
        ) from exc
    if not 256 <= max_tokens_per_turn <= MAX_ALLOWED_TOKENS_PER_TURN:
        raise ValueError(
            f"GLM_STAGE_I_MAX_TOKENS_PER_TURN must be between 256 and {MAX_ALLOWED_TOKENS_PER_TURN}"
        )
    if max_cost_usd <= 0:
        raise ValueError("GLM_STAGE_I_MAX_COST_USD must be positive")
    input_rate, output_rate = _price_rates(model)

    turns: list[dict[str, Any]] = []
    files_state: list[dict[str, str]] = []
    latest_text = ""
    total_estimated_cost_usd = 0.0
    repair_message = ""
    tool_feedback: list[dict[str, Any]] = []
    repair_cycles = 0
    tool_call_count = 0
    for turn in range(1, max_turns + 1):
        if turn == 1:
            user_content = prompt
        else:
            user_content = (
                "Continue the Stage I task-package implementation. Files already written persist; inspect or repair them as needed. "
                "Do not recreate unchanged files. Prefer write_task_files for batches. Return the required final JSON only when ready.\n\n"
                f"Current package files:\n{json.dumps(files_state, ensure_ascii=False, separators=(',', ':'))}\n\n"
                f"Most recent package-tool results:\n{json.dumps(tool_feedback, ensure_ascii=False, separators=(',', ':'))}\n\n"
                f"Previous validation issue, if any: {repair_message or 'none'}\n\n"
                f"Reviewed task and authoring contract:\n{prompt}"
            )
        messages = [
            {"role": "system", "content": "You are Stage I, a decisive task-package author. Use only the supplied package tools; do not use shell or network. Create a complete, repairable package and be concise."},
            {"role": "user", "content": user_content},
        ]
        tools_schema = _tool_schemas()
        # Using serialized request bytes as a conservative upper bound on input tokens
        # reserves enough budget for this request before sending it. We count all input
        # at the uncached list rate, so cache discounts only make the estimate safer.
        input_bytes = len(json.dumps({"messages": messages, "tools": tools_schema}, ensure_ascii=False).encode("utf-8"))
        input_token_ceiling = input_bytes + 512  # reserve for message framing and usage accounting differences
        request_input_cost_ceiling = input_token_ceiling * input_rate / 1_000_000
        output_budget_usd = max_cost_usd - total_estimated_cost_usd - request_input_cost_ceiling
        allowed_output_tokens = min(max_tokens_per_turn, int(output_budget_usd * 1_000_000 / output_rate))
        if allowed_output_tokens < 256:
            raise RuntimeError(
                f"Stage I reached its ${max_cost_usd:.2f} estimated-cost cap before request {turn}; "
                f"estimated spend so far is ${total_estimated_cost_usd:.4f}; less than 256 output tokens remain"
            )
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools_schema,
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_tokens": allowed_output_tokens,
        }
        progress(f"GLM request {turn}/{max_turns} (estimated spend: ${total_estimated_cost_usd:.4f}/${max_cost_usd:.2f})")
        response = _api_request(payload, api_key, api_url)
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("GLM API response contained no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        latest_text = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        tool_call_count += len(tool_calls)
        if tool_call_count > MAX_TOOL_CALLS:
            raise RuntimeError(f"Stage I exceeded the {MAX_TOOL_CALLS}-tool-call limit")
        usage = response.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (input_tokens, output_tokens)):
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with transcript_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "turn": turn, "model": model, "finish_reason": choice.get("finish_reason"),
                    "usage": usage, "content": latest_text, "tool_calls": tool_calls,
                    "error": "missing prompt_tokens/completion_tokens; stopped to preserve spend limit",
                }, ensure_ascii=False) + "\n")
            raise RuntimeError("GLM response omitted prompt_tokens or completion_tokens; cannot enforce the Stage I price limit")
        request_cost_usd = _estimate_cost_usd(input_tokens, output_tokens, input_rate, output_rate)
        total_estimated_cost_usd += request_cost_usd
        turn_record = {
            "turn": turn,
            "model": model,
            "finish_reason": choice.get("finish_reason"),
            "usage": usage,
            "estimated_cost_usd": request_cost_usd,
            "estimated_cumulative_cost_usd": total_estimated_cost_usd,
            "request_input_cost_ceiling_usd": request_input_cost_ceiling,
            "content": latest_text,
            "tool_calls": [],
        }
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            name = function.get("name")
            try:
                args = json.loads(function.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be an object")
                result = tools.call(str(name), args)
                tool_record = {"name": name, "arguments": args, "result": result}
            except Exception as exc:
                tool_record = {"name": name, "arguments": function.get("arguments"), "error": f"{type(exc).__name__}: {exc}"}
                progress(f"GLM package tool {name!r} failed: {exc}")
            turn_record["tool_calls"].append(tool_record)
        tool_feedback = []
        for record in turn_record["tool_calls"]:
            summary = {key: value for key, value in record.items() if key != "arguments"}
            result = summary.get("result")
            if isinstance(result, dict) and isinstance(result.get("content"), str):
                summary["result"] = {**result, "content": f"[read {len(result['content'])} characters; inspect again with read_task_file if needed]"}
            tool_feedback.append(summary)
        files_state = _collect_package_files(tools.package_root, tools.purposes)
        files_state = [{"role": item["role"], "path": item["path"], "purpose": item["purpose"]} for item in files_state]
        turns.append(turn_record)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with transcript_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(turn_record, ensure_ascii=False) + "\n")
        if total_estimated_cost_usd > max_cost_usd + 1e-9:
            raise RuntimeError(
                f"GLM-reported usage exceeded the ${max_cost_usd:.2f} estimated-cost cap "
                f"(${total_estimated_cost_usd:.4f} estimated); stopping before another request"
            )
        if not tool_calls:
            try:
                parsed = _parse_json(latest_text)
                if not isinstance(parsed, dict):
                    raise ValueError("GLM final response must be a JSON object")
                if parsed.get("decision") == "ready":
                    _write_synthetic_manifest(tools.package_root, tools.purposes)
                package_files = _collect_package_files(tools.package_root, tools.purposes)
                _validate_implementation({**parsed, "files": package_files})
                return latest_text, turns
            except (ValueError, json.JSONDecodeError) as exc:
                if repair_cycles >= 1:
                    raise ValueError(f"GLM Stage I response failed validation after one repair cycle: {exc}") from exc
                repair_cycles += 1
                repair_message = f"{type(exc).__name__}: {exc}"
                tool_feedback = [{"validation_error": repair_message}]
                progress(f"deterministic package validation failed; using one repair cycle: {repair_message}")
                continue
    raise RuntimeError(
        f"Stage I reached its {max_turns}-request limit before returning a final response; "
        f"estimated spend is ${total_estimated_cost_usd:.4f}/${max_cost_usd:.2f}"
    )


async def run_stage_i_glm(spec_path: Path, approval_status_path: Path, output_dir: Path) -> Path:
    """Generate a package with GLM and leave execution/verification to Stage J."""
    if output_dir.exists():
        raise FileExistsError(f"Stage I output already exists: {output_dir}")
    spec, approval_status = _validate_approval(spec_path, approval_status_path)
    task_id = spec["task_id"]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", task_id):
        raise ValueError("approved task_id must be lowercase kebab-case")
    output_dir.mkdir(parents=True)
    package_root = output_dir / "task-package"
    package_root.mkdir(parents=True)
    purposes: dict[tuple[str, str], str] = {}
    prompt = _stage_i_prompt(spec, _load_stage_g_review_context(approval_status))
    prompt_path = output_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    progress_path = output_dir / "agent-progress.log"
    transcript_path = output_dir / "agent-transcript.jsonl"

    def record_progress(message: str) -> None:
        line = f"[{datetime.now(timezone.utc).isoformat()}] Stage I {message}"
        print(line, flush=True)
        with progress_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    agent_turns: list[dict[str, Any]] = []
    try:
        final_text, agent_turns = _run_agent(
            prompt, _PackageTools(package_root, purposes, record_progress), transcript_path, record_progress,
        )
        result = json.loads(final_text)
        if not isinstance(result, dict):
            raise ValueError("GLM final response must be a JSON object")
        if result.get("decision") == "ready":
            _write_synthetic_manifest(package_root, purposes)
        files = _collect_package_files(package_root, purposes)
        _validate_implementation({**result, "files": files})
    except Exception as exc:
        current_files = _collect_package_files(package_root, purposes)
        _write_json(output_dir / "agent-error.json", {
            "error_type": type(exc).__name__, "error": str(exc),
            "files_written": [{"role": x["role"], "path": x["path"]} for x in current_files],
        })
        _write_json(output_dir / "status.json", {
            "status": "implementation_failed", "provider": "glm",
            "stage_g_spec": str(spec_path), "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        raise

    _write_json(output_dir / "agent-summary.json", result)
    (output_dir / "agent-final-response.txt").write_text(final_text, encoding="utf-8")
    model = os.environ.get("GLM_MODEL", DEFAULT_MODEL_ID)
    input_rate, output_rate = _price_rates(model)
    total_input_tokens = sum(turn["usage"]["prompt_tokens"] for turn in agent_turns)
    total_output_tokens = sum(turn["usage"]["completion_tokens"] for turn in agent_turns)
    estimated_cost_usd = sum(turn["estimated_cost_usd"] for turn in agent_turns)
    metadata = {
        "stage": "I", "provider": "glm", "model_id": model,
        "api_url": os.environ.get("GLM_API_URL", API_URL),
        "price_source_url": PRICE_SOURCE_URL,
        "max_turns": int(os.environ.get("GLM_STAGE_I_MAX_TURNS", MAX_TURNS)),
        "max_tokens_per_turn": int(os.environ.get("GLM_STAGE_I_MAX_TOKENS_PER_TURN", MAX_TOKENS_PER_TURN)),
        "max_cost_usd": float(os.environ.get("GLM_STAGE_I_MAX_COST_USD", DEFAULT_MAX_COST_USD)),
        "estimated_cost_usd": estimated_cost_usd,
        "cost_estimate_basis": "provider usage tokens at configured per-million rates; all input charged at uncached rate",
        "input_usd_per_million_tokens": input_rate,
        "output_usd_per_million_tokens": output_rate,
        "reported_input_tokens": total_input_tokens,
        "reported_output_tokens": total_output_tokens,
        "agent_turns": agent_turns,
        "tool_access": "TaskForge package-file tools only; Stage J executes and verifies generated code",
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "artifact_type": "initial_task_implementation_unverified",
        "schema_repair_cycles_allowed": 1,
    }
    _write_json(output_dir / "implementation-review.json", {
        "decision": result["decision"], "summary": result["summary"],
        "blocking_questions": result["blocking_questions"],
        "implementation_notes": result["implementation_notes"],
    })
    _write_json(output_dir / "provenance.json", metadata)
    if result["decision"] == "blocked":
        _write_json(output_dir / "status.json", {
            "status": "blocked_for_spec_clarification", "stage_g_spec": str(spec_path),
            "approval_status": str(approval_status_path), "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        return output_dir / "implementation-review.json"

    manifest_files = []
    for item in files:
        relative = _safe_relative_path(item["path"])
        package_path = Path(ROLE_DIRS[item["role"]]).joinpath(*relative.parts)
        manifest_files.append({
            "role": item["role"], "path": package_path.as_posix(), "purpose": item["purpose"],
            "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
        })
    _write_json(package_root / "manifest.json", {
        "task_id": task_id, "title": spec["title"], "category": spec["category"],
        "subcategory": spec["subcategory"], "source_spec": str(spec_path),
        "approval_status": approval_status.get("status"),
        "implementation_status": "generated_unverified", "files": manifest_files,
    })
    _write_json(output_dir / "status.json", {
        "status": "implementation_generated_unverified", "provider": "glm",
        "stage_g_spec": str(spec_path), "approval_status": str(approval_status_path),
        "task_package": str(package_root), "next_stage": "Stage J verification and hardening",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return package_root / "manifest.json"
