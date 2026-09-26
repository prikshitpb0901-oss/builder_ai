from __future__ import annotations

from collections import defaultdict
from math import log, sqrt
from typing import Any
from urllib.parse import urlparse

from .external_footprint import EXPERIMENTAL_ACQUISITION_MODES, aggregate_footprint, publishable_observation


STRATEGIES = (
    "company_site_feed",
    "company_site_identity",
    "company_site_activity",
    "company_directory_identity",
    "verified_handle_extraction",
    "registry_workforce_snapshot",
    "about_contact_deep_pages",
    "youtube_channel_feed",
    "places_identity_resolution",
    "places_rating_reviews",
    "jobs_feed_discovery",
    "linkedin_workforce_snapshot",
    "annual_report_workforce_snapshot",
    "social_profile_metrics",
    "independent_news_discovery",
    "exact_entity_article_capture",
    "independent_sentiment",
    "buzz_peer_normalization",
)


def development_score(profile: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [item for item in observations if publishable_observation(item)]
    experimental = [
        item for item in observations
        if item.get("acquisition_mode") in EXPERIMENTAL_ACQUISITION_MODES
        and item.get("exact_entity") and item.get("identity_proof") and item.get("content_sha256")
    ]
    platforms = {str(item.get("platform")) for item in accepted}
    handle_platforms = {
        str(item.get("platform")) for item in accepted if item.get("signal_type") == "profile_handle"
    }
    signals = {str(item.get("signal_type")) for item in accepted}
    website = profile.get("evidence", {}).get("website", {})
    identity_ok = bool((website.get("value") or {}).get("identity_assessment", {}).get("publishable")) or any(
        (item.get("platform") == "wikidata" and item.get("signal_type") == "company_profile")
        or (item.get("platform") == "google_places" and item.get("signal_type") == "place_summary")
        for item in accepted
    )
    sentiment = aggregate_footprint(accepted).get("sentiment", {})
    experimental_sentiment = [item for item in experimental if item.get("sentiment_label")]
    experimental_sentiment_hosts = {
        (urlparse(str(item.get("source_url"))).hostname or "").casefold().removeprefix("www.")
        for item in experimental_sentiment
    }
    experimental_sentiment_reviewers = {
        str(item.get("reviewer_id")) for item in experimental_sentiment if item.get("reviewer_id")
    }
    experimental_review_summaries = [
        item for item in experimental
        if item.get("signal_type") == "review_summary"
        and int((item.get("metrics") or {}).get("review_count") or 0) >= 10
        and 0 < float((item.get("metrics") or {}).get("rating") or 0) <= 5
    ]
    experimental_sentiment_ready = bool(experimental_review_summaries) or len(experimental_sentiment) >= 10 and (
        len(experimental_sentiment_hosts) >= 2 or len(experimental_sentiment_reviewers) >= 10
    )
    pieces = {
        "exact_external_identity": 20.0 if identity_ok and accepted else 0.0,
        "verified_handles": min(15.0, len(handle_platforms) * 5.0),
        "profile_metrics": 10.0 if "profile_metrics" in signals else 0.0,
        "places_identity": 5.0 if "place_summary" in signals else 0.0,
        "places_reviews": 10.0 if signals & {"review", "review_summary"} else 0.0,
        "workforce_jobs": 10.0 if signals & {"job_posting", "workforce_snapshot"} else 0.0,
        "public_buzz": 10.0 if signals & {"public_post", "public_mention", "buzz_metrics"} else 0.0,
        "independent_sentiment": 15.0 if sentiment.get("status") == "available" else 0.0,
        "freshness_evidence": 5.0 if accepted and all(item.get("retrieved_at") and item.get("content_sha256") for item in accepted) else 0.0,
    }
    potential_signals = signals | {str(item.get("signal_type")) for item in experimental}
    potential_handle_platforms = handle_platforms | {
        str(item.get("platform")) for item in experimental if item.get("signal_type") == "profile_handle"
    }
    potential_identity_ok = identity_ok or any(
        (item.get("platform") == "google_places" and item.get("signal_type") == "place_summary")
        or (item.get("platform") == "company_directory" and item.get("signal_type") == "company_profile")
        for item in experimental
    )
    potential_pieces = {
        **pieces,
        "exact_external_identity": 20.0 if potential_identity_ok and (accepted or experimental) else 0.0,
        "verified_handles": min(15.0, len(potential_handle_platforms) * 5.0),
        "profile_metrics": 10.0 if "profile_metrics" in potential_signals else pieces["profile_metrics"],
        "places_identity": 5.0 if "place_summary" in potential_signals else pieces["places_identity"],
        "places_reviews": 10.0 if potential_signals & {"review", "review_summary"} else pieces["places_reviews"],
        "workforce_jobs": 10.0 if potential_signals & {"job_posting", "workforce_snapshot"} else pieces["workforce_jobs"],
        "public_buzz": 10.0 if potential_signals & {"public_post", "public_mention", "buzz_metrics"} else pieces["public_buzz"],
        "independent_sentiment": 15.0 if experimental_sentiment_ready else pieces["independent_sentiment"],
    }
    return {
        "score": round(sum(pieces.values()), 3),
        "components": pieces,
        "accepted_observations": len(accepted),
        "experimental_observations": len(experimental),
        "experimental_potential_score": round(sum(potential_pieces.values()), 3),
        "experimental_components": potential_pieces,
        "platforms": sorted(platforms),
        "signals": sorted(signals),
        "sentiment_status": sentiment.get("status", "abstain"),
        "experimental_sentiment_status": "available" if experimental_sentiment_ready else "abstain",
        "claim_boundary": "Development coverage score, not competition points; blind accuracy and rights gates still apply.",
    }


def strategy_order(prior_iterations: list[dict[str, Any]], strategies: tuple[str, ...] = STRATEGIES) -> list[str]:
    """Rank strategies by observed marginal reward plus a small exploration bonus."""
    gains: dict[str, list[float]] = defaultdict(list)
    for item in prior_iterations:
        gains[str(item["strategy"])].append(float(item.get("learning_gain") or item.get("score_delta") or 0))
    index = {name: position for position, name in enumerate(STRATEGIES)}
    total = max(1, len(prior_iterations))

    def priority(name: str) -> float:
        observed = gains[name]
        mean = sum(observed) / len(observed) if observed else 0.0
        exploration = sqrt(log(total + 1) / (len(observed) + 1))
        return mean + 0.35 * exploration

    return sorted(
        strategies,
        key=lambda name: (-priority(name), index[name]),
    )


def strategy_history(prior_iterations: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    rows = [item for item in prior_iterations if item.get("strategy") == strategy]
    rewards = [float(item.get("learning_gain") or item.get("score_delta") or 0) for item in rows]
    return {
        "prior_runs": len(rows),
        "prior_successes": sum(value > 0 for value in rewards),
        "prior_mean_learning_gain": round(sum(rewards) / len(rewards), 3) if rewards else 0.0,
    }


def run_company_control(
    profile: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    prior_iterations: list[dict[str, Any]] | None = None,
    target: float = 80.0,
    minimum_iterations: int = 10,
    maximum_iterations: int | None = None,
) -> dict[str, Any]:
    prior_iterations = prior_iterations or []
    iterations = []
    visible: list[dict[str, Any]] = []
    previous = development_score(profile, visible)
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in observations:
        by_strategy[str(item.get("strategy") or "buzz_peer_normalization")].append(item)

    remaining = list(STRATEGIES)
    adaptive_history = list(prior_iterations)
    iteration_budget = len(remaining) if maximum_iterations is None else min(maximum_iterations, len(remaining))
    for iteration in range(1, iteration_budget + 1):
        strategy = strategy_order(adaptive_history, tuple(remaining))[0]
        remaining.remove(strategy)
        history = strategy_history(adaptive_history, strategy)
        additions = by_strategy.get(strategy, [])
        visible.extend(additions)
        current = development_score(profile, visible)
        delta = round(current["score"] - previous["score"], 3)
        experimental_delta = round(current["experimental_potential_score"] - previous["experimental_potential_score"], 3)
        # Experimental evidence influences exploration order at a discount, but never
        # masquerades as publishable or awardable score.
        learning_gain = round(delta + max(0.0, experimental_delta - delta) * 0.25, 3)
        iterations.append(
            {
                "iteration": iteration,
                "strategy": strategy,
                "controller_action": "replicate" if history["prior_mean_learning_gain"] > 0 else "explore",
                **history,
                "new_observations": len(additions),
                "score_before": previous["score"],
                "score_after": current["score"],
                "score_delta": delta,
                "experimental_potential_delta": experimental_delta,
                "learning_gain": learning_gain,
                "experimental_potential_score": current["experimental_potential_score"],
                "sentiment_status": current["sentiment_status"],
                "experimental_sentiment_status": current["experimental_sentiment_status"],
                "result": (
                    "reinforce_publishable" if delta > 0
                    else "reinforce_experimental" if experimental_delta > 0
                    else "hold" if additions
                    else "blocked_no_input"
                ),
            }
        )
        adaptive_history.append(iterations[-1])
        previous = current
        # Evaluate every strategy exactly once. Early stopping made the final score
        # depend on strategy order: adding a weak connector could change which
        # already-collected evidence was visible when a company crossed the target.
        # The controller still learns and orders acquisition, but the scorecard must
        # judge the complete frozen evidence set consistently.
    return {
        "organisation_number": profile.get("organisation_number"),
        "company_name": profile.get("name"),
        "target": target,
        "iterations_run": len(iterations),
        "target_reached": previous["score"] >= target,
        "final": previous,
        "iterations": iterations,
    }
