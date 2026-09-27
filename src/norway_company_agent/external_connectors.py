"""External footprint connectors for YouTube and LinkedIn discovery.

Provides bounded, exact-entity discovery of social footprints:
1. YouTube Channel Discovery (via bounded yt-dlp flat extraction with multi-match & domain gates)
2. LinkedIn Company Profile Discovery (via guest typeahead API with exact legal core gating)
"""
from __future__ import annotations

import hashlib
import json
import re
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


def _normalize_name(value: str) -> str:
    """Normalize company name to lowercase ASCII alphanumeric core, stripping legal suffixes."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def discover_linkedin_company(company_name: str, org_number: str, timeout: float = 6.0) -> dict[str, Any] | None:
    """Discover verified LinkedIn company profile via LinkedIn guest typeahead.

    Strict entity gate: requires exact normalized legal core match.
    """
    if not company_name:
        return None

    legal_core = _normalize_name(company_name)
    if not legal_core:
        return None

    # Query with the core name or clean name
    clean_query = urllib.parse.quote(company_name.strip())
    url = f"https://www.linkedin.com/jobs-guest/api/typeaheadHits?typeaheadType=COMPANY&query={clean_query}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        if not isinstance(data, list):
            return None

        # Look for exact core match
        for item in data:
            if item.get("type") != "COMPANY" or not item.get("id"):
                continue
            display_name = str(item.get("displayName") or "")
            candidate_core = _normalize_name(display_name)

            if candidate_core == legal_core:
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
        pass

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

        query = f'ytsearch{max_results}:"{company_name}" Norway'
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
