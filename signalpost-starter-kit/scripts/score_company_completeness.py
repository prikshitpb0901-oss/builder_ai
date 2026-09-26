#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

FOUNDATION_WEIGHTS = {"official_identity": 5.0, "annual_accounts": 10.0, "roles": 5.0, "locations": 5.0, "website_terminal_state": 5.0}
ENRICHMENT_WEIGHTS = {"exact_external_identity": 8.0, "verified_handles": 8.0, "profile_metrics": 8.0, "places_identity": 4.0, "places_reviews": 8.0, "workforce_jobs": 8.0, "public_buzz": 8.0, "independent_sentiment": 12.0, "freshness_evidence": 6.0}
CONTROLLER_MAXIMA = {"exact_external_identity": 20.0, "verified_handles": 15.0, "profile_metrics": 10.0, "places_identity": 5.0, "places_reviews": 10.0, "workforce_jobs": 10.0, "public_buzz": 10.0, "independent_sentiment": 15.0, "freshness_evidence": 5.0}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def module_present(evidence: dict[str, Any], name: str) -> bool:
    module = evidence.get(name)
    return isinstance(module, dict) and module.get("status") not in {"error", "not_run"}


def foundation_components(profile: dict[str, Any]) -> dict[str, float]:
    evidence = profile.get("evidence") or {}
    registry = (evidence.get("registry_live") or {}).get("value") or {}
    exact_identity = str(registry.get("organisation_number") or "") == str(profile.get("organisation_number") or "")
    financial = evidence.get("financials") or evidence.get("financial") or {}
    return {
        "official_identity": FOUNDATION_WEIGHTS["official_identity"] if exact_identity else 0.0,
        "annual_accounts": FOUNDATION_WEIGHTS["annual_accounts"] if financial.get("status") == "available" else 0.0,
        "roles": FOUNDATION_WEIGHTS["roles"] if module_present(evidence, "roles") else 0.0,
        "locations": FOUNDATION_WEIGHTS["locations"] if module_present(evidence, "locations") else 0.0,
        "website_terminal_state": FOUNDATION_WEIGHTS["website_terminal_state"] if module_present(evidence, "website") else 0.0,
    }


def enrichment_components(components: dict[str, Any]) -> dict[str, float]:
    return {
        name: round(weight * min(1.0, max(0.0, float(components.get(name) or 0) / CONTROLLER_MAXIMA[name])), 3)
        for name, weight in ENRICHMENT_WEIGHTS.items()
    }


def score_rows(profiles: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    profile_map = {str(row["organisation_number"]): row for row in profiles}
    scored = []
    for result in results:
        org = str(result["organisation_number"])
        if org not in profile_map:
            raise ValueError(f"missing profile for {org}")
        foundation = foundation_components(profile_map[org])
        final = result.get("final") or {}
        strict = enrichment_components(final.get("components") or {})
        experimental = enrichment_components(final.get("experimental_components") or {})
        foundation_score = round(sum(foundation.values()), 3)
        scored.append({
            "organisation_number": org,
            "company_name": result.get("company_name"),
            "foundation": foundation,
            "strict_enrichment": strict,
            "experimental_enrichment": experimental,
            "foundation_score": foundation_score,
            "strict_completeness_score": round(foundation_score + sum(strict.values()), 3),
            "experimental_completeness_score": round(foundation_score + sum(experimental.values()), 3),
        })
    return scored


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    mean = lambda key: round(sum(float(row[key]) for row in rows) / n, 3) if n else 0.0
    coverage = {}
    for group, weights in (("foundation", FOUNDATION_WEIGHTS), ("strict_enrichment", ENRICHMENT_WEIGHTS)):
        coverage[group] = {name: round(sum(float(row[group][name]) > 0 for row in rows) / n, 4) if n else 0.0 for name in weights}
    return {
        "companies": n,
        "foundation_mean": mean("foundation_score"),
        "strict_completeness_mean": mean("strict_completeness_score"),
        "experimental_completeness_mean": mean("experimental_completeness_score"),
        "strict_at_50": sum(float(row["strict_completeness_score"]) >= 50 for row in rows),
        "experimental_at_65": sum(float(row["experimental_completeness_score"]) >= 65 for row in rows),
        "strict_distribution": dict(sorted(Counter(row["strict_completeness_score"] for row in rows).items())),
        "component_coverage": coverage,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score all-source completeness and held-out extension transfer.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--base-size", type=int, default=100)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rows-output")
    args = parser.parse_args()
    scored = score_rows(read_jsonl(Path(args.profiles)), read_jsonl(Path(args.results)))
    if len(scored) <= args.base_size:
        raise ValueError("results must include a non-empty extension after base-size")
    base, extension, combined = summarize(scored[:args.base_size]), summarize(scored[args.base_size:]), summarize(scored)
    report = {
        "scorer": "signalpost_all_source_completeness_v1",
        "definition": "Per-company field completeness across official Norwegian records and verified external evidence. Missing fields earn zero; unavailable sentiment remains unavailable rather than neutral.",
        "weights": {"official_foundation": FOUNDATION_WEIGHTS, "external_enrichment": ENRICHMENT_WEIGHTS},
        "base": base,
        "extension": extension,
        "combined": combined,
        "transfer": {
            "strict_mean_delta": round(extension["strict_completeness_mean"] - base["strict_completeness_mean"], 3),
            "experimental_mean_delta": round(extension["experimental_completeness_mean"] - base["experimental_completeness_mean"], 3),
            "strict_retention": round(extension["strict_completeness_mean"] / base["strict_completeness_mean"], 4) if base["strict_completeness_mean"] else None,
            "experimental_retention": round(extension["experimental_completeness_mean"] / base["experimental_completeness_mean"], 4) if base["experimental_completeness_mean"] else None,
        },
        "claim_boundary": "Completeness, not correctness. Strict evidence passes the current publication policy; experimental evidence is rights- or qualification-pending. Hidden exact-entity labels remain a separate accuracy gate.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.rows_output:
        Path(args.rows_output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in scored), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
