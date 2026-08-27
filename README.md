# UptimeLite — simple uptime monitoring with public status pages

Know when your site is down. In seconds. Built for indie devs and small teams who don't want Datadog's complexity.

**v1.1** adds shareable public status pages (`/status/{slug}`) monetized via Google AdSense, plus Stripe checkout for paid plans. All on the same single-process stack.

## What it does

- Create monitors for any HTTP(S) URL, check every 15–900s
- Email alerts the moment a check transitions to DOWN
- 24h / 7d uptime % on every monitor + detail page with 50 recent checks
- **Public status page per monitor** at `/status/{slug}` — no login required, Google-indexable, ad-ready
- Plans: Free (5 monitors), Pro (50, $9/mo), Team (500, $29/mo) — enforced in app, upgraded via Stripe webhook

## Stack (lightweight, no paid infra required to start)

- **Python FastAPI + SQLite** (stdlib-backed, no Postgres, no Redis)
- **Jinja2 + Tailwind CDN** (no Node build, no React)
- **HTMX** for dashboard interactivity
- **SQLite** single file (`uptime.db` or `DB_PATH` env)
- Background pinger: `asyncio` loop every 15s calling `scheduler.run_once()` (stdlib `urllib` + `smtplib`)
- Optional: `stripe` (install only if you wire production payments), Google AdSense auto-ads script on public pages

**Why this stack?** Single process, runs anywhere (`python app.py`), debuggable with `print()`, survives on a $5/mo VPS. No containers required (but works with Docker). Follows the existing repo's constraints verbatim.

## How it makes money

### 1. Ads (passive, immediate)

Public status pages (`/status/{slug}`) are the ad surface. When `GOOGLE_ADSENSE_ID` is set, three standard AdSense units are rendered (top, middle, bottom) using the standard `adsbygoogle.js` auto-ads integration — no dark patterns, no deceptive placements.

- No traffic yet → allow ~$0–$30/mo. At ~1k daily pageviews → $3–$30/mo. Scales with organic traffic because status pages are indexed by Google.
- You must [apply for AdSense](https://www.google.com/adsense/start/) and replace the env var with your `ca-pub-...` ID. Until approved, pages render without ads (no errors).

### 2. Subscriptions (Stripe)

Free → Pro ($9/mo, 50 monitors, ad-free status pages, custom domains) → Team ($29/mo, 500 monitors, SMS). Stripe Checkout creates the session; the webhook (`POST /stripe/webhook`) flips `users.plan` to `pro` automatically.

- Set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, plus optional `STRIPE_PRICE_PRO_MONTHLY` / `STRIPE_PRICE_PRO_ANNUAL` etc. if you created Prices in Stripe Dashboard.
- Without keys, `/billing/checkout` redirects to `https://checkout.stripe.com/pay/test_uptime_lite` so local dev/testing still passes without real Stripe.

### 3. Upgrade prompts (hybrid)

Free status pages show a subtle footer: "Status page powered by UptimeLite — monitor your site in 10 seconds" linking to `/register`. Dashboard shows upgrade banners when at limit.

**Honest expected revenue:** Small. Most solo-built status tools start at $0–$40/mo ARR (see StatusPage.me — 132 users, $40/mo). Growth is organic over months. The hybrid ads+subs model hedges: ads pay hosting even before first subscriber.

## Quick start

```bash
pip install -r requirements.txt
# optional env
export DB_PATH=./uptime.db
export GOOGLE_ADSENSE_ID=ca-pub-XXXXXXXXXXXX
export STRIPE_SECRET_KEY=sk_test_...
export STRIPE_WEBHOOK_SECRET=whsec_...
export STRIPE_PRICE_PRO_MONTHLY=price_...
export SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@example.com SMTP_PASS=... SMTP_FROM=...

python app.py
# http://localhost:8000
```

### Stripe test (no keys)

```bash
curl -X POST http://localhost:8000/stripe/webhook \
  -H 'Content-Type: application/json' \
  -d '{"type":"checkout.session.completed","data":{"object":{"customer_email":"you@example.com","customer":"cus_test","subscription":"sub_test"}}}'
# -> flips you@example.com to pro
```

## Routes

```
GET  /, /login, /register, /dashboard
GET  /pricing
GET  /billing, POST /billing/checkout, POST /stripe/webhook
POST /monitors, POST /monitors/{id}/delete, POST /monitors/{id}/toggle
GET  /monitors/{id}
GET  /status/{slug}          # public — share this URL with users/customers
```

Public status pages return 404 when the monitor is paused or the slug doesn't exist.

## Tests

```bash
pip install pytest httpx
pytest -q
# 12 tests: register, login, monitor CRUD + slug uniqueness, public status 200/404,
# paused hidden, pricing, billing auth, checkout redirect, webhook upgrade, uptime_pct, make_slug
```

## Passive operation — what still needs a human

| Automated | Needs occasional human |
|---|---|
| 15s pinger loop (all monitors) | VPS health / SSL renewal (certbot cron ~monthly) |
| Ads are served by Google once approved | AdSense application + approval (one-time) |
| Stripe webhook auto-upgrades plans | Stripe account + price creation (one-time) + webhook secret |
| Public pages auto-render from SQLite | DB backup (monthly `sqlite3 uptime.db .dump > backup.sql`) |

No daily manual work. The builder's ongoing job is ~quarterly: check host uptime, rotate backups, respond to Stripe emails.

## Sources (research phase)

- Uptime Robot ~$606k/yr, 6 employees (Growjo); pattern: free tier + paid subscriptions
- Client Uptime — Node.js + SQLite on $6/mo VPS, $5/mo for 500 monitors (Indie Hackers Feb 2026)
- UpOrGone — $9/mo launch Feb 2026, Next.js + Supabase (uporgone.com/blog)
- StatusPage.me — $40/mo ARR, 132 users (Indie Hackers July 2026)
- isitup.com ~$4.7k/yr ads alone (SiteIndices)
- $100k AdSense solo dev (Indie Hackers Jan 2021)

## License

AGPL-3.0 (same as repo). Commercial hosting allowed; source disclosure required on distribution.
