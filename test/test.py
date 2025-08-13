import sqlite3
import json

conn = sqlite3.connect("jobs.db")
c = conn.cursor()

# جدول بساز
c.execute("""
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    required_skills TEXT NOT NULL -- JSON رشته‌ای ذخیره میشه
)
""")

# نمونه داده‌ها
jobs = [
    {
        "title": "Data Analyst",
        "description": "SQL and Python required for data analysis",
        "required_skills": ["sql", "python", "excel"]
    },
    {
        "title": "Backend Developer",
        "description": "Strong Django and REST API skills needed",
        "required_skills": ["python", "django", "rest", "api"]
    },
    {
        "title": "Frontend Developer",
        "description": "Vue.js and JavaScript expert",
        "required_skills": ["javascript", "vue", "html", "css"]
    }
]

# داده‌ها را وارد کن
for job in jobs:
    c.execute(
        "INSERT INTO jobs (title, description, required_skills) VALUES (?, ?, ?)",
        (job["title"], job["description"], json.dumps(job["required_skills"]))
    )

conn.commit()
conn.close()

print("Database and table created, sample data inserted.")
