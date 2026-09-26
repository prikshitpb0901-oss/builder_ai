#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    path = Path(args.input)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    statuses = Counter()
    quarantined_links = 0
    for row in rows:
        website = row.get("evidence", {}).get("website", {})
        if website.get("status") != "available":
            continue
        gated = apply_website_identity_gate(row, website)
        assessment = gated["assessment"]
        quarantined_links += gated["quarantined_social_links"]
        statuses[assessment["status"]] += 1
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)
    print(json.dumps({"profiles": len(rows), "website_identity_statuses": dict(statuses), "quarantined_social_links": quarantined_links}, ensure_ascii=False))


if __name__ == "__main__":
    main()
