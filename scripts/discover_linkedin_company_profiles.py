#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

try:
    from scripts.run_linkedin_guest_experiment import (
        canonical_company_url,
        extract_profile,
        fetch,
        legal_name_profile_url,
        normalized_company,
        registered_domain,
    )
    from scripts.run_linkedin_guest_jobs_connector import fetch as fetch_jobs
except ModuleNotFoundError:  # Direct `python scripts/...` execution.
    from run_linkedin_guest_experiment import (  # type: ignore[no-redef]
        canonical_company_url,
        extract_profile,
        fetch,
        legal_name_profile_url,
        normalized_company,
        registered_domain,
    )
    from run_linkedin_guest_jobs_connector import fetch as fetch_jobs  # type: ignore[no-redef]


LEGAL_SUFFIXES = {"as", "asa", "ba", "da", "enk", "nuf", "sa", "stiftelsen"}


def normalized_full_name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9æøå]+", str(value or "").casefold()))


def parse_exact_typeahead(raw: bytes, legal_name: str) -> list[dict]:
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    expected = normalized_full_name(legal_name)
    return [
        {"linkedin_company_id": str(item["id"]), "display_name": str(item.get("displayName") or "")}
        for item in payload
        if item.get("type") == "COMPANY"
        and item.get("id")
        and normalized_full_name(item.get("displayName")) == expected
    ]


def job_company_urls(raw: bytes) -> set[str]:
    soup = BeautifulSoup(raw, "html.parser")
    return {
        url
        for link in soup.select("h4.base-search-card__subtitle a[href]")
        if (url := canonical_company_url(str(link.get("href") or "")))
    }


def official_site_aliases(profile: dict) -> list[str]:
    website = (profile.get("evidence") or {}).get("website") or {}
    if website.get("status") != "available":
        return []
    value = website.get("value") or {}
    raw = [value.get("title")]
    raw.extend(item.get("name") for item in value.get("structured_organisations") or [] if isinstance(item, dict))
    aliases = []
    banned = {"home", "homepage", "forside", "welcome", "velkommen", "official site"}
    legal = normalized_full_name(profile.get("name"))
    for item in raw:
        candidate = re.split(r"\s+[|\u2013\u2014]\s+", str(item or ""), maxsplit=1)[0].strip()
        normalized = normalized_full_name(candidate)
        if not normalized or normalized in banned or normalized == legal or len(normalized) < 3 or len(normalized) > 80:
            continue
        if candidate not in aliases:
            aliases.append(candidate)
    return aliases[:3]


def profile_leaders(profile: dict) -> list[str]:
    roles = (((profile.get("evidence") or {}).get("roles") or {}).get("value") or {}).get("roles") or []
    priority = {"DAGL", "LEDE", "INNH"}
    return [str(item.get("name") or "") for item in roles if item.get("role_code") in priority and item.get("name")][:5]


def _profile_official_domain(profile: dict) -> str | None:
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    return registered_domain(value.get("final_url") or profile.get("website"))


def discovery_identity(profile: dict, linkedin_profile: dict, sources: set[str]) -> dict:
    legal_full = normalized_full_name(profile.get("name"))
    linkedin_full = normalized_full_name(linkedin_profile.get("name"))
    full_name_match = bool(legal_full and legal_full == linkedin_full)
    core_name_match = bool(
        normalized_company(profile.get("name"))
        and normalized_company(profile.get("name")) == normalized_company(linkedin_profile.get("name"))
    )
    official_domain = _profile_official_domain(profile)
    linkedin_domain = registered_domain(linkedin_profile.get("website"))
    domain_match = bool(official_domain and linkedin_domain and official_domain == linkedin_domain)
    municipality = normalized_full_name(profile.get("municipality"))
    linkedin_location = normalized_full_name(
        " ".join(
            str(item or "")
            for item in (
                linkedin_profile.get("headquarters"),
                json.dumps(linkedin_profile.get("address") or {}, ensure_ascii=False),
            )
        )
    )
    location_match = bool(municipality and municipality in linkedin_location)
    company_id_job_link = "company_id_job_link" in sources
    aliases = [item.split(":", 1)[1] for item in sources if item.startswith("official_site_alias:")]
    linkedin_name = normalized_full_name(linkedin_profile.get("name"))
    alias_match = any(
        normalized_full_name(alias) == linkedin_name
        or normalized_company(alias) == normalized_company(linkedin_profile.get("name"))
        for alias in aliases
    )
    linkedin_text = normalized_full_name(
        " ".join(
            [str(linkedin_profile.get("description") or "")]
            + [str(item.get("text") or "") for item in linkedin_profile.get("posts") or []]
        )
    )
    matched_leaders = [leader for leader in profile_leaders(profile) if normalized_full_name(leader) in linkedin_text]
    exact = bool(
        (core_name_match and domain_match)
        or (alias_match and domain_match)
        or (full_name_match and location_match)
        or (full_name_match and company_id_job_link)
        or (alias_match and location_match and matched_leaders)
    )
    return {
        "exact_entity": exact,
        "full_legal_name_match": full_name_match,
        "legal_name_core_match": core_name_match,
        "official_domain": official_domain,
        "linkedin_website_domain": linkedin_domain,
        "reverse_domain_match": domain_match,
        "municipality": profile.get("municipality"),
        "linkedin_headquarters": linkedin_profile.get("headquarters"),
        "location_match": location_match,
        "company_id_job_link": company_id_job_link,
        "official_site_aliases": aliases,
        "official_site_alias_match": alias_match,
        "matched_registered_leaders": matched_leaders,
        "method": "linkedin_typeahead_plus_domain_location_or_company_id_job_v1",
    }


class SnapshotCache:
    def __init__(self, path: Path):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def get(self, url: str, timeout: float, *, jobs: bool = False) -> tuple[bytes, str, str]:
        request_hash = hashlib.sha256(url.encode()).hexdigest()
        snapshot = self.path / f"{request_hash}.bin"
        if snapshot.exists():
            raw = snapshot.read_bytes()
        else:
            raw = fetch_jobs(url, timeout) if jobs else fetch(url, timeout)[0]
            with self.lock:
                if not snapshot.exists():
                    snapshot.write_bytes(raw)
        return raw, hashlib.sha256(raw).hexdigest(), str(snapshot)


def discover_one(profile: dict, cache: SnapshotCache, timeout: float, delay: float, fuzzy: bool = False) -> dict:
    org = str(profile["organisation_number"])
    legal_name = str(profile.get("name") or "")
    result = {"organisation_number": org, "name": legal_name, "typeahead": [], "candidates": [], "accepted": None, "errors": []}
    queries = [(legal_name, False)] + [(alias, True) for alias in official_site_aliases(profile) if fuzzy]
    for query, is_alias in queries:
        query_url = "https://www.linkedin.com/jobs-guest/api/typeaheadHits?" + urllib.parse.urlencode(
            {"typeaheadType": "COMPANY", "query": query}
        )
        try:
            raw, _, typeahead_snapshot = cache.get(query_url, timeout, jobs=True)
            for item in parse_exact_typeahead(raw, query):
                item["query"] = query
                item["official_site_alias"] = is_alias
                item["typeahead_snapshot_path"] = typeahead_snapshot
                result["typeahead"].append(item)
        except Exception as exc:
            result["errors"].append(f"typeahead {type(exc).__name__}: {str(exc)[:140]}")
    if not result["typeahead"]:
        return result

    candidates: dict[str, set[str]] = {}
    for query, is_alias in queries:
        query_profile_url = legal_name_profile_url(query)
        if query_profile_url:
            source = f"official_site_alias:{query}" if is_alias else "legal_name_slug"
            candidates.setdefault(canonical_company_url(query_profile_url) or query_profile_url, set()).add(source)
    for item in result["typeahead"][:2]:
        jobs_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urllib.parse.urlencode(
            {"f_C": item["linkedin_company_id"], "location": "Norway", "start": 0}
        )
        try:
            raw, _, snapshot = cache.get(jobs_url, timeout, jobs=True)
            for url in job_company_urls(raw):
                candidates.setdefault(url, set()).add("company_id_job_link")
                if item.get("official_site_alias"):
                    candidates[url].add(f"official_site_alias:{item['query']}")
            item["jobs_snapshot_path"] = snapshot
        except Exception as exc:
            result["errors"].append(f"company_id {item['linkedin_company_id']} {type(exc).__name__}: {str(exc)[:120]}")
        time.sleep(max(0, delay))

    for candidate, sources in candidates.items():
        candidate_row = {"url": candidate, "sources": sorted(sources), "accepted": False}
        try:
            snapshot = cache.path / f"{hashlib.sha256(candidate.encode()).hexdigest()}-profile.html"
            if snapshot.exists():
                raw = snapshot.read_bytes()
                final_url = candidate
            else:
                raw, final_url = fetch(candidate, timeout)
                with cache.lock:
                    snapshot.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            linkedin_profile = extract_profile(raw)
            identity = discovery_identity(profile, linkedin_profile, sources)
            candidate_row.update(
                {
                    "resolved_url": linkedin_profile.get("page_url") or canonical_company_url(final_url),
                    "profile": linkedin_profile,
                    "identity": identity,
                    "content_sha256": digest,
                    "snapshot_path": str(snapshot),
                    "accepted": identity["exact_entity"],
                }
            )
            if identity["exact_entity"]:
                result["accepted"] = candidate_row
                result["candidates"].append(candidate_row)
                break
        except urllib.error.HTTPError as exc:
            candidate_row["error"] = f"HTTP {exc.code}"
        except Exception as exc:
            candidate_row["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        result["candidates"].append(candidate_row)
        time.sleep(max(0, delay))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover exact LinkedIn company profiles from a frozen Norwegian company corpus.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--fuzzy", action="store_true", help="Also query exact aliases found on verified official websites.")
    args = parser.parse_args()
    profiles = [json.loads(line) for line in Path(args.profiles).read_text().splitlines() if line.strip()]
    cache = SnapshotCache(Path(args.cache_dir))
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(discover_one, profile, cache, args.timeout, args.delay, args.fuzzy): profile for profile in profiles}
        for index, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if index % 50 == 0:
                print(f"{index}/{len(profiles)} accepted={sum(bool(item['accepted']) for item in results)}", flush=True)
    results.sort(key=lambda item: next(i for i, profile in enumerate(profiles) if str(profile["organisation_number"]) == item["organisation_number"]))
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    observations = []
    for row in results:
        accepted = row.get("accepted")
        if not accepted:
            continue
        identity = accepted["identity"]
        profile_url = accepted["resolved_url"]
        observations.append(
            {
                "id": "linkedin-discovered-handle-" + hashlib.sha256(f"{row['organisation_number']}|{profile_url}".encode()).hexdigest()[:24],
                "organisation_number": row["organisation_number"],
                "platform": "linkedin",
                "signal_type": "profile_handle",
                "source_url": profile_url,
                "profile_url": profile_url,
                "linkedin_company_ids": sorted({item["linkedin_company_id"] for item in row.get("typeahead") or []}),
                "retrieved_at": retrieved_at,
                "content_sha256": accepted["content_sha256"],
                "exact_entity": True,
                "identity_proof": [{"type": "linkedin_discovery_identity", "value": identity}],
                "acquisition_mode": "unofficial_api_experiment",
                "rights_status": "experimental",
                "source_class": "professional_network",
                "strategy": "verified_handle_extraction",
            }
        )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in observations), encoding="utf-8")
    report = {
        "connector": "linkedin_exact_company_discovery_v1",
        "companies": len(profiles),
        "exact_typeahead_matches": sum(bool(item["typeahead"]) for item in results),
        "candidate_profiles": sum(len(item["candidates"]) for item in results),
        "accepted_profiles": len(observations),
        "fuzzy_alias_mode": args.fuzzy,
        "results": results,
        "publishable": False,
        "claim_boundary": "Discovery output is experimental pending LinkedIn source-rights approval; exact-entity gates remain mandatory.",
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))


if __name__ == "__main__":
    main()
