"""Structured request tracing.

WHAT THIS IS
------------
Every user request produces a sequence of JSON events in
``logs/request_traces.jsonl``. The events are the evidence behind report
heading 4 ("where the time goes"): they let us split a slow request into queue
waiting, retrieval, generation, verification and retry waiting.

DESIGN RULES
------------
1. **UTC timestamps** for anything persisted. A reviewer in another timezone
   must be able to read the file.
2. **Monotonic durations** for elapsed time. Wall clocks can jump; monotonic
   clocks cannot, which matters when measuring milliseconds.
3. **Missing is null, never zero.** A request that never called a model has no
   first-token measurement; writing 0 ms would be a lie.
4. **No secrets.** The caller passes ``settings.redacted()``; this module never
   receives the API key.
5. **One JSON object per line.** Append-only, easy to grep, easy to load.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with milliseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def monotonic_ms() -> float:
    """Return a monotonic clock reading in milliseconds.

    ``time.monotonic()`` is not affected by system clock changes, so it is the
    correct clock for measuring durations. It has an arbitrary origin; only
    differences are meaningful.
    """
    import time

    return time.monotonic() * 1000.0


class JsonlWriter:
    """Small append-only JSONL writer that is safe to use from async code.

    ``threading.Lock`` is used rather than ``asyncio.Lock`` because the write
    itself is a short blocking file append. Holding an asyncio lock around
    blocking I/O would be misleading; a thread lock is honest about what is
    happening.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class TraceWriter:
    """Writes one structured event per call to ``request_traces.jsonl``.

    Event names used by the pipeline (the brief and master prompt ask for these
    specifically):

    request_received, scope_filtered, retrieval_started, retrieval_completed,
    admission_decided, queue_entered, queue_exited, model_call_started,
    first_token_received, model_call_completed, verification_started,
    verification_completed, retry_scheduled, request_refused, request_rejected,
    request_expired, request_completed, cache_checked, evidence_assessed,
    regeneration_scheduled, loop_scheduled.
    """

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._writer = JsonlWriter(path)

    def log(
        self,
        request_id: str,
        stage: str,
        event_type: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Append one event and return it (useful for tests).

        ``fields`` are merged into the record. They may include ``None``; JSON
        encodes that as ``null``, which is how we represent "not measured".
        """
        event: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "request_id": request_id,
            "timestamp": utc_now_iso(),
            "stage": stage,
            "event_type": event_type,
        }
        event.update(fields)
        if self.enabled:
            self._writer.append(event)
        return event


class ModelCallWriter:
    """Writes one record per actual or simulated model call.

    This is the reconciliation file: the sum of ``tokens_in`` and ``tokens_out``
    for a request must equal that request's row in ``results.csv``. The tests
    in ``tests/test_instrumentation.py`` assert it.
    """

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._writer = JsonlWriter(path)

    def write(self, record: dict[str, Any]) -> None:
        if self.enabled:
            self._writer.append(record)
