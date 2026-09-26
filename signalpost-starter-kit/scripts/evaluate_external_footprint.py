#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_footprint import publishable_observation, validate_observation  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate frozen external-footprint observations and exact-entity labels.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--labels", required=True, help="JSONL: id, exact_entity, metric_correct, sentiment_correct")
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-audit", type=int, default=100)
    args = parser.parse_args()

    profiles = read_jsonl(Path(args.profiles))
    observations = read_jsonl(Path(args.observations))
    label_rows = read_jsonl(Path(args.labels))
    labels = {str(item["id"]): item for item in label_rows}
    if len(labels) < args.minimum_audit:
        audit_size_gate = False
    else:
        audit_size_gate = True
    if len(labels) != len(label_rows):
        raise ValueError("Label IDs must be unique")

    audited = [item for item in observations if str(item.get("id")) in labels]
    published = [item for item in audited if publishable_observation(item)]
    wrong_entity = sum(not labels[str(item["id"])].get("exact_entity", False) for item in published)
    wrong_metric = sum(not labels[str(item["id"])].get("metric_correct", False) for item in published)
    unsupported = sum(bool(validate_observation(item)) for item in published)
    sentiment_audited = [item for item in published if item.get("sentiment_label") is not None]
    sentiment_correct = sum(labels[str(item["id"])].get("sentiment_correct", False) for item in sentiment_audited)

    all_orgs = {str(item["organisation_number"]) for item in profiles}
    accepted_all = [item for item in observations if publishable_observation(item)]
    org_platforms: dict[str, set[str]] = defaultdict(set)
    org_signals: dict[str, set[str]] = defaultdict(set)
    for item in accepted_all:
        org = str(item["organisation_number"])
        if org not in all_orgs:
            continue
        org_platforms[org].add(str(item["platform"]))
        org_signals[org].add(str(item["signal_type"]))

    coverage = {
        "any_external": ratio(sum(bool(org_platforms[org]) for org in all_orgs), len(all_orgs)),
        "two_platforms": ratio(sum(len(org_platforms[org]) >= 2 for org in all_orgs), len(all_orgs)),
        "workforce_jobs": ratio(sum(bool(org_signals[org] & {"job_posting", "workforce_snapshot"}) for org in all_orgs), len(all_orgs)),
        "ratings_reviews": ratio(sum(bool(org_signals[org] & {"review", "review_summary", "place_summary"}) for org in all_orgs), len(all_orgs)),
        "buzz_engagement": ratio(sum(bool(org_signals[org] & {"public_post", "public_mention", "profile_metrics"}) for org in all_orgs), len(all_orgs)),
        "sentiment": ratio(sum(any(item.get("sentiment_label") for item in accepted_all if str(item.get("organisation_number")) == org) for org in all_orgs), len(all_orgs)),
    }
    acquisition_modes = Counter(str(item.get("acquisition_mode")) for item in observations)
    entity_precision = ratio(len(published) - wrong_entity, len(published))
    metric_precision = ratio(len(published) - wrong_metric, len(published))
    sentiment_accuracy = ratio(sentiment_correct, len(sentiment_audited)) if sentiment_audited else None
    qualification = bool(
        audit_size_gate
        and published
        and wrong_entity == 0
        and unsupported == 0
        and entity_precision >= 0.995
        and metric_precision >= 0.98
    )
    report = {
        "scorer": "signalpost_external_footprint_eval_v1",
        "claim_boundary": "Held-out observation audit plus full-corpus coverage; it does not validate an unlabelled connector.",
        "profiles": len(profiles),
        "observations": len(observations),
        "audited_observations": len(audited),
        "published_audited": len(published),
        "wrong_entity_publications": wrong_entity,
        "unsupported_publications": unsupported,
        "entity_precision": entity_precision,
        "metric_precision": metric_precision,
        "sentiment_audited": len(sentiment_audited),
        "sentiment_accuracy": sentiment_accuracy,
        "coverage": coverage,
        "platform_counts": dict(Counter(str(item.get("platform")) for item in accepted_all)),
        "acquisition_modes": dict(acquisition_modes),
        "minimum_audit": args.minimum_audit,
        "audit_size_gate": audit_size_gate,
        "qualification_passed": qualification,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
