from typing import List, Optional, Dict, Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache, cached
from rapidfuzz import process, fuzz  # optional but recommended

app = FastAPI(title="Job Match API (Jobinja, requests+BS4)")

# ---------- تنظیمات الگوریتم ----------
PRIORITY_WEIGHTS = [1.0, 0.9, 0.8, 0.7, 0.6]  # برای 5 مهارت اول
DEFAULT_WEIGHT_AFTER_5 = 0.5

LEVEL_TO_MULTIPLIER = {
    1: 0.2,
    2: 0.4,
    3: 0.6,
    4: 0.8,
    5: 1.0
}

# هر نمونه کار 5% اضافی تا سقف 20%
SAMPLE_PER = 0.05
SAMPLE_CAP = 0.20

FUZZY_THRESHOLD = 75  # اگر از fuzzy استفاده می‌کنیم: حداقل نمره برای پذیرفتن
ALLOWED_DOMAIN = "jobinja.ir"  # محدودیت ساده برای جلوگیری از SSRF — قابل تغییر

# cache برای استخراج مهارت‌ها: maxsize و TTL (ثانیه)
skills_cache = TTLCache(maxsize=1000, ttl=60 * 60)  # کش یک ساعته

# ---------- مدل‌های ورودی ----------
class CandidateSkill(BaseModel):
    name: str
    level: int  # 1..5
    samples: int = 0

class MatchRequest(BaseModel):
    job_url: Optional[HttpUrl] = None
    skills_override: Optional[List[str]] = None  # در صورتی که استخراج نکنیم
    candidate_skills: List[CandidateSkill]


# ---------- توابع کمکی ----------
def validate_jobinja_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="invalid_url_scheme")
    host = parsed.netloc.lower()
    # جلوگیری از SSRF با یک allowlist ساده
    if ALLOWED_DOMAIN not in host:
        raise HTTPException(status_code=400, detail=f"only {ALLOWED_DOMAIN} domain is allowed for scraping")


@cached(skills_cache)
def extract_skills_from_jobinja(url: str) -> List[str]:
    """
    استخراج مهارت‌ها با requests + BeautifulSoup با استفاده از ساختاری که در Inspect پیدا کردی:
    <li class="c-infoBox__item">
        <h4 class="c-infoBox__itemTitle">مهارت‌های مورد نیاز</h4>
        <div class="tags">
            <span class="black">...</span>
        </div>
    </li>
    """
    headers = {"User-Agent": "Mozilla/5.0 (JobMatchBot)"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"failed_fetching_job_page: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.find_all("li", class_="c-infoBox__item")
    skills: List[str] = []
    for item in items:
        title = item.find("h4", class_="c-infoBox__itemTitle")
        if title and "مهارت" in title.get_text():
            tags_div = item.find("div", class_="tags")
            if not tags_div:
                continue
            spans = tags_div.find_all("span")
            for sp in spans:
                text = sp.get_text(strip=True)
                if text:
                    skills.append(text)
            break

    # fallback: اگر خالی موند، تلاش برای پیدا کردن عبارت‌های تک‌کلمه‌ای فنی در متن صفحه
    if not skills:
        body = soup.get_text(" ").lower()
        keywords = ["python","django","docker","react","vue","javascript","sql","mysql","postgres","linux","rest","api"]
        found = []
        for kw in keywords:
            if kw in body and kw not in found:
                found.append(kw)
        skills = found

    # نرمال‌سازی: trim و حذف تکرار
    normalized = []
    for s in skills:
        s2 = " ".join(s.split())  # remove extra spaces
        if s2 and s2 not in normalized:
            normalized.append(s2)
    return normalized


def sample_multiplier(samples: int) -> float:
    add = min(samples * SAMPLE_PER, SAMPLE_CAP)
    return 1.0 + add


def get_weight_for_index(idx: int) -> float:
    if idx < len(PRIORITY_WEIGHTS):
        return PRIORITY_WEIGHTS[idx]
    return DEFAULT_WEIGHT_AFTER_5


def compute_match(required_skills: List[str], candidate: List[CandidateSkill]) -> Dict[str, Any]:
    # آماده‌سازی candidate map (اسم -> skill)
    cand_map = {c.name.strip().lower(): c for c in candidate}
    cand_names = list(cand_map.keys())

    details = []
    total_score = 0.0
    weights = []
    for i in range(len(required_skills)):
        weights.append(get_weight_for_index(i))
    max_possible = sum(weights) * (1.0 * (1.0 + SAMPLE_CAP))  # در نظر گرفتن سقف نمونه‌کار (مثلاً 1.2)

    for idx, req in enumerate(required_skills):
        req_norm = req.strip().lower()
        weight = weights[idx]
        matched = None
        fuzzy_score = None

        # 1) تلاش exact match
        if req_norm in cand_map:
            matched = cand_map[req_norm]
            fuzzy_score = 100
            match_type = "exact"
        else:
            # 2) تلاش fuzzy (rapidfuzz)
            if cand_names:
                choice, score, _ = process.extractOne(req_norm, cand_names, scorer=fuzz.token_sort_ratio)
                if score is not None and score >= FUZZY_THRESHOLD:
                    matched = cand_map[choice]
                    fuzzy_score = int(score)
                    match_type = "fuzzy"
                else:
                    matched = None
                    fuzzy_score = int(score) if score is not None else None
                    match_type = "none"
            else:
                match_type = "none"

        if matched:
            level = max(1, min(5, int(matched.level)))
            lvl_mul = LEVEL_TO_MULTIPLIER.get(level, 0.2)
            samp_mul = sample_multiplier(int(matched.samples))
            skill_score = weight * lvl_mul * samp_mul
            total_score += skill_score
            details.append({
                "required_skill": req,
                "matched_with": matched.name,
                "match_type": match_type,
                "fuzzy_score": fuzzy_score,
                "weight": weight,
                "level": level,
                "sample_count": matched.samples,
                "sample_multiplier": round(samp_mul, 3),
                "skill_score": round(skill_score, 4)
            })
        else:
            details.append({
                "required_skill": req,
                "matched_with": None,
                "match_type": match_type,
                "fuzzy_score": fuzzy_score,
                "weight": weight,
                "level": None,
                "sample_count": 0,
                "sample_multiplier": 1.0,
                "skill_score": 0.0
            })

    percentage = round((total_score / max_possible) * 100, 2) if max_possible > 0 else 0.0
    

    recommendations = []
    for detail in details:
        if detail["matched_with"] is None:
            recommendations.append({
                "skill": detail["required_skill"],
                "recommendation": "یادگیری این مهارت ضروری است."
            })
        else:
            if detail["level"] is not None and detail["level"] < 3:
                recommendations.append({
                    "skill": detail["required_skill"],
                    "recommendation": f"سطح مهارت پایین است (سطح فعلی: {detail['level']}). توصیه می‌شود سطح را ارتقا دهید."
                })

    return {
        "percentage": percentage,
        "total_score": round(total_score, 4),
        "max_possible_score": round(max_possible, 4),
        "details": details,
        "recommendations": recommendations,
        "required_skills": required_skills
    }


# ---------- endpoints ----------
@app.post("/match")
def match(req: MatchRequest):
    # 1) تعیین مهارت‌های آگهی (از URL یا override)
    required_skills: List[str] = []
    if req.skills_override:
        required_skills = req.skills_override
    elif req.job_url:
        validate_jobinja_url(str(req.job_url))
        required_skills = extract_skills_from_jobinja(str(req.job_url))
        if not required_skills:
            raise HTTPException(status_code=400, detail="failed_to_extract_skills")
    else:
        raise HTTPException(status_code=400, detail="provide job_url or skills_override")

    # 2) محاسبه تطابق
    result = compute_match(required_skills, req.candidate_skills)
    return {"ok": True, "result": result}


@app.get("/extract-skills")
def extract_skills_endpoint(job_url: HttpUrl):
    validate_jobinja_url(str(job_url))
    skills = extract_skills_from_jobinja(str(job_url))
    if not skills:
        raise HTTPException(status_code=404, detail="no_skills_found")
    return {"ok": True, "job_url": str(job_url), "skills": skills}

