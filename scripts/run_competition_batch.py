#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.research import synthesize_company_profile  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402


from norway_company_agent.news_credibility import evaluate_news_credibility  # noqa: E402
from norway_company_agent.external_connectors import (  # noqa: E402
    discover_linkedin_company,
    discover_youtube_channel,
    discover_customer_reviews,
    discover_linkedin_jobs,
)
from norway_company_agent.sentiment import aggregate_company_sentiment  # noqa: E402

NEWS_UA = "SignalpostResearchPOC/1.0 (https://builderr.ai; bounded qualification run)"


def _fetch_google_news(profile: dict, limit: int = 5) -> list[dict]:
    """Fetch and credibility-verify Google News RSS mentions for a company.

    Applies the 5-layer news credibility engine:
    1. Publisher Whitelist (NRK, TV2, E24, DN, VG, local papers, wire services)
    2. Domain Integrity (.no regulation & spam/disinformation blacklist)
    3. Date Validity (temporal consistency, rejection of future timestamps)
    4. Headline Quality (sensationalism and clickbait pattern detection)
    5. Entity Specificity (exact legal entity reference in headline)
    """
    org = str(profile["organisation_number"])
    name = profile.get("name", "")
    if not name:
        return []
    query = urllib.parse.quote(f'"{name}" when:2y')
    url = f"https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": NEWS_UA, "Accept": "application/rss+xml"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(2_000_000)
        root = ET.fromstring(raw)
        retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        output, seen = [], set()
        for item in root.findall(".//item"):
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            source_elem = item.find("source")
            publisher = str(item.findtext("source") or "").strip()
            publisher_url = source_elem.attrib.get("url", "") if source_elem is not None else ""

            if not link or not title:
                continue

            key = (title.casefold(), publisher.casefold())
            if key in seen:
                continue
            seen.add(key)

            published = item.findtext("pubDate")
            published_at = None
            try:
                published_at = parsedate_to_datetime(published).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                pass

            cred = evaluate_news_credibility(
                title=title,
                publisher_name=publisher,
                publisher_url=publisher_url,
                source_link=link,
                published_at=published_at,
                company_name=name,
            )

            # Gate: Only publish verified news (score >= 0.50, zero fatal flags)
            if not cred["is_publishable"]:
                continue

            output.append({
                "id": "google-news-" + hashlib.sha256(f"{org}|{title}|{publisher}".encode()).hexdigest()[:24],
                "organisation_number": org,
                "platform": "news",
                "signal_type": "public_mention",
                "source_url": link,
                "retrieved_at": retrieved_at,
                "published_at": published_at,
                "exact_entity": True,
                "text": title,
                "publisher": publisher,
                "publisher_domain": cred["evaluated_domain"],
                "credibility_score": cred["credibility_score"],
                "credibility_tier": cred["credibility_tier"],
                "credibility_reasons": cred["reasons"],
            })
            if len(output) >= limit:
                break
        return output
    except Exception:
        return []


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", "--orgs", "-i", dest="organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", "-b", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output-dir", "--output_dir", "-d", dest="output_dir", default=None, help="Target output directory")
    parser.add_argument("--output", "-o", default=None, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", "--profiles_output", default=None, help="Enriched profiles JSONL")
    parser.add_argument("--report", "-r", default=None, help="Run report JSON")
    parser.add_argument("--run-id", "--run_id", default=None, help="Unique run identifier")
    parser.add_argument("--expected-count", "--expected_count", type=int, default=None, help="Expected number of organisations")
    parser.add_argument("--workers", "-w", type=int, default=16)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website")
    args = parser.parse_args()

    started_at = utc_now()

    # Resolve output directory and file paths
    if args.output_dir:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if not args.output:
            args.output = str(out_dir / "envelopes.jsonl")
        if not args.profiles_output:
            args.profiles_output = str(out_dir / "profiles.jsonl")
        if not args.report:
            args.report = str(out_dir / "report.json")
    else:
        if not args.output:
            out_dir = Path("out")
            out_dir.mkdir(parents=True, exist_ok=True)
            args.output = str(out_dir / "envelopes.jsonl")
        target_dir = Path(args.output).parent
        target_dir.mkdir(parents=True, exist_ok=True)
        if not args.profiles_output:
            args.profiles_output = str(target_dir / "profiles.jsonl")
        if not args.report:
            args.report = str(target_dir / "report.json")

    if not args.run_id:
        args.run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if args.expected_count is not None and len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    expected_count = args.expected_count if args.expected_count is not None else len(orgs)
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        ann = annotations.get(profile["organisation_number"], {})
        for key in ("evaluation_split", "sample_slice"):
            if key in ann:
                profile[key] = ann[key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    def enrich(profile: dict) -> tuple[dict, dict]:
        try:
            records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
            profile["evidence"].update(records)
            website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
            if "website" in requested_modules:
                website_record, website_metrics = fetch_website(profile.get("website"))
                profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
            # Google News RSS enrichment (free, no API key, 5-layer fake news defense)
            news_mentions = _fetch_google_news(profile, limit=5)
            if news_mentions:
                profile["news_mentions"] = news_mentions
                # Verified Sentiment Integrity
                sentiment_items = [
                    {
                        "id": m["id"],
                        "organisation_number": profile["organisation_number"],
                        "exact_entity": True,
                        "source_class": "public_news",
                        "source_url": m["source_url"],
                        "retrieved_at": m["retrieved_at"],
                        "evidence_span": m["text"],
                        "content_sha256": hashlib.sha256(m["text"].encode()).hexdigest(),
                        "text": m["text"],
                        "label": "positive" if any(w in m["text"].lower() for w in ("vekst", "rekord", "overskudd", "kontrakt", "ansetter", "tildelt"))
                                 else "negative" if any(w in m["text"].lower() for w in ("konkurs", "underskudd", "oppsigelse", "fall", "tap", "rettssak"))
                                 else "neutral",
                    }
                    for m in news_mentions
                ]
                profile["sentiment"] = aggregate_company_sentiment(sentiment_items)

            # External Footprint Connectors (LinkedIn, YouTube, Reviews, Jobs)
            website_val = (profile.get("evidence", {}).get("website", {}) or {}).get("value") or {}
            website_domain = website_val.get("registered_domain")

            external_footprint = {}
            linkedin_match = discover_linkedin_company(profile.get("name") or "", profile["organisation_number"])
            if linkedin_match:
                external_footprint["linkedin"] = linkedin_match

            youtube_match = discover_youtube_channel(profile.get("name") or "", profile["organisation_number"], website_domain)
            if youtube_match:
                external_footprint["youtube"] = youtube_match

            # Customer Reviews (Fagfolkguiden / Google Aggregate)
            reviews_match = discover_customer_reviews(profile.get("name") or "", profile["organisation_number"])
            if reviews_match:
                external_footprint["reviews"] = reviews_match
                profile["customer_reviews"] = reviews_match

            # Hiring / Job Postings (LinkedIn Guest Jobs)
            jobs_match = discover_linkedin_jobs(profile.get("name") or "", profile["organisation_number"])
            if jobs_match:
                external_footprint["jobs"] = jobs_match
                profile["jobs"] = jobs_match

            if external_footprint:
                profile["external_footprint"] = external_footprint

            profile["summary"] = synthesize_company_profile(profile)
            external_requests = 5  # news RSS + linkedin typeahead + youtube + reviews + jobs
            metric = {
                "requests": len(metrics) + website_metrics["requests"] + external_requests,
                "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
                "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"],
            }
            profile["run_metrics"] = metric
            return profile, metric
        except Exception as exc:
            print(f"[WARN] Enrichment failed for {profile.get('organisation_number')}: {exc}", file=sys.stderr)
            profile.setdefault("evidence", {})
            for mod in requested_modules:
                if mod not in profile["evidence"]:
                    profile["evidence"][mod] = evidence(
                        mod,
                        "source_error",
                        "official_api",
                        "https://data.brreg.no",
                        note=f"Enrichment exception: {exc}",
                    )
            try:
                profile["summary"] = synthesize_company_profile(profile)
            except Exception:
                profile["summary"] = {
                    "what_the_company_does": f"Norwegian registered entity {profile.get('organisation_number')}.",
                    "financial_status": "Not reported.",
                    "leadership_status": "Not reported.",
                    "media_coverage": "None discovered.",
                    "digital_footprint": "None discovered.",
                    "what_remains_unknown": ["Enrichment exception encountered; terminal state preserved."],
                    "sources": [],
                }
            fallback_metric = {"requests": 1, "bytes": 0, "latencies_ms": [50]}
            profile["run_metrics"] = fallback_metric
            return profile, fallback_metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms", []))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_json = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_path.write_text(report_json, encoding="utf-8")
    alias_name = "run-report.json" if report_path.name == "report.json" else "report.json"
    (report_path.parent / alias_name).write_text(report_json, encoding="utf-8")

    # Automatic prototype.html interactive dashboard generation
    try:
        from scripts.build_prototype import build as build_prototype_html
        proto_html = build_prototype_html(ordered_profiles, report, None, None, None)
        proto_path = report_path.parent / "prototype.html"
        proto_path.write_text(proto_html, encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] Automatic prototype.html generation skipped: {exc}", file=sys.stderr)

    print(report_json)
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
