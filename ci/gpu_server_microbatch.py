"""One-model, ordered micro-batcher for the pinned gpu-server Flask app.

Gunicorn's normal sync worker processes one HTTP request at a time.  That left the
single GPU-resident spaCy model idle between resolver requests, while adding
Gunicorn workers would duplicate the model and its VRAM.  This module is overlaid
onto the pinned gpu-server at setup time: a single inference thread owns the model,
and several lightweight HTTP threads enqueue work for it.  Requests are split and
reassembled in original order, so batching is transport-only and does not alter the
per-line result contract.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
import threading
import time
from typing import Any, Callable


@dataclass
class _Ticket:
    texts: list[str]
    lang: str
    with_span_text: bool
    enqueued_at: float
    offset: int = 0
    remaining: int = field(init=False)
    results: list[dict[str, Any] | None] = field(init=False)
    error: BaseException | None = None
    complete: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.remaining = len(self.texts)
        self.results = [None] * len(self.texts)


class OrderedMicroBatcher:
    """Serialize inference through one model while batching compatible requests.

    ``runner`` must return ``{"results": [...]}`` in the same order as its input.
    Only this class calls it, therefore spaCy/CuPy never receives concurrent model
    calls even though Gunicorn serves several HTTP waiters concurrently.
    """

    def __init__(self, runner: Callable[[list[str], str, bool], dict[str, Any]]):
        self._runner = runner
        self._max_texts = max(1, int(os.environ.get("LINKER_NER_MICROBATCH_TEXTS", "100")))
        self._wait_seconds = max(
            0.0,
            float(os.environ.get("LINKER_NER_MICROBATCH_WAIT_MS", "8")) / 1000.0,
        )
        self._pending: deque[_Ticket] = deque()
        self._condition = threading.Condition()
        self._metrics_lock = threading.Lock()
        self._batches = 0
        self._requests = 0
        self._texts = 0
        self._model_seconds = 0.0
        self._queue_seconds = 0.0
        self._thread = threading.Thread(
            target=self._serve,
            name="otzaria-ner-microbatch",
            daemon=True,
        )
        self._thread.start()

    def submit(self, texts: list[str], lang: str, with_span_text: bool) -> dict[str, Any]:
        # Preserve the upstream empty-list result exactly without waking the model.
        if not texts:
            return {"results": []}
        ticket = _Ticket(list(texts), lang, with_span_text, time.monotonic())
        with self._metrics_lock:
            self._requests += 1
        with self._condition:
            self._pending.append(ticket)
            self._condition.notify()
        ticket.complete.wait()
        if ticket.error is not None:
            raise ticket.error
        if any(value is None for value in ticket.results):
            raise RuntimeError("micro-batcher completed a request with missing results")
        return {"results": ticket.results}

    def metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            return {
                "batches": self._batches,
                "requests": self._requests,
                "texts": self._texts,
                "model_seconds": round(self._model_seconds, 6),
                "queue_seconds": round(self._queue_seconds, 6),
                "max_texts": self._max_texts,
                "wait_ms": round(self._wait_seconds * 1000, 3),
            }

    def _take_batch(self) -> tuple[list[_Ticket], list[tuple[_Ticket, int, int]], list[str]]:
        with self._condition:
            while not self._pending:
                self._condition.wait()
            # A tiny coalescing window makes independently scheduled resolver workers
            # arrive together.  It is bounded and applies only before model work.
            if self._wait_seconds:
                self._condition.wait(timeout=self._wait_seconds)
            first = self._pending.popleft()
            key = (first.lang, first.with_span_text)
            tickets = [first]
            segments: list[tuple[_Ticket, int, int]] = []
            texts: list[str] = []
            capacity = self._max_texts

            def take(ticket: _Ticket) -> bool:
                nonlocal capacity
                start = ticket.offset
                count = min(capacity, len(ticket.texts) - start)
                if count <= 0:
                    raise RuntimeError("micro-batcher queued an exhausted request")
                end = start + count
                texts.extend(ticket.texts[start:end])
                segments.append((ticket, start, end))
                ticket.offset = end
                capacity -= count
                if ticket.offset < len(ticket.texts):
                    self._pending.appendleft(ticket)
                    return False
                return capacity > 0

            if take(first):
                while capacity and self._pending:
                    candidate = self._pending[0]
                    if (candidate.lang, candidate.with_span_text) != key:
                        break
                    candidate = self._pending.popleft()
                    tickets.append(candidate)
                    if not take(candidate):
                        break
            return tickets, segments, texts

    def _serve(self) -> None:
        while True:
            tickets, segments, texts = self._take_batch()
            first = tickets[0]
            started = time.monotonic()
            try:
                payload = self._runner(texts, first.lang, first.with_span_text)
                results = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(results, list) or len(results) != len(texts):
                    raise RuntimeError("GPU model returned invalid micro-batch result shape")
                cursor = 0
                for ticket, start, end in segments:
                    ticket.results[start:end] = results[cursor:cursor + (end - start)]
                    ticket.remaining -= end - start
                    cursor += end - start
                    if ticket.remaining == 0:
                        ticket.complete.set()
            except BaseException as error:
                for ticket in {id(ticket): ticket for ticket in tickets}.values():
                    ticket.error = error
                    ticket.complete.set()
            finally:
                ended = time.monotonic()
                with self._metrics_lock:
                    self._batches += 1
                    self._texts += len(texts)
                    self._model_seconds += ended - started
                    self._queue_seconds += sum(started - ticket.enqueued_at for ticket in tickets)
