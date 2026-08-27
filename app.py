"""UptimeLite - FastAPI + SQLite + HTMX. Single process, lightweight, easy to debug.

v1.1: Adds public status pages (ads + freemium), Stripe checkout for plan upgrades.
"""
import asyncio
import os
import json
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import scheduler

db.init_db()
app = FastAPI(title="UptimeLite")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

# --- Plan config ---
FREE_LIMIT = 5  # free plan: 5 monitors
PLAN_LIMITS = {"free": FREE_LIMIT, "pro": 50, "team": 500}
PLAN_PRICES = {
    "pro": {"monthly": 9, "annual": 90},
    "team": {"monthly": 29, "annual": 290},
}
COOKIE = "ul_session"

# --- AdSense (optional) ---
# Set GOOGLE_ADSENSE_ID to enable ads on public status pages.
# Example: GOOGLE_ADSENSE_ID=ca-pub-123456789012345
ADSENSE_ID = os.environ.get("GOOGLE_ADSENSE_ID", "")


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
        "limit": PLAN_LIMITS.get(u["plan"], FREE_LIMIT),
        "count": len(monitors),
        "PLAN_PRICES": PLAN_PRICES,
    })


@app.post("/monitors")
def create_monitor(request: Request, name: str = Form(...), url: str = Form(...), interval: int = Form(60)):
    u = require_user(request)
    if db.count_monitors(u["id"]) >= PLAN_LIMITS.get(u["plan"], FREE_LIMIT):
        return templates.TemplateResponse(request, "dashboard.html", {
            "user": u, "monitors": db.list_monitors(u["id"]),
            "limit": PLAN_LIMITS.get(u["plan"], FREE_LIMIT), "count": db.count_monitors(u["id"]),
            "PLAN_PRICES": PLAN_PRICES,
            "error": "Monitor limit reached for your plan. Upgrade to add more.",
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


# ---------- public status pages ----------
@app.get("/status/{slug}", response_class=HTMLResponse)
def public_status(request: Request, slug: str):
    """Public, shareable status page. No auth required. Monetized via AdSense."""
    m = db.get_monitor_by_slug(slug)
    if not m:
        raise HTTPException(status_code=404, detail="Status page not found")
    # Only show active monitors
    if not m["active"]:
        raise HTTPException(status_code=404, detail="Monitor is paused")
    last_checks = db.get_recent_checks(m["id"], limit=30)
    uptime24 = db.uptime_pct(m["id"], 24)
    uptime7 = db.uptime_pct(m["id"], 24 * 7)
    return templates.TemplateResponse(request, "status.html", {
        "monitor": m,
        "checks": last_checks,
        "uptime24": uptime24,
        "uptime7": uptime7,
        "adsense_id": ADSENSE_ID,
    })


# ---------- pricing ----------
@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    u = current_user(request)
    return templates.TemplateResponse(request, "pricing.html", {
        "user": u,
        "PLAN_PRICES": PLAN_PRICES,
        "PLAN_LIMITS": PLAN_LIMITS,
        "FREE_LIMIT": FREE_LIMIT,
    })


# ---------- billing / stripe ----------
@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    u = require_user(request)
    sub = db.get_subscription(u["id"])
    success = request.query_params.get("success") == "1"
    return templates.TemplateResponse(request, "billing.html", {
        "user": u,
        "subscription": sub,
        "PLAN_PRICES": PLAN_PRICES,
        "PLAN_LIMITS": PLAN_LIMITS,
        "error": None,
        "success": success,
    })


@app.post("/billing/checkout")
def billing_checkout(request: Request, plan: str = Form("pro"), interval: str = Form("monthly")):
    u = require_user(request)
    stripe_key = os.environ.get("STRIPE_SECRET_KEY")

    # If Stripe not configured, redirect to a test URL (for local dev/testing)
    if not stripe_key:
        print("[STRIPE-SKIP] no STRIPE_SECRET_KEY, redirecting to test checkout")
        return RedirectResponse("https://checkout.stripe.com/pay/test_uptime_lite", status_code=303)

    try:
        import stripe
        stripe.api_key = stripe_key

        monthly_price_id = os.environ.get("STRIPE_PRICE_" + plan.upper() + "_MONTHLY")
        annual_price_id = os.environ.get("STRIPE_PRICE_" + plan.upper() + "_ANNUAL")

        if interval == "annual" and annual_price_id:
            price_id = annual_price_id
            session = stripe.checkout.Session.create(
                customer_email=u["email"],
                line_items=[{"price": price_id, "quantity": 1}],
                mode="subscription",
                success_url=str(request.base_url) + "billing?success=1",
                cancel_url=str(request.base_url) + "billing?canceled=1",
            )
        elif interval == "monthly" and monthly_price_id:
            price_id = monthly_price_id
            session = stripe.checkout.Session.create(
                customer_email=u["email"],
                line_items=[{"price": price_id, "quantity": 1}],
                mode="subscription",
                success_url=str(request.base_url) + "billing?success=1",
                cancel_url=str(request.base_url) + "billing?canceled=1",
            )
        else:
            # Ad-hoc price
            unit_amount = PLAN_PRICES[plan]["annual"] * 100 if interval == "annual" else PLAN_PRICES[plan]["monthly"] * 100
            interval_str = "year" if interval == "annual" else "month"
            session = stripe.checkout.Session.create(
                customer_email=u["email"],
                line_items=[{"price_data": {
                    "currency": "usd",
                    "unit_amount": unit_amount,
                    "recurring": {"interval": interval_str},
                    "product_data": {"name": f"UptimeLite {plan.title()} ({interval.title()})"},
                }, "quantity": 1}],
                mode="subscription",
                success_url=str(request.base_url) + "billing?success=1",
                cancel_url=str(request.base_url) + "billing?canceled=1",
            )

        if session.get("customer"):
            db.set_stripe_customer(u["id"], session["customer"])
        print(f"[STRIPE] checkout {plan}/{interval} user {u['id']} url {session.url}")
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        print("[STRIPE-ERR]", e)
        sub = db.get_subscription(u["id"])
        return templates.TemplateResponse(request, "billing.html", {
            "user": u, "subscription": sub,
            "PLAN_PRICES": PLAN_PRICES, "PLAN_LIMITS": PLAN_LIMITS, "error": str(e), "success": False,
        })


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events to upgrade/downgrade users."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    event = None
    if secret and sig:
        try:
            import stripe
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            event = stripe.Webhook.construct_event(payload, sig, secret)
        except Exception as e:
            print("[WEBHOOK-SIG-ERR]", e)
            try:
                event = json.loads(payload)
            except Exception:
                return PlainTextResponse("invalid signature", status_code=400)
    else:
        # For testing without real Stripe: accept JSON payload directly
        try:
            event = json.loads(payload)
        except Exception as e:
            print("[WEBHOOK-JSON-ERR]", e)
            return PlainTextResponse("invalid payload", status_code=400)
        if not event:
            event = {}

    try:
        etype = event.get("type", "") if isinstance(event, dict) else getattr(event, "type", "")
        data_obj = event.get("data", {}).get("object", {}) if isinstance(event, dict) else event.data.object
        print(f"[WEBHOOK] event {etype}")

        if etype in ("checkout.session.completed", "customer.subscription.created",
                      "customer.subscription.updated", "invoice.paid"):
            # Determine plan from the event
            plan_key = "pro"
            if isinstance(data_obj, dict):
                # For checkout.session.completed, look at line_items
                if etype == "checkout.session.completed":
                    customer_id = data_obj.get("customer")
                    sub_id = data_obj.get("subscription")
                else:
                    customer_id = data_obj.get("customer")
                    sub_id = data_obj.get("id")

                # Try to find user by stripe customer_id
                user = None
                if customer_id:
                    user = db.get_user_by_stripe_customer(customer_id)
                if not user:
                    email = data_obj.get("customer_email")
                    if not email:
                        try:
                            if isinstance(data_obj.get("customer_details"), dict):
                                email = data_obj["customer_details"].get("email")
                        except Exception:
                            pass
                    if email:
                        user = db.get_user_by_email(email)

                if user:
                    db.set_plan(user["id"], plan_key)
                    renews_at = None
                    if isinstance(data_obj, dict):
                        renews_at = data_obj.get("current_period_end")
                        if renews_at:
                            try:
                                renews_at = datetime.fromtimestamp(int(renews_at), tz=timezone.utc).isoformat()
                            except Exception:
                                renews_at = str(renews_at)
                    db.upsert_subscription(user["id"], sub_id or "sub_test", plan_key, "active",
                                           renews_at or datetime.now(timezone.utc).isoformat())
                    print(f"[WEBHOOK] upgraded user {user['id']} to {plan_key}")
                else:
                    print(f"[WEBHOOK] no user found for customer {customer_id}")

        elif etype == "customer.subscription.deleted":
            data_obj = event.get("data", {}).get("object", {}) if isinstance(event, dict) else event.data.object
            if isinstance(data_obj, dict):
                customer_id = data_obj.get("customer")
                if customer_id:
                    user = db.get_user_by_stripe_customer(customer_id)
                    if user:
                        db.set_plan(user["id"], "free")
                        db.set_subscription_status(user["id"], "canceled")
                        print(f"[WEBHOOK] downgraded user {user['id']} to free")
    except Exception as e:
        print("[WEBHOOK-HANDLE-ERR]", e)
        return PlainTextResponse(f"webhook error: {e}", status_code=500)
    return PlainTextResponse("ok", status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
