from __future__ import annotations

import hashlib
import json
from typing import Any

from .http import FetchResult


class SnapshotFetcher:
    """Evaluator-owned byte snapshot adapter with the same FetchResult contract as live HTTP."""

    def __init__(self, snapshot: dict[str, Any]):
        self.snapshot = snapshot
        self.requests: list[str] = []

    def __call__(self, url: str) -> FetchResult:
        self.requests.append(url)
        item = self.snapshot.get("responses", {}).get(url)
        if item is None:
            return FetchResult(url, 0, 0, 0, error="snapshot response missing", retrieved_at=self.snapshot.get("retrieved_at"), effective_at=self.snapshot.get("effective_at"))
        status = int(item.get("status", 200))
        if "raw_json" in item:
            raw = str(item["raw_json"]).encode("utf-8")
            body = json.loads(raw)
        else:
            body = item.get("body")
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return FetchResult(
            url=url,
            status=status,
            elapsed_ms=int(item.get("elapsed_ms", 0)),
            bytes_received=len(raw),
            body=body if status == 200 else None,
            error=None if status == 200 else f"HTTP {status}",
            content_sha256=hashlib.sha256(raw).hexdigest(),
            retrieved_at=item.get("retrieved_at") or self.snapshot.get("retrieved_at"),
            effective_at=item.get("effective_at") or self.snapshot.get("effective_at"),
        )
