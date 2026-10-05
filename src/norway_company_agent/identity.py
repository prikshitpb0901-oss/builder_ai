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
    core = _tokens(profile.get("name"))
    hostname = urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").hostname or ""
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
        "domeneshop parkering", "proisp parkering", "webhuset parkering",
        "parked free, courtesy of", "parkingcrew", "bodis.com", "sedoparking",
        "under construction", "site is under maintenance",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    candidate_lower = candidate_text.lower()
    detected_fraud = next((marker for marker in FRAUD_AND_SCAM_MARKERS if marker in normalized_raw or marker in candidate_lower), None)
    detected_parked = next((marker for marker in PARKED_AND_PLACEHOLDER_MARKERS if marker in normalized_raw or marker in candidate_lower), None)
    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    core_distinct = [t for t in core if t not in CORPORATE_MODIFIERS]
    core_distinct_set = set(core_distinct)
    hostname_clean = hostname.removeprefix("www.").casefold()
    hostname_tokens = _tokens(hostname_clean)
    hostname_compact = "".join(hostname_tokens)
    req_url = str(value.get("requested_url") or website.get("source_url") or "")
    req_host = (urllib.parse.urlparse(req_url).hostname or "").removeprefix("www.").casefold()
    req_host_compact = "".join(_tokens(req_host))
    core_compact = "".join(core)
    distinct_compact = "".join(core_distinct)
    title_tokens = _tokens(value.get("title"))
    exact_distinct_in_homepage = bool(core_distinct and any(core_distinct_set.issubset(tokens) for tokens in homepage_token_sets))
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 100
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))
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
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in homepage identity evidence")
    elif len(core) == 1 and exact_homepage_name and (substantive_homepage or core[0] in hostname_tokens or core[0] in hostname_compact):
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence and domain")
    elif core_distinct and len(core_distinct) >= 2 and exact_distinct_in_homepage and (distinct_compact in hostname_compact or hostname_compact in distinct_compact or any(t in hostname_tokens for t in core_distinct)):
        score = 0.95
        reasons.append("distinctive corporate name tokens match homepage and domain evidence")
    elif core_distinct and len(core_distinct) >= 2 and (distinct_compact in hostname_compact or core_distinct_set.issubset(set(hostname_tokens))):
        score = 0.95
        reasons.append("distinctive corporate name tokens directly match domain hostname")
    elif len(core_distinct) == 1 and len(core_distinct[0]) >= 4 and (core_distinct[0] in hostname_tokens or core_distinct[0] in hostname_compact) and (core_distinct[0] in title_tokens):
        score = 0.95
        reasons.append("distinctive corporate brand token matches domain and homepage title")
    elif core_compact and len(core_compact) >= 6 and (core_compact in hostname_compact or hostname_compact.startswith(core_compact)):
        score = 0.95
        reasons.append("normalized legal name core directly matches domain hostname")
    elif core_compact and len(core_compact) >= 5 and (core_compact in req_host_compact or req_host_compact.startswith(core_compact)):
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
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")
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

    if core_compact and core_compact in handle_compact:
        score = 0.98
        reason = "normalized legal-name sequence appears in the social handle"
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
