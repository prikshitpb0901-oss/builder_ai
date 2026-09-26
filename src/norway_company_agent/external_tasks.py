from __future__ import annotations

import hashlib
import json
from typing import Any


def _task_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def plan_external_tasks(profile: dict[str, Any]) -> list[dict[str, Any]]:
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "")
    if not org or not name:
        return []
    registry = (profile.get("evidence", {}).get("registry_live", {}).get("value") or profile)
    address = registry.get("business_address") or registry.get("postal_address") or {}
    municipality = address.get("kommune") or profile.get("municipality") or ""
    website = profile.get("evidence", {}).get("website", {})
    web_value = website.get("value") or {}
    social_links = web_value.get("social_links") or []
    tasks: list[dict[str, Any]] = []

    def add(connector: str, purpose: str, candidate_url: str | None = None) -> None:
        core = {
            "organisation_number": org,
            "company_name": name,
            "municipality": municipality,
            "connector": connector,
            "purpose": purpose,
            "candidate_url": candidate_url,
        }
        tasks.append({"task_id": _task_id(core), **core, "state": "pending"})

    add("google_places_api", "resolve_places_and_public_rating")
    add("licensed_news_search", "discover_independent_mentions")
    add("jobs_provider", "discover_active_jobs")
    for link in social_links:
        platform = str(link.get("platform") or "")
        url = str(link.get("url") or "")
        if platform and url:
            add(f"{platform}_connector", "refresh_verified_profile_metrics", url)
    if not social_links:
        add("permitted_search_api", "discover_social_handles")
    return tasks
