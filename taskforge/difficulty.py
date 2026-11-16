"""Aggregate valid agent trials into per-task difficulty estimates."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Iterable


VALID_OUTCOMES = {"solved", "agent_failure"}
EXCLUDED_OUTCOMES = {"environment_error", "grader_error", "harness_error", "cancelled"}


@dataclass(frozen=True)
class TrialRecord:
    task_id: str
    category: str
    model_id: str
    agent_id: str
    harness_id: str
    panel_id: str
    repeat_index: int
    outcome: str

    def validate(self) -> None:
        required = (self.task_id, self.category, self.model_id, self.agent_id, self.harness_id, self.panel_id)
        if any(not field for field in required):
            raise ValueError("trial identity fields must be non-empty")
        if self.repeat_index < 0:
            raise ValueError("repeat_index must be non-negative")
        if self.outcome not in VALID_OUTCOMES | EXCLUDED_OUTCOMES:
            raise ValueError(f"unknown outcome: {self.outcome}")


@dataclass(frozen=True)
class TaskPassRate:
    task_id: str
    category: str
    panel_id: str
    model_id: str
    agent_id: str
    harness_id: str
    successes: int
    valid_trials: int
    excluded_trials: int
    pass_rate: float
    wilson_95_low: float
    wilson_95_high: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if trials == 0:
        return (0.0, 1.0)
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    margin = z * ((p * (1 - p) / trials + z * z / (4 * trials * trials)) ** 0.5) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def aggregate_pass_rates(records: Iterable[TrialRecord]) -> list[TaskPassRate]:
    """Return per-task pass rates within a single fixed panel.

    Infrastructure and grader failures are excluded from the difficulty estimate
    and reported separately. Rates are kept separate for each model, agent, and
    harness so panel composition does not erase model-specific behavior.
    """
    rows = list(records)
    for row in rows:
        row.validate()
    panels = {row.panel_id for row in rows}
    if len(panels) > 1:
        raise ValueError("aggregate one fixed panel at a time")
    trial_keys = [(row.task_id, row.model_id, row.agent_id, row.harness_id, row.repeat_index) for row in rows]
    if len(trial_keys) != len(set(trial_keys)):
        raise ValueError("duplicate repeat_index for a task/model/agent/harness trial")

    grouped: dict[tuple[str, str, str, str, str, str], list[TrialRecord]] = defaultdict(list)
    for row in rows:
        grouped[(row.task_id, row.category, row.panel_id, row.model_id, row.agent_id, row.harness_id)].append(row)

    output: list[TaskPassRate] = []
    for (task_id, category, panel_id, model_id, agent_id, harness_id), task_rows in sorted(grouped.items()):
        valid = [row for row in task_rows if row.outcome in VALID_OUTCOMES]
        successes = sum(row.outcome == "solved" for row in valid)
        low, high = _wilson_interval(successes, len(valid))
        output.append(TaskPassRate(
            task_id=task_id,
            category=category,
            panel_id=panel_id,
            model_id=model_id,
            agent_id=agent_id,
            harness_id=harness_id,
            successes=successes,
            valid_trials=len(valid),
            excluded_trials=len(task_rows) - len(valid),
            pass_rate=successes / len(valid) if valid else float("nan"),
            wilson_95_low=low,
            wilson_95_high=high,
        ))
    return output


def pass_rate_distribution(rates: Iterable[TaskPassRate]) -> dict[str, dict[str, list[float]]]:
    """Return per-task pass-rate distributions by category and model."""
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rates:
        if row.valid_trials:
            grouped[(row.category, row.model_id)].append(row.pass_rate)
    result: dict[str, dict[str, list[float]]] = defaultdict(dict)
    for (category, model_id), values in sorted(grouped.items()):
        result[category][model_id] = sorted(values)
    return dict(result)
