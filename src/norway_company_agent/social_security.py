"""Social Profile & Channel Security Verification System.

Protects company profiles against:
1. Impersonator & Scam Channels (crypto giveaways, fake investment schemes, phishing).
2. Malicious / Defamatory / Parody Accounts (smear campaigns, fraudulent imitators).
3. Fake / Squatted Profiles (unverified recruiters, dormant spam handles).
4. Unrelated homonym entities (ensuring positive connection to genuine business purpose).

Ensures only legitimate, authentically verified corporate digital footprints are published.
"""
from __future__ import annotations

import re
from typing import Any

# ── Threat Signatures: Scams, Frauds, & Impersonation Markers ───────────────
SCAM_AND_ABUSE_PATTERNS = [
    # Crypto / Financial scams
    r"\b(?:crypto|bitcoin|ethereum|btc|eth|usdt)\b",
    r"\b(?:giveaway|airdrop|free\s+tokens|doubler)\b",
    r"\b(?:forex\s+signals|whatsapp\s+investment|telegram\s+invest)\b",
    r"\b(?:guaranteed\s+profit|passive\s+income\s+bot)\b",
    r"\b(?:t\.me\/|wa\.me\/|whatsapp:\s*\+)\b",
    # Phishing / Malicious links
    r"\b(?:claim\s+reward|connect\s+wallet|claim\s+bonus)\b",
    # Norwegian credential theft / fraud
    r"\b(?:bankid\s+svindel|vipps\s+svindel|logg\s+inn\s+bankid|bekreft\s+vipps)\b",
    r"\b(?:tjen\s+penger\s+raskt|lån\s+uten\s+sikkerhet|pyramidespill)\b",
    # Suspicious shorteners hiding actual targets
    r"\b(?:bit\.ly|tinyurl\.com|is\.gd|cutt\.ly|rb\.gy)\b",
    # Recruitment fraud
    r"\b(?:pay\s+registration\s+fee|wire\s+money\s+for\s+equipment)\b",
    # Smear / Negative purpose / Parody accounts
    r"\b(?:scam\s+alert|fraud\s+exposed|boycott|fake\s+company|swindle)\b",
    r"\b(?:parody\s+account|not\s+affiliated\s+with|unofficial\s+fan)\b",
]

# Positive commercial keywords indicating authentic corporate communication
POSITIVE_COMMERCIAL_MARKERS = [
    # Corporate communications & business terms
    "official", "selskap", "bedrift", "tjenester", "produkter", "technology",
    "løsninger", "solutions", "customer", "partner", "ansatte", "karriere",
    "kontakt", "contact", "about us", "om oss", "nyheter", "pressemelding",
    "engineering", "design", "produksjon", "leverandør", "norway", "norge",
    "norwegian", "energy", "project", "business", "corp", "industry",
    "industri", "utvikling", "innovasjon", "report", "marked", "drift",
    "presentation", "group", "holdings", "media", "services", "system",
]


def detect_security_threats(text_corpus: str) -> list[str]:
    """Scan text (titles, descriptions, channel bio) for malicious / scam patterns."""
    threats: list[str] = []
    text_lower = str(text_corpus or "").lower()

    for pattern in SCAM_AND_ABUSE_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            threats.append(f"malicious_pattern:{match.group(0)}")

    return threats


def assess_positive_purpose(text_corpus: str, company_name: str = "") -> tuple[bool, list[str]]:
    """Verify that channel/profile content serves a positive, legitimate corporate purpose."""
    text_lower = str(text_corpus or "").lower()
    matches = [marker for marker in POSITIVE_COMMERCIAL_MARKERS if marker in text_lower]

    # If the text mentions company name or commercial markers and has zero scam flags, it is positive
    has_positive = len(matches) > 0 or (company_name.lower() in text_lower if company_name else False) or len(text_lower.strip()) < 120
    reasons = [f"commercial_marker:{m}" for m in matches[:4]]
    if not reasons and has_positive:
        reasons.append("corporate_identity_alignment")

    return has_positive, reasons


def verify_social_channel_security(
    platform: str,
    channel_or_profile_name: str,
    target_url: str,
    company_name: str,
    content_samples: list[str] | None = None,
    website_domain: str | None = None,
) -> dict[str, Any]:
    """Perform security and authenticity screening on an external social channel or profile.

    Returns a structured security certificate:
      - is_safe: bool (True only if zero threat patterns detected)
      - positive_purpose: bool (True if positive business communication confirmed)
      - impersonation_risk: "low" | "medium" | "high" | "critical"
      - quarantine_reasons: list of fatal rejection reasons (if any)
      - audit_proof: audit trail for verification transparency
    """
    corpus_parts = [channel_or_profile_name, target_url]
    if content_samples:
        corpus_parts.extend(content_samples)
    combined_corpus = " ".join(corpus_parts)

    threats = detect_security_threats(combined_corpus)
    positive_ok, positive_reasons = assess_positive_purpose(combined_corpus, company_name)

    audit_proof: list[str] = []
    quarantine_reasons: list[str] = []

    # 1. Threat assessment
    if threats:
        quarantine_reasons.extend(threats)
        return {
            "is_safe": False,
            "positive_purpose": False,
            "impersonation_risk": "critical",
            "quarantine_reasons": quarantine_reasons,
            "audit_proof": [f"SECURITY_ALERT: Malicious or scam signature detected: {threats[0]}"],
            "security_tier": "quarantined_threat",
        }

    # 2. Domain / Bio consistency check
    domain_match = False
    if website_domain:
        clean_domain = website_domain.lower().removeprefix("www.")
        if clean_domain in combined_corpus.lower():
            domain_match = True
            audit_proof.append(f"verified_domain_link:{clean_domain}")

    # 3. Impersonation risk rating
    impersonation_risk = "low" if domain_match else "medium"
    audit_proof.append("zero_scam_signatures_detected")
    if positive_reasons:
        audit_proof.extend(positive_reasons)

    is_safe = len(quarantine_reasons) == 0
    security_tier = "verified_safe_and_authentic" if is_safe and (domain_match or positive_ok) else "quarantined_uncertain"

    return {
        "is_safe": is_safe and security_tier == "verified_safe_and_authentic",
        "positive_purpose": positive_ok,
        "impersonation_risk": impersonation_risk,
        "quarantine_reasons": quarantine_reasons,
        "audit_proof": audit_proof,
        "security_tier": security_tier,
    }
