"""SQLite data layer for UptimeLite. Pure stdlib, no external deps."""
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uptime.db"))


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            stripe_customer_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            slug TEXT,
            interval_seconds INTEGER NOT NULL DEFAULT 60,
            active INTEGER NOT NULL DEFAULT 1,
            last_status INTEGER,
            last_checked TEXT,
            last_latency REAL,
            next_check TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            status INTEGER NOT NULL,
            status_code INTEGER,
            latency REAL,
            error TEXT,
            checked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            stripe_sub_id TEXT,
            plan TEXT,
            status TEXT,
            renews_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_checks_monitor ON checks(monitor_id, checked_at);
        """
    )
    conn.commit()
    conn.close()
    # Migrate: add slug column if missing (for old databases)
    try:
        conn = get_conn()
        conn.execute("SELECT slug FROM monitors LIMIT 1")
        conn.close()
    except Exception:
        try:
            conn = get_conn()
            conn.execute("ALTER TABLE monitors ADD COLUMN slug TEXT")
            conn.commit()
            conn.close()
        except Exception:
            pass
    print("[DB] initialized at", DB_PATH)


# ---------- auth ----------
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
    return salt + ":" + dk.hex()


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, dk = stored.split(":")
        expected = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000).hex()
        return secrets.compare_digest(expected, dk)
    except Exception:
        return False


def create_user(email: str, pw: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
        (email.lower(), hash_password(pw), datetime.now(timezone.utc).isoformat()),
    )
    uid = cur.lastrowid
    conn.commit()
    conn.close()
    return uid


def get_user_by_email(email: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
    conn.close()
    return row


def get_user_by_token(token: str):
    conn = get_conn()
    s = conn.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
    if not s:
        conn.close()
        return None
    u = conn.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
    conn.close()
    return u


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
        (token, user_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return token


def destroy_session(token: str):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


def set_plan(user_id: int, plan: str):
    conn = get_conn()
    conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
    conn.commit()
    conn.close()


def set_stripe_customer(user_id: int, customer_id: str):
    conn = get_conn()
    conn.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (customer_id, user_id))
    conn.commit()
    conn.close()


def upsert_subscription(user_id, stripe_sub_id, plan, status, renews_at):
    conn = get_conn()
    conn.execute(
        "INSERT INTO subscriptions (user_id, stripe_sub_id, plan, status, renews_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET stripe_sub_id=?, plan=?, status=?, renews_at=?",
        (user_id, stripe_sub_id, plan, status, renews_at, stripe_sub_id, plan, status, renews_at),
    )
    conn.commit()
    conn.close()


def get_subscription(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def set_subscription_status(user_id, status):
    conn = get_conn()
    conn.execute("UPDATE subscriptions SET status=? WHERE user_id=?", (status, user_id))
    conn.commit()
    conn.close()


# ---------- monitors ----------
def make_slug(text):
    """Generate a URL-safe slug from text."""
    import re
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = re.sub(r'^-+|-+$', '', text)
    if not text:
        text = "monitor"
    return text


def unique_slug(user_id, base_slug):
    """Ensure slug is unique within a user's monitors."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT slug FROM monitors WHERE user_id=? AND slug=?", (user_id, base_slug)
    ).fetchall()
    conn.close()
    if not existing:
        return base_slug
    for i in range(2, 100):
        candidate = f"{base_slug}-{i}"
        conn = get_conn()
        check = conn.execute(
            "SELECT 1 FROM monitors WHERE user_id=? AND slug=?", (user_id, candidate)
        ).fetchone()
        conn.close()
        if not check:
            return candidate
    return f"{base_slug}-{secrets.token_hex(4)}"


def add_monitor(user_id, name, url, interval_seconds=60, slug=None):
    now = datetime.now(timezone.utc)
    if slug:
        slug = make_slug(slug)
    else:
        slug = make_slug(name)
    # ensure uniqueness — append short suffix if taken
    slug = unique_slug(user_id, slug)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO monitors (user_id, name, url, slug, interval_seconds, next_check, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, name, url, slug, interval_seconds, now.isoformat(), now.isoformat()),
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


def list_monitors(user_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM monitors WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    conn.close()
    return rows


def get_monitor(mid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM monitors WHERE id=?", (mid,)).fetchone()
    conn.close()
    return row


def get_monitor_by_slug(slug):
    conn = get_conn()
    row = conn.execute("SELECT * FROM monitors WHERE slug=?", (slug,)).fetchone()
    conn.close()
    return row


def delete_monitor(mid, user_id):
    conn = get_conn()
    conn.execute("DELETE FROM monitors WHERE id=? AND user_id=?", (mid, user_id))
    conn.execute("DELETE FROM checks WHERE monitor_id=?", (mid,))
    conn.commit()
    conn.close()


def set_monitor_active(mid, user_id, active):
    conn = get_conn()
    conn.execute("UPDATE monitors SET active=? WHERE id=? AND user_id=?", (1 if active else 0, mid, user_id))
    conn.commit()
    conn.close()


def count_monitors(user_id):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) c FROM monitors WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["c"]


def get_user_email(user_id):
    u = get_user_by_id(user_id)
    return u["email"] if u else None


def get_user_by_id(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_user_by_stripe_customer(customer_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
    conn.close()
    return row


def uptime_pct(monitor_id, hours=24):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) c, SUM(status) s FROM checks WHERE monitor_id=? AND checked_at >= ?",
        (monitor_id, since),
    ).fetchone()
    conn.close()
    if not row or row["c"] == 0:
        return None
    return round(100.0 * row["s"] / row["c"], 1)


def get_recent_checks(monitor_id, limit=30):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM checks WHERE monitor_id=? ORDER BY checked_at DESC LIMIT ?",
        (monitor_id, limit),
    ).fetchall()
    conn.close()
    return rows
