#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.crawl_events import merge_profile_events, missing_seed_error_events  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.operations import domain_request_summary, latency_summary, peak_rss_bytes  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def terminal_events_for_run(profiles: list[dict], events: list[dict], crawl_complete: bool) -> list[dict]:
    return missing_seed_error_events(profiles, events) if crawl_complete else []


def main() -> None:
    try:
        from scrapy.crawler import CrawlerProcess
        from scrapy.utils.project import get_project_settings
        from norway_company_agent.scrapy_crawler import SignalpostWebsiteSpider
    except ImportError as exc:
        raise SystemExit("Install the crawler runtime with: uv sync --extra crawler") from exc

    parser = argparse.ArgumentParser(description="Resumable, robots-aware Scrapy scheduler for registry-linked company sites.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--jobdir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--organisation-number", action="append", dest="organisation_numbers")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--per-domain", type=int, default=2)
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    input_path = Path(args.input)
    input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    jobdir = Path(args.jobdir)
    jobdir.mkdir(parents=True, exist_ok=True)
    metadata_path = jobdir / "signalpost-input.json"
    metadata = {"input_sha256": input_hash, "input_path": str(input_path.resolve())}
    if metadata_path.exists() and json.loads(metadata_path.read_text(encoding="utf-8")) != metadata:
        raise SystemExit("Job directory belongs to a different input. Choose a new --jobdir.")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    events_path = Path(args.events)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_before = read_jsonl(events_path) if events_path.exists() else []
    settings = get_project_settings()
    settings.update({
        "CONCURRENT_REQUESTS": args.concurrency,
        "CONCURRENT_REQUESTS_PER_DOMAIN": args.per_domain,
        "JOBDIR": str(jobdir),
        "LOG_LEVEL": args.log_level,
        "FEEDS": {str(events_path.resolve()): {"format": "jsonlines", "encoding": "utf8", "overwrite": False}},
    })
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(SignalpostWebsiteSpider)
    selected_numbers = set(args.organisation_numbers or []) or None
    process.crawl(
        crawler,
        profiles_path=str(input_path),
        limit=args.limit,
        organisation_numbers=",".join(sorted(selected_numbers)) if selected_numbers else None,
    )
    run_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = time.monotonic()
    process.start()
    elapsed = time.monotonic() - started
    run_finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stats = crawler.stats.get_stats()
    crawl_complete = stats.get("finish_reason") == "finished"

    rows = read_jsonl(input_path)
    crawl_targets = [
        row for row in rows
        if row.get("website") and (selected_numbers is None or row["organisation_number"] in selected_numbers)
    ][: args.limit if args.limit else None]
    events = read_jsonl(events_path) if events_path.exists() else []
    missing_seed_events = terminal_events_for_run(crawl_targets, events, crawl_complete)
    if missing_seed_events:
        with events_path.open("a", encoding="utf-8") as handle:
            for event in missing_seed_events:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        events.extend(missing_seed_events)
    by_org: dict[str, list[dict]] = defaultdict(list)
    dedupe = set()
    for event in events:
        key = (event.get("organisation_number"), event.get("page_kind"), event.get("final_url"), event.get("content_sha256"), event.get("status"))
        if key in dedupe:
            continue
        dedupe.add(key)
        by_org[str(event.get("organisation_number") or "")].append(event)
    touched = 0
    website_statuses: Counter[str] = Counter()
    identity_statuses: Counter[str] = Counter()
    published_socials = 0
    for row in rows:
        profile_events = by_org.get(row["organisation_number"])
        if not profile_events:
            continue
        website = merge_profile_events(row, profile_events)
        gated = apply_website_identity_gate(row, website)
        row.setdefault("evidence", {})["website"] = gated["website"]
        website_statuses[gated["website"].get("status", "invalid")] += 1
        if gated["assessment"]:
            identity_statuses[gated["assessment"].get("status", "invalid")] += 1
        published_socials += len((gated["website"].get("value") or {}).get("social_links") or [])
        touched += 1
    write_jsonl(Path(args.output), rows)
    output_hash = hashlib.sha256(Path(args.output).read_bytes()).hexdigest()

    request_latencies = getattr(crawler, "signalpost_request_latency_ms", [])
    response_download_latencies = getattr(crawler, "signalpost_response_download_latency_ms", [])
    failure_attempt_latencies = getattr(crawler, "signalpost_failure_attempt_latency_ms", [])
    selected_orgs = {row["organisation_number"] for row in crawl_targets}
    company_completion_by_org = dict(getattr(crawler, "signalpost_company_completion_ms_by_org", {}))
    synthetic_completion_orgs = selected_orgs - set(company_completion_by_org)
    company_completion_latencies = list(company_completion_by_org.values())
    company_completion_latencies.extend([elapsed * 1000] * len(synthetic_completion_orgs))
    request_domains = getattr(crawler, "signalpost_request_domains", Counter())
    response_statuses = getattr(crawler, "signalpost_response_statuses", Counter())
    error_buckets = getattr(crawler, "signalpost_error_buckets", Counter())
    events_added = max(0, len(events) - len(events_before))
    terminal_orgs = selected_orgs & {str(event.get("organisation_number") or "") for event in events}
    report = {
        "input_sha256": input_hash,
        "profiles": len(rows),
        "website_seeds_selected": len(crawl_targets),
        "profiles_with_events": touched,
        "unique_events": len(dedupe),
        "events_added": events_added,
        "events_present_before_run": len(events_before),
        "synthetic_terminal_events": len(missing_seed_events),
        "website_statuses": dict(website_statuses),
        "identity_statuses": dict(identity_statuses),
        "published_social_profiles": published_socials,
        "elapsed_seconds": round(elapsed, 3),
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "events_per_second": round(events_added / elapsed, 3) if elapsed else None,
        "requests": stats.get("downloader/request_count", 0),
        "responses": stats.get("downloader/response_count", 0),
        "response_bytes": stats.get("downloader/response_bytes", 0),
        "retries": sum(value for key, value in stats.items() if str(key).startswith("retry/reason_count/")),
        "robots_forbidden": stats.get("robotstxt/forbidden", 0),
        "request_wall_latency_including_queue": latency_summary(request_latencies),
        "response_download_latency": latency_summary(response_download_latencies),
        "failure_attempt_wall_latency": latency_summary(failure_attempt_latencies),
        "company_completion_latency": latency_summary(company_completion_latencies),
        "company_completion_synthetic_upper_bounds": len(synthetic_completion_orgs),
        "request_domain_fairness": domain_request_summary(request_domains),
        "response_statuses": dict(sorted(response_statuses.items())),
        "error_buckets": dict(sorted(error_buckets.items())),
        "request_amplification_per_seed": round(stats.get("downloader/request_count", 0) / len(crawl_targets), 3) if crawl_targets else None,
        "terminal_coverage": round(len(terminal_orgs) / len(selected_orgs), 6) if selected_orgs else None,
        "crawl_complete": crawl_complete,
        "peak_rss_bytes": peak_rss_bytes(),
        "output_sha256": output_hash,
        "finish_reason": stats.get("finish_reason"),
        "resume_noop": bool(events_before and stats.get("downloader/request_count", 0) == 0),
        "concurrency": args.concurrency,
        "per_domain": args.per_domain,
        "jobdir": str(jobdir),
        "events_path": str(events_path),
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
