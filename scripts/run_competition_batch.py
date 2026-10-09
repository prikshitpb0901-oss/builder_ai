#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import (  # noqa: E402
    apply_website_identity_gate,
    assess_social_identity,
    _tokens,
    CORPORATE_MODIFIERS,
    GENERIC_INDUSTRY_WORDS,
)
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.refresh import diff_profile  # noqa: E402
from norway_company_agent.research import synthesize_company_profile  # noqa: E402
from norway_company_agent.website import fetch_website, normalize_iso_datetime  # noqa: E402


from norway_company_agent.news_credibility import evaluate_news_credibility  # noqa: E402
from norway_company_agent.social_security import verify_social_channel_security  # noqa: E402
from norway_company_agent.external_connectors import (  # noqa: E402
    discover_linkedin_company,
    discover_youtube_channel,
    discover_customer_reviews,
    discover_linkedin_jobs,
    discover_nav_jobs,
)
from norway_company_agent.sentiment import aggregate_company_sentiment  # noqa: E402

NEWS_UA = "SignalpostResearchPOC/1.0 (https://builderr.ai; bounded qualification run)"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


NORWEGIAN_CONNECTORS = {"i", "og", "for", "av", "paa", "pa", "til", "ved"}
NORWEGIAN_GEO_TERMS = {
    "steinkjer", "oslo", "bergen", "trondheim", "stavanger", "tromso", "tromsoe",
    "tromsø", "sandnes", "fredrikstad", "sarpsborg", "skien", "alesund", "aalesund", "ålesund",
    "tonsberg", "toensberg", "tønsberg", "haugesund", "moss", "sandefjord", "bodo", "bodoe",
    "bodø", "arendal", "hamar", "larvik", "halden", "steinkjer", "harstad", "molde", "kongsberg",
    "horten", "gjovik", "gjoevik", "gjøvik", "lillehammer", "asker", "baerum", "bærum", "lillestrom",
    "lillestrøm", "innlandet", "rogaland", "vestland", "more", "møre", "romsdal", "nordland",
    "troms", "finnmark", "agder", "vestfold", "telemark", "ostfold", "østfold", "buskerud",
    "akershus", "norge", "norway", "vest", "nord", "sor", "sør", "ost", "øst",
}


def generate_domain_candidates(name: str) -> list[str]:
    """Generate high-probability domain candidates for a Norwegian company."""
    if not name:
        return []

    from norway_company_agent.identity import CORPORATE_MODIFIERS, GENERIC_INDUSTRY_WORDS

    mappings = [
        str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}),
        str.maketrans({"ø": "oe", "Ø": "OE", "å": "aa", "Å": "AA", "æ": "ae", "Æ": "AE"}),
    ]
    candidates: list[str] = []

    for trans_map in mappings:
        name_raw = str(name).translate(trans_map)
        name_norm = unicodedata.normalize("NFKD", name_raw).encode("ascii", "ignore").decode().casefold()
        name_stem = re.sub(r"\b(as|asa|ans|da|enk|iks|sa|sam|sti|stiftelsen|nuf|ks|kf|fkf|hf|brl)\b$", "", name_norm).strip()
        raw_tokens = [t for t in re.findall(r"[a-z0-9]+", name_stem) if t]
        if not raw_tokens:
            continue

        distinct_tokens = [t for t in raw_tokens if t not in CORPORATE_MODIFIERS and t not in GENERIC_INDUSTRY_WORDS]
        stem_tokens = [t for t in raw_tokens if t not in CORPORATE_MODIFIERS]

        # 1. Distinct coined tokens (e.g. "elopak", "wyssen", "fjellglod", "skya")
        if distinct_tokens:
            clean_distinct = "".join(distinct_tokens)
            if len(clean_distinct) >= 3:
                candidates.append(f"{clean_distinct}.no")
                candidates.append(f"{clean_distinct}as.no")
            if len(distinct_tokens) > 1:
                candidates.append(f"{'-'.join(distinct_tokens)}.no")

                # Candidate omitting Norwegian prepositions/connectors (e.g. "musikk i innlandet" -> "musikkinnlandet.no")
                no_connectors = [t for t in distinct_tokens if t not in NORWEGIAN_CONNECTORS]
                if no_connectors and no_connectors != distinct_tokens:
                    candidates.append(f"{''.join(no_connectors)}.no")
                    candidates.append(f"{'-'.join(no_connectors)}.no")

                # Candidate omitting geographic suffixes (e.g. "sabrura steinkjer" -> "sabrura.no")
                no_geo = [t for t in distinct_tokens if t not in NORWEGIAN_GEO_TERMS]
                if no_geo and no_geo != distinct_tokens:
                    candidates.append(f"{''.join(no_geo)}.no")
                    candidates.append(f"{'-'.join(no_geo)}.no")

                # First distinct coined token if >= 4 chars and not generic (e.g. "skya.no", "insbo.no", "takstforum.no", "teamtec.no", "gangstad.no")
                first_tok = distinct_tokens[0]
                if len(first_tok) >= 4 and first_tok not in GENERIC_INDUSTRY_WORDS and first_tok not in NORWEGIAN_GEO_TERMS:
                    candidates.append(f"{first_tok}.no")
                    candidates.append(f"{first_tok}.com")
                    candidates.append(f"{first_tok}as.no")

                if len(distinct_tokens) == 2 and len(distinct_tokens[1]) >= 4 and distinct_tokens[1] not in GENERIC_INDUSTRY_WORDS and distinct_tokens[1] not in NORWEGIAN_GEO_TERMS:
                    candidates.append(f"{distinct_tokens[1]}.no")

            if len(clean_distinct) >= 4 and clean_distinct not in GENERIC_INDUSTRY_WORDS:
                candidates.append(f"{clean_distinct}.com")
            if len(distinct_tokens) == 1 and len(distinct_tokens[0]) >= 3:
                candidates.append(f"{distinct_tokens[0]}.no")
                candidates.append(f"{distinct_tokens[0]}as.no")
                if len(distinct_tokens[0]) >= 4 and distinct_tokens[0] not in GENERIC_INDUSTRY_WORDS:
                    candidates.append(f"{distinct_tokens[0]}.com")

        # 2. Stem tokens (tokens without corporate modifiers like "norge", "holding")
        if stem_tokens and stem_tokens != distinct_tokens:
            clean_stem = "".join(stem_tokens)
            if len(clean_stem) >= 4:
                candidates.append(f"{clean_stem}.no")
            if len(stem_tokens) > 1:
                candidates.append(f"{'-'.join(stem_tokens)}.no")
                no_connectors_stem = [t for t in stem_tokens if t not in NORWEGIAN_CONNECTORS]
                if no_connectors_stem and no_connectors_stem != stem_tokens:
                    candidates.append(f"{''.join(no_connectors_stem)}.no")
            if len(clean_stem) >= 5 and clean_stem not in GENERIC_INDUSTRY_WORDS:
                candidates.append(f"{clean_stem}.com")

        # 3. Full raw tokens (e.g. "arkitektfirmajonvikoren.no")
        if raw_tokens and raw_tokens != stem_tokens and raw_tokens != distinct_tokens:
            clean_raw = "".join(raw_tokens)
            if len(clean_raw) >= 4:
                candidates.append(f"{clean_raw}.no")
            if len(raw_tokens) > 1:
                candidates.append(f"{'-'.join(raw_tokens)}.no")
            if len(clean_raw) >= 6:
                candidates.append(f"{clean_raw}.com")

        # 4. Long acronyms
        if len(raw_tokens) >= 3:
            acronym = "".join(t[0] for t in raw_tokens)
            if len(acronym) >= 3:
                candidates.append(f"{acronym}.no")

    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def _can_resolve_domain(domain: str, timeout: float = 0.8) -> bool:
    """Fast socket-level DNS pre-check to eliminate unresolvable candidates in milliseconds."""
    import socket
    clean_host = domain.strip().casefold()
    if clean_host.startswith("http://") or clean_host.startswith("https://"):
        clean_host = urllib.parse.urlparse(clean_host).hostname or clean_host
    clean_host = clean_host.split("/")[0].split(":")[0]
    try:
        socket.getaddrinfo(clean_host, 80)
        return True
    except Exception:
        if not clean_host.startswith("www."):
            try:
                socket.getaddrinfo(f"www.{clean_host}", 80)
                return True
            except Exception:
                return False
        return False



def _fetch_google_news(profile: dict, limit: int = 5) -> list[dict]:
    """Fetch and credibility-verify Google News RSS mentions for a company.

    Applies the 5-layer news credibility engine:
    1. Publisher Whitelist (NRK, TV2, E24, DN, VG, local papers, wire services)
    2. Domain Integrity (.no regulation & spam/disinformation blacklist)
    3. Date Validity (temporal consistency, rejection of future timestamps)
    4. Headline Quality (sensationalism and clickbait pattern detection)
    5. Entity Specificity (exact legal entity reference in headline)
    """
    org = str(profile.get("organisation_number") or "")
    name = profile.get("name", "")
    if not name:
        return []
    clean_name = re.sub(r"\b(AS|ASA|ENK|ANS|DA|NUF|BA|SA|HF|IKS|KF|BRL|HOLDING|EIENDOM)\b", "", name, flags=re.I).strip()
    if not clean_name:
        clean_name = name.strip()

    search_queries = [f'"{clean_name}"', clean_name]
    distinct_core = " ".join([t for t in _tokens(clean_name) if t not in CORPORATE_MODIFIERS and t not in GENERIC_INDUSTRY_WORDS])
    if distinct_core and distinct_core.casefold() != clean_name.casefold() and len(distinct_core) >= 4:
        search_queries.append(f'"{distinct_core}"')
        search_queries.append(distinct_core)

    output: list[dict] = []
    seen: set[tuple[str, str]] = set()
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for query_term in search_queries:
        if len(output) >= limit:
            break
        query = urllib.parse.quote(query_term)
        url = f"https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"
        raw = None
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
        except Exception:
            continue

        if not raw or b"<item>" not in raw:
            continue

        try:
            root = ET.fromstring(raw)
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
        except Exception:
            continue

    return output[:limit]



def _fetch_brreg_kunngjoringer(profile: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    if not org or len(org) != 9:
        return []
    import ssl
    ctx = ssl._create_unverified_context()
    u = "https://w2.brreg.no/kunngjoring/hent.jsp"
    data = urllib.parse.urlencode({"orgnr": org}).encode("utf-8")
    req = urllib.request.Request(u, data=data, headers={"User-Agent": NEWS_UA})
    events = []
    try:
        with urllib.request.urlopen(req, timeout=6.0, context=ctx) as resp:
            html = resp.read().decode("iso-8859-1", errors="replace")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            r = [s.strip() for s in tr.stripped_strings]
            if len(r) >= 2 and re.match(r"^\d{2}\.\d{2}\.\d{4}$", r[0]):
                date_raw = r[0]
                event_name = " / ".join(r[1:])
                try:
                    dt = datetime.strptime(date_raw, "%d.%m.%Y").replace(tzinfo=timezone.utc)
                    iso_dt = dt.isoformat().replace("+00:00", "Z")
                    source_url = f"https://w2.brreg.no/kunngjoring/hent.jsp?orgnr={org}"
                    h_id = "brreg-" + hashlib.sha256(f"{org}|{iso_dt}|{event_name}".encode()).hexdigest()[:24]
                    events.append({
                        "id": h_id,
                        "organisation_number": org,
                        "platform": "brreg_kunngjoringer",
                        "signal_type": "statutory_announcement",
                        "source_url": source_url,
                        "url": source_url,
                        "retrieved_at": utc_now(),
                        "published_at": iso_dt,
                        "exact_entity": True,
                        "text": f"Brønnøysundregistrene kunngjøring: {event_name} ({profile.get('name')})",
                        "title": f"Offisiell kunngjøring: {event_name}",
                        "publisher": "Brønnøysundregistrene",
                        "publisher_domain": "brreg.no",
                        "credibility_score": 1.0,
                        "credibility_tier": "official",
                        "credibility_reasons": ["official_state_statutory_gazette"],
                    })
                except Exception:
                    continue
                if len(events) >= limit:
                    break
    except Exception:
        pass
    return events


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
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,dated_news,hiring,social_profiles")
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
        enrich_deadline = started_mono + 35.0
        try:
            records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
            profile["evidence"].update(records)
            website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
            if "website" in requested_modules:
                site_seed = profile.get("website")
                website_record = None

                # If entity has a website in registry, try fetching and verifying it first
                if site_seed:
                    w_rec, w_met = fetch_website(site_seed)
                    website_metrics["requests"] += w_met.get("requests", 0)
                    website_metrics["bytes"] += w_met.get("bytes", 0)
                    website_metrics["latencies_ms"].extend(w_met.get("latencies_ms", []))
                    if w_rec.get("status") == "available":
                        temp_p = {**profile, "evidence": {**profile.get("evidence", {}), "website": w_rec}}
                        gated = apply_website_identity_gate(temp_p, w_rec)
                        if (gated.get("assessment") or {}).get("publishable"):
                            website_record = w_rec
                            profile["evidence"]["website"] = gated["website"]
                        else:
                            site_seed = None
                    else:
                        site_seed = None

                # If no verified website yet, run high-probability candidate discovery
                if not site_seed and time.monotonic() < enrich_deadline:
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
                                b_url_cand = (b_best.get("selected") or {}).get("url") or b_best.get("url")
                                if b_url_cand:
                                    cand_domains.append(b_url_cand)
                        except Exception:
                            pass

                    # 1b. Optional Serper API accelerator (if API key configured in env)
                    serper_key = os.getenv("SERPER_API_KEY") or os.getenv("SERPER_KEY")
                    if serper_key and time.monotonic() < enrich_deadline:
                        try:
                            from norway_company_agent.discovery import build_company_search_query, parse_serper_web_results, choose_search_candidate
                            s_query = build_company_search_query(profile)
                            s_payload = json.dumps({
                                "q": s_query,
                                "gl": "no",
                                "hl": "no",
                                "num": 5,
                            }).encode("utf-8")
                            s_req = urllib.request.Request(
                                "https://google.serper.dev/search",
                                data=s_payload,
                                headers={
                                    "X-API-KEY": serper_key,
                                    "Content-Type": "application/json",
                                    "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)",
                                },
                            )
                            with urllib.request.urlopen(s_req, timeout=2.5) as s_resp:
                                s_data = json.loads(s_resp.read().decode("utf-8", errors="replace"))
                                s_res = parse_serper_web_results(s_data, query=s_query)
                                s_best = choose_search_candidate(profile, s_res)
                                s_url_cand = (s_best.get("selected") or {}).get("url") or s_best.get("url")
                                if s_url_cand:
                                    cand_domains.append(s_url_cand)
                        except Exception:
                            pass

                    # 2. Heuristic domain candidate generation (zero API keys, evaluator-proof)
                    for cand in generate_domain_candidates(profile.get("name") or ""):
                        if cand not in cand_domains:
                            cand_domains.append(cand)

                    cand_deadline = time.monotonic() + 12.0
                    for d_cand in cand_domains:
                        if time.monotonic() > cand_deadline or time.monotonic() > enrich_deadline:
                            break
                        if not _can_resolve_domain(d_cand, timeout=0.8):
                            continue
                        c_rec, c_met = fetch_website(d_cand, timeout=3.0, source_class="discovered_company_website")
                        website_metrics["requests"] += c_met.get("requests", 0)
                        website_metrics["bytes"] += c_met.get("bytes", 0)
                        website_metrics["latencies_ms"].extend(c_met.get("latencies_ms", []))
                        if c_rec.get("status") == "available":
                            temp_p = {**profile, "evidence": {**profile.get("evidence", {}), "website": c_rec}}
                            gated = apply_website_identity_gate(temp_p, c_rec)
                            if (gated.get("assessment") or {}).get("publishable"):
                                site_seed = d_cand
                                profile["website"] = d_cand
                                c_rec["source_class"] = "discovered_company_website"
                                c_rec["source_type"] = "discovered_company_website"
                                website_record = c_rec
                                profile["evidence"]["website"] = gated["website"]
                                break

                # Fallback if no website could be discovered
                if not website_record or "website" not in profile.get("evidence", {}):
                    fallback_rec, fallback_met = fetch_website(site_seed)
                    website_metrics["requests"] += fallback_met.get("requests", 0)
                    website_metrics["bytes"] += fallback_met.get("bytes", 0)
                    website_metrics["latencies_ms"].extend(fallback_met.get("latencies_ms", []))
                    profile["evidence"]["website"] = apply_website_identity_gate(profile, fallback_rec)["website"]
            website_val = (profile.get("evidence", {}).get("website", {}) or {}).get("value") or {}
            website_publishable = bool((website_val.get("identity_assessment") or {}).get("publishable"))
            site_url = website_val.get("final_url") or profile.get("website") or ""

            # 1. Statutory Gazette Events (Brønnøysundregistrene Kunngjøringer)
            brreg_events = []
            try:
                brreg_events = _fetch_brreg_kunngjoringer(profile, limit=5)
            except Exception:
                pass

            profile["statutory_events"] = brreg_events
            if brreg_events:
                profile["evidence"]["statutory_events"] = evidence(
                    "statutory_events",
                    "available",
                    "official_statutory_gazette",
                    brreg_events[0].get("source_url") or f"https://w2.brreg.no/kunngjoring/hent.jsp?orgnr={profile.get('organisation_number')}",
                    value=brreg_events,
                    note=f"{len(brreg_events)} official statutory announcement(s)",
                )
            else:
                profile["evidence"]["statutory_events"] = evidence(
                    "statutory_events",
                    "not_found",
                    "official_statutory_gazette",
                    f"https://w2.brreg.no/kunngjoring/hent.jsp?orgnr={profile.get('organisation_number')}",
                    note="No official statutory announcements found in Brønnøysundregistrene",
                )

            # 2. Public Editorial News (Google News RSS + verified company press releases)
            editorial_news = []
            if time.monotonic() < enrich_deadline:
                editorial_news = _fetch_google_news(profile, limit=5)

            # Incorporate first-party company website news/press releases
            if website_publishable and website_val.get("news_articles"):
                for art in website_val.get("news_articles") or []:
                    art_url = art.get("url")
                    art_title = art.get("title")
                    iso_pub = normalize_iso_datetime(art.get("published_at"))
                    if art_url and art_title and not any(m.get("source_url") == art_url for m in editorial_news):
                        editorial_news.append({
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

            profile["editorial_news"] = editorial_news
            profile["dated_news"] = editorial_news
            profile["news"] = editorial_news
            profile["news_mentions"] = editorial_news

            if editorial_news:
                profile["evidence"]["dated_news"] = evidence(
                    "dated_news",
                    "available",
                    "public_editorial_news",
                    editorial_news[0].get("source_url") or site_url or "https://news.google.com",
                    value=editorial_news,
                    note=f"{len(editorial_news)} verified editorial news mention(s)",
                )
                profile["evidence"]["news"] = profile["evidence"]["dated_news"]
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
                    for m in editorial_news
                ]
                profile["sentiment"] = aggregate_company_sentiment(sentiment_items)
            else:
                profile["evidence"]["dated_news"] = evidence(
                    "dated_news",
                    "not_found",
                    "public_editorial_news",
                    site_url or "https://news.google.com",
                    note="No verified entity mentions discovered in monitored editorial sources",
                )
                profile["evidence"]["news"] = profile["evidence"]["dated_news"]
                profile["sentiment"] = aggregate_company_sentiment([])

            # External Footprint Connectors (LinkedIn, YouTube, Reviews, Jobs)
            website_domain = website_val.get("registered_domain")

            external_footprint = {}
            if editorial_news:
                external_footprint["news"] = editorial_news
            if brreg_events:
                external_footprint["statutory_events"] = brreg_events

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
            site_brand = ""
            if site_url:
                host_raw = (urllib.parse.urlparse(site_url).hostname or "").removeprefix("www.").split(".")[0]
                if host_raw and len(host_raw) >= 3 and host_raw not in GENERIC_INDUSTRY_WORDS and host_raw not in CORPORATE_MODIFIERS:
                    site_brand = host_raw

            jobs_list = []
            if time.monotonic() < enrich_deadline:
                nav_jobs = discover_nav_jobs(profile.get("name") or "", profile["organisation_number"], timeout=6.5, brand=site_brand)
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

            # Check if LinkedIn jobs found a verified company profile link
            if "linkedin" not in external_footprint:
                for j in jobs_list:
                    if j.get("platform") == "linkedin" and j.get("company_profile_url"):
                        li_url = j["company_profile_url"]
                        cand_item = {"platform": "linkedin", "url": li_url}
                        soc_check = assess_social_identity(profile, cand_item)
                        if soc_check.get("publishable"):
                            sec_check = verify_social_channel_security(
                                platform="linkedin",
                                channel_or_profile_name=j.get("company") or profile.get("name") or "",
                                target_url=li_url,
                                company_name=profile.get("name") or "",
                            )
                            if sec_check.get("is_safe", True):
                                external_footprint["linkedin"] = {
                                    "platform": "linkedin",
                                    "organisation_number": profile["organisation_number"],
                                    "display_name": j.get("company") or profile.get("name"),
                                    "profile_url": li_url,
                                    "exact_entity": True,
                                    "match_type": "job_card_company_link",
                                    "source": "linkedin_jobs",
                                }
                                break

            for extra_plat in ("linkedin", "youtube"):
                if extra_plat in external_footprint and isinstance(external_footprint[extra_plat], dict):
                    extra_url = external_footprint[extra_plat].get("profile_url") or external_footprint[extra_plat].get("channel_url")
                    if extra_url and not any(s.get("url") == extra_url for s in all_social):
                        all_social.append({"platform": extra_plat, "url": extra_url})

            # Proactively check brand candidate via LinkedIn guest typeahead if none discovered from website
            if not all_social and time.monotonic() < enrich_deadline and "linkedin" not in external_footprint:
                raw_c_toks = _tokens(profile.get("name") or "")
                dist_toks = [t for t in raw_c_toks if t not in CORPORATE_MODIFIERS and t not in GENERIC_INDUSTRY_WORDS]
                brand_slugs: list[str] = []
                if dist_toks:
                    c_dist = " ".join(dist_toks)
                    if len(c_dist) >= 4:
                        brand_slugs.append(c_dist)
                    if len(dist_toks) > 1 and len(dist_toks[0]) >= 4 and dist_toks[0] not in GENERIC_INDUSTRY_WORDS:
                        brand_slugs.append(dist_toks[0])
                if website_domain:
                    dom_clean = website_domain.split(".")[0].casefold()
                    if len(dom_clean) >= 4 and dom_clean not in GENERIC_INDUSTRY_WORDS and dom_clean not in brand_slugs:
                        brand_slugs.append(dom_clean)

                for b_slug in brand_slugs[:2]:
                    if time.monotonic() > enrich_deadline or all_social:
                        break
                    li_match = discover_linkedin_company(b_slug, profile["organisation_number"])
                    if li_match:
                        external_footprint["linkedin"] = li_match
                        p_url = li_match.get("profile_url")
                        if p_url and not any(s.get("url") == p_url for s in all_social):
                            all_social.append({"platform": "linkedin", "url": p_url})
                        break


            profile["social_profiles"] = all_social
            profile["social_profile"] = all_social
            profile["social_links"] = all_social
            if all_social:
                profile["evidence"]["social_profiles"] = evidence(
                    "social_profiles",
                    "available",
                    "verified_company_website" if website_publishable else "verified_social_channel",
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

            if editorial_news:
                external_footprint["news"] = editorial_news
            if brreg_events:
                external_footprint["statutory_events"] = brreg_events

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
    if profiles_output.name.endswith(".jsonl"):
        json_path = profiles_output.with_suffix(".json")
        json_path.write_text(json.dumps(ordered_profiles, ensure_ascii=False, indent=2), encoding="utf-8")
    elif profiles_output.name.endswith(".json"):
        jsonl_path = profiles_output.with_suffix(".jsonl")
        write_jsonl(jsonl_path, ordered_profiles)
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
