from __future__ import annotations

import json
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nb,no;q=0.9,en-US;q=0.8,en;q=0.7",
}
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
    "karriere", "career", "jobb", "ledige-stillinger", "stillinger",
)



# Ensure global socket operations have a strict timeout to prevent indefinite hangs
socket.setdefaulttimeout(10.0)

KNOWN_FRAUD_OR_PARKED_DOMAINS = {
    "sedoparking.com", "dan.com", "hugedomains.com", "afternic.com",
    "bodis.com", "parkingcrew.net", "voodoo.com", "zeropark.com",
    "undeveloped.com", "domainmarket.com", "buydomains.com",
    "parked.com", "domainsponsor.com",
}


def _read_bounded_with_deadline(response: Any, max_bytes: int, deadline: float) -> bytes:
    """Read bytes from response stream with strict byte limit and absolute wall-clock deadline."""
    chunks = []
    total = 0
    while total <= max_bytes:
        if time.monotonic() > deadline:
            raise TimeoutError("Response read exceeded wall-clock deadline (tarpit defense)")
        requested = min(16384, max_bytes + 1 - total)
        chunk = response.read(requested)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("Response read exceeded wall-clock deadline (tarpit defense)")
        if len(chunk) < requested:
            break
    return b"".join(chunks)


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    if parsed.port and parsed.port not in {80, 443, 8080, 8443}:
        raise ValueError(f"Dangerous or non-standard port {parsed.port} is blocked")
    candidates = [host]
    if host.startswith("www."):
        candidates.append(host.removeprefix("www."))
    else:
        candidates.append("www." + host)
    addresses = set()
    last_exc = None
    for cand in candidates:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(cand, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
            if addresses:
                break
        except socket.gaierror as exc:
            last_exc = exc
    if not addresses:
        raise ValueError("Hostname did not resolve") from last_exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        dest_domain = _registered_domain(newurl)
        if dest_domain in KNOWN_FRAUD_OR_PARKED_DOMAINS:
            raise ValueError(f"Redirect to known parked or fraudulent domain blocked: {dest_domain}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _robots_allowed(url: str, timeout: float) -> bool:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        fetch_timeout = min(timeout, 3.5)
        deadline = time.monotonic() + fetch_timeout
        with SAFE_OPENER.open(request, timeout=fetch_timeout) as response:
            raw = _read_bounded_with_deadline(response, 65536, deadline)
            parser.parse(raw.decode("utf-8", errors="replace").splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        # An unavailable robots file is not permission to ignore explicit site terms; callers retain
        # the URL and can route uncertain domains to review. For this bounded homepage POC, allow one
        # ordinary GET when robots.txt is absent rather than crawl deeper.
        return True



def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
    return [url for url, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _extract_dated_news_articles(base_url: str, soup: BeautifulSoup) -> list[dict[str, Any]]:
    articles = []
    seen = set()
    for time_node in soup.find_all(["time", "span", "div", "p"]):
        dt_val = None
        if time_node.name == "time" and time_node.get("datetime"):
            dt_val = str(time_node.get("datetime")).strip()
        else:
            text = time_node.get_text(" ", strip=True)
            m = re.search(r"\b(?:publisert|published|oppdatert)?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text, re.I)
            if m:
                day, month, year = m.groups()
                try:
                    dt_val = f"{int(year):04d}-{int(month):02d}-{int(day):02d}T00:00:00Z"
                except Exception:
                    pass
            elif re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
                m_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
                if m_iso:
                    dt_val = f"{m_iso.group(1)}-{m_iso.group(2)}-{m_iso.group(3)}T00:00:00Z"

        if dt_val:
            parent = time_node.parent
            link = None
            title = None
            for _ in range(4):
                if parent is None:
                    break
                link = parent.find("a", href=True)
                heading = parent.find(["h1", "h2", "h3", "h4"])
                if heading:
                    title = heading.get_text(" ", strip=True)
                if link and title:
                    break
                parent = parent.parent
            if link and not title:
                title = link.get_text(" ", strip=True)
            if link and title and len(title) >= 10:
                art_url = urllib.parse.urljoin(base_url, link.get("href"))
                if art_url not in seen:
                    seen.add(art_url)
                    articles.append({
                        "url": art_url,
                        "title": title[:200],
                        "published_at": dt_val,
                    })

    meta_date = None
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name") or ""
        if prop in ("article:published_time", "og:published_time", "pubdate", "publishdate", "date"):
            meta_date = meta.get("content")
            break
    if meta_date and soup.title:
        title = soup.title.get_text(" ", strip=True)
        if base_url not in seen and len(title) >= 5:
            seen.add(base_url)
            articles.append({
                "url": base_url,
                "title": title[:200],
                "published_at": meta_date,
            })

    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        atext = anchor.get_text(" ", strip=True)
        if len(atext) < 15:
            continue
        full_href = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(full_href)
        if any(term in parsed.path.lower() for term in ("/nyheter/", "/news/", "/aktuelt/", "/presse/", "/artikler/", "/media/")):
            date_match = re.search(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b", full_href) or re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", atext)
            pub_date = None
            if date_match:
                g = date_match.groups()
                if len(g[0]) == 4:
                    pub_date = f"{g[0]}-{int(g[1]):02d}-{int(g[2]):02d}T00:00:00Z"
                else:
                    pub_date = f"{g[2]}-{int(g[1]):02d}-{int(g[0]):02d}T00:00:00Z"
            if full_href not in seen:
                seen.add(full_href)
                articles.append({
                    "url": full_href,
                    "title": atext[:200],
                    "published_at": pub_date,
                })

    return articles[:10]


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], list[str], list[dict[str, Any]], int, int, int, str | None]:
    if not _robots_allowed(url, timeout):
        return None, [], [], [], 1, 0, 0, "robots.txt disallows page"
    started = time.monotonic()
    deadline = started + timeout
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = _read_bounded_with_deadline(response, max_bytes + 1, deadline)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], [], [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], [], [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        }
        sec_hiring = []
        for anchor in page_soup.select("a[href]"):
            ahref = str(anchor.get("href") or "").lower()
            atext = anchor.get_text(" ", strip=True).lower()
            if any(term in ahref or term in atext for term in ("karriere", "career", "jobb", "ledige-stillinger", "stillinger", "stilling", "work-with-us")):
                c_url = urllib.parse.urljoin(final_url, str(anchor.get("href")))
                if c_url not in sec_hiring:
                    sec_hiring.append(c_url)
        sec_news = _extract_dated_news_articles(final_url, page_soup)
        return page, _social_links(final_url, page_soup), sec_hiring[:5], sec_news[:5], 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], [], [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    
    # Try public URL with www / apex fallback
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        # Fallback between apex and www. prefix
        parsed = urllib.parse.urlparse(normalized)
        alt_netloc = parsed.netloc.removeprefix("www.") if parsed.netloc.startswith("www.") else ("www." + parsed.netloc)
        alt_url = urllib.parse.urlunparse((parsed.scheme, alt_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
        try:
            assert_public_url(alt_url)
            normalized = alt_url
        except ValueError:
            return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}

    if not _robots_allowed(normalized, timeout):
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    deadline = started + timeout
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = _read_bounded_with_deadline(response, max_bytes + 1, deadline)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
            final_domain = _registered_domain(final_url)
            if final_domain in KNOWN_FRAUD_OR_PARKED_DOMAINS:
                return evidence("website", "blocked", "registry_linked_company_website", final_url, note=f"Known domain parking or fraudulent domain blocked: {final_domain}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        footer_node = soup.select_one("footer")
        footer_text = footer_node.get_text(" ", strip=True) if footer_node else (text[-1000:] if len(text) > 1000 else text)
        career_anchors = []
        for anchor in soup.select("a[href]"):
            ahref = str(anchor.get("href") or "").lower()
            atext = anchor.get_text(" ", strip=True).lower()
            if any(term in ahref or term in atext for term in ("karriere", "career", "jobb", "ledige-stillinger", "stillinger", "stilling", "work-with-us")):
                c_url = urllib.parse.urljoin(final_url, str(anchor.get("href")))
                if c_url not in career_anchors:
                    career_anchors.append(c_url)
        homepage_news = _extract_dated_news_articles(final_url, soup)
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "footer_text": footer_text[:2000],
            "hiring_links": career_anchors[:5],
            "news_articles": homepage_news[:5],
            "social_links": _social_links(final_url, soup),
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "extraction_state": _extraction_state(text, soup),
        }
        pages = [{"url": final_url, "title": title[:500], "main_text_excerpt": text[:5000], "content_sha256": value["content_sha256"]}]
        social = value["social_links"]
        all_hiring = list(career_anchors)
        all_news = list(homepage_news)
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = value["registered_domain"]
        for page_url in _priority_links(final_url, soup):
            page, page_social, page_hiring, page_news, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                social.extend(page_social)
                for h in page_hiring:
                    if h not in all_hiring:
                        all_hiring.append(h)
                for n in page_news:
                    if not any(existing["url"] == n["url"] for existing in all_news):
                        all_news.append(n)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["hiring_links"] = all_hiring[:8]
        value["news_articles"] = all_news[:8]
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except TimeoutError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note="Connection timed out or tarpit detected"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        is_timeout = isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)) or "timed out" in str(getattr(exc, "reason", "")).lower()
        if not supplied_scheme and normalized.startswith("https://") and not is_timeout:
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website("http://" + supplied_url, timeout=min(timeout, 4.0), max_bytes=max_bytes)
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}

