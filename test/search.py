# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
import requests
from bs4 import BeautifulSoup
import time
import sqlite3
from urllib.parse import urljoin, urlparse
import re

DB_FILE = "jobs.db"
USER_AGENT = "Mozilla/5.0 (JobCrawler/1.0; +https://example.com/bot)"

app = FastAPI(title="Jobinja Crawler (requests + BS4 + SQLite)")

# ---------- DB helpers ----------
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_title TEXT,
        category TEXT,
        min_education TEXT,
        location TEXT,
        work_type TEXT,
        skills TEXT,      -- json-like comma separated
        url TEXT UNIQUE,
        fetched_at INTEGER
    )
    """)
    con.commit()
    con.close()

def clear_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM jobs")
    con.commit()
    con.close()

def save_job(record: dict):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO jobs (job_title, category, min_education, location, work_type, skills, url, fetched_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record.get("job_title"),
        record.get("category"),
        record.get("min_education"),
        record.get("location"),
        record.get("work_type"),
        ",".join(record.get("skills", [])),
        record.get("url"),
        int(time.time())
    ))
    con.commit()
    con.close()

def count_jobs():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM jobs")
    n = cur.fetchone()[0]
    con.close()
    return n

def list_jobs(limit: Optional[int] = 100):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT id, job_title, category, min_education, location, work_type, skills, url, fetched_at FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    con.close()
    res = []
    for r in rows:
        res.append({
            "id": r[0],
            "job_title": r[1],
            "category": r[2],
            "min_education": r[3],
            "location": r[4],
            "work_type": r[5],
            "skills": r[6].split(",") if r[6] else [],
            "url": r[7],
            "fetched_at": r[8]
        })
    return res

# ---------- scraping helpers ----------
def get_soup(url: str, timeout=10):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def extract_job_links_from_list_page(list_url: str) -> List[str]:
    """
    پیدا کردن لینک آگهی‌ها از صفحه لیست.
    سلکتور اصلی: a.c-jobListView__titleLink
    """
    soup = get_soup(list_url)
    links = []
    # اصلی: لینک‌های عنوان
    for a in soup.select("a.c-jobListView__titleLink"):
        href = a.get("href")
        if href:
            links.append(urljoin(list_url, href))
    # fallback: item container anchor
    if not links:
        for a in soup.select("li.o-listView__item a"):
            href = a.get("href")
            if href:
                links.append(urljoin(list_url, href))
    # dedupe while preserving order
    seen = set()
    out = []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out

def extract_details_from_job_page(job_url: str) -> dict:
    soup = get_soup(job_url)
    # Title
    title = None
    # common candidates
    title_selectors = [
        "h1.c-jobView__title", "h1", "h2.c-jobView__title", "h2.o-jobView__title",
        "h2.o-listView__itemTitle", "h1[itemprop='title']"
    ]
    for sel in title_selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            break

    # Category (breadcrumb or category block)
    category = None
    cat_candidates = soup.select(".c-jobView__breadcrumb a, .c-jobView__category, .c-jobView__meta a")
    if cat_candidates:
        texts = [c.get_text(strip=True) for c in cat_candidates if c.get_text(strip=True)]
        if texts:
            category = " > ".join(texts[:3])

    # Location and work_type extraction (search meta block)
    location = None
    work_type = None
    meta_items = soup.select("ul.o-listView__itemComplementInfo li, ul.c-jobListView__meta li, div.c-jobView__meta li")
    if meta_items:
        for li in meta_items:
            txt = li.get_text(" ", strip=True)
            if re.search(r"\b(تهران|اصفهان|شیراز|مشهد|کرج|ساری|رشت|تبریز)\b", txt):
                location = txt
            if re.search(r"(تمام‌وقت|پاره‌وقت|پاره وقت|پاره‌وقت|فریلنس|ساعتی|پاره)", txt):
                work_type = txt

    # min_education: look for header containing 'تحصیل' or 'تحصیلات'
    min_education = None
    info_items = soup.select("li.c-infoBox__item")
    for item in info_items:
        h4 = item.select_one("h4.c-infoBox__itemTitle")
        if h4 and "تحصیل" in h4.get_text():
            # find spans or text
            txt = item.get_text(" ", strip=True)
            min_education = txt.replace(h4.get_text(strip=True), "").strip()
            break
        # also check title containing 'تحصیلات' or 'حداقل مدرک'
        if h4 and ("مدرک" in h4.get_text() or "تحصیلات" in h4.get_text()):
            txt = item.get_text(" ", strip=True)
            min_education = txt.replace(h4.get_text(strip=True), "").strip()
            break

    # skills: find the li that has h4 contains 'مهارت' then div.tags span
    skills = []
    for item in info_items:
        h4 = item.select_one("h4.c-infoBox__itemTitle")
        if h4 and "مهارت" in h4.get_text():
            tags_div = item.select_one("div.tags")
            if tags_div:
                spans = tags_div.select("span")
                for sp in spans:
                    t = sp.get_text(strip=True)
                    if t:
                        skills.append(t)
            else:
                # fallback: split text
                txt = item.get_text(" ", strip=True)
                parts = re.split(r"[،,;•\-]", txt)
                for p in parts:
                    p = p.strip()
                    if p and len(p) < 80:
                        skills.append(p)
            break

    # final fallback: try to find known tech keywords anywhere in page text
    if not skills:
        body = soup.get_text(" ").lower()
        keywords = ["python","django","docker","react","vue","javascript","sql","mysql","postgres","linux","office","microsoft office","پشتیبانی"]
        found = []
        for kw in keywords:
            if kw in body and kw not in found:
                found.append(kw)
        skills = found

    # normalize results
    title = title or ""
    category = category or ""
    min_education = min_education or ""
    location = location or ""
    work_type = work_type or ""
    skills = [s.strip() for s in skills if s and len(s) < 200]

    return {
        "job_title": title,
        "category": category,
        "min_education": min_education,
        "location": location,
        "work_type": work_type,
        "skills": skills,
        "url": job_url
    }

# ---------- API models ----------
class CrawlRequest(BaseModel):
    start_url: HttpUrl
    max_jobs: Optional[int] = 30
    delay: Optional[float] = 0.8  # seconds between requests

# ---------- endpoints ----------
@app.post("/crawl")
def crawl(req: CrawlRequest):
    init_db()
    # clear existing data as requested
    clear_db()

    start_url = str(req.start_url)
    max_jobs = int(req.max_jobs or 30)
    delay = float(req.delay or 0.8)

    # validate start_url is a job list page (simple)
    parsed = urlparse(start_url)
    if "jobinja.ir" not in parsed.netloc:
        raise HTTPException(status_code=400, detail="only jobinja.ir domain supported")

    collected = 0
    page_url = start_url
    visited_job_urls = set()

    # We'll iterate pages until we reach max_jobs or no more pages
    current_page = 1
    while collected < max_jobs:
        try:
            list_links = extract_job_links_from_list_page(page_url)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to fetch list page: {e}")

        if not list_links:
            break

        for job_link in list_links:
            if collected >= max_jobs:
                break
            if job_link in visited_job_urls:
                continue
            visited_job_urls.add(job_link)
            try:
                details = extract_details_from_job_page(job_link)
                save_job(details)
                collected += 1
                print(f"[{collected}] saved: {details.get('job_title')}")
            except Exception as e:
                print(f"failed to fetch job {job_link}: {e}")
            time.sleep(delay)

        # try to find next page — heuristic: replace page=X param or increment page number
        # if start_url contains page=, increment it; otherwise try to follow a next link
        parsed_q = dict([p.split("=") for p in parsed.query.split("&") if "=" in p]) if parsed.query else {}
        if "page" in parsed_q:
            # increment page number
            current_page += 1
            new_query = re.sub(r"page=\d+", f"page={current_page}", parsed.query)
            page_url = parsed._replace(query=new_query).geturl()
            # to avoid infinite loop, if no new links were found, break
        else:
            # try to find a next link on the page
            try:
                soup = get_soup(page_url)
                next_a = soup.select_one("a.c-pagination__next, a[rel='next']")
                if next_a and next_a.get("href"):
                    page_url = urljoin(page_url, next_a.get("href"))
                else:
                    break
            except Exception:
                break

    return {"ok": True, "saved": collected, "db_count": count_jobs()}

@app.get("/jobs")
def get_jobs(limit: Optional[int] = 100):
    init_db()
    return {"ok": True, "count": count_jobs(), "jobs": list_jobs(limit)}

# ---------- start ----------
if __name__ == "__main__":
    init_db()
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
