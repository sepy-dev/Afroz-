from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
import requests
from bs4 import BeautifulSoup
import time
import sqlite3
from urllib.parse import urljoin, urlparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_FILE = "jobs.db"
USER_AGENT = "Mozilla/5.0 (JobCrawler/1.0; +https://example.com/bot)"

app = FastAPI(title="Jobinja Crawler (requests + BS4 + SQLite + ThreadPool)")

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
        skills TEXT,
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
    soup = get_soup(list_url)
    links = []
    for a in soup.select("a.c-jobListView__titleLink"):
        href = a.get("href")
        if href:
            links.append(urljoin(list_url, href))
    seen = set()
    out = []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out

def extract_details_from_job_page(job_url: str) -> dict:
    soup = get_soup(job_url)
    # title
    title = None
    for sel in ["h1.c-jobView__title", "h1", "h2.c-jobView__title", "h2.o-jobView__title"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            break

    # category
    category = None
    cat_candidates = soup.select(".c-jobView__breadcrumb a, .c-jobView__category, .c-jobView__meta a")
    if cat_candidates:
        texts = [c.get_text(strip=True) for c in cat_candidates if c.get_text(strip=True)]
        if texts:
            category = " > ".join(texts[:3])

    # location, work_type
    location = None
    work_type = None
    meta_items = soup.select("ul.o-listView__itemComplementInfo li, ul.c-jobListView__meta li, div.c-jobView__meta li")
    if meta_items:
        for li in meta_items:
            txt = li.get_text(" ", strip=True)
            if re.search(r"\b(تهران|اصفهان|شیراز|مشهد|کرج|ساری|رشت|تبریز)\b", txt):
                location = txt
            if re.search(r"(تمام‌وقت|پاره‌وقت|پاره وقت|فریلنس|ساعتی|پاره)", txt):
                work_type = txt

    # min_education
    min_education = None
    info_items = soup.select("li.c-infoBox__item")
    for item in info_items:
        h4 = item.select_one("h4.c-infoBox__itemTitle")
        if h4 and ("تحصیل" in h4.get_text() or "مدرک" in h4.get_text() or "تحصیلات" in h4.get_text()):
            txt = item.get_text(" ", strip=True)
            min_education = txt.replace(h4.get_text(strip=True), "").strip()
            break

    # skills
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
            break

    return {
        "job_title": title or "",
        "category": category or "",
        "min_education": min_education or "",
        "location": location or "",
        "work_type": work_type or "",
        "skills": skills,
        "url": job_url
    }

# ---------- API models ----------
class CrawlRequest(BaseModel):
    start_url: HttpUrl
    max_jobs: Optional[int] = 30
    delay: Optional[float] = 0.1  # کاهش تاخیر

@app.post("/crawl")
def crawl(req: CrawlRequest):
    init_db()
    clear_db()

    start_url = str(req.start_url)
    max_jobs = int(req.max_jobs or 30)
    delay = float(req.delay or 0.1)

    parsed = urlparse(start_url)
    if "jobinja.ir" not in parsed.netloc:
        raise HTTPException(status_code=400, detail="only jobinja.ir domain supported")

    collected = 0
    page_url = start_url
    visited_job_urls = set()
    current_page = 1

    with ThreadPoolExecutor(max_workers=10) as executor:
        while collected < max_jobs:
            try:
                list_links = extract_job_links_from_list_page(page_url)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"failed to fetch list page: {e}")

            if not list_links:
                break

            # فقط آگهی‌های جدید
            new_links = [l for l in list_links if l not in visited_job_urls]

            # محدود کردن به مابقی مورد نیاز
            remaining = max_jobs - collected
            to_process = new_links[:remaining]

            futures = {executor.submit(extract_details_from_job_page, url): url for url in to_process}

            for future in as_completed(futures):
                job_url = futures[future]
                try:
                    details = future.result()
                    save_job(details)
                    collected += 1
                    print(f"[{collected}] saved: {details.get('job_title')}")
                except Exception as e:
                    print(f"failed to fetch job {job_url}: {e}")

            visited_job_urls.update(to_process)

            if collected >= max_jobs:
                break

            # ساخت لینک صفحه بعد
            parsed_q = dict([p.split("=") for p in parsed.query.split("&") if "=" in p]) if parsed.query else {}
            if "page" in parsed_q:
                current_page += 1
                new_query = re.sub(r"page=\d+", f"page={current_page}", parsed.query)
                page_url = parsed._replace(query=new_query).geturl()
            else:
                try:
                    soup = get_soup(page_url)
                    next_a = soup.select_one("a.c-pagination__next, a[rel='next']")
                    if next_a and next_a.get("href"):
                        page_url = urljoin(page_url, next_a.get("href"))
                    else:
                        break
                except Exception:
                    break

            if delay > 0:
                time.sleep(delay)

    return {"ok": True, "saved": collected, "db_count": count_jobs()}

@app.get("/jobs")
def get_jobs(limit: Optional[int] = 100):
    init_db()
    return {"ok": True, "count": count_jobs(), "jobs": list_jobs(limit)}

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
