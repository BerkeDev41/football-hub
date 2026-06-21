# FootballHub API (your own backend)

A tiny, zero-dependency proxy + cache that sits in front of
[api-football.com](https://www.api-football.com). Your iOS app talks to **this**
server instead of api-football directly.

## Why this saves you money

api-football bills you **per request**. Today every phone that opens "today's
fixtures" makes its own paid request. With this server in front:

- One upstream request is **shared by all your users** (server-side cache).
- Identical requests that arrive at the same time are **coalesced** into a single
  upstream call (in-flight de-duplication).
- When you hit a rate limit or upstream is down, users still get **slightly stale
  data** instead of errors.
- Your secret api-football key lives on the **server**, not inside the shipped app.

In practice this cuts upstream calls by 10–100× depending on how many users share
the same data, while keeping api-football's full data quality (live events,
lineups, player ratings, standings — everything the app already uses).

It does **not** invent data — you still need an upstream provider. But you now own
the API layer and can later swap/add providers without updating the app.

## Endpoints

- `GET /health` — liveness check (no auth).
- `GET /_stats` — cache hit-rate + real upstream usage today (auth required).
- `GET /<any api-football path>` — e.g. `/fixtures?live=all`, `/standings?...`.
  Returns the **exact same JSON** api-football returns, so the app needs no
  changes beyond pointing at this server.

Response headers: `X-FH-Cache` is `fresh` | `cache` | `stale`, `X-FH-Age` is the
cached age in seconds.

## Run locally

```bash
cd server
export API_SPORTS_KEY="your-key-from-api-football"
export APP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
python3 app.py
# -> FootballHub API listening on :8080
curl localhost:8080/health
curl -H "x-fh-token: $APP_TOKEN" "localhost:8080/fixtures?live=all"
```

Run the offline test (no internet needed):

```bash
python3 smoke_test.py
```

## Deploy to Render — 100% from the browser (no terminal)

A `render.yaml` is included, so Render configures everything automatically.

### Step 1 — Put this code on GitHub (web upload, no git needed)

1. Create a free account at [github.com](https://github.com) → **New repository**
   (name it e.g. `footballhub`, keep it private if you like) → **Create**.
2. On the empty repo page click **Add file → Upload files**.
3. Drag in these files from your `server/` folder:
   `app.py`, `Dockerfile`, `render.yaml`, `README.md`, `.env.example`, `.gitignore`
4. Click **Commit changes**.

### Step 2 — Deploy on Render

1. Sign up at [render.com](https://render.com) (free) and connect your GitHub.
2. **New + → Blueprint** → choose your repo. Render reads `render.yaml`.
3. It will ask for the two secret values:
   - `API_SPORTS_KEY` = your api-football.com key
   - `APP_TOKEN` = any password-like string you invent (you'll reuse it in the app)
4. **Apply / Create** → wait for the build. You'll get a URL like
   `https://footballhub-api.onrender.com`.
5. Test it: open `https://your-url/health` in your browser → `{"status":"ok"}`.

### Step 3 — Stop the free tier from sleeping (optional, free)

Render's free plan sleeps after ~15 min idle (first request after is slow).
Keep it awake with a free uptime pinger — also browser-only:

1. Sign up at [uptimerobot.com](https://uptimerobot.com) (free).
2. Add an **HTTP(s) monitor** pointing at `https://your-url/health`, interval 5 min.

That keeps the server warm during the day at no cost.

### Deploy elsewhere (optional)

The included `Dockerfile` runs on any container host (Railway, a VPS, etc.).
On a VPS: `docker build -t footballhub-api . && docker run -d -p 80:8080 -e API_SPORTS_KEY=... -e APP_TOKEN=... --restart unless-stopped footballhub-api`.

## Point the app at it

In `FootballHub/Secrets.plist` (not committed) add:

```xml
<key>BACKEND_BASE_URL</key>
<string>https://your-deployment-url</string>
<key>APP_TOKEN</key>
<string>the-same-token-you-set-on-the-server</string>
```

When `BACKEND_BASE_URL` is present the app routes everything through your backend
and sends `x-fh-token` instead of the api-football key. Remove it and the app goes
back to calling api-football directly — so it's a safe, reversible switch.

## Scaling notes

- Cache is in-memory per instance. For multiple instances behind a load balancer,
  set `CACHE_MAX_ENTRIES` higher or add a shared Redis later (the cache layer is
  isolated in `Cache` for exactly this reason).
- A single small instance (256–512 MB RAM) comfortably serves thousands of users
  for this workload.
