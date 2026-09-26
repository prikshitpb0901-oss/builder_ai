#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply independently verified website seeds before the normal crawl and identity gate."
    )
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--seeds", required=True, help="JSON array containing organisation_number, website and proof_url")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    profiles = read_jsonl(Path(args.profiles))
    seed_rows = json.loads(Path(args.seeds).read_text(encoding="utf-8"))
    seeds = {str(row["organisation_number"]): row for row in seed_rows}
    profile_orgs = {str(row["organisation_number"]) for row in profiles}
    unknown = sorted(set(seeds) - profile_orgs)
    if unknown:
        raise ValueError(f"verified seeds contain unknown organisations: {unknown}")

    applied = []
    for profile in profiles:
        org = str(profile["organisation_number"])
        seed = seeds.get(org)
        if not seed:
            continue
        website = str(seed.get("website") or "").strip()
        proof_url = str(seed.get("proof_url") or "").strip()
        if not website.startswith(("https://", "http://")) or not proof_url.startswith(("https://", "http://")):
            raise ValueError(f"seed {org} needs absolute website and proof_url")
        profile["website"] = website
        profile["website_seed_source"] = "independently_verified_exact_entity"
        profile["website_seed_proof"] = {
            "proof_url": proof_url,
            "proof": str(seed.get("proof") or ""),
        }
        applied.append({"organisation_number": org, "website": website, "proof_url": proof_url})

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in profiles), encoding="utf-8")
    report = {
        "profiles": len(profiles),
        "verified_seeds": len(seed_rows),
        "applied": len(applied),
        "rows": applied,
        "claim_boundary": "Seeds are candidates; publication still requires the normal fetched-page exact-entity gate.",
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, indent=2))


if __name__ == "__main__":
    main()
