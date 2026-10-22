# Company Contact Finder ⚡ Full Scale

A powerful, full-scale business contact discovery application designed to extract publicly listed emails, phone numbers, and social profiles directly from company websites. Features automated headless Chromium fallback for JavaScript-rendered Single Page Applications (React, Vue, Next.js), smart column synonym detection for spreadsheets, and instant search via company name OR website domain.

---

## 🚀 Key Features

- **Search via Name or Website**: Search using just a brand/company name (e.g. `Nike`, `Stripe`) or direct URL (e.g. `drinkprime.in`). If only a name is given, intelligent web search resolves the official website. If only a website is given, the brand name is automatically derived.
- **Headless JavaScript-Rendered Fallback (Playwright)**: When static scraping detects 0 contacts (common on React, Vue, Next.js, and client-side rendered websites), the engine automatically falls back to headless Chromium with stealth flags, scrolls the page, and extracts dynamically rendered content.
- **Extensive Column Synonym Matching**: Automatically detects CSV/Excel columns regardless of header variations:
  - **Company synonyms**: `Brand`, `Brand Name`, `Company`, `Business`, `Organization`, `Agency`, `Firm`, `Client`, `Vendor`, `Store`, `Shop`, `Lead`, `Target`, `Name`, etc.
  - **Website synonyms**: `URL`, `Website`, `Domain`, `Site`, `Link`, `Homepage`, `Web Address`, `Company URL`, `Online Store`, `Href`, etc.
  - **Smart Content Detection**: If headers are missing or unusual, column contents are automatically analyzed for domain and URL patterns.
- **Cloudflare & Obfuscation Decoding**:
  - Automatically decodes Cloudflare email protection (`/cdn-cgi/l-email-protection#...` and `data-cfemail`).
  - Decodes obfuscated text emails like `sales [at] domain [dot] com` or `support(at)company.com`.
  - Decodes HTML entities and obfuscated mailto links.
- **Social Media Profile Discovery**: Automatically discovers official profiles on LinkedIn, Twitter/X, Facebook, Instagram, YouTube, and GitHub.
- **Instant Search & Batch Scanning**:
  - **Instant Search**: Type a single company name or domain for instant discovery.
  - **Bulk Input**: Paste multi-line lists mixing domains and brand names.
  - **Spreadsheets**: Drag-and-drop CSV, TSV, TXT, XLSX, and XLSM files up to 25 MB and 2,500 rows.
- **Real-Time Interactive Dashboard**:
  - Live progress tracking and metric cards (Total Scanned, Contacts Found, Success Rate, JS Fallbacks Used).
  - Real-time client-side table search and filtering.
  - 1-click "Copy All Emails" and "Copy All Phones" buttons.
  - Export to professionally formatted Excel (`.xlsx`) and CSV.

---

## 🛠️ Quick Start (Windows)

### Option 1: Automatic Launcher
Double-click `run_windows.bat`. It will create `.venv`, install all packages, install the Playwright Chromium browser, and open the app in your default browser at `http://127.0.0.1:8000`.

### Option 2: PowerShell Manual Run

1. Open PowerShell in this directory:
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install Python dependencies:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. Install Playwright browser:
   ```powershell
   python -m playwright install chromium
   ```
4. Start the server:
   ```powershell
   python -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```
5. Open your browser at **http://127.0.0.1:8000**.

---

## 🐳 Docker Run

Build and run using Docker:
```bash
docker build -t company-contact-finder .
docker run -p 8000:8000 company-contact-finder
```

---

## 📋 Input & File Support

### 1. Single Input
Search instantly with either:
- A company name: `OpenAI`
- A domain: `drinkprime.in`
- A full URL: `https://stripe.com`

### 2. Multi-line Text Paste
Paste mixed entries directly into the bulk textarea:
```text
https://stripe.com
DrinkPrime
Nike
airbnb.com
```

### 3. Spreadsheets (CSV, TSV, XLSX, XLSM)
The parser automatically detects varied column naming conventions. Example supported layouts:

| Brand Name | URL |
|---|---|
| DrinkPrime | https://drinkprime.in |
| Razorpay | |
| | https://stripe.com |
| Anthropic | https://anthropic.com |

Or single-column target lists:

| Targets |
|---|
| stripe.com |
| OpenAI |
| https://github.com |

---

## 📊 Result Status Badges

- `Found`: Public email addresses or phone numbers discovered via static crawl.
- `Found (JS Fallback)`: Discovered using headless Chromium rendering when static crawl returned empty.
- `No Contacts`: Pages successfully crawled, but no public business contact details were listed.
- `Site Not Found`: Company name could not be resolved to an official website domain.
- `Error`: Request or timeout error.

---

## ⚖️ Responsible Crawling & Privacy

- Scans a bounded number of public pages per domain (prioritizing contact, about, and support pages).
- Respects `robots.txt` directives.
- Implements polite domain-level rate limiting and request timeouts.
- Designed strictly for lawful, legitimate business discovery and research. Always comply with relevant privacy, anti-spam, and data protection regulations.
