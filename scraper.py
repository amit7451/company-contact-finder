"""Public business contact discovery with bounded crawling and headless JS-rendered fallback."""
from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from collections import deque
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("company_contact_finder.scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "(CompanyContactFinder/2.0; public research; respects robots.txt)"
)
REQUEST_TIMEOUT = 10
MAX_PAGES = 8
MAX_BYTES = 1_500_000
MIN_DOMAIN_DELAY = 0.5

EMAIL_RE = re.compile(
    r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b"
)
# Matches obfuscated emails like "contact [at] example [dot] com" or "support(at)domain.com"
OBFUSCATED_AT_RE = re.compile(r"(?i)\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+)\s*")
OBFUSCATED_DOT_RE = re.compile(r"(?i)\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)\s*")

PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().\-]{7,}\d)(?!\w)")

SKIP_EMAIL_PARTS = (
    "example.com", "domain.com", "email.com", "yourdomain", "yourcompany",
    "wixpress.com", "sentry.io", "cloudflare.com", "webpack", "localhost",
    "test.com", "mailinator.com", "invalid.com", "placeholder", "bootstrap",
    "wordpress.org", "schema.org", "2x.png", "3x.png", "user@domain", "name@domain"
)

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip",
    ".mp4", ".mp3", ".css", ".js", ".xml", ".woff", ".woff2", ".ttf", ".eot"
)

CONTACT_WORDS = (
    "contact", "contact-us", "contactus", "support", "help", "customer-care",
    "customer care", "about", "about-us", "team", "our-team", "leadership",
    "reach-us", "reach us", "company", "privacy", "terms", "legal", "sales",
    "location", "locations", "impressum", "connect", "inquiry"
)

SKIP_PATH_WORDS = (
    "login", "signin", "sign-in", "register", "cart", "checkout", "account",
    "search", "tag/", "category/", "author/", "wp-admin", "wp-content/plugins"
)

SOCIAL_DOMAINS = {
    "linkedin": re.compile(r"(?i)https?://(?:www\.)?linkedin\.com/(?:company|in|school)/[a-zA-Z0-9_\-]+/?"),
    "twitter": re.compile(r"(?i)https?://(?:www\.)?(?:twitter|x)\.com/[a-zA-Z0-9_]{1,30}/?"),
    "facebook": re.compile(r"(?i)https?://(?:www\.)?facebook\.com/[a-zA-Z0-9.\-_]{2,50}/?"),
    "instagram": re.compile(r"(?i)https?://(?:www\.)?instagram\.com/[a-zA-Z0-9._]{1,30}/?"),
    "youtube": re.compile(r"(?i)https?://(?:www\.)?youtube\.com/(?:@|c/|channel/|user/)?[a-zA-Z0-9._\-]+/?"),
    "github": re.compile(r"(?i)https?://(?:www\.)?github\.com/[a-zA-Z0-9_\-]+/?"),
}

_robots_cache: dict[str, RobotFileParser | None | bool] = {}
_robots_lock = threading.Lock()
_domain_last_request: dict[str, float] = {}
_domain_delay_lock = threading.Lock()

# Playwright browser lock to prevent overwhelming memory on local machines
_playwright_lock = threading.Lock()


def decode_cloudflare_email(cf_hex: str) -> str:
    """Decode Cloudflare email protection hex string."""
    try:
        cf_hex = cf_hex.strip()
        if not cf_hex or len(cf_hex) < 4:
            return ""
        k = int(cf_hex[:2], 16)
        email = "".join(chr(int(cf_hex[i:i+2], 16) ^ k) for i in range(2, len(cf_hex), 2))
        return email if "@" in email else ""
    except Exception:
        return ""


def normalize_input_url(value: str) -> str | None:
    """Normalize input URL or domain string."""
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
    return _host(url_a) == _host(url_b)


def _robots_for(origin_url: str) -> RobotFileParser | None | bool:
    host = (urlparse(origin_url).hostname or "").lower()
    with _robots_lock:
        if host in _robots_cache:
            return _robots_cache[host]
    parsed = urlparse(origin_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = requests.get(
            robots_url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        if response.status_code in (404, 410):
            result: RobotFileParser | None | bool = None
        elif response.status_code in (401, 403) or response.status_code >= 500:
            result = False
        elif response.ok:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            result = parser
        else:
            result = False
    except requests.RequestException:
        result = None  # Polite fallback if robots.txt unreachable
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
        return True


def _polite_delay(url: str) -> None:
    host = _host(url)
    with _domain_delay_lock:
        now = time.monotonic()
        last = _domain_last_request.get(host, 0.0)
        wait = MIN_DOMAIN_DELAY - (now - last)
        if wait > 0:
            time.sleep(wait)
        _domain_last_request[host] = time.monotonic()


def _valid_email(email: str) -> bool:
    email = email.strip().strip(".,;:!?)>]}\"'\t\n\r").lower()
    if len(email) > 254 or "@" not in email or ".." in email:
        return False
    user, _, domain = email.partition("@")
    if not user or not domain or "." not in domain:
        return False
    if any(part in email for part in SKIP_EMAIL_PARTS):
        return False
    if domain.endswith(SKIP_EXTENSIONS):
        return False
    # Filter numeric or single-letter bogus domains
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2 or not tld.isalpha():
        return False
    return True


def _clean_phone(raw: str) -> str | None:
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if not (8 <= len(digits) <= 15):
        return None
    if len(set(digits)) <= 2:  # Avoid 0000000000 or 11111111
        return None
    # Reject range/schedule patterns like "0-2 2-4 4-6" or excessive hyphenation
    if re.search(r"\d+-\d+\s+\d+-\d+", raw) or raw.count("-") >= 4:
        return None
    # Reject date ranges like 2020-2024 or 2018-2026
    if re.search(r"20[0-2]\d[-–—]20[0-3]\d", raw):
        return None
    segments = re.findall(r"\d+", raw)
    if len(segments) >= 4 and all(len(s) <= 2 for s in segments):
        return None
    # Standardize spaces and symbols
    cleaned = re.sub(r"\s+", " ", raw).strip(" .-()")
    return cleaned


def _extract_phone_candidates(text: str) -> list[str]:
    found: list[str] = []
    for match in PHONE_RE.findall(text):
        cleaned = _clean_phone(match)
        if cleaned:
            found.append(cleaned)
    return found


def _clean_link(href: str, base_url: str) -> str | None:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    try:
        absolute = urldefrag(urljoin(base_url, href))[0]
    except Exception:
        return None
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not _same_site(absolute, base_url):
        return None
    if parsed.path.lower().endswith(SKIP_EXTENSIONS) or any(word in parsed.path.lower() for word in SKIP_PATH_WORDS):
        return None
    return absolute.rstrip("/") or f"{parsed.scheme}://{parsed.netloc}"


def _priority(href: str, label: str = "") -> int:
    haystack = (href + " " + label).lower()
    return sum(4 if word in haystack else 0 for word in CONTACT_WORDS)


def _candidate_contact_paths(root_url: str) -> list[str]:
    parsed = urlparse(root_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    paths = [
        "/contact", "/contact-us", "/contactus", "/about", "/about-us",
        "/support", "/help", "/customer-care", "/pages/contact-us",
        "/reach-us", "/our-team", "/team", "/impressum", "/locations"
    ]
    return [origin + p for p in paths]


def _extract_social_links(soup: BeautifulSoup | None, text_content: str = "") -> dict[str, str]:
    socials: dict[str, str] = {}
    links: list[str] = []
    if soup:
        for a in soup.find_all("a", href=True):
            links.append(a.get("href", "").strip())
    if text_content:
        for token in text_content.split():
            if "://" in token:
                links.append(token)

    for platform, pattern in SOCIAL_DOMAINS.items():
        if platform in socials:
            continue
        for link in links:
            match = pattern.search(link)
            if match:
                clean_url = match.group(0).rstrip("/")
                # Filter out generic share links or base domain links
                path = urlparse(clean_url).path.strip("/")
                if not path or path in ("intent", "share", "sharer", "sharer.php", "home", "search", "login"):
                    continue
                socials[platform] = clean_url
                break
    return socials


def _extract_contacts_from_html(html_str: str, base_url: str) -> tuple[set[str], set[str], dict[str, str], str]:
    """Parse HTML and extract emails, phones, social links, and page title."""
    emails: set[str] = set()
    phones: set[str] = set()
    unescaped_html = html.unescape(html_str or "")
    soup = BeautifulSoup(unescaped_html, "html.parser")

    # 1. Page Title
    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(" ", strip=True)[:200]
    if not title:
        og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
        if og_title and og_title.get("content"):
            title = str(og_title["content"]).strip()[:200]

    # 2. Cloudflare Protected Emails
    for cf_elem in soup.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cloudflare_email(cf_elem.get("data-cfemail", ""))
        if decoded and _valid_email(decoded):
            emails.add(decoded.lower())
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/cdn-cgi/l-email-protection#" in href:
            cf_hex = href.split("/cdn-cgi/l-email-protection#", 1)[-1].split("?", 1)[0]
            decoded = decode_cloudflare_email(cf_hex)
            if decoded and _valid_email(decoded):
                emails.add(decoded.lower())

    # 3. Mailto & Tel Links, and WhatsApp Links
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        lower_href = href.lower()
        if lower_href.startswith("mailto:"):
            raw_email = href[7:].split("?", 1)[0].strip()
            for found in EMAIL_RE.findall(raw_email):
                if _valid_email(found):
                    emails.add(found.lower())
        elif lower_href.startswith("tel:"):
            raw_phone = href[4:].split("?", 1)[0].strip()
            cleaned = _clean_phone(raw_phone)
            if cleaned:
                phones.add(cleaned)
        elif "wa.me/" in lower_href or "api.whatsapp.com/send" in lower_href:
            # WhatsApp business phone
            if "wa.me/" in lower_href:
                part = href.split("wa.me/", 1)[-1].split("?", 1)[0].strip()
            else:
                qs = parse_qs(urlparse(href).query)
                part = (qs.get("phone") or [""])[0]
            cleaned = _clean_phone(part)
            if cleaned:
                phones.add(cleaned)

    # 4. Social Links
    socials = _extract_social_links(soup)

    # 5. Visible Text Extraction
    for node in soup(["script", "style", "noscript", "svg", "head", "iframe"]):
        node.decompose()
    page_text = soup.get_text(" ", strip=True)

    # Obfuscated Email check: "name [at] domain [dot] com"
    obf_text = OBFUSCATED_AT_RE.sub("@", page_text)
    obf_text = OBFUSCATED_DOT_RE.sub(".", obf_text)
    for email in EMAIL_RE.findall(obf_text):
        if _valid_email(email):
            emails.add(email.lower())

    # Regular Visible Text Email check
    for email in EMAIL_RE.findall(page_text):
        if _valid_email(email):
            emails.add(email.lower())

    # Phone candidates from text
    phones.update(_extract_phone_candidates(page_text))

    return emails, phones, socials, title


def _download_html(url: str) -> tuple[str | None, str | None, str | None]:
    """Download HTML via polite requests."""
    current_url = url
    for _ in range(4):
        if not _same_site(current_url, url):
            return None, current_url, "Redirected off-site"
        if not _allowed_by_robots(current_url):
            return None, None, "Disallowed by robots.txt"
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
                    return None, current_url, f"HTTP {response.status_code} without Location header"
                redirected = urljoin(current_url, location)
                if not _same_site(redirected, url):
                    return None, redirected, "Redirected off-site"
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


def scrape_website_js_rendered(url: str, candidate_paths: list[str] | None = None) -> dict:
    """Fallback headless browser rendering using Playwright for JavaScript-heavy or SPA websites."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright is not installed. Skipping JS fallback.")
        return {"emails": [], "phones": [], "socials": {}, "pages_visited": 0, "source_urls": [], "error": "Playwright not installed"}

    emails: set[str] = set()
    phones: set[str] = set()
    socials: dict[str, str] = {}
    visited_urls: list[str] = []
    title = ""

    # Limit concurrency of browser instances to protect memory
    with _playwright_lock:
        playwright_obj = None
        browser = None
        try:
            playwright_obj = sync_playwright().start()
            browser = playwright_obj.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(14000)

            # 1. Load Homepage
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=12000)
                # Wait briefly for JS hydration (React/Vue/Next.js)
                page.wait_for_timeout(1800)
                # Scroll down to trigger lazy footers or contact widgets
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
                    page.wait_for_timeout(500)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    page.wait_for_timeout(500)
                except Exception:
                    pass

                content = page.content()
                current_url = page.url or url
                visited_urls.append(current_url)

                em, ph, soc, t = _extract_contacts_from_html(content, current_url)
                emails.update(em)
                phones.update(ph)
                socials.update(soc)
                if not title and t:
                    title = t
            except Exception as e:
                logger.debug("Playwright error loading homepage %s: %s", url, e)

            # 2. If still no contacts, try visiting a high-priority contact path
            if not emails and not phones and candidate_paths:
                for candidate in candidate_paths[:2]:  # Try top 2 candidate contact paths
                    if candidate.rstrip("/") in {u.rstrip("/") for u in visited_urls}:
                        continue
                    try:
                        page.goto(candidate, wait_until="domcontentloaded", timeout=10000)
                        page.wait_for_timeout(1500)
                        contact_content = page.content()
                        visited_urls.append(page.url or candidate)
                        em, ph, soc, t = _extract_contacts_from_html(contact_content, page.url or candidate)
                        emails.update(em)
                        phones.update(ph)
                        socials.update(soc)
                        if emails or phones:
                            break
                    except Exception:
                        continue

            context.close()
        except Exception as exc:
            logger.error("Playwright JS rendering failed for %s: %s", url, exc)
            return {
                "emails": sorted(emails),
                "phones": sorted(phones),
                "socials": socials,
                "pages_visited": len(visited_urls),
                "source_urls": visited_urls,
                "site_title": title,
                "error": f"JS rendering error: {exc.__class__.__name__}",
            }
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright_obj:
                try:
                    playwright_obj.stop()
                except Exception:
                    pass

    return {
        "emails": sorted(emails),
        "phones": sorted(phones),
        "socials": socials,
        "pages_visited": len(visited_urls),
        "source_urls": visited_urls,
        "site_title": title,
        "error": "",
    }


def scrape_website(url: str, max_pages: int = MAX_PAGES, enable_js_fallback: bool = True) -> dict:
    """Primary website scraper with static crawling and automatic JS-rendered fallback."""
    url = normalize_input_url(url) or url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {
            "website": url,
            "site_title": "",
            "emails": [],
            "phones": [],
            "socials": {},
            "pages_visited": 0,
            "source_urls": [],
            "status": "invalid_url",
            "error": "Enter a valid website URL or domain",
            "js_rendered": False,
        }

    queue: deque[str] = deque([url])
    queued = {url.rstrip("/")}
    visited: list[str] = []
    emails: set[str] = set()
    phones: set[str] = set()
    socials: dict[str, str] = {}
    first_error: str | None = None
    title = ""

    # Phase 1: Static Requests Crawl
    while queue and len(visited) < max_pages:
        current = queue.popleft()
        if current.rstrip("/") in {u.rstrip("/") for u in visited}:
            continue
        if not _same_site(current, url):
            continue

        html_content, final_url, error = _download_html(current)
        if error and not first_error:
            first_error = error
        if not html_content:
            continue

        current_url = final_url or current
        visited.append(current_url)

        # Extract Contacts & Socials
        em, ph, soc, t = _extract_contacts_from_html(html_content, current_url)
        emails.update(em)
        phones.update(ph)
        socials.update(soc)
        if not title and t:
            title = t

        # Crawl internal links
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            for a in soup.find_all("a", href=True):
                href_value = a.get("href", "").strip()
                label = a.get_text(" ", strip=True)
                cleaned = _clean_link(href_value, current_url)
                if cleaned and cleaned not in queued and len(queued) < max_pages * 6:
                    queued.add(cleaned)
                    if _priority(href_value, label) > 0:
                        queue.appendleft(cleaned)
                    else:
                        queue.append(cleaned)
        except Exception:
            pass

        # Inject common candidate paths after home page
        if len(visited) == 1:
            for candidate in _candidate_contact_paths(url):
                candidate = candidate.rstrip("/")
                if candidate not in queued:
                    queued.add(candidate)
                    queue.append(candidate)

    # Phase 2: Headless JS Rendered Fallback if no contacts found
    js_rendered = False
    if enable_js_fallback and not emails and not phones:
        logger.info("No contacts found statically on %s. Launching JS-rendered fallback...", url)
        candidates = _candidate_contact_paths(url)
        js_result = scrape_website_js_rendered(url, candidate_paths=candidates)
        if js_result.get("emails") or js_result.get("phones"):
            js_rendered = True
            emails.update(js_result.get("emails", []))
            phones.update(js_result.get("phones", []))
            socials.update(js_result.get("socials", {}))
            if not title and js_result.get("site_title"):
                title = js_result.get("site_title")
            for src in js_result.get("source_urls", []):
                if src not in visited:
                    visited.append(src)
        elif js_result.get("pages_visited", 0) > 0 and not visited:
            visited = js_result.get("source_urls", [])
            socials.update(js_result.get("socials", {}))

    status = (
        "found (js_rendered)" if (emails or phones) and js_rendered
        else "found" if (emails or phones)
        else ("no_contacts_found" if visited else "no_pages_scanned")
    )

    return {
        "website": url,
        "site_title": title,
        "emails": sorted(emails),
        "phones": sorted(phones),
        "socials": socials,
        "pages_visited": len(visited),
        "source_urls": visited,
        "status": status,
        "error": first_error if not visited and not emails and not phones else "",
        "js_rendered": js_rendered,
    }
