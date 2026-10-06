"""External footprint connectors for YouTube and LinkedIn discovery.

Provides bounded, exact-entity discovery of social footprints:
1. YouTube Channel Discovery (via bounded yt-dlp flat extraction with multi-match & domain gates)
2. LinkedIn Company Profile Discovery (via guest typeahead API with exact legal core gating)
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from typing import Any

from .social_security import verify_social_channel_security

LEGAL_SUFFIXES = {
    "as", "asa", "ba", "da", "enk", "iks", "nuf", "sa", "sam",
    "sti", "stiftelsen", "holding", "eiendom",
}

USER_AGENT = "Mozilla/5.0 (compatible; SignalpostResearchPOC/1.0; +https://builderr.ai)"
_LI_SEMAPHORE = threading.Semaphore(4)


class CircuitBreaker:
    """Thread-safe circuit breaker preventing cascading hangs on unresponsive or tarpitting external endpoints."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 20.0):
        self.threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self.failures = 0
        self.opened_at = 0.0
        self.lock = threading.Lock()

    def is_available(self) -> bool:
        with self.lock:
            if self.failures >= self.threshold:
                if time.monotonic() - self.opened_at < self.cooldown:
                    return False
                self.failures = self.threshold - 1
            return True

    def record_success(self) -> None:
        with self.lock:
            self.failures = 0

    def record_failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()


_LI_CIRCUIT = CircuitBreaker(failure_threshold=3, cooldown_seconds=20.0)
_REVIEWS_CIRCUIT = CircuitBreaker(failure_threshold=3, cooldown_seconds=20.0)
_JOBS_CIRCUIT = CircuitBreaker(failure_threshold=3, cooldown_seconds=20.0)


def _read_bounded_with_deadline(response: Any, max_bytes: int, deadline: float) -> bytes:
    """Read stream with size limit and hard wall-clock deadline."""
    chunks = []
    total = 0
    while total <= max_bytes:
        if time.monotonic() > deadline:
            raise TimeoutError("Read exceeded deadline")
        requested = min(16384, max_bytes + 1 - total)
        chunk = response.read(requested)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("Read exceeded deadline")
        if len(chunk) < requested:
            break
    return b"".join(chunks)


def _normalize_name(value: str) -> str:
    """Normalize company name to lowercase ASCII alphanumeric core, stripping legal suffixes."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def discover_linkedin_company(company_name: str, org_number: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """Discover verified LinkedIn company profile via LinkedIn guest typeahead.

    Strict entity gate: requires exact normalized legal core match.
    """
    if not company_name or not _LI_CIRCUIT.is_available():
        return None

    legal_core = _normalize_name(company_name)
    if not legal_core:
        return None

    queries = [company_name.strip()]
    if legal_core and legal_core.casefold() != company_name.strip().casefold():
        queries.append(legal_core)

    try:
        for q_idx, query_candidate in enumerate(queries):
            clean_query = urllib.parse.quote(query_candidate)
            url = f"https://www.linkedin.com/jobs-guest/api/typeaheadHits?typeaheadType=COMPANY&query={clean_query}"

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
                },
            )
            deadline = time.monotonic() + timeout
            with _LI_SEMAPHORE:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = _read_bounded_with_deadline(resp, 200_000, deadline)
                    data = json.loads(raw.decode("utf-8", errors="replace"))
            _LI_CIRCUIT.record_success()

            if not isinstance(data, list):
                continue

            # Look for exact core match
            for item in data:
                if item.get("type") != "COMPANY" or not item.get("id"):
                    continue
                display_name = str(item.get("displayName") or "")
                candidate_core = _normalize_name(display_name)

                if candidate_core == legal_core:
                    # Specificity guard for secondary core query: require multi-word or len >= 6 or norwegian chars
                    if q_idx > 0:
                        words = legal_core.split()
                        is_specific = (
                            len(words) >= 2
                            or len(legal_core) >= 6
                            or any(ch in company_name.lower() for ch in ("æ", "ø", "å"))
                        )
                        if not is_specific:
                            continue

                    company_id = str(item["id"])
                    # Generate clean canonical company slug / search URL
                    slug = re.sub(r"[^a-z0-9\-]+", "-", display_name.lower()).strip("-")
                    canonical_url = f"https://www.linkedin.com/company/{slug}" if slug else f"https://www.linkedin.com/company/{company_id}"

                    # Security & authenticity screening
                    sec = verify_social_channel_security(
                        platform="linkedin",
                        channel_or_profile_name=display_name,
                        target_url=canonical_url,
                        company_name=company_name,
                    )
                    if not sec["is_safe"]:
                        continue

                    return {
                        "platform": "linkedin",
                        "organisation_number": str(org_number),
                        "linkedin_company_id": company_id,
                        "display_name": display_name,
                        "profile_url": canonical_url,
                        "exact_entity": True,
                        "match_type": "exact_legal_core",
                        "source": "linkedin_guest_api",
                        "security_assessment": sec,
                    }

    except Exception:
        _LI_CIRCUIT.record_failure()

    return None


def discover_youtube_channel(
    company_name: str,
    org_number: str,
    website_domain: str | None = None,
    max_results: int = 4,
) -> dict[str, Any] | None:
    """Discover verified YouTube channel using bounded flat extraction with yt-dlp.

    Verification gates:
    1. Exact normalized channel name match.
    2. At least 2 search results from the channel, OR website domain referenced.
    """
    if not company_name:
        return None

    core = _normalize_name(company_name)
    if not core or len(core) < 3:
        return None

    try:
        import yt_dlp

        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": max_results,
            "skip_download": True,
            "socket_timeout": 8,
        }

        clean_name = re.sub(r"\b(AS|ASA|ENK|ANS|DA|NUF|BA|SA|HF|IKS|KF|BRL|HOLDING|EIENDOM)\b", "", company_name, flags=re.I).strip() or company_name
        query = f'ytsearch{max_results}:"{clean_name}" Norway'
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(query, download=False)

        entries = [item for item in (info.get("entries") or []) if item]
        if not entries:
            return None

        by_channel: dict[str, list[dict]] = {}
        channel_names: dict[str, str] = {}

        for item in entries:
            channel_name = str(item.get("channel") or item.get("uploader") or "").strip()
            channel_url = str(item.get("channel_url") or item.get("uploader_url") or "").strip()
            if not channel_url or not channel_name:
                continue

            cand_core = _normalize_name(channel_name)
            if cand_core != core:
                continue

            by_channel.setdefault(channel_url, []).append(item)
            channel_names[channel_url] = channel_name

        if not by_channel:
            return None

        # Rank by number of matching videos
        ranked = sorted(by_channel.items(), key=lambda pair: -len(pair[1]))
        best_url, matched_items = ranked[0]
        channel_display_name = channel_names[best_url]

        # Domain verification in descriptions
        domain_verified = False
        if website_domain:
            clean_dom = website_domain.lower().removeprefix("www.")
            domain_verified = any(
                clean_dom in str(item.get("description") or "").casefold()
                for item in matched_items
            )

        # Gate: require at least 2 distinct search items or domain verification
        if len(matched_items) < 2 and not domain_verified:
            return None

        recent_videos = []
        for item in matched_items[:3]:
            title = str(item.get("title") or "")
            url = str(item.get("url") or item.get("webpage_url") or "")
            if title and url:
                recent_videos.append({
                    "title": title[:160],
                    "url": url,
                    "view_count": item.get("view_count"),
                })

        # Security & authenticity screening (anti-impersonation & scam filtering)
        sec = verify_social_channel_security(
            platform="youtube",
            channel_or_profile_name=channel_display_name,
            target_url=best_url,
            company_name=company_name,
            content_samples=[v["title"] for v in recent_videos],
            website_domain=website_domain,
        )
        if not sec["is_safe"]:
            return None

        return {
            "platform": "youtube",
            "organisation_number": str(org_number),
            "channel_name": channel_display_name,
            "channel_url": best_url,
            "exact_entity": True,
            "matched_videos_count": len(matched_items),
            "recent_videos": recent_videos,
            "domain_verified": domain_verified,
            "source": "youtube_channel_search",
            "security_assessment": sec,
        }

    except Exception:
        pass

    return None


def discover_customer_reviews(company_name: str, org_number: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """Discover verified customer reviews and aggregate rating with exact-entity gate."""
    if not company_name or not org_number or not _REVIEWS_CIRCUIT.is_available():
        return None
    try:
        from bs4 import BeautifulSoup
        clean_slug = _normalize_name(company_name).replace(" ", "-")
        url = f"https://www.fagfolkguiden.no/bedrift/{clean_slug}-{org_number}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        deadline = time.monotonic() + timeout
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _read_bounded_with_deadline(resp, 200_000, deadline)
        _REVIEWS_CIRCUIT.record_success()
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(node.string or node.get_text() or "{}")
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                rating_data = (item or {}).get("aggregateRating") if isinstance(item, dict) else None
                if isinstance(rating_data, dict):
                    val = rating_data.get("ratingValue")
                    count = rating_data.get("ratingCount") or rating_data.get("reviewCount")
                    if val is not None and count is not None and float(val) > 0:
                        return {
                            "platform": "customer_reviews",
                            "source_url": url,
                            "rating": round(float(val), 2),
                            "review_count": int(count),
                            "exact_entity": True,
                            "source": "fagfolkguiden_google_aggregate",
                        }
    except Exception:
        _REVIEWS_CIRCUIT.record_failure()
    return None


def discover_linkedin_jobs(company_name: str, org_number: str, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Discover verified LinkedIn guest job postings with company core name matching."""
    if not company_name or not _JOBS_CIRCUIT.is_available():
        return []
    try:
        clean = urllib.parse.quote(company_name.strip())
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={clean}&location=Norway"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
            },
        )
        deadline = time.monotonic() + timeout
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _read_bounded_with_deadline(resp, 200_000, deadline)
        _JOBS_CIRCUIT.record_success()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")
        jobs = []
        comp_core = _normalize_name(company_name)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for card in soup.select("div.base-search-card")[:3]:
            title_node = card.select_one("h3.base-search-card__title, span.sr-only")
            company_node = card.select_one("h4.base-search-card__subtitle")
            loc_node = card.select_one("span.job-search-card__location")
            link_node = card.select_one("a.base-card__full-link")
            comp_name = company_node.get_text(" ", strip=True) if company_node else ""
            c_core = _normalize_name(comp_name)
            j_url = link_node.get("href") if link_node else ""
            if comp_core and c_core and (comp_core in c_core or c_core in comp_core) and j_url:
                jobs.append({
                    "id": "li-job-" + hashlib.sha256(f"{org_number}|{j_url}".encode()).hexdigest()[:20],
                    "organisation_number": str(org_number),
                    "platform": "linkedin",
                    "signal_type": "job_posting",
                    "title": title_node.get_text(" ", strip=True) if title_node else "Open Position",
                    "company": comp_name,
                    "location": loc_node.get_text(" ", strip=True) if loc_node else "Norway",
                    "job_url": j_url,
                    "source_url": j_url,
                    "url": j_url,
                    "exact_entity": True,
                    "retrieved_at": now_iso,
                })
        return jobs
    except Exception:
        _JOBS_CIRCUIT.record_failure()
        return []


def discover_nav_jobs(company_name: str, org_number: str, timeout: float = 3.0) -> list[dict[str, Any]]:
    """Discover verified Norwegian national job postings from official NAV Arbeidsplassen public search."""
    if not company_name:
        return []
    clean_name = re.sub(r"\b(AS|ASA|ENK|ANS|DA|NUF|BA|SA|HF|IKS|KF|BRL|HOLDING|EIENDOM)\b", "", company_name, flags=re.I).strip()
    if not clean_name:
        clean_name = company_name.strip()
    try:
        url = f"https://arbeidsplassen.nav.no/stillinger/api/search?q={urllib.parse.quote(clean_name)}&size=5"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        deadline = time.monotonic() + timeout
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _read_bounded_with_deadline(resp, 300_000, deadline)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return []

        comp_core = _normalize_name(company_name)
        jobs = []
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        for hit in hits:
            src = hit.get("_source", {})
            uuid = src.get("uuid")
            if not uuid:
                continue
            employer = src.get("businessName") or src.get("employer") or ""
            emp_core = _normalize_name(employer)
            # Require matching employer core
            if not (comp_core and emp_core and (comp_core in emp_core or emp_core in comp_core)):
                continue

            j_url = f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
            title = src.get("title") or "Ledig stilling"
            loc_list = src.get("locations") or []
            location = loc_list[0].get("city") if loc_list and isinstance(loc_list[0], dict) else "Norge"
            pub_date = src.get("published")

            jobs.append({
                "id": "nav-job-" + hashlib.sha256(f"{org_number}|{uuid}".encode()).hexdigest()[:20],
                "organisation_number": str(org_number),
                "platform": "job_board",
                "signal_type": "job_posting",
                "title": title,
                "company": employer,
                "location": location,
                "job_url": j_url,
                "source_url": j_url,
                "url": j_url,
                "date_posted": pub_date,
                "published_at": pub_date,
                "exact_entity": True,
                "source": "nav_arbeidsplassen",
                "retrieved_at": now_iso,
            })
            if len(jobs) >= 3:
                break
        return jobs
    except Exception:
        return []

