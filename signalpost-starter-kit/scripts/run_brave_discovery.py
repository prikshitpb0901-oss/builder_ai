#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.discovery import build_company_search_query, choose_search_candidate, parse_brave_web_results  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def brave_search(profile: dict, api_key: str, *, timeout: float, count: int) -> tuple[list[dict], dict]:
    query = build_company_search_query(profile)
    url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode({
        "q": query,
        "count": count,
        "country": "no",
        "search_lang": "nb",
        "safesearch": "moderate",
        "spellcheck": "0",
    })
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)",
        "X-Subscription-Token": api_key,
    })
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        payload = json.loads(raw)
        return parse_brave_web_results(payload, query=query), {
            "status": response.status,
            "latency_ms": elapsed_ms,
            "bytes": len(raw),
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        return [], {
            "status": getattr(exc, "code", 0),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "bytes": 0,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "error": type(exc).__name__,
        }


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def main() -> None:
    parser = argparse.ArgumentParser(description="Transient Brave discovery followed by independent exact-entity site crawling.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--limit", type=int, default=20, help="Maximum missing-website profiles to query")
    parser.add_argument("--count", type=int, default=10, choices=range(1, 21), metavar="1..20")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--min-interval", type=float, default=0.1)
    parser.add_argument("--promote-verified", action="store_true", help="Copy exact-entity discovered sites into canonical website evidence")
    parser.add_argument("--api-key-env", default="BRAVE_SEARCH_API_KEY")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        parser.error(f"Missing API key in environment variable {args.api_key_env}")

    rows = read_jsonl(Path(args.input))
    counts: Counter[str] = Counter()
    provider_latencies: list[int] = []
    started_at = utc_now()
    queried = 0
    for row in rows:
        if queried >= args.limit:
            break
        if row.get("website"):
            counts["registry_website_present_skipped"] += 1
            continue
        queried += 1
        results, operation = brave_search(row, api_key, timeout=args.timeout, count=args.count)
        provider_latencies.append(operation["latency_ms"])
        counts["provider_requests"] += 1
        counts["provider_bytes"] += operation["bytes"]
        if operation.get("error"):
            counts["provider_errors"] += 1
        decision = choose_search_candidate(row, results)
        selected = decision.get("selected")
        discovery_summary = {
            "provider": "brave_search_api",
            "query_sha256": operation["query_sha256"],
            "candidate_count": len(results),
            "selected_for_independent_crawl": bool(selected),
            "provider_status": operation["status"],
            "retention_policy": "Search titles, snippets, ranks, query text, and raw response are not persisted.",
        }
        if not selected:
            counts["abstained_before_crawl"] += 1
            row.setdefault("evidence", {})["website_discovery"] = evidence(
                "website_discovery",
                "not_found",
                "transient_brave_search",
                BRAVE_ENDPOINT,
                value=discovery_summary,
                note="No result passed the deterministic crawl-candidate gate; raw search output was discarded.",
            )
            time.sleep(args.min_interval)
            continue

        website, web_ops = fetch_website(selected["url"], timeout=args.timeout)
        gated = apply_website_identity_gate(row, website)
        website = gated["website"]
        assessment = gated["assessment"]
        value = website.get("value") or {}
        # Only independently fetched page evidence is retained. Brave title/snippet/rank/query are discarded.
        website["source_type"] = "search_discovered_company_website"
        website["value"] = value
        row.setdefault("evidence", {})["website_discovery"] = evidence(
            "website_discovery",
            "available" if assessment["publishable"] else "not_found",
            "transient_brave_search_then_independent_crawl",
            BRAVE_ENDPOINT,
            value={**discovery_summary, "independent_page_url": website.get("source_url") if assessment["publishable"] else None},
            note="Search output was transient. Publication depends only on independently fetched exact-entity page evidence.",
        )
        row["evidence"]["website_discovered"] = website
        counts["independent_crawls"] += 1
        counts["crawl_requests"] += web_ops.get("requests", 0)
        if assessment["publishable"] and website.get("status") == "available":
            counts["verified_sites"] += 1
            if args.promote_verified:
                row["evidence"]["website"] = website
                counts["promoted_sites"] += 1
        else:
            counts["quarantined_sites"] += 1
        time.sleep(args.min_interval)

    write_jsonl(Path(args.output), rows)
    report = {
        "generated_at": utc_now(),
        "started_at": started_at,
        "provider": "Brave Search API",
        "provider_endpoint": BRAVE_ENDPOINT,
        "input_profiles": len(rows),
        "queried_missing_website_profiles": queried,
        "counts": dict(counts),
        "provider_latency_ms": {"p50": percentile(provider_latencies, 0.5), "p95": percentile(provider_latencies, 0.95)},
        "raw_search_results_persisted": False,
        "promote_verified_enabled": args.promote_verified,
        "qualification": "not_evaluated_on_500_org_external_final_corpus",
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
