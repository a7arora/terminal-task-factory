"""Hierarchical topic sampling from a pinned benchmark distribution."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TopicDraw:
    category: str
    subcategory: str
    category_probability: float
    conditional_subcategory_probability: float
    joint_probability: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "subcategory": self.subcategory,
            "category_probability": self.category_probability,
            "conditional_subcategory_probability": self.conditional_subcategory_probability,
            "joint_probability": self.joint_probability,
        }


def load_distribution(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    categories = data["categories"]
    if not categories:
        raise ValueError("distribution has no categories")
    if sum(item["count"] for item in categories.values()) != data["task_count"]:
        raise ValueError("category counts do not sum to task_count")
    for name, item in categories.items():
        if item["count"] <= 0 or not item["subcategories"]:
            raise ValueError(f"invalid category: {name}")
        if sum(item["subcategories"].values()) != item["count"]:
            raise ValueError(f"subcategory counts do not sum to category count: {name}")
    return data


def draw_topic(rng: random.Random, distribution: dict[str, Any]) -> TopicDraw:
    """Draw category then subcategory, independently for each task."""
    categories = distribution["categories"]
    names = list(categories)
    counts = [categories[name]["count"] for name in names]
    total = sum(counts)
    category = rng.choices(names, weights=counts, k=1)[0]
    category_probability = categories[category]["count"] / total

    subcategories = categories[category]["subcategories"]
    sub_names = list(subcategories)
    sub_counts = [subcategories[name] for name in sub_names]
    sub_total = sum(sub_counts)
    subcategory = rng.choices(sub_names, weights=sub_counts, k=1)[0]
    conditional_probability = subcategories[subcategory] / sub_total
    return TopicDraw(
        category=category,
        subcategory=subcategory,
        category_probability=category_probability,
        conditional_subcategory_probability=conditional_probability,
        joint_probability=category_probability * conditional_probability,
    )


def draw_topic_batch(seed: int, count: int, distribution: dict[str, Any]) -> list[TopicDraw]:
    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    return [draw_topic(rng, distribution) for _ in range(count)]
