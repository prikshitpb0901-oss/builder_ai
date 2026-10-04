"""News credibility and anti-fake-news verification engine.

Provides multi-layer verification to distinguish legitimate, editorially governed
journalism from fake news, disinformation, content farms, and clickbait.

Layers of defense:
1. Publisher Governance & Whitelist (NRK, TV2, E24, DN, VG, regional Norwegian press, NTB)
2. Domain Verification & Blacklist (Norid .no regulation vs. anonymous/content farm domains)
3. Headline Manipulation & Clickbait Detection (Norwegian & English sensationalism patterns)
4. Exact Legal Entity Gating (prevent attributing unrelated stories to companies)
5. Temporal Integrity (future date detection, reasonable lookback window)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

# ── LAYER 1: Reputable Editorial Publishers ──────────────────────────────────
# Regulated under the Norwegian Press Code of Ethics (Vær Varsom-plakaten)
# or accredited international news agencies.
TRUSTED_PUBLISHERS = {
    # National news & broadcasting
    "nrk.no", "tv2.no", "vg.no", "dagbladet.no", "aftenposten.no",
    "dagsavisen.no", "klassekampen.no", "morgenbladet.no", "nettavisen.no",
    # Financial & business press
    "dn.no", "e24.no", "finansavisen.no", "kapital.no", "borsen.no",
    # Technology & industry journals
    "shifter.no", "tek.no", "digi.no", "tu.no", "computerworld.no",
    "kyst.no", "intrafish.no", "bygg.no", "at.no",
    # Regional & local newspapers with established editorial boards
    "bt.no", "adressa.no", "aftenbladet.no", "fvn.no", "nordlys.no",
    "an.no", "itromso.no", "ba.no", "ta.no", "sb.no", "rbnett.no",
    "romsdals-budstikke.no", "smp.no", "firda.no", "firdaposten.no",
    "fjordingen.no", "fjt.no", "h-avis.no", "ha-halden.no", "moss-avis.no",
    "op.no", "oa.no", "gd.no", "dt.no", "tb.no", "varden.no", "pd.no",
    "nyss.no", "telemarkavisa.no", "lofotposten.no", "vol.no", "fremo.no",
    "sunnmoringen.no", "morenytt.no", "driva.no", "auraavis.no",
    # Nordic / International wire services & public bodies
    "ntb.no", "reuters.com", "bloomberg.com", "apnews.com",
    "regjeringen.no", "ssb.no", "brreg.no", "toll.no", "skatteetaten.no",
}

# ── LAYER 2: Known Disinformation / Spam / Content Farm Blacklist ───────────
BLACKLISTED_DOMAINS = {
    # Uncurated self-publishing / user-generated content
    "medium.com", "blogspot.com", "wordpress.com", "tumblr.com",
    "substack.com", "wixsite.com", "weebly.com", "squarespace.com",
    "hubpages.com", "buzzfeed.com",
    # Content scrapers / tabloid clickbait farms
    "dailymail.co.uk", "thesun.co.uk", "nationalenquirer.com",
    # Known state-sponsored or fringe disinformation networks
    "rt.com", "sputniknews.com", "globalresearch.ca", "infowars.com",
    "naturalnews.com", "breitbart.com", "beforeitsnews.com",
    "newspunch.com", "thegatewaypundit.com", "zerohedge.com",
}

# Fraud, scam, and malicious phishing patterns in news headlines
FRAUD_AND_SCAM_NEWS_PATTERNS = [
    r"\b(?:crypto\s+giveaway|bitcoin\s+scam|free\s+tokens|claim\s+reward)\b",
    r"\b(?:bankid\s+svindel|vipps\s+svindel|investeringssvindel|phishing\s+angrep)\b",
    r"\b(?:pyramidespill|ponzi\s+scheme|get\s+rich\s+quick|t\.me\/|wa\.me\/)\b",
    r"\b(?:hack\s+konto|passord\s+stjålet|sikkerhetshull\s+svindel)\b",
    r"\b(?:fake\s+giveaway|airdrop\s+scam|wallet\s+drainer)\b",
]

# Sensationalist / clickbait markers in Norwegian & English
CLICKBAIT_PATTERNS = [
    r"\bdu vil ikke tro\b",
    r"\bsjokk(?:erend|ert|)\b",
    r"\bavslørt\b",
    r"\bhemmelig(?:het|)\b",
    r"\bderfor raser\b",
    r"\bher er trikset\b",
    r"\bkatastrofe\b",
    r"\bpanikk\b",
    r"\byou won't believe\b",
    r"\bshocking\b",
    r"\bsecret revealed\b",
    r"\bthis one trick\b",
    r"\bclick here\b",
    r"\bmust read\b",
    r"\bblow your mind\b",
]

LEGAL_SUFFIXES = {"as", "asa", "sa", "ba", "da", "ans", "enk", "nuf", "sti"}


def extract_domain(url_or_domain: str) -> str:
    """Extract clean registrable domain (e.g. 'nyss.no', 'e24.no') from a URL or text."""
    raw = str(url_or_domain or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        hostname = urlparse(raw).hostname or ""
        hostname = hostname.removeprefix("www.")
        parts = hostname.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return hostname
    except Exception:
        return ""


def detect_clickbait(title: str) -> list[str]:
    """Identify manipulative or sensationalist clickbait signals in headlines."""
    flags: list[str] = []
    text = str(title or "").strip()
    if not text:
        return ["empty_title"]

    lower = text.lower()
    for pattern in CLICKBAIT_PATTERNS:
        if re.search(pattern, lower):
            flags.append(f"clickbait_phrase:{pattern.strip(r'\\b')}")

    # Punctuation screams
    if text.count("!") >= 2:
        flags.append("excessive_exclamation")
    if text.count("?") >= 2:
        flags.append("excessive_questions")

    # ALL CAPS shouting (min length 12)
    letters = [ch for ch in text if ch.isalpha()]
    if letters and len(letters) >= 12 and all(ch.isupper() for ch in letters):
        flags.append("all_caps_title")

    # Spam length anomalies
    if len(text) < 15:
        flags.append("suspiciously_short")

    return flags


def verify_entity_in_headline(company_name: str, title: str) -> dict[str, bool | str]:
    """Verify that headline specifically references the company and not an ambiguous homonym."""
    comp_tokens = re.findall(r"[a-z0-9æøå]+", str(company_name or "").casefold())
    title_clean = str(title or "").rsplit(" - ", 1)[0]
    title_tokens = re.findall(r"[a-z0-9æøå]+", title_clean.casefold())

    if not comp_tokens or not title_tokens:
        return {"matched": False, "mode": "none"}

    # Exact token sequence match
    n_comp = len(comp_tokens)
    for i in range(len(title_tokens) - n_comp + 1):
        if title_tokens[i:i + n_comp] == comp_tokens:
            return {"matched": True, "mode": "exact_legal_name"}

    # Base name without legal suffix (e.g. 'Byggmester Flo' for 'Byggmester Flo AS')
    base_tokens = [t for t in comp_tokens if t not in LEGAL_SUFFIXES]
    if base_tokens and len(base_tokens) >= 2:
        n_base = len(base_tokens)
        for i in range(len(title_tokens) - n_base + 1):
            if title_tokens[i:i + n_base] == base_tokens:
                return {"matched": True, "mode": "multi_token_base_name"}

    return {"matched": False, "mode": "none"}


def evaluate_news_credibility(
    title: str,
    publisher_name: str,
    publisher_url: str,
    source_link: str,
    published_at: str | None,
    company_name: str,
) -> dict:
    """Comprehensive credibility assessment for a news observation.

    Scores 5 dimensions (0.0 to 1.0 total):
      1. Publisher Trust     (0.00 to 0.35) — Editorial accountability & whitelist
      2. Domain Integrity    (0.00 to 0.20) — Norid .no regulation & blacklist check
      3. Date Validity       (0.00 to 0.15) — Freshness and sanity (no future dates)
      4. Headline Quality    (0.00 to 0.15) — Absence of clickbait / manipulation
      5. Entity Specificity  (0.00 to 0.15) — Exact legal entity alignment

    Returns structured audit trail with score, tier, rejection flags, and reasons.
    """
    reasons: list[str] = []
    fatal_flags: list[str] = []
    score = 0.0

    # Resolve domain from publisher URL or publisher name or link
    pub_domain = extract_domain(publisher_url) or extract_domain(publisher_name) or extract_domain(source_link)

    # ── Fatal Blacklist Check ──
    if pub_domain in BLACKLISTED_DOMAINS:
        fatal_flags.append(f"blacklisted_source:{pub_domain}")
        return {
            "credibility_score": 0.0,
            "credibility_tier": "rejected",
            "is_publishable": False,
            "fatal_flags": fatal_flags,
            "reasons": [f"Source domain {pub_domain} is on known disinformation/content-farm blacklist."],
            "evaluated_domain": pub_domain,
        }

    # ── Fatal Fraud / Scam Headline Pattern Check ──
    lower_title = str(title or "").lower()
    for pattern in FRAUD_AND_SCAM_NEWS_PATTERNS:
        match = re.search(pattern, lower_title)
        if match:
            fatal_flags.append(f"fraud_pattern_detected:{match.group(0)}")
            return {
                "credibility_score": 0.0,
                "credibility_tier": "rejected",
                "is_publishable": False,
                "fatal_flags": fatal_flags,
                "reasons": [f"News headline matched fraudulent or scam pattern: {match.group(0)}"],
                "evaluated_domain": pub_domain,
            }

    # ── 1. Publisher Trust (0.00–0.35) ──
    pub_lower = publisher_name.lower().strip()
    if pub_domain in TRUSTED_PUBLISHERS:
        score += 0.35
        reasons.append(f"trusted_editorial_publisher:{pub_domain}")
    elif any(tp.split(".")[0] in pub_lower for tp in ("nrk", "tv2", "vg", "dn", "e24", "aftenposten")):
        score += 0.30
        reasons.append(f"recognized_media_brand:{pub_lower}")
    elif pub_lower and len(pub_lower) >= 3:
        score += 0.12
        reasons.append(f"named_independent_publisher:{pub_lower}")
    else:
        reasons.append("unidentified_publisher")

    # ── 2. Domain Integrity (0.00–0.20) ──
    if pub_domain.endswith(".no"):
        score += 0.20
        reasons.append(f"norwegian_regulated_domain:{pub_domain}")
    elif pub_domain in TRUSTED_PUBLISHERS:
        score += 0.20
        reasons.append(f"verified_global_domain:{pub_domain}")
    elif pub_domain:
        score += 0.05
        reasons.append(f"generic_domain:{pub_domain}")
    else:
        reasons.append("no_domain_available")

    # ── 3. Date Validity (0.00–0.15) ──
    if published_at:
        try:
            pub_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age_days = (now - pub_dt).days
            if age_days < -1:  # More than 1 day in the future
                score -= 0.20
                fatal_flags.append(f"future_date_manipulation:{published_at}")
                reasons.append("suspicious_future_timestamp")
            elif 0 <= age_days <= 730:
                score += 0.15
                reasons.append(f"valid_fresh_date:{age_days}d_old")
            else:
                score += 0.05
                reasons.append(f"historical_archive:{age_days}d_old")
        except Exception:
            score += 0.04
            reasons.append("unparseable_date_format")
    else:
        reasons.append("missing_publication_date")

    # ── 4. Headline Quality & Manipulation (0.00–0.15) ──
    manipulations = detect_clickbait(title)
    if not manipulations:
        score += 0.15
        reasons.append("clean_journalistic_headline")
    else:
        penalty = min(0.15, 0.06 * len(manipulations))
        score -= penalty
        reasons.extend(manipulations)
        if any("all_caps" in m or "clickbait" in m for m in manipulations):
            reasons.append("sensationalism_detected")

    # ── 5. Entity Specificity (0.00–0.15) ──
    entity_check = verify_entity_in_headline(company_name, title)
    if entity_check["matched"] and entity_check["mode"] == "exact_legal_name":
        score += 0.15
        reasons.append("exact_legal_company_name_in_headline")
    elif entity_check["matched"] and entity_check["mode"] == "multi_token_base_name":
        score += 0.10
        reasons.append("multi_token_base_name_match")
    else:
        score += 0.02
        reasons.append("weak_or_ambiguous_entity_reference")

    final_score = round(max(0.0, min(1.0, score)), 2)

    tier = (
        "high" if final_score >= 0.70 else
        "medium" if final_score >= 0.50 else
        "low" if final_score >= 0.25 else
        "rejected"
    )

    # Publication gate: strict entity alignment required to prevent false attribution
    is_publishable = (
        final_score >= 0.60
        and bool(entity_check.get("matched"))
        and not fatal_flags
    )

    return {
        "credibility_score": final_score,
        "credibility_tier": tier,
        "is_publishable": is_publishable,
        "fatal_flags": fatal_flags,
        "reasons": reasons,
        "evaluated_domain": pub_domain,
    }
