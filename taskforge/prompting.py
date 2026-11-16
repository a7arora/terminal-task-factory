"""Prompt rendering for idea generation and a separate novelty judge."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .distribution import TopicDraw

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def render_idea_prompt(
    topic: TopicDraw,
    revision_feedback: str | None = None,
    prior_idea: dict[str, Any] | None = None,
    reference_tasks: list[dict[str, Any]] | None = None,
    difficulty_retry_summary: str | None = None,
    repository_policy: dict[str, Any] | None = None,
) -> str:
    template = (PROMPTS_DIR / "task_idea_generator.md").read_text(encoding="utf-8")
    if difficulty_retry_summary:
        revision = {
            "type": "difficulty_hardening",
            "instruction": (
                "The prior task passed all validity and attribution checks but was too easy: "
                "the screening agent completed it within the deadline and passed deterministic checks. "
                "Generate a materially harder replacement."
            ),
            "previous_task_summary": difficulty_retry_summary,
            "constraint": "Keep the same sampled category and subcategory; all requirements remain feasible and deterministically checkable.",
        }
        revision_context = "Difficulty-hardening context included in this initial idea prompt:\n" + json.dumps(
            revision, indent=2, ensure_ascii=False
        )
    elif revision_feedback:
        revision = {
            "type": "novelty_revision",
            "instruction": "Revise the previous idea to address the abstract novelty feedback.",
            "previous_idea": prior_idea or {},
            "feedback": revision_feedback,
            "constraint": "Keep the same sampled category and subcategory.",
        }
        revision_context = "Revision context (the reference MCP browsing tools remain available for the assigned topic; novelty feedback is abstract and contains no source task text):\n" + json.dumps(
            revision, indent=2, ensure_ascii=False
        )
    else:
        revision_context = "This is the first proposal for this sampled topic."
    if reference_tasks is None:
        reference_json = "Full task descriptions are fetched only when you request specific task IDs through the reference MCP tools."
    else:
        reference_json = json.dumps(reference_tasks, indent=2, ensure_ascii=False)
    policy = repository_policy or {
        "minimum_stars": 200,
        "preferred_stars": 500,
        "approved_permissive_licenses": ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD"],
    }
    return (
        template.replace("{{CATEGORY}}", topic.category)
        .replace("{{SUBCATEGORY}}", topic.subcategory)
        .replace("{{REFERENCE_TASKS_JSON}}", reference_json)
        .replace("{{REPOSITORY_POLICY_JSON}}", json.dumps(policy, indent=2, ensure_ascii=False))
        .replace("{{REVISION_CONTEXT}}", revision_context)
    )


def render_novelty_prompt(
    topic: TopicDraw,
    idea: dict[str, Any],
    reference_tasks: list[dict[str, Any]],
) -> str:
    for task in reference_tasks:
        if task.get("category") != topic.category:
            raise ValueError(
                f"reference {task.get('task_id', '<unknown>')} is not in category {topic.category}"
            )
        if task.get("subcategory") != topic.subcategory:
            raise ValueError(
                f"reference {task.get('task_id', '<unknown>')} is not in subcategory {topic.subcategory}"
            )
        if not task.get("task_id") or not task.get("description"):
            raise ValueError("each reference needs task_id and description fields")
    template = (PROMPTS_DIR / "novelty_judge.md").read_text(encoding="utf-8")
    return (
        template.replace("{{CATEGORY}}", topic.category)
        .replace("{{SUBCATEGORY}}", topic.subcategory)
        .replace("{{CANDIDATE_IDEA_JSON}}", json.dumps(idea, indent=2, ensure_ascii=False))
        .replace("{{REFERENCE_TASKS_JSON}}", json.dumps(reference_tasks, indent=2, ensure_ascii=False))
    )
