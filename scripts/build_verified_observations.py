#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def build(config: list[dict], profiles: list[dict]) -> list[dict]:
    names = {str(row["organisation_number"]): str(row["name"]) for row in profiles}
    output = []
    for seed in config:
        seed = dict(seed)
        org = str(seed["organisation_number"])
        if org not in names:
            raise ValueError(f"unknown organisation number: {org}")
        digest = str(seed.get("content_sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"invalid content hash for {org}")
        mode = seed.get("acquisition_mode") or "rights_review_experiment"
        proof = str(seed.pop("proof"))
        row = {
            **seed,
            "id": "verified-observation-" + hashlib.sha256(f"{org}|{seed['source_url']}|{seed['signal_type']}".encode()).hexdigest()[:24],
            "retrieved_at": "2026-08-23T05:28:01Z",
            "exact_entity": True,
            "identity_proof": [{"type": "manual_exact_entity_review", "legal_name": names[org], "basis": proof}],
            "acquisition_mode": mode,
            "rights_status": seed.get("rights_status") or "review_required",
            "source_class": seed.get("source_class") or "public_news",
            "strategy": seed.get("strategy") or "independent_news_discovery",
        }
        output.append(row)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build frozen observations from independently reviewed exact-entity sources.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    profiles = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    rows = build(config, profiles)
    Path(args.output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"observations": len(rows), "companies": len({row['organisation_number'] for row in rows})}, indent=2))


if __name__ == "__main__":
    main()
