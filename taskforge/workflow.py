"""Topic selection (A) and idea generation/editor/novelty (B-D) campaign."""
from __future__ import annotations

import asyncio
import json
import os
import re
import random
from pathlib import Path
from typing import Any, Callable

from .distribution import draw_topic, TopicDraw
from .mcp_runtime import MCPRuntime, load_mcp_config
from .stage_b import run_stage_b
from .stage_c import review_idea
from .stage_d import review_novelty


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def run_stage_abcd(
    seed: int,
    count: int,
    max_topic_draws: int,
    distribution: dict[str, Any],
    output_dir: Path,
    mcp_config_path: Path,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[Path]:
    if count < 1:
        raise ValueError("count must be positive")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("set ANTHROPIC_API_KEY before running the A-D workflow")
    from anthropic import Anthropic

    client = Anthropic()
    config = load_mcp_config(str(mcp_config_path))
    rng = random.Random(seed)
    accepted: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for task_index in range(1, count + 1):
        task_dir = output_dir / f"task-{task_index:04d}"
        task_dir.mkdir(parents=True, exist_ok=True)
        accepted_for_task = False
        for draw_index in range(1, min(max_topic_draws, 2) + 1):
            topic = draw_topic(rng, distribution)
            slug = re.sub(r"[^a-z0-9]+", "-", f"{topic.category}-{topic.subcategory}".lower()).strip("-")
            draw_dir = task_dir / f"draw-{draw_index:02d}-{slug}"
            if draw_dir.exists():
                raise FileExistsError(f"workflow output already exists: {draw_dir}")
            draw_dir.mkdir(parents=True)
            _write_json(draw_dir / "topic.json", {"seed": seed, "task_index": task_index, "draw_index": draw_index, "topic": topic.to_dict()})
            if progress:
                progress({"candidate": task_index, "stage": "A", "state": "completed",
                    "message": f"drew {topic.category} / {topic.subcategory}", "artifact": str(draw_dir / "topic.json"),
                    "artifact_label": "Topic draw", "artifact_preview": topic.to_dict(), "draw": draw_index})
            generator_feedback: str | None = None
            editor_feedback: str | None = None
            previous_idea: dict[str, Any] | None = None
            for idea_attempt in range(1, 3):
                attempt_dir = draw_dir / f"idea-attempt-{idea_attempt}"
                if progress:
                    progress({"candidate": task_index, "stage": "B", "state": "started",
                        "message": f"generating idea with MCP evidence (topic draw {draw_index}, attempt {idea_attempt})", "draw": draw_index})
                generated = await run_stage_b(
                    [topic], seed, attempt_dir / "stage-b", mcp_config_path,
                    revision_feedback=generator_feedback, prior_idea=previous_idea,
                    draw_index_start=draw_index,
                )
                raw_idea = json.loads(generated[0].read_text(encoding="utf-8"))
                if progress:
                    progress({"candidate": task_index, "stage": "B", "state": "completed",
                        "message": "idea generated; showing it before editor review", "artifact": str(generated[0]),
                        "artifact_label": "Generated idea", "artifact_preview": raw_idea, "draw": draw_index})
                    progress({"candidate": task_index, "stage": "C", "state": "started",
                        "message": "editor review is running; the generated idea is shown above", "draw": draw_index})
                editor = review_idea(client, topic, raw_idea, editor_feedback)
                _write_json(attempt_dir / "editor-review.json", editor)
                if editor["decision"] == "restart":
                    if progress:
                        progress({"candidate": task_index, "stage": "C", "state": "rejected",
                            "message": "editor requested an idea restart", "artifact": str(attempt_dir / "editor-review.json"),
                            "draw": draw_index})
                    generator_feedback = editor["restart_feedback"]
                    editor_feedback = generator_feedback
                    previous_idea = raw_idea
                    continue
                idea = editor["edited_idea"]
                _write_json(attempt_dir / "idea-after-editor.json", idea)
                if progress:
                    progress({"candidate": task_index, "stage": "C", "state": "completed",
                        "message": "editor accepted/edited the idea; beginning novelty review", "artifact": str(attempt_dir / "idea-after-editor.json"),
                        "artifact_label": "Idea after editor", "artifact_preview": idea, "draw": draw_index})
                    progress({"candidate": task_index, "stage": "D", "state": "started",
                        "message": "checking novelty against the same-subcategory reference tasks", "draw": draw_index})
                async with MCPRuntime(config) as runtime:
                    novelty = await review_novelty(client, runtime, topic, idea)
                _write_json(attempt_dir / "novelty-review.json", novelty)
                if progress:
                    progress({"candidate": task_index, "stage": "D", "state": "accepted" if novelty["decision"] == "accept" else "rejected",
                        "message": f"novelty review decision: {novelty['decision']}", "artifact": str(attempt_dir / "novelty-review.json"),
                        "draw": draw_index})
                if novelty["decision"] == "accept":
                    candidate_path = draw_dir / "accepted-idea.json"
                    _write_json(candidate_path, idea)
                    _write_json(draw_dir / "draw-status.json", {"status": "accepted", "idea_attempt": idea_attempt})
                    accepted.append(candidate_path)
                    accepted_for_task = True
                    break
                generator_feedback = novelty["revision_feedback"]
                editor_feedback = None
                previous_idea = idea
            if accepted_for_task:
                break
            _write_json(draw_dir / "draw-status.json", {
                "status": "discarded_after_two_ideas",
                "reason": "Both idea attempts were restarted by the editor or rejected by the novelty judge.",
            })
        if not accepted_for_task:
            raise RuntimeError(
                f"task {task_index} produced no accepted idea after {min(max_topic_draws, 2)} topic draw(s) "
                "and two idea attempts per draw"
            )
    return accepted
