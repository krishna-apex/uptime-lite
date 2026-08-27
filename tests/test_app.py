"""Basic tests for UptimeLite v1.1 — public status pages, ads, billing."""
import os
import tempfile
import db
import app as app_module
from fastapi.testclient import TestClient

# use temp DB
tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
os.environ["DB_PATH"] = tmp.name
# force reinit
import importlib
importlib.reload(db)
importlib.reload(app_module)

client = TestClient(app_module.app)


def register(email="alice@example.com", password="pass1234"):
    return client.post("/register", data={"email": email, "password": password}, follow_redirects=False)


def login(email="alice@example.com", password="pass1234"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_register_and_login():
    r = register()
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    # duplicate
    r2 = register()
    assert r2.status_code == 200
    assert b"already registered" in r2.content.lower()


def test_dashboard_requires_auth():
    c2 = TestClient(app_module.app)
    r = c2.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303


def test_create_monitor_and_slug():
    # login first
    r = login()
    assert r.status_code == 303
    cookies = r.cookies
    # create monitor
    r = client.post("/monitors", data={"name": "My API", "url": "https://example.com", "interval": "60"},
                    cookies=cookies, follow_redirects=False)
    assert r.status_code == 303
    # check DB
    u = db.get_user_by_email("alice@example.com")
    monitors = db.list_monitors(u["id"])
    assert len(monitors) >= 1
    m = monitors[0]
    assert m["slug"] == "my-api"
    # second monitor same name -> slug uniqueness
    r = client.post("/monitors", data={"name": "My API", "url": "https://example2.com", "interval": "60"},
                    cookies=cookies, follow_redirects=False)
    assert r.status_code == 303
    monitors = db.list_monitors(u["id"])
    slugs = [x["slug"] for x in monitors]
    assert "my-api" in slugs
    assert "my-api-2" in slugs


def test_public_status_page():
    u = db.get_user_by_email("alice@example.com")
    monitors = db.list_monitors(u["id"])
    slug = monitors[0]["slug"]
    # public, no auth needed
    c2 = TestClient(app_module.app)
    r = c2.get(f"/status/{slug}")
    assert r.status_code == 200
    assert monitors[0]["name"].encode() in r.content
    # not found
    r = c2.get("/status/not-a-real-slug-xyz")
    assert r.status_code == 404


def test_paused_monitor_hidden():
    u = db.get_user_by_email("alice@example.com")
    m = db.list_monitors(u["id"])[0]
    db.set_monitor_active(m["id"], u["id"], False)
    c2 = TestClient(app_module.app)
    r = c2.get(f"/status/{m['slug']}")
    assert r.status_code == 404
    # reactivate
    db.set_monitor_active(m["id"], u["id"], True)
    r = c2.get(f"/status/{m['slug']}")
    assert r.status_code == 200


def test_pricing_page():
    r = client.get("/pricing")
    assert r.status_code == 200
    assert b"$9" in r.content


def test_billing_requires_auth():
    c2 = TestClient(app_module.app)
    r = c2.get("/billing", follow_redirects=False)
    assert r.status_code == 303


def test_billing_page_authed():
    # need cookies from login
    r = login()
    cookies = r.cookies
    r = client.get("/billing", cookies=cookies)
    assert r.status_code == 200
    assert b"Current plan" in r.content


def test_checkout_redirect():
    r = login()
    cookies = r.cookies
    r = client.post("/billing/checkout", data={"plan": "pro", "interval": "monthly"}, cookies=cookies, follow_redirects=False)
    assert r.status_code == 303
    # without real Stripe, goes to test checkout
    assert "checkout.stripe.com" in r.headers["location"]


def test_webhook_fake_upgrade():
    # simulate checkout.session.completed webhook without Stripe signature
    u = db.get_user_by_email("alice@example.com")
    assert u["plan"] == "free"
    payload = {"type": "checkout.session.completed", "data": {"object": {"customer_email": u["email"], "customer": "cus_test123", "subscription": "sub_test123"}}}
    import json
    r = client.post("/stripe/webhook", content=json.dumps(payload), headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    u2 = db.get_user_by_email("alice@example.com")
    assert u2["plan"] == "pro"
    sub = db.get_subscription(u2["id"])
    assert sub is not None
    assert sub["plan"] == "pro"


def test_uptime_pct_none_when_no_checks():
    u = db.get_user_by_email("alice@example.com")
    m = db.list_monitors(u["id"])[0]
    # clear checks
    conn = db.get_conn()
    conn.execute("DELETE FROM checks WHERE monitor_id=?", (m["id"],))
    conn.commit()
    conn.close()
    assert db.uptime_pct(m["id"], 24) is None


def test_make_slug():
    assert db.make_slug("Hello World!") == "hello-world"
    assert db.make_slug("  API---v2  ") == "api-v2"
    assert db.make_slug("") == "monitor"
