from datetime import datetime, timezone
from pathlib import Path
import json
import re
import time
from .schemas import canonical, digest, plain


def safe_id(task_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", task_id) + "-" + digest(task_id)[:8]


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


class EpisodeLog:
    def __init__(self, root, run_id, task_id):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id, self.task_id = run_id, task_id
        self.started = time.monotonic()
        self.seq = 0

    def write(self, relative, value):
        write_json(self.root / relative, value)
        return {"ref": relative, "sha256": digest(value)}

    def write_text(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return {"ref": relative, "sha256": digest(text)}

    def event(self, event_type, state=None, **payload):
        self.seq += 1
        event = {
            "schema_version": "1", "run_id": self.run_id, "task_id": self.task_id,
            "event_seq": self.seq, "event_type": event_type,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "monotonic_elapsed_ms": (time.monotonic() - self.started) * 1000,
            "worker_step": state.budget.worker_dispatches_used if state else 0,
            "decision_index": state.budget.router_decisions_used if state else 0,
            "state_hash": digest(state) if state else None, "payload": payload,
        }
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(canonical(event) + "\n")
            handle.flush()
        return event
