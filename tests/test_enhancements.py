from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import unittest
from unittest.mock import patch, MagicMock
from bs4 import BeautifulSoup

from norway_company_agent.website import _extract_job_postings, ATS_PLATFORM_NAMES
from norway_company_agent.news_credibility import classify_news_freshness
from norway_company_agent.external_connectors import discover_finn_jobs, discover_facebook_page
from norway_company_agent.research import synthesize_company_profile, _categorize_job_discipline


class TestEnhancements(unittest.TestCase):

    def test_extract_job_postings_enriched(self):
        html = """
        <html>
        <head>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org/",
            "@type": "JobPosting",
            "title": "Senior Cloud Engineer",
            "datePosted": "2026-10-01",
            "validThrough": "2026-12-31T23:59:59Z",
            "employmentType": "FULL_TIME",
            "hiringOrganization": {
                "@type": "Organization",
                "name": "Acme Corp AS"
            },
            "baseSalary": {
                "@type": "MonetaryAmount",
                "currency": "NOK",
                "value": {
                    "@type": "QuantitativeValue",
                    "value": 850000
                }
            },
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "addressLocality": "Oslo"
                }
            },
            "url": "https://acme.no/careers/cloud-engineer"
        }
        </script>
        </head>
        <body></body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_job_postings("https://acme.no", soup)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["title"], "Senior Cloud Engineer")
        self.assertEqual(job["location"], "Oslo")
        self.assertEqual(job["employment_type"], "FULL_TIME")
        self.assertEqual(job["hiring_organization"], "Acme Corp AS")
        self.assertIn("850000 NOK", job["salary"])
        self.assertEqual(job["is_expired"], False)
        self.assertIn("valid_through", job)

    def test_job_discipline_categorization(self):
        self.assertEqual(_categorize_job_discipline("Senior Fullstack Utvikler"), "Engineering & Technology")
        self.assertEqual(_categorize_job_discipline("Key Account Manager B2B"), "Sales & Commercial")
        self.assertEqual(_categorize_job_discipline("Daglig Leder / CEO"), "Executive & Management")
        self.assertEqual(_categorize_job_discipline("Lagermedarbeider og sjåfør"), "Operations & Trades")
        self.assertEqual(_categorize_job_discipline("Regnskapsfører"), "Finance & Administration")
        self.assertEqual(_categorize_job_discipline("Sykepleier 100% fast"), "Healthcare & Clinical")

    def test_classify_news_freshness(self):
        # Empty
        res_empty = classify_news_freshness([])
        self.assertEqual(res_empty["freshness_tier"], "no_dated_coverage")

        # Breaking (recent date)
        articles = [
            {"title": "Ny kontrakt", "published_at": "2026-10-10T12:00:00Z"},
            {"title": "Gammelt nytt", "published_at": "2025-01-01T12:00:00Z"},
        ]
        res = classify_news_freshness(articles, as_of="2026-10-14T00:00:00Z")
        self.assertEqual(res["freshness_tier"], "breaking")
        self.assertEqual(res["latest_article_age_days"], 3)
        self.assertEqual(res["dated_article_count"], 2)

        # Archival
        old_articles = [{"title": "Historisk", "published_at": "2022-01-01T00:00:00Z"}]
        res_old = classify_news_freshness(old_articles, as_of="2026-10-14T00:00:00Z")
        self.assertEqual(res_old["freshness_tier"], "archival")

    @patch("urllib.request.urlopen")
    def test_discover_finn_jobs_mocked(self, mock_urlopen):
        mock_html = """
        <html><body>
        <article>
            <a class="job-card-link" href="/job/ad/123456">Prosjektleder Bygg</a>
            <div>Prosjektleder Bygg | Fjellbygg AS | Spennende prosjekter | Lillehammer | 2 dager siden</div>
        </article>
        </body></html>
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_html.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        jobs = discover_finn_jobs("Fjellbygg AS", "999888777")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Prosjektleder Bygg")
        self.assertEqual(jobs[0]["source"], "finn_no")
        self.assertEqual(jobs[0]["company"], "Fjellbygg AS")
        self.assertIn("123456", jobs[0]["url"])

    @patch("urllib.request.urlopen")
    def test_discover_facebook_page_mocked(self, mock_urlopen):
        mock_html = """
        <html><head>
        <meta property="og:title" content="Fjellbygg AS - Mesterbedrift" />
        </head><body></body></html>
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_html.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        fb = discover_facebook_page("Fjellbygg AS", "999888777")
        self.assertIsNotNone(fb)
        self.assertEqual(fb["platform"], "facebook")
        self.assertEqual(fb["exact_entity"], True)

    def test_synthesize_company_profile_enriched_keys(self):
        sample_profile = {
            "name": "Testbedrift AS",
            "organisation_number": "123456789",
            "legal_form": "AS",
            "municipality": "Oslo",
            "employees": 12,
            "evidence": {
                "website": {
                    "status": "available",
                    "value": {
                        "ats_platforms": ["Teamtailor"],
                        "hiring_links": ["https://testbedrift.teamtailor.com"],
                    }
                }
            },
            "jobs": [
                {"title": "Senior Utvikler", "source": "finn_no", "url": "https://finn.no/job/1"},
                {"title": "Salgskonsulent", "source": "nav_arbeidsplassen", "url": "https://nav.no/job/2"}
            ],
            "dated_news": [
                {"title": "Rekordresultat", "published_at": "2026-09-01T00:00:00Z", "publisher": "e24.no"}
            ],
            "external_footprint": {
                "linkedin": {"profile_url": "https://linkedin.com/company/testbedrift"},
                "facebook": {"profile_url": "https://facebook.com/testbedrift"}
            }
        }
        res = synthesize_company_profile(sample_profile)
        self.assertIn("news_freshness", res)
        self.assertIn("hiring_velocity", res)
        self.assertIn("social_vitality", res)
        self.assertEqual(res["social_vitality"]["verified_platforms_count"], 2)
        self.assertEqual(res["hiring_velocity"]["open_vacancies_count"], 2)
        self.assertIn("Engineering & Technology", res["hiring_velocity"]["disciplines_hiring"])


if __name__ == "__main__":
    unittest.main()
