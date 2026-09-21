"""JSONL logging for every model call, for later debugging.

One line per LLM call: the request payload (messages, tools, params — aliases
only by construction), a bounded response excerpt, usage, and latency; failures
log the error code. Gateway credentials are transport headers and never enter
payloads; bindings, tokens, and Xiaomi secrets never reach the gateway layer,
so they can never reach these records. Logging failures must never break a turn.
"""

import json
import sys
import threading
from datetime import datetime, timezone


class LlmCallLogger:
    def __init__(self, path: str | None = None, sink=None):
        self._path = path
        self._sink = sink or sys.stdout
        self._lock = threading.Lock()

    def log(self, record: dict) -> None:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock:
                if self._path:
                    with open(self._path, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                else:
                    self._sink.write(line + "\n")
                    self._sink.flush()
        except (OSError, ValueError):
            pass  # A broken log sink must not fail the turn.
