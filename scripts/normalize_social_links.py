#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.website import normalize_social_url, structured_social_links  # noqa: E402
from norway_company_agent.identity import assess_social_identity  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    path = Path(args.input)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    before = after = 0
    for row in rows:
        value = row.get("evidence", {}).get("website", {}).get("value") or {}
        links = list(value.get("discovered_social_links") or value.get("social_links") or [])
        links.extend(structured_social_links(value.get("structured_organisations") or []))
        before += len(links)
        normalized = [item for item in (normalize_social_url(link.get("url", "")) for link in links) if item]
        value["discovered_social_links"] = list({(item["platform"], item["url"]): item for item in normalized}.values())
        publishable = value.get("identity_assessment", {}).get("publishable", True)
        assessments = [assess_social_identity(row, link) for link in value["discovered_social_links"]]
        value["social_link_assessments"] = assessments
        value["social_links"] = [
            {"platform": item["platform"], "url": item["url"]}
            for item in assessments if publishable and item["publishable"]
        ]
        after += len(value["discovered_social_links"])
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)
    print(json.dumps({"profiles": len(rows), "links_before": before, "links_after": after, "removed_or_deduplicated": before - after}))


if __name__ == "__main__":
    main()
