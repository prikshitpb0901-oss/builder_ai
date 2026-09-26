#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


LEGAL_STOP = {
    "as", "asa", "sa", "enk", "nuf", "da", "ans", "ba", "stiftelsen", "sameiet",
    "avd", "avdeling",
}
GENERIC_MAP_TOKENS = {"holding", "eiendom", "invest", "bolig", "service", "drift", "gruppen", "group"}
VERIFIED_TRADE_NAMES = {
    ("932083108", "privatmegleren premium"): "https://www.proff.no/selskap/privatmegleren-premium/oslo/eiendomsmegling/IFEXRXG00B1",
    ("963430663", "mobit kanalveien"): "https://www.mobit.no/forhandlere/bergen/kanalveien",
    ("967292907", "lunsj service og orebekk fisk vilt"): "https://www.starina.no/",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tokens(value: object) -> list[str]:
    normalized = str(value or "").casefold()
    normalized = normalized.translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))
    return re.findall(r"[a-z0-9]+", normalized)


def meaningful_name_tokens(value: object) -> list[str]:
    return [token for token in tokens(value) if token not in LEGAL_STOP and (len(token) >= 3 or token.isdigit())]


def normalized_phone(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("47") and len(digits) > 8:
        digits = digits[2:]
    return digits[-8:]


def registered_domain(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = (urllib.parse.urlparse(raw).hostname or "").casefold().removeprefix("www.")
    return host


def candidate_score(profile: dict, candidate: dict) -> dict:
    registry = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
    target = set(meaningful_name_tokens(profile.get("name")))
    title = set(meaningful_name_tokens(candidate.get("title")))
    overlap = len(target & title)
    name_score = overlap / len(target) if target else 0.0
    if target and target.issubset(title):
        name_score = 1.0

    registry_street = set(tokens(registry.get("forretningsadresse.adresse")))
    candidate_address = set(tokens(candidate.get("address")))
    postcode = str(registry.get("forretningsadresse.postnummer") or "")
    registry_city = set(tokens(registry.get("forretningsadresse.poststed")))
    address_match = bool(
        registry_street
        and registry_street.issubset(candidate_address)
        and (not postcode or postcode in candidate_address)
    )
    postcode_city_match = bool(
        postcode and postcode in candidate_address
        and (not registry_city or registry_city.issubset(candidate_address))
    )
    registry_phone = normalized_phone(registry.get("telefon") or registry.get("mobil"))
    candidate_phone = normalized_phone(candidate.get("phone"))
    phone_match = bool(len(registry_phone) >= 5 and registry_phone == candidate_phone)

    website = ((profile.get("evidence") or {}).get("website") or {}).get("value") or {}
    identity = website.get("identity_assessment") or {}
    exact_site = website.get("final_url") if identity.get("publishable") else ""
    if not exact_site and identity.get("publishable"):
        exact_site = ((profile.get("evidence") or {}).get("website") or {}).get("source_url")
    expected_domain = registered_domain(exact_site)
    candidate_domain = registered_domain(candidate.get("web_site"))
    website_match = bool(
        expected_domain and candidate_domain
        and (candidate_domain == expected_domain or candidate_domain.endswith("." + expected_domain))
    )
    target_name = " ".join(meaningful_name_tokens(profile.get("name")))
    candidate_name = " ".join(meaningful_name_tokens(candidate.get("title")))
    fuzzy_name_score = difflib.SequenceMatcher(None, target_name, candidate_name).ratio() if target_name and candidate_name else 0.0
    strong_channels = sum((address_match or postcode_city_match, phone_match, website_match))
    accepted = bool((name_score >= 0.99 and strong_channels >= 1) or (name_score >= 0.66 and strong_channels >= 2))
    trade_key = (str(profile.get("organisation_number") or ""), " ".join(tokens(candidate.get("title"))))
    trade_name_source = VERIFIED_TRADE_NAMES.get(trade_key)
    # Shared group addresses and phone numbers are not enough: every trading
    # name must be independently corroborated to the exact organisation.
    trade_name_match = bool(name_score == 0 and address_match and phone_match and trade_name_source)
    verified_domain_fuzzy_name_match = bool(website_match and fuzzy_name_score >= 0.85)
    accepted = accepted or trade_name_match or verified_domain_fuzzy_name_match
    weak_generic_name = len(target) == 1 and next(iter(target), "") in GENERIC_MAP_TOKENS
    if weak_generic_name and not (address_match or phone_match or website_match):
        accepted = False
    score = name_score * 4 + address_match * 3 + phone_match * 2 + website_match * 3
    score += postcode_city_match * 2
    score += min(1.0, math.log10(max(1, int(candidate.get("review_count") or 0))) / 5)
    return {
        "accepted": accepted,
        "score": round(score, 4),
        "name_score": round(name_score, 4),
        "address_match": address_match,
        "postcode_city_match": postcode_city_match,
        "weak_generic_name": weak_generic_name,
        "phone_match": phone_match,
        "website_match": website_match,
        "trade_name_match": trade_name_match,
        "trade_name_source": trade_name_source,
        "verified_domain_fuzzy_name_match": verified_domain_fuzzy_name_match,
        "fuzzy_name_score": round(fuzzy_name_score, 4),
        "target_name_tokens": sorted(target),
        "candidate_name_tokens": sorted(title),
    }


def review_label(rating: int | float) -> str:
    if rating <= 2:
        return "negative"
    if rating >= 4:
        return "positive"
    return "neutral"


def result_hash(candidate: dict) -> str:
    return hashlib.sha256(json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalize_company(profile: dict, candidates: list[dict], retrieved_at: str) -> tuple[list[dict], dict]:
    unique = {}
    for item in candidates:
        key = str(item.get("place_id") or item.get("data_id") or item.get("link") or result_hash(item))
        unique[key] = item
    candidates = list(unique.values())
    assessed = [{"candidate": item, "assessment": candidate_score(profile, item)} for item in candidates]
    accepted = [item for item in assessed if item["assessment"]["accepted"]]
    accepted.sort(key=lambda item: (-item["assessment"]["score"], str(item["candidate"].get("data_id") or "")))
    if not accepted:
        return [], {"status": "no_exact_match", "candidates": len(candidates)}
    if len(accepted) > 1 and accepted[0]["assessment"]["score"] == accepted[1]["assessment"]["score"]:
        return [], {"status": "ambiguous_exact_match", "candidates": len(candidates)}

    chosen = accepted[0]
    candidate = chosen["candidate"]
    assessment = chosen["assessment"]
    digest = result_hash(candidate)
    org = str(profile["organisation_number"])
    source_url = str(candidate.get("link") or "")
    proof = [
        {"type": "maps_title_name_score", "value": assessment["name_score"]},
        {"type": "registry_address_match", "value": assessment["address_match"]},
        {"type": "registry_postcode_city_match", "value": assessment["postcode_city_match"]},
        {"type": "registry_phone_match", "value": assessment["phone_match"]},
        {"type": "verified_website_domain_match", "value": assessment["website_match"]},
        {"type": "independently_verified_trade_name", "value": assessment.get("trade_name_match"), "source_url": assessment.get("trade_name_source")},
    ]
    common = {
        "organisation_number": org,
        "platform": "google_places",
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": proof,
        "acquisition_mode": "unofficial_api_experiment",
        "rights_status": "review_required",
    }
    evidence = (
        f"{candidate.get('title')}; {candidate.get('address')}; {candidate.get('phone')}; "
        f"rating={candidate.get('review_rating')}; reviews={candidate.get('review_count')}"
    )
    observations = [{
        **common,
        "id": f"gmaps-place-{org}-{candidate.get('place_id') or digest[:16]}",
        "signal_type": "place_summary",
        "source_class": "public_business_listing",
        "evidence_span": evidence,
        "metrics": {
            "title": candidate.get("title"),
            "address": candidate.get("address"),
            "phone": candidate.get("phone"),
            "website": candidate.get("web_site"),
            "place_id": candidate.get("place_id"),
            "data_id": candidate.get("data_id"),
            "latitude": candidate.get("latitude"),
            "longitude": candidate.get("longitude", candidate.get("longtitude")),
        },
        "strategy": "places_identity_resolution",
    }]
    if candidate.get("web_site"):
        observations.append({
            **common,
            "id": f"gmaps-company-profile-{org}-{candidate.get('place_id') or digest[:16]}",
            "signal_type": "company_profile",
            "source_class": "public_business_listing",
            "evidence_span": evidence,
            "metrics": {
                "official_website_candidate": candidate.get("web_site"),
                "title": candidate.get("title"),
                "place_id": candidate.get("place_id"),
            },
            "strategy": "company_site_identity",
        })
    rating = float(candidate.get("review_rating") or 0)
    count = int(candidate.get("review_count") or 0)
    if count > 0 and 0 < rating <= 5:
        observations.append({
            **common,
            "id": f"gmaps-review-summary-{org}-{candidate.get('place_id') or digest[:16]}",
            "signal_type": "review_summary",
            "source_class": "customer_review_summary",
            "evidence_span": evidence,
            "metrics": {
                "rating": rating,
                "rating_scale": 5,
                "review_count": count,
                "reviews_per_rating": candidate.get("reviews_per_rating") or {},
                "place_id": candidate.get("place_id"),
            },
            "strategy": "places_rating_reviews",
        })
        observations.append({
            **common,
            "id": f"gmaps-profile-metrics-{org}-{candidate.get('place_id') or digest[:16]}",
            "signal_type": "profile_metrics",
            "source_class": "public_business_listing",
            "evidence_span": evidence,
            "metrics": {
                "rating": rating,
                "rating_scale": 5,
                "review_count": count,
                "place_id": candidate.get("place_id"),
            },
            "strategy": "social_profile_metrics",
        })
        observations.append({
            **common,
            "id": f"gmaps-buzz-{org}-{candidate.get('place_id') or digest[:16]}",
            "signal_type": "buzz_metrics",
            "source_class": "customer_review_summary",
            "evidence_span": evidence,
            "metrics": {
                "review_count": count,
                "rating": rating,
                "rating_scale": 5,
                "place_id": candidate.get("place_id"),
            },
            "strategy": "buzz_peer_normalization",
        })
    for index, review in enumerate(candidate.get("user_reviews") or []):
        star = float(review.get("rating_float") or review.get("Rating") or 0)
        if not 0 < star <= 5:
            continue
        review_id = str(review.get("review_id") or hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[:20])
        text = str(review.get("text_original") or review.get("Description") or "").strip()
        observations.append({
            **common,
            "id": f"gmaps-review-{org}-{review_id}",
            "signal_type": "review",
            "source_class": "customer_review",
            "evidence_span": text[:1200] if text else f"Explicit customer rating: {star}/5",
            "published_at": review.get("published_at"),
            "reviewer_id": str(review.get("author_url") or review.get("Name") or f"anonymous-{index}"),
            "metrics": {"rating": star, "rating_scale": 5, "review_id": review_id},
            "sentiment_label": review_label(star),
            "sentiment_model_version": "explicit_star_rating_v1",
            "strategy": "independent_sentiment",
        })
    return observations, {
        "status": "exact_match",
        "candidates": len(candidates),
        "title": candidate.get("title"),
        "review_count": count,
        "review_rating": rating,
        "assessment": assessment,
        "place_id": candidate.get("place_id"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Identity-gate and normalize Google Maps scraper JSONL.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--organisations", required=True)
    parser.add_argument("--raw-results", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    wanted = [line.strip() for line in Path(args.organisations).read_text().splitlines() if line.strip()]
    profiles = {str(row["organisation_number"]): row for row in read_jsonl(Path(args.profiles))}
    raw = [item for source in args.raw_results for item in read_jsonl(Path(source))]
    by_org: dict[str, list[dict]] = defaultdict(list)
    for row in raw:
        by_org[str(row.get("input_id") or "")].append(row)
    retrieved_at = utc_now()
    observations: list[dict] = []
    company_results = []
    for org in wanted:
        company_observations, result = normalize_company(profiles[org], by_org.get(org, []), retrieved_at)
        observations.extend(company_observations)
        company_results.append({"organisation_number": org, **result})
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in observations), encoding="utf-8")
    statuses = defaultdict(int)
    for item in company_results:
        statuses[item["status"]] += 1
    report = {
        "connector": "google_maps_unofficial_crawler_identity_gate_v1",
        "companies": len(wanted),
        "raw_results": len(raw),
        "exact_matches": statuses["exact_match"],
        "companies_with_ratings": sum(item.get("review_count", 0) > 0 for item in company_results),
        "review_observations": sum(item.get("signal_type") == "review" for item in observations),
        "observations": len(observations),
        "status_counts": dict(statuses),
        "company_results": company_results,
        "claim_boundary": "Technically verified experimental output from an unofficial Google Maps crawler; terms review remains required.",
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "company_results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
