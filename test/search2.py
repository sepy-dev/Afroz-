import asyncio
import httpx
from bs4 import BeautifulSoup
import aiosqlite

BASE_URL = "https://jobinja.ir/jobs"
CONCURRENT_PAGES = 5  # تعداد صفحات همزمان
CONCURRENT_JOBS = 10  # تعداد آگهی همزمان

async def fetch(client, url):
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"[Error] fetching {url}: {e}")
        return None

async def parse_job_list(html):
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for li in soup.select("li.o-listView__item"):
        a_tag = li.select_one("h2.o-listView__itemTitle a")
        if a_tag:
            job_url = a_tag['href']
            if not job_url.startswith("http"):
                job_url = "https://jobinja.ir" + job_url
            jobs.append(job_url)
    return jobs

async def parse_job_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    # عنوان شغل
    title_tag = soup.select_one("h1.c-jobSingle__title")
    title = title_tag.text.strip() if title_tag else "بدون عنوان"

    # مهارت‌ها
    skills = []
    for li in soup.select("li.c-infoBox__item"):
        h4 = li.select_one("h4.c-infoBox__itemTitle")
        if h4 and "مهارت‌های مورد نیاز" in h4.text:
            skills = [span.text.strip() for span in li.select("span.black")]
            break

    # دسته‌بندی شغلی
    category = None
    for li in soup.select("li.c-infoBox__item"):
        h4 = li.select_one("h4.c-infoBox__itemTitle")
        if h4 and "دسته‌بندی شغلی" in h4.text:
            category = li.select_one("a")
            category = category.text.strip() if category else None
            break

    # حداقل تحصیلات
    education = None
    for li in soup.select("li.c-infoBox__item"):
        h4 = li.select_one("h4.c-infoBox__itemTitle")
        if h4 and "حداقل مدرک تحصیلی" in h4.text:
            education = li.select_one("span")
            education = education.text.strip() if education else None
            break

    return {
        "title": title,
        "skills": skills,
        "category": category,
        "education": education,
    }

async def save_job(db, job_url, job_data):
    await db.execute(
        """
        INSERT OR IGNORE INTO jobs (url, title, skills, category, education)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            job_url,
            job_data['title'],
            ",".join(job_data['skills']),
            job_data['category'],
            job_data['education'],
        )
    )
    await db.commit()

async def process_job(client, db, job_url, semaphore):
    async with semaphore:
        html = await fetch(client, job_url)
        if html:
            job_data = await parse_job_detail(html)
            await save_job(db, job_url, job_data)
            print(f"[Saved] {job_data['title']} | مهارت‌ها: {len(job_data['skills'])} | دسته: {job_data['category']} | تحصیلات: {job_data['education']}")

async def main():
    semaphore_jobs = asyncio.Semaphore(CONCURRENT_JOBS)
    async with httpx.AsyncClient(timeout=30) as client, aiosqlite.connect("jobinja_async.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                skills TEXT,
                category TEXT,
                education TEXT
            )
        """)
        await db.commit()

        page = 1
        while True:
            page_url = f"{BASE_URL}?page={page}"
            print(f"\n[Fetch Page] {page_url}")
            html = await fetch(client, page_url)
            if not html:
                print("صفحه پیدا نشد یا خطا در دریافت صفحه، متوقف می‌شود.")
                break
            job_urls = await parse_job_list(html)
            if not job_urls:
                print("دیگه آگهی جدیدی پیدا نشد، پایان اسکرپینگ.")
                break

            # پردازش موازی آگهی‌ها با محدودیت همزمانی
            tasks = [process_job(client, db, url, semaphore_jobs) for url in job_urls]
            await asyncio.gather(*tasks)

            page += 1

asyncio.run(main())
