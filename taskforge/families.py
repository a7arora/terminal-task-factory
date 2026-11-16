"""Procedural seed families. These are original toy tasks, not benchmark copies."""
from __future__ import annotations

from typing import Protocol
import random

from dataclasses import replace
from .spec import Check, TaskSpec


class TaskFamily(Protocol):
    name: str
    def generate(self, seed: int) -> TaskSpec: ...


class RepositoryRepair:
    name = "repository-repair"

    def generate(self, seed: int) -> TaskSpec:
        return TaskSpec(
            task_id=f"repo-repair-{seed:04d}", family=self.name,
            title="Fix stable ordering in the report builder",
            prompt=("Update report.py so rows are sorted by the 'name' field "
                    "case-insensitively, with ties preserving the original order. "
                    "Do not change the input data."),
            capability_tags=("code-editing", "debugging", "stable-sort"),
            initial_files={"report.py": "def sort_rows(rows):\n    return sorted(rows, key=lambda row: row['name'])\n"},
            reference_files={"report.py": "def sort_rows(rows):\n    return sorted(rows, key=lambda row: row['name'].casefold())\n"},
            checks=(Check("report.py", "def sort_rows(rows):\n    return sorted(rows, key=lambda row: row['name'].casefold())\n", "case-insensitive stable ordering"),),
            provenance={"kind": "procedural_seed", "seed": seed},
        )


class DataCli:
    name = "data-cli"

    def generate(self, seed: int) -> TaskSpec:
        return TaskSpec(
            task_id=f"data-cli-{seed:04d}", family=self.name,
            title="Normalize a small inventory export",
            prompt=("Create normalized.csv from inventory.csv. Trim whitespace in "
                    "item names, lowercase names, and retain only rows with quantity > 0. "
                    "Keep the header and original row order."),
            capability_tags=("file-processing", "csv", "data-cleaning"),
            initial_files={"inventory.csv": "item,quantity\n Bolt ,4\nWIDGET,0\n washer ,2\n"},
            reference_files={"normalized.csv": "item,quantity\nbolt,4\nwasher,2\n"},
            checks=(Check("normalized.csv", "item,quantity\nbolt,4\nwasher,2\n", "normalized output rows"),),
            provenance={"kind": "procedural_seed", "seed": seed},
        )


class SafeSystemConfig:
    name = "system-config"

    def generate(self, seed: int) -> TaskSpec:
        return TaskSpec(
            task_id=f"system-config-{seed:04d}", family=self.name,
            title="Correct a service configuration",
            prompt=("Edit service.conf so the service listens on 127.0.0.1:8080, "
                    "writes logs to /var/log/demo/service.log, and keeps debug mode off."),
            capability_tags=("configuration", "requirements-following", "safe-operations"),
            initial_files={"service.conf": "listen=0.0.0.0:80\nlog_file=/tmp/service.log\ndebug=true\n"},
            reference_files={"service.conf": "listen=127.0.0.1:8080\nlog_file=/var/log/demo/service.log\ndebug=false\n"},
            checks=(Check("service.conf", "listen=127.0.0.1:8080\nlog_file=/var/log/demo/service.log\ndebug=false\n", "required service settings"),),
            provenance={"kind": "procedural_seed", "seed": seed},
        )


FAMILIES: tuple[TaskFamily, ...] = (RepositoryRepair(), DataCli(), SafeSystemConfig())


def generate_weighted_batch(
    seed: int,
    count: int,
    weights: dict[str, float] | None = None,
) -> list[TaskSpec]:
    """Sample task families independently according to configured weights.

    Weights are relative probabilities, not percentages; omitted families get
    zero probability when a mapping is supplied. Defaults are only a balanced
    demo mix and are not Terminal-Bench category proportions.
    """
    if count < 1:
        raise ValueError("count must be positive")
    by_name = {family.name: family for family in FAMILIES}
    active_weights = weights if weights is not None else {name: 1.0 for name in by_name}
    unknown = set(active_weights) - set(by_name)
    if unknown:
        raise ValueError(f"unknown task families in weights: {sorted(unknown)}")
    names = [name for name, weight in active_weights.items() if weight > 0]
    if not names or any(weight < 0 for weight in active_weights.values()):
        raise ValueError("weights must include at least one positive value and none may be negative")
    rng = random.Random(seed)
    probabilities = [active_weights[name] for name in names]
    tasks: list[TaskSpec] = []
    for index in range(count):
        family_name = rng.choices(names, weights=probabilities, k=1)[0]
        task = by_name[family_name].generate(seed + index)
        task = replace(
            task,
            task_id=f"{family_name}-{seed:04d}-{index:04d}",
            provenance={**task.provenance, "campaign_seed": seed, "draw_index": index,
                        "family_weight": active_weights[family_name]},
        )
        task.validate()
        tasks.append(task)
    return tasks
