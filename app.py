"""UptimeLite - FastAPI + SQLite + HTMX. Single process, lightweight, easy to debug."""
import asyncio
import os
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import scheduler

db.init_db()
app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

FREEE_LIMIT = 5
COOKIE = "ul_session"
PLAN_LIMITS = {"free": FREEE_LIMIT, "pro": 50, "team": 500}


# ---------- auth helpers ----------
def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    return db.get_user_by_token(token)


def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, headers={"location": "/login"}, detail="login")
    return u


# ---------- background pinger ----------
@app.on_event("startup")
async def start_scheduler():
    async def loop():
        while True:
            try:
                scheduler.run_once()
            except Exception as e:  # noqa: BLE001
                print("[SCHED-ERR]", e)
            await asyncio.sleep(15)
    asyncio.create_task(loop())


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    u = current_user(request)
    if u:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "home.html", {})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    u = db.get_user_by_email(email)
    if not u or not db.verify_password(password, u["password_hash"]):
        return templates.TemplateResponse(request, "login.html", {"error": "Bad email or password"})
    token = db.create_session(u["id"])
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...)):
    if db.get_user_by_email(email):
        return templates.TemplateResponse(request, "register.html", {"error": "Email already registered"})
    db.create_user(email, password)
    u = db.get_user_by_email(email)
    token = db.create_session(u["id"])
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        db.destroy_session(token)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    u = require_user(request)
    monitors = db.list_monitors(u["id"])
    enriched = []
    for m in monitors:
        d = dict(m)
        d["uptime24"] = db.uptime_pct(m["id"], 24)
        enriched.append(d)
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": u,
        "monitors": enriched,
        "limit": PLAN_LIMITS.get(u["plan"], FREEE_LIMIT),
        "count": len(monitors),
    })


@app.post("/monitors")
def create_monitor(request: Request, name: str = Form(...), url: str = Form(...), interval: int = Form(60)):
    u = require_user(request)
    if db.count_monitors(u["id"]) >= PLAN_LIMITS.get(u["plan"], FREEE_LIMIT):
        return templates.TemplateResponse(request, "dashboard.html", {
            "user": u, "monitors": db.list_monitors(u["id"]),
            "limit": PLAN_LIMITS.get(u["plan"], FREEE_LIMIT), "count": db.count_monitors(u["id"]),
            "error": "Monitor limit reached for your plan. Upgrade to add more."
        })
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    db.add_monitor(u["id"], name, url, max(15, interval))
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/monitors/{mid}/delete")
def delete(request: Request, mid: int):
    u = require_user(request)
    db.delete_monitor(mid, u["id"])
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/monitors/{mid}/toggle")
def toggle(request: Request, mid: int):
    u = require_user(request)
    m = db.get_monitor(mid)
    if m and m["user_id"] == u["id"]:
        db.set_monitor_active(mid, u["id"], not m["active"])
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/monitors/{mid}", response_class=HTMLResponse)
def detail(request: Request, mid: int):
    u = require_user(request)
    m = db.get_monitor(mid)
    if not m or m["user_id"] != u["id"]:
        return RedirectResponse("/dashboard", status_code=303)
    conn = db.get_conn()
    checks = conn.execute(
        "SELECT * FROM checks WHERE monitor_id=? ORDER BY checked_at DESC LIMIT 50", (mid,)
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(request, "detail.html", {
        "monitor": m, "checks": checks,
        "uptime24": db.uptime_pct(mid, 24), "uptime7": db.uptime_pct(mid, 24 * 7),
    })


# Stripe webhook placeholder (wire when ready). Keeps the path documented.
# @app.post("/stripe/webhook")
# def stripe_webhook(request: Request):
#     ... set db.set_plan(user_id, "pro") on checkout.session.completed ...

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
