#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: str | None) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}


def rows(path: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def capped(weight: float, value: float | int | None) -> float:
    return round(weight * max(0.0, min(1.0, float(value or 0))), 3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost competition proxy v3: external intelligence is the primary differentiator.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--external-report", required=True)
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--resume-report")
    parser.add_argument("--refresh-report", required=True)
    parser.add_argument("--research-report", required=True)
    parser.add_argument("--sentiment-report")
    parser.add_argument("--ux-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", type=float, default=80)
    args = parser.parse_args()

    profiles = rows(args.profiles)
    n = len(profiles)
    external = load(args.external_report)
    batch = load(args.batch_report)
    resume = load(args.resume_report)
    refresh = load(args.refresh_report)
    research = load(args.research_report)
    sentiment = load(args.sentiment_report)
    ux = load(args.ux_report)
    coverage = external.get("coverage", {})

    external_qualified = bool(external.get("qualification_passed"))
    zero_wrong = external.get("wrong_entity_publications") == 0 and external.get("published_audited", 0) > 0
    supported = external.get("unsupported_publications") == 0 and external.get("published_audited", 0) > 0
    entity_points = 10.0 if external_qualified and zero_wrong else 0.0
    breadth_points = capped(10, coverage.get("two_platforms")) if external_qualified else 0.0
    workforce_points = capped(7, coverage.get("workforce_jobs")) if external_qualified else 0.0
    review_points = capped(8, coverage.get("ratings_reviews")) if external_qualified else 0.0
    buzz_points = capped(7, coverage.get("buzz_engagement")) if external_qualified else 0.0

    sentiment_gate = bool(
        sentiment.get("qualification_passed")
        and sentiment.get("wrong_entity_predictions") == 0
        and sentiment.get("evidence_support_rate") == 1.0
    )
    sentiment_points = capped(10, coverage.get("sentiment")) if sentiment_gate else 0.0
    freshness_points = capped(3, external.get("fresh_coverage")) if external_qualified else 0.0
    external_score = {
        "verified_external_identity": entity_points,
        "multi_source_breadth": breadth_points,
        "workforce_and_jobs": workforce_points,
        "ratings_and_reviews": review_points,
        "buzz_and_engagement": buzz_points,
        "qualified_sentiment": sentiment_points,
        "external_freshness": freshness_points,
    }

    exact_registry = ratio(
        sum(
            (row.get("evidence", {}).get("registry_live", {}).get("value") or {}).get("organisation_number")
            == row.get("organisation_number")
            for row in profiles
        ),
        n,
    )
    financial = ratio(sum(row.get("evidence", {}).get("financials", {}).get("status") == "available" for row in profiles), n)
    roles_locations = ratio(
        sum(all(module in row.get("evidence", {}) for module in ("roles", "locations")) for row in profiles),
        n,
    )
    website_terminal = ratio(sum("website" in row.get("evidence", {}) for row in profiles), n)
    foundation = {
        "official_identity": capped(4, exact_registry),
        "annual_accounts": capped(4, financial),
        "roles_and_locations": capped(4, roles_locations),
        "website_seed_and_terminal_state": capped(3, website_terminal),
    }

    research_raw = min(12.0, float(research.get("score") or 0))
    external_qa_passed = bool(research.get("external_footprint_qa_passed"))
    research_points = round(min(10.0, research_raw * 10 / 12), 3)
    if not external_qa_passed:
        research_points = min(5.0, research_points)

    batch_valid = bool(batch.get("validation", {}).get("passed") and batch.get("emitted_envelopes") == n)
    resume_valid = bool(resume.get("validation", {}).get("passed") and resume.get("profiles_fetched_this_run") == 0) if resume else False
    refresh_valid = bool(refresh.get("qualification_passed") and refresh.get("evidence_complete") and refresh.get("idempotent_rerun"))
    p95 = batch.get("operations", {}).get("p95_ms")
    connector_policy = bool(external.get("connector_policy_passed"))
    extensibility = {
        "terminal_daily_batch": 4.0 if batch_valid else 0.0,
        "deterministic_resume": 2.0 if resume_valid else 0.0,
        "measured_refresh_diffs": 3.0 if refresh_valid else 0.0,
        "latency_budget": 1.0 if batch_valid and p95 is not None and p95 <= 10_000 else 0.0,
        "connector_rights_and_rate_policy": 2.0 if connector_policy else 0.0,
    }

    ux_raw = min(8.0, float(ux.get("score") or 0))
    ux_external = bool(ux.get("external_intelligence_presented"))
    ux_points = ux_raw if ux_external else min(4.0, ux_raw)

    categories = {
        "external_footprint_intelligence": round(sum(external_score.values()), 3),
        "official_company_foundation": round(sum(foundation.values()), 3),
        "research_agent": research_points,
        "daily_extensibility_refresh": round(sum(extensibility.values()), 3),
        "product_ux_design": ux_points,
    }
    raw_score = round(sum(categories.values()), 3)
    gates = {
        "external_audit_at_least_100": bool(external.get("audit_size_gate")),
        "zero_wrong_company_external_publications": zero_wrong,
        "external_claims_supported": supported,
        "external_connector_policy": connector_policy,
        "official_identity_complete": exact_registry == 1.0,
        "terminal_batch_contract": batch_valid,
        "refresh_replay": refresh_valid,
    }
    qualification = all(gates.values())
    report = {
        "scorer": "signalpost_external_first_competition_proxy_v3",
        "claim_boundary": "Optimization proxy. Final score requires the organiser's frozen hidden companies and independent labels.",
        "rubric_weights": {
            "external_footprint_intelligence": 55,
            "official_company_foundation": 15,
            "research_agent": 10,
            "daily_extensibility_refresh": 12,
            "product_ux_design": 8,
        },
        "profiles": n,
        "details": {"external": external_score, "foundation": foundation, "extensibility": extensibility},
        "category_scores": categories,
        "raw_score": raw_score,
        "target": args.target,
        "target_met": raw_score >= args.target,
        "qualification_gates": gates,
        "qualification_passed": qualification,
        "awardable_score": raw_score if qualification else 0,
        "unproven_or_failed": [name for name, passed in gates.items() if not passed],
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
