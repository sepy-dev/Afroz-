from fastapi import FastAPI, HTTPException, Query
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

app = FastAPI(title="Jobinja Crawler + Recommendations (SQLite)")

# ---------- DB helpers ----------
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_title TEXT,
        min_education TEXT,
        location TEXT,
        work_type TEXT,
        skills TEXT,
        url TEXT UNIQUE,
        fetched_at INTEGER
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        parent_id INTEGER DEFAULT NULL,
        FOREIGN KEY(parent_id) REFERENCES categories(id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS job_categories (
        job_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        PRIMARY KEY(job_id, category_id),
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
        FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
    )
    """)
    con.commit()
    con.close()

def clear_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM job_categories")
    cur.execute("DELETE FROM categories")
    cur.execute("DELETE FROM jobs")
    con.commit()
    con.close()

def get_or_create_category(con, category_name, parent_id=None):
    cur = con.cursor()
    cur.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO categories (name, parent_id) VALUES (?, ?)", (category_name, parent_id))
    con.commit()
    return cur.lastrowid

def save_job_with_categories(record: dict):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO jobs (job_title, min_education, location, work_type, skills, url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        record.get("job_title"),
        record.get("min_education"),
        record.get("location"),
        record.get("work_type"),
        ",".join(record.get("skills", [])),
        record.get("url"),
        int(time.time())
    ))
    con.commit()

    cur.execute("SELECT id FROM jobs WHERE url = ?", (record.get("url"),))
    job_id = cur.fetchone()[0]

    categories_str = record.get("category", "")
    categories = [c.strip() for c in re.split(r"[>,،]", categories_str) if c.strip()]

    parent_id = None
    for cat_name in categories:
        cat_id = get_or_create_category(con, cat_name, parent_id)
        cur.execute("INSERT OR IGNORE INTO job_categories (job_id, category_id) VALUES (?, ?)", (job_id, cat_id))
        parent_id = cat_id

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
    cur.execute("""
        SELECT id, job_title, min_education, location, work_type, skills, url, fetched_at
        FROM jobs ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall()

    jobs = []
    for r in rows:
        job_id = r[0]
        cur.execute("""
            SELECT c.name FROM categories c
            JOIN job_categories jc ON c.id = jc.category_id
            WHERE jc.job_id = ?
        """, (job_id,))
        cats = [row[0] for row in cur.fetchall()]

        jobs.append({
            "id": job_id,
            "job_title": r[1],
            "min_education": r[2],
            "location": r[3],
            "work_type": r[4],
            "skills": r[5].split(",") if r[5] else [],
            "url": r[6],
            "fetched_at": r[7],
            "categories": cats
        })
    con.close()
    return jobs

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

    title = None
    for sel in ["h1.c-jobView__title", "h1", "h2.c-jobView__title", "h2.o-jobView__title"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            break

    category = ""
    info_items = soup.select("ul.c-jobView__firstInfoBox.c-infoBox > li.c-infoBox__item")
    for item in info_items:
        h4 = item.select_one("h4.c-infoBox__itemTitle")
        if h4 and "دسته‌بندی شغلی" in h4.get_text():
            tags_div = item.select_one("div.tags")
            if tags_div:
                spans = tags_div.select("span.black")
                categories = [sp.get_text(strip=True) for sp in spans if sp.get_text(strip=True)]
                category = " > ".join(categories)
            break

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

    min_education = None
    info_items = soup.select("li.c-infoBox__item")
    for item in info_items:
        h4 = item.select_one("h4.c-infoBox__itemTitle")
        if h4 and ("تحصیل" in h4.get_text() or "مدرک" in h4.get_text() or "تحصیلات" in h4.get_text()):
            txt = item.get_text(" ", strip=True)
            min_education = txt.replace(h4.get_text(strip=True), "").strip()
            break

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
    delay: Optional[float] = 0.1

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

            new_links = [l for l in list_links if l not in visited_job_urls]
            remaining = max_jobs - collected
            to_process = new_links[:remaining]

            futures = {executor.submit(extract_details_from_job_page, url): url for url in to_process}

            for future in as_completed(futures):
                job_url = futures[future]
                try:
                    details = future.result()
                    save_job_with_categories(details)
                    collected += 1
                    print(f"[{collected}] saved: {details.get('job_title')}")
                except Exception as e:
                    print(f"failed to fetch job {job_url}: {e}")

            visited_job_urls.update(to_process)

            if collected >= max_jobs:
                break

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

@app.get("/recommendations")
def get_recommendations(skills: List[str] = Query(..., description="لیست مهارت‌ها")):
    """
    مثال درخواست:
    /recommendations?skills=python&skills=sql&skills=fastapi
    """
    init_db()
    jobs = list_jobs(500)

    results = []
    for job in jobs:
        job_skills = set([s.strip().lower() for s in job["skills"]])
        user_skills = set([s.strip().lower() for s in skills])

        if not job_skills:
            match_percent = 0
        else:
            match_percent = int((len(job_skills & user_skills) / len(job_skills)) * 100)

        results.append({
            "job_title": job["job_title"],
            "url": job["url"],
            "categories": job["categories"],
            "skills_required": job["skills"],
            "match_percent": match_percent,
            "recommendation": "پیشنهاد می‌شود" if match_percent >= 50 else "نیاز به بهبود مهارت‌ها"
        })

    results.sort(key=lambda x: x["match_percent"], reverse=True)
    return {"ok": True, "recommendations": results}

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run("main2:app", host="0.0.0.0", port=8000, reload=True)
