from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from scraper import normalize_input_url, scrape_website

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("company_contact_finder")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_ROWS = 2500
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

app = FastAPI(
    title="Company Contact Finder",
    description="Full-scale company & website contact discoverer with JS rendering fallback and smart synonym detection.",
    version="2.0.0",
)
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
job_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="job")

# Extensive Synonym Dictionaries for Column Headers
COMPANY_SYNONYMS = {
    # Direct company terms
    "company", "companies", "companyname", "companynames", "co", "coname", "companytitle",
    # Brand terms
    "brand", "brands", "brandname", "brandnames", "brandtitle",
    # Business terms
    "business", "businesses", "businessname", "businessnames", "biz", "bizname",
    # Organization terms
    "organization", "organizations", "organisation", "organisations", "org", "orgs", "orgname",
    # Firm & Agency
    "firm", "firms", "firmname", "agency", "agencies", "agencyname",
    # Corporate & Enterprise
    "enterprise", "enterprises", "enterprisename", "corporation", "corporations", "corp", "corpname",
    # Account & Client
    "account", "accounts", "accountname", "client", "clients", "clientname",
    # Customer, Vendor, Merchant
    "customer", "customers", "customername", "vendor", "vendors", "vendorname",
    "merchant", "merchants", "merchantname", "seller", "sellers", "sellername",
    # Store & Shop
    "store", "stores", "storename", "shop", "shops", "shopname",
    # Leads & Prospects
    "lead", "leads", "leadname", "prospect", "prospects", "prospectname",
    "target", "targets", "targetcompany", "targetname",
    # Generic entity & name
    "name", "names", "entity", "entityname", "title", "legalname", "tradename",
    "dba", "doingbusinessas", "employer", "institute", "institution", "startup",
}

WEBSITE_SYNONYMS = {
    # Direct website terms
    "website", "websites", "websiteurl", "websitesurl", "web", "webaddress", "webaddresses",
    # URL terms
    "url", "urls", "urllink", "urllinks", "weburl", "weburls",
    # Link terms
    "link", "links", "hyperlink", "hyperlinks", "sitelink", "sitelinks",
    # Domain terms
    "domain", "domains", "domainname", "domainnames",
    # Site terms
    "site", "sites", "siteurl", "siteurls", "homepage", "homepages", "webpage", "webpages",
    # Company URL terms
    "companywebsite", "companyurl", "companydomain", "companyweb", "companylink",
    "businesswebsite", "businessurl", "businessdomain", "businessweb",
    "brandwebsite", "brandurl", "branddomain", "brandlink",
    # Online store & Page
    "onlinestore", "storeurl", "storewebsite", "shopurl", "shopwebsite",
    "page", "pages", "pageurl", "href", "http", "https", "internet",
}


def _looks_like_url(value: str) -> bool:
    """Check if a string resembles a web address or domain."""
    value = (value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return False
    if value.startswith(("http://", "https://", "www.")):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?", value))


def _normal_header(value: Any) -> str:
    """Normalize column header into clean lowercase alphanumeric."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _clean_brand_from_title(title: str, domain: str = "") -> str:
    """Extract a clean company or brand name from page title or domain."""
    if not title:
        # derive from domain (e.g. acme-corp.com -> Acme Corp)
        if domain:
            base = domain.lower().removeprefix("www.").split(".")[0]
            parts = [p.capitalize() for p in re.split(r"[-_.]+", base) if p]
            return " ".join(parts)
        return ""
    # Strip common title noise like " | Official Website", " - Home", " : Buy Online"
    cleaned = re.split(r"\s*[\-|–—:•·]\s*", title)[0].strip()
    for noise in ("Home", "Welcome", "Official Site", "Official Website", "Homepage"):
        if cleaned.lower() == noise.lower():
            # Try second part if available
            parts = re.split(r"\s*[\-|–—:•·]\s*", title)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
    return cleaned[:100]


def _match_column_synonyms(headers: list[str]) -> tuple[int | None, int | None]:
    """Find company and website column indices using extensive synonyms."""
    norm_headers = [_normal_header(h) for h in headers]
    company_idx: int | None = None
    website_idx: int | None = None

    # Step 1: Exact Synonym Match
    for i, norm in enumerate(norm_headers):
        if company_idx is None and norm in COMPANY_SYNONYMS:
            company_idx = i
        if website_idx is None and norm in WEBSITE_SYNONYMS:
            website_idx = i

    # Step 2: Substring / Compound Keyword Match if not found
    if company_idx is None:
        for i, norm in enumerate(norm_headers):
            if i != website_idx:
                if any(k in norm for k in ("company", "brand", "business", "client", "org", "firm")):
                    company_idx = i
                    break

    if website_idx is None:
        for i, norm in enumerate(norm_headers):
            if i != company_idx:
                if any(k in norm for k in ("website", "url", "domain", "web", "site", "link")):
                    website_idx = i
                    break

    return company_idx, website_idx


def _detect_columns_by_content(rows: list[list[str]]) -> tuple[int | None, int | None]:
    """Heuristic content analysis to identify website and company columns if headers fail."""
    if not rows:
        return None, None
    num_cols = max(len(r) for r in rows)
    if num_cols == 1:
        return 0, None  # Single column handles both URLs and company names

    sample_rows = rows[:30]
    col_url_counts = [0] * num_cols
    col_text_counts = [0] * num_cols

    for row in sample_rows:
        for c in range(min(num_cols, len(row))):
            val = row[c].strip()
            if not val:
                continue
            if _looks_like_url(val):
                col_url_counts[c] += 1
            elif len(val) >= 2 and not val.replace(".", "").isdigit():
                col_text_counts[c] += 1

    # Website column is the one with highest URL ratio
    website_idx = None
    best_url_count = 0
    for c in range(num_cols):
        if col_url_counts[c] >= max(2, len(sample_rows) * 0.25):
            if col_url_counts[c] > best_url_count:
                best_url_count = col_url_counts[c]
                website_idx = c

    # Company column is the one with highest text count (excluding website col)
    company_idx = None
    best_text_count = 0
    for c in range(num_cols):
        if c != website_idx and col_text_counts[c] > best_text_count:
            best_text_count = col_text_counts[c]
            company_idx = c

    return company_idx, website_idx


def _parse_sheet(data: bytes, filename: str) -> list[dict[str, str]]:
    """Parse CSV, TSV, TXT, XLSX, XLSM into normalized company and website items."""
    ext = os.path.splitext(filename.lower())[1]
    raw_rows: list[list[str]] = []

    if ext in (".xlsx", ".xlsm", ".xltx"):
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = workbook.active
        for row in sheet.iter_rows(values_only=True):
            raw_rows.append(["" if v is None else str(v).strip() for v in row])
        workbook.close()
    elif ext in (".csv", ".tsv", ".txt", ".tab"):
        # Try multiple text encodings
        text = ""
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                text = data.decode(enc)
                break
            except Exception:
                continue
        if not text:
            text = data.decode("utf-8", errors="replace")

        try:
            dialect = csv.Sniffer().sniff(text[:16384], delimiters=",\t;|")
        except Exception:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        raw_rows = [[str(v).strip() for v in row] for row in reader]
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format '{ext}'. Please upload .csv, .tsv, .txt, .xlsx, or .xlsm.",
        )

    # Filter empty rows
    rows = [r for r in raw_rows if any(str(v).strip() for v in r)]
    if not rows:
        return []

    # Check headers
    first_row = rows[0]
    company_idx, website_idx = _match_column_synonyms(first_row)
    header_found = company_idx is not None or website_idx is not None

    if header_found:
        data_rows = rows[1:]
    else:
        # Heuristic detection on all rows
        company_idx, website_idx = _detect_columns_by_content(rows)
        data_rows = rows

    output: list[dict[str, str]] = []
    for row in data_rows:
        def cell(idx: int | None) -> str:
            if idx is not None and 0 <= idx < len(row):
                return str(row[idx] or "").strip()
            return ""

        comp = cell(company_idx)
        site = cell(website_idx)

        # Single column or ambiguous data handling
        if not site and _looks_like_url(comp):
            site = comp
            comp = ""
        elif not comp and not _looks_like_url(site):
            comp = site
            site = ""

        if not comp and not site:
            continue

        output.append({"company": comp, "website": site})
        if len(output) >= MAX_ROWS:
            break

    return output


def probe_direct_domain(company_name: str) -> str | None:
    """Fast direct domain probing for brand names before resorting to search engines."""
    cleaned = re.sub(r"(?i)\b(inc|llc|ltd|limited|pvt|private|technologies|solutions|corp|corporation|co)\b", "", company_name)
    slug = re.sub(r"[^a-z0-9]", "", cleaned.lower())
    if len(slug) < 3:
        return None

    candidates = [
        f"https://{slug}.com",
        f"https://www.{slug}.com",
        f"https://{slug}.in",
        f"https://{slug}.co",
        f"https://{slug}.io",
    ]
    for url in candidates:
        try:
            import requests
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3.0, allow_redirects=True)
            if resp.status_code == 200 and resp.url:
                final = resp.url.rstrip("/")
                # Check that it didn't redirect to a generic parking page
                host = (urlparse(final).hostname or "").lower()
                if slug in host and not any(p in host for p in ("sedo", "godaddy", "domainmarket", "dan.com", "namecheap")):
                    logger.info("Direct domain probe hit for %s -> %s", company_name, final)
                    return final
        except Exception:
            continue
    return None


def _search_company_website(company: str) -> tuple[str | None, str]:
    """Robust company-to-website resolver via direct domain probing, Brave Search API, or DuckDuckGo."""
    company = company.strip()
    if not company:
        return None, "Empty company name provided."

    # Step 1: Direct Domain Probe (e.g. "doordripp" -> https://doordripp.com)
    probed = probe_direct_domain(company)
    if probed:
        return probed, "Website resolved via direct brand domain verification."

    # Step 2: Search Engine Discovery
    candidates: list[tuple[str, str, str]] = []

    brave_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if brave_key:
        try:
            import requests
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": f'"{company}" official website', "count": 10},
                headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
                timeout=8,
            )
            if resp.ok:
                results = resp.json().get("web", {}).get("results", [])
                candidates = [(r.get("url", ""), r.get("title", ""), r.get("description", "")) for r in results]
        except Exception as e:
            logger.debug("Brave search error: %s", e)

    if not candidates:
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = ddgs.text(f'"{company}" official website', max_results=10)
            candidates = [
                (r.get("href") or r.get("url") or "", r.get("title", ""), r.get("body", ""))
                for r in (results or [])
            ]
        except Exception as exc:
            return None, f"Search service unavailable ({exc.__class__.__name__}). Enter a direct website URL."

    blocked_domains = (
        "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "wikipedia.org",
        "justdial.com", "indiamart.com", "sulekha.com", "crunchbase.com", "glassdoor.com",
        "ambitionbox.com", "twitter.com", "x.com", "reddit.com", "zoominfo.com",
        "mapquest.com", "yelp.com", "bbb.org", "yellowpages.com"
    )

    company_tokens = [
        t for t in re.findall(r"[a-z0-9]+", company.lower())
        if len(t) > 2 and t not in {"the", "and", "for", "inc", "llc", "ltd", "limited", "pvt", "private", "co"}
    ]

    best_url: str | None = None
    best_score = -999

    for raw_url, title, snippet in candidates:
        url = normalize_input_url(raw_url)
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if any(b in host for b in blocked_domains):
            continue

        haystack = f"{host} {title} {snippet}".lower()
        score = 0

        for token in company_tokens:
            if token in host:
                score += 5
            elif token in haystack:
                score += 1

        if "official" in haystack:
            score += 2
        if any(term in host for term in ("directory", "review", "jobs", "careers", "wiki", "forum")):
            score -= 5

        # If search returned a subpath like /login, /delivery, /app, strip to root origin!
        parsed = urlparse(url)
        path = parsed.path.rstrip("/").lower()
        if path in ("/login", "/signin", "/sign-in", "/delivery", "/app", "/portal", "/cart", "/checkout") or any(path.startswith(p) for p in ("/login", "/signin", "/app/")):
            url = f"{parsed.scheme}://{parsed.netloc}"

        if score > best_score:
            best_score = score
            best_url = url

    if best_url and best_score >= 1:
        return best_url, "Website resolved automatically via search."
    return None, "No official website found. Provide the website domain or URL directly."


def _process_one(item: dict[str, str], enable_js_fallback: bool = True) -> dict[str, Any]:
    """Process a single target: prioritizes direct URL from CSV, uses direct domain probing and multi-layered discovery."""
    company = (item.get("company") or "").strip()
    website_input = (item.get("website") or "").strip()
    search_note = ""

    # RULE 1: If both are mentioned in CSV/input, ALWAYS PREFER the direct URL!
    if website_input:
        # Check if columns were swapped
        if not _looks_like_url(website_input) and _looks_like_url(company):
            website_input, company = company, website_input
    elif company and _looks_like_url(company):
        website_input = company
        company = ""

    # RULE 2: If website is still missing, search or probe via company name
    if not website_input and company:
        website_input, search_note = _search_company_website(company)

    # Clean the URL (strips dead /login or /delivery subpaths to root origin)
    normalized = normalize_input_url(website_input or "")
    if normalized:
        parsed = urlparse(normalized)
        path = parsed.path.rstrip("/").lower()
        if path in ("/login", "/signin", "/sign-in", "/delivery", "/app", "/portal", "/cart", "/checkout") or any(path.startswith(p) for p in ("/login", "/signin", "/app/")):
            normalized = f"{parsed.scheme}://{parsed.netloc}"

    if not normalized:
        return {
            "company": company or website_input or "Unknown",
            "website": website_input,
            "site_title": "",
            "emails": [],
            "phones": [],
            "socials": {},
            "pages_visited": 0,
            "source_urls": [],
            "status": "website_not_found" if not website_input else "invalid_url",
            "error": search_note or "Could not identify a valid website URL.",
            "js_rendered": False,
        }

    # Perform multi-layered scraping (Static -> Playwright JS -> External Search)
    result = scrape_website(
        normalized,
        enable_js_fallback=enable_js_fallback,
        company_name=company or _clean_brand_from_title(normalized),
    )

    # If company name was missing, derive it from title or domain
    if not company:
        host = (urlparse(normalized).hostname or "").removeprefix("www.")
        company = _clean_brand_from_title(result.get("site_title", ""), host)

    result["company"] = company or result.get("site_title") or normalized
    result["website"] = normalized
    if search_note:
        result["search_note"] = search_note

    return result


def _run_job(job_id: str, rows: list[dict[str, str]], enable_js: bool = True) -> None:
    """Execute background scan job across rows with polite concurrency."""
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["message"] = "Scanning pages for business emails, phones, and social profiles..."

    results: list[dict[str, Any] | None] = [None] * len(rows)
    try:
        # Use 4 parallel workers for polite scraping
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="crawler") as pool:
            future_map = {pool.submit(_process_one, row, enable_js): i for i, row in enumerate(rows)}
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    res = future.result()
                except Exception as exc:
                    logger.exception("Error processing row %s: %s", idx, exc)
                    res = {
                        "company": rows[idx].get("company", ""),
                        "website": rows[idx].get("website", ""),
                        "site_title": "",
                        "emails": [],
                        "phones": [],
                        "socials": {},
                        "pages_visited": 0,
                        "source_urls": [],
                        "status": "error",
                        "error": f"Error: {exc.__class__.__name__}",
                        "js_rendered": False,
                    }
                results[idx] = res
                with jobs_lock:
                    jobs[job_id]["completed"] += 1
                    jobs[job_id]["results"] = [r for r in results if r is not None]

        with jobs_lock:
            final_res = [r for r in results if r is not None]
            jobs[job_id]["results"] = final_res
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["message"] = f"Finished scanning {len(final_res)} companies."
            jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["message"] = f"Scan job failed ({exc.__class__.__name__})."


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "total": job["total"],
        "completed": job["completed"],
        "message": job["message"],
        "results": job.get("results", []),
    }


@app.get("/")
def index():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "app": "Company Contact Finder", "version": "2.0.0", "js_fallback": True}


@app.get("/api/search")
def instant_search(query: str = Query(..., description="Company name or Website URL")):
    """Instant single search by company name or website URL."""
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")
    if _looks_like_url(query):
        item = {"company": "", "website": query}
    else:
        item = {"company": query, "website": ""}
    result = _process_one(item, enable_js_fallback=True)
    return result


@app.post("/api/jobs")
async def create_job(
    items_json: str = Form(default="[]"),
    enable_js: bool = Form(default=True),
    file: UploadFile | None = File(default=None),
):
    """Create a new batch contact extraction job."""
    rows: list[dict[str, str]] = []

    # 1. Parse items_json
    if items_json and items_json != "[]":
        try:
            raw = json.loads(items_json)
            if isinstance(raw, str):
                raw = raw.splitlines()
            for entry in raw:
                if isinstance(entry, str):
                    val = entry.strip()
                    if val:
                        if _looks_like_url(val):
                            rows.append({"company": "", "website": val})
                        else:
                            rows.append({"company": val, "website": ""})
                elif isinstance(entry, dict):
                    comp = str(entry.get("company") or entry.get("name") or entry.get("brand") or "").strip()
                    site = str(entry.get("website") or entry.get("url") or entry.get("domain") or "").strip()
                    if comp or site:
                        rows.append({"company": comp, "website": site})
        except Exception:
            raise HTTPException(status_code=400, detail="Could not parse input list JSON.")

    # 2. Parse uploaded file
    if file and file.filename:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"File exceeds maximum upload size of {MAX_UPLOAD_BYTES // (1024*1024)} MB.")
        rows.extend(_parse_sheet(data, file.filename))

    # De-duplicate entries
    deduped: list[dict[str, str]] = []
    seen = set()
    for row in rows:
        key = ((row.get("company") or "").strip().lower(), (row.get("website") or "").strip().lower())
        if key not in seen and any(key):
            deduped.append(row)
            seen.add(key)
    rows = deduped

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="Enter at least one company name or website, or upload a spreadsheet file.",
        )

    if len(rows) > MAX_ROWS:
        rows = rows[:MAX_ROWS]

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",
        "total": len(rows),
        "completed": 0,
        "message": "Queued for scanning.",
        "results": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "enable_js": enable_js,
    }
    with jobs_lock:
        jobs[job_id] = job

    job_pool.submit(_run_job, job_id, rows, enable_js)
    return {"id": job_id, "total": len(rows), "status": "queued"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """Retrieve scan job status and results."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found or expired.")
        return _job_snapshot(job)


def _flatten(result: dict[str, Any]) -> dict[str, Any]:
    socials = result.get("socials", {}) or {}
    return {
        "Company / Brand": result.get("company", ""),
        "Website": result.get("website", ""),
        "Page Title": result.get("site_title", ""),
        "Emails": "; ".join(result.get("emails", []) or []),
        "Phone Numbers": "; ".join(result.get("phones", []) or []),
        "LinkedIn": socials.get("linkedin", ""),
        "Twitter / X": socials.get("twitter", ""),
        "Facebook": socials.get("facebook", ""),
        "Instagram": socials.get("instagram", ""),
        "YouTube": socials.get("youtube", ""),
        "GitHub": socials.get("github", ""),
        "JS Rendered": "Yes" if result.get("js_rendered") else "No",
        "Pages Visited": result.get("pages_visited", 0),
        "Status": result.get("status", ""),
        "Notes": result.get("error", "") or result.get("search_note", ""),
        "Source URLs": "; ".join(result.get("source_urls", []) or []),
    }


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str, format: str = "csv"):
    """Export scan results to CSV or professionally formatted Excel (.xlsx)."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        results = list(job.get("results", []))

    if not results:
        raise HTTPException(status_code=409, detail="No scan results available yet.")

    flat_rows = [_flatten(r) for r in results]
    headers = list(flat_rows[0].keys())

    if format.lower() == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Business Contacts"
        sheet.append(headers)

        for item in flat_rows:
            sheet.append([item.get(h, "") for h in headers])

        # Style Header Row
        header_fill = PatternFill("solid", fgColor="0F172A")
        header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", horizontal="left")
        sheet.row_dimensions[1].height = 26

        # Column widths & wrapping
        widths = [26, 32, 28, 38, 28, 30, 24, 24, 24, 24, 24, 14, 14, 18, 40, 60]
        for i, width in enumerate(widths, start=1):
            sheet.column_dimensions[chr(64 + i) if i <= 26 else f"A{chr(64 + i - 26)}"].width = width

        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="company-contacts-{job_id}.xlsx"'},
        )

    # CSV Export
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    writer.writerows(flat_rows)
    return StreamingResponse(
        iter(["\ufeff" + buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="company-contacts-{job_id}.csv"'},
    )
