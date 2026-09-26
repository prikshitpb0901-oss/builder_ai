#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.research import answer_profile, screen_profiles  # noqa: E402
from norway_company_agent.workspace import empty_workspace, record_screen, save_workspace  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    by_org = {row["organisation_number"]: row for row in rows}
    suite = json.loads(Path(args.suite).read_text(encoding="utf-8"))

    single_spec = suite["single_company"]
    single = answer_profile(by_org[single_spec["organisation_number"]], single_spec["question"])
    single_supported = len(single["facts"]) >= single_spec["minimum_facts"] and all(
        item.get("source_url") and item.get("retrieved_at") and item.get("content_sha256") for item in single["facts"]
    )

    screen_results = []
    exact_screens = 0
    exact_plans = 0
    export_supported = True
    workspace = empty_workspace()
    for spec in suite["screens"]:
        result = screen_profiles(rows, spec["query"])
        actual = [item["organisation_number"] for item in result["results"]]
        exact = actual == spec["expected_organisation_numbers"]
        plan_fields = [item["field"] for item in result["plan"]["filters"]]
        plan_exact = plan_fields == spec["expected_filter_fields"]
        citations_complete = all(
            citation.get("source_url") and citation.get("retrieved_at") and citation.get("content_sha256")
            for item in result["results"] for citation in item["citations"]
        )
        exact_screens += int(exact)
        exact_plans += int(plan_exact)
        export_supported = export_supported and citations_complete
        workspace = record_screen(workspace, result, pin_organisations=actual[:1])
        screen_results.append({"query": spec["query"], "expected": spec["expected_organisation_numbers"], "actual": actual, "exact_membership_and_order": exact, "exact_plan": plan_exact, "citations_complete": citations_complete})
    save_workspace(args.workspace, workspace)
    saved_work = Path(args.workspace).exists() and len(workspace["history"]) == len(suite["screens"]) and bool(workspace["pins"])

    unsupported = screen_profiles(rows, suite["unsupported"]["query"])
    abstention = unsupported["abstained"] is suite["unsupported"]["must_abstain"]
    screen_rate = exact_screens / len(suite["screens"])
    plan_rate = exact_plans / len(suite["screens"])
    points = {
        "cited_single_company_qa": 4 if single_supported else 0,
        "cross_company_screening": 3 * screen_rate,
        "exact_inspectable_filters": 2 * plan_rate,
        "unsupported_abstention": 1 if abstention else 0,
        "saved_history": 1 if saved_work else 0,
        "provenance_export": 1 if export_supported else 0,
    }
    report = {
        "corpus": suite["corpus"],
        "profiles": len(rows),
        "questions": 1 + len(suite["screens"]) + 1,
        "single_company_supported": single_supported,
        "screen_results": screen_results,
        "unsupported_abstention": abstention,
        "saved_work": saved_work,
        "provenance_export": export_supported,
        "points": points,
        "score": sum(points.values()),
        "maximum": 12,
        "qualification_passed": single_supported and screen_rate == 1 and plan_rate == 1 and abstention and saved_work and export_supported,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "screen_results"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["qualification_passed"] else 1)


if __name__ == "__main__":
    main()
