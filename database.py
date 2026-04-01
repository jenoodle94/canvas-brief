"""SQLite database for briefs and analytics."""

import sqlite3
import os
from datetime import datetime

# Use /data on Render (persistent disk), fall back to local dir
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "canvas_brief.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            course_name TEXT NOT NULL,
            module_id INTEGER NOT NULL,
            module_name TEXT NOT NULL,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            course_name TEXT,
            module_name TEXT,
            detail TEXT,
            ip_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_briefs_unique
            ON briefs(course_id, module_id);

        CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(event_type);

        CREATE INDEX IF NOT EXISTS idx_events_created
            ON events(created_at);
    """)
    conn.close()


# -------------------------------------------------------------------
# Briefs
# -------------------------------------------------------------------

def get_brief(course_id, module_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM briefs WHERE course_id = ? AND module_id = ?",
        (course_id, module_id),
    ).fetchone()
    conn.close()
    return row


def get_brief_by_id(brief_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    conn.close()
    return row


def save_brief(course_id, course_name, module_id, module_name, filename):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO briefs
           (course_id, course_name, module_id, module_name, filename, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (course_id, course_name, module_id, module_name, filename, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_briefs():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM briefs ORDER BY course_name, module_name"
    ).fetchall()
    conn.close()
    return rows


def get_briefs_for_course(course_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM briefs WHERE course_id = ? ORDER BY module_name",
        (course_id,),
    ).fetchall()
    conn.close()
    return rows


# -------------------------------------------------------------------
# Events / Analytics
# -------------------------------------------------------------------

def log_event(event_type, course_name=None, module_name=None, detail=None, ip_hash=None):
    conn = get_db()
    conn.execute(
        """INSERT INTO events (event_type, course_name, module_name, detail, ip_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_type, course_name, module_name, detail, ip_hash, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_metrics():
    """Return a dict of key metrics for the dashboard."""
    conn = get_db()

    total_briefs = conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]
    total_downloads = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'download'"
    ).fetchone()[0]
    total_generates = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'generate'"
    ).fetchone()[0]
    unique_sessions = conn.execute(
        "SELECT COUNT(DISTINCT ip_hash) FROM events WHERE event_type = 'login'"
    ).fetchone()[0]

    # Downloads by course
    downloads_by_course = conn.execute(
        """SELECT course_name, COUNT(*) as cnt
           FROM events WHERE event_type = 'download'
           GROUP BY course_name ORDER BY cnt DESC"""
    ).fetchall()

    # Most downloaded briefs
    top_briefs = conn.execute(
        """SELECT module_name, course_name, COUNT(*) as cnt
           FROM events WHERE event_type = 'download'
           GROUP BY course_name, module_name ORDER BY cnt DESC LIMIT 20"""
    ).fetchall()

    # Daily activity (last 30 days)
    daily_activity = conn.execute(
        """SELECT DATE(created_at) as day, event_type, COUNT(*) as cnt
           FROM events
           WHERE created_at >= datetime('now', '-30 days')
           GROUP BY day, event_type ORDER BY day"""
    ).fetchall()

    # Generates over time
    generates_by_day = conn.execute(
        """SELECT DATE(created_at) as day, COUNT(*) as cnt
           FROM events WHERE event_type = 'generate'
           GROUP BY day ORDER BY day"""
    ).fetchall()

    conn.close()

    return {
        "total_briefs": total_briefs,
        "total_downloads": total_downloads,
        "total_generates": total_generates,
        "unique_sessions": unique_sessions,
        "downloads_by_course": [dict(r) for r in downloads_by_course],
        "top_briefs": [dict(r) for r in top_briefs],
        "daily_activity": [dict(r) for r in daily_activity],
        "generates_by_day": [dict(r) for r in generates_by_day],
    }
