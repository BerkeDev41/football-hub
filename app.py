#!/usr/bin/env python3
"""
FootballHub API — your own backend in front of api-football.com.

It transparently proxies the same paths your iOS app already calls
(e.g. /fixtures, /standings, /players ...) and adds a shared server-side
cache so ONE upstream request serves ALL your users. That is what cuts the
api-football bill: 1,000 phones opening "today's fixtures" become a single
upstream call instead of 1,000.

It also:
  * keeps your api-football key on the server (out of the shipped app),
  * coalesces identical concurrent requests (in-flight de-duplication),
  * serves slightly stale data when upstream is rate-limited or down,
  * optionally requires an app token so randoms can't burn your quota.

Zero third-party dependencies — runs on any Python 3.8+.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8080"))
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://v3.football.api-sports.io").rstrip("/")
UPSTREAM_KEY = os.environ.get("API_SPORTS_KEY", "")
UPSTREAM_KEY_HEADER = os.environ.get("API_SPORTS_KEY_HEADER", "x-apisports-key")
# Optional shared secret your app must send as `x-fh-token`. Leave empty to disable.
APP_TOKEN = os.environ.get("APP_TOKEN", "")
CACHE_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", "500"))
# How long stale entries may still be served if upstream fails (seconds).
STALE_GRACE = int(os.environ.get("STALE_GRACE", "86400"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "15"))

START_TIME = time.time()


# ---------------------------------------------------------------------------
# Per-path cache TTL — mirrors the iOS app's APIConfiguration.cacheTTL(for:)
# ---------------------------------------------------------------------------

def ttl_for(path: str) -> float:
    if path == "/fixtures/events":
        return 8
    if path == "/fixtures/lineups":
        return 300
    if path == "/fixtures/statistics":
        return 60
    if path == "/fixtures/players":
        return 45
    if "/fixtures" in path:
        return 12
    if "/standings" in path or "/topscorers" in path or "/topassists" in path:
        return 3600
    if "/players/profiles" in path:
        return 1800
    if "/players" in path or "/transfers" in path:
        return 7200
    return 600


# ---------------------------------------------------------------------------
# In-memory cache with TTL, stale-grace and in-flight de-duplication
# ---------------------------------------------------------------------------

class CacheEntry:
    __slots__ = ("body", "status", "content_type", "saved_at", "ttl")

    def __init__(self, body: bytes, status: int, content_type: str, ttl: float):
        self.body = body
        self.status = status
        self.content_type = content_type
        self.saved_at = time.time()
        self.ttl = ttl

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.saved_at) < self.ttl

    @property
    def age(self) -> float:
        return time.time() - self.saved_at

    def servable_stale(self) -> bool:
        return self.age < (self.ttl + STALE_GRACE)


class Cache:
    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._store: dict[str, CacheEntry] = {}
        self._guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self.hits = 0
        self.misses = 0

    def key_lock(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def get(self, key: str) -> CacheEntry | None:
        with self._guard:
            return self._store.get(key)

    def set(self, key: str, entry: CacheEntry) -> None:
        with self._guard:
            if len(self._store) >= self.max_entries and key not in self._store:
                oldest = min(self._store, key=lambda k: self._store[k].saved_at)
                self._store.pop(oldest, None)
            self._store[key] = entry

    def stats(self) -> dict:
        with self._guard:
            return {
                "entries": len(self._store),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / (self.hits + self.misses), 3) if (self.hits + self.misses) else 0.0,
            }


CACHE = Cache(CACHE_MAX_ENTRIES)


# ---------------------------------------------------------------------------
# Upstream usage tracking (per-day) so you can watch your real bill
# ---------------------------------------------------------------------------

class Usage:
    def __init__(self):
        self._guard = threading.Lock()
        self._day = date.today().isoformat()
        self.upstream_today = 0
        self.upstream_total = 0
        self.last_rate_limit_headers: dict[str, str] = {}

    def _roll(self):
        today = date.today().isoformat()
        if today != self._day:
            self._day = today
            self.upstream_today = 0

    def record_upstream(self):
        with self._guard:
            self._roll()
            self.upstream_today += 1
            self.upstream_total += 1

    def record_headers(self, headers):
        captured = {}
        for k, v in headers.items():
            lk = k.lower()
            if "ratelimit" in lk or "x-requests" in lk:
                captured[k] = v
        if captured:
            with self._guard:
                self.last_rate_limit_headers = captured

    def stats(self) -> dict:
        with self._guard:
            self._roll()
            return {
                "day": self._day,
                "upstream_today": self.upstream_today,
                "upstream_total": self.upstream_total,
                "upstream_rate_limit": self.last_rate_limit_headers,
            }


USAGE = Usage()


# ---------------------------------------------------------------------------
# Shared fan votes (FootballHub community board)
# ---------------------------------------------------------------------------

VOTES_FILE = os.environ.get(
    "VOTES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "votes.json"),
)
VALID_VOTE_SIDES = {"home", "away", "draw"}


class VoteStore:
    """Persists one vote per user per fixture; keeps a recent-activity feed."""

    def __init__(self, path: str):
        self.path = path
        self._guard = threading.Lock()
        self._data: dict = {"fixtures": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and "fixtures" in loaded:
                self._data = loaded
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"[votes] load failed: {exc}", flush=True)

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh)
        os.replace(tmp, self.path)

    @staticmethod
    def _fixture_key(fixture_id: int) -> str:
        return str(fixture_id)

    def cast(self, fixture_id: int, user_id: str, nickname: str, side: str) -> dict:
        if side not in VALID_VOTE_SIDES:
            raise ValueError("invalid side")
        if not user_id or len(user_id) > 64:
            raise ValueError("invalid user id")
        nickname = (nickname or "Fan")[:32]

        key = self._fixture_key(fixture_id)
        with self._guard:
            fixtures = self._data.setdefault("fixtures", {})
            bucket = fixtures.setdefault(key, {"votes": {}, "recent": []})
            votes: dict = bucket["votes"]
            recent: list = bucket["recent"]

            previous = votes.get(user_id)
            if previous == side:
                return self._snapshot(fixture_id, user_id)

            votes[user_id] = side
            recent = [r for r in recent if r.get("user_id") != user_id]
            recent.insert(0, {
                "user_id": user_id,
                "nickname": nickname,
                "side": side,
                "voted_at": time.time(),
            })
            bucket["recent"] = recent[:100]
            self._save()
            return self._snapshot(fixture_id, user_id)

    def snapshot(self, fixture_id: int, user_id: str | None = None) -> dict:
        with self._guard:
            return self._snapshot(fixture_id, user_id)

    def _snapshot(self, fixture_id: int, user_id: str | None = None) -> dict:
        key = self._fixture_key(fixture_id)
        bucket = self._data.get("fixtures", {}).get(key, {"votes": {}, "recent": []})
        votes: dict = bucket.get("votes", {})
        tally = {"home": 0, "away": 0, "draw": 0}
        for side in votes.values():
            if side in tally:
                tally[side] += 1

        recent = []
        for row in bucket.get("recent", []):
            recent.append({
                "user_id": row.get("user_id", ""),
                "nickname": row.get("nickname", "Fan"),
                "side": row.get("side", "home"),
                "voted_at": row.get("voted_at", 0),
                "is_you": bool(user_id and row.get("user_id") == user_id),
            })

        your_side = votes.get(user_id) if user_id else None
        return {
            "fixture_id": fixture_id,
            "tally": tally,
            "total": sum(tally.values()),
            "recent": recent,
            "your_side": your_side,
        }


VOTES = VoteStore(VOTES_FILE)


# ---------------------------------------------------------------------------
# Upstream fetch
# ---------------------------------------------------------------------------

def cache_key(path: str, query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    pairs.sort()
    return path + "?" + urllib.parse.urlencode(pairs)


def fetch_upstream(path: str, query: str) -> CacheEntry:
    url = UPSTREAM_BASE + path
    if query:
        url += "?" + query
    req = urllib.request.Request(url)
    if UPSTREAM_KEY:
        req.add_header(UPSTREAM_KEY_HEADER, UPSTREAM_KEY)
    req.add_header("Accept", "application/json")

    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
        body = resp.read()
        status = resp.getcode()
        content_type = resp.headers.get("Content-Type", "application/json")
        USAGE.record_upstream()
        USAGE.record_headers(resp.headers)

    return CacheEntry(body, status, content_type, ttl_for(path))


def get_or_fetch(path: str, query: str) -> tuple[CacheEntry, str]:
    """Return (entry, source) where source is 'fresh', 'cache', 'revalidated' or 'stale'."""
    key = cache_key(path, query)

    entry = CACHE.get(key)
    if entry and entry.is_fresh:
        CACHE.hits += 1
        return entry, "cache"

    lock = CACHE.key_lock(key)
    with lock:
        entry = CACHE.get(key)
        if entry and entry.is_fresh:
            CACHE.hits += 1
            return entry, "cache"

        CACHE.misses += 1
        try:
            fresh = fetch_upstream(path, query)
            CACHE.set(key, fresh)
            return fresh, "fresh"
        except Exception as exc:  # noqa: BLE001
            if entry and entry.servable_stale():
                return entry, "stale"
            raise exc


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "FootballHubAPI/1.0"
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict, extra_headers: dict | None = None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not APP_TOKEN:
            return True
        return self.headers.get("x-fh-token", "") == APP_TOKEN

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def _handle_votes_get(self, query: str) -> None:
        params = dict(urllib.parse.parse_qsl(query))
        fixture_raw = params.get("fixture", "")
        try:
            fixture_id = int(fixture_raw)
        except ValueError:
            self._send_json(400, {"error": "fixture query param required"})
            return
        user_id = self.headers.get("x-fh-user-id", "")
        payload = VOTES.snapshot(fixture_id, user_id or None)
        self._send_json(200, payload)

    def _handle_votes_post(self) -> None:
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        try:
            fixture_id = int(body.get("fixture_id"))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "fixture_id required"})
            return

        side = str(body.get("side", "")).lower()
        nickname = str(body.get("nickname", "Fan"))
        user_id = self.headers.get("x-fh-user-id", "")
        if not user_id:
            self._send_json(400, {"error": "x-fh-user-id header required"})
            return

        try:
            payload = VOTES.cast(fixture_id, user_id, nickname, side)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        self._send_json(200, payload)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = parsed.query

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "uptime_seconds": round(time.time() - START_TIME, 1),
                "upstream_configured": bool(UPSTREAM_KEY),
            })
            return

        if path == "/_stats":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, {"cache": CACHE.stats(), "usage": USAGE.stats()})
            return

        if path == "/" or path == "":
            self._send_json(200, {
                "name": "FootballHub API",
                "endpoints": ["/health", "/_stats", "/fh/votes", "/<api-football path>"],
            })
            return

        if path == "/fh/votes":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._handle_votes_get(query)
            return

        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        if not UPSTREAM_KEY:
            self._send_json(500, {"error": "server missing API_SPORTS_KEY"})
            return

        try:
            entry, source = get_or_fetch(path, query)
            self._send_raw(
                entry.status,
                entry.body,
                entry.content_type,
                extra_headers={
                    "X-FH-Cache": source,
                    "X-FH-Age": str(round(entry.age, 1)),
                    "Cache-Control": f"public, max-age={int(max(ttl_for(path) - entry.age, 0))}",
                },
            )
        except urllib.error.HTTPError as exc:
            self._send_json(exc.code, {"error": f"upstream {exc.code}"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": "upstream unavailable", "detail": str(exc)})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/fh/votes":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._handle_votes_post()
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"FootballHub API listening on :{PORT}", flush=True)
    print(f"  upstream      = {UPSTREAM_BASE}", flush=True)
    print(f"  key set       = {bool(UPSTREAM_KEY)}", flush=True)
    print(f"  app token     = {'on' if APP_TOKEN else 'off'}", flush=True)
    print(f"  cache entries = {CACHE_MAX_ENTRIES}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
