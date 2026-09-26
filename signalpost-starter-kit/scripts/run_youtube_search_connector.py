#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp


LEGAL = {"as", "asa", "sa", "da", "ans", "enk", "nuf"}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def norm(value: object) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return " ".join(token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL)


def domain(value: object) -> str:
    raw = str(value or "")
    if "://" not in raw:
        raw = "https://" + raw
    return (urlparse(raw).hostname or "").casefold().removeprefix("www.")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Find exact-name public YouTube channels with bounded yt-dlp search.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--handles", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--organisations")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--search-results", type=int, default=8)
    args = parser.parse_args()
    existing = {
        str(item["organisation_number"])
        for source in args.handles for item in read_jsonl(Path(source))
        if item.get("platform") == "youtube"
    }
    wanted = None
    if args.organisations:
        wanted = {line.strip() for line in Path(args.organisations).read_text().splitlines() if line.strip()}
    candidates = []
    for profile in read_jsonl(Path(args.profiles)):
        if wanted is not None and str(profile["organisation_number"]) not in wanted:
            continue
        website = ((profile.get("evidence") or {}).get("website") or {})
        value = website.get("value") or {}
        if website.get("status") != "available" or not (value.get("identity_assessment") or {}).get("publishable"):
            continue
        if str(profile["organisation_number"]) in existing:
            continue
        candidates.append(profile)
    observations = []
    errors = []
    statuses = {"searched": 0, "exact_channels": 0, "abstained": 0}
    options = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": args.search_results, "skip_download": True}
    for profile in candidates[: args.limit]:
        org = str(profile["organisation_number"])
        core = norm(profile["name"])
        website = profile["evidence"]["website"]
        site_domain = domain(website.get("source_url") or (website.get("value") or {}).get("final_url"))
        statuses["searched"] += 1
        try:
            with yt_dlp.YoutubeDL(options) as client:
                info = client.extract_info(f'ytsearch{args.search_results}:"{profile["name"]}" Norway', download=False)
            entries = [item for item in (info.get("entries") or []) if item]
            by_channel = {}
            for item in entries:
                channel_name = str(item.get("channel") or item.get("uploader") or "")
                channel_url = str(item.get("channel_url") or item.get("uploader_url") or "")
                if not channel_url or norm(channel_name) != core:
                    continue
                by_channel.setdefault(channel_url, []).append(item)
            ranked = sorted(by_channel.items(), key=lambda pair: (-len(pair[1]), pair[0]))
            if not ranked:
                statuses["abstained"] += 1
                continue
            channel_url, matched = ranked[0]
            domain_in_description = any(site_domain and site_domain in str(item.get("description") or "").casefold() for item in matched)
            if len(matched) < 2 and not domain_in_description:
                statuses["abstained"] += 1
                continue
            retrieved_at = utc_now()
            digest = hashlib.sha256(json.dumps(matched, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            proof = [
                {"type": "company_site_identity", "score": ((website.get("value") or {}).get("identity_assessment") or {}).get("score")},
                {"type": "exact_normalized_youtube_channel_name", "value": core, "matched_search_results": len(matched)},
            ]
            common = {
                "organisation_number": org, "platform": "youtube", "retrieved_at": retrieved_at,
                "content_sha256": digest, "exact_entity": True, "identity_proof": proof,
                "acquisition_mode": "unofficial_api_experiment", "rights_status": "review_required",
                "source_class": "company_social",
            }
            observations.append({
                **common, "id": f"youtube-search-handle-{org}-{digest[:16]}", "signal_type": "profile_handle",
                "source_url": channel_url, "profile_url": channel_url, "strategy": "verified_handle_extraction",
            })
            views = []
            for item in matched:
                url = str(item.get("url") or item.get("webpage_url") or "")
                if not url.startswith("http") or not ("youtube.com/watch" in url or "youtu.be/" in url):
                    continue
                metrics = {key: item.get(key) for key in ("view_count", "like_count", "comment_count") if item.get(key) is not None}
                if item.get("view_count") is not None:
                    views.append(int(item["view_count"]))
                observations.append({
                    **common, "id": "youtube-search-post-" + hashlib.sha256(f"{org}|{url}".encode()).hexdigest()[:24],
                    "signal_type": "public_post", "source_url": url,
                    "evidence_span": str(item.get("title") or item.get("description") or "public YouTube post")[:1200],
                    "metrics": metrics, "strategy": "youtube_channel_feed",
                })
            if views:
                observations.append({
                    **common, "id": f"youtube-search-metrics-{org}-{digest[:16]}", "signal_type": "profile_metrics",
                    "source_url": channel_url,
                    "metrics": {"matched_recent_posts": len(matched), "recent_views_total": sum(views), "recent_views_mean": round(sum(views) / len(views), 1)},
                    "strategy": "social_profile_metrics",
                })
            statuses["exact_channels"] += 1
        except Exception as exc:
            errors.append({"organisation_number": org, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in observations), encoding="utf-8")
    report = {"connector": "yt_dlp_exact_channel_search_v1", "eligible": len(candidates), **statuses, "observations": len(observations), "errors": errors, "claim_boundary": "Exact-name, repeated-result YouTube channel candidates; experimental and terms-sensitive."}
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "errors"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
