"""Core data structures for generated terminal tasks."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Check:
    path: str
    equals: str
    rationale: str


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    family: str
    title: str
    prompt: str
    capability_tags: tuple[str, ...]
    initial_files: dict[str, str]
    reference_files: dict[str, str]
    checks: tuple[Check, ...]
    difficulty: str = "unrated"
    provenance: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.task_id or any(c in self.task_id for c in "/\\ .."):
            raise ValueError(f"unsafe task_id: {self.task_id!r}")
        if not self.family or not self.prompt.strip():
            raise ValueError("family and prompt are required")
        for group in (self.initial_files, self.reference_files):
            for path in group:
                if path.startswith("/") or ".." in path.split("/"):
                    raise ValueError(f"unsafe task path: {path!r}")
        if not self.checks:
            raise ValueError("at least one deterministic check is required")
        for check in self.checks:
            if check.path not in self.reference_files:
                raise ValueError(f"check path missing from reference files: {check.path}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
