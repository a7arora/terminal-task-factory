"""Provider-agnostic retry loop between an idea generator and novelty judge."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .distribution import TopicDraw


@dataclass(frozen=True)
class CandidateAttempt:
    attempt: int
    idea: dict[str, Any]
    review: dict[str, Any]


@dataclass(frozen=True)
class RetryResult:
    accepted: bool
    topic: dict[str, Any]
    attempts: tuple[CandidateAttempt, ...]


def generate_until_novel(
    topic: TopicDraw,
    generate_idea: Callable[[TopicDraw, str | None, dict[str, Any] | None], dict[str, Any]],
    judge_novelty: Callable[[TopicDraw, dict[str, Any]], dict[str, Any]],
    max_attempts: int = 3,
) -> RetryResult:
    """Retry an idea with judge feedback while keeping the original topic draw."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    history: list[CandidateAttempt] = []
    feedback: str | None = None
    previous_idea: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        idea = generate_idea(topic, feedback, previous_idea)
        review = judge_novelty(topic, idea)
        decision = review.get("decision")
        if decision not in {"accept", "retry"}:
            raise ValueError("novelty judge decision must be 'accept' or 'retry'")
        history.append(CandidateAttempt(attempt, idea, review))
        if decision == "accept":
            return RetryResult(True, topic.to_dict(), tuple(history))
        feedback = review.get("revision_feedback", "")
        if not feedback:
            raise ValueError("retry decision must include abstract revision_feedback")
        previous_idea = idea
    return RetryResult(False, topic.to_dict(), tuple(history))
