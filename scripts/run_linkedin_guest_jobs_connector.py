#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup


USER_AGENT = "Mozilla/5.0 (compatible; SignalpostResearch/1.0)"
LEGAL_SUFFIXES = {"as", "asa", "ba", "da", "enk", "nuf", "sa", "stiftelsen"}


def normalized_company(value: str) -> str:
    words = re.findall(r"[a-z0-9æøå]+", unquote(str(value or "")).casefold())
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def canonical_company_url(value: str) -> str | None:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").casefold()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    parts = [unquote(item).strip() for item in parsed.path.split("/") if item.strip()]
    if len(parts) < 2 or parts[0].casefold() != "company":
        return None
    slug = parts[1].casefold()
    if not slug:
        return None
    return f"https://linkedin.com/company/{quote(slug, safe='-_.~')}"


def fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.8,no;q=0.6",
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read(2_000_000)


def parse_job_cards(raw: bytes, expected_company_url: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(raw, "html.parser")
    cards = soup.select("div.base-search-card")
    exact = []
    for card in cards:
        company_link = card.select_one("h4.base-search-card__subtitle a")
        company_url = canonical_company_url(company_link.get("href", "") if company_link else "")
        if company_url != expected_company_url:
            continue
        job_link = card.select_one("a.base-card__full-link")
        job_url = str(job_link.get("href") or "") if job_link else ""
        urn = str(card.get("data-entity-urn") or "")
        job_id_match = re.search(r"(\d{6,})", urn) or re.search(r"-(\d{6,})(?:[/?]|$)", job_url)
        if not job_id_match:
            continue
        job_id = job_id_match.group(1)
        title = card.select_one("span.sr-only")
        company = company_link.get_text(" ", strip=True) if company_link else ""
        location = card.select_one("span.job-search-card__location")
        posted = card.select_one("time")
        exact.append(
            {
                "job_id": job_id,
                "job_url": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title.get_text(" ", strip=True) if title else "",
                "company": company,
                "company_url": company_url,
                "location": location.get_text(" ", strip=True) if location else "",
                "date_posted": str(posted.get("datetime") or "") if posted else "",
            }
        )
    return exact, len(cards)


def parse_typeahead(raw: bytes, legal_name: str) -> list[dict]:
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    legal_core = normalized_company(legal_name)
    return [
        {
            "linkedin_company_id": str(item.get("id") or ""),
            "display_name": str(item.get("displayName") or ""),
            "exact_legal_name_core": normalized_company(item.get("displayName")) == legal_core,
        }
        for item in payload
        if item.get("type") == "COMPANY" and item.get("id")
    ]


def parse_detail_company_urls(raw: bytes) -> set[str]:
    soup = BeautifulSoup(raw, "html.parser")
    return {
        canonical
        for link in soup.select('a[href*="linkedin.com/company/"]')
        if (canonical := canonical_company_url(str(link.get("href") or "")))
    }


def frozen_fetch(url: str, cache_dir: Path, timeout: float) -> tuple[bytes, str, str]:
    raw = fetch(url, timeout)
    content_hash = hashlib.sha256(raw).hexdigest()
    request_hash = hashlib.sha256(url.encode()).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = cache_dir / f"{request_hash}.bin"
    if not snapshot_path.exists():
        snapshot_path.write_bytes(raw)
    return raw, content_hash, str(snapshot_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl LinkedIn's logged-out jobs surface and retain only exact verified company-handle matches."
    )
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--handles", required=True)
    parser.add_argument("--organisations", help="Optional newline-separated organisation numbers; defaults to all profiles.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    profiles = {
        str(row["organisation_number"]): row
        for row in (json.loads(line) for line in Path(args.profiles).read_text().splitlines() if line.strip())
    }
    wanted = (
        {line.strip() for line in Path(args.organisations).read_text().splitlines() if line.strip()}
        if args.organisations
        else set(profiles)
    )
    handles = [
        row
        for row in (json.loads(line) for line in Path(args.handles).read_text().splitlines() if line.strip())
        if row.get("platform") == "linkedin" and str(row.get("organisation_number")) in wanted
    ]
    observations = []
    company_rows = []
    seen_jobs: set[tuple[str, str]] = set()
    cache_dir = Path(args.cache_dir)
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for index, handle in enumerate(handles, 1):
        org = str(handle["organisation_number"])
        profile = profiles[org]
        expected_url = canonical_company_url(str(handle.get("profile_url") or handle.get("source_url") or ""))
        row = {
            "organisation_number": org,
            "name": profile["name"],
            "profile_url": expected_url,
            "pages_requested": 0,
            "candidate_cards": 0,
            "exact_jobs": 0,
            "typeahead_candidates": [],
            "confirmed_linkedin_company_id": None,
            "errors": [],
        }
        if not expected_url:
            row["errors"].append("invalid LinkedIn company handle")
            company_rows.append(row)
            continue

        query = normalized_company(str(profile["name"])) or str(profile["name"])
        typeahead_url = "https://www.linkedin.com/jobs-guest/api/typeaheadHits?" + urlencode(
            {"typeaheadType": "COMPANY", "query": query}
        )
        try:
            raw, _, _ = frozen_fetch(typeahead_url, cache_dir, args.timeout)
            row["typeahead_candidates"] = parse_typeahead(raw, str(profile["name"]))
        except Exception as exc:
            row["errors"].append(f"typeahead {type(exc).__name__}: {str(exc)[:160]}")
        time.sleep(max(0, args.delay))

        jobs_before_handle = len(seen_jobs)
        company_ids = sorted(
            set(str(item) for item in handle.get("linkedin_company_ids") or [] if item)
            | {
                item["linkedin_company_id"]
                for item in row["typeahead_candidates"]
                if item["exact_legal_name_core"]
            }
        )
        for company_id in company_ids:
            company_jobs_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode(
                {"f_C": company_id, "location": "Norway", "start": 0}
            )
            try:
                raw, content_hash, snapshot_path = frozen_fetch(company_jobs_url, cache_dir, args.timeout)
                exact, candidates = parse_job_cards(raw, expected_url)
                row["pages_requested"] += 1
                row["candidate_cards"] += candidates
                for job in exact:
                    key = (org, job["job_id"])
                    if key in seen_jobs:
                        continue
                    seen_jobs.add(key)
                    evidence = " — ".join(item for item in (job["title"], job["company"], job["location"]) if item)
                    observations.append(
                        {
                            "id": "linkedin-guest-job-" + hashlib.sha256(f"{org}|{job['job_id']}".encode()).hexdigest()[:24],
                            "organisation_number": org,
                            "platform": "linkedin",
                            "signal_type": "job_posting",
                            "source_url": job["job_url"],
                            "retrieved_at": retrieved_at,
                            "content_sha256": content_hash,
                            "exact_entity": True,
                            "identity_proof": list(handle.get("identity_proof") or [])
                            + [
                                {"type": "exact_linkedin_company_url_match", "value": expected_url},
                                {"type": "linkedin_company_id_confirmed_by_exact_job_company_url", "value": company_id},
                            ],
                            "acquisition_mode": "jobspy_experiment",
                            "rights_status": "experimental",
                            "source_class": "job_board",
                            "evidence_span": evidence,
                            "metrics": {**job, "linkedin_company_id": company_id, "search_snapshot_path": snapshot_path},
                            "strategy": "jobs_feed_discovery",
                        }
                    )
                if exact:
                    row["confirmed_linkedin_company_id"] = company_id
                    break
            except Exception as exc:
                row["errors"].append(f"company id {company_id} {type(exc).__name__}: {str(exc)[:120]}")
            time.sleep(max(0, args.delay))

        for page in range(max(1, args.pages)):
            search_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode(
                {"keywords": query, "location": "Norway", "start": page * 10}
            )
            try:
                raw, content_hash, snapshot_path = frozen_fetch(search_url, cache_dir, args.timeout)
                exact, candidates = parse_job_cards(raw, expected_url)
                row["pages_requested"] += 1
                row["candidate_cards"] += candidates
                for job in exact:
                    key = (org, job["job_id"])
                    if key in seen_jobs:
                        continue
                    seen_jobs.add(key)
                    evidence = " — ".join(item for item in (job["title"], job["company"], job["location"]) if item)
                    observations.append(
                        {
                            "id": "linkedin-guest-job-" + hashlib.sha256(f"{org}|{job['job_id']}".encode()).hexdigest()[:24],
                            "organisation_number": org,
                            "platform": "linkedin",
                            "signal_type": "job_posting",
                            "source_url": job["job_url"],
                            "retrieved_at": retrieved_at,
                            "content_sha256": content_hash,
                            "exact_entity": True,
                            "identity_proof": list(handle.get("identity_proof") or [])
                            + [{"type": "exact_linkedin_company_url_match", "value": expected_url}],
                            "acquisition_mode": "jobspy_experiment",
                            "rights_status": "experimental",
                            "source_class": "job_board",
                            "evidence_span": evidence,
                            "metrics": {**job, "search_snapshot_path": snapshot_path},
                            "strategy": "jobs_feed_discovery",
                        }
                    )
                if candidates == 0:
                    break
            except Exception as exc:
                row["errors"].append(f"jobs page {page} {type(exc).__name__}: {str(exc)[:160]}")
                break
            time.sleep(max(0, args.delay))

        if len(seen_jobs) > jobs_before_handle:
            for candidate in row["typeahead_candidates"]:
                if not candidate["exact_legal_name_core"]:
                    continue
                company_id = candidate["linkedin_company_id"]
                company_jobs_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode(
                    {"f_C": company_id, "location": "Norway", "start": 0}
                )
                try:
                    raw, _, _ = frozen_fetch(company_jobs_url, cache_dir, args.timeout)
                    confirmed_jobs, _ = parse_job_cards(raw, expected_url)
                    if confirmed_jobs:
                        row["confirmed_linkedin_company_id"] = company_id
                        for item in observations:
                            if item["organisation_number"] == org and (item.get("metrics") or {}).get("company_url") == expected_url:
                                item["metrics"]["linkedin_company_id"] = company_id
                                item["identity_proof"].append(
                                    {"type": "linkedin_company_id_confirmed_by_exact_job_company_url", "value": company_id}
                                )
                        break
                except Exception as exc:
                    row["errors"].append(f"company id {company_id} {type(exc).__name__}: {str(exc)[:120]}")
                time.sleep(max(0, args.delay))

        row["exact_jobs"] = len(seen_jobs) - jobs_before_handle
        company_rows.append(row)
        print(f"{index}/{len(handles)} {org} exact_jobs={row['exact_jobs']}", flush=True)

    detail_verified = []
    detail_errors = []
    for item in observations:
        job_id = str((item.get("metrics") or {}).get("job_id") or "")
        expected_url = str((item.get("metrics") or {}).get("company_url") or "")
        detail_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
        try:
            raw, content_hash, snapshot_path = frozen_fetch(detail_url, cache_dir, args.timeout)
            if expected_url not in parse_detail_company_urls(raw):
                raise RuntimeError("job detail does not link the expected exact company handle")
            item["source_url"] = detail_url
            item["content_sha256"] = content_hash
            item["metrics"]["detail_snapshot_path"] = snapshot_path
            item["identity_proof"].append({"type": "job_detail_exact_company_url_match", "value": expected_url})
            detail_verified.append(item)
        except Exception as exc:
            detail_errors.append(
                {"organisation_number": item["organisation_number"], "job_id": job_id, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            )
        time.sleep(max(0, args.delay))
    observations = detail_verified
    for row in company_rows:
        row["exact_jobs"] = sum(
            item["organisation_number"] == row["organisation_number"]
            and (item.get("metrics") or {}).get("company_url") == row["profile_url"]
            for item in observations
        )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in observations), encoding="utf-8"
    )
    report = {
        "connector": "linkedin_guest_exact_handle_jobs_v1",
        "companies_requested": len(wanted),
        "verified_linkedin_handles": len(handles),
        "companies_with_exact_jobs": len({item["organisation_number"] for item in observations}),
        "candidate_cards": sum(item["candidate_cards"] for item in company_rows),
        "exact_jobs": len(observations),
        "detail_verification_errors": detail_errors,
        "company_results": company_rows,
        "publishable": False,
        "claim_boundary": (
            "Logged-out LinkedIn job activity only. Company-profile followers, staff count, posts and employee histories "
            "are not available through this connector. Output remains experimental pending source-rights approval."
        ),
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "company_results"}, indent=2))


if __name__ == "__main__":
    main()
