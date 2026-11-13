"""Bounded transport primitives. Authorization stays in the supplied connector."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import re
import threading
import time
from .policy import ByteBudget, GateError


def probe_range(open_authorized_response, *, start: int, end: int, budget: ByteBudget, timeout: float = 15) -> dict:
    """Verify this exact asset/range, and close a Range-ignored 200 unread.

    open_authorized_response(start,end,timeout) revalidates current source identity
    and returns a response with status, headers, read(n), close(). No URL/cookies
    are accepted or returned here. budget is cumulative across retry attempts.
    """
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        raise GateError('INVALID_RANGE')
    wanted = end - start + 1
    if wanted > budget.limit - budget.received:
        raise GateError('BYTE_BUDGET_EXHAUSTED')
    response = open_authorized_response(start, end, timeout)
    actual = 0
    try:
        status = response.status
        if status in (401, 403, 429):
            raise GateError('SOURCE_ACCESS_DENIED')
        if status == 200:
            return {'state': 'unsupported', 'received_bytes': 0, 'reason': 'Range ignored; body closed without consumption', 'full_stream_fallback_allowed': False}
        if status != 206:
            return {'state': 'unknown', 'received_bytes': 0, 'reason': 'No valid 206 response', 'full_stream_fallback_allowed': False}
        value = response.headers.get('Content-Range') or response.headers.get('content-range') or ''
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+|\*)', value)
        if not match:
            raise GateError('INVALID_CONTENT_RANGE')
        a, b = int(match[1]), int(match[2])
        total = None if match[3] == '*' else int(match[3])
        if a != start or b != end or (total is not None and (total <= b or total <= 0)):
            raise GateError('CONTENT_RANGE_MISMATCH')
        declared = response.headers.get('Content-Length') or response.headers.get('content-length')
        if declared is not None and (not declared.isdecimal() or int(declared) != wanted):
            raise GateError('CONTENT_LENGTH_MISMATCH')
        began = time.monotonic()
        while actual < wanted:
            if time.monotonic() - began > timeout:
                raise GateError('PROBE_TIMEOUT')
            chunk = response.read(min(64 * 1024, wanted - actual, budget.limit - budget.received))
            if not chunk:
                raise GateError('TRUNCATED_RANGE')
            if len(chunk) > wanted - actual:
                # An invalid transport violated read(n); charge before refusing.
                budget.charge(len(chunk))
                raise GateError('READER_VIOLATED_BOUND')
            budget.charge(len(chunk)); actual += len(chunk)
        return {'state': 'verified_available', 'capability': 'range_206', 'received_bytes': actual, 'requested_start': start, 'requested_end': end, 'total_bytes': total, 'scope': 'this exact asset/range observation', 'full_stream_fallback_allowed': False}
    finally:
        response.close()


class BoundedReader:
    def __init__(self, reader, budget: ByteBudget, *, revalidate=None):
        self.reader, self.budget, self.revalidate = reader, budget, revalidate

    def read(self, size):
        if type(size) is not int or size <= 0:
            raise GateError('UNBOUNDED_READ_FORBIDDEN')
        if self.revalidate:
            self.revalidate()
        remaining = self.budget.limit - self.budget.received
        if not remaining:
            raise GateError('BYTE_BUDGET_EXHAUSTED')
        cap = min(size, remaining)
        chunk = self.reader.read(cap)
        self.budget.charge(len(chunk))
        if len(chunk) > cap:
            raise GateError('READER_VIOLATED_BOUND')
        return chunk

    def close(self):
        self.reader.close()


class ByteBoundedQueue:
    """Explicit backpressure: queue caps both item count and resident byte sizes."""
    def __init__(self, max_items: int, max_bytes: int):
        if max_items < 1 or max_bytes < 1:
            raise ValueError('Queue limits must be positive')
        self.max_items, self.max_bytes = max_items, max_bytes
        self.items, self.resident_bytes = deque(), 0
        self.condition = threading.Condition()

    def put(self, payload: bytes, *, timeout: float = 30):
        if len(payload) > self.max_bytes:
            raise GateError('ITEM_EXCEEDS_QUEUE_BUDGET')
        expires = time.monotonic() + timeout
        with self.condition:
            while len(self.items) >= self.max_items or self.resident_bytes + len(payload) > self.max_bytes:
                left = expires - time.monotonic()
                if left <= 0:
                    raise GateError('BACKPRESSURE_TIMEOUT')
                self.condition.wait(left)
            self.items.append(payload); self.resident_bytes += len(payload)
            self.condition.notify_all()

    def get(self, *, timeout: float = 30) -> bytes:
        expires = time.monotonic() + timeout
        with self.condition:
            while not self.items:
                left = expires - time.monotonic()
                if left <= 0:
                    raise GateError('QUEUE_EMPTY_TIMEOUT')
                self.condition.wait(left)
            value = self.items.popleft(); self.resident_bytes -= len(value)
            self.condition.notify_all()
            return value
