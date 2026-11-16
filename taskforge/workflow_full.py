"""Sequential A-G-I-K question campaign with live progress and a CSV index."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .stage_i import run_stage_i
from .stage_k import run_stage_k
from .workflow_abcdefg import run_stage_abcdefg

CSV_FIELDS = [
    "candidate_id", "question_id", "seed", "task_id", "title", "category", "subcategory",
    "failed_stage", "stage_i_status", "stage_k_status", "glm_solved", "benchmark_retained",
    "unresolved_rubric_task_defects", "task_directory", "stage_k_report", "error",
]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _write_candidate_summary(candidate_root: Path, row: dict[str, Any]) -> None:
    _write_json(candidate_root / "candidate-summary.json", row)


def _status_snapshot(path: Path, *, status: str, requested: int, retained: int,
                     attempts: int, candidate_limit: int, rows: list[dict[str, Any]], current: dict[str, Any] | None = None) -> None:
    _write_json(path, {
        "status": status,
        "requested_questions": requested,
        "questions_retained": retained,
        "candidate_attempts": attempts,
        "candidate_limit": candidate_limit,
        "current_candidate": current,
        "results_csv": str(path.parent / "results.csv"),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidates": rows,
    })


async def run_stage_full(
    seed: int,
    count: int,
    max_topic_draws: int,
    max_candidates: int,
    distribution: dict[str, Any],
    output_root: Path,
    mcp_config_path: Path,
    baseline_config_path: Path | None = None,
    lineage_memory_path: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Create up to count retained questions, evaluating each candidate before the next begins."""
    if count < 1:
        raise ValueError("count must be positive")
    if max_candidates < count:
        raise ValueError("max_candidates must be at least count")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"full-run output directory is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    csv_path = output_root / "results.csv"
    status_path = output_root / "campaign-status.json"
    lineage_path = lineage_memory_path or output_root / "lineage-memory.json"
    rows: list[dict[str, Any]] = []
    retained_count = 0
    stopped_on_runtime_error = False

    for candidate_number in range(1, max_candidates + 1):
        candidate_id = f"candidate-{candidate_number:04d}"
        question_id = f"question-{retained_count + 1:04d}"
        candidate_root = output_root / candidate_id
        candidate_root.mkdir()
        candidate_seed = seed + candidate_number - 1
        current: dict[str, Any] = {"candidate_id": candidate_id, "seed": candidate_seed, "stage": "A"}
        row: dict[str, Any] = {
            "candidate_id": candidate_id, "question_id": "", "seed": candidate_seed,
            "task_directory": str(candidate_root.resolve()), "benchmark_retained": False,
            "unresolved_rubric_task_defects": "", "error": "",
        }

        def scoped(event: dict[str, Any]) -> None:
            current.update({key: event.get(key) for key in ("stage", "state", "message") if key in event})
            if progress:
                progress({**event, "candidate": candidate_number, "candidate_id": candidate_id,
                          "completed_count": retained_count})
            _status_snapshot(status_path, status="running", requested=count, retained=retained_count,
                             attempts=candidate_number, candidate_limit=max_candidates, rows=rows,
                             current={**current, "candidate_id": candidate_id, "seed": candidate_seed})

        scoped({"stage": "A-G", "state": "started", "message": f"starting full generation for {candidate_id}"})
        stage_label = "A-G"
        try:
            specs = await run_stage_abcdefg(
                candidate_seed, 1, max_topic_draws, distribution,
                candidate_root / "stage-abcdefg", mcp_config_path,
                baseline_config_path, progress=scoped,
            )
            if not specs:
                raise RuntimeError("A-G workflow returned no cleared task specification")
            spec_path = specs[0]
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            scoped({"stage": "I", "state": "started", "message": "Stage I is implementing the package; the final revised spec is shown first",
                    "artifact": str(spec_path), "artifact_label": "Specification before implementation", "artifact_preview": spec})
            approval_status = spec_path.parents[1] / "status.json"
            stage_label = "I"
            manifest_path = await run_stage_i(spec_path, approval_status, candidate_root / "stage-i")
            stage_i_status = json.loads((candidate_root / "stage-i" / "status.json").read_text(encoding="utf-8"))
            row["stage_i_status"] = stage_i_status.get("status", "unknown")
            row.update({key: spec.get(key, "") for key in ("title", "category", "subcategory")})
            if stage_i_status.get("status") != "implementation_generated_unverified":
                row["error"] = "Stage I did not produce a ready package"
                rows.append(row)
                _write_candidate_summary(candidate_root, row)
                _write_csv(csv_path, rows)
                scoped({"stage": "I", "state": "failed", "message": row["error"],
                        "artifact": str(manifest_path)})
                continue

            package_manifest = json.loads((candidate_root / "stage-i" / "task-package" / "manifest.json").read_text(encoding="utf-8"))
            row["task_id"] = package_manifest.get("task_id", "")
            scoped({"stage": "I", "state": "completed", "message": "task package generated; moving to Stage J/K"})
            stage_label = "J/K"
            stage_k_output = candidate_root / "stage-k"
            report_path = run_stage_k(candidate_root / "stage-i", stage_k_output, lineage_path, progress=scoped)
            stage_k_report = json.loads(report_path.read_text(encoding="utf-8"))
            row.update({
                "stage_k_status": stage_k_report.get("status", "unknown"),
                "glm_solved": stage_k_report.get("glm_solved", ""),
                "benchmark_retained": stage_k_report.get("benchmark_retained", False),
                "stage_k_report": str(report_path.resolve()),
                "unresolved_rubric_task_defects": stage_k_report.get("unresolved_defect_count", ""),
            })
            if row["benchmark_retained"]:
                row["question_id"] = question_id
                retained_count += 1
            rows.append(row)
            _write_candidate_summary(candidate_root, row)
            _write_csv(csv_path, rows)
            scoped({"stage": "candidate", "state": "completed" if row["benchmark_retained"] else "rejected",
                    "message": f"{candidate_id}: {stage_k_report.get('status')}; glm_solved={row['glm_solved']}; retained={row['benchmark_retained']}",
                    "artifact": str(report_path)})
        except Exception as exc:
            row["failed_stage"] = current.get("stage", stage_label)
            row["stage_k_status"] = "failed" if stage_label == "J/K" else "not_reached"
            row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            _write_candidate_summary(candidate_root, row)
            _write_csv(csv_path, rows)
            scoped({"stage": stage_label, "state": "failed", "message": row["error"]})
            # Generation failures can be skipped like the existing bounded candidate campaign.
            # A Stage K runtime failure is likely credentials/infrastructure, so pause the batch.
            if stage_label == "J/K":
                stopped_on_runtime_error = True
                break

        _status_snapshot(status_path, status="running", requested=count, retained=retained_count,
                         attempts=candidate_number, candidate_limit=max_candidates, rows=rows,
                         current={"candidate_id": candidate_id, "seed": candidate_seed, "stage": "candidate-complete"})
        if retained_count >= count:
            break

    campaign_status = "completed" if retained_count >= count else "failed" if stopped_on_runtime_error else "candidate_limit_reached"
    _status_snapshot(status_path, status=campaign_status, requested=count, retained=retained_count,
                     attempts=len(rows), candidate_limit=max_candidates, rows=rows)
    _write_json(output_root / "campaign-status.json", _read_json(status_path))
    if progress:
        progress({"stage": "campaign", "state": "campaign_completed" if campaign_status == "completed" else "campaign_failed",
                  "completed_count": retained_count, "message": f"run {campaign_status}: retained {retained_count}/{count} question(s)",
                  "artifact": str(csv_path)})
    return csv_path
