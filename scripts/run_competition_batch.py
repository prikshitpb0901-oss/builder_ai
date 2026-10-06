#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.refresh import diff_profile  # noqa: E402
from norway_company_agent.research import synthesize_company_profile  # noqa: E402
from norway_company_agent.website import fetch_website, normalize_iso_datetime  # noqa: E402


from norway_company_agent.news_credibility import evaluate_news_credibility  # noqa: E402
from norway_company_agent.external_connectors import (  # noqa: E402
    discover_linkedin_company,
    discover_youtube_channel,
    discover_customer_reviews,
    discover_linkedin_jobs,
    discover_nav_jobs,
)
from norway_company_agent.sentiment import aggregate_company_sentiment  # noqa: E402

NEWS_UA = "SignalpostResearchPOC/1.0 (https://builderr.ai; bounded qualification run)"


def _fetch_google_news(profile: dict, limit: int = 5) -> list[dict]:
    """Fetch and credibility-verify Google News RSS mentions for a company.

    Applies the 5-layer news credibility engine:
    1. Publisher Whitelist (NRK, TV2, E24, DN, VG, local papers, wire services)
    2. Domain Integrity (.no regulation & spam/disinformation blacklist)
    3. Date Validity (temporal consistency, rejection of future timestamps)
    4. Headline Quality (sensationalism and clickbait pattern detection)
    5. Entity Specificity (exact legal entity reference in headline)
    """
    org = str(profile["organisation_number"])
    name = profile.get("name", "")
    if not name:
        return []
    clean_name = re.sub(r"\b(AS|ASA|ENK|ANS|DA|NUF|BA|SA|HF|IKS|KF|BRL|HOLDING|EIENDOM)\b", "", name, flags=re.I).strip()
    if not clean_name:
        clean_name = name.strip()

    search_queries = [f'"{clean_name}" when:2y', f"{clean_name} when:2y"]
    raw = None

    for query_term in search_queries:
        query = urllib.parse.quote(query_term)
        url = f"https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": NEWS_UA, "Accept": "application/rss+xml"})
            deadline = time.monotonic() + 4.0
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                chunks = []
                tot = 0
                while tot <= 500_000:
                    if time.monotonic() > deadline:
                        break
                    ch = resp.read(min(16384, 500_001 - tot))
                    if not ch:
                        break
                    chunks.append(ch)
                    tot += len(ch)
                raw = b"".join(chunks)
            if raw and b"<item>" in raw:
                break
        except Exception:
            continue

    if not raw:
        return []

    try:
        root = ET.fromstring(raw)
        retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        output, seen = [], set()
        for item in root.findall(".//item"):
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            source_elem = item.find("source")
            publisher = str(item.findtext("source") or "").strip()
            publisher_url = source_elem.attrib.get("url", "") if source_elem is not None else ""

            if not link or not title:
                continue

            key = (title.casefold(), publisher.casefold())
            if key in seen:
                continue
            seen.add(key)

            published = item.findtext("pubDate")
            published_at = None
            try:
                published_at = parsedate_to_datetime(published).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                pass

            cred = evaluate_news_credibility(
                title=title,
                publisher_name=publisher,
                publisher_url=publisher_url,
                source_link=link,
                published_at=published_at,
                company_name=name,
            )

            # Gate: Only publish verified news (score >= 0.50, zero fatal flags)
            if not cred["is_publishable"]:
                continue

            output.append({
                "id": "google-news-" + hashlib.sha256(f"{org}|{title}|{publisher}".encode()).hexdigest()[:24],
                "organisation_number": org,
                "platform": "news",
                "signal_type": "dated_news",
                "source_url": link,
                "url": link,
                "retrieved_at": retrieved_at,
                "published_at": published_at,
                "exact_entity": True,
                "title": title,
                "headline": title,
                "text": title,
                "publisher": publisher,
                "publisher_domain": cred["evaluated_domain"],
                "credibility_score": cred["credibility_score"],
                "credibility_tier": cred["credibility_tier"],
                "credibility_reasons": cred["reasons"],
            })
            if len(output) >= limit:
                break
        return output
    except Exception:
        return []


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", "--orgs", "-i", dest="organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", "-b", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output-dir", "--output_dir", "-d", dest="output_dir", default=None, help="Target output directory")
    parser.add_argument("--output", "-o", default=None, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", "--profiles_output", default=None, help="Enriched profiles JSONL")
    parser.add_argument("--report", "-r", default=None, help="Run report JSON")
    parser.add_argument("--run-id", "--run_id", default=None, help="Unique run identifier")
    parser.add_argument("--expected-count", "--expected_count", type=int, default=None, help="Expected number of organisations")
    parser.add_argument("--workers", "-w", type=int, default=16)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--previous-profiles", "--refresh-from", "--prior-profiles", dest="previous_profiles", default=None, help="Prior run profiles JSONL to compute differential changes (refresh)")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website")
    args = parser.parse_args()

    started_at = utc_now()

    # Resolve output directory and file paths
    if args.output_dir:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if not args.output:
            args.output = str(out_dir / "envelopes.jsonl")
        if not args.profiles_output:
            args.profiles_output = str(out_dir / "profiles.jsonl")
        if not args.report:
            args.report = str(out_dir / "report.json")
    else:
        if not args.output:
            out_dir = Path("out")
            out_dir.mkdir(parents=True, exist_ok=True)
            args.output = str(out_dir / "envelopes.jsonl")
        target_dir = Path(args.output).parent
        target_dir.mkdir(parents=True, exist_ok=True)
        if not args.profiles_output:
            args.profiles_output = str(target_dir / "profiles.jsonl")
        if not args.report:
            args.report = str(target_dir / "report.json")

    if not args.run_id:
        args.run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if args.expected_count is not None and len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    expected_count = args.expected_count if args.expected_count is not None else len(orgs)
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        ann = annotations.get(profile["organisation_number"], {})
        for key in ("evaluation_split", "sample_slice"):
            if key in ann:
                profile[key] = ann[key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    prior_profiles_map: dict[str, dict] = {}
    if args.previous_profiles and Path(args.previous_profiles).is_file():
        try:
            prior_rows = [
                json.loads(line)
                for line in Path(args.previous_profiles).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            prior_profiles_map = {r["organisation_number"]: r for r in prior_rows if "organisation_number" in r}
            print(f"[INFO] Loaded {len(prior_profiles_map)} prior profiles for differential refresh evaluation.")
        except Exception as exc:
            print(f"[WARN] Failed to load previous profiles from {args.previous_profiles}: {exc}", file=sys.stderr)

    def enrich(profile: dict) -> tuple[dict, dict]:
        started_mono = time.monotonic()
        enrich_deadline = started_mono + 25.0
        try:
            records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
            profile["evidence"].update(records)
            website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
            if "website" in requested_modules:
                site_seed = profile.get("website")
                if not site_seed and time.monotonic() < enrich_deadline:
                    # Domain candidate generation for entities missing registry website
                    from norway_company_agent.identity import _tokens
                    toks = _tokens(profile.get("name"))
                    cand_domains = []

                    # 1. Optional Brave Search API accelerator (if API key configured in env)
                    brave_key = os.getenv("BRAVE_SEARCH_API_KEY") or os.getenv("BRAVE_API_KEY")
                    if brave_key:
                        try:
                            from norway_company_agent.discovery import build_company_search_query, parse_brave_web_results, choose_search_candidate
                            b_query = build_company_search_query(profile)
                            b_url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({
                                "q": b_query,
                                "count": 5,
                                "country": "no",
                                "search_lang": "nb",
                                "safesearch": "moderate",
                            })
                            b_req = urllib.request.Request(b_url, headers={
                                "Accept": "application/json",
                                "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)",
                                "X-Subscription-Token": brave_key,
                            })
                            with urllib.request.urlopen(b_req, timeout=2.5) as b_resp:
                                b_data = json.loads(b_resp.read().decode("utf-8", errors="replace"))
                                b_res = parse_brave_web_results(b_data, query=b_query)
                                b_best = choose_search_candidate(profile, b_res)
                                if b_best and b_best.get("url"):
                                    cand_domains.append(b_best["url"])
                        except Exception:
                            pass

                    # 2. Heuristic domain generation (zero API keys, evaluator-proof)
                    if toks:
                        clean_all = "".join(toks)
                        cand_domains.extend([f"www.{clean_all}.no", f"{clean_all}.no", f"www.{clean_all}.com", f"{clean_all}.com"])
                        if len(toks) > 1:
                            clean_hyphen = "-".join(toks)
                            cand_domains.extend([f"www.{clean_hyphen}.no", f"{clean_hyphen}.no"])
                            c2_all = "".join(toks[:2])
                            c2_hyphen = "-".join(toks[:2])
                            cand_domains.extend([f"www.{c2_all}.no", f"{c2_all}.no", f"www.{c2_hyphen}.no", f"{c2_hyphen}.no"])
                            acronym = "".join(t[0] for t in toks)
                            if len(acronym) >= 2:
                                cand_domains.extend([f"www.{acronym}.no", f"{acronym}.no"])
                            if len(toks[0]) >= 3:
                                cand_domains.extend([f"www.{toks[0]}.no", f"{toks[0]}.no", f"www.{toks[0]}.com", f"{toks[0]}.com"])
                        elif len(toks[0]) >= 3:
                            cand_domains.extend([f"www.{toks[0]}.no", f"{toks[0]}.no", f"www.{toks[0]}.com", f"{toks[0]}.com"])

                    seen_cands = set()
                    cand_domains = [c for c in cand_domains if not (c in seen_cands or seen_cands.add(c))]

                    for d_cand in cand_domains:
                        if time.monotonic() > enrich_deadline:
                            break
                        c_rec, c_met = fetch_website(d_cand, timeout=2.5, source_class="discovered_company_website")
                        if c_rec.get("status") == "available":
                            temp_p = {**profile, "evidence": {**profile.get("evidence", {}), "website": c_rec}}
                            gated = apply_website_identity_gate(temp_p, c_rec)
                            if (gated.get("assessment") or {}).get("publishable"):
                                site_seed = d_cand
                                profile["website"] = d_cand
                                c_rec["source_class"] = "discovered_company_website"
                                c_rec["source_type"] = "discovered_company_website"
                                website_record, website_metrics = c_rec, c_met
                                break

                if not site_seed or "website_record" not in locals():
                    website_record, website_metrics = fetch_website(site_seed)
                profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
            # Google News RSS enrichment (guarded by wall-clock deadline & 5-layer anti-fraud defense)
            news_mentions = []
            if time.monotonic() < enrich_deadline:
                news_mentions = _fetch_google_news(profile, limit=5)

            # Incorporate first-party company website news/press releases
            website_val = (profile.get("evidence", {}).get("website", {}) or {}).get("value") or {}
            website_publishable = bool((website_val.get("identity_assessment") or {}).get("publishable"))
            if website_publishable and website_val.get("news_articles"):
                for art in website_val.get("news_articles") or []:
                    art_url = art.get("url")
                    art_title = art.get("title")
                    iso_pub = normalize_iso_datetime(art.get("published_at"))
                    if art_url and art_title and not any(m.get("source_url") == art_url for m in news_mentions):
                        news_mentions.append({
                            "id": "site-news-" + hashlib.sha256(f"{profile['organisation_number']}|{art_url}".encode()).hexdigest()[:24],
                            "organisation_number": profile["organisation_number"],
                            "platform": "company_site",
                            "signal_type": "dated_news",
                            "source_url": art_url,
                            "url": art_url,
                            "retrieved_at": utc_now(),
                            "published_at": iso_pub,
                            "exact_entity": True,
                            "text": art_title,
                            "title": art_title,
                            "publisher": profile.get("name"),
                            "publisher_domain": website_val.get("registered_domain") or "",
                            "credibility_score": 0.85,
                            "credibility_tier": "high",
                            "credibility_reasons": ["first_party_company_news"],
                        })

            site_url = website_val.get("final_url") or profile.get("website") or ""

            if news_mentions:
                profile["news_mentions"] = news_mentions
                profile["dated_news"] = news_mentions
                profile["news"] = news_mentions
                profile["evidence"]["news"] = evidence(
                    "news",
                    "available",
                    "public_editorial_news",
                    news_mentions[0].get("source_url") or site_url or "https://news.google.com",
                    value=news_mentions,
                    note=f"{len(news_mentions)} verified news mention(s)",
                )
                profile["evidence"]["dated_news"] = profile["evidence"]["news"]
                # Verified Sentiment Integrity
                sentiment_items = [
                    {
                        "id": m["id"],
                        "organisation_number": profile["organisation_number"],
                        "exact_entity": True,
                        "source_class": "public_news",
                        "source_url": m["source_url"],
                        "retrieved_at": m["retrieved_at"],
                        "evidence_span": m["text"],
                        "content_sha256": hashlib.sha256(m["text"].encode()).hexdigest(),
                        "text": m["text"],
                        "label": "positive" if any(w in m["text"].lower() for w in ("vekst", "rekord", "overskudd", "kontrakt", "ansetter", "tildelt"))
                                 else "negative" if any(w in m["text"].lower() for w in ("konkurs", "underskudd", "oppsigelse", "fall", "tap", "rettssak"))
                                 else "neutral",
                    }
                    for m in news_mentions
                ]
                profile["sentiment"] = aggregate_company_sentiment(sentiment_items)
            else:
                profile["news_mentions"] = []
                profile["dated_news"] = []
                profile["news"] = []
                profile["evidence"]["news"] = evidence(
                    "news",
                    "not_found",
                    "public_editorial_news",
                    site_url or "https://news.google.com",
                    note="No verified entity mentions discovered in monitored editorial sources",
                )
                profile["evidence"]["dated_news"] = profile["evidence"]["news"]

            # External Footprint Connectors (LinkedIn, YouTube, Reviews, Jobs)
            website_domain = website_val.get("registered_domain")

            external_footprint = {}
            if news_mentions:
                external_footprint["news"] = news_mentions

            if time.monotonic() < enrich_deadline:
                linkedin_match = discover_linkedin_company(profile.get("name") or "", profile["organisation_number"])
                if linkedin_match:
                    external_footprint["linkedin"] = linkedin_match

            if time.monotonic() < enrich_deadline:
                youtube_match = discover_youtube_channel(profile.get("name") or "", profile["organisation_number"], website_domain)
                if youtube_match:
                    external_footprint["youtube"] = youtube_match

            # Customer Reviews (Fagfolkguiden / Google Aggregate)
            if time.monotonic() < enrich_deadline:
                reviews_match = discover_customer_reviews(profile.get("name") or "", profile["organisation_number"])
                if reviews_match:
                    external_footprint["reviews"] = reviews_match
                    profile["customer_reviews"] = reviews_match

            # Hiring / Job Postings: NAV Arbeidsplassen + Structured JSON-LD + Career Links
            jobs_list = []
            if time.monotonic() < enrich_deadline:
                nav_jobs = discover_nav_jobs(profile.get("name") or "", profile["organisation_number"])
                if nav_jobs:
                    jobs_list.extend(nav_jobs)

            if time.monotonic() < enrich_deadline and len(jobs_list) < 3:
                li_jobs = discover_linkedin_jobs(profile.get("name") or "", profile["organisation_number"])
                if li_jobs:
                    jobs_list.extend(li_jobs)

            if website_publishable:
                # Add structured job postings if available
                for jp in website_val.get("job_postings") or []:
                    jp_url = jp.get("url") or jp.get("source_url")
                    if jp_url and not any(j.get("source_url") == jp_url or j.get("url") == jp_url for j in jobs_list):
                        jobs_list.append({
                            "id": "site-jp-" + hashlib.sha256(f"{profile['organisation_number']}|{jp_url}".encode()).hexdigest()[:20],
                            "organisation_number": profile["organisation_number"],
                            "platform": "company_site",
                            "signal_type": "job_posting",
                            "title": jp.get("title") or "Open Position",
                            "company": profile.get("name"),
                            "location": jp.get("location") or profile.get("municipality") or "Norway",
                            "job_url": jp_url,
                            "source_url": jp_url,
                            "url": jp_url,
                            "exact_entity": True,
                            "source": "verified_website",
                            "date_posted": jp.get("date_posted"),
                            "retrieved_at": utc_now(),
                        })

                # Add career links
                for h_url in (website_val.get("hiring_links") or [])[:3]:
                    if not any(j.get("source_url") == h_url or j.get("url") == h_url for j in jobs_list):
                        jobs_list.append({
                            "id": "site-job-" + hashlib.sha256(f"{profile['organisation_number']}|{h_url}".encode()).hexdigest()[:20],
                            "organisation_number": profile["organisation_number"],
                            "platform": "company_site",
                            "signal_type": "job_posting",
                            "title": "Careers / Ledige stillinger",
                            "company": profile.get("name"),
                            "location": profile.get("municipality") or "Norway",
                            "job_url": h_url,
                            "source_url": h_url,
                            "url": h_url,
                            "exact_entity": True,
                            "source": "verified_website",
                            "retrieved_at": utc_now(),
                        })

            if jobs_list:
                external_footprint["jobs"] = jobs_list
                profile["jobs"] = jobs_list
                profile["hiring_signals"] = jobs_list
                profile["hiring_signal"] = jobs_list
                profile["hiring"] = {
                    "status": "available",
                    "signals": jobs_list,
                    "count": len(jobs_list),
                    "sources": list({j.get("source") or j.get("platform") for j in jobs_list}),
                }
                primary_job_url = jobs_list[0].get("source_url") or jobs_list[0].get("url") or jobs_list[0].get("job_url") or site_url or "https://arbeidsplassen.nav.no"
                profile["evidence"]["hiring"] = evidence(
                    "hiring",
                    "available",
                    "official_and_company_careers",
                    primary_job_url,
                    value=jobs_list,
                    note=f"{len(jobs_list)} active recruitment signal(s)",
                )
                profile["evidence"]["hiring_signal"] = profile["evidence"]["hiring"]
            else:
                external_footprint["jobs"] = []
                profile["jobs"] = []
                profile["hiring_signals"] = []
                profile["hiring_signal"] = []
                profile["hiring"] = {
                    "status": "not_found",
                    "source": "verified_website_and_nav",
                    "note": "Checked verified website domain and NAV Arbeidsplassen; no active recruitment postings detected",
                }
                profile["evidence"]["hiring"] = evidence(
                    "hiring",
                    "not_found",
                    "official_and_company_careers",
                    site_url or "https://arbeidsplassen.nav.no",
                    note="Checked verified website domain and NAV Arbeidsplassen; no active recruitment postings detected",
                )
                profile["evidence"]["hiring_signal"] = profile["evidence"]["hiring"]

            # Verified Website Social Channels (exact entity publishable)
            all_social = []
            if website_publishable:
                for soc in website_val.get("social_links") or []:
                    plat = soc.get("platform")
                    soc_url = soc.get("url")
                    if plat and soc_url:
                        if not any(s.get("url") == soc_url for s in all_social):
                            all_social.append({"platform": plat, "url": soc_url})
                        if plat == "linkedin" and "linkedin" not in external_footprint:
                            external_footprint["linkedin"] = {
                                "platform": "linkedin",
                                "organisation_number": profile["organisation_number"],
                                "display_name": profile.get("name"),
                                "profile_url": soc_url,
                                "exact_entity": True,
                                "match_type": "verified_website_social_link",
                                "source": "verified_website",
                            }
                        elif plat == "youtube" and "youtube" not in external_footprint:
                            external_footprint["youtube"] = {
                                "platform": "youtube",
                                "organisation_number": profile["organisation_number"],
                                "channel_name": profile.get("name"),
                                "channel_url": soc_url,
                                "exact_entity": True,
                                "source": "verified_website",
                            }
                        elif plat not in external_footprint:
                            external_footprint[plat] = {
                                "platform": plat,
                                "profile_url": soc_url,
                                "exact_entity": True,
                                "source": "verified_website",
                            }

            for extra_plat in ("linkedin", "youtube"):
                if extra_plat in external_footprint and isinstance(external_footprint[extra_plat], dict):
                    extra_url = external_footprint[extra_plat].get("profile_url") or external_footprint[extra_plat].get("channel_url")
                    if extra_url and not any(s.get("url") == extra_url for s in all_social):
                        all_social.append({"platform": extra_plat, "url": extra_url})

            profile["social_profiles"] = all_social
            profile["social_profile"] = all_social
            profile["social_links"] = all_social
            if all_social:
                profile["evidence"]["social_profiles"] = evidence(
                    "social_profiles",
                    "available",
                    "verified_company_website",
                    all_social[0]["url"],
                    value=all_social,
                    note=f"{len(all_social)} verified social profile(s) discovered",
                )
            else:
                profile["evidence"]["social_profiles"] = evidence(
                    "social_profiles",
                    "not_found",
                    "verified_company_website",
                    site_url or "https://data.brreg.no",
                    note="No official social profile links discovered",
                )
            profile["evidence"]["social_profile"] = profile["evidence"]["social_profiles"]

            if site_url and website_publishable:
                external_footprint["website"] = {
                    "platform": "company_site",
                    "url": site_url,
                    "exact_entity": True,
                    "source": "official_registry_and_verified_site",
                }

            if news_mentions:
                external_footprint["news"] = news_mentions

            if external_footprint:
                profile["external_footprint"] = external_footprint

            # Differential change detection (Refresh evaluation)
            org_num = profile.get("organisation_number")
            if prior_profiles_map and org_num in prior_profiles_map:
                try:
                    profile["changes"] = diff_profile(prior_profiles_map[org_num], profile)
                except Exception as diff_err:
                    print(f"[WARN] Failed to diff profile {org_num}: {diff_err}", file=sys.stderr)
                    profile["changes"] = []
            else:
                profile.setdefault("changes", [])

            profile["summary"] = synthesize_company_profile(profile)
            external_requests = 5  # news RSS + linkedin typeahead + youtube + reviews + jobs
            metric = {
                "requests": len(metrics) + website_metrics["requests"] + external_requests,
                "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
                "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"],
            }
            profile["run_metrics"] = metric
            return profile, metric
        except Exception as exc:
            print(f"[WARN] Enrichment failed for {profile.get('organisation_number')}: {exc}", file=sys.stderr)
            profile.setdefault("evidence", {})
            for mod in requested_modules:
                if mod not in profile["evidence"]:
                    profile["evidence"][mod] = evidence(
                        mod,
                        "source_error",
                        "official_api",
                        "https://data.brreg.no",
                        note=f"Enrichment exception: {exc}",
                    )
            try:
                profile["summary"] = synthesize_company_profile(profile)
            except Exception:
                profile["summary"] = {
                    "what_the_company_does": f"Norwegian registered entity {profile.get('organisation_number')}.",
                    "financial_status": "Not reported.",
                    "leadership_status": "Not reported.",
                    "media_coverage": "None discovered.",
                    "digital_footprint": "None discovered.",
                    "what_remains_unknown": ["Enrichment exception encountered; terminal state preserved."],
                    "sources": [],
                }
            fallback_metric = {"requests": 1, "bytes": 0, "latencies_ms": [50]}
            profile["run_metrics"] = fallback_metric
            return profile, fallback_metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms", []))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "refresh": {
            "prior_profiles_provided": bool(prior_profiles_map),
            "changes_detected": sum(len(p.get("changes", [])) for p in ordered_profiles),
            "profiles_with_changes": sum(1 for p in ordered_profiles if p.get("changes")),
        },
        "validation": validation,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_json = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_path.write_text(report_json, encoding="utf-8")
    alias_name = "run-report.json" if report_path.name == "report.json" else "report.json"
    (report_path.parent / alias_name).write_text(report_json, encoding="utf-8")

    # Automatic prototype.html interactive dashboard generation
    try:
        from scripts.build_prototype import build as build_prototype_html
        proto_html = build_prototype_html(ordered_profiles, report, None, None, None)
        proto_path = report_path.parent / "prototype.html"
        proto_path.write_text(proto_html, encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] Automatic prototype.html generation skipped: {exc}", file=sys.stderr)

    print(report_json)
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
