"""End-to-end A-G campaign with a bounded restart for author clarification."""
# Reconstructed from preserved bytecode and the corresponding persisted run artifacts.
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .workflow import run_stage_abcd
from .workflow_efg import SpecClarificationRequired, run_stage_efg


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def run_stage_abcdefg(
    seed: int,
    count: int,
    max_topic_draws: int,
    distribution: dict[str, Any],
    output_root: Path,
    mcp_config_path: Path,
    baseline_config_path: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[Path]:
    """Generate specs, restarting once if Stage G requires author clarification."""
    if count < 1:
        raise ValueError("count must be positive")
    output_root.mkdir(parents=True, exist_ok=True)
    implementation_specs: list[Path] = []
    generation = 1
    f_restarts = 0
    while len(implementation_specs) < count:
        generation_root = output_root / f"generation-{generation:04d}"
        if generation_root.exists():
            raise FileExistsError(f"workflow output already exists: {generation_root}")
        generation_root.mkdir(parents=True)
        generation_seed = seed + generation - 1
        if progress:
            progress({"stage": "A-D", "state": "started", "message": f"starting topic and idea workflow for generation {generation}"})
        ideas = await run_stage_abcd(
            generation_seed,
            count - len(implementation_specs),
            max_topic_draws,
            distribution,
            generation_root / "stage-abcd",
            mcp_config_path,
            progress=progress,
        )
        if progress:
            progress({"stage": "A-D", "state": "completed", "message": f"accepted {len(ideas)} idea(s)"})
        generation_status = "completed"
        for idea_path in ideas:
            try:
                spec_path = await run_stage_efg(
                    idea_path,
                    generation_root / "stage-efg",
                    baseline_config_path,
                    progress=progress,
                )
            except SpecClarificationRequired as exc:
                if f_restarts >= 1:
                    generation_status = "failed_after_stage_g_restart_limit"
                    _write_json(generation_root / "generation-status.json", {
                        "status": generation_status,
                        "generation": generation,
                        "seed": generation_seed,
                        "implementation_specs": [str(path) for path in implementation_specs],
                        "stage_g_reason": str(exc),
                        "stage_g_restarts_used": f_restarts,
                        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                    })
                    raise RuntimeError(f"requested task failed after one Stage G-triggered restart; latest reason: {exc}") from exc
                f_restarts += 1
                generation_status = "restarted_after_stage_g"
                break
            implementation_specs.append(spec_path)
            if len(implementation_specs) >= count:
                break
        _write_json(generation_root / "generation-status.json", {
            "status": generation_status,
            "generation": generation,
            "seed": generation_seed,
            "stage_g_restarts_used": f_restarts,
            "implementation_specs": [str(path) for path in implementation_specs],
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        generation += 1
    return implementation_specs
