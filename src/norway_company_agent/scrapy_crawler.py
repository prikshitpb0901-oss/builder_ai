from __future__ import annotations

import re
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import scrapy
from bs4 import BeautifulSoup
from scrapy.exceptions import IgnoreRequest

from .crawl_events import error_page_event, extract_page_event
from .website import USER_AGENT, _priority_links, assert_public_url, normalize_homepage


class PublicNetworkMiddleware:
    def process_request(self, request):
        try:
            assert_public_url(request.url)
        except ValueError as exc:
            raise IgnoreRequest(str(exc)) from exc

    def process_response(self, request, response):
        try:
            assert_public_url(response.url)
        except ValueError as exc:
            raise IgnoreRequest(str(exc)) from exc
        return response


class OperationalTelemetryMiddleware:
    def __init__(self, crawler):
        self.crawler = crawler
        crawler.signalpost_request_latency_ms = []
        crawler.signalpost_response_download_latency_ms = []
        crawler.signalpost_failure_attempt_latency_ms = []
        crawler.signalpost_request_domains = Counter()
        crawler.signalpost_response_statuses = Counter()
        crawler.signalpost_error_buckets = Counter()

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_request(self, request, spider):
        request.meta["_signalpost_started_at"] = time.monotonic()

    def _record(self, request):
        started = request.meta.get("_signalpost_started_at")
        if started is not None:
            self.crawler.signalpost_request_latency_ms.append((time.monotonic() - started) * 1000)
        host = (urllib.parse.urlparse(request.url).hostname or "unknown").casefold()
        self.crawler.signalpost_request_domains[host] += 1

    def process_response(self, request, response, spider):
        self._record(request)
        download_latency = request.meta.get("download_latency")
        if download_latency is not None:
            self.crawler.signalpost_response_download_latency_ms.append(float(download_latency) * 1000)
        self.crawler.signalpost_response_statuses[str(response.status)] += 1
        return response

    def process_exception(self, request, exception, spider):
        started = request.meta.get("_signalpost_started_at")
        self._record(request)
        if started is not None:
            self.crawler.signalpost_failure_attempt_latency_ms.append((time.monotonic() - started) * 1000)
        self.crawler.signalpost_error_buckets[type(exception).__name__] += 1


class SignalpostWebsiteSpider(scrapy.Spider):
    name = "signalpost_websites"
    custom_settings = {
        "USER_AGENT": USER_AGENT,
        "ROBOTSTXT_OBEY": True,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 1,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.25,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        # Apply the bound globally so Scrapy's internal robots.txt requests do
        # not retain the framework's 180/60 second connection defaults.
        "DOWNLOAD_TIMEOUT": 10,
        "DOWNLOAD_MAXSIZE": 2_000_000,
        "DOWNLOAD_WARNSIZE": 1_000_000,
        "REDIRECT_MAX_TIMES": 5,
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "DOWNLOADER_MIDDLEWARES": {
            "norway_company_agent.scrapy_crawler.PublicNetworkMiddleware": 50,
            "norway_company_agent.scrapy_crawler.OperationalTelemetryMiddleware": 850,
        },
    }

    def __init__(self, profiles_path: str, limit: int | None = None, organisation_numbers: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import json

        rows = [json.loads(line) for line in Path(profiles_path).read_text(encoding="utf-8").splitlines() if line.strip()]
        allowed = set(organisation_numbers.split(",")) if organisation_numbers else None
        self.profiles = [
            row for row in rows
            if row.get("website") and (allowed is None or row["organisation_number"] in allowed)
        ][: int(limit) if limit else None]

    def _initial_requests(self):
        for profile in self.profiles:
            supplied = str(profile.get("website") or "").strip()
            url = normalize_homepage(supplied)
            if not url:
                continue
            yield scrapy.Request(
                url,
                callback=self.parse_homepage,
                errback=self.errback_page,
                meta={
                    "organisation_number": profile["organisation_number"],
                    "requested_url": url,
                    "page_kind": "homepage",
                    "scheme_supplied": bool(re.match(r"^https?://", supplied, re.I)),
                    "_company_started_at": time.monotonic(),
                },
            )

    async def start(self):
        for request in self._initial_requests():
            yield request

    def start_requests(self):
        # Compatibility with Scrapy versions before the asynchronous start() contract.
        yield from self._initial_requests()

    def parse_homepage(self, response):
        self._record_company_completion(response.request)
        event = self._event(response)
        yield event
        if event.get("status") != "available":
            return
        soup = BeautifulSoup(response.body.decode(response.encoding or "utf-8", errors="replace"), "lxml")
        for url in _priority_links(response.url, soup):
            yield scrapy.Request(
                url,
                callback=self.parse_secondary,
                errback=self.errback_page,
                meta={
                    "organisation_number": response.meta["organisation_number"],
                    "requested_url": url,
                    "page_kind": "priority",
                    "scheme_supplied": True,
                },
            )

    def parse_secondary(self, response):
        yield self._event(response)

    def _event(self, response):
        return extract_page_event(
            organisation_number=response.meta["organisation_number"],
            requested_url=response.meta["requested_url"],
            final_url=response.url,
            status_code=response.status,
            content_type=response.headers.get(b"Content-Type", b"").decode("latin-1"),
            body=bytes(response.body),
            page_kind=response.meta["page_kind"],
        )

    def errback_page(self, failure):
        request = failure.request
        if (
            request.meta.get("page_kind") == "homepage"
            and not request.meta.get("scheme_supplied")
            and request.url.startswith("https://")
            and not request.meta.get("http_fallback_attempted")
        ):
            fallback = "http://" + request.url.removeprefix("https://")
            yield scrapy.Request(
                fallback,
                callback=self.parse_homepage,
                errback=self.errback_page,
                dont_filter=True,
                meta={**request.meta, "requested_url": fallback, "http_fallback_attempted": True},
            )
            return
        if request.meta.get("page_kind") == "homepage":
            self._record_company_completion(request)
        yield error_page_event(
            organisation_number=request.meta["organisation_number"],
            requested_url=request.meta["requested_url"],
            page_kind=request.meta["page_kind"],
            error=failure.getErrorMessage(),
        )

    def _record_company_completion(self, request):
        organisation_number = request.meta["organisation_number"]
        values = getattr(self.crawler, "signalpost_company_completion_ms_by_org", {})
        if organisation_number in values:
            return
        started = request.meta.get("_company_started_at")
        if started is not None:
            values[organisation_number] = (time.monotonic() - started) * 1000
        self.crawler.signalpost_company_completion_ms_by_org = values
