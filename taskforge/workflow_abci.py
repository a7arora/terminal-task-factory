"""A-G-I campaign: produce a requested number of unverified task packages."""
# Reconstructed from preserved bytecode and the corresponding persisted run artifacts.
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .stage_f import StageFSpecValidationError
from .stage_g import StageGReviewValidationError
from .stage_i import run_stage_i
from .workflow_abcdefg import run_stage_abcdefg


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def run_stage_abci(
    seed: int,
    count: int,
    max_topic_draws: int,
    max_i_candidates: int,
    distribution: dict[str, Any],
    output_root: Path,
    mcp_config_path: Path,
    baseline_config_path: Path | None = None,
) -> list[Path]:
    """Generate fresh Stage G-cleared candidates until Stage I emits `count` packages or cap is hit."""
    if count < 1:
        raise ValueError("count must be positive")
    if max_i_candidates < count:
        raise ValueError("max_i_candidates must be at least count")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"campaign output directory is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    packages: list[Path] = []
    attempts: list[dict[str, Any]] = []
    for candidate_number in range(1, max_i_candidates + 1):
        candidate_root = output_root / f"candidate-{candidate_number:04d}"
        if candidate_root.exists():
            raise FileExistsError(f"candidate output already exists: {candidate_root}")
        candidate_root.mkdir(parents=True)
        candidate_seed = seed + candidate_number - 1
        try:
            spec_paths = await run_stage_abcdefg(
                candidate_seed,
                1,
                max_topic_draws,
                distribution,
                candidate_root / "stage-abcdefg",
                mcp_config_path,
                baseline_config_path,
            )
            spec_path = spec_paths[0]
            approval_status_path = spec_path.parents[1] / "status.json"
            result_path = await run_stage_i(spec_path, approval_status_path, candidate_root / "stage-i")
            result_status = json.loads((candidate_root / "stage-i" / "status.json").read_text(encoding="utf-8"))
        except Exception as exc:
            failure_stage = "stage_f_validation" if isinstance(exc, StageFSpecValidationError) else (
                "stage_g_validation" if isinstance(exc, StageGReviewValidationError) else "generation"
            )
            attempts.append({
                "candidate": candidate_number,
                "seed": candidate_seed,
                "status": "failed",
                "failed_stage": failure_stage,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            _write_json(output_root / "campaign-status.json", {
                "status": "in_progress",
                "requested_packages": count,
                "packages_generated": len(packages),
                "candidate_limit": max_i_candidates,
                "candidates_used": candidate_number,
                "packages": [str(path) for path in packages],
                "attempts": attempts,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            continue

        attempts.append({
            "candidate": candidate_number,
            "seed": candidate_seed,
            "spec": str(spec_path),
            "stage_i_status": result_status["status"],
            "stage_i_result": str(result_path),
        })
        if result_status["status"] == "implementation_generated_unverified":
            packages.append(result_path)
        _write_json(output_root / "campaign-status.json", {
            "status": "completed" if len(packages) >= count else "in_progress",
            "requested_packages": count,
            "packages_generated": len(packages),
            "candidate_limit": max_i_candidates,
            "candidates_used": candidate_number,
            "packages": [str(path) for path in packages],
            "attempts": attempts,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        if len(packages) >= count:
            return packages

    _write_json(output_root / "campaign-status.json", {
        "status": "candidate_limit_reached",
        "requested_packages": count,
        "packages_generated": len(packages),
        "candidate_limit": max_i_candidates,
        "candidates_used": len(attempts),
        "packages": [str(path) for path in packages],
        "attempts": attempts,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return packages
