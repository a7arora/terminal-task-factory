"""Terminal and on-disk progress reporting for long task-factory runs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProgressReporter:
    def __init__(self, output_dir: Path, *, total: int, show_artifacts: bool = True) -> None:
        self.output_dir = output_dir.resolve()
        self.total = total
        self.show_artifacts = show_artifacts
        self.events_path = self.output_dir / "progress.jsonl"
        self.status_path = self.output_dir / "progress-status.json"
        self.events: list[dict[str, Any]] = []

    def __call__(self, value: dict[str, Any]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), **value}
        event["sequence"] = len(self.events) + 1
        self.events.append(event)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()

        completed = [
            {"candidate": item.get("candidate"), "stage": item.get("stage"), "message": item.get("message"), "artifact": item.get("artifact")}
            for item in self.events if item.get("state") in {"completed", "accepted", "rejected", "skipped", "failed"}
        ]
        snapshot = {
            "status": "running" if event.get("state") not in {"campaign_completed", "campaign_failed"} else event["state"],
            "requested_count": self.total,
            "current": event,
            "completed_steps": completed,
            "events_file": str(self.events_path),
            "updated_at_utc": event["timestamp"],
        }
        temp = self.status_path.with_suffix(".tmp")
        temp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(self.status_path)

        candidate = event.get("candidate")
        if candidate is None:
            candidate_text = f"batch {event.get('completed_count', 0)}/{self.total}"
        else:
            candidate_text = f"candidate {candidate}; retained {event.get('completed_count', 0)}/{self.total}"
        state = str(event.get("state", "update")).upper().replace("_", " ")
        stage = event.get("stage", "workflow")
        message = event.get("message", "")
        print(f"[{candidate_text}] {stage} — {state}: {message}", flush=True)
        artifact = event.get("artifact")
        if self.show_artifacts and artifact:
            print(f"--- {event.get('artifact_label', stage + ' artifact')}: {artifact} ---", flush=True)
            content = event.get("artifact_preview")
            if content is None:
                try:
                    path = Path(artifact)
                    content = path.read_text(encoding="utf-8") if path.is_file() else "(artifact path is not a file)"
                except (OSError, UnicodeDecodeError):
                    content = "(artifact saved; not displayable as UTF-8 text)"
            if not isinstance(content, str):
                content = json.dumps(content, indent=2, ensure_ascii=False)
            print(content[:16000] + ("\n...[display truncated; full artifact saved]" if len(content) > 16000 else ""), flush=True)
            print(f"--- end {event.get('artifact_label', stage + ' artifact')} ---", flush=True)
