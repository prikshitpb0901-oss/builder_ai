#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup


UA = "Mozilla/5.0 (compatible; SignalpostResearch/1.0)"
LEGAL_SUFFIXES = {"as", "asa", "ba", "da", "enk", "nuf", "sa", "stiftelsen"}


def clean_number(value: str) -> int | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else None


def canonical_company_url(value: str) -> str | None:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").casefold()
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (host != "linkedin.com" and not host.endswith(".linkedin.com")) or len(parts) < 2 or parts[0].casefold() != "company":
        return None
    return f"https://linkedin.com/company/{parts[1].casefold()}"


def normalized_company(value: str) -> str:
    words = re.findall(r"[a-z0-9æøå]+", str(value or "").casefold())
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def legal_name_profile_url(value: str) -> str | None:
    """Build one deterministic stale-handle fallback from the registry legal name."""
    transliterated = (
        str(value or "").casefold().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    )
    words = re.findall(r"[a-z0-9]+", transliterated)
    return f"https://www.linkedin.com/company/{'-'.join(words)}" if words else None


def fetch_profile_page(profile_url: str, legal_name: str, timeout: float = 20) -> tuple[bytes, str, str | None]:
    """Fetch the published handle, then one legal-name slug only when it is stale (404)."""
    try:
        raw, final_url = fetch(profile_url, timeout)
        return raw, final_url, None
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    fallback = legal_name_profile_url(legal_name)
    if not fallback or canonical_company_url(fallback) == canonical_company_url(profile_url):
        raise urllib.error.HTTPError(profile_url, 404, "Not Found", {}, None)
    raw, final_url = fetch(fallback, timeout)
    return raw, final_url, fallback


def registered_domain(value: str) -> str | None:
    parsed = urlparse(str(value or "") if "://" in str(value or "") else f"https://{value}")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    return host or None


def fetch(url: str, timeout: float = 20) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "no,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2_000_000), response.geturl()


def json_ld_graph(soup: BeautifulSoup) -> list[dict]:
    rows: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("@graph"), list):
            rows.extend(item for item in value["@graph"] if isinstance(item, dict))
        elif isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            rows.append(value)
    return rows


def _page_company_ids(soup: BeautifulSoup) -> list[str]:
    encoded_values = set(
        re.findall(r"facetCurrentCompany(?:%3D|=)(?:%255B|%5B|\[)(.*?)(?:%255D|%5D|\])", str(soup), re.I)
    )
    output = set()
    for encoded in encoded_values:
        decoded = unquote(unquote(encoded))
        output.update(re.findall(r"\d+", decoded))
    return sorted(output, key=int)


def _followers(soup: BeautifulSoup) -> int | None:
    descriptions = [
        str(tag.get("content") or "")
        for tag in soup.select('meta[name="description"],meta[property="og:description"],meta[name="twitter:description"]')
    ]
    match = re.search(
        r"([\d\s\u00a0.,]+)\s+(?:followers|follower|følgere|Follower:innen|seguidores)",
        " ".join(descriptions),
        re.I,
    )
    return clean_number(match.group(1)) if match else None


def _about_value(soup: BeautifulSoup, test_id: str) -> str | None:
    node = soup.select_one(f'[data-test-id="{test_id}"]')
    if not node:
        return None
    values = [item.get_text(" ", strip=True) for item in node.select("dd")]
    text = " ".join(values).strip() if values else node.get_text(" ", strip=True)
    return text or None


def _about_website(soup: BeautifulSoup) -> str | None:
    node = soup.select_one('[data-test-id="about-us__website"] dd a')
    if not node:
        return None
    return node.get_text(" ", strip=True) or None


def _post_engagement(soup: BeautifulSoup) -> dict[str, dict[str, int]]:
    result = {}
    for article in soup.select("article.main-feed-activity-card[data-activity-urn]"):
        activity_id = str(article.get("data-activity-urn") or "").split(":")[-1]
        reactions = article.select_one('[data-test-id="social-actions__reactions"]')
        comments = article.select_one('[data-test-id="social-actions__comments"]')
        result[activity_id] = {
            "likes": int(reactions.get("data-num-reactions") or 0) if reactions else 0,
            "comments": int(comments.get("data-num-comments") or 0) if comments else 0,
        }
    return result


def extract_profile(raw: bytes, expected_url: str | None = None) -> dict:
    soup = BeautifulSoup(raw, "html.parser")
    graph = json_ld_graph(soup)
    organisations = [item for item in graph if item.get("@type") == "Organization"]
    if not organisations:
        raise RuntimeError("LinkedIn guest page returned no structured organization profile")
    organisation = organisations[-1]
    page_url = canonical_company_url(str(organisation.get("url") or ""))
    if expected_url and page_url != expected_url:
        raise RuntimeError(f"LinkedIn page identity mismatch: expected {expected_url}, received {page_url}")
    number = organisation.get("numberOfEmployees") or {}
    visible_employees = clean_number(number.get("value")) if isinstance(number, dict) else None
    engagement = _post_engagement(soup)
    posts = []
    for item in graph:
        if item.get("@type") != "DiscussionForumPosting":
            continue
        author_url = canonical_company_url(str((item.get("author") or {}).get("url") or ""))
        if page_url and author_url != page_url:
            continue
        url = str(item.get("url") or item.get("mainEntityOfPage") or "")
        activity_match = re.search(r"activity-(\d+)", unquote(url))
        metrics = engagement.get(activity_match.group(1) if activity_match else "", {"likes": 0, "comments": 0})
        posts.append(
            {
                "url": url,
                "date_published": item.get("datePublished"),
                "text": str(item.get("text") or "").strip(),
                **metrics,
            }
        )
    return {
        "name": str(organisation.get("name") or ""),
        "page_url": page_url,
        "linkedin_organisation_ids": _page_company_ids(soup),
        "followers": _followers(soup),
        "visible_employees": visible_employees,
        "employee_size_label": _about_value(soup, "about-us__size"),
        "description": str(organisation.get("description") or ""),
        "website": html.unescape(_about_website(soup) or "") or None,
        "industry": _about_value(soup, "about-us__industry"),
        "headquarters": _about_value(soup, "about-us__headquarters"),
        "address": organisation.get("address"),
        "posts": posts,
    }


def assess_profile_identity(profile: dict, requested_url: str | None, metrics: dict) -> dict:
    website = ((profile.get("evidence") or {}).get("website") or {})
    website_value = website.get("value") or {}
    official_domain = registered_domain(
        website_value.get("final_url") or website.get("source_url") or profile.get("website")
    )
    linkedin_domain = registered_domain(metrics.get("website"))
    legal_core = normalized_company(str(profile.get("name") or ""))
    linkedin_core = normalized_company(str(metrics.get("name") or ""))
    exact_name = bool(legal_core and legal_core == linkedin_core)
    reverse_domain = bool(official_domain and official_domain == linkedin_domain)
    structured_page = bool(metrics.get("page_url"))
    return {
        "publishable_candidate": bool(structured_page and (exact_name or reverse_domain)),
        "requested_url": requested_url,
        "resolved_url": metrics.get("page_url"),
        "exact_legal_name_core": exact_name,
        "official_domain": official_domain,
        "linkedin_website_domain": linkedin_domain,
        "reverse_domain_match": reverse_domain,
        "method": "company_site_crosslink_plus_linkedin_name_or_reverse_domain_v1",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Experimental logged-out LinkedIn company-page extraction; never publish without accepted platform rights."
    )
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--handles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    handles = [json.loads(line) for line in Path(args.handles).read_text().splitlines() if line.strip()]
    linkedin = [item for item in handles if item.get("platform") == "linkedin"]
    profiles = {
        str(item["organisation_number"]): item
        for item in (json.loads(line) for line in Path(args.profiles).read_text().splitlines() if line.strip())
    }
    observations = []
    errors = []
    company_results = []
    duplicate_resolved_handles = []
    seen_resolved: set[tuple[str, str]] = set()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for handle in linkedin:
        expected_url = canonical_company_url(str(handle.get("profile_url") or handle.get("source_url") or ""))
        try:
            profile = profiles[str(handle["organisation_number"])]
            raw, final_url, fallback_url = fetch_profile_page(
                str(handle["profile_url"]), str(profile.get("name") or ""), args.timeout
            )
            digest = hashlib.sha256(raw).hexdigest()
            snapshot = cache_dir / f"{digest}.html"
            if not snapshot.exists():
                snapshot.write_bytes(raw)
            metrics = extract_profile(raw)
            identity = assess_profile_identity(profile, expected_url, metrics)
            if not identity["publishable_candidate"]:
                raise RuntimeError(f"LinkedIn exact-entity identity not established: {json.dumps(identity, ensure_ascii=False)}")
            resolved_key = (str(handle["organisation_number"]), str(metrics["page_url"]))
            if resolved_key in seen_resolved:
                duplicate_resolved_handles.append(
                    {
                        "organisation_number": handle["organisation_number"],
                        "requested_url": expected_url,
                        "resolved_url": metrics["page_url"],
                    }
                )
                continue
            seen_resolved.add(resolved_key)
            retrieved = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            common = {
                "organisation_number": handle["organisation_number"],
                "platform": "linkedin",
                "retrieved_at": retrieved,
                "content_sha256": digest,
                "exact_entity": True,
                "identity_proof": list(handle["identity_proof"])
                + [{"type": "linkedin_structured_company_identity", "value": identity}],
                "acquisition_mode": "unofficial_api_experiment",
                "rights_status": "experimental",
                "source_class": "professional_network",
            }
            profile_values = {
                key: metrics[key]
                for key in (
                    "followers",
                    "visible_employees",
                    "employee_size_label",
                    "linkedin_organisation_ids",
                    "website",
                    "industry",
                    "headquarters",
                )
            }
            observations.append(
                {
                    **common,
                    "id": "linkedin-profile-" + hashlib.sha256(f"{handle['organisation_number']}|{expected_url}|profile".encode()).hexdigest()[:24],
                    "signal_type": "profile_metrics",
                    "source_url": final_url,
                    "evidence_span": f"{metrics['name']} — {metrics['followers']} followers — {metrics['visible_employees']} visible employee profiles",
                    "metrics": {**profile_values, "snapshot_path": str(snapshot)},
                    "strategy": "social_profile_metrics",
                }
            )
            if metrics["visible_employees"] is not None or metrics["employee_size_label"]:
                observations.append(
                    {
                        **common,
                        "id": "linkedin-workforce-" + hashlib.sha256(f"{handle['organisation_number']}|{expected_url}|workforce".encode()).hexdigest()[:24],
                        "signal_type": "workforce_snapshot",
                        "source_url": final_url,
                        "evidence_span": f"LinkedIn structured company profile reports {metrics['visible_employees']} associated employee profiles and size {metrics['employee_size_label']}.",
                        "metrics": {
                            "employees": metrics["visible_employees"],
                            "employee_size_label": metrics["employee_size_label"],
                            "scope": "linkedin_profiles_associated_with_company_page",
                            "snapshot_path": str(snapshot),
                        },
                        "strategy": "linkedin_workforce_snapshot",
                    }
                )
            for post in metrics["posts"]:
                if not post["url"] or not post["text"]:
                    continue
                observations.append(
                    {
                        **common,
                        "id": "linkedin-post-" + hashlib.sha256(f"{handle['organisation_number']}|{post['url']}".encode()).hexdigest()[:24],
                        "signal_type": "public_post",
                        "source_url": post["url"],
                        "evidence_span": post["text"],
                        "metrics": {
                            "likes": post["likes"],
                            "comments": post["comments"],
                            "date_published": post["date_published"],
                            "snapshot_path": str(snapshot),
                        },
                        "strategy": "social_profile_metrics",
                    }
                )
            company_results.append(
                {
                    "organisation_number": handle["organisation_number"],
                    "profile_url": expected_url,
                    "resolved_profile_url": metrics["page_url"],
                    "fallback_url": fallback_url,
                    "identity_assessment": identity,
                    "followers": metrics["followers"],
                    "visible_employees": metrics["visible_employees"],
                    "posts": len(metrics["posts"]),
                    "content_sha256": digest,
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "organisation_number": handle.get("organisation_number"),
                    "url": handle.get("profile_url"),
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                }
            )
        time.sleep(max(0, args.delay))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in observations), encoding="utf-8")
    report = {
        "connector": "linkedin_guest_structured_company_v2",
        "handles": len(linkedin),
        "handles_extracted": len(company_results),
        "companies_extracted": len({item["organisation_number"] for item in company_results}),
        "observations": len(observations),
        "profile_metrics": sum(item["signal_type"] == "profile_metrics" for item in observations),
        "workforce_snapshots": sum(item["signal_type"] == "workforce_snapshot" for item in observations),
        "public_posts": sum(item["signal_type"] == "public_post" for item in observations),
        "company_results": company_results,
        "duplicate_resolved_handles": duplicate_resolved_handles,
        "errors": errors,
        "publishable": False,
        "claim_boundary": (
            "Public logged-out company-page fields only. Associated LinkedIn profiles are not payroll headcount. "
            "Automated reuse remains experimental because LinkedIn's terms prohibit scraping without separate permission."
        ),
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"company_results", "errors"}}, indent=2))


if __name__ == "__main__":
    main()
