from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}
CORPORATE_MODIFIERS = {
    "holding", "eiendom", "eiendommer", "invest", "drift", "utvikling",
    "forvaltning", "group", "gruppen", "norge", "norway", "avd", "avdeling", "vgs", "filial",
}
GENERIC_INDUSTRY_WORDS = {
    "frakt", "transport", "logistikk", "klinikk", "klinikken", "tannlege", "tannlegene",
    "regnskap", "revisjon", "okonomi", "advokat", "advokatene", "eiendom", "eiendommer", "bygg", "anlegg",
    "consulting", "consult", "it", "tech", "technology", "service", "services", "solutions",
    "seafood", "fisk", "kjott", "mat", "kafe", "restaurant", "bar", "hotel", "hotell",
    "capital", "kapital", "invest", "investor", "holding", "drift", "utvikling", "partner",
    "partners", "gruppen", "group", "norge", "norway", "nordic", "scandinavia", "media",
    "design", "foto", "musikk", "kunst", "helse", "terapi", "auto", "bil", "motor", "energi",
    "solar", "kraft", "kraftverk", "vind", "marine", "shipping", "handel", "butikk", "shop",
    "online", "digital", "studio", "arkitekt", "arkitekter", "frisor", "frisorer", "salong",
    "veterinar", "dyreklinikk", "taxi", "buss", "renhold", "vask", "sikkerhet", "security",
    "miljo", "sport", "fitness", "trening", "care", "pharma", "lab", "kjemi",
    "vintage", "equity", "fund", "funds", "venture", "ventures", "finance", "global", "international",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    source_class = website.get("source_class") or website.get("source_type") or "registry_linked_company_website"
    is_discovered = (source_class == "discovered_company_website")

    core = _tokens(profile.get("name"))
    core_distinct = [t for t in core if t not in CORPORATE_MODIFIERS]
    core_non_generic = [t for t in core_distinct if t not in GENERIC_INDUSTRY_WORDS]

    # Gate Requirement 1: Fail-closed on empty tokens
    if not core:
        return {
            "status": "rejected",
            "score": 0.0,
            "publishable": False,
            "is_fraud": False,
            "is_parked": False,
            "legal_name_tokens": [],
            "matched_tokens": [],
            "reasons": ["entity legal name contains no evaluable tokens"],
            "method": "deterministic_name_org_evidence_v2",
        }

    final_url = value.get("final_url") or website.get("source_url") or ""
    parsed_final = urllib.parse.urlparse(final_url)
    hostname = (parsed_final.hostname or "").casefold()
    hostname_clean = hostname.removeprefix("www.")
    hostname_tokens = _tokens(hostname_clean)
    hostname_compact = "".join(hostname_tokens)
    is_no_tld = hostname.endswith(".no")

    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}
    homepage_identity_parts = [
        value.get("title"), value.get("description"), value.get("identity_text_excerpt"), hostname, *structured_names,
        rendered.get("title"), value.get("footer_text"),
    ]
    candidate_parts = [
        *homepage_identity_parts, value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
    ]
    candidate_parts.append(rendered.get("main_text_excerpt"))
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(str(part or "") for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    normalized_candidate_text = " ".join(_tokens(candidate_text))
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    compact_candidate = re.sub(r"\D", "", candidate_text)
    compact_homepage_candidate = re.sub(r"\D", "", homepage_candidate_text)
    footer_text = str(value.get("footer_text") or "")
    footer_digits = re.sub(r"\D", "", footer_text)
    org_in_footer = bool(org_digits and (org_digits in footer_digits or org_digits in compact_homepage_candidate))
    legal_org_prefixed = bool(
        org_digits and re.search(rf"(?:org(?:anis[a-z]+)?\.?\s*(?:nr|nummer)?|mva|foretaksregisteret)\D{{0,15}}{org_digits}", candidate_text, re.I)
    )
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons = []

    FRAUD_AND_SCAM_MARKERS = (
        # Online casinos / gambling / betting (common expired domain hijack)
        "casino bonus", "nettcasino", "spilleautomater", "free spins",
        "online casino", "best casino", "betting bonus", "slot machine",
        "spill penger", "norske casino", "gambling", "oddsbonuser",
        # Crypto / Forex / Get-rich scams
        "crypto trading bot", "bitcoin investment", "automated wealth",
        "passive income bot", "forex signals", "binary options",
        "claim your reward", "connect your wallet", "airdrop claim",
        # Phishing / Credential harvesting (BankID / Vipps / Posten imposter)
        "enter your bankid", "verifiser din bankid", "bekreft ditt vipps",
        "ditt vipps er sperret", "sikkerhetsvarsel bankid", "logg inn med bankid",
        "pakkesporing gebyr", "tollgebyr betaling",
        # Tech support / Scareware / Fake virus
        "your computer is infected", "call microsoft support", "zeus virus",
        "critical security alert", "threats detected on your pc",
        "trojan detected", "security scan required",
    )
    PARKED_AND_PLACEHOLDER_MARKERS = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
        "kjøp dette domenet", "buy this domain", "domenet er parkert", "dette domenet er parkert",
        "domene er parkert", "parkert domene", "parkert hos", "domeneshop parkering",
        "proisp parkering", "webhuset parkering", "webhuset.no/parkering", "/parkering",
        "parked free, courtesy of", "parkingcrew", "bodis.com", "sedoparking", "sedo",
        "under construction", "site is under maintenance", "under konstruksjon",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    candidate_lower = candidate_text.lower()
    final_path = parsed_final.path.lower()

    detected_fraud = next((marker for marker in FRAUD_AND_SCAM_MARKERS if marker in normalized_raw or marker in candidate_lower), None)
    detected_parked = next((marker for marker in PARKED_AND_PLACEHOLDER_MARKERS if marker in normalized_raw or marker in candidate_lower), None)
    if not detected_parked:
        if final_path.startswith("/parkering") or final_path.startswith("/parked") or "parkering" in hostname_clean or hostname_clean in {"webhuset.no", "proisp.no", "domeneshop.no"}:
            detected_parked = "hosting_parking_path_or_host_detected"

    # Gate Requirement 1b: Fail closed if all tokens are generic industry/corporate words and no org number confirmed
    if not core_non_generic and not (org_digits and (org_in_footer or legal_org_prefixed)):
        score = 0.4 if ratio >= 0.5 else 0.2
        reasons.append("all legal name tokens are generic corporate or industry terms; requires exact organisation number to verify")
        return {
            "status": "review" if ratio >= 0.5 else "rejected",
            "score": score,
            "publishable": False,
            "is_fraud": False,
            "is_parked": bool(detected_parked),
            "legal_name_tokens": core,
            "matched_tokens": overlap,
            "reasons": reasons,
            "method": "deterministic_name_org_evidence_v2",
        }

    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    core_distinct_set = set(core_distinct)
    req_url = str(value.get("requested_url") or website.get("source_url") or "")
    req_host = (urllib.parse.urlparse(req_url).hostname or "").removeprefix("www.").casefold()
    req_host_compact = "".join(_tokens(req_host))
    core_compact = "".join(core)
    distinct_compact = "".join(core_distinct)
    title_tokens = _tokens(value.get("title"))
    exact_distinct_in_homepage = bool(core_distinct and any(core_distinct_set.issubset(tokens) for tokens in homepage_token_sets))
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))

    # Gate Requirement 2: Strict Scoring
    if detected_fraud:
        score = 0.0
        reasons.append(f"fraud_or_scam_signature_detected: {detected_fraud}")
    elif detected_parked:
        score = 0.1
        reasons.append(f"captured page is a parked, for-sale, or hosting placeholder: {detected_parked}")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif org_digits and (org_in_footer or legal_org_prefixed):
        score = 1.0
        reasons.append("exact organisation number appears in website footer/imprint or identity evidence")
    elif len(core) >= 2 and exact_homepage_name and (distinct_compact in hostname_compact or core_distinct_set.issubset(set(hostname_tokens))):
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in homepage identity evidence and domain")
    elif len(core_distinct) == 1 and core_distinct[0] in core_non_generic and (core_distinct[0] in hostname_tokens or hostname_compact == core_distinct[0] or hostname_compact.startswith(core_distinct[0])) and (core_distinct[0] in title_tokens or exact_homepage_name):
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence and domain")
    elif core_distinct and len(core_distinct) >= 2 and exact_distinct_in_homepage and (distinct_compact in hostname_compact or hostname_compact in distinct_compact or any(t in hostname_tokens for t in core_distinct)):
        score = 0.95
        reasons.append("distinctive corporate name tokens match homepage and domain evidence")
    elif core_distinct and len(core_distinct) >= 2 and (distinct_compact in hostname_compact or core_distinct_set.issubset(set(hostname_tokens))):
        score = 0.95
        reasons.append("distinctive corporate name tokens directly match domain hostname")
    elif core_compact and len(core_compact) >= 6 and (core_compact in hostname_compact or hostname_compact.startswith(core_compact)):
        score = 0.95
        reasons.append("normalized legal name core directly matches domain hostname")
    elif distinct_compact and len(distinct_compact) >= 4 and distinct_compact in hostname_compact and distinct_compact in normalized_candidate_text:
        score = 0.95
        reasons.append("distinctive brand name token directly matches domain hostname and page content")
    elif core_compact and len(core_compact) >= 5 and (core_compact in req_host_compact or req_host_compact.startswith(core_compact)) and not is_discovered and not detected_parked:
        score = 0.95
        reasons.append("registry-linked domain registered by entity matches corporate core")
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("website lacks strong exact-entity identity evidence")

    # Gate Requirement 3: Require Norway-specific corroboration for non-.no discovered domains
    if is_discovered and not is_no_tld and score >= 0.90:
        muni = (profile.get("business_address_municipality") or profile.get("municipality") or "").casefold().strip()
        has_norway_corroboration = False

        if org_digits and (org_digits in compact_candidate or legal_org_prefixed):
            has_norway_corroboration = True
        elif re.search(r"(?:\+47|0047)\s*[2-9][0-9]", candidate_text):
            has_norway_corroboration = True
        elif re.search(r"\b(?:org\.?\s*nr|organisasjonsnummer|foretaksregisteret|mva)\b", candidate_text, re.I):
            has_norway_corroboration = True
        elif muni and len(muni) >= 3 and muni in candidate_lower and ("norge" in candidate_lower or "norway" in candidate_lower or "postboks" in candidate_lower):
            has_norway_corroboration = True
        elif f"{profile.get('name', '').lower()}" in candidate_lower or (core_compact and (f"{core_compact} as" in candidate_lower or f"{core_compact} asa" in candidate_lower)):
            has_norway_corroboration = True
        elif "/nb/" in final_url or "/no/" in final_url or parsed_final.path.startswith("/nb") or parsed_final.path.startswith("/no"):
            has_norway_corroboration = True
        elif value.get("has_norway_in_html"):
            has_norway_corroboration = True

        if not has_norway_corroboration:
            score = 0.70
            reasons.append("discovered non-.no domain lacks Norway-specific entity corroboration (no org nr, +47, norwegian municipality, or mva)")

    if detected_fraud:
        status = "quarantined_fraud"
    elif detected_parked:
        status = "quarantined_parked"
    else:
        status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "is_fraud": bool(detected_fraud),
        "is_parked": bool(detected_parked),
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "reasons": reasons,
        "method": "deterministic_name_org_evidence_v2",
    }


def assess_social_identity(profile: dict[str, Any], link: dict[str, str]) -> dict[str, Any]:
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    handle_text = urllib.parse.unquote(parsed.path)
    handle_compact = "".join(_tokens(handle_text))
    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    ratio = len(set(matched)) / len(set(core)) if core else 0.0
    web_val = (profile.get("evidence", {}).get("website", {}) or {}).get("value") or {}
    web_assessment = web_val.get("identity_assessment") or {}
    web_domain = web_val.get("registered_domain") or urllib.parse.urlparse(web_val.get("final_url") or "").hostname or ""
    dom_tokens = _tokens(web_domain.removeprefix("www."))
    dom_compact = "".join(dom_tokens)

    # First-party declared link on an exact-verified company website is trusted authority
    is_verified_site_link = bool(web_assessment.get("publishable"))

    core_distinct = [t for t in core if t not in CORPORATE_MODIFIERS]
    core_non_generic = [t for t in core_distinct if t not in GENERIC_INDUSTRY_WORDS]
    distinct_compact = "".join(core_distinct)

    if core_compact and core_compact in handle_compact:
        score = 0.98
        reason = "normalized legal-name sequence appears in the social handle"
    elif len(core_distinct) >= 2 and len(distinct_compact) >= 4 and distinct_compact in handle_compact and core_non_generic:
        score = 0.95
        reason = "distinctive company brand sequence appears in the social handle"
    elif len(core_distinct) == 1 and core_distinct[0] in core_non_generic and (len(core_distinct[0]) >= 5 or any(ch in core_distinct[0] for ch in ("æ", "ø", "å"))) and core_distinct[0] in handle_compact:
        score = 0.95
        reason = "single distinctive brand token appears in the social handle"
    elif len(core_distinct) >= 2 and all(t in handle_compact for t in core_distinct) and core_non_generic:
        score = 0.95
        reason = "all distinctive brand tokens appear in the social handle"
    elif dom_compact and len(dom_compact) >= 3 and (dom_compact in handle_compact or any(t in handle_compact for t in dom_tokens if len(t) >= 3)):
        score = 0.95
        reason = "social handle matches verified company website domain"
    elif is_verified_site_link:
        score = 0.95
        reason = "social link declared on exact-verified company website"
    elif len(core) == 1 and matched:
        score = 0.95
        reason = "single distinctive legal-name token appears in the social handle"
    elif ratio >= 0.75 and len(set(matched)) >= 2:
        score = 0.9
        reason = "most legal-name tokens appear in the social handle"
    else:
        score = 0.3
        reason = "social handle lacks strong exact-entity name evidence"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v1",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(temporary_profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    if assessment.get("is_fraud"):
        website["status"] = "blocked"
        website["note"] = f"Fraudulent or malicious site quarantined: {assessment['reasons'][0]}"
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }
