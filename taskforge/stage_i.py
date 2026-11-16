"""Stage I provider dispatcher. Claude Code on GLM is default; direct providers remain available."""
from __future__ import annotations

import os
from pathlib import Path

from .stage_i_claude import ROLE_DIRS


async def run_stage_i(
    spec_path: Path,
    approval_status_path: Path,
    output_dir: Path,
    *,
    provider: str | None = None,
) -> Path:
    selected = (provider or os.environ.get("TASKFORGE_STAGE_I_PROVIDER", "claude-code-glm")).strip().lower()
    if selected == "glm":
        from .stage_i_glm import run_stage_i_glm

        return await run_stage_i_glm(spec_path, approval_status_path, output_dir)
    if selected in {"claude", "claude-code-glm"}:
        from .stage_i_claude import run_stage_i as run_stage_i_claude

        return await run_stage_i_claude(spec_path, approval_status_path, output_dir, provider=selected)
    raise ValueError("Stage I provider must be 'claude-code-glm', 'glm', or 'claude'")
