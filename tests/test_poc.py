from __future__ import annotations

import json
import gzip
import csv
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.crawl_events import extract_page_event, merge_profile_events, missing_seed_error_events  # noqa: E402
from norway_company_agent.discovery import build_company_search_query, choose_search_candidate, parse_brave_web_results, score_search_candidate  # noqa: E402
from norway_company_agent.official import _reserve_history_slot, accounting_obligation_assessment, normalize_entity, normalize_financial_history, normalize_financials, normalize_roles  # noqa: E402
from norway_company_agent.operations import domain_request_summary, latency_summary, percentile  # noqa: E402
from norway_company_agent.sampling import deterministic_extension_sample, deterministic_financial_filer_sample, deterministic_website_audit_sample, financial_filer_eligible, normalize_row, stratum  # noqa: E402
from norway_company_agent.research import answer_profile, parse_screen_query, screen_profiles  # noqa: E402
from norway_company_agent.workspace import load_workspace, record_screen, save_workspace  # noqa: E402
from norway_company_agent.refresh import diff_datasets, diff_profile  # noqa: E402
from norway_company_agent.sentiment import aggregate_company_sentiment, evaluate_predictions, publishable_sentiment_item, sentiment_input_eligibility  # noqa: E402
from norway_company_agent.external_footprint import aggregate_footprint, publishable_observation, validate_observation  # noqa: E402
from norway_company_agent.external_tasks import plan_external_tasks  # noqa: E402
from norway_company_agent.external_control import development_score, run_company_control, strategy_order  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate, assess_social_identity, assess_website_identity  # noqa: E402
from norway_company_agent.website import _extraction_state, _priority_links, _social_links, assert_public_url, normalize_homepage, normalize_social_url, structured_social_links  # noqa: E402
from norway_company_agent.batch import evidence_terminal_state, profile_complete_for_modules, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.snapshots import SnapshotFetcher  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from scripts.build_prototype import compact as compact_prototype, qualification_copy  # noqa: E402
from scripts.run_brave_discovery import brave_search  # noqa: E402
from scripts.run_annual_report_workforce_connector import extract_candidate, needs_ocr  # noqa: E402
from scripts.normalize_google_maps_results import candidate_score  # noqa: E402
from scripts.run_scrapy_websites import terminal_events_for_run  # noqa: E402
from scripts.run_sentiment_model import MODEL_REVISION, normalize_generated_label  # noqa: E402
from scripts.score_company_completeness import score_rows, summarize  # noqa: E402
from scripts.extract_company_site_activity import observation as site_activity_observation  # noqa: E402
from scripts.extract_company_site_news import observation as site_news_observation  # noqa: E402
from scripts.build_verified_observations import build as build_verified_observations  # noqa: E402
from scripts.run_google_news_rss_connector import exact_title_match  # noqa: E402
from scripts.run_linkedin_guest_jobs_connector import canonical_company_url, parse_detail_company_urls, parse_job_cards, parse_typeahead  # noqa: E402
from scripts.run_linkedin_guest_experiment import (  # noqa: E402
    assess_profile_identity as assess_linkedin_profile_identity,
    extract_profile as extract_linkedin_profile,
    legal_name_profile_url,
)
from scripts.run_fagfolkguiden_reviews_connector import extract_aggregate_rating, slug  # noqa: E402
from scripts.discover_linkedin_company_profiles import (  # noqa: E402
    discovery_identity as linkedin_discovery_identity,
    normalized_full_name as linkedin_normalized_full_name,
    official_site_aliases as linkedin_official_site_aliases,
    parse_exact_typeahead as parse_linkedin_exact_typeahead,
)


class EvidenceTests(unittest.TestCase):
    def test_missing_is_not_zero_and_provenance_is_required(self):
        record = evidence("financials", "not_found", "official_annual_accounts", "https://example.test/123")
        self.assertIsNone(record["value"])
        self.assertNotEqual(record["value"], 0)
        self.assertTrue(record["source_url"])
        self.assertTrue(record["retrieved_at"])

    def test_available_zero_is_preserved(self):
        record = evidence("employees", "available", "official_registry_bulk", "https://example.test", value=0)
        self.assertEqual(record["value"], 0)
        self.assertEqual(record["status"], "available")

    def test_content_hash_can_be_carried_with_evidence(self):
        record = evidence("entity", "available", "official", "https://example.test", value={}, content_sha256="a" * 64, source_row_key="999999999")
        self.assertEqual(record["content_sha256"], "a" * 64)
        self.assertEqual(record["source_class"], "official")
        self.assertEqual(record["source_row_key"], "999999999")

    def test_not_fetched_is_distinct_from_not_applicable(self):
        record = evidence("history", "not_fetched", "official", "https://example.test", note="No filing flag in snapshot")
        self.assertEqual(record["status"], "not_fetched")
        self.assertNotEqual(record["status"], "not_applicable")


class ExternalFootprintTests(unittest.TestCase):
    def observation(self, **changes):
        base = {
            "id": "obs-1",
            "organisation_number": "923609016",
            "platform": "google_places",
            "signal_type": "review",
            "source_url": "https://maps.google.com/example",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "address_match", "value": "Oslo"}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "customer_review",
            "evidence_span": "Helpful staff",
        }
        return {**base, **changes}

    def test_publication_requires_rights_identity_hash_and_span(self):
        self.assertTrue(publishable_observation(self.observation()))
        bad = self.observation(exact_entity=False, content_sha256=None, evidence_span=None, rights_status="unknown")
        reasons = validate_observation(bad)
        self.assertIn("exact legal entity is not verified", reasons)
        self.assertIn("missing content hash", reasons)
        self.assertIn("missing evidence span", reasons)
        self.assertIn("source rights are not approved", reasons)

    def test_unofficial_scraper_output_is_experimental_not_publishable(self):
        item = self.observation(platform="linkedin", signal_type="job_posting", acquisition_mode="jobspy_experiment")
        self.assertFalse(publishable_observation(item))

    def test_linkedin_guest_jobs_require_exact_verified_company_url(self):
        raw = b'''<div class="base-search-card" data-entity-urn="urn:li:jobPosting:4456746433">
          <a class="base-card__full-link" href="https://no.linkedin.com/jobs/view/example-4456746433?x=1"></a>
          <span class="sr-only">Project manager</span>
          <h4 class="base-search-card__subtitle"><a href="https://no.linkedin.com/company/af-gruppen?trk=x">AF Gruppen</a></h4>
          <span class="job-search-card__location">Oslo</span><time datetime="2026-08-23"></time>
        </div>
        <div class="base-search-card" data-entity-urn="urn:li:jobPosting:4456746434">
          <a class="base-card__full-link" href="https://linkedin.com/jobs/view/other-4456746434"></a>
          <span class="sr-only">Wrong parent job</span>
          <h4 class="base-search-card__subtitle"><a href="https://linkedin.com/company/af-gruppen-sverige">AF Gruppen Sverige</a></h4>
        </div>'''
        jobs, candidates = parse_job_cards(raw, "https://linkedin.com/company/af-gruppen")
        self.assertEqual(candidates, 2)
        self.assertEqual([item["job_id"] for item in jobs], ["4456746433"])
        self.assertEqual(jobs[0]["company_url"], "https://linkedin.com/company/af-gruppen")

    def test_linkedin_company_urls_and_typeahead_are_normalized_without_claiming_ambiguous_ids(self):
        self.assertEqual(
            canonical_company_url("https://no.linkedin.com/company/Norsk-Fiskeeksport/about?trk=x"),
            "https://linkedin.com/company/norsk-fiskeeksport",
        )
        candidates = parse_typeahead(
            json.dumps([
                {"id": "34440", "type": "COMPANY", "displayName": "AF Gruppen"},
                {"id": "1188022", "type": "COMPANY", "displayName": "AF Gruppen Sverige"},
            ]).encode(),
            "AF GRUPPEN ASA",
        )
        self.assertTrue(candidates[0]["exact_legal_name_core"])
        self.assertFalse(candidates[1]["exact_legal_name_core"])
        self.assertEqual(
            parse_detail_company_urls(
                b'<a href="https://no.linkedin.com/company/af-gruppen?trk=job">AF Gruppen</a>'
                b'<a href="https://example.test/company/wrong">Wrong</a>'
            ),
            {"https://linkedin.com/company/af-gruppen"},
        )

    def test_linkedin_guest_profile_uses_structured_company_data_and_ignores_dormant_challenge_code(self):
        graph = {
            "@graph": [
                {
                    "@type": "DiscussionForumPosting",
                    "author": {"url": "https://no.linkedin.com/company/af-gruppen"},
                    "datePublished": "2026-08-21T06:15:05Z",
                    "text": "Exact company update",
                    "url": "https://no.linkedin.com/posts/example-activity-7496449781678927873-x",
                },
                {
                    "@type": "Organization",
                    "name": "AF Gruppen",
                    "url": "https://no.linkedin.com/company/af-gruppen",
                    "description": "Construction group",
                    "numberOfEmployees": {"value": 1303},
                },
            ]
        }
        raw = (
            '<meta name="description" content="AF Gruppen | 56 726 followers on LinkedIn">'
            f'<script type="application/ld+json">{json.dumps(graph)}</script>'
            '<script>const dormant="recaptcha/challengepage";</script>'
            '<div data-test-id="about-us__size"><dd>5,001-10,000 employees</dd></div>'
            '<article class="main-feed-activity-card" data-activity-urn="urn:li:activity:7496449781678927873">'
            '<a data-test-id="social-actions__reactions" data-num-reactions="29"></a>'
            '<a data-test-id="social-actions__comments" data-num-comments="4"></a></article>'
        ).encode()
        profile = extract_linkedin_profile(raw, "https://linkedin.com/company/af-gruppen")
        self.assertEqual(profile["followers"], 56726)
        self.assertEqual(profile["visible_employees"], 1303)
        self.assertEqual(profile["employee_size_label"], "5,001-10,000 employees")
        self.assertEqual(profile["posts"][0]["likes"], 29)
        self.assertEqual(profile["posts"][0]["comments"], 4)

    def test_linkedin_guest_profile_rejects_authwall_without_organization_data(self):
        with self.assertRaisesRegex(RuntimeError, "no structured organization"):
            extract_linkedin_profile(b'<script>recaptcha/challengepage</script>', "https://linkedin.com/company/example")

    def test_linkedin_stale_handle_fallback_is_bounded_to_registry_legal_name(self):
        self.assertEqual(legal_name_profile_url("DIPS AS"), "https://www.linkedin.com/company/dips-as")
        self.assertEqual(legal_name_profile_url("RØD & BLÅ AS"), "https://www.linkedin.com/company/rod-bla-as")

    def test_linkedin_discovery_requires_exact_typeahead_name_and_corroboration(self):
        raw = json.dumps([
            {"id": "1", "type": "COMPANY", "displayName": "DIPS AS"},
            {"id": "2", "type": "COMPANY", "displayName": "DIPS ASA"},
        ]).encode()
        self.assertEqual([item["linkedin_company_id"] for item in parse_linkedin_exact_typeahead(raw, "DIPS AS")], ["1"])
        self.assertEqual(linkedin_normalized_full_name("RØD & BLÅ AS"), "rød blå as")
        company = {
            "name": "DIPS AS",
            "municipality": "BODØ",
            "website": "https://dips.com",
            "evidence": {"website": {"status": "available", "value": {"final_url": "https://dips.com"}}},
        }
        exact = linkedin_discovery_identity(company, {"name": "DIPS AS", "website": "https://www.dips.com", "headquarters": "Bodø"}, {"legal_name_slug"})
        self.assertTrue(exact["exact_entity"])
        weak = linkedin_discovery_identity(company, {"name": "DIPS AS", "website": "https://unrelated.test", "headquarters": "Oslo"}, {"legal_name_slug"})
        self.assertFalse(weak["exact_entity"])

    def test_linkedin_fuzzy_discovery_uses_verified_site_alias_and_reverse_domain(self):
        company = {
            "name": "JARRE AS",
            "municipality": "INDRE ØSTFOLD",
            "website": "https://jarre.co",
            "evidence": {
                "website": {"status": "available", "value": {"final_url": "https://jarre.co", "title": "Jarre&Co"}},
                "roles": {"value": {"roles": [{"name": "Christian Jarre", "role_code": "DAGL"}]}},
            },
        }
        self.assertEqual(linkedin_official_site_aliases(company), ["Jarre&Co"])
        exact = linkedin_discovery_identity(
            company,
            {"name": "Jarre & Co", "website": "https://www.jarre.co", "headquarters": "Askim", "description": ""},
            {"official_site_alias:Jarre&Co"},
        )
        self.assertTrue(exact["exact_entity"])

    def test_linkedin_profile_identity_accepts_redirect_alias_only_with_name_or_reverse_domain_proof(self):
        profile = {
            "name": "ZAPTEC ASA",
            "website": "https://zaptec.com",
            "evidence": {"website": {"source_url": "https://www.zaptec.com/", "value": {"final_url": "https://www.zaptec.com/"}}},
        }
        accepted = assess_linkedin_profile_identity(
            profile,
            "https://linkedin.com/company/gozaptec",
            {"name": "Zaptec", "page_url": "https://linkedin.com/company/zaptec", "website": "https://www.zaptec.com"},
        )
        self.assertTrue(accepted["publishable_candidate"])
        rejected = assess_linkedin_profile_identity(
            profile,
            "https://linkedin.com/company/gozaptec",
            {"name": "Unrelated Parent", "page_url": "https://linkedin.com/company/unrelated", "website": "https://parent.test"},
        )
        self.assertFalse(rejected["publishable_candidate"])

    def test_google_play_observation_is_supported_but_unofficial_output_stays_experimental(self):
        item = self.observation(
            platform="google_play",
            signal_type="review_summary",
            acquisition_mode="unofficial_api_experiment",
            rights_status="review_required",
        )
        reasons = validate_observation(item)
        self.assertNotIn("unsupported platform", reasons)
        self.assertFalse(publishable_observation(item))

    def test_company_directory_is_not_a_website_discovery_candidate(self):
        profile = {"name": "OBLOMOV AS", "organisation_number": "991167315", "municipality": "SOLA"}
        result = {"url": "https://www.northdata.com/Oblomov-AS/BR-991167315", "title": "Oblomov AS", "snippet": "991167315", "rank": 1}
        assessment = score_search_candidate(profile, result)
        self.assertFalse(assessment["publishable_candidate"])
        self.assertEqual(assessment["status"], "rejected")

    def test_unknown_company_directory_with_org_number_is_not_a_candidate(self):
        profile = {"name": "AKSLA AS", "organisation_number": "923304290", "municipality": "ÅLESUND"}
        result = {"url": "https://vexter.no/selskap/aksla-as/923304290", "title": "AKSLA AS", "snippet": "923304290", "rank": 1}
        self.assertFalse(score_search_candidate(profile, result)["publishable_candidate"])

    def test_annual_workforce_parser_does_not_treat_norwegian_o_as_zero(self):
        heading = "Note 2 - Lonnskostnader, antall ansatte og lan til ansatte"
        self.assertEqual(extract_candidate(heading), (None, None, "no_employee_phrase", None))
        count, span, status, measure = extract_candidate("Det er to ansatte i sameiet.")
        self.assertEqual((count, status, measure), (2, "accepted", "employees"))
        self.assertEqual(span, "Det er to ansatte i sameiet.")
        self.assertEqual(extract_candidate("Selskapet har 1 2025 sysselsatt 2 arsverk.")[0], 2)
        self.assertEqual(extract_candidate("Antall arsverk syssetsatt i regnskapsaret: 3")[0], 3)
        self.assertEqual(extract_candidate("Stiftelsen har ingen ansatte og ingen arsverk.")[0], 0)
        self.assertEqual(extract_candidate("Selskapet hadde ingen ansatte i 2025.")[0], 0)
        self.assertEqual(extract_candidate("Gjennomsnittlig antall ansatte i regnskapsaret: 0")[0], 0)
        self.assertEqual(extract_candidate("Note Antall Aarsverk i regnskapsaret 0.00")[0], 0)
        self.assertEqual(extract_candidate("Tal pa Aarsverk i rekneskapsaret 1.50")[0], 1.5)
        self.assertTrue(needs_ocr("Digital cover text without the employee note"))
        self.assertFalse(needs_ocr("Selskapet har 2 ansatte. " + "Digital report text. " * 8))

    def test_aggregate_keeps_source_metrics_separate_and_abstains_on_thin_sentiment(self):
        items = [
            self.observation(id="a", sentiment_label="positive", sentiment_model_version="m1"),
            self.observation(id="b", platform="youtube", signal_type="profile_metrics", source_url="https://youtube.com/@example", evidence_span=None),
        ]
        result = aggregate_footprint(items, as_of="2026-08-22T00:00:00Z")
        self.assertEqual(result["accepted_observations"], 2)
        self.assertEqual(result["sentiment"]["status"], "abstain")
        self.assertNotIn("popularity_score", result)

    def test_customer_review_sentiment_accepts_ten_independent_reviewers_on_one_platform(self):
        items = [
            self.observation(
                id=f"review-{index}",
                sentiment_label="positive",
                sentiment_model_version="explicit_star_rating_v1",
                reviewer_id=f"reviewer-{index}",
            )
            for index in range(10)
        ]
        result = aggregate_footprint(items, as_of="2026-08-22T00:00:00Z")
        self.assertEqual(result["sentiment"]["status"], "available")
        self.assertEqual(result["sentiment"]["independent_reviewers"], 10)

    def test_google_maps_identity_gate_rejects_neighbor_and_accepts_exact_address(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {
                "registry": {"value": {
                    "forretningsadresse.adresse": "Standardveien 1",
                    "forretningsadresse.postnummer": "0581",
                    "telefon": "22 89 11 00",
                }},
                "website": {"value": {
                    "final_url": "https://afgruppen.no/",
                    "identity_assessment": {"publishable": True},
                }},
            },
        }
        exact = candidate_score(profile, {
            "title": "AF Gruppen", "address": "Standardveien 1, 0581 Oslo, Norge",
            "phone": "+47 22 89 11 00", "web_site": "https://afgruppen.no/", "review_count": 21,
        })
        neighbor = candidate_score(profile, {
            "title": "AF Eiendom", "address": "Standardveien 1, 0581 Oslo, Norge",
            "phone": "+47 22 89 11 00", "web_site": "https://afgruppen.no/eiendom/", "review_count": 0,
        })
        self.assertTrue(exact["accepted"])
        self.assertFalse(neighbor["accepted"])

    def test_google_maps_exact_name_and_postcode_city_can_resolve_operating_address(self):
        profile = {
            "organisation_number": "999999999",
            "name": "EXAMPLE INDUSTRI AS",
            "evidence": {"registry": {"value": {
                "forretningsadresse.adresse": "c/o Accountant Other Street 1",
                "forretningsadresse.postnummer": "4021",
                "forretningsadresse.poststed": "STAVANGER",
            }}},
        }
        result = candidate_score(profile, {
            "title": "Example Industri AS", "address": "Factory Road 7, 4021 Stavanger, Norway",
            "phone": "", "web_site": "", "review_count": 4,
        })
        self.assertTrue(result["accepted"])
        self.assertTrue(result["postcode_city_match"])

    def test_google_maps_trade_name_requires_exact_address_phone_and_no_partial_name_collision(self):
        profile = {
            "name": "OSLOFJORDEN EIENDOMSMEGLING AS",
            "evidence": {"registry": {"value": {
                "forretningsadresse.adresse": "Stranden 81", "forretningsadresse.postnummer": "0250",
                "forretningsadresse.poststed": "Oslo", "telefon": "22620000",
            }}},
        }
        candidate = {"title": "PrivatMegleren Premium", "address": "Stranden 81, 0250 Oslo", "phone": "+47 22 62 00 00"}
        result = candidate_score(profile, candidate)
        self.assertFalse(result["trade_name_match"])
        self.assertFalse(result["accepted"])
        profile["organisation_number"] = "932083108"
        result = candidate_score(profile, candidate)
        self.assertTrue(result["trade_name_match"])
        self.assertTrue(result["accepted"])
        candidate["phone"] = "+47 99 99 99 99"
        self.assertFalse(candidate_score(profile, candidate)["accepted"])

    def test_experimental_maps_signals_raise_only_experimental_places_score(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        common = {
            "organisation_number": "938702675",
            "platform": "google_places",
            "source_url": "https://www.google.com/maps/place/example",
            "retrieved_at": "2026-08-22T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "registry_address_match", "value": True}],
            "acquisition_mode": "unofficial_api_experiment",
            "rights_status": "review_required",
            "source_class": "public_business_listing",
            "evidence_span": "AF Gruppen; Standardveien 1; rating=2.5; reviews=21",
        }
        observations = [
            {**common, "id": "place", "signal_type": "place_summary", "strategy": "places_identity_resolution"},
            {**common, "id": "summary", "signal_type": "review_summary", "strategy": "places_rating_reviews"},
        ]
        score = development_score(profile, observations)
        self.assertEqual(score["score"], 0.0)
        self.assertEqual(score["experimental_potential_score"], 35.0)

    def test_aggregate_maps_rating_is_experimental_sentiment_and_buzz(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        common = {
            "organisation_number": "938702675",
            "platform": "google_places",
            "source_url": "https://www.google.com/maps/place/example",
            "retrieved_at": "2026-08-22T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "registry_address_match", "value": True}],
            "acquisition_mode": "unofficial_api_experiment",
            "rights_status": "review_required",
            "evidence_span": "AF Gruppen; rating=4.4; reviews=21",
            "metrics": {"rating": 4.4, "rating_scale": 5, "review_count": 21},
        }
        observations = [
            {**common, "id": "summary", "signal_type": "review_summary", "strategy": "places_rating_reviews"},
            {**common, "id": "buzz", "signal_type": "buzz_metrics", "strategy": "buzz_peer_normalization"},
        ]
        score = development_score(profile, observations)
        self.assertEqual(score["score"], 0.0)
        self.assertEqual(score["experimental_sentiment_status"], "available")
        self.assertEqual(score["experimental_potential_score"], 35.0)

    def test_task_planner_uses_verified_handles_and_adds_core_connectors(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {
                "website": {"value": {"social_links": [{"platform": "youtube", "url": "https://youtube.com/@example"}]}},
            },
        }
        tasks = plan_external_tasks(profile)
        connectors = {item["connector"] for item in tasks}
        self.assertIn("google_places_api", connectors)
        self.assertIn("jobs_provider", connectors)
        self.assertIn("youtube_connector", connectors)
        self.assertNotIn("permitted_search_api", connectors)

    def test_controller_recomputes_sentiment_and_records_marginal_gain(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}},
        }
        handle = self.observation(signal_type="profile_handle", source_class="company_social", evidence_span=None, strategy="verified_handle_extraction")
        score = development_score(profile, [handle])
        self.assertGreater(score["score"], 0)
        self.assertEqual(score["sentiment_status"], "abstain")
        result = run_company_control(profile, [handle], minimum_iterations=10, maximum_iterations=15)
        self.assertGreaterEqual(result["iterations_run"], 10)
        self.assertTrue(any(item["score_delta"] > 0 for item in result["iterations"]))
        self.assertTrue(all("sentiment_status" in item for item in result["iterations"]))

    def test_controller_final_score_is_not_path_dependent_after_target_is_reached(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}},
        }
        observations = [
            self.observation(signal_type="profile_handle", source_class="company_social", evidence_span=None, strategy="verified_handle_extraction"),
            self.observation(id="metric", platform="youtube", signal_type="profile_metrics", source_url="https://youtube.com/@example", evidence_span=None, strategy="social_profile_metrics"),
        ]
        result = run_company_control(profile, observations, target=20, minimum_iterations=1)
        self.assertEqual(result["iterations_run"], len(strategy_order([])))
        self.assertEqual(result["final"], development_score(profile, observations))

    def test_exact_wikidata_org_profile_can_supply_external_identity(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        wikidata = self.observation(
            platform="wikidata",
            signal_type="company_profile",
            source_url="https://www.wikidata.org/wiki/Q123",
            evidence_span="Q123: P2333=923609016",
            source_class="open_knowledge_graph",
            strategy="company_site_identity",
        )
        score = development_score(profile, [wikidata])
        self.assertEqual(score["components"]["exact_external_identity"], 20.0)
        self.assertTrue(publishable_observation(wikidata))

    def test_controller_replicates_prior_winning_strategy_first(self):
        prior = [
            {"strategy": "youtube_channel_feed", "learning_gain": 8.0},
            {"strategy": "verified_handle_extraction", "learning_gain": 2.0},
        ]
        self.assertEqual(strategy_order(prior)[0], "youtube_channel_feed")
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}}}
        result = run_company_control(profile, [], prior_iterations=prior, minimum_iterations=1, maximum_iterations=2)
        self.assertEqual(result["iterations"][0]["strategy"], "youtube_channel_feed")
        self.assertEqual(result["iterations"][0]["controller_action"], "replicate")


class CompletenessScoreTests(unittest.TestCase):
    def test_all_source_weights_sum_to_one_hundred(self):
        from scripts.score_company_completeness import ENRICHMENT_WEIGHTS, FOUNDATION_WEIGHTS

        self.assertEqual(sum(FOUNDATION_WEIGHTS.values()) + sum(ENRICHMENT_WEIGHTS.values()), 100.0)

    def test_all_source_score_combines_foundation_and_external_without_imputing_missing(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry_live": {"status": "available", "value": {"organisation_number": "923609016"}},
                "financials": {"status": "available"},
                "roles": {"status": "available"},
                "locations": {"status": "available"},
                "website": {"status": "not_found"},
            },
        }
        components = {
            "exact_external_identity": 20,
            "verified_handles": 15,
            "profile_metrics": 10,
            "places_identity": 5,
            "places_reviews": 10,
            "workforce_jobs": 10,
            "public_buzz": 10,
            "independent_sentiment": 0,
            "freshness_evidence": 5,
        }
        result = {"organisation_number": "923609016", "company_name": "Example AS", "final": {"components": components, "experimental_components": components}}
        scored = score_rows([profile], [result])
        self.assertEqual(scored[0]["foundation_score"], 30.0)
        self.assertEqual(scored[0]["strict_enrichment"]["independent_sentiment"], 0.0)
        self.assertEqual(scored[0]["strict_completeness_score"], 88.0)
        self.assertEqual(summarize(scored)["companies"], 1)

    def test_site_activity_requires_exact_identity_and_preserves_snapshot_provenance(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {"website": {
                "status": "available",
                "source_url": "https://example.test/",
                "retrieved_at": "2026-08-23T00:00:00Z",
                "value": {
                    "final_url": "https://example.test/",
                    "content_sha256": "a" * 64,
                    "identity_assessment": {"publishable": True, "status": "exact", "score": 1.0},
                    "pages": [{"url": "https://example.test/"}],
                },
            }},
        }
        item = site_activity_observation(profile)
        self.assertIsNotNone(item)
        self.assertEqual(item["strategy"], "company_site_activity")
        self.assertTrue(publishable_observation(item))
        profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"] = False
        self.assertIsNone(site_activity_observation(profile))

    def test_news_title_gate_requires_the_full_legal_name_core(self):
        self.assertTrue(exact_title_match("NORDIC DOOR AS", "Nordic Door AS åpner ny fabrikk - Lokalavisa"))
        self.assertFalse(exact_title_match("NORDIC DOOR AS", "Nordic investors prefer another door - Example"))
        self.assertTrue(exact_title_match("SOLVANG ASA", "Sterkt årsresultat fra Solvang ASA i 2024 - Skipsrevyen"))
        self.assertFalse(exact_title_match("VIND HOLDING AS", "Inntektene til Aneo Roan Vind Holding AS stupte - mn24.no"))
        self.assertFalse(exact_title_match("CONSTO AS", "Drastisk fall hos Consto Bergen AS - BT"))

    def test_site_news_requires_exact_identity_and_a_captured_news_path(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {"website": {
                "status": "available", "retrieved_at": "2026-08-23T00:00:00Z",
                "value": {
                    "identity_assessment": {"publishable": True, "score": 1.0},
                    "pages": [{"url": "https://example.test/aktuelt/new-contract", "title": "New contract", "content_sha256": "a" * 64}],
                },
            }},
        }
        item = site_news_observation(profile)
        self.assertEqual(item["signal_type"], "public_post")
        self.assertTrue(publishable_observation(item))
        profile["evidence"]["website"]["value"]["pages"][0]["url"] = "https://example.test/contact"
        self.assertIsNone(site_news_observation(profile))

    def test_verified_observations_require_known_org_and_snapshot_hash(self):
        profiles = [{"organisation_number": "923609016", "name": "Example AS"}]
        seed = {"organisation_number": "923609016", "platform": "news", "signal_type": "public_mention", "source_url": "https://example.test/news", "content_sha256": "a" * 64, "evidence_span": "Example AS", "proof": "Exact legal name"}
        self.assertTrue(build_verified_observations([seed], profiles)[0]["exact_entity"])
        seed["content_sha256"] = "bad"
        with self.assertRaises(ValueError):
            build_verified_observations([seed], profiles)

    def test_directory_identity_is_experimental_and_never_becomes_strict(self):
        item = {
            "id": "directory-1", "organisation_number": "923609016", "platform": "company_directory",
            "signal_type": "company_profile", "source_url": "https://example.test/923609016",
            "retrieved_at": "2026-08-23T00:00:00Z", "content_sha256": "a" * 64,
            "exact_entity": True, "identity_proof": [{"type": "organisation_number", "value": "923609016"}],
            "acquisition_mode": "rights_review_experiment", "rights_status": "review_required",
            "source_class": "public_company_directory", "strategy": "company_directory_identity",
        }
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {}}
        score = development_score(profile, [item])
        self.assertEqual(score["components"]["exact_external_identity"], 0.0)
        self.assertEqual(score["experimental_components"]["exact_external_identity"], 20.0)
        self.assertFalse(publishable_observation(item))

    def test_fagfolk_rating_parser_uses_jsonld_and_slug_is_stable(self):
        raw = b'<script type="application/ld+json">{"aggregateRating":{"ratingValue":4.4,"ratingCount":25}}</script>'
        self.assertEqual(extract_aggregate_rating(raw)[:2], (4.4, 25))
        self.assertEqual(slug("NORDIC DØR AS"), "nordic-dor-as")


class SamplingTests(unittest.TestCase):
    def test_financial_filer_sample_requires_current_active_rows_and_preserves_overlap(self):
        fields = ["organisasjonsnummer", "navn", "organisasjonsform.kode", "sisteInnsendteAarsregnskap", "konkurs", "underAvvikling"]
        rows = [
            {"organisasjonsnummer": str(200000000 + index), "navn": f"Company {index}", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "false", "underAvvikling": "false"}
            for index in range(12)
        ] + [
            {"organisasjonsnummer": "300000001", "navn": "Stale AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2024", "konkurs": "false", "underAvvikling": "false"},
            {"organisasjonsnummer": "300000002", "navn": "Bankrupt AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "true", "underAvvikling": "false"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
            repeated, _ = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
        self.assertEqual([row["organisation_number"] for row in selected], [row["organisation_number"] for row in repeated])
        self.assertIn("200000003", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000001", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000002", {row["organisation_number"] for row in selected})
        self.assertEqual(metadata["eligible_rows"], 12)
        self.assertEqual(metadata["preserved_eligible_selected"], 1)
        self.assertTrue(all(financial_filer_eligible(row, "2025") for row in selected))

    def test_extension_sample_is_deterministic_and_excludes_initial(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": str(100000000 + index), "navn": f"Company {index}", "hjemmeside": "", "organisasjonsform.kode": "AS"}
            for index in range(20)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            first, metadata = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
            second, _ = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
        self.assertEqual([item["organisation_number"] for item in first], [item["organisation_number"] for item in second])
        self.assertEqual(len(first), 5)
        self.assertEqual(metadata["overlap_with_excluded"], 0)

    def test_strata_distinguish_adverse_and_web_coverage(self):
        base = {"legal_form": "AS", "employees": 12, "bankrupt": False, "liquidating": False, "website": "example.no"}
        self.assertEqual(stratum(base), "AS|5-19|active|web")
        self.assertEqual(stratum({**base, "bankrupt": True, "website": ""}), "AS|5-19|adverse|no-web")

    def test_normalize_does_not_invent_employee_count(self):
        row = normalize_row({"organisasjonsnummer": "923609016", "navn": "Example AS", "antallAnsatte": ""})
        self.assertIsNone(row["employees"])
        self.assertEqual(row["latest_submitted_accounts"], "")

    def test_fresh_website_audit_sample_excludes_poc_and_deduplicates_hosts(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": "111111111", "navn": "Excluded AS", "hjemmeside": "excluded.no", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "222222222", "navn": "A AS", "hjemmeside": "https://www.shared.no/a", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "333333333", "navn": "B AS", "hjemmeside": "shared.no/b", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "444444444", "navn": "C AS", "hjemmeside": "unique.no", "organisasjonsform.kode": "AS"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_website_audit_sample(path, 1, {"111111111"}, {"shared.no"}, seed=9)
        self.assertEqual(len(selected), 1)
        self.assertNotIn("111111111", {row["organisation_number"] for row in selected})
        self.assertEqual(selected[0]["organisation_number"], "444444444")
        self.assertEqual(metadata["unique_hosts_selected"], 1)
        self.assertEqual(metadata["excluded_website_hosts"], 1)


class OperationsTests(unittest.TestCase):
    def test_history_rate_limiter_spaces_request_starts_not_responses(self):
        import norway_company_agent.official as official

        old = official._history_last_request
        now = [10.0]
        sleeps = []

        def clock():
            return now[0]

        def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        try:
            official._history_last_request = 9.0
            _reserve_history_slot(clock, sleeper)
            self.assertAlmostEqual(sleeps[0], 1.1)
            self.assertAlmostEqual(official._history_last_request, 11.1)
            now[0] = 13.3
            _reserve_history_slot(clock, sleeper)
            self.assertEqual(len(sleeps), 1)
            self.assertAlmostEqual(official._history_last_request, 13.3)
        finally:
            official._history_last_request = old

    def test_batch_input_preserves_split_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgs.jsonl"
            path.write_text(json.dumps({"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress", "ignored": "x"}) + "\n", encoding="utf-8")
            self.assertEqual(read_organisation_inputs(path), [{"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress"}])

    def test_batch_contract_emits_exact_terminal_envelopes(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://example.test", content_sha256="a" * 64),
                "website": evidence("website", "blocked", "company_site", "https://example.test", note="robots.txt denied"),
            },
        }
        envelope = terminal_envelope(profile, run_id="day-1", modules=["registry", "website"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:01:00Z")
        self.assertEqual(envelope["modules"]["registry"]["state"], "complete")
        self.assertEqual(envelope["modules"]["website"]["state"], "blocked_robots")
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])
        self.assertFalse(validate_envelopes([envelope], 2)["passed"])

    def test_unknown_evidence_state_is_submission_error(self):
        self.assertEqual(evidence_terminal_state({"status": "not_fetched"}), "submission_error")

    def test_batch_resume_only_skips_profiles_with_all_terminal_modules(self):
        complete = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_found"}}}
        partial = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_fetched"}}}
        self.assertTrue(profile_complete_for_modules(complete, ["registry", "website"]))
        self.assertFalse(profile_complete_for_modules(partial, ["registry", "website"]))

    def test_nearest_rank_percentiles_are_deterministic(self):
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.5), 3)
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.95), 100)
        self.assertEqual(latency_summary([1, 2, 3]), {"n": 3, "p50_ms": 2.0, "p95_ms": 3.0, "max_ms": 3.0})

    def test_domain_fairness_summary_preserves_tail(self):
        result = domain_request_summary(__import__("collections").Counter({"a.no": 1, "b.no": 2, "c.no": 9}))
        self.assertEqual(result["domains"], 3)
        self.assertEqual(result["p50_requests"], 2.0)
        self.assertEqual(result["max_requests"], 9)


class WebsiteTests(unittest.TestCase):
    def test_interrupted_run_does_not_synthesize_terminal_failures(self):
        profiles = [{"organisation_number": "1", "website": "pending.no"}]
        self.assertEqual(terminal_events_for_run(profiles, [], False), [])
        self.assertEqual(len(terminal_events_for_run(profiles, [], True)), 1)

    def test_missing_seed_gets_explicit_terminal_event(self):
        profiles = [
            {"organisation_number": "1", "website": "example.no"},
            {"organisation_number": "2", "website": "blocked.no"},
            {"organisation_number": "3", "website": ""},
        ]
        existing = [{"organisation_number": "1", "status": "available"}]
        missing = missing_seed_error_events(profiles, existing)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["organisation_number"], "2")
        self.assertEqual(missing[0]["status"], "source_error")
        self.assertIn("robots.txt", missing[0]["error"])

    def test_crawl_event_extraction_and_merge_preserve_page_hashes(self):
        homepage = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/",
            final_url="https://example.no/",
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=b'<html><head><title>Example AS</title><meta name="description" content="Company"></head><body><p>Example AS provides enough substantive company information for extraction and identity review.</p><a href="https://linkedin.com/company/example">LinkedIn</a></body></html>',
            page_kind="homepage",
            retrieved_at="2026-08-22T00:00:00Z",
        )
        secondary = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/contact",
            final_url="https://example.no/contact",
            status_code=200,
            content_type="text/html",
            body=b"<html><title>Contact</title><body>Contact Example AS in Oslo.</body></html>",
            page_kind="priority",
            retrieved_at="2026-08-22T00:00:01Z",
        )
        record = merge_profile_events({"website": "https://example.no"}, [homepage, secondary])
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(record["value"]["pages"]), 2)
        self.assertEqual(record["content_sha256"], homepage["content_sha256"])
        self.assertEqual(record["value"]["scheduler"], "scrapy_resumable_v1")

    def test_footer_identity_is_preserved_for_exact_company_gate(self):
        event = extract_page_event(
            organisation_number="985628572",
            requested_url="https://netsolution.no/",
            final_url="https://netsolution.no/",
            status_code=200,
            content_type="text/html",
            body=(
                b'<html><head><title>IT services</title></head><body><main>Useful services for customers.</main>'
                b'<footer>Netsolution Viken AS, Kobbervikdalen 75 A, 3036 Drammen</footer></body></html>'
            ),
            page_kind="homepage",
            retrieved_at="2026-08-23T00:00:00Z",
        )
        website = merge_profile_events({"website": "https://netsolution.no/"}, [event])
        profile = {
            "organisation_number": "985628572",
            "name": "NETSOLUTION VIKEN AS",
            "evidence": {"website": website},
        }
        self.assertIn("Netsolution Viken AS", website["value"]["identity_text_excerpt"])
        self.assertTrue(assess_website_identity(profile)["publishable"])

    def test_normalizes_registry_hostname(self):
        self.assertEqual(normalize_homepage("example.no"), "https://example.no/")
        self.assertEqual(normalize_homepage("http://example.no"), "http://example.no/")

    def test_only_extracts_declared_social_links(self):
        soup = BeautifulSoup('<a href="https://www.linkedin.com/company/example/">LinkedIn</a><a href="/about">About</a>', "html.parser")
        self.assertEqual(_social_links("https://example.no", soup), [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}])

    def test_extracts_embedded_company_social_profiles(self):
        soup = BeautifulSoup(
            '<div class="fb-page" data-href="https://www.facebook.com/ExampleCompany"></div>'
            '<iframe src="https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FSecondCompany"></iframe>',
            "html.parser",
        )
        self.assertEqual(
            _social_links("https://example.no/", soup),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "facebook", "url": "https://facebook.com/SecondCompany"},
            ],
        )

    def test_extracts_schema_same_as_company_social_profiles(self):
        value = [{
            "@type": "Organization",
            "sameAs": [
                "https://www.facebook.com/ExampleCompany/",
                "https://instagram.com/examplecompany",
                "https://linkedin.com/in/example-person",
            ],
        }]
        self.assertEqual(
            structured_social_links(value),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "instagram", "url": "https://instagram.com/examplecompany"},
            ],
        )

    def test_social_profiles_reject_share_event_group_and_policy_links(self):
        rejected = (
            "https://facebook.com/sharer.php?u=x", "https://facebook.com/events/123",
            "https://facebook.com/groups/123", "https://facebook.com/policy.php",
            "https://facebook.com/privacy/explanation",
            "https://linkedin.com/shareArticle?url=x", "https://instagram.com/p/abc",
        )
        self.assertTrue(all(normalize_social_url(url) is None for url in rejected))

    def test_social_profiles_canonicalize_www_variants(self):
        self.assertEqual(normalize_social_url("https://www.facebook.com/Example/"), {"platform": "facebook", "url": "https://facebook.com/Example"})
        self.assertEqual(normalize_social_url("https://linkedin.com/company/example/admin/feed/posts"), {"platform": "linkedin", "url": "https://linkedin.com/company/example"})
        self.assertEqual(normalize_social_url("https://youtube.com/channel/abc/featured"), {"platform": "youtube", "url": "https://youtube.com/channel/abc"})
        self.assertIsNone(normalize_social_url("https://facebook.com/profile.php"))
        self.assertIsNone(normalize_social_url("https://[object Object]"))

    def test_priority_pages_stay_on_exact_site(self):
        soup = BeautifulSoup('<a href="/kontakt">Contact</a><a href="https://other.no/about">About</a><a href="/products">Products</a>', "html.parser")
        self.assertEqual(_priority_links("https://example.no/", soup), ["https://example.no/kontakt"])

    def test_js_shell_is_only_a_fallback_candidate(self):
        shell = BeautifulSoup('<html><script src="a.js"></script><script src="b.js"></script></html>', "html.parser")
        self.assertEqual(_extraction_state("", shell), "js_fallback_candidate")
        self.assertEqual(_extraction_state("A" * 100, shell), "static_complete")

    def test_blocks_local_network_targets(self):
        for url in ("http://127.0.0.1/admin", "http://localhost/", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                assert_public_url(url)


class DiscoveryTests(unittest.TestCase):
    def test_brave_request_keeps_key_out_of_url_and_parses_in_memory(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"web": {"results": [{
                    "url": "https://example.no", "title": "Example AS", "description": "Example in Oslo",
                }]}}).encode()

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.get_header("X-subscription-token")
            captured["timeout"] = timeout
            return Response()

        with patch("scripts.run_brave_discovery.urllib.request.urlopen", fake_open):
            results, operation = brave_search({
                "name": "Example AS", "organisation_number": "999999999", "municipality": "OSLO",
            }, "secret-test-key", timeout=3.0, count=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(operation["status"], 200)
        self.assertNotIn("secret-test-key", captured["url"])
        self.assertEqual(captured["key"], "secret-test-key")
        self.assertEqual(captured["timeout"], 3.0)

    def test_company_query_contains_exact_name_org_and_location(self):
        query = build_company_search_query({
            "name": "Norsk Fiskeeksport AS", "organisation_number": "923 609 016", "municipality": "NOTODDEN",
        })
        self.assertEqual(query, '"Norsk Fiskeeksport AS" 923609016 NOTODDEN')

    def test_brave_parser_is_provider_neutral_candidate_input(self):
        results = parse_brave_web_results({"web": {"results": [
            {"url": "https://example.no", "title": "Example AS", "description": "Example in Oslo"},
            {"title": "Missing URL"},
        ]}}, query='"Example AS" 999999999')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider"], "brave_search_api")
        self.assertEqual(results[0]["rank"], 1)

    def test_directory_and_social_results_are_not_company_site_candidates(self):
        profile = {"organisation_number": "923609016", "name": "Example Norge AS", "municipality": "OSLO"}
        for url in ("https://proff.no/selskap/example", "https://linkedin.com/company/example"):
            with self.subTest(url=url):
                self.assertEqual(score_search_candidate(profile, {"url": url, "title": "Example Norge AS"})["status"], "rejected")

    def test_exact_name_in_title_and_host_is_only_a_crawl_candidate(self):
        profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "municipality": "NOTODDEN"}
        decision = choose_search_candidate(profile, [{
            "url": "https://norskfiskeeksport.no/",
            "title": "Norsk Fiskeeksport AS",
            "snippet": "Seafood exporter in Notodden",
            "rank": 1,
            "provider": "fixture",
        }])
        self.assertFalse(decision["abstained"])
        self.assertEqual(decision["selected"]["status"], "accepted_for_crawl")
        self.assertIn("Publication still requires", decision["policy"])

    def test_ambiguous_name_match_without_host_support_abstains(self):
        profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "municipality": "NOTODDEN"}
        decision = choose_search_candidate(profile, [{"url": "https://parent-group.no/", "title": "Norsk Fiskeeksport AS - portfolio", "snippet": "Group companies"}])
        self.assertTrue(decision["abstained"])


class SentimentTests(unittest.TestCase):
    @staticmethod
    def item(item_id, label="positive", source="https://news.example/a", **changes):
        item = {
            "id": str(item_id), "label": label, "exact_entity": True,
            "source_class": "licensed_news", "source_url": source,
            "retrieved_at": "2026-08-22T00:00:00Z", "evidence_span": "Exact-company event sentence.",
            "content_sha256": "a" * 64,
        }
        item.update(changes)
        return item

    def test_company_owned_or_unhashed_items_are_not_publishable(self):
        self.assertFalse(publishable_sentiment_item(self.item(1, source_class="company_owned")))
        self.assertFalse(publishable_sentiment_item(self.item(1, content_sha256=None)))

    def test_inference_input_requires_exact_entity_independent_source_and_hash(self):
        item = self.item(1, text="Selskapet vant en ny kontrakt.")
        self.assertTrue(sentiment_input_eligibility(item)[0])
        accepted, reasons = sentiment_input_eligibility({**item, "exact_entity": False, "content_sha256": None})
        self.assertFalse(accepted)
        self.assertIn("exact company identity is not verified", reasons)
        self.assertIn("missing content_sha256", reasons)

    def test_pinned_model_output_normalization_is_closed_set(self):
        self.assertEqual(len(MODEL_REVISION), 40)
        self.assertEqual(normalize_generated_label(" Positive. "), "positive")
        self.assertIsNone(normalize_generated_label("bullish"))

    def test_rollup_requires_two_independent_publishers(self):
        same_publisher = [
            self.item(1, source="https://news.example/a"),
            self.item(2, source="https://news.example/b"),
        ]
        self.assertEqual(aggregate_company_sentiment(same_publisher)["status"], "abstain")
        independent = same_publisher + [self.item(3, source="https://other.example/c")]
        result = aggregate_company_sentiment(independent)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["label"], "positive")

    def test_perfect_balanced_300_item_corpus_passes_poc_gate(self):
        labels = ("positive", "neutral", "negative", "mixed")
        gold = [{"id": str(i), "label": labels[i % 4]} for i in range(300)]
        predictions = [self.item(i, labels[i % 4], source=f"https://news{i % 7}.example/item/{i}") for i in range(300)]
        report = evaluate_predictions(gold, predictions)
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["macro_f1"], 1.0)
        self.assertTrue(report["qualification_passed"])
        self.assertFalse(report["production_scale_gate_passed"])

    def test_wrong_entity_and_company_owned_predictions_fail_gate(self):
        labels = ("positive", "neutral", "negative")
        gold = [{"id": str(i), "label": labels[i % 3]} for i in range(300)]
        predictions = [self.item(i, labels[i % 3], source=f"https://news.example/{i}") for i in range(300)]
        predictions[0]["exact_entity"] = False
        predictions[1]["source_class"] = "company_owned"
        report = evaluate_predictions(gold, predictions)
        self.assertEqual(report["wrong_entity_predictions"], 1)
        self.assertEqual(report["company_owned_predictions"], 1)
        self.assertFalse(report["qualification_passed"])

    def test_qualification_minimum_cannot_be_weakened(self):
        with self.assertRaises(ValueError):
            evaluate_predictions([{"id": "1", "label": "positive"}], [self.item(1)], minimum_items=1)


class OfficialNormalizationTests(unittest.TestCase):
    def test_accounting_obligation_is_categorical_for_as_but_not_enk(self):
        company = accounting_obligation_assessment({"organisation_number": "923609016", "legal_form": "AS"})
        sole_trader = accounting_obligation_assessment({"organisation_number": "923609017", "legal_form": "ENK", "employees": 0})
        self.assertEqual(company["value"]["classification"], "required_by_legal_form")
        self.assertEqual(sole_trader["value"]["classification"], "threshold_or_activity_dependent")
        self.assertTrue(company["content_sha256"])
        self.assertEqual(company["source_row_key"], "923609016")

    def test_observed_filing_overrides_rule_path(self):
        record = accounting_obligation_assessment({"organisation_number": "923609018", "legal_form": "ENK", "latest_submitted_accounts": "2024"})
        self.assertEqual(record["value"]["classification"], "filing_observed")

    def test_entity_normalization_keeps_identity(self):
        record = normalize_entity({"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "organisasjonsform": {"kode": "ASA"}})
        self.assertEqual(record["organisation_number"], "923609016")
        self.assertEqual(record["legal_form"], "ASA")

    def test_financial_fields_keep_period_currency_and_zero(self):
        body = [{"id": 1, "valuta": "NOK", "regnskapsperiode": {"tilDato": "2025-12-31"}, "resultatregnskapResultat": {"driftsresultat": {"driftsresultat": 0, "driftsinntekter": {"sumDriftsinntekter": 12}}}}]
        record = normalize_financials(body)["records"][0]
        self.assertEqual(record["revenue"], 12)
        self.assertEqual(record["operating_result"], 0)
        self.assertEqual(record["currency"], "NOK")

    def test_financial_history_is_sorted_and_links_to_official_pdfs(self):
        record = normalize_financial_history(["2024", "2022", "2024", "invalid"], "923609016")
        self.assertEqual(record["years"], ["2022", "2024"])
        self.assertEqual(record["pdfs"][0]["year"], "2024")
        self.assertTrue(record["pdfs"][0]["url"].endswith("/923609016/2024"))

    def test_public_roles_drop_birth_dates(self):
        body = {"rollegrupper": [{"type": {"kode": "STYR"}, "roller": [{"type": {"kode": "LEDE", "beskrivelse": "Chair"}, "person": {"fodselsdato": "1970-01-01", "navn": {"fornavn": "Ada", "etternavn": "Nord"}}}]}]}
        record = normalize_roles(body)["roles"][0]
        self.assertEqual(record["name"], "Ada Nord")
        self.assertNotIn("fodselsdato", json.dumps(record))


class ResearchAgentTests(unittest.TestCase):
    @staticmethod
    def screen_row(org, municipality, employees, revenue):
        return {
            "organisation_number": org,
            "name": f"Company {org}",
            "municipality": municipality,
            "employees": employees,
            "evidence": {
                "registry": evidence("registry", "available", "official_registry_bulk", "https://example.test/registry", content_sha256="a" * 64),
                "financials": evidence("financials", "available", "official_annual_accounts", "https://example.test/accounts", value={"records": [{"revenue": revenue, "annual_result": 1}]}, content_sha256="b" * 64),
            },
        }

    def test_cross_company_screen_has_exact_membership_and_inspectable_plan(self):
        rows = [
            self.screen_row("111111111", "OSLO", 20, 2_000_000),
            self.screen_row("222222222", "OSLO", 5, 2_000_000),
            self.screen_row("333333333", "BERGEN", 20, 2_000_000),
            self.screen_row("444444444", "OSLO", 20, 500_000),
        ]
        result = screen_profiles(rows, "companies in Oslo with more than 10 employees and revenue over 1 million")
        self.assertFalse(result["abstained"])
        self.assertEqual([item["organisation_number"] for item in result["results"]], ["111111111"])
        self.assertEqual(len(result["plan"]["filters"]), 3)
        self.assertTrue(all(citation["content_sha256"] for citation in result["results"][0]["citations"]))

    def test_unsupported_cross_company_criterion_abstains(self):
        plan = parse_screen_query("companies in Oslo with positive Glassdoor sentiment")
        self.assertFalse(plan["executable"])
        result = screen_profiles([], "companies in Oslo with positive Glassdoor sentiment")
        self.assertTrue(result["abstained"])
        self.assertIn("Glassdoor", result["reason"])

    def test_missing_website_is_not_treated_as_proven_absence(self):
        result = screen_profiles([], "companies without a website")
        self.assertTrue(result["abstained"])
        self.assertIn("does not prove", result["reason"])

    def test_workspace_saves_pins_history_and_recovers_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            workspace, warning = load_workspace(path)
            self.assertIsNone(warning)
            updated = record_screen(workspace, {"query": "in Oslo", "plan": {"filters": []}, "result_count": 1, "results": [{"organisation_number": "111111111"}]}, pin_organisations=["111111111"])
            save_workspace(path, updated)
            loaded, warning = load_workspace(path)
            self.assertEqual(loaded["pins"], ["111111111"])
            self.assertEqual(len(loaded["history"]), 1)
            path.write_text("not json", encoding="utf-8")
            recovered, warning = load_workspace(path)
            self.assertEqual(recovered["pins"], [])
            self.assertIn("recovery", warning or "")

    def test_sentiment_abstains_and_every_fact_has_a_source(self):
        row = {
            "organisation_number": "923609016", "name": "Example AS", "legal_form": "AS",
            "municipality": "OSLO", "employees": None,
            "evidence": {"registry": evidence("registry", "available", "official", "https://example.test")},
        }
        result = answer_profile(row, "Give me employee sentiment")
        self.assertTrue(any("Sentiment is not scored" in item for item in result["unsupported_or_uncertain"]))
        self.assertTrue(all(fact["source_url"] for fact in result["facts"]))
        self.assertFalse(any(fact["claim"] == "Registry employee count" for fact in result["facts"]))

    def test_natural_leads_word_routes_to_roles(self):
        roles = evidence("roles", "available", "official", "https://example.test/roles", value={"roles": [{"name": "Ada Nord", "role": "Chair", "inactive": False}]})
        row = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"roles": roles}}
        result = answer_profile(row, "Who leads this company?")
        self.assertEqual(result["facts"][0]["value"], "Ada Nord")

    def test_quarantined_website_claims_are_not_returned(self):
        website = evidence("website", "available", "company_site", "https://parent.test", value={"description": "Parent claim", "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/parent"}], "identity_assessment": {"publishable": False}})
        row = {"organisation_number": "923609016", "name": "Subsidiary AS", "evidence": {"website": website}}
        result = answer_profile(row, "What social information is available?")
        self.assertFalse(result["facts"])
        self.assertTrue(any("quarantined" in item for item in result["unsupported_or_uncertain"]))


class PrototypeTests(unittest.TestCase):
    def test_qualification_copy_keeps_production_boundary_visible(self):
        header, boundary = qualification_copy({
            "weighted_score": {"verified_points": 100, "maximum_points": 100},
            "qualification": {"poc_qualified": True, "production_qualified": False},
        })
        self.assertIn("100/100", header)
        self.assertIn("not production-qualified", header)
        self.assertIn("remain quarantined", boundary)

    def test_quarantined_social_links_are_counted_but_not_published(self):
        row = {
            "organisation_number": "923609016",
            "name": "Subsidiary AS",
            "legal_form": "AS",
            "employees": None,
            "municipality": "OSLO",
            "industry_code": None,
            "industry_label": None,
            "website": "https://parent.test",
            "bankrupt": False,
            "liquidating": False,
            "evidence": {
                "website": evidence(
                    "website",
                    "available",
                    "company_site",
                    "https://parent.test",
                    value={
                        "identity_assessment": {"publishable": False},
                        "social_links": [],
                        "discovered_social_links": [
                            {"platform": "linkedin", "url": "https://linkedin.com/company/parent"}
                        ],
                    },
                )
            },
        }
        website = compact_prototype(row)["web"]["value"]
        self.assertEqual(website["quarantined_social_count"], 1)
        self.assertEqual(website["social_links"], [])


class RefreshTests(unittest.TestCase):
    def test_snapshot_fetcher_hashes_evaluator_bytes_and_carries_times(self):
        url = "https://example.test/entity/1"
        fetcher = SnapshotFetcher({"retrieved_at": "2026-01-02T00:00:00Z", "effective_at": "2026-01-01T00:00:00Z", "responses": {url: {"body": {"value": 1}}}})
        result = fetcher(url)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(result.content_sha256 or ""), 64)
        self.assertEqual(result.effective_at, "2026-01-01T00:00:00Z")

    def test_identical_refresh_is_an_idempotent_noop(self):
        row = {"organisation_number": "923609016", "name": "Example AS", "employees": 4}
        self.assertEqual(diff_profile(row, dict(row)), [])

    def test_missing_to_zero_is_a_real_change_with_provenance(self):
        source = evidence("registry", "available", "official", "https://example.test/entity")
        old = {"organisation_number": "923609016", "employees": None, "evidence": {"registry": source}}
        new = {"organisation_number": "923609016", "employees": 0, "evidence": {"registry": source}}
        change = diff_profile(old, new)[0]
        self.assertIsNone(change["old_value"])
        self.assertEqual(change["new_value"], 0)
        self.assertEqual(change["source_url"], "https://example.test/entity")

    def test_refresh_rejects_membership_or_identity_drift(self):
        with self.assertRaises(ValueError):
            diff_datasets([{"organisation_number": "923609016"}], [{"organisation_number": "999999999"}])


class WebsiteIdentityTests(unittest.TestCase):
    def test_group_contact_page_listing_subsidiary_org_number_is_not_exact_homepage_identity(self):
        profile = {
            "organisation_number": "915637353",
            "name": "SKS PRODUKSJON AS",
            "evidence": {"website": evidence("website", "available", "company_site", "https://sks.no", value={
                "final_url": "https://sks.no/",
                "title": "Konsern - SKS - Forside",
                "main_text_excerpt": "SKS is a power group with multiple subsidiaries.",
                "pages": [{"title": "Contact", "main_text_excerpt": "SKS Produksjon AS organisation number 915 637 353"}],
            })},
        }
        assessment = assess_website_identity(profile)
        self.assertFalse(assessment["publishable"])
        self.assertNotEqual(assessment["score"], 1.0)

    def test_shared_identity_gate_quarantines_parent_social_links(self):
        website = evidence("website", "available", "company_site", "https://parent.test", value={
            "title": "Parent Group", "main_text_excerpt": "Parent Group portfolio",
            "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/parent"}],
        })
        profile = {"organisation_number": "923609016", "name": "Exact Subsidiary AS", "evidence": {}}
        result = apply_website_identity_gate(profile, website)
        self.assertFalse(result["assessment"]["publishable"])
        self.assertEqual(result["website"]["value"]["social_links"], [])
        self.assertEqual(result["quarantined_social_links"], 1)

    def test_exact_legal_name_is_publishable(self):
        row = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "evidence": {"website": {"status": "available", "value": {"title": "Norsk Fiskeeksport AS"}}}}
        self.assertTrue(assess_website_identity(row)["publishable"])

    def test_parent_brand_without_legal_name_is_quarantined(self):
        row = {"organisation_number": "988412406", "name": "Tevlingveien 23 Invest AS", "evidence": {"website": {"status": "available", "value": {"title": "Ragde Eiendom"}}}}
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_parked_domain_and_parent_company_sports_site_are_quarantined(self):
        parked = {"organisation_number": "996081001", "name": "Condalign AS", "evidence": {"website": {"status": "available", "value": {"title": "CondAlign.com is for sale | HugeDomains"}}}}
        sports = {"organisation_number": "996242692", "name": "Primulator B.I.L.", "evidence": {"website": {"status": "available", "value": {"title": "Primulator", "description": "Premium products for HoReCa"}}}}
        self.assertFalse(assess_website_identity(parked)["publishable"])
        self.assertFalse(assess_website_identity(sports)["publishable"])

    def test_hosting_placeholder_and_generic_link_page_are_quarantined(self):
        hosting = {"organisation_number": "917568278", "name": "HJELMEN AS", "evidence": {"website": {"status": "available", "value": {"title": "www.Hjelmen-as.no is parked at Miss Hosting Web Hosting", "main_text_excerpt": "Hjelmen " * 100}}}}
        links = {"organisation_number": "986606009", "name": "KOALA ANS", "evidence": {"website": {"status": "available", "value": {"title": "koala.no", "description": "Find the best information and most relevant links on all topics related to"}}}}
        self.assertFalse(assess_website_identity(hosting)["publishable"])
        self.assertFalse(assess_website_identity(links)["publishable"])

    def test_broader_umbrella_site_is_not_exact_when_name_only_appears_in_body(self):
        row = {
            "organisation_number": "976994027",
            "name": "AVALDSNES SOKN",
            "evidence": {"website": {"status": "available", "value": {
                "title": "Kirken i Karmøy",
                "final_url": "https://www.karmoykirken.no/",
                "main_text_excerpt": "Avaldsnes sokn is one of several parishes represented on this umbrella site.",
            }}},
        }
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_social_handle_requires_exact_entity_name_evidence(self):
        aon = {"name": "Aon Norway AS"}
        fish = {"name": "Norsk Fiskeeksport AS"}
        self.assertFalse(assess_social_identity(aon, {"platform": "linkedin", "url": "https://linkedin.com/company/aon"})["publishable"])
        self.assertTrue(assess_social_identity(fish, {"platform": "linkedin", "url": "https://linkedin.com/company/norsk-fiskeeksport"})["publishable"])


class VerifiedSiteSeedTests(unittest.TestCase):
    def test_verified_seed_is_applied_and_unknown_org_is_rejected(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles.jsonl"
            seeds = root / "seeds.json"
            output = root / "output.jsonl"
            report = root / "report.json"
            profiles.write_text(json.dumps({"organisation_number": "123456789", "name": "Example AS"}) + "\n")
            seeds.write_text(json.dumps([{
                "organisation_number": "123456789",
                "website": "https://example.no/",
                "proof_url": "https://source.example/proof",
                "proof": "Exact name and organisation number",
            }]))
            command = [
                sys.executable, str(ROOT / "scripts" / "apply_verified_site_seeds.py"),
                "--profiles", str(profiles), "--seeds", str(seeds),
                "--output", str(output), "--report", str(report),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            row = json.loads(output.read_text().strip())
            self.assertEqual(row["website"], "https://example.no/")
            self.assertEqual(row["website_seed_source"], "independently_verified_exact_entity")
            self.assertEqual(json.loads(report.read_text())["applied"], 1)

            seeds.write_text(json.dumps([{
                "organisation_number": "987654321",
                "website": "https://unknown.no/",
                "proof_url": "https://source.example/proof",
            }]))
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("unknown organisations", failed.stderr)


if __name__ == "__main__":
    unittest.main()
