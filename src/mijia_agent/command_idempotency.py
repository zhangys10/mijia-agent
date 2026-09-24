"""Process-local idempotency for ``POST /ai/command``.

Port of the console ``lib/ai/security/idempotency.ts`` semantics: replay
completed responses, reject same-key/different-body conflicts, report
processing, TTL expiry. Process-local only — the same soft boundary the
console ``/api/ai/command`` has today. A durable executor claim remains the
gate for real activation documented in docs.
"""

import hashlib
import json
import threading
import time

PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"


def request_hash(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def valid_idempotency_key(value: str | None) -> bool:
    return isinstance(value, str) and 16 <= len(value) <= 128


class _Record:
    __slots__ = ("expires_at", "request_hash", "response", "status")

    def __init__(self, request_hash: str, expires_at: float):
        self.request_hash = request_hash
        self.status = PROCESSING
        self.response: dict | None = None
        self.expires_at = expires_at


class IdempotencyStore:
    def __init__(self, ttl_seconds: float = 600.0):
        self.ttl = ttl_seconds
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    def lookup(self, key: str, body_hash: str) -> str:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return "miss"
            if record.expires_at < time.monotonic():
                del self._records[key]
                return "miss"
            if record.request_hash != body_hash:
                return "conflict"
            return record.status

    def start(self, key: str, body_hash: str) -> None:
        with self._lock:
            self._records[key] = _Record(body_hash, time.monotonic() + self.ttl)

    def complete(self, key: str, response: dict) -> None:
        with self._lock:
            record = self._records.get(key)
            if record:
                record.status = COMPLETED
                record.response = response

    def fail(self, key: str, response: dict) -> None:
        with self._lock:
            record = self._records.get(key)
            if record:
                record.status = FAILED
                record.response = response

    def completed(self, key: str) -> dict | None:
        with self._lock:
            record = self._records.get(key)
            if record and record.status == COMPLETED and record.expires_at >= time.monotonic():
                return record.response
            return None

    def failure(self, key: str) -> dict | None:
        with self._lock:
            record = self._records.get(key)
            if record and record.status == FAILED and record.expires_at >= time.monotonic():
                return record.response
            return None
