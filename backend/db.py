"""SQLite storage layer for LinkedIn lead generation.

Single shared database (data/leadflow.db) holding:
  - raw_items: every scraped post/job, once, regardless of which user(s) it gets extracted for.
  - extraction_attempts: (user_id, raw_item_id) pairs already run through the LLM extractor,
    so re-running the pipeline never re-spends a Gemini call on the same item for the same user.
  - leads: the final structured ExtractedLead rows, one per (user_id, post_urn).
  - users: lightweight profile row per user_id (name/email), created on first login.

This is the single source of truth for structured output -- it replaces the per-user
unique_filtered_leads.csv / .json / emails_sent.csv files.
"""
import sqlite3
import json
import threading
from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

PROJECT_ROOT = Path(__file__).resolve().parent
DB_DIR = PROJECT_ROOT / "data"
DB_PATH = DB_DIR / "leadflow.db"

_init_lock = threading.Lock()
_initialized = False

# Columns on ExtractedLead that are stored as JSON text in SQLite (lists/None -> TEXT)
_JSON_FIELDS = {"tech_stack", "skills_required", "graduation_years", "experience_level"}


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist yet. Safe to call repeatedly/concurrently."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = get_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    email TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,           -- 'posts' or 'jobs'
                    urn TEXT NOT NULL UNIQUE,
                    raw_json TEXT NOT NULL,
                    scraped_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS extraction_attempts (
                    user_id TEXT NOT NULL,
                    raw_item_id INTEGER NOT NULL,
                    attempted_at TEXT NOT NULL,
                    matched INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, raw_item_id)
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    raw_item_id INTEGER,
                    post_urn TEXT NOT NULL,
                    application_method TEXT,
                    posting_date TEXT,
                    author_name TEXT,
                    author_profile TEXT,
                    is_job_posting INTEGER,
                    post_category TEXT,
                    job_title TEXT,
                    company_name TEXT,
                    location TEXT,
                    work_mode TEXT,
                    experience_level TEXT,
                    salary_range TEXT,
                    tech_stack TEXT,
                    skills_required TEXT,
                    application_contact TEXT,
                    post_url TEXT,
                    duplicate_key TEXT,
                    email_template_type TEXT,
                    role_level TEXT,
                    is_internship INTEGER,
                    is_fresher INTEGER,
                    graduation_years TEXT,
                    internship_duration TEXT,
                    stipend_range TEXT,
                    application_deadline TEXT,
                    eligibility_criteria TEXT,
                    company_logo TEXT,
                    source TEXT,
                    email_sent INTEGER NOT NULL DEFAULT 0,
                    email_sent_at TEXT,
                    template_used TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (user_id, post_urn)
                );

                CREATE INDEX IF NOT EXISTS idx_leads_user ON leads(user_id);
                CREATE INDEX IF NOT EXISTS idx_leads_user_method ON leads(user_id, application_method);
                CREATE INDEX IF NOT EXISTS idx_extraction_attempts_user ON extraction_attempts(user_id);

                CREATE TABLE IF NOT EXISTS target_companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    company_slug TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (user_id, company_slug)
                );
                """
            )
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
            if "source" not in existing_cols:
                conn.execute("ALTER TABLE leads ADD COLUMN source TEXT")
            conn.commit()
        finally:
            conn.close()
        _initialized = True


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def ensure_user(user_id: str, name: Optional[str] = None, email: Optional[str] = None):
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO users (user_id, name, email, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "name=COALESCE(excluded.name, users.name), email=COALESCE(excluded.email, users.email)",
            (user_id, name, email, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def user_exists(user_id: str) -> bool:
    init_db()
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Target companies (per-user Greenhouse board slugs to scrape)
# ---------------------------------------------------------------------------

def get_target_companies(user_id: str) -> List[str]:
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT company_slug FROM target_companies WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        return [row["company_slug"] for row in rows]
    finally:
        conn.close()


def get_all_target_companies() -> List[str]:
    """Distinct target-company slugs across every user -- the global scrape pipeline scrapes
    every company any user has targeted once, then normal per-user LLM filtering (preferred
    roles/locations) decides which of those postings actually become leads for whom."""
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT company_slug FROM target_companies").fetchall()
        return [row["company_slug"] for row in rows]
    finally:
        conn.close()


def add_target_company(user_id: str, company_slug: str):
    init_db()
    ensure_user(user_id)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO target_companies (user_id, company_slug, created_at) VALUES (?, ?, ?)",
            (user_id, company_slug.strip().lower(), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def remove_target_company(user_id: str, company_slug: str):
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM target_companies WHERE user_id = ? AND company_slug = ?",
            (user_id, company_slug.strip().lower()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Raw scraped items (shared across all users)
# ---------------------------------------------------------------------------

def _item_urn(item: Dict, source: str, fallback_index: int) -> str:
    urn = (
        item.get("urn")           # posts (already has a LinkedIn urn)
        or item.get("jobId")      # LinkedIn jobs actor
        or item.get("jobPostingId")
        or item.get("id")         # Glassdoor jobs actor (numeric job id)
        or item.get("key")        # Indeed jobs actor
        or item.get("refNum")
        or item.get("jobUrl")
        or item.get("url")
    )
    return str(urn) if urn else f"{source}_{fallback_index}"


def insert_raw_items(items: List[Dict], source: str) -> int:
    """Insert raw scraped items, skipping ones already stored (by urn). Returns count newly inserted.

    Each item's own '_scraper_source' tag (set by ScraperRegistry.scrape_all) takes precedence
    over the `source` argument, so a mixed posts+jobs batch is stored with the correct label per item.
    """
    init_db()
    conn = get_connection()
    inserted = 0
    try:
        now = datetime.now().isoformat()
        for idx, item in enumerate(items):
            item_source = item.get('_scraper_source', source)
            urn = _item_urn(item, item_source, idx)
            cur = conn.execute(
                "INSERT OR IGNORE INTO raw_items (source, urn, raw_json, scraped_at) VALUES (?, ?, ?, ?)",
                (item_source, urn, json.dumps(item, ensure_ascii=False), now),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def get_unprocessed_raw_items_for_user(user_id: str, limit: Optional[int] = None) -> List[Dict]:
    """Raw items this user's extractor has never attempted, oldest first.

    Each returned dict has: id, source, urn, scraped_at, and the original raw fields merged in.
    """
    init_db()
    conn = get_connection()
    try:
        sql = (
            "SELECT r.id, r.source, r.urn, r.raw_json, r.scraped_at FROM raw_items r "
            "LEFT JOIN extraction_attempts a ON a.raw_item_id = r.id AND a.user_id = ? "
            "WHERE a.raw_item_id IS NULL ORDER BY r.id ASC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, (user_id,)).fetchall()
        results = []
        for row in rows:
            data = json.loads(row["raw_json"])
            data["_raw_item_id"] = row["id"]
            data["_source"] = row["source"]
            results.append(data)
        return results
    finally:
        conn.close()


def get_recent_raw_items(source: Optional[str] = None, limit: int = 50) -> List[Dict]:
    """Most recently scraped raw items, optionally filtered by source ('posts' or 'jobs')."""
    init_db()
    conn = get_connection()
    try:
        if source:
            rows = conn.execute(
                "SELECT source, urn, raw_json, scraped_at FROM raw_items WHERE source = ? ORDER BY id DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source, urn, raw_json, scraped_at FROM raw_items ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            data = json.loads(row["raw_json"])
            data["_source"] = row["source"]
            data["_scraped_at"] = row["scraped_at"]
            results.append(data)
        return results
    finally:
        conn.close()


def mark_attempted(user_id: str, raw_item_id: int, matched: bool):
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO extraction_attempts (user_id, raw_item_id, attempted_at, matched) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, raw_item_id) DO UPDATE SET attempted_at=excluded.attempted_at, matched=excluded.matched",
            (user_id, raw_item_id, datetime.now().isoformat(), 1 if matched else 0),
        )
        conn.commit()
    finally:
        conn.close()


def get_raw_item_by_urn(urn: str) -> Optional[Dict]:
    init_db()
    conn = get_connection()
    try:
        row = conn.execute("SELECT raw_json FROM raw_items WHERE urn = ?", (str(urn),)).fetchone()
        return json.loads(row["raw_json"]) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Leads (the unified structured output)
# ---------------------------------------------------------------------------

def get_recent_duplicate_keys(user_id: str, days: int = 5) -> set:
    """duplicate_key values (company+title fingerprint, see extractor._generate_duplicate_key)
    already saved for this user in the last `days` days. Cross-source duplicates (e.g. the same
    job posted on both LinkedIn and Indeed) get different post_urn values, so the UNIQUE(user_id,
    post_urn) constraint alone can't catch them -- this lets remove_duplicates() seed its in-batch
    dedup set with recent history instead of only comparing leads within the current run.
    """
    init_db()
    conn = get_connection()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT DISTINCT duplicate_key FROM leads "
            "WHERE user_id = ? AND created_at >= ? AND duplicate_key IS NOT NULL",
            (user_id, cutoff),
        ).fetchall()
        return {row["duplicate_key"] for row in rows}
    finally:
        conn.close()


def _lead_to_row(lead) -> Dict[str, Any]:
    d = asdict(lead)
    for f in _JSON_FIELDS:
        d[f] = json.dumps(d.get(f) or [], ensure_ascii=False)
    d["is_job_posting"] = int(bool(d.get("is_job_posting")))
    d["is_internship"] = None if d.get("is_internship") is None else int(bool(d["is_internship"]))
    d["is_fresher"] = None if d.get("is_fresher") is None else int(bool(d["is_fresher"]))
    return d


def save_leads(user_id: str, leads: List, raw_item_id_by_urn: Optional[Dict[str, int]] = None) -> int:
    """Persist extracted leads for a user. Skips leads whose post_urn already exists for this user.
    Returns count of newly inserted leads."""
    if not leads:
        return 0
    init_db()
    ensure_user(user_id)
    conn = get_connection()
    inserted = 0
    try:
        now = datetime.now().isoformat()
        for lead in leads:
            row = _lead_to_row(lead)
            raw_item_id = (raw_item_id_by_urn or {}).get(row["post_urn"])
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO leads (
                    user_id, raw_item_id, post_urn, application_method, posting_date, author_name,
                    author_profile, is_job_posting, post_category, job_title, company_name, location,
                    work_mode, experience_level, salary_range, tech_stack, skills_required,
                    application_contact, post_url, duplicate_key, email_template_type, role_level,
                    is_internship, is_fresher, graduation_years, internship_duration, stipend_range,
                    application_deadline, eligibility_criteria, company_logo, source, created_at
                ) VALUES (
                    :user_id, :raw_item_id, :post_urn, :application_method, :posting_date, :author_name,
                    :author_profile, :is_job_posting, :post_category, :job_title, :company_name, :location,
                    :work_mode, :experience_level, :salary_range, :tech_stack, :skills_required,
                    :application_contact, :post_url, :duplicate_key, :email_template_type, :role_level,
                    :is_internship, :is_fresher, :graduation_years, :internship_duration, :stipend_range,
                    :application_deadline, :eligibility_criteria, :company_logo, :source, :created_at
                )
                """,
                {**row, "user_id": user_id, "raw_item_id": raw_item_id, "created_at": now},
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def _deserialize_lead_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for f in _JSON_FIELDS:
        try:
            d[f] = json.loads(d[f]) if d.get(f) else []
        except (TypeError, ValueError):
            d[f] = []
    d["is_job_posting"] = bool(d.get("is_job_posting"))
    d["is_internship"] = None if d.get("is_internship") is None else bool(d["is_internship"])
    d["is_fresher"] = None if d.get("is_fresher") is None else bool(d["is_fresher"])
    d["email_sent"] = bool(d.get("email_sent"))
    return d


def get_leads_for_user(user_id: str) -> List[Dict]:
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [_deserialize_lead_row(r) for r in rows]
    finally:
        conn.close()


def get_lead_by_urn(user_id: str, post_urn: str) -> Optional[Dict]:
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? AND post_urn = ?", (user_id, str(post_urn))
        ).fetchone()
        return _deserialize_lead_row(row) if row else None
    finally:
        conn.close()


def can_send_email(user_id: str, post_urn: str, to_email: Optional[str], cooldown_days: int = 7) -> bool:
    """7-day cooldown by post_urn OR recipient email, scoped to this user -- mirrors the old CSV logic."""
    if not to_email:
        return False
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT email_sent_at FROM leads WHERE user_id = ? AND email_sent = 1 "
            "AND (post_urn = ? OR application_contact = ?) "
            "ORDER BY email_sent_at DESC LIMIT 1",
            (user_id, str(post_urn), to_email),
        ).fetchone()
        if not row or not row["email_sent_at"]:
            return True
        last_sent = datetime.fromisoformat(row["email_sent_at"])
        return datetime.now() - last_sent >= timedelta(days=cooldown_days)
    finally:
        conn.close()


def mark_email_sent(user_id: str, post_urn: str, template_used: Optional[str] = None):
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE leads SET email_sent = 1, email_sent_at = ?, template_used = ? "
            "WHERE user_id = ? AND post_urn = ?",
            (datetime.now().isoformat(), template_used, user_id, str(post_urn)),
        )
        conn.commit()
    finally:
        conn.close()


def get_dashboard_stats(user_id: str) -> Dict[str, Any]:
    init_db()
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM leads WHERE user_id = ?", (user_id,)).fetchone()["c"]
        email_leads = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE user_id = ? AND application_method = 'email'", (user_id,)
        ).fetchone()["c"]
        link_leads = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE user_id = ? AND application_method = 'link'", (user_id,)
        ).fetchone()["c"]
        other_leads = total - email_leads - link_leads
        emails_sent = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE user_id = ? AND email_sent = 1", (user_id,)
        ).fetchone()["c"]
        internships = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE user_id = ? AND is_internship = 1", (user_id,)
        ).fetchone()["c"]
        fresher_roles = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE user_id = ? AND is_fresher = 1", (user_id,)
        ).fetchone()["c"]
        recent_cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        recent_leads = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE user_id = ? AND created_at > ?", (user_id, recent_cutoff)
        ).fetchone()["c"]
        return {
            "total_leads_processed": total,
            "emails_sent": emails_sent,
            "success_rate": round((emails_sent / email_leads * 100), 1) if email_leads else 0,
            "email_leads": email_leads,
            "link_leads": link_leads,
            "other_leads": other_leads,
            "internships": internships,
            "fresher_roles": fresher_roles,
            "recent_leads": recent_leads,
        }
    finally:
        conn.close()


def get_daily_trends(user_id: str, days: int = 30) -> List[Dict]:
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS date,
                   COUNT(*) AS Leads,
                   SUM(CASE WHEN email_sent = 1 THEN 1 ELSE 0 END) AS Emails
            FROM leads WHERE user_id = ?
            GROUP BY date ORDER BY date ASC
            """,
            (user_id,),
        ).fetchall()
        trends = [dict(r) for r in rows]
        return trends[-days:]
    finally:
        conn.close()


def get_recent_activity(user_id: str, limit: int = 10) -> List[Dict]:
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT job_title, company_name, created_at FROM leads WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
