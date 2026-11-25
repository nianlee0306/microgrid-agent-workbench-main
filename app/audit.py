"""Local structured audit and Agent-scoped history."""

import json
from pathlib import Path
from threading import Lock


class Audit:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Lock()

    def write(self, event: dict) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")

    def read(self, session_id: str, agent_id: str | None = None, limit: int = 30) -> list[dict]:
        with self.lock:
            if not self.path.is_file():
                return []
            with self.path.open(encoding="utf-8") as stream:
                rows = []
                for line in stream:
                    event = json.loads(line)
                    if event.get("event") != "review" or event.get("session_id") != session_id:
                        continue
                    if agent_id is None or event.get("agent_id") == agent_id:
                        rows.append(event)
                return rows[-limit:]
