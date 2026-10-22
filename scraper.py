"""Public business contact discovery with bounded, robots-aware crawling."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

USER_AGENT = "CompanyContactFinder/1.0 (+local business research; respects robots.txt)"
REQUEST_TIMEOUT = 8
MAX_PAGES = 7
MAX_BYTES = 1_200_000
MIN_DOMAIN_DELAY = 0.65

EMAIL_RE = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().\-]{7,}\d)(?!\w)")
SKIP_EMAIL_PARTS = (
    "example.com", "domain.com", "email.com", "yourdomain", "yourcompany",
    "wixpress.com", "sentry.io", "cloudflare.com", "webpack", "localhost",
    "test.com", "mailinator.com", "invalid.com", "placeholder"
)
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip", ".mp4", ".mp3", ".css", ".js", ".xml")
CONTACT_WORDS = ("contact", "support", "help", "customer-care", "customer care", "about", "team", "reach-us", "reach us", "company", "privacy", "terms", "legal", "sales", "service", "location")
SKIP_PATH_WORDS = ("login", "signin", "sign-in", "register", "cart", "checkout", "account", "search", "tag/", "category/", "author/", "wp-admin")
BLOCKED_SEARCH_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "x.com", "twitter.com",
    "wikipedia.org", "justdial.com", "indiamart.com", "sulekha.com", "crunchbase.com",
    "glassdoor.com", "ambitionbox.com", "tracxn.com", "zoominfo.com", "rocketreach.co",
    "google.com/maps", "mapquest.com", "pinterest.com", "reddit.com"
)

_robots_cache: dict[str, RobotFileParser | None | bool] = {}
_robots_lock = threading.Lock()
_domain_last_request: dict[str, float] = {}
_domain_delay_lock = threading.Lock()


def normalize_input_url(value: str) -> str | None:
    value = (value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    if value.startswith(("http://", "https://")):
        candidate = value
    elif value.startswith("//"):
        candidate = "https:" + value
    elif "." in value and not value.startswith(("mailto:", "tel:")):
        candidate = "https://" + value
    else:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "." not in parsed.hostname:
        return None
    return candidate.rstrip("/")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _same_site(url_a: str, url_b: str) -> bool:
    # Keep crawling on the same host; only ignore a leading www.
    return _host(url_a) == _host(url_b)


def _robots_for(origin_url: str) -> RobotFileParser | None | bool:
    host = (urlparse(origin_url).hostname or "").lower()
    with _robots_lock:
        if host in _robots_cache:
            return _robots_cache[host]
    parsed = urlparse(origin_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if response.status_code in (404, 410):
            result: RobotFileParser | None | bool = None  # no robots file: allowed by default
        elif response.status_code in (401, 403) or response.status_code >= 500:
            result = False  # fail closed on explicit restriction or server failure
        elif response.ok:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            result = parser
        else:
            result = False
    except requests.RequestException:
        result = False  # fail closed if robots policy cannot be checked
    with _robots_lock:
        _robots_cache[host] = result
    return result


def _allowed_by_robots(url: str) -> bool:
    policy = _robots_for(url)
    if policy is False:
        return False
    if policy is None:
        return True
    try:
        return bool(policy.can_fetch(USER_AGENT, url))
    except Exception:
        return False


def _polite_delay(url: str) -> None:
    host = _host(url)
    with _domain_delay_lock:
        now = time.monotonic()
        last = _domain_last_request.get(host, 0.0)
        wait = MIN_DOMAIN_DELAY - (now - last)
        if wait > 0:
            time.sleep(wait)
        _domain_last_request[host] = time.monotonic()


def _download_html(url: str) -> tuple[str | None, str | None, str | None]:
    """Return (html, final_url, error); cap bytes and never follow an off-site redirect."""
    current_url = url
    for _ in range(4):
        if not _same_site(current_url, url):
            return None, current_url, "Redirected to a different host; skipped"
        if not _allowed_by_robots(current_url):
            return None, None, "Blocked by robots.txt or robots policy could not be checked"
        _polite_delay(current_url)
        try:
            response = requests.get(
                current_url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.2"},
                timeout=REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            return None, None, f"Request failed: {exc.__class__.__name__}"

        with response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    return None, current_url, f"HTTP {response.status_code} without redirect location"
                redirected = urljoin(current_url, location)
                if not _same_site(redirected, url):
                    return None, redirected, "Redirected to a different host; skipped"
                current_url = redirected
                continue
            if response.status_code >= 400:
                return None, current_url, f"HTTP {response.status_code}"
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not any(x in content_type for x in ("text/html", "application/xhtml+xml", "text/plain")):
                return None, current_url, "Not an HTML page"
            pieces: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                pieces.append(chunk)
                size += len(chunk)
                if size >= MAX_BYTES:
                    break
            raw = b"".join(pieces)[:MAX_BYTES]
            encoding = response.encoding or "utf-8"
            return raw.decode(encoding, errors="replace"), current_url, None
    return None, current_url, "Too many redirects"


def _valid_email(email: str) -> bool:
    email = email.strip().strip(".,;:!?)>]}\"").lower()
    if len(email) > 254 or ".." in email or any(part in email for part in SKIP_EMAIL_PARTS):
        return False
    if email.rsplit("@", 1)[-1].endswith((".png", ".jpg", ".webp", ".svg")):
        return False
    return True


def _extract_phone_candidates(text: str) -> list[str]:
    found: list[str] = []
    for match in PHONE_RE.findall(text):
        value = re.sub(r"\s+", " ", match).strip(" .-()")
        digits = re.sub(r"\D", "", value)
        if 10 <= len(digits) <= 15 and len(set(digits)) > 1:
            found.append(value)
    return found


def _priority(href: str, label: str = "") -> int:
    haystack = (href + " " + label).lower()
    return sum(4 if word in haystack else 0 for word in CONTACT_WORDS)


def _clean_link(href: str, base_url: str) -> str | None:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    absolute = urldefrag(urljoin(base_url, href))[0]
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not _same_site(absolute, base_url):
        return None
    if parsed.path.lower().endswith(SKIP_EXTENSIONS) or any(word in parsed.path.lower() for word in SKIP_PATH_WORDS):
        return None
    return absolute.rstrip("/") or f"{parsed.scheme}://{parsed.netloc}"


def _candidate_contact_paths(root_url: str) -> list[str]:
    parsed = urlparse(root_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    paths = [
        "/contact", "/contact-us", "/contactus", "/about", "/about-us", "/support",
        "/help", "/customer-care", "/contact.html", "/company/contact", "/pages/contact-us",
    ]
    return [origin + path for path in paths]


def scrape_website(url: str, max_pages: int = MAX_PAGES) -> dict:
    url = normalize_input_url(url) or url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"website": url, "emails": [], "phones": [], "pages_visited": 0, "source_urls": [], "status": "invalid_url", "error": "Enter a valid website URL or domain"}

    queue: deque[str] = deque([url])
    queued = {url.rstrip("/")}
    visited: list[str] = []
    emails: set[str] = set()
    phones: set[str] = set()
    first_error: str | None = None
    title = ""

    while queue and len(visited) < max_pages:
        current = queue.popleft()
        if current.rstrip("/") in {u.rstrip("/") for u in visited}:
            continue
        if not _same_site(current, url):
            continue
        html, final_url, error = _download_html(current)
        if error and not first_error:
            first_error = error
        if not html:
            continue
        current_url = final_url or current
        visited.append(current_url)
        soup = BeautifulSoup(html, "html.parser")
        if not title:
            title_tag = soup.find("title")
            title = title_tag.get_text(" ", strip=True)[:200] if title_tag else ""

        # Extract visible page text and explicit links, then look for emails and phones.
        for node in soup(["script", "style", "noscript", "svg", "head"]):
            node.decompose()
        page_text = soup.get_text(" ", strip=True)
        for email in EMAIL_RE.findall(page_text):
            email = email.strip(".,;:!?)>]}\"").lower()
            if _valid_email(email):
                emails.add(email)
        for a in soup.find_all("a", href=True):
            href_value = a.get("href", "").strip()
            label = a.get_text(" ", strip=True)
            if href_value.lower().startswith("mailto:"):
                raw = href_value[7:].split("?", 1)[0]
                for email in EMAIL_RE.findall(raw):
                    email = email.lower()
                    if _valid_email(email):
                        emails.add(email)
            elif href_value.lower().startswith("tel:"):
                raw_phone = href_value[4:].split("?", 1)[0].strip()
                digits = re.sub(r"\D", "", raw_phone)
                if 10 <= len(digits) <= 15:
                    phones.add(raw_phone)
            else:
                cleaned = _clean_link(href_value, current_url)
                if cleaned and cleaned not in queued and len(queued) < max_pages * 8:
                    queued.add(cleaned)
                    if _priority(href_value, label) > 0:
                        # add contact pages before generic links
                        queue.appendleft(cleaned)
                    else:
                        queue.append(cleaned)
        phones.update(_extract_phone_candidates(page_text))

        # Inject common contact endpoints after the home page's real links.
        if len(visited) == 1:
            for candidate in _candidate_contact_paths(url):
                candidate = candidate.rstrip("/")
                if candidate not in queued:
                    queued.add(candidate)
                    queue.append(candidate)

    status = "found" if emails or phones else ("no_contacts_found" if visited else "no_pages_scanned")
    return {
        "website": url,
        "site_title": title,
        "emails": sorted(emails),
        "phones": sorted(phones),
        "pages_visited": len(visited),
        "source_urls": visited,
        "status": status,
        "error": first_error if not visited else "",
    }
