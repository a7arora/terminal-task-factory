"""Stages E-G: repository preflight, specification drafting, and review."""
# Reconstructed from preserved bytecode and the corresponding persisted run artifacts.
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .stage_e import run_stage_e
from .stage_f import StageFSpecValidationError, expand_task_spec
from .stage_g import StageGReviewValidationError, review_and_revise_spec

MAX_SPEC_REVIEWERS = 3


class SpecClarificationRequired(RuntimeError):
    """Raised when the reviewer needs author clarification."""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_attempts(directory: Path, attempts: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in attempts:
        number = attempt["attempt"]
        (directory / f"attempt-{number}-response.txt").write_text(attempt["response_text"], encoding="utf-8")
        error = attempt.get("validation_error")
        if error:
            (directory / f"attempt-{number}-validation-error.txt").write_text(error + "\n", encoding="utf-8")


async def run_stage_efg(
    idea_path: Path,
    output_root: Path,
    baseline_config_path: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Run repository preflight, draft a spec, then review and revise it."""
    idea = json.loads(idea_path.read_text(encoding="utf-8"))
    if not isinstance(idea, dict):
        raise ValueError("accepted idea file must contain a JSON object")
    for key in ("title", "category", "subcategory", "objective", "repository_selection"):
        if key not in idea:
            raise ValueError(f"accepted idea is missing {key!r}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("set ANTHROPIC_API_KEY before running Stage F")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError("Stage F model support requires `pip install -e '.[stage-b]'`") from exc

    client = Anthropic()
    slug = re.sub(r"[^a-z0-9]+", "-", str(idea["title"]).lower()).strip("-")[:70].strip("-")
    if not slug:
        raise ValueError("idea title cannot produce a task output directory")
    run_dir = output_root / slug
    if run_dir.exists():
        raise FileExistsError(f"Stage E-G output already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    if progress:
        progress({"stage": "E", "state": "started", "message": "checking the pinned repository baseline"})
    report = run_stage_e(idea, run_dir / "stage-e", baseline_config_path)
    if progress:
        progress({"stage": "E", "state": "completed" if report.get("passed") else "failed",
                  "message": report.get("summary", "repository preflight completed"),
                  "artifact": str(run_dir / "stage-e" / "preflight.json")})
    if not report.get("passed"):
        _write_json(run_dir / "status.json", {
            "status": "stopped_at_stage_e",
            "reason": report.get("summary", "Repository baseline did not pass."),
            "stage_e_report": str(run_dir / "stage-e" / "preflight.json"),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        raise RuntimeError(f"Stage E did not pass ({report.get('status')}); see {run_dir / 'stage-e' / 'preflight.json'}")

    stage_f_dir = run_dir / "stage-f"
    if progress:
        progress({"stage": "F", "state": "started", "message": "drafting the task specification"})
    try:
        spec, metadata, attempts = await asyncio.to_thread(expand_task_spec, client, idea, report)
    except StageFSpecValidationError as exc:
        _write_attempts(stage_f_dir, exc.attempts)
        exc.set_artifact_dir(stage_f_dir)
        _write_json(stage_f_dir / "validation-failure.json", {
            "status": "failed_after_one_schema_repair_retry",
            "reason": str(exc),
            "attempts": [{key: item.get(key) for key in ("attempt", "prompt_sha256", "validation_error", "usage")} for item in exc.attempts],
        })
        _write_json(run_dir / "status.json", {
            "status": "stage_f_validation_failed",
            "reason": str(exc),
            "stage_e_report": str(run_dir / "stage-e" / "preflight.json"),
            "stage_f_failure": str(stage_f_dir / "validation-failure.json"),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        raise
    _write_attempts(stage_f_dir, attempts)
    _write_json(stage_f_dir / "task-spec.json", spec)
    _write_json(stage_f_dir / "provenance.json", metadata)
    if progress:
        progress({"stage": "F", "state": "completed", "message": "spec drafted; showing it before reviewer rounds",
                  "artifact": str(stage_f_dir / "task-spec.json"), "artifact_label": "Stage F specification",
                  "artifact_preview": spec})

    current_spec = spec
    review_rounds: list[dict[str, Any]] = []
    approved = False
    for round_number in range(1, MAX_SPEC_REVIEWERS + 1):
        round_dir = run_dir / "stage-g" / f"reviewer-{round_number}"
        if progress:
            progress({"stage": "G", "state": "started", "message": f"independent reviewer {round_number}/{MAX_SPEC_REVIEWERS} is reviewing the spec"})
        try:
            revised, review, review_metadata, review_attempts = await asyncio.to_thread(
                review_and_revise_spec, client, idea, report, current_spec,
            )
        except StageGReviewValidationError as exc:
            _write_attempts(round_dir, exc.attempts)
            exc.set_artifact_dir(round_dir)
            _write_json(round_dir / "validation-failure.json", {
                "status": "failed_after_one_schema_repair_retry",
                "reason": str(exc),
                "attempts": [{key: item.get(key) for key in ("attempt", "prompt_sha256", "validation_error", "usage", "stop_reason")} for item in exc.attempts],
            })
            _write_json(run_dir / "status.json", {
                "status": "stage_g_validation_failed",
                "reason": str(exc),
                "stage_e_report": str(run_dir / "stage-e" / "preflight.json"),
                "stage_f_draft": str(stage_f_dir / "task-spec.json"),
                "stage_g_failure": str(round_dir / "validation-failure.json"),
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            raise
        _write_attempts(round_dir, review_attempts)
        _write_json(round_dir / "task-spec.json", revised)
        _write_json(round_dir / "review.json", review)
        _write_json(round_dir / "provenance.json", review_metadata)
        if progress:
            progress({"stage": "G", "state": "completed", "message": f"reviewer {round_number} decision: {review['decision']}",
                      "artifact": str(round_dir / "review.json")})
        review_rounds.append({"reviewer": round_number, **review})
        current_spec = revised
        if review["decision"] == "clarification_required":
            _write_json(run_dir / "stage-g" / "review.json", {
                "status": "clarification_required",
                "maximum_reviewers": MAX_SPEC_REVIEWERS,
                "reviewers_used": len(review_rounds),
                "rounds": review_rounds,
            })
            _write_json(run_dir / "status.json", {
                "status": "generation_restart_required",
                "reason": "Stage G requires author clarification about the core task intent.",
                "stage_g_review": str(run_dir / "stage-g" / "review.json"),
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            raise SpecClarificationRequired("Stage G requires author clarification; this generation must restart.")
        if review["decision"] == "approved":
            approved = True
            break

    _write_json(run_dir / "stage-g" / "revised-task-spec.json", current_spec)
    review_summary = {
        "status": "approved" if approved else "proceeding_after_review_limit",
        "maximum_reviewers": MAX_SPEC_REVIEWERS,
        "reviewers_used": len(review_rounds),
        "rounds": review_rounds,
    }
    _write_json(run_dir / "stage-g" / "review.json", review_summary)
    _write_json(run_dir / "status.json", {
        "status": "spec_approved_by_reviser_loop" if approved else "spec_proceeding_after_review_limit",
        "review_limit_reached": not approved,
        "review_limit_decision": None if approved else review_rounds[-1]["decision"],
        "stage_g_reviewers_used": len(review_rounds),
        "stage_e_report": str(run_dir / "stage-e" / "preflight.json"),
        "stage_f_draft": str(stage_f_dir / "task-spec.json"),
        "stage_g_review": str(run_dir / "stage-g" / "review.json"),
        "task_spec": str(run_dir / "stage-g" / "revised-task-spec.json"),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    if progress:
        progress({"stage": "G", "state": "accepted" if approved else "completed",
                  "message": "Stage G complete; final revised spec is ready for Stage I",
                  "artifact": str(run_dir / "stage-g" / "revised-task-spec.json"),
                  "artifact_label": "Final Stage G specification", "artifact_preview": current_spec})
    return run_dir / "stage-g" / "revised-task-spec.json"
