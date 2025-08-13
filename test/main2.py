from fastapi import FastAPI
from typing import List, Dict, Any
import sqlite3
import json

app = FastAPI()

def compute_match(required_skills: List[str], candidate_skills: List[Dict[str, Any]]) -> Dict[str, Any]:
    PRIORITY_WEIGHTS = [1.0, 0.9, 0.8, 0.7, 0.6]
    DEFAULT_WEIGHT_AFTER_5 = 0.5
    LEVEL_TO_MULTIPLIER = {1:0.2, 2:0.4, 3:0.6, 4:0.8, 5:1.0}
    SAMPLE_PER = 0.05
    SAMPLE_CAP = 0.20
    
    def sample_multiplier(samples: int) -> float:
        add = min(samples * SAMPLE_PER, SAMPLE_CAP)
        return 1.0 + add
    
    def get_weight_for_index(idx: int) -> float:
        if idx < len(PRIORITY_WEIGHTS):
            return PRIORITY_WEIGHTS[idx]
        return DEFAULT_WEIGHT_AFTER_5
    
    cand_map = {c['name'].lower(): c for c in candidate_skills}
    total_score = 0.0
    weights = [get_weight_for_index(i) for i in range(len(required_skills))]
    max_possible = sum(weights) * (1.0 + SAMPLE_CAP)
    details = []
    
    for idx, req_skill in enumerate(required_skills):
        weight = weights[idx]
        req_norm = req_skill.lower()
        matched = cand_map.get(req_norm)
        if matched:
            level = matched.get("level",1)
            samples = matched.get("samples",0)
            lvl_mul = LEVEL_TO_MULTIPLIER.get(level, 0.2)
            samp_mul = sample_multiplier(samples)
            skill_score = weight * lvl_mul * samp_mul
            total_score += skill_score
            details.append({
                "required_skill": req_skill,
                "matched_with": matched['name'],
                "level": level,
                "samples": samples,
                "skill_score": round(skill_score,4)
            })
        else:
            details.append({
                "required_skill": req_skill,
                "matched_with": None,
                "level": None,
                "samples": 0,
                "skill_score": 0.0
            })
    percentage = round((total_score / max_possible) * 100, 2) if max_possible > 0 else 0.0
    
    recommendations = []
    for d in details:
        if d["matched_with"] is None:
            recommendations.append({
                "skill": d["required_skill"],
                "recommendation": "یادگیری این مهارت ضروری است."
            })
        elif d["level"] is not None and d["level"] < 3:
            recommendations.append({
                "skill": d["required_skill"],
                "recommendation": f"سطح مهارت پایین است (فعلی: {d['level']}). توصیه به ارتقا."
            })
    
    return {
        "percentage": percentage,
        "details": details,
        "recommendations": recommendations
    }


@app.get("/all-results")
def all_results():
    conn = sqlite3.connect("jobs.db")
    c = conn.cursor()
    c.execute("SELECT id, title, description, required_skills FROM jobs")
    jobs = c.fetchall()
    conn.close()

    # فرض می‌کنیم required_skills یک رشته JSON است
    # مهارت‌های نمونه برای کاندید
    candidate_skills = [
        {"name": "python", "level": 4, "samples": 2},
        {"name": "django", "level": 3, "samples": 1},
        {"name": "sql", "level": 2, "samples": 0}
    ]

    results = []
    for job in jobs:
        job_id, title, description, skills_json = job
        try:
            required_skills = json.loads(skills_json)
        except Exception:
            required_skills = []
        match_result = compute_match(required_skills, candidate_skills)
        results.append({
            "id": job_id,
            "title": title,
            "description": description,
            "required_skills": required_skills,
            "match_percentage": match_result["percentage"],
            "recommendations": match_result["recommendations"],
            "details": match_result["details"]
        })
    return {"jobs": results}
