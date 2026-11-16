"""Deterministic candidate validation and package writer."""
from __future__ import annotations

import json
from pathlib import Path
from .spec import TaskSpec


class QualityGateError(RuntimeError):
    pass


def validate_candidate(task: TaskSpec) -> dict[str, object]:
    """Require reference checks to pass and the untouched fixture to fail.

    This checks grader/spec consistency before release. It does not measure agent
    difficulty; that requires a configured agent harness and is intentionally a
    separate stage.
    """
    task.validate()
    reference_passes = all(task.reference_files[c.path] == c.equals for c in task.checks)
    if not reference_passes:
        raise QualityGateError("reference solution does not satisfy every check")
    initial_passes = all(task.initial_files.get(c.path) == c.equals for c in task.checks)
    if initial_passes:
        raise QualityGateError("grader accepts the untouched initial state")
    return {
        "status": "auto_releasable_seed",
        "reference_passes": reference_passes,
        "initial_state_rejected": not initial_passes,
        "check_count": len(task.checks),
        "difficulty": "unmeasured",
        "limitations": ["No agent trial panel configured", "No benchmark novelty catalog configured"],
    }


def package_task(task: TaskSpec, output_root: Path) -> Path:
    report = validate_candidate(task)
    destination = output_root / task.task_id
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")
    (destination / "workspace").mkdir(parents=True)
    (destination / "hidden").mkdir()
    for path, content in task.initial_files.items():
        target = destination / "workspace" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (destination / "hidden" / "reference.json").write_text(
        json.dumps(task.reference_files, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (destination / "hidden" / "checks.json").write_text(
        json.dumps([c.__dict__ for c in task.checks], indent=2) + "\n", encoding="utf-8"
    )
    grader = "\"\"\"Hidden deterministic grader.\"\"\"\nimport json\nimport sys\nfrom pathlib import Path\n\nroot = Path(sys.argv[1])\nhidden = Path(__file__).parent\nchecks = json.loads((hidden / \"checks.json\").read_text())\nresults = []\nfor check in checks:\n    path = root / check[\"path\"]\n    actual = path.read_text() if path.is_file() else None\n    passed = actual == check[\"equals\"]\n    results.append({\"path\": check[\"path\"], \"passed\": passed, \"rationale\": check[\"rationale\"]})\nprint(json.dumps({\"passed\": all(r[\"passed\"] for r in results), \"checks\": results}))\nsys.exit(0 if all(r[\"passed\"] for r in results) else 1)\n"
    (destination / "hidden" / "grade.py").write_text(grader, encoding="utf-8")
    manifest = task.to_dict()
    manifest.pop("reference_files")
    manifest.pop("checks")
    manifest["release_report"] = report
    (destination / "task.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (destination / "prompt.txt").write_text(task.prompt + "\n", encoding="utf-8")
    return destination
