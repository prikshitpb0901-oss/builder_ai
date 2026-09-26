#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def observation(profile: dict) -> dict | None:
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not identity.get("publishable"):
        return None
    source_url = value.get("final_url") or website.get("source_url")
    digest = value.get("content_sha256") or website.get("content_sha256")
    if not source_url or not digest or len(str(digest)) != 64:
        return None
    pages = value.get("pages") or []
    socials = value.get("social_links") or []
    org = str(profile["organisation_number"])
    return {
        "id": f"company-site-activity-{org}-{str(digest)[:16]}",
        "organisation_number": org,
        "platform": "company_site",
        "signal_type": "profile_metrics",
        "source_url": source_url,
        "retrieved_at": website.get("retrieved_at"),
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": list(identity.get("promotion_proof") or []) + [
            {"type": "website_identity_gate", "status": identity.get("status"), "score": identity.get("score")}
        ],
        "acquisition_mode": "permitted_public_page",
        "rights_status": "approved",
        "source_class": "company_site",
        "evidence_span": f"Exact company site snapshot with {len(pages)} bounded pages and {len(socials)} verified social links.",
        "metrics": {
            "bounded_pages_captured": len(pages),
            "verified_social_links": len(socials),
            "structured_organisation_records": len(value.get("structured_organisations") or []),
            "extraction_state": value.get("extraction_state"),
            "interpretation": "Observed site-surface completeness; not audience traffic or popularity.",
        },
        "strategy": "company_site_activity",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract reproducible activity metrics from exact company-site snapshots.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    observations = [item for profile in rows if (item := observation(profile))]
    Path(args.output).write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in observations), encoding="utf-8")
    report = {
        "connector": "exact_company_site_activity_v1",
        "profiles": len(rows),
        "observations": len(observations),
        "claim_boundary": "Site-surface completeness only; this is not traffic, engagement, sentiment, or independent buzz.",
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
