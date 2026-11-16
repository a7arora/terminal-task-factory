"""Stage K: solve, score, reflect, attribute, and repair a Stage I task package."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .glm_adapter import DockerSolverSandbox, GLMToolAgent
from .claude_code_solver import ClaudeCodeSolverAgent
from .stage_j import run_stage_j

CLAUDE_MODEL = "claude-sonnet-5-5"
MAX_VERIFICATION_ROUNDS = 4
DEFAULT_GLM_SOFT_TARGET_USD = 1.50
DEFAULT_GLM_HARD_LIMIT_USD = 1.50
DEFAULT_GLM_MAX_TURNS = 32


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_from_text(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _anthropic() -> Any:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("set ANTHROPIC_API_KEY before running Stage K Claude roles")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError("install the Anthropic SDK with: python -m pip install -e '.[stage-b]'") from exc
    return Anthropic()


def _prompt(name: str) -> str:
    path = Path(__file__).resolve().parents[1] / "prompts" / name
    return path.read_text(encoding="utf-8")


def _claude_json(client: Any, prompt: str, *, max_tokens: int = 16000) -> Any:
    response = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=max_tokens,
        system="Follow the supplied role exactly. Return only valid JSON.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = "\n".join(getattr(block, "text", "") for block in response.content if getattr(block, "type", "") == "text")
    return _json_from_text(text)


def _replace(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{([A-Z0-9_]+)\}", lambda match: values.get(match.group(1), match.group(0)), template)


def _read_text_files(root: Path, *, limit: int = 60_000) -> list[dict[str, str]]:
    found = []
    remaining = limit
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in {".txt", ".md", ".json", ".csv", ".yaml", ".yml", ".py", ".sh", ".toml", ".ini"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        excerpt = content[:remaining]
        found.append({"path": path.relative_to(root).as_posix(), "content": excerpt})
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    return found


def _compact(value: Any, string_limit: int = 6000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit] + "\n...[truncated in model review context]"
    if isinstance(value, list):
        return [_compact(item, string_limit) for item in value]
    if isinstance(value, dict):
        return {key: _compact(item, string_limit) for key, item in value.items()}
    return value


def _solver_trace_for_review(path: Path) -> str:
    trace = _read_json(path)
    if not isinstance(trace, list):
        return json.dumps(trace, ensure_ascii=False)
    # Include every event while clipping large command output; the unabridged trace remains on disk.
    compacted = []
    for event in trace:
        if event.get("type") == "tool":
            result = event.get("result", {})
            compacted.append({
                "type": "tool", "name": event.get("name"),
                "arguments": _compact(event.get("arguments", {}), 200),
                "result": {key: _compact(result.get(key), 300) for key in ("returncode", "timed_out", "stdout", "stderr", "error", "accepted", "status") if key in result},
            })
        else:
            calls = event.get("tool_calls", [])
            compacted.append({
                "type": event.get("type"), "call_index": event.get("call_index"),
                "content": _compact(event.get("content"), 500),
                "tool_calls": [{"id": call.get("id"), "name": (call.get("function") or {}).get("name"),
                                "arguments": _compact((call.get("function") or {}).get("arguments"), 250)} for call in calls],
                "finish_reason": event.get("finish_reason"),
            })
    return json.dumps(compacted, ensure_ascii=False)


def _package_evidence(package: Path) -> dict[str, Any]:
    task_prompt = package / "workspace" / "task" / "task_prompt.md"
    if not task_prompt.is_file():
        # Accept the alternate single-file convention and make the failure explicit otherwise.
        candidates = list((package / "workspace").glob("**/task_prompt.md"))
        if len(candidates) == 1:
            task_prompt = candidates[0]
        else:
            raise ValueError("Stage K requires workspace/task/task_prompt.md in the Stage I task package")
    rubric_path = package / "authoring" / "rubric.json"
    rubric = _read_json(rubric_path)
    _validate_rubric(rubric)
    synthetic_root = package / "workspace" / "synthetic"
    manifest_path = synthetic_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("Stage K requires workspace/synthetic/manifest.json")
    synthetic_manifest = _read_json(manifest_path)
    if not isinstance(synthetic_manifest, dict) or set(synthetic_manifest) != {"version", "files"} or synthetic_manifest.get("version") != 1 or not isinstance(synthetic_manifest.get("files"), list):
        raise ValueError("synthetic manifest must have version 1 and a files array")
    declared: dict[str, str] = {}
    for item in synthetic_manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("synthetic manifest entries must contain exactly path and sha256")
        relative, digest = item["path"], item["sha256"]
        if not isinstance(relative, str) or not relative or relative in declared or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("synthetic manifest contains an invalid or duplicate path/hash")
        target = (synthetic_root / relative).resolve()
        if not target.is_relative_to(synthetic_root.resolve()) or not target.is_file() or _sha(target) != digest:
            raise ValueError(f"synthetic manifest hash/path verification failed for {relative!r}")
        declared[relative] = digest
    actual = {p.relative_to(synthetic_root).as_posix(): _sha(p) for p in synthetic_root.rglob("*") if p.is_file() and p != manifest_path}
    if declared != actual:
        raise ValueError("synthetic manifest must cover exactly the companion files present")
    workspace_files = _read_text_files(package / "workspace")
    parsed_paths = {item["path"] for item in workspace_files}
    workspace_paths = [p.relative_to(package / "workspace").as_posix()
                       for p in sorted((package / "workspace").rglob("*"))
                       if p.is_file() and not p.is_symlink() and not p.relative_to(package / "workspace").as_posix().startswith("deliverable/")]
    for relative in workspace_paths:
        if relative not in parsed_paths:
            workspace_files.append({"path": relative, "content": "[binary or unsupported format; path is available to the solver but Stage K did not parse its contents]"})
    return {
        "task_prompt": task_prompt.read_text(encoding="utf-8"),
        "rubric": rubric,
        "workspace_files": workspace_files,
        "workspace_paths": workspace_paths,
        "private": {
            "reference_solution": _read_text_files(package / "reference"),
            "grader": _read_text_files(package / "grader"),
            "authoring": _read_text_files(package / "authoring"),
        },
    }


def _repair_call(client: Any, prompt: str, sandbox: DockerSolverSandbox, out: Path,
                 progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Give the repair agent bounded code_exec access to the working scenario."""
    sandbox.start()
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    log: list[dict[str, Any]] = []
    deadline = time.monotonic() + 2 * 60 * 60
    try:
        for _ in range(200):
            if time.monotonic() >= deadline:
                raise TimeoutError("Stage 6 repair exceeded the two-hour operational limit")
            if progress:
                progress({"stage": "K/repair", "state": "working", "message": "waiting for Claude Stage 6 repair response"})
            response = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=16000,
                system="You are the Stage 6 coding benchmark repair agent. Use code_exec to inspect and make the required minimal edits. Return the required JSON summary when finished.",
                messages=messages,
                tools=[{"name": "code_exec", "description": "Run one bounded shell command in the isolated repair workspace; files persist, shell state does not.",
                        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"], "additionalProperties": False}}],
            )
            blocks = list(response.content)
            calls = [b for b in blocks if getattr(b, "type", "") == "tool_use"]
            if not calls:
                text = "\n".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")
                result = _json_from_text(text)
                _write_json(out / "repair-agent-transcript.json", log)
                return result
            messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]})
            tool_results = []
            for call in calls:
                cmd = call.input.get("cmd", "")
                if progress:
                    progress({"stage": "K/repair", "state": "working", "message": "running Stage 6 code_exec in the isolated repair workspace"})
                value = sandbox.execute(cmd, 600)
                log.append({"tool": "code_exec", "cmd": cmd, "result": value})
                if progress:
                    progress({"stage": "K/repair", "state": "working", "message": f"Stage 6 command returned code {value.get('returncode')}"})
                tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                     "content": json.dumps(value, ensure_ascii=False)})
            messages.append({"role": "user", "content": tool_results})
        raise RuntimeError("Stage 6 repair exceeded the 200-call limit")
    finally:
        stopped = sandbox.stop()
        _write_json(out / "repair-agent-transcript.json", {"calls": log, "sandbox_stop": stopped})


def _validate_score(report: Any, rubric: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(report, dict) or not isinstance(report.get("rubric_scores"), list):
        raise ValueError("Claude scorer response must contain rubric_scores array")
    scores = report["rubric_scores"]
    if len(scores) != len(rubric):
        raise ValueError("Claude scorer must return exactly one result per rubric item")
    for index, (entry, item) in enumerate(zip(scores, rubric)):
        if entry.get("rubric_item_index") != index or entry.get("score") != item["score"] or entry.get("criterion") != item["criterion"]:
            raise ValueError(f"Claude scorer did not echo rubric item {index} correctly")
        award = entry.get("awarded_score")
        if not isinstance(award, int) or isinstance(award, bool):
            raise ValueError(f"Claude scorer award for item {index} must be an integer")
        if item["score"] > 0 and not 0 <= award <= item["score"]:
            raise ValueError(f"Claude scorer award for positive item {index} is out of range")
        if item["score"] < 0 and not item["score"] <= award <= 0:
            raise ValueError(f"Claude scorer penalty award for item {index} is out of range")
        entry["satisfied"] = award == item["score"] if item["score"] > 0 else award == 0
    return scores


def _validate_rubric(rubric: Any) -> None:
    if not isinstance(rubric, list) or not rubric:
        raise ValueError("Stage 6 must leave a non-empty ordered rubric array")
    for index, item in enumerate(rubric):
        if not isinstance(item, dict) or set(item) != {"score", "criterion", "required", "tags"}:
            raise ValueError(f"repaired rubric item {index} has an invalid schema")
        if not isinstance(item["score"], int) or isinstance(item["score"], bool) or item["score"] == 0:
            raise ValueError(f"repaired rubric item {index} needs a non-zero integer score")
        if not isinstance(item["criterion"], str) or not item["criterion"].strip():
            raise ValueError(f"repaired rubric item {index} needs criterion text")
        if not isinstance(item["required"], bool) or not isinstance(item["tags"], list):
            raise ValueError(f"repaired rubric item {index} has invalid required/tags fields")
        if any(not isinstance(tag, str) or not tag.strip() for tag in item["tags"]):
            raise ValueError(f"repaired rubric item {index} tags must be non-empty strings")


def _validate_attribution(result: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(result, dict) or not isinstance(result.get("attributions"), list):
        raise ValueError("Stage K attribution response must contain an attributions array")
    entries = result["attributions"]
    if len(entries) != len(findings):
        raise ValueError("Stage K attribution must return one judgment for every finding")
    allowed = {"Agent", "Rubric", "Task"}
    for index, (entry, finding) in enumerate(zip(entries, findings)):
        if not isinstance(entry, dict) or entry.get("finding_id") != finding["finding_id"]:
            raise ValueError(f"Stage K attribution entry {index} must preserve its finding_id")
        if entry.get("score") != finding.get("score") or entry.get("awarded_score") != finding.get("awarded_score"):
            raise ValueError(f"Stage K attribution entry {index} changed its score")
        labels = entry.get("labels")
        if not isinstance(labels, list) or not labels or any(label not in allowed for label in labels):
            raise ValueError(f"Stage K attribution entry {index} has invalid cause labels")
        if labels != [label for label in ("Agent", "Rubric", "Task") if label in labels]:
            raise ValueError(f"Stage K attribution entry {index} labels must be in canonical order")
        if entry.get("primary_cause") not in labels:
            raise ValueError(f"Stage K attribution entry {index} primary_cause must be one of its labels")
    return entries


def run_stage_k(stage_i_output: Path, output_dir: Path | None = None, lineage_memory: Path | None = None,
                progress: Callable[[dict[str, Any]], None] | None = None) -> Path:
    """Execute one task through at most four solve/score/reflect/attribute rounds."""
    stage_i_output = stage_i_output.resolve()
    output_dir = (output_dir or stage_i_output / "stage-k").resolve()
    if output_dir.exists():
        raise FileExistsError(f"Stage K output already exists: {output_dir}")
    package = stage_i_output / "task-package"
    status = _read_json(stage_i_output / "status.json")
    if status.get("status") != "implementation_generated_unverified":
        raise ValueError("Stage K requires a completed Stage I output")
    output_dir.mkdir(parents=True)
    client = _anthropic()
    report: dict[str, Any] = {"stage": "K", "started_at_utc": datetime.now(timezone.utc).isoformat(),
                              "stage_i_output": str(stage_i_output), "status": "failed", "rounds": []}
    working_i = output_dir / "working-stage-i"
    shutil.copytree(stage_i_output, working_i, ignore=shutil.ignore_patterns("stage-j", "stage-k"))
    working_package = working_i / "task-package"
    lineage_memory = (lineage_memory or output_dir / "lineage-memory.json").resolve()
    lineage_memory.parent.mkdir(parents=True, exist_ok=True)
    existing_lineage = _read_json(lineage_memory) if lineage_memory.exists() else {"lineage_entries": []}
    lineage_entries = list(existing_lineage.get("lineage_entries", []))
    anchor_metadata_path = working_package / "workspace" / "anchors" / "metadata.json"
    anchor_metadata = _read_json(anchor_metadata_path) if anchor_metadata_path.exists() else {"anchors": []}
    task_id = _read_json(working_package / "manifest.json").get("task_id", "task")
    final_disposition = "discarded"
    glm_spend_usd = 0.0
    glm_turns_used = 0
    repair_round = 0
    last_rubric_pass = False
    last_repairable_count = 0
    try:
        while True:
            round_dir = output_dir / f"round-{repair_round + 1}"
            round_dir.mkdir()
            deliverable_dir = working_package / "workspace" / "deliverable"
            if deliverable_dir.exists():
                shutil.rmtree(deliverable_dir)
            deliverable_dir.mkdir(parents=True)
            if progress:
                progress({"stage": "J", "state": "started", "message": f"Stage J verification round {repair_round + 1} is starting"})
            j_report_path = run_stage_j(working_i, round_dir / "stage-j", progress=progress)
            j_report = _read_json(j_report_path)
            if not j_report.get("image_export", {}).get("archive"):
                raise RuntimeError(f"Stage J did not export a runnable image: {j_report.get('summary', j_report.get('error'))}")
            evidence = _package_evidence(working_package)
            workspace = working_package / "workspace"
            solver_tool_list = "- code_exec(cmd): run a shell command in /workspace (10-minute command limit).\n- finish(summary, files): submit existing absolute paths under /workspace/deliverable/.\n- abandon(reason): report a truly blocking missing input or incoherent task."
            refs = "\n".join(f"- /workspace/{x['path']}" for x in evidence["workspace_files"] if x["path"] != "task/task_prompt.md")
            if not refs:
                refs = "- No additional solver-visible reference files."
            solver_prompt = _replace(_prompt("stage_k_solver.md"), {
                "REFERENCE_FILES": refs, "SOLVER_TOOLS": solver_tool_list, "TASK_PROMPT": evidence["task_prompt"]
            })
            sandbox = DockerSolverSandbox(j_report["image_build"]["runtime_image"]["tag"],
                Path(j_report["image_export"]["archive"]), workspace, round_dir / "glm", j_report.get("sandbox_policy", {}))
            target_usd = float(os.environ.get("GLM_STAGE_K_TARGET_COST_USD", DEFAULT_GLM_SOFT_TARGET_USD))
            hard_limit_usd = float(os.environ.get("GLM_STAGE_K_MAX_COST_USD", DEFAULT_GLM_HARD_LIMIT_USD))
            if target_usd <= 0 or hard_limit_usd < target_usd:
                raise ValueError("Stage K GLM hard cost limit must be at least the positive soft target")
            remaining_budget_usd = hard_limit_usd - glm_spend_usd
            max_turns = max(1, min(64, int(os.environ.get("GLM_STAGE_K_MAX_TURNS", DEFAULT_GLM_MAX_TURNS))))
            remaining_turns = max_turns - glm_turns_used
            if glm_spend_usd >= target_usd or remaining_budget_usd <= 0 or remaining_turns <= 0:
                stop_reason = f"${target_usd:.2f} GLM soft spend target" if glm_spend_usd >= target_usd else f"${hard_limit_usd:.2f} GLM hard limit" if remaining_budget_usd <= 0 else f"{max_turns} agent-turn limit"
                glm = {"status": "failed", "model": "glm-5.3-flash", "error": f"Stage K reached its {stop_reason}", "estimated_glm_cost_usd": 0.0}
            else:
                solver_provider = os.environ.get("TASKFORGE_STAGE_K_SOLVER", "claude-code-glm").strip().lower()
                if progress:
                    progress({"stage": "K/GLM", "state": "started", "message": f"GLM solve started; up to 8 hours for round {repair_round + 1}; estimated GLM budget remaining ${remaining_budget_usd:.4f}"})
                if solver_provider == "glm":
                    glm = GLMToolAgent(
                        round_dir / "glm", sandbox, max_cost_usd=remaining_budget_usd,
                        max_api_turns=remaining_turns, progress=progress,
                    ).run(
                        "You are an independent coding task solver. Follow the task and use only solver-visible files/tools.", solver_prompt)
                elif solver_provider == "claude-code-glm":
                    glm = ClaudeCodeSolverAgent(
                        round_dir / "glm", sandbox, timeout_seconds=8 * 60 * 60,
                        max_budget_usd=remaining_budget_usd, max_turns=remaining_turns, progress=progress,
                    ).run("You are an independent coding task solver. Follow the task and use only solver-visible files/tools.", solver_prompt)
                else:
                    raise ValueError("TASKFORGE_STAGE_K_SOLVER must be 'claude-code-glm' or 'glm'")
                request_cost = glm.get("estimated_glm_cost_usd")
                if isinstance(request_cost, (int, float)) and request_cost >= 0:
                    glm_spend_usd += float(request_cost)
                turns = (glm.get("agent_result") or {}).get("num_turns")
                if isinstance(turns, int) and turns > 0:
                    glm_turns_used += turns
                elif isinstance(glm.get("api_turns"), int):
                    glm_turns_used += glm["api_turns"]
                elif isinstance(glm.get("tool_calls"), int):
                    glm_turns_used += glm["tool_calls"]
                glm["estimated_cumulative_glm_cost_usd"] = glm_spend_usd
                glm["cumulative_agent_turns"] = glm_turns_used
                glm["agent_turn_limit"] = max_turns
                glm["glm_cost_target_usd"] = target_usd
                glm["glm_hard_cost_limit_usd"] = hard_limit_usd
            _write_json(round_dir / "glm" / "result.json", glm)
            if progress:
                progress({"stage": "K/GLM", "state": "completed" if glm["status"] == "solved" else "failed",
                          "message": f"GLM status={glm['status']}; elapsed {glm.get('elapsed_seconds', 0):.1f}s; {glm.get('tool_calls', 0)} tool calls",
                          "artifact": str(round_dir / "glm" / "result.json")})
            if glm["status"] != "solved":
                last_rubric_pass = False
                report["rounds"].append({"round": repair_round + 1, "stage_j": str(j_report_path), "glm": glm})
                final_disposition = "unsolved"
                break
            deliverable_files = []
            for path in glm["finish"]["files"]:
                p = Path(path)
                raw = p.read_bytes()
                relative = str(p.relative_to(workspace))
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = f"[binary deliverable; {len(raw)} bytes; sha256={hashlib.sha256(raw).hexdigest()}; Stage K text scorer cannot parse this format]"
                deliverable_files.append({"path": relative, "content": content[:60_000]})
            scoring_prompt = _replace(_prompt("stage_k_scorer.md"), {
                "TASK_PROMPT": evidence["task_prompt"], "RUBRIC": json.dumps(evidence["rubric"], ensure_ascii=False),
                "REFERENCE_FILES_PARSED": json.dumps(evidence["workspace_files"], ensure_ascii=False),
                "REFERENCE_DELIVERABLE_PARSED": json.dumps(deliverable_files, ensure_ascii=False),
                "ARTIFACT_CHECK_REPORT": json.dumps(_compact(j_report), ensure_ascii=False),
            })
            if progress:
                progress({"stage": "K/score", "state": "started", "message": "Claude is scoring the deliverable item by item against the rubric"})
            scoring = _claude_json(client, scoring_prompt)
            scores = _validate_score(scoring, evidence["rubric"])
            last_rubric_pass = all(item["satisfied"] for item in scores)
            _write_json(round_dir / "scoring.json", scoring)
            if progress:
                progress({"stage": "K/score", "state": "completed", "message": f"rubric pass={last_rubric_pass}",
                          "artifact": str(round_dir / "scoring.json")})
            failures = [{"finding_id": f"score-{i}", "rubric_item_index": i, "criterion": x["criterion"],
                         "score": x["score"], "awarded_score": x["awarded_score"], "rationale": x["rationale"]}
                        for i, x in enumerate(scores) if not x["satisfied"]]
            if all(item["satisfied"] for item in scores):
                reflector_prompt = _replace(_prompt("stage_k_reflector.md"), {
                    "TASK_PROMPT": evidence["task_prompt"], "REFERENCE_FILES_PARSED": json.dumps(evidence["workspace_files"], ensure_ascii=False),
                    "RUBRIC": json.dumps(evidence["rubric"], ensure_ascii=False), "SCORING_REPORT": json.dumps(scoring, ensure_ascii=False),
                    "SOLVER_TRACE": _solver_trace_for_review(round_dir / "glm" / "tool-transcript.json"),
                    "REFERENCE_DELIVERABLE_PARSED": json.dumps(deliverable_files, ensure_ascii=False),
                    "STAGE_J_REPORT": json.dumps(_compact(j_report), ensure_ascii=False),
                    "PRIVATE_PACKAGE_EVIDENCE": json.dumps(evidence["private"], ensure_ascii=False),
                })
                if progress:
                    progress({"stage": "K/reflect", "state": "started", "message": "Claude is auditing the full-credit result for rubric/task loopholes"})
                reflector = _claude_json(client, reflector_prompt)
                if not isinstance(reflector, dict) or not isinstance(reflector.get("flags"), list):
                    raise ValueError("Stage K reflector response must contain a flags array")
            else:
                reflector = {"flags": [], "summary": "Skipped because the GLM deliverable did not receive full rubric credit."}
            if progress:
                progress({"stage": "K/reflect", "state": "completed",
                          "message": f"reflector flagged {len(reflector['flags'])} possible issue(s)" if last_rubric_pass else "skipped: rubric score was not full credit",
                          "artifact": str(round_dir / "reflector.json")})
            for index, flag in enumerate(reflector["flags"]):
                if not isinstance(flag, dict) or not isinstance(flag.get("finding_id"), str) or not flag["finding_id"].strip():
                    raise ValueError(f"Stage K reflector flag {index} needs a finding_id")
                rubric_index = flag.get("rubric_item_index")
                if rubric_index is None:
                    if flag.get("score") is not None or flag.get("awarded_score") is not None:
                        raise ValueError("reflector flags without rubric coverage must use null score fields")
                elif not isinstance(rubric_index, int) or isinstance(rubric_index, bool) or not 0 <= rubric_index < len(scores):
                    raise ValueError(f"Stage K reflector flag {index} has an invalid rubric_item_index")
                elif flag.get("score") != scores[rubric_index]["score"] or flag.get("awarded_score") != scores[rubric_index]["awarded_score"]:
                    raise ValueError(f"Stage K reflector flag {index} changed its rubric scores")
                if not isinstance(flag.get("evidence"), list) or not flag["evidence"]:
                    raise ValueError(f"Stage K reflector flag {index} needs evidence")
            _write_json(round_dir / "reflector.json", reflector)
            findings = failures + [{"finding_id": x.get("finding_id", f"reflector-{i+1}"),
                          "rubric_item_index": x.get("rubric_item_index"), "criterion": x.get("criterion"),
                          "score": x.get("score"), "awarded_score": x.get("awarded_score"), "rationale": x.get("concern"),
                          "evidence": x.get("evidence")} for i, x in enumerate(reflector["flags"])]
            if not findings:
                final_disposition = "accepted"
                report["rounds"].append({"round": repair_round + 1, "stage_j": str(j_report_path), "glm": glm,
                    "scoring": str(round_dir / "scoring.json"), "reflector": str(round_dir / "reflector.json"), "findings": []})
                break
            attribution_prompt = _replace(_prompt("stage_k_attribution.md"), {
                "TASK_PROMPT": evidence["task_prompt"], "REFERENCE_FILES_PARSED": json.dumps(evidence["workspace_files"], ensure_ascii=False),
                "REFERENCE_DELIVERABLE_PARSED": json.dumps(deliverable_files, ensure_ascii=False),
                "RUBRIC": json.dumps(evidence["rubric"], ensure_ascii=False), "FINDINGS": json.dumps(findings, ensure_ascii=False),
                "SOLVER_TRACE": _solver_trace_for_review(round_dir / "glm" / "tool-transcript.json"),
                "STAGE_J_REPORT": json.dumps(_compact(j_report), ensure_ascii=False), "ROUND_INDEX": str(repair_round + 1),
            })
            if progress:
                progress({"stage": "K/attribute", "state": "started", "message": f"Claude is attributing {len(findings)} score/reflector finding(s)"})
            attribution = _claude_json(client, attribution_prompt)
            entries = _validate_attribution(attribution, findings)
            _write_json(round_dir / "attribution.json", attribution)
            repairable = [x for x in entries if any(label in x.get("labels", []) for label in ("Rubric", "Task"))]
            last_repairable_count = len(repairable)
            if progress:
                progress({"stage": "K/attribute", "state": "completed",
                          "message": f"{len(repairable)} finding(s) have a Rubric or Task label",
                          "artifact": str(round_dir / "attribution.json")})
            report["rounds"].append({"round": repair_round + 1, "stage_j": str(j_report_path), "glm": glm,
                "scoring": str(round_dir / "scoring.json"), "reflector": str(round_dir / "reflector.json"),
                "attribution": str(round_dir / "attribution.json"), "finding_count": len(findings),
                "repairable_count": len(repairable)})
            if not repairable:
                final_disposition = "accepted_with_agent_failures"
                break
            if repair_round + 1 >= MAX_VERIFICATION_ROUNDS:
                final_disposition = "discarded"
                report["discard_reason"] = "Rubric/Task defects remain after four verification rounds"
                break
            scenario_dir = round_dir / "repair-workspace"
            scenario_dir.mkdir()
            task_dir = scenario_dir / "task"
            synthetic_dir = scenario_dir / "synthetic"
            (task_dir / "task_prompt.md").parent.mkdir(parents=True, exist_ok=True)
            (task_dir / "task_prompt.md").write_text(evidence["task_prompt"], encoding="utf-8")
            (task_dir / "rubric.json").write_text(json.dumps(evidence["rubric"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if (workspace / "synthetic").exists(): shutil.copytree(workspace / "synthetic", synthetic_dir)
            if (workspace / "anchors").exists(): shutil.copytree(workspace / "anchors", scenario_dir / "anchors")
            repair_lineage = scenario_dir / "lineage-memory.json"
            _write_json(repair_lineage, {"lineage_entries": lineage_entries})
            anchor_before = {p.relative_to(scenario_dir / "anchors").as_posix(): _sha(p) for p in (scenario_dir / "anchors").rglob("*") if p.is_file()}
            repair_prompt = _replace(_prompt("stage_k_repair.md"), {
                "TASK_PROMPT": evidence["task_prompt"], "REFERENCE_FILES_PARSED": json.dumps(evidence["workspace_files"], ensure_ascii=False),
                "RUBRIC": json.dumps(evidence["rubric"], ensure_ascii=False), "REFERENCE_DELIVERABLE_PARSED": json.dumps(deliverable_files, ensure_ascii=False),
                "ATTRIBUTIONS": json.dumps(attribution, ensure_ascii=False), "STAGE_J_REPORT": json.dumps(_compact(j_report), ensure_ascii=False),
                "ROUND_INDEX": str(repair_round + 1), "LINEAGE_MEMORY_FILE": "/workspace/lineage-memory.json",
                "PRIVATE_PACKAGE_EVIDENCE": json.dumps(evidence["private"], ensure_ascii=False),
            })
            repair_sandbox = DockerSolverSandbox(j_report["image_build"]["runtime_image"]["tag"],
                Path(j_report["image_export"]["archive"]), scenario_dir, round_dir / "repair", j_report.get("sandbox_policy", {}))
            if progress:
                progress({"stage": "K/repair", "state": "started", "message": f"Stage 6 repair for verification round {repair_round + 1}"})
            repair_result = _repair_call(client, repair_prompt, repair_sandbox, round_dir / "repair", progress=progress)
            if repair_result.get("status") not in {"accepted", "updated", "discarded"}:
                raise ValueError("Stage 6 returned an invalid repair status")
            repair_log = scenario_dir / "verification" / f"round_{repair_round + 1}" / "repair_log.json"
            if not repair_log.is_file():
                raise ValueError("Stage 6 did not write its required repair_log.json")
            repair_log_data = _read_json(repair_log)
            if repair_log_data.get("status") != repair_result.get("status"):
                raise ValueError("Stage 6 response status does not match repair_log.json")
            after_hashes = {p.relative_to(scenario_dir / "anchors").as_posix(): _sha(p) for p in (scenario_dir / "anchors").rglob("*") if p.is_file()}
            if anchor_before != after_hashes:
                raise RuntimeError("Stage 6 attempted to modify immutable anchors; repair rejected")
            if any(path.is_symlink() for path in scenario_dir.rglob("*")):
                raise RuntimeError("Stage 6 created a symlink in its workspace; repair rejected")
            prompt_path, rubric_path = task_dir / "task_prompt.md", task_dir / "rubric.json"
            updated_rubric = _read_json(rubric_path)
            _validate_rubric(updated_rubric)
            shutil.copy2(prompt_path, workspace / "task" / "task_prompt.md")
            shutil.copy2(rubric_path, working_package / "authoring" / "rubric.json")
            package_synthetic = workspace / "synthetic"
            if package_synthetic.exists(): shutil.rmtree(package_synthetic)
            if synthetic_dir.exists(): shutil.copytree(synthetic_dir, package_synthetic)
            manifest_path = package_synthetic / "manifest.json"
            if not manifest_path.is_file():
                raise ValueError("Stage 6 removed required synthetic/manifest.json")
            manifest = _read_json(manifest_path)
            digests = {p.relative_to(package_synthetic).as_posix(): _sha(p)
                       for p in sorted(package_synthetic.rglob("*")) if p.is_file() and p != manifest_path}
            if not isinstance(manifest, dict) or set(manifest) != {"version", "files"} or manifest.get("version") != 1:
                raise ValueError("Stage 6 synthetic manifest must use the version 1 schema")
            manifest["files"] = [{"path": name, "sha256": digest} for name, digest in digests.items()]
            _write_json(manifest_path, manifest)
            _write_json(round_dir / "repair-result.json", repair_result)
            if progress:
                progress({"stage": "K/repair", "state": repair_result["status"], "message": f"Stage 6 returned {repair_result['status']}",
                          "artifact": str(repair_log)})
            if repair_result["status"] == "discarded":
                final_disposition = "discarded"
                report["discard_reason"] = repair_result.get("notes_for_next_pass", "Stage 6 determined the scenario cannot be repaired safely.")
                break
            repair_round += 1
        report["status"] = final_disposition
        report["glm_cost_target_usd"] = float(os.environ.get("GLM_STAGE_K_TARGET_COST_USD", DEFAULT_GLM_SOFT_TARGET_USD))
        report["glm_hard_cost_limit_usd"] = float(os.environ.get("GLM_STAGE_K_MAX_COST_USD", DEFAULT_GLM_HARD_LIMIT_USD))
        report["estimated_glm_cost_usd"] = glm_spend_usd
        report["glm_agent_turns"] = glm_turns_used
        report["glm_agent_turn_limit"] = int(os.environ.get("GLM_STAGE_K_MAX_TURNS", DEFAULT_GLM_MAX_TURNS))
        report["glm_solved"] = last_rubric_pass
        report["accepted"] = final_disposition == "accepted"
        report["glm_submitted"] = any(r.get("glm", {}).get("status") == "solved" for r in report["rounds"])
        report["benchmark_retained"] = final_disposition != "discarded"
        report["unresolved_defect_count"] = last_repairable_count if final_disposition == "discarded" else 0
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        lineage_entries.append({"task_id": task_id, "outcome": final_disposition,
            "anchors": anchor_metadata.get("anchors", []), "stage_k_output": str(output_dir)})
        _write_json(lineage_memory, {"lineage_entries": lineage_entries})
        _write_json(output_dir / "report.json", report)
        _write_json(output_dir / "status.json", {"status": final_disposition, "glm_solved": report["glm_solved"],
            "benchmark_retained": report["benchmark_retained"], "report": str((output_dir / "report.json").resolve())})
        if progress:
            progress({"stage": "K", "state": "completed", "message": f"final status={final_disposition}; glm_solved={report['glm_solved']}; retained={report['benchmark_retained']}",
                      "artifact": str(output_dir / "report.json")})
        return output_dir / "report.json"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(output_dir / "report.json", report)
        _write_json(output_dir / "status.json", {"status": "failed", "error": report["error"],
            "report": str((output_dir / "report.json").resolve())})
        if not any(isinstance(item, dict) and item.get("stage_k_output") == str(output_dir) for item in lineage_entries):
            lineage_entries.append({"task_id": task_id, "outcome": "failed", "anchors": anchor_metadata.get("anchors", []),
                                    "stage_k_output": str(output_dir), "error": report["error"]})
            _write_json(lineage_memory, {"lineage_entries": lineage_entries})
        raise
