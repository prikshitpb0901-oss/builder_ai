from __future__ import annotations

import json
import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class FetchResult:
    url: str
    status: int
    elapsed_ms: int
    bytes_received: int
    body: Any = None
    error: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    effective_at: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_bounded_with_deadline(response: Any, max_bytes: int, deadline: float) -> bytes:
    """Read response stream with strict byte limit and absolute wall-clock deadline."""
    chunks = []
    total = 0
    while total <= max_bytes:
        if time.monotonic() > deadline:
            raise TimeoutError("Read operation exceeded wall-clock deadline (tarpit defense)")
        requested = min(16384, max_bytes + 1 - total)
        chunk = response.read(requested)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("Read operation exceeded wall-clock deadline (tarpit defense)")
        if len(chunk) < requested:
            break
    if total > max_bytes:
        raise ValueError(f"Response exceeded size limit ({max_bytes} bytes)")
    return b"".join(chunks)


def fetch_json(url: str, *, timeout: float = 20.0, attempts: int = 3, max_bytes: int = 5_000_000) -> FetchResult:
    last_error = "request failed"
    for attempt in range(attempts):
        started = time.monotonic()
        deadline = started + timeout
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = _read_bounded_with_deadline(response, max_bytes, deadline)
                elapsed = int((time.monotonic() - started) * 1000)
                return FetchResult(url, response.status, elapsed, len(raw), json.loads(raw), content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            try:
                raw = _read_bounded_with_deadline(exc, 65536, deadline)
            except Exception:
                raw = b""
            if exc.code in {404, 410}:
                return FetchResult(url, exc.code, elapsed, len(raw), error=f"HTTP {exc.code}", content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
            last_error = f"HTTP {exc.code}"
        except TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            last_error = "TimeoutError: connection or read exceeded deadline"
        except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError, Exception) as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            last_error = f"{type(exc).__name__}: {str(exc)[:100]}"
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))
    return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())
