#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.refresh import diff_datasets  # noqa: E402
from norway_company_agent.snapshots import SnapshotFetcher  # noqa: E402


def materialize(base_profiles: list[dict], snapshot: dict, modules: set[str]) -> tuple[list[dict], SnapshotFetcher]:
    fetcher = SnapshotFetcher(snapshot)
    rows = copy.deepcopy(base_profiles)
    for row in rows:
        records, _ = fetch_official_modules(row["organisation_number"], modules, fetcher=fetcher)
        row.setdefault("evidence", {}).update(records)
        live = records.get("registry_live", {})
        if live.get("status") == "available":
            value = live.get("value") or {}
            for source, target in (("name", "name"), ("legal_form", "legal_form"), ("employees", "employees"), ("website", "website"), ("latest_submitted_accounts", "latest_submitted_accounts")):
                if source in value:
                    row[target] = value[source]
    return rows, fetcher


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay evaluator-owned old/new source bytes through production normalizers")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    modules = set(manifest["modules"])
    base = manifest["profiles"]
    previous, old_fetcher = materialize(base, manifest["snapshots"]["old"], modules)
    current, new_fetcher = materialize(base, manifest["snapshots"]["new"], modules)
    changes = diff_datasets(previous, current)
    observed = {(item["organisation_number"], item["field"]) for item in changes}
    expected = {(item["organisation_number"], item["field"]) for item in manifest.get("expected_changes", [])}
    true_positive = len(expected & observed)
    false_positive = len(observed - expected)
    false_negative = len(expected - observed)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    evidence_complete = all(item.get("source_url") and item.get("retrieved_at") and item.get("effective_at") and item.get("old_content_sha256") and item.get("new_content_sha256") for item in changes)
    idempotent = diff_datasets(current, current) == []
    report = {
        "corpus": manifest.get("corpus", "evaluator-owned snapshot replay"),
        "profiles": len(base),
        "modules": sorted(modules),
        "old_requests": len(old_fetcher.requests),
        "new_requests": len(new_fetcher.requests),
        "expected_changes": len(expected),
        "observed_changes": len(observed),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "evidence_complete": evidence_complete,
        "idempotent_rerun": idempotent,
        "qualification_passed": precision >= 0.95 and recall >= 0.95 and evidence_complete and idempotent,
        "events": changes,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "events"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["qualification_passed"] else 1)


if __name__ == "__main__":
    main()
