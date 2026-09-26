from __future__ import annotations

import hashlib
from typing import Any

import extruct
import trafilatura
from bs4 import BeautifulSoup

from .evidence import evidence, utc_now
from .website import _extraction_state, _jsonld_organisations, _registered_domain, _social_links, normalize_homepage, structured_social_links


def extract_page_event(
    *,
    organisation_number: str,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
    body: bytes,
    page_kind: str,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    base = {
        "organisation_number": organisation_number,
        "requested_url": requested_url,
        "final_url": final_url,
        "status_code": status_code,
        "content_type": content_type,
        "page_kind": page_kind,
        "retrieved_at": retrieved_at or utc_now(),
        "bytes": len(body),
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }
    if status_code < 200 or status_code >= 300:
        return {**base, "status": "not_found" if status_code in {404, 410} else "source_error", "error": f"HTTP {status_code}"}
    if "html" not in content_type.casefold():
        return {**base, "status": "source_error", "error": f"Unsupported content type: {content_type}"}
    page_html = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(page_html, "lxml")
    text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
    title = soup.title.get_text(" ", strip=True)[:500] if soup.title else ""
    description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = str(description_tag.get("content") or "").strip()[:2000] if description_tag else ""
    identity_nodes = soup.select(
        'footer, address, [itemprop="legalName"], [itemprop="address"], '
        '[itemprop="telephone"], [itemprop="email"]'
    )
    identity_text = " ".join(node.get_text(" ", strip=True) for node in identity_nodes)
    identity_text = " ".join(identity_text.split())[:3000]
    event = {
        **base,
        "status": "available",
        "title": title,
        "description": description,
        "main_text_excerpt": text[:5000],
        "identity_text_excerpt": identity_text,
        "social_links": _social_links(final_url, soup),
        "extraction_state": _extraction_state(text, soup),
    }
    if page_kind == "homepage":
        structured = extruct.extract(page_html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        event["structured_organisations"] = _jsonld_organisations(structured)
        combined_social = event["social_links"] + structured_social_links(event["structured_organisations"])
        event["social_links"] = list({(item["platform"], item["url"]): item for item in combined_social}.values())
        event["registered_domain"] = _registered_domain(final_url)
    return event


def error_page_event(
    *,
    organisation_number: str,
    requested_url: str,
    page_kind: str,
    error: str,
) -> dict[str, Any]:
    return {
        "organisation_number": organisation_number,
        "requested_url": requested_url,
        "final_url": requested_url,
        "status_code": 0,
        "content_type": "",
        "page_kind": page_kind,
        "retrieved_at": utc_now(),
        "bytes": 0,
        "content_sha256": hashlib.sha256(b"").hexdigest(),
        "status": "source_error",
        "error": error[:240],
    }


def missing_seed_error_events(profiles: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every crawlable seed a terminal ledger entry, including robots rejections."""
    event_orgs = {str(event.get("organisation_number") or "") for event in events}
    missing = []
    for profile in profiles:
        organisation_number = str(profile.get("organisation_number") or "")
        if not profile.get("website") or organisation_number in event_orgs:
            continue
        requested_url = normalize_homepage(str(profile["website"]))
        if requested_url:
            missing.append(error_page_event(
                organisation_number=organisation_number,
                requested_url=requested_url,
                page_kind="homepage",
                error="No page event emitted; request was rejected before download (for example by robots.txt).",
            ))
    return missing


def merge_profile_events(profile: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    homepage_events = [item for item in events if item.get("page_kind") == "homepage"]
    homepage_events.sort(key=lambda item: item.get("retrieved_at") or "")
    homepage = homepage_events[-1] if homepage_events else None
    if not homepage:
        return evidence(
            "website",
            "not_found",
            "registry_linked_company_website_scrapy",
            str(profile.get("website") or "https://data.brreg.no/enhetsregisteret/api/enheter"),
            note="No homepage crawl event was produced",
        )
    if homepage.get("status") != "available":
        return evidence(
            "website",
            homepage.get("status", "source_error"),
            "registry_linked_company_website_scrapy",
            homepage.get("final_url") or homepage.get("requested_url"),
            note=homepage.get("error") or "Homepage fetch failed",
            retrieved_at=homepage.get("retrieved_at"),
            content_sha256=homepage.get("content_sha256"),
        )
    available = [item for item in events if item.get("status") == "available"]
    unique_pages = {}
    social = {}
    for item in sorted(available, key=lambda value: (value.get("page_kind") != "homepage", value.get("final_url") or "")):
        page = {
            "url": item.get("final_url"),
            "title": item.get("title") or "",
            "main_text_excerpt": item.get("main_text_excerpt") or "",
            "identity_text_excerpt": item.get("identity_text_excerpt") or "",
            "content_sha256": item.get("content_sha256"),
        }
        unique_pages[item.get("final_url")] = page
        for link in item.get("social_links") or []:
            social[(link.get("platform"), link.get("url"))] = link
    value = {
        "requested_url": homepage.get("requested_url"),
        "final_url": homepage.get("final_url"),
        "registered_domain": homepage.get("registered_domain"),
        "title": homepage.get("title") or "",
        "description": homepage.get("description") or "",
        "main_text_excerpt": homepage.get("main_text_excerpt") or "",
        "identity_text_excerpt": homepage.get("identity_text_excerpt") or "",
        "social_links": list(social.values()),
        "structured_organisations": homepage.get("structured_organisations") or [],
        "content_sha256": homepage.get("content_sha256"),
        "extraction_state": homepage.get("extraction_state"),
        "pages": list(unique_pages.values()),
        "crawl_errors": [
            {"url": item.get("final_url") or item.get("requested_url"), "error": item.get("error")}
            for item in events
            if item.get("status") != "available"
        ],
        "scheduler": "scrapy_resumable_v1",
    }
    return evidence(
        "website",
        "available",
        "registry_linked_company_website_scrapy",
        homepage.get("final_url"),
        value=value,
        note="Company-controlled claim layer; not an official registry fact",
        retrieved_at=homepage.get("retrieved_at"),
        content_sha256=homepage.get("content_sha256"),
    )
