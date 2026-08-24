"""Ping loop + alerting. Pure stdlib (urllib, smtplib). Runs inside FastAPI event loop."""
import time
import os
import smtplib
import urllib.request
from datetime import datetime, timezone, timedelta

import db


def ping(url: str, timeout: int = 10):
    start = time.time()
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "UptimeLite/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = time.time() - start
            return 1, resp.status, round(latency, 3), None
    except Exception as e:  # noqa: BLE001
        latency = time.time() - start
        return 0, None, round(latency, 3), str(e)[:300]


def send_alert(monitor, code, err):
    email = db.get_user_email(monitor["user_id"])
    body = f"MONITOR DOWN\nName: {monitor['name']}\nURL: {monitor['url']}\nHTTP: {code}\nError: {err}"
    host = os.environ.get("SMTP_HOST")
    if host and email:
        try:
            import email as em
            import email.utils
            msg = em.Message()
            msg["From"] = os.environ.get("SMTP_FROM", host)
            msg["To"] = email
            msg["Subject"] = f"[UptimeLite] DOWN: {monitor['name']}"
            msg.set_payload(body)
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))) as s:
                if os.environ.get("SMTP_USER"):
                    s.starttls()
                    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.sendmail(msg["From"], [email], msg.as_string())
            return
        except Exception as e:  # noqa: BLE001
            print("[ALERT-SMTP-FAIL]", e)
    # Fallback: always log so you see it even without SMTP configured
    print(f"[ALERT] {body} -> {email}")


def run_once():
    now = datetime.now(timezone.utc)
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM monitors WHERE active=1 AND (next_check IS NULL OR next_check <= ?)",
        (now.isoformat(),),
    )
    for m in cur.fetchall():
        status, code, latency, err = ping(m["url"])
        checked_at = datetime.now(timezone.utc)
        cur.execute(
            "INSERT INTO checks (monitor_id, status, status_code, latency, error, checked_at) VALUES (?,?,?,?,?,?)",
            (m["id"], status, code, latency, err, checked_at.isoformat()),
        )
        nxt = checked_at + timedelta(seconds=m["interval_seconds"])
        # alert on transition to DOWN (was not down before)
        if status == 0 and m["last_status"] != 0:
            send_alert(m, code, err)
        cur.execute(
            "UPDATE monitors SET last_status=?, last_checked=?, last_latency=?, next_check=? WHERE id=?",
            (status, checked_at.isoformat(), latency, nxt.isoformat(), m["id"]),
        )
        conn.commit()
    conn.close()
