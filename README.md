# Company Contact Finder

A small local web app to find publicly listed business emails and phone numbers on company websites. It supports direct website URLs, company-name lookup, bulk pasted lists, and CSV/XLSX uploads.

## Features

- Simple HTML frontend served locally by FastAPI.
- Direct scan by domain/URL or best-effort company-name → official website search.
- CSV, TSV, TXT, XLSX and XLSM upload; recognizes `Company`/`Company Name` and `Website`/`URL`/`Domain` columns.
- Scans a limited number of same-host pages, prioritizing contact/about/support pages.
- Extracts public email addresses, `mailto:` links, telephone links, and likely phone numbers.
- Job progress, status notes, source pages, and CSV/Excel export.
- Checks `robots.txt`, uses request timeouts, caps page download size, and rate-limits requests per domain.

## Run on Windows

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/). During install, enable **Add Python to PATH**.
2. Extract this folder somewhere convenient, then open PowerShell in the folder.
3. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, use this command just for the current terminal, or run the app with `.\.venv\Scripts\python.exe` directly:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\.venv\Scripts\Activate.ps1
   ```

4. Install packages:

   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

5. Start the app:

   ```powershell
   python -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```

6. Open **http://127.0.0.1:8000** in your browser. Keep the terminal open while using it. Press `Ctrl+C` to stop.

## Input examples

Paste a URL or company name, one per line:

```text
https://example.org
DrinkPrime
A company with spaces
```

For a spreadsheet, a useful format is:

| Company | Website |
|---|---|
| Example Org | https://example.org |
| DrinkPrime | https://drinkprime.in |
| Another Brand | |

A website URL is preferred. When only a company name is provided, the app uses public search results to guess its official website. That match may be wrong for ambiguous names; inspect the result before use. The default DDGS search can be rate-limited. For more reliable name lookups, optionally set a Brave Search API key in your environment before launching the app:

```powershell
$env:BRAVE_SEARCH_API_KEY="YOUR_KEY"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Use the API key according to your provider's terms. The app does not require a search API key when scanning URLs directly.

## Result status meanings

- `found`: one or more email addresses or phone numbers were detected.
- `no_contacts_found`: pages loaded, but no contact values were detected.
- `no_pages_scanned`: no pages could be scanned (may be robots restrictions, timeouts, or site blocking).
- `website_not_found`: a company name did not resolve to a likely website.
- `invalid_url` / `error`: input or request issue; see Notes.

## Important limitations

- This is a best-effort public webpage parser, not a paid contact database. Some sites hide details behind JavaScript, render them only after interaction, use images, or block automated requests; such contacts may not be detected.
- Company-name lookups can select an incorrect or unofficial domain. Verify the domain and source URLs before using results.
- It deliberately does not bypass logins, captchas, robots restrictions, or access controls. It scans up to seven pages per site and limits page size and request frequency.
- Use contact details only for relevant, lawful business communication. Follow applicable privacy/anti-spam laws and honor opt-outs.
- Job history is kept in memory while the app runs; restarting the app clears jobs/results.

## Project files

- `app.py`: FastAPI endpoints, spreadsheet parsing, jobs, exports.
- `scraper.py`: polite website crawler and public contact extractor.
- `static/index.html`: frontend UI.
- `requirements.txt`: Python dependencies.
- `.env.example`: optional search-provider environment setting.
