"""
football-data.org provider — transforms v4 responses into api-football JSON
so the iOS app needs no changes.

Free tier covers 12 top leagues (PL, La Liga, Serie A, Bundesliga, Ligue 1, CL…)
but NOT Süper Lig. Unmapped leagues and endpoints fall back to api-football.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FD_BASE = "https://api.football-data.org/v4"

# api-football league id -> football-data competition code
LEAGUE_TO_FD: dict[int, str] = {
    39: "PL",
    140: "PD",
    135: "SA",
    78: "BL1",
    61: "FL1",
    2: "CL",
    3: "EL",
    88: "DED",
    94: "PPL",
    40: "ELC",
}

FD_TO_LEAGUE: dict[str, int] = {code: af_id for af_id, code in LEAGUE_TO_FD.items()}

# Always served from api-football (not on football-data free tier, or special).
API_ONLY_LEAGUES = {1, 203}  # World Cup, Süper Lig

# League metadata for transformed responses (api-football ids preserved).
LEAGUE_META: dict[int, dict] = {
    39: {"name": "Premier League", "country": "England"},
    140: {"name": "La Liga", "country": "Spain"},
    135: {"name": "Serie A", "country": "Italy"},
    78: {"name": "Bundesliga", "country": "Germany"},
    61: {"name": "Ligue 1", "country": "France"},
    2: {"name": "Champions League", "country": "World"},
    3: {"name": "Europa League", "country": "World"},
    88: {"name": "Eredivisie", "country": "Netherlands"},
    94: {"name": "Primeira Liga", "country": "Portugal"},
    40: {"name": "Championship", "country": "England"},
    203: {"name": "Süper Lig", "country": "Turkey"},
    1: {"name": "World Cup", "country": "World"},
}


class FootballDataProvider:
    def __init__(self, api_key: str, timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout
        self.requests = 0

    def configured(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # Public routing
    # ------------------------------------------------------------------

    def try_fetch(self, path: str, query: str) -> bytes | None:
        """Return api-football-shaped JSON bytes, or None to fall back."""
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))

        if path == "/fixtures":
            body = self._fixtures(params)
        elif path == "/standings":
            body = self._standings(params)
        elif path == "/players/topscorers":
            body = self._scorers(params)
        elif path in ("/fixtures/events", "/fixtures/lineups", "/fixtures/statistics", "/fixtures/players"):
            body = self._empty_detail(path, params)
        elif path.startswith("/players") or path.startswith("/teams") or path.startswith("/transfers"):
            return None  # always api-football
        elif path.startswith("/coachs") or path.startswith("/venues") or path.startswith("/trophies"):
            return None
        else:
            return None

        if body is None:
            return None
        return json.dumps(body).encode("utf-8")

    # ------------------------------------------------------------------
    # Endpoint handlers
    # ------------------------------------------------------------------

    def _fixtures(self, params: dict) -> dict | None:
        if params.get("live") == "all":
            fd = self._fetch_matches({"status": "LIVE,IN_PLAY,PAUSED"})
            return envelope([self._to_fixture(m) for m in fd])

        if fixture_id := params.get("id"):
            try:
                match = self._fetch_json(f"/matches/{fixture_id}")
                return envelope([self._to_fixture(match)])
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                raise

        if params.get("team"):
            return None  # team ids differ between providers

        if params.get("h2h") or params.get("headtohead"):
            return None

        if league_raw := params.get("league"):
            league_id = int(league_raw)
            if league_id in API_ONLY_LEAGUES or league_id not in LEAGUE_TO_FD:
                return None
            code = LEAGUE_TO_FD[league_id]
            season = params.get("season")
            fd_params: dict[str, str] = {}
            if season:
                fd_params["season"] = season
            if params.get("last"):
                fd_params["status"] = "FINISHED"
            if params.get("next"):
                fd_params["status"] = "SCHEDULED,TIMED"
            matches = self._fetch_competition_matches(code, fd_params)
            if params.get("last"):
                matches = sorted(matches, key=lambda m: m.get("utcDate", ""), reverse=True)
                limit = int(params["last"])
                matches = matches[:limit]
            elif params.get("next"):
                matches = sorted(matches, key=lambda m: m.get("utcDate", ""))
                limit = int(params["next"])
                matches = matches[:limit]
            return envelope([self._to_fixture(m, league_id=league_id) for m in matches])

        if date_raw := params.get("date"):
            matches = self._fetch_matches({"dateFrom": date_raw, "dateTo": date_raw})
            return envelope([self._to_fixture(m) for m in matches])

        return None

    def _standings(self, params: dict) -> dict | None:
        league_raw = params.get("league")
        if not league_raw:
            return None
        league_id = int(league_raw)
        if league_id in API_ONLY_LEAGUES or league_id not in LEAGUE_TO_FD:
            return None
        code = LEAGUE_TO_FD[league_id]
        season = params.get("season")
        fd_params = {}
        if season:
            fd_params["season"] = season
        data = self._fetch_json(f"/competitions/{code}/standings", fd_params)
        return envelope([self._to_standings_bundle(data, league_id, int(season) if season else None)])

    def _scorers(self, params: dict) -> dict | None:
        league_raw = params.get("league")
        if not league_raw:
            return None
        league_id = int(league_raw)
        if league_id in API_ONLY_LEAGUES or league_id not in LEAGUE_TO_FD:
            return None
        code = LEAGUE_TO_FD[league_id]
        season = params.get("season")
        fd_params = {}
        if season:
            fd_params["season"] = season
        data = self._fetch_json(f"/competitions/{code}/scorers", fd_params)
        scorers = data.get("scorers", [])
        return envelope([self._to_top_scorer(s, league_id) for s in scorers])

    def _empty_detail(self, path: str, params: dict) -> dict:
        """football-data free tier has no events/lineups/stats — return empty."""
        _ = path, params
        return envelope([])

    def supplement_live_fixtures(self) -> list[dict]:
        """Not used directly — kept for merge helper."""
        return []

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch_json(self, path: str, params: dict | None = None) -> dict:
        url = FD_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url)
        req.add_header("X-Auth-Token", self.api_key)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            self.requests += 1
            return json.loads(resp.read())

    def _fetch_matches(self, params: dict) -> list[dict]:
        data = self._fetch_json("/matches", params)
        return data.get("matches", [])

    def _fetch_competition_matches(self, code: str, params: dict) -> list[dict]:
        data = self._fetch_json(f"/competitions/{code}/matches", params)
        return data.get("matches", [])

    # ------------------------------------------------------------------
    # Transforms
    # ------------------------------------------------------------------

    def _to_fixture(self, match: dict, league_id: int | None = None) -> dict:
        comp = match.get("competition", {})
        code = comp.get("code")
        resolved_league = league_id or FD_TO_LEAGUE.get(code) or comp.get("id", 0)
        meta = LEAGUE_META.get(resolved_league, {})
        score = match.get("score", {})
        ft = score.get("fullTime") or {}
        ht = score.get("halfTime") or {}
        status = _map_status(match.get("status", "SCHEDULED"))
        season = _season_year(match.get("season", {}).get("startDate") or match.get("utcDate"))

        return {
            "fixture": {
                "id": match["id"],
                "date": match.get("utcDate"),
                "timestamp": _to_timestamp(match.get("utcDate")),
                "periods": {"first": None, "second": None},
                "venue": {
                    "id": None,
                    "name": None,
                    "city": None,
                },
                "referee": None,
                "status": status,
            },
            "league": {
                "id": resolved_league,
                "name": meta.get("name") or comp.get("name", "League"),
                "country": meta.get("country") or comp.get("area", {}).get("name", "World"),
                "logo": comp.get("emblem"),
                "flag": None,
                "season": season,
                "round": _round_label(match),
            },
            "teams": {
                "home": _team(match.get("homeTeam", {})),
                "away": _team(match.get("awayTeam", {})),
            },
            "goals": {
                "home": ft.get("home"),
                "away": ft.get("away"),
            },
            "score": {
                "halftime": {"home": ht.get("home"), "away": ht.get("away")},
                "fulltime": {"home": ft.get("home"), "away": ft.get("away")},
            },
        }

    def _to_standings_bundle(self, data: dict, league_id: int, season: int | None) -> dict:
        meta = LEAGUE_META.get(league_id, {})
        comp = data.get("competition", {})
        season_year = season or _season_year(
            data.get("season", {}).get("startDate") or comp.get("lastUpdated")
        )
        tables = []
        for block in data.get("standings", []):
            rows = []
            for row in block.get("table", []):
                team = row.get("team", {})
                rows.append({
                    "rank": row.get("position"),
                    "team": _team(team),
                    "points": row.get("points", 0),
                    "goalsDiff": row.get("goalDifference", 0),
                    "form": row.get("form"),
                    "description": row.get("description"),
                    "group": block.get("group"),
                    "all": {
                        "played": row.get("playedGames", 0),
                        "win": row.get("won", 0),
                        "draw": row.get("draw", 0),
                        "lose": row.get("lost", 0),
                        "goals": {
                            "for": row.get("goalsFor", 0),
                            "against": row.get("goalsAgainst", 0),
                        },
                    },
                })
            if rows:
                tables.append(rows)

        return {
            "league": {
                "id": league_id,
                "name": meta.get("name") or comp.get("name", "League"),
                "country": meta.get("country") or comp.get("area", {}).get("name", "World"),
                "logo": comp.get("emblem"),
                "season": season_year,
                "standings": tables or [[]],
            }
        }

    def _to_top_scorer(self, entry: dict, league_id: int) -> dict:
        player = entry.get("player", {})
        team = entry.get("team", {})
        return {
            "player": {
                "id": player.get("id"),
                "name": player.get("name"),
                "firstname": player.get("firstName"),
                "lastname": player.get("lastName"),
                "age": None,
                "nationality": player.get("nationality"),
                "photo": None,
            },
            "statistics": [{
                "team": _team(team),
                "league": {
                    "id": league_id,
                    "name": LEAGUE_META.get(league_id, {}).get("name", "League"),
                    "country": LEAGUE_META.get(league_id, {}).get("country", "World"),
                    "logo": None,
                    "flag": None,
                    "season": None,
                },
                "games": {
                    "appearences": entry.get("playedMatches"),
                    "lineups": None,
                    "minutes": None,
                    "position": player.get("position"),
                },
                "goals": {
                    "total": entry.get("goals"),
                    "assists": entry.get("assists"),
                },
            }],
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def envelope(response: list) -> dict:
    return {
        "get": "football-data.org",
        "parameters": {},
        "errors": [],
        "results": len(response),
        "paging": {"current": 1, "total": 1},
        "response": response,
    }


def _team(raw: dict) -> dict:
    return {
        "id": raw.get("id", 0),
        "name": raw.get("name") or raw.get("shortName") or "Team",
        "logo": raw.get("crest"),
    }


def _map_status(fd_status: str) -> dict:
    table = {
        "SCHEDULED": ("Not Started", "NS"),
        "TIMED": ("Not Started", "NS"),
        "IN_PLAY": ("In Play", "1H"),
        "PAUSED": ("Half Time", "HT"),
        "FINISHED": ("Match Finished", "FT"),
        "POSTPONED": ("Match Postponed", "PST"),
        "SUSPENDED": ("Match Suspended", "SUSP"),
        "CANCELLED": ("Match Cancelled", "CANC"),
        "AWARDED": ("Match Finished", "FT"),
    }
    long, short = table.get(fd_status, ("Unknown", "NS"))
    elapsed = 45 if short in ("1H", "HT") else (90 if short == "FT" else None)
    return {"long": long, "short": short, "elapsed": elapsed, "extra": None}


def _round_label(match: dict) -> str | None:
    if match.get("matchday"):
        return f"Matchday {match['matchday']}"
    return match.get("stage")


def _season_year(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(str(value)[:4])
    except ValueError:
        return None


def _to_timestamp(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return None


def merge_live_envelopes(primary: dict, supplement: dict) -> dict:
    """Merge two api-football-shaped live fixture responses."""
    merged = list(primary.get("response", [])) + list(supplement.get("response", []))
    body = envelope(merged)
    body["get"] = "hybrid"
    return body
