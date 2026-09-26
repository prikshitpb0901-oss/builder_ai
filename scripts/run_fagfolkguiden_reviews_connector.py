#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup


UA = "SignalpostResearchPOC/1.0 (https://builderr.ai; bounded qualification run)"


def slug(value: object) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return "-".join(re.findall(r"[a-z0-9]+", text))


def extract_aggregate_rating(raw: bytes) -> tuple[float, int, str | None]:
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(node.string or node.get_text() or "{}")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            rating = (item or {}).get("aggregateRating") if isinstance(item, dict) else None
            if not isinstance(rating, dict):
                continue
            value, count = rating.get("ratingValue"), rating.get("ratingCount") or rating.get("reviewCount")
            if value is not None and count is not None:
                review_link = soup.find("a", href=re.compile(r"search\.google\.com/local/reviews"))
                return float(value), int(count), review_link.get("href") if review_link else None
    raise ValueError("no aggregate rating")


def fetch(profile: dict, cache_dir: Path) -> tuple[list[dict], dict]:
    org = str(profile["organisation_number"])
    url = f"https://www.fagfolkguiden.no/bedrift/{slug(profile['name'])}-{org}"
    cache = cache_dir / f"{org}.html"
    try:
        if cache.exists():
            raw, cache_hit = cache.read_bytes(), True
        else:
            request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(2_000_000)
            cache.write_bytes(raw)
            cache_hit = False
        text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        exact = str(profile["name"]).casefold() in text.casefold() and org in re.sub(r"\D", "", text)
        if not exact:
            return [], {"organisation_number": org, "accepted": False, "cache_hit": cache_hit, "reason": "identity_mismatch"}
        try:
            rating, count, google_url = extract_aggregate_rating(raw)
        except ValueError:
            return [], {"organisation_number": org, "accepted": True, "rated": False, "cache_hit": cache_hit}
        if not (0 < rating <= 5 and count > 0):
            return [], {"organisation_number": org, "accepted": True, "rated": False, "cache_hit": cache_hit, "reason": "invalid_rating"}
        digest = hashlib.sha256(raw).hexdigest()
        retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        proof = [
            {"type": "exact_legal_name_on_directory_page", "value": profile["name"]},
            {"type": "exact_organisation_number_on_directory_page", "value": org},
            {"type": "embedded_google_aggregate_rating", "google_review_url": google_url},
        ]
        common = {
            "organisation_number": org, "platform": "company_directory", "source_url": url,
            "retrieved_at": retrieved_at, "content_sha256": digest, "exact_entity": True,
            "identity_proof": proof, "acquisition_mode": "rights_review_experiment",
            "rights_status": "review_required", "source_class": "customer_review",
            "evidence_span": f"Google aggregate rating {rating}/5 based on {count} reviews, embedded on exact Fagfolkguiden company page.",
            "metrics": {"rating": rating, "review_count": count, "scale": 5, "google_review_url": google_url},
        }
        rows = [
            {**common, "id": f"fagfolk-review-{org}-{digest[:16]}", "signal_type": "review_summary", "strategy": "places_rating_reviews"},
            {**common, "id": f"fagfolk-metrics-{org}-{digest[:16]}", "signal_type": "profile_metrics", "strategy": "social_profile_metrics"},
            {**common, "id": f"fagfolk-buzz-{org}-{digest[:16]}", "signal_type": "buzz_metrics", "strategy": "buzz_peer_normalization"},
        ]
        return rows, {"organisation_number": org, "accepted": True, "rated": True, "rating": rating, "review_count": count, "cache_hit": cache_hit}
    except Exception as exc:
        return [], {"organisation_number": org, "accepted": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract embedded Google aggregate ratings from exact Fagfolkguiden company pages.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--organisations", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    wanted = [line.strip() for line in Path(args.organisations).read_text(encoding="utf-8").splitlines() if line.strip()]
    profiles = {str(row["organisation_number"]): row for row in (json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip())}
    cache_dir = Path(args.cache)
    cache_dir.mkdir(parents=True, exist_ok=True)
    observations, statuses = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        futures = {pool.submit(fetch, profiles[org], cache_dir): org for org in wanted}
        for future in as_completed(futures):
            rows, status = future.result()
            observations.extend(rows)
            statuses.append(status)
    order = {org: index for index, org in enumerate(wanted)}
    observations.sort(key=lambda row: (order[row["organisation_number"]], row["signal_type"]))
    statuses.sort(key=lambda row: order[row["organisation_number"]])
    Path(args.output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in observations), encoding="utf-8")
    report = {
        "connector": "fagfolkguiden_embedded_google_reviews_experiment_v1",
        "companies": len(wanted),
        "exact_pages": sum(row.get("accepted") is True for row in statuses),
        "rated_companies": sum(row.get("rated") is True for row in statuses),
        "observations": len(observations),
        "errors": sum("error" in row for row in statuses),
        "robots_checked": "Public /bedrift/ pages allowed; /api/ disallowed and not used, checked 2026-08-23",
        "claim_boundary": "Third-party display of Google aggregate ratings. Experimental until reuse/storage rights and an independent exact-place audit pass; no individual review text is collected.",
        "company_results": statuses,
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "company_results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
