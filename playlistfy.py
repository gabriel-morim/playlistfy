#!/usr/bin/env python3
"""
playlistfy — build niche playlists from your own listening.

  1. SEEDS      your Spotify liked songs + top tracks, plus what you actually
                listen to (Last.fm scrobbles and/or the --harvest log), plus an
                optional full streaming history export
  2. FILTER     keep seeds whose artist is tagged with your niche on Last.fm
  3. DISCOVER   Last.fm similar tracks, falling back to similar artists and a
                mix of their hits and deeper cuts (--depth)
  4. RANK       score candidates by how many of your seeds point to them,
                weighted by how much you play those seeds; tag-check them; cap
                tracks per artist; rotate out tracks already suggested; optional
                Claude rerank by a vibe prompt
  5. MATCH      find each pick on Spotify (strict title/artist matching)
  6. WRITE      create a private playlist, or refresh an existing one

Uses Spotify's post-February-2026 endpoints (/me/playlists,
/playlists/{id}/items). Spotify's own recommendation endpoints are gone,
which is why discovery comes from Last.fm.

Quick start
  pip install -r requirements.txt
  cp .env.example .env                       # fill in your keys
  python playlistfy.py --list-tags           # see what your library is tagged as
  python playlistfy.py --tags "deep house" --dry-run
  python playlistfy.py --tags "house,deep house" --save-profile house --name "House"
  python playlistfy.py --profile house       # refresh it any time

Profiles (profiles.json) store a playlist's options and its playlist ID, so
each genre is one short command. Options you pass on the command line override
the profile for that run; add --save-profile NAME to store them.

See --help for all options.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import csv
import glob
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / ".cache"


# ==========================================================================
# Text normalisation and matching
# ==========================================================================

_PAREN_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(original mix|radio edit|remaster(ed)?|extended mix|"
    r"club mix|album version|single version|mixed)[^\)\]]*[\)\]]", re.I)
_DASH_NOISE = re.compile(r"\s+-\s+(original mix|radio edit|remaster(ed)?.*|extended mix|"
                         r"album version|single version|mixed)$", re.I)
# versions cut from continuous DJ-mix compilations: short and crossfaded
_DJ_MIX = re.compile(r"(\s-\s*mixed\s*$|[\(\[]\s*mixed\s*[\)\]])", re.I)


def is_dj_mix(title: str) -> bool:
    return bool(_DJ_MIX.search(title or ""))
_FEAT = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring)\b.*?([\)\]]|$)", re.I)


def norm(s: str) -> str:
    """Lowercase, strip accents, mix suffixes and 'feat.' credits."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = _PAREN_NOISE.sub("", s)
    s = _DASH_NOISE.sub("", s)
    s = _FEAT.sub("", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def lookup_title(title: str) -> str:
    """Title as Last.fm is likely to know it: no 'feat.', no 'Original Mix' etc.
    Keeps remix names, since those are different tracks."""
    t = _PAREN_NOISE.sub("", title or "")
    t = _DASH_NOISE.sub("", t)
    t = _FEAT.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def key(artist: str, title: str) -> str:
    return f"{norm(artist)}|{norm(title)}"


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def is_remix(title: str) -> bool:
    return bool(re.search(r"\b(remix|rework|edit|dub|vip)\b", title, re.I))


# ==========================================================================
# Small JSON cache so repeated runs don't re-hit Last.fm
# ==========================================================================

class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                self.data = {}
        self.dirty = 0

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v):
        self.data[k] = v
        self.dirty += 1
        if self.dirty >= 50:
            self.save()

    def save(self):
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
            self.dirty = 0


# ==========================================================================
# Spotify (auth via spotipy PKCE, calls made directly against the
# current endpoints)
# ==========================================================================

SPOTIFY_SCOPES = ("user-library-read user-top-read user-read-recently-played "
                  "playlist-modify-private playlist-modify-public playlist-read-private")


class Spotify:
    API = "https://api.spotify.com/v1"

    def __init__(self, client_id: str, redirect_uri: str):
        from spotipy.oauth2 import SpotifyPKCE
        CACHE_DIR.mkdir(exist_ok=True)
        self.auth = SpotifyPKCE(client_id=client_id, redirect_uri=redirect_uri,
                                scope=SPOTIFY_SCOPES,
                                cache_path=str(CACHE_DIR / "spotify_token.json"))
        self.s = requests.Session()

    def _req(self, method: str, path: str, **kw):
        url = path if path.startswith("http") else f"{self.API}{path}"
        for attempt in range(6):
            token = self.auth.get_access_token()
            r = self.s.request(method, url, headers={"Authorization": f"Bearer {token}"},
                               timeout=20, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2")) + 1
                print(f"  Spotify rate limit, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Spotify {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"Spotify {method} {path} kept failing")

    def saved_tracks(self, max_tracks: int = 5000):
        out, url = [], "/me/tracks?limit=50"
        while url and len(out) < max_tracks:
            page = self._req("GET", url)
            out += [it["track"] for it in page.get("items", []) if it.get("track")]
            url = page.get("next")
        return out

    def top_tracks(self, time_range: str):
        return self._req("GET", f"/me/top/tracks?limit=50&time_range={time_range}").get("items", [])

    def recently_played(self, after_ms: int | None = None):
        """Last (up to) 50 tracks played, oldest first. Spotify only keeps 50."""
        params = {"limit": 50}
        if after_ms:
            params["after"] = after_ms
        items = self._req("GET", "/me/player/recently-played", params=params).get("items", [])
        return sorted(items, key=lambda it: it["played_at"])

    def search_track(self, artist: str, title: str):
        q = f'track:"{title}" artist:"{artist}"'
        res = self._req("GET", "/search", params={"q": q, "type": "track", "limit": 10})
        items = res.get("tracks", {}).get("items", [])
        if not items:  # looser query as fallback
            res = self._req("GET", "/search", params={"q": f"{artist} {title}", "type": "track", "limit": 10})
            items = res.get("tracks", {}).get("items", [])
        return items

    def find_album(self, artist: str, album: str) -> dict | None:
        """Best-matching album for 'artist' + 'album name', or None."""
        best, best_score = None, 0.0
        for q in (f'album:"{album}" artist:"{artist}"', f"{artist} {album}"):
            res = self._req("GET", "/search", params={"q": q, "type": "album", "limit": 10})
            for al in res.get("albums", {}).get("items", []):
                if not al:
                    continue
                a_score = max(sim(artist, a["name"]) for a in al["artists"]) if artist else 1.0
                score = 0.6 * sim(album, al["name"]) + 0.4 * a_score
                if score > best_score:
                    best, best_score = al, score
            if best_score >= 0.85:
                break
        return best if best_score >= 0.75 else None

    def album_tracks(self, album_id: str) -> list[dict]:
        out, url = [], f"/albums/{album_id}/tracks?limit=50"
        while url:
            page = self._req("GET", url)
            out += [t for t in page.get("items", []) if t]
            url = page.get("next")
        return out

    def create_playlist(self, name: str, description: str, public: bool = False) -> dict:
        return self._req("POST", "/me/playlists",
                         json={"name": name, "description": description, "public": public})

    def replace_items(self, playlist_id: str, uris: list[str]):
        """Replace playlist contents (first 100 via PUT, rest via POST)."""
        self._req("PUT", f"/playlists/{playlist_id}/items", json={"uris": uris[:100]})
        self.add_items(playlist_id, uris[100:])

    def add_items(self, playlist_id: str, uris: list[str]):
        for i in range(0, len(uris), 100):
            self._req("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + 100]})


# ==========================================================================
# Last.fm
# ==========================================================================

class LastFM:
    API = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key: str, cache: Cache, min_interval: float = 0.22):
        self.key = api_key
        self.cache = cache
        self.min_interval = min_interval  # stay under ~5 req/s
        self._last = 0.0
        self.s = requests.Session()

    def _call(self, method: str, **params):
        ck = method + "|" + json.dumps(params, sort_keys=True)
        hit = self.cache.get(ck)
        if hit is not None:
            return hit
        for attempt in range(4):
            dt = time.time() - self._last
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            self._last = time.time()
            try:
                r = self.s.get(self.API, params={"method": method, "api_key": self.key,
                                                 "format": "json", "autocorrect": 1, **params},
                               timeout=20)
                data = r.json()
            except (requests.RequestException, ValueError):
                time.sleep(1 + attempt)
                continue
            if "error" in data:
                if data["error"] in (11, 16, 29):  # service offline / temp error / rate limit
                    time.sleep(2 + 2 * attempt)
                    continue
                data = {}  # e.g. 6 = not found; cache the miss
            self.cache.set(ck, data)
            return data
        return {}

    def artist_tags(self, artist: str, top_n: int = 10) -> list[str]:
        d = self._call("artist.getTopTags", artist=artist)
        tags = d.get("toptags", {}).get("tag", []) if d else []
        if isinstance(tags, dict):
            tags = [tags]
        return [t["name"].lower().strip() for t in tags[:top_n] if int(t.get("count", 0)) >= 5]

    def similar_artists(self, artist: str, limit: int = 15):
        d = self._call("artist.getSimilar", artist=artist, limit=limit)
        arts = d.get("similarartists", {}).get("artist", []) if d else []
        if isinstance(arts, dict):
            arts = [arts]
        return [{"artist": a["name"], "match": float(a.get("match", 0))} for a in arts]

    def artist_top_tracks(self, artist: str, limit: int = 5):
        """Artist's tracks by Last.fm popularity: [{title, listeners}], most popular first."""
        d = self._call("artist.getTopTracks", artist=artist, limit=limit)
        tracks = d.get("toptracks", {}).get("track", []) if d else []
        if isinstance(tracks, dict):
            tracks = [tracks]
        return [{"title": t["name"], "listeners": int(t.get("listeners", 0) or 0)}
                for t in tracks[:limit]]

    def user_scrobbles(self, user: str, since_ts: int, max_pages: int = 50):
        """All scrobbles for a Last.fm user since a unix timestamp (not cached)."""
        out, page = [], 1
        while page <= max_pages:
            dtl = time.time() - self._last
            if dtl < self.min_interval:
                time.sleep(self.min_interval - dtl)
            self._last = time.time()
            r = self.s.get(self.API, params={
                "method": "user.getRecentTracks", "user": user, "api_key": self.key,
                "format": "json", "limit": 200, "page": page, "from": since_ts}, timeout=20)
            d = r.json()
            if "error" in d:
                raise RuntimeError(f"Last.fm user.getRecentTracks: {d.get('message')}")
            rt = d.get("recenttracks", {})
            tracks = rt.get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            for t in tracks:
                if t.get("@attr", {}).get("nowplaying"):
                    continue
                out.append({"artist": t["artist"].get("#text") or t["artist"].get("name", ""),
                            "title": t["name"], "ts": int(t.get("date", {}).get("uts", 0))})
            if page >= int(rt.get("@attr", {}).get("totalPages", 1) or 1):
                break
            page += 1
        return out

    def track_tags(self, artist: str, title: str, top_n: int = 10) -> list[str]:
        d = self._call("track.getTopTags", artist=artist, track=title)
        tags = d.get("toptags", {}).get("tag", []) if d else []
        if isinstance(tags, dict):
            tags = [tags]
        return [t["name"].lower().strip() for t in tags[:top_n] if int(t.get("count", 0)) >= 5]

    def similar_tracks(self, artist: str, title: str, limit: int = 30):
        d = self._call("track.getSimilar", artist=artist, track=title, limit=limit)
        tracks = d.get("similartracks", {}).get("track", []) if d else []
        if isinstance(tracks, dict):
            tracks = [tracks]
        return [{"artist": t["artist"]["name"], "title": t["name"],
                 "match": float(t.get("match", 0))} for t in tracks]


# ==========================================================================
# Pipeline
# ==========================================================================

@dataclass
class Seed:
    artist: str
    title: str
    weight: float = 0.0
    uri: str | None = None
    sources: set = field(default_factory=set)


@dataclass
class Candidate:
    artist: str
    title: str
    score: float = 0.0
    support: int = 0
    via: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    uri: str | None = None
    spotify_name: str = ""
    note: str = ""


def _add_seed(seeds: dict, artist: str, title: str, w: float, src: str, uri=None):
    k = key(artist, title)
    s = seeds.get(k) or Seed(artist, title)
    s.weight += w
    s.sources.add(src)
    s.uri = s.uri or uri
    seeds[k] = s


def collect_seeds(sp: Spotify | None, history_glob: str | None, use_liked=True, use_top=True,
                  listened: dict | None = None) -> dict:
    seeds: dict[str, Seed] = {}
    if sp and use_liked:
        liked = sp.saved_tracks()
        print(f"  liked songs: {len(liked)}")
        for t in liked:
            _add_seed(seeds, t["artists"][0]["name"], t["name"], 1.0, "liked", t["uri"])
    if sp and use_top:
        for rng, w in (("short_term", 3.0), ("medium_term", 2.5), ("long_term", 2.0)):
            top = sp.top_tracks(rng)
            print(f"  top tracks ({rng}): {len(top)}")
            for rank, t in enumerate(top):
                _add_seed(seeds, t["artists"][0]["name"], t["name"],
                          w * (1 - rank / 100), f"top_{rng}", t["uri"])
    if listened:
        for (artist, title, uri), n in listened.items():
            _add_seed(seeds, artist, title, 1.0 + math.log1p(n), "listened", uri)
    if history_glob:
        plays = load_history(history_glob)
        print(f"  streaming history: {len(plays)} distinct tracks")
        for (artist, title, uri), n in plays.items():
            _add_seed(seeds, artist, title, math.log1p(n), "history", uri)
    return seeds


LOG_NAME = "listening_log.jsonl"


def harvest(sp: Spotify, log_path: Path) -> int:
    """Append Spotify's recently-played list to a local log. Spotify only keeps
    the last 50 plays (and only logs plays over ~30 s, so instant skips never
    appear), so run this every hour or two, e.g. from a scheduler."""
    seen = set()
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                seen.add(json.loads(line)["played_at"])
    new = []
    for it in sp.recently_played():
        if it["played_at"] in seen:
            continue
        t = it["track"]
        new.append({"played_at": it["played_at"], "artist": t["artists"][0]["name"],
                    "title": t["name"], "uri": t["uri"],
                    "context": (it.get("context") or {}).get("type")})
    if new:
        with log_path.open("a", encoding="utf-8") as f:
            for row in new:
                f.write(json.dumps(row) + "\n")
    return len(new)


def _iso_ts(ts: str) -> float:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def load_listening(log_path: Path | None, lf: LastFM | None, lastfm_user: str | None,
                   days: int, min_plays: int) -> dict:
    """Plays in the last `days` from the harvest log and/or Last.fm scrobbles.
    Both sources usually record the same plays, so per track we take the larger
    count rather than adding them. Returns {(artist, title, uri|None): plays}."""
    since = time.time() - days * 86400
    from_log: dict = defaultdict(int)
    from_lfm: dict = defaultdict(int)
    info: dict = {}
    if log_path and log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if _iso_ts(r["played_at"]) < since:
                continue
            k = key(r["artist"], r["title"])
            from_log[k] += 1
            info[k] = (r["artist"], r["title"], r.get("uri"))
        print(f"  harvest log: {sum(from_log.values())} plays in the last {days} days")
    if lf and lastfm_user:
        scr = lf.user_scrobbles(lastfm_user, int(since))
        print(f"  Last.fm scrobbles: {len(scr)} in the last {days} days")
        for r in scr:
            k = key(r["artist"], r["title"])
            from_lfm[k] += 1
            info.setdefault(k, (r["artist"], r["title"], None))
    out = {}
    for k in info:
        n = max(from_log.get(k, 0), from_lfm.get(k, 0))
        if n >= min_plays:
            out[info[k]] = n
    return out


def load_history(pattern: str, min_ms: int = 30_000) -> dict:
    """Spotify 'Extended streaming history' export: Streaming_History_Audio_*.json.
    Counts plays of at least 30 s."""
    counts: dict = defaultdict(int)
    files = sorted(glob.glob(pattern))
    if not files:
        sys.exit(f"No history files match {pattern!r}")
    for f in files:
        for row in json.loads(Path(f).read_text(encoding="utf-8")):
            title = row.get("master_metadata_track_name")
            artist = row.get("master_metadata_album_artist_name")
            if not title or not artist or row.get("ms_played", 0) < min_ms:
                continue
            counts[(artist, title, row.get("spotify_track_uri"))] += 1
    return counts


TAG_DEPTH = 6  # only an artist's top N tags count (set by --tag-depth)


def _first_hit(tags: list[str], words: list[str]):
    for i, t in enumerate(tags[:TAG_DEPTH]):
        if any(w == t or w in t for w in words):
            return i
    return None


def tag_match(tags: list[str], want: list[str], avoid: list[str]) -> bool:
    """Artist fits if a wanted tag is in its top tags AND ranks above any
    avoided tag. So an artist tagged [house, electronic, hip-hop] passes with
    --avoid-tags hip-hop, but [rap, trap, ..., house] does not."""
    if not want:  # no niche tags: only the avoid list applies
        return _first_hit(tags, avoid) is None if avoid else True
    iw = _first_hit(tags, want)
    if iw is None:
        return False
    ia = _first_hit(tags, avoid) if avoid else None
    return ia is None or ia > iw


def filter_seeds(seeds: dict, lf: LastFM, want, avoid, artist_tag_cache: dict,
                 force: set | None = None, report_dir: Path | None = None,
                 trust_untagged_listened: bool = False) -> list[Seed]:
    force = force or set()
    artists = sorted({s.artist for s in seeds.values()})
    print(f"  checking tags for {len(artists)} artists on Last.fm...")
    for i, a in enumerate(artists, 1):
        if a not in artist_tag_cache:
            artist_tag_cache[a] = lf.artist_tags(a)
        if i % 50 == 0:
            print(f"    {i}/{len(artists)}", end="\r")

    def fits(artist):
        return norm(artist) in force or tag_match(artist_tag_cache[artist], want, avoid)

    def keep(s):
        if fits(s.artist):
            return True
        # small artists often have no Last.fm tags at all; if you listened
        # to them in a session, you can choose to trust that
        return trust_untagged_listened and "listened" in s.sources and not artist_tag_cache[s.artist]

    kept = [s for s in seeds.values() if keep(s)]
    kept.sort(key=lambda s: -s.weight)

    if report_dir:
        with (report_dir / "seeds.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["artist", "title", "weight", "sources", "tags"])
            for s in kept:
                w.writerow([s.artist, s.title, f"{s.weight:.2f}", " ".join(sorted(s.sources)),
                            ", ".join(artist_tag_cache[s.artist][:6])])
        # artists that carry a wanted tag somewhere but were left out
        misses = defaultdict(list)
        for s in seeds.values():
            tags = artist_tag_cache[s.artist]
            if not fits(s.artist) and any(any(wt == t or wt in t for wt in want) for t in tags):
                misses[s.artist].append(s.title)
        with (report_dir / "near_misses.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["artist", "your_tracks", "reason", "tags", "example_titles"])
            for artist, titles in sorted(misses.items(), key=lambda x: -len(x[1])):
                tags = artist_tag_cache[artist]
                iw = next(i for i, t in enumerate(tags) if any(wt == t or wt in t for wt in want))
                reason = (f"niche tag is #{iw + 1}, beyond --tag-depth {TAG_DEPTH}" if iw >= TAG_DEPTH
                          else "an --avoid-tags tag ranks higher")
                w.writerow([artist, len(titles), reason, ", ".join(tags), " | ".join(titles[:3])])
        if misses:
            print(f"  {len(misses)} artists with a niche tag were left out -> near_misses.csv "
                  f"(add any you want with --force-artists)")
    return kept


DEPTH_PRESETS = {
    # (tracks fetched per similar artist, ranks always skipped, tracks taken)
    "hits": (4, 0, 4),
    "mixed": (25, 0, 4),   # 1 of the top 3 + 3 from deeper in the catalogue
    "deep": (40, 5, 4),    # skip each artist's 5 biggest tracks entirely
}


def pick_depth(tracks: list[dict], depth: str, rng: random.Random,
               n: int | None = None) -> list[tuple[str, float]]:
    """Choose which of an artist's tracks to use. Returns [(title, weight)].
    hits = their biggest; mixed = a few hits + deeper cuts; deep = skip the
    5 biggest. Deeper tracks are weighted only slightly lower."""
    if not tracks:
        return []
    fetch, skip, take = DEPTH_PRESETS[depth]
    take = n or take
    if depth == "hits":
        return [(t["title"], 1 - 0.5 * r / max(take, 1)) for r, t in enumerate(tracks[:take])]
    if depth == "mixed":
        n_head = max(1, take // 4)
        head, tail = tracks[:3], tracks[3:]
        chosen = rng.sample(head, min(n_head, len(head)))
        chosen += rng.sample(tail, min(take - len(chosen), len(tail)))
    else:
        pool = tracks[skip:] or tracks
        chosen = rng.sample(pool, min(take, len(pool)))
    return [(t["title"], 0.85) for t in chosen]


def discover(seeds: list[Seed], lf: LastFM, n_seeds: int, per_seed: int,
             fallback_artists: int = 12, depth: str = "mixed", rng: random.Random | None = None,
             fallback_weight: float = 0.6) -> dict:
    """Similar tracks from Last.fm for each seed. Last.fm's track-level
    similarity is patchy, so when a seed has none we fall back to similar
    ARTISTS and their most-played tracks (weighted a bit lower)."""
    rng = rng or random.Random(0)
    cands: dict[str, Candidate] = {}
    use = seeds[:n_seeds]
    print(f"  fetching similar tracks for {len(use)} seeds...")
    n_track, n_fallback, n_none = 0, 0, 0
    artist_done: dict[str, list] = {}

    def add(artist, title, w, via):
        k = key(artist, title)
        c = cands.get(k) or Candidate(artist, title)
        c.score += w
        c.support += 1
        if len(c.via) < 3 and via not in c.via:
            c.via.append(via)
        cands[k] = c

    for i, s in enumerate(use, 1):
        via = f"{s.artist} - {s.title}"
        sims = lf.similar_tracks(s.artist, lookup_title(s.title), per_seed)
        if sims:
            n_track += 1
            for t in sims:
                add(t["artist"], t["title"], s.weight * t["match"], via)
            continue
        # fallback: similar artists -> their top tracks
        if s.artist not in artist_done:
            found = []
            for a in lf.similar_artists(s.artist, fallback_artists):
                tops = lf.artist_top_tracks(a["artist"], DEPTH_PRESETS[depth][0])
                for title, w in pick_depth(tops, depth, rng):
                    found.append((a["artist"], title, a["match"] * w))
            artist_done[s.artist] = found
        found = artist_done[s.artist]
        if found:
            n_fallback += 1
            for artist, title, m in found:
                add(artist, title, s.weight * m * fallback_weight, via + " (via similar artists)")
        else:
            n_none += 1
        if i % 10 == 0:
            print(f"    {i}/{len(use)}", end="\r")
    print(f"  seeds with similar-track data: {n_track}, used similar-artist fallback: "
          f"{n_fallback}, nothing found: {n_none}")
    return cands


def list_tags(seeds: dict, lf: LastFM, artist_tag_cache: dict, top: int = 40):
    """Show which Last.fm tags your library actually carries, weighted by
    how much you listen, to help choose --tags / --avoid-tags."""
    weight: dict[str, float] = defaultdict(float)
    artists: dict[str, set] = defaultdict(set)
    by_artist: dict[str, float] = defaultdict(float)
    for s in seeds.values():
        by_artist[s.artist] += s.weight
    print(f"  checking tags for {len(by_artist)} artists on Last.fm...")
    for a, w in by_artist.items():
        if a not in artist_tag_cache:
            artist_tag_cache[a] = lf.artist_tags(a)
        for t in artist_tag_cache[a]:
            weight[t] += w
            artists[t].add(a)
    print(f"\n  {'tag':28} {'weight':>8} {'artists':>8}  examples")
    for t, w in sorted(weight.items(), key=lambda x: -x[1])[:top]:
        ex = ", ".join(sorted(artists[t], key=lambda a: -by_artist[a])[:3])
        print(f"  {t[:28]:28} {w:8.1f} {len(artists[t]):8d}  {ex}")
    print("\nPick a few of these for --tags, e.g. --tags \"house,deep house\"")


def rank(cands: dict, known: set, known_artists: set, lf: LastFM, want, avoid,
         artist_tag_cache: dict, max_per_artist: int, discover_only: bool,
         pool: int, force: set | None = None, suggested: dict | None = None) -> list[Candidate]:
    """suggested: {track key: times already put in a playlist}. Each previous
    appearance halves the score, so refreshes rotate in new tracks."""
    force = force or set()
    suggested = suggested or {}
    # Support across several seeds matters more than one strong link.
    pre = [c for k, c in cands.items() if k not in known]
    if discover_only:
        pre = [c for c in pre if norm(c.artist) not in known_artists]
    for c in pre:
        c.score *= 1 + 0.5 * math.log(c.support)
        c.score *= 0.5 ** suggested.get(key(c.artist, c.title), 0)
    pre.sort(key=lambda c: -c.score)
    pre = pre[: pool * 3]

    out, per_artist = [], defaultdict(int)
    songs_seen = set()
    for c in pre:
        if c.artist not in artist_tag_cache:
            artist_tag_cache[c.artist] = lf.artist_tags(c.artist)
        c.tags = artist_tag_cache[c.artist]
        a = norm(c.artist)
        if a not in force and not tag_match(c.tags, want, avoid):
            continue
        if per_artist[a] >= max_per_artist:
            continue
        song = (a, base_title(c.title))
        if song in songs_seen:
            continue  # another version (remix, mix, edit) of a song already picked
        songs_seen.add(song)
        per_artist[a] += 1
        out.append(c)
        if len(out) >= pool:
            break
    return out


def llm_rerank(cands: list[Candidate], vibe: str, n: int, model: str,
               anchors: list[str] | None = None) -> list[Candidate]:
    """Optional: let Claude choose the n candidates that best fit a vibe,
    given the songs the playlist is built from. Each pick gets a short reason
    (saved in picks.csv as claude_note)."""
    import anthropic
    client = anthropic.Anthropic()
    listing = "\n".join(
        f"{i}. {c.artist} - {c.title} | tags: {', '.join(c.tags[:5]) or '-'} | "
        f"found via: {'; '.join(v.replace(' (via similar artists)', '') for v in c.via[:2])}"
        for i, c in enumerate(cands))
    anchor_txt = "\n".join(f"- {a}" for a in (anchors or [])[:12]) or "- (none given)"
    prompt = (
        "You are curating a playlist for one listener.\n\n"
        f"Songs the playlist is built from (the listener loves these):\n{anchor_txt}\n\n"
        f"The vibe they want:\n{vibe}\n\n"
        "Candidates (number. artist - title | Last.fm tags | which of their songs led here):\n"
        f"{listing}\n\n"
        f"Choose the {n} candidates that best fit the vibe and sit well next to the songs above. "
        "Use what you know about these artists and tracks; where you don't know a track, judge "
        "from the artist, tags and how it was found, and don't invent details about it. "
        "Prefer variety of artists. Order them best fit first.\n\n"
        'Reply with only JSON: {"picks": [{"n": <number>, "why": "<max 12 words>"}, ...]}'
    )
    msg = client.messages.create(model=model, max_tokens=4000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    chosen: list[tuple[int, str]] = []
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            for p in json.loads(m.group(0)).get("picks", []):
                chosen.append((int(p["n"]), str(p.get("why", ""))[:120]))
        except (ValueError, KeyError, TypeError):
            chosen = []
    if not chosen:  # fall back to a bare list of numbers
        m = re.search(r"\[[\d,\s]*\]", text)
        if m:
            chosen = [(int(i), "") for i in json.loads(m.group(0))]
    if not chosen:
        print("  ! Claude's reply couldn't be read, keeping the normal ranking")
        return cands[:n]
    picks = []
    for i, why in chosen:
        if 0 <= i < len(cands) and cands[i] not in picks:
            cands[i].note = why
            picks.append(cands[i])
    print(f"  Claude picked {len(picks)} of {len(cands)}")
    return picks[:n]


_VERSION = re.compile(r"\s*(?:[-(\[]\s*)?[^-(\[]*\b(remix|rework|edit|dub|vip|mix|version|remaster(ed)?)\b.*$", re.I)


def base_title(title: str) -> str:
    """'Before The Flood - Ankhoi Remix' -> 'before the flood'."""
    return norm(_VERSION.sub("", title or "") or title)


def match_on_spotify(sp: Spotify, cands: list[Candidate], cache: Cache,
                     threshold: float = 0.82, any_version: bool = False,
                     prefer_uris: set | None = None) -> list[Candidate]:
    """any_version: for songs you named yourself, accept a remix/edit when the
    exact version isn't found, preferring a version you've actually played
    (prefer_uris)."""
    prefer_uris = prefer_uris or set()
    matched = []
    for i, c in enumerate(cands, 1):
        ck = ("matchany2|" if any_version else "match2|") + key(c.artist, c.title)
        hit = cache.get(ck)
        if hit is not None and any_version and not hit:
            hit = None  # retry misses for named songs; your library may have changed
        if hit is None:
            results = sp.search_track(c.artist, lookup_title(c.title) or c.title)
            if any_version:
                # also search the bare title so remixes of it show up
                bt = base_title(c.title)
                if bt and bt != norm(c.title):
                    results += sp.search_track(c.artist, bt)
            best, best_score = None, 0.0
            for t in results:
                a_score = max(sim(c.artist, a["name"]) for a in t["artists"])
                exact_version = is_remix(c.title) == is_remix(t["name"])
                if exact_version:
                    score = 0.55 * sim(c.title, t["name"]) + 0.45 * a_score
                elif any_version:
                    # same song, different version: compare bare titles, small penalty
                    score = 0.55 * SequenceMatcher(None, base_title(c.title), base_title(t["name"])).ratio() \
                        + 0.45 * a_score - 0.05
                else:
                    continue  # don't swap an original for a remix or vice versa
                if is_dj_mix(t["name"]):
                    score -= 0.3  # prefer the standalone release; skip if only the mix cut exists
                if t["uri"] in prefer_uris and score >= threshold - 0.1:
                    score += 0.2  # the exact version you've been playing
                if score > best_score:
                    best, best_score = t, score
            hit = ({"uri": best["uri"], "name": f'{best["artists"][0]["name"]} - {best["name"]}'}
                   if best and best_score >= threshold else {})
            cache.set(ck, hit)
        if hit:
            c.uri, c.spotify_name = hit["uri"], hit["name"]
            matched.append(c)
        print(f"  matched {len(matched)}/{i}", end="\r")
    print()
    return matched


def interleave(new: list, own: list) -> list:
    """Spread `own` evenly through `new`, keeping each list's order."""
    if not own:
        return list(new)
    if not new:
        return list(own)
    out, i, j = [], 0, 0
    total = len(new) + len(own)
    for k in range(total):
        # take from whichever list is furthest behind its fair share
        if j < len(own) and (i >= len(new) or (j + 1) / len(own) <= (i + 1) / len(new)):
            out.append(own[j]); j += 1
        else:
            out.append(new[i]); i += 1
    return out


def write_report(picks: list[Candidate], path: Path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "artist", "title", "score", "support", "tags", "via", "spotify_match",
                    "uri", "claude_note"])
        for i, c in enumerate(picks, 1):
            w.writerow([i, c.artist, c.title, f"{c.score:.2f}", c.support,
                        ", ".join(c.tags[:5]), " | ".join(c.via), c.spotify_name, c.uri or "", c.note])


# ==========================================================================

GENERIC_TAGS = {"electronic", "electronica", "dance", "seen live", "favorites", "favourite",
                "female vocalists", "male vocalists", "pop", "instrumental", "beautiful",
                "chill", "chillout", "producer", "dj", "remix", "under 2000 listeners"}


def split_likes(text: str) -> list[str]:
    """Several items separated by ';' or newlines, or by commas when every
    comma-separated part is itself 'Artist - Song' (titles can contain commas)."""
    parts = [p.strip() for p in re.split(r"[;\n]", text) if p.strip()]
    out = []
    for p in parts:
        bits = [b.strip() for b in p.split(",") if b.strip()]
        if len(bits) > 1 and all(" - " in b for b in bits):
            out += bits
        else:
            out.append(p)
    return out


def album_filter(tracks: list[dict], lf: LastFM, want: list[str], skip: list[str]):
    """Keep album tracks unless their own Last.fm tags mark them as a skipped
    genre (e.g. rap) without any of the playlist's genres (e.g. house) in
    their top tags. Tracks with no tags are kept. Returns (kept, dropped, n_untagged)."""
    def is_want(x):  # "house" on a track counts for a playlist that wants "afro house"
        return any(w == x or w in x or x in w for w in want)

    def is_skip(x):
        return any(sk == x or sk in x for sk in skip)

    kept, dropped, untagged = [], [], 0
    for t in tracks:
        artist = t["artists"][0]["name"]
        tags = lf.track_tags(artist, lookup_title(t["name"]))[:TAG_DEPTH]
        hit_skip = [x for x in tags if is_skip(x)]
        hit_want = [x for x in tags if is_want(x)]
        if hit_skip and not hit_want:
            dropped.append((t, f"tagged {', '.join(hit_skip[:2])}"))
            continue
        if not hit_want:
            # no genre evidence on the track itself: check who it features
            # (e.g. a rapper on "Jimmy Cooks (feat. 21 Savage)")
            feat = None
            for fa in t["artists"][1:]:
                ftags = lf.artist_tags(fa["name"])[:TAG_DEPTH]
                iskip = next((i for i, x in enumerate(ftags) if is_skip(x)), None)
                iwant = next((i for i, x in enumerate(ftags) if is_want(x)), None)
                if iskip is not None and (iwant is None or iskip < iwant):
                    feat = f"feat. {fa['name']}, tagged {ftags[iskip]}"
                    break
            if feat:
                dropped.append((t, feat))
                continue
        if not tags:
            untagged += 1
        kept.append(t)
    return kept, dropped, untagged


def parse_like(text: str) -> tuple[str, str | None]:
    """'Artist' or 'Artist - Song'."""
    if " - " in text:
        artist, title = text.split(" - ", 1)
        return artist.strip(), title.strip()
    return text.strip(), None


def auto_tags(artists: list[str], lf: LastFM, artist_tag_cache: dict, per_artist: int = 2) -> list[str]:
    """The genre tags that best describe the artists you named."""
    out = []
    for a in artists:
        if a not in artist_tag_cache:
            artist_tag_cache[a] = lf.artist_tags(a)
        tags = [t for t in artist_tag_cache[a] if t not in GENERIC_TAGS]
        for t in tags[:per_artist]:
            if t not in out:
                out.append(t)
    return out


def resolve_likes(likes: list[tuple[str, str | None]], lf: LastFM) -> list[tuple[str, str | None]]:
    """'Black Coffee, Enoo Napa' is two artists, but 'Tyler, The Creator' is
    one: split on commas only when Last.fm doesn't know the whole name."""
    out = []
    for artist, title in likes:
        if not title and "," in artist and not lf.artist_top_tracks(artist, 40):
            out += [(a.strip(), None) for a in artist.split(",") if a.strip()]
        else:
            out.append((artist, title))
    return out


def like_seeds(likes: list[tuple[str, str | None]], lf: LastFM, depth: str,
               rng: random.Random, artist_tracks: int):
    """Seeds from artists/songs you named, plus songs to include by them:
    a named song is always included; a named artist contributes
    `artist_tracks` songs from their catalogue at the chosen depth."""
    seeds: dict[str, Seed] = {}
    include: list[Candidate] = []
    for artist, title in likes:
        tops = lf.artist_top_tracks(artist, 40)
        if title:
            _add_seed(seeds, artist, title, 5.0, "like")
            include.append(Candidate(artist, title, 5.0, 0, ["(your pick)"]))
            continue
        if not tops:
            print(f"  ! Last.fm doesn't know '{artist}'. Check the spelling.")
            continue
        for r, t in enumerate(tops[:10]):
            _add_seed(seeds, artist, t["title"], 3.0 * (1 - r / 20), "like")
        for title_, _ in pick_depth(tops, depth, rng, artist_tracks):
            include.append(Candidate(artist, title_, 3.0, 0, [f"(by {artist}, {depth})"]))
    seen, uniq = set(), []
    for c in include:
        k = key(c.artist, c.title)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return seeds, uniq


PROFILE_KEYS = ["tags", "avoid_tags", "tag_depth", "force_artists", "name", "size",
                "max_per_artist", "include_seeds", "discover_only", "depth", "seeds",
                "min_plays", "listened_days", "trust_listened", "vibe", "playlist_id",
                "public", "no_liked", "no_top", "like", "artist_tracks", "with_library", "album", "album_skip_tags"]


_ARGV: list[str] = []


def load_profiles(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        sys.exit(f"{path.name} is not valid JSON; fix or delete it")


def save_profiles(path: Path, profiles: dict):
    path.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")


def _explicit(ap: argparse.ArgumentParser, args, k: str) -> bool:
    """True if the user passed this option on the command line."""
    return f"--{k.replace('_', '-')}" in _ARGV or any(
        a.startswith(f"--{k.replace('_', '-')}=") for a in _ARGV)


def apply_profile(ap, args, profile: dict):
    for k, v in profile.items():
        if k in PROFILE_KEYS and not _explicit(ap, args, k):
            setattr(args, k, v)


def profile_from_args(ap, args) -> dict:
    """Store only options that differ from the defaults (plus tags and the ID)."""
    out = {}
    for k in PROFILE_KEYS:
        v = getattr(args, k, None)
        if v is None:
            continue
        if k in ("tags", "playlist_id", "name") or v != ap.get_default(k):
            out[k] = v
    return out


def main(argv: list[str] | None = None):
    global _ARGV
    argv = sys.argv[1:] if argv is None else list(argv)
    _ARGV = argv
    if not argv:
        return wizard()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--like", action="append", default=None, metavar='"ARTIST" or "ARTIST - SONG"',
                    help="build around an artist or song instead of your library (repeatable, "
                         "or separate several with ;). Their genre tags are added automatically")
    ap.add_argument("--album", action="append", default=None, metavar='"ARTIST - ALBUM"',
                    help="spread an album's tracks through the playlist, in album order "
                         "(repeatable, or separate several with ;). Skips the genre filter "
                         "and doesn't steer the recommendations")
    ap.add_argument("--album-skip-tags", default=None,
                    help='drop album tracks tagged with these (e.g. "rap,hip-hop") unless they also '
                         "carry one of the playlist's genre tags. Defaults to --avoid-tags")
    ap.add_argument("--artist-tracks", type=int, default=10,
                    help="with --like ARTIST: how many of that artist's own songs to include (default 10)")
    ap.add_argument("--with-library", action="store_true",
                    help="with --like: also use your liked/top/listened tracks as seeds")
    ap.add_argument("--profile", help="load saved options for this playlist from profiles.json")
    ap.add_argument("--save-profile", metavar="NAME",
                    help="save this run's options (and the playlist ID) under NAME")
    ap.add_argument("--list-profiles", action="store_true", help="show saved profiles, then exit")
    ap.add_argument("--harvest", action="store_true",
                    help="save Spotify's last 50 plays to listening_log.jsonl, then exit "
                         "(run every 1-2 h so nothing you hear is lost)")
    ap.add_argument("--lastfm-user", default=os.environ.get("LASTFM_USER"),
                    help="your Last.fm username, to use scrobbled plays (or set LASTFM_USER in .env)")
    ap.add_argument("--listened-days", type=int, default=60,
                    help="how far back to count listened tracks (default 60)")
    ap.add_argument("--min-plays", type=int, default=1,
                    help="a listened track counts once played this many times (default 1)")
    ap.add_argument("--trust-listened", action="store_true",
                    help="keep listened tracks by artists with no Last.fm tags at all")
    ap.add_argument("--depth", choices=list(DEPTH_PRESETS), default="mixed",
                    help="hits = each similar artist's biggest tracks; mixed (default) = one hit "
                         "+ deeper cuts; deep = skip their 5 biggest tracks")
    ap.add_argument("--shuffle-seed", type=int,
                    help="fix the deep-cut sampling (default changes weekly)")
    ap.add_argument("--list-tags", action="store_true",
                    help="show the most common Last.fm tags in your library, then exit")
    ap.add_argument("--tags", default="",
                    help='comma-separated Last.fm tags that define the niche, e.g. "deep house,lo-fi house"')
    ap.add_argument("--avoid-tags", default="",
                    help='comma-separated tags to exclude, e.g. "tech house,edm"')
    ap.add_argument("--tag-depth", type=int, default=6,
                    help="only look at each artist's top N Last.fm tags (default 6; lower = stricter)")
    ap.add_argument("--name", help="playlist name (default: from the tags)")
    ap.add_argument("--size", type=int, default=50, help="number of tracks (default 50)")
    ap.add_argument("--max-per-artist", type=int, default=2)
    ap.add_argument("--include-seeds", default="0",
                    help='also include your own niche tracks: a number (your top N) or "all" '
                         "(default 0 = only new tracks). --size counts new tracks only")
    ap.add_argument("--force-artists", default="",
                    help='comma-separated artists to treat as part of the niche regardless of tags')
    ap.add_argument("--discover-only", action="store_true",
                    help="exclude artists you already have in your seeds")
    ap.add_argument("--history", help='glob for streaming history export, e.g. "my_spotify_data/Streaming_History_Audio_*.json"')
    ap.add_argument("--no-liked", action="store_true", help="don't use liked songs as seeds")
    ap.add_argument("--no-top", action="store_true", help="don't use top tracks as seeds")
    ap.add_argument("--seeds", type=int, default=60, help="how many of your strongest niche tracks to expand (default 60)")
    ap.add_argument("--vibe", help="free-text vibe for optional Claude rerank (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--llm-model", default=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("--playlist-id", help="refresh this existing playlist instead of creating a new one")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="write picks.csv but don't touch playlists")
    args = ap.parse_args(argv)
    profiles_path = HERE / "profiles.json"
    profiles = load_profiles(profiles_path)
    if args.list_profiles:
        if not profiles:
            print("No profiles yet. Create one with --save-profile NAME.")
        for name, opts in profiles.items():
            print(f"{name}: " + ", ".join(f"{k}={v}" for k, v in opts.items()))
        return
    if args.profile:
        if args.profile not in profiles:
            sys.exit(f"No profile {args.profile!r} in profiles.json (have: {', '.join(profiles) or 'none'})")
        apply_profile(ap, args, profiles[args.profile])
    profile_name = args.save_profile or args.profile
    likes = [parse_like(x) for item in (args.like or []) for x in split_likes(item)]
    like_artists = list(dict.fromkeys(a for a, _ in likes))
    auto_name = not args.name
    if not args.name and likes:
        args.name = like_artists[0] + (" & similar" if len(like_artists) == 1 else " & co")
    if not args.name and args.tags:
        args.name = " / ".join(t.strip().title() for t in args.tags.split(",") if t.strip())

    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    args.lastfm_user = args.lastfm_user or os.environ.get("LASTFM_USER") or None

    if not args.tags and not likes and not args.list_tags and not args.harvest:
        sys.exit("Pass --tags or --like, or run with --list-tags first to see what your library is tagged as.")
    global TAG_DEPTH
    if args.include_seeds != "all" and not args.include_seeds.isdigit():
        sys.exit('--include-seeds must be a number or "all"')
    force = {norm(a) for a in args.force_artists.split(",") if a.strip()}
    TAG_DEPTH = args.tag_depth
    want = [t.strip().lower() for t in args.tags.split(",") if t.strip()]
    avoid = [t.strip().lower() for t in args.avoid_tags.split(",") if t.strip()]
    lf_key = os.environ.get("LASTFM_API_KEY")
    sp_id = os.environ.get("SPOTIFY_CLIENT_ID")
    redirect = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    if not lf_key or not sp_id:
        sys.exit("Set LASTFM_API_KEY and SPOTIFY_CLIENT_ID in .env (see .env.example)")

    cache = Cache(CACHE_DIR / "lastfm.json")
    match_cache = Cache(CACHE_DIR / "spotify_matches.json")
    artist_tags = cache.get("__artist_tags__") or {}
    lf = LastFM(lf_key, cache)
    if likes:
        likes = resolve_likes(likes, lf)
        like_artists = list(dict.fromkeys(a for a, _ in likes))
        if auto_name:
            args.name = like_artists[0] + (" & similar" if len(like_artists) == 1 else " & co")
    sp = Spotify(sp_id, redirect)

    log_path = HERE / LOG_NAME
    if args.harvest:
        n = harvest(sp, log_path)
        print(f"Saved {n} new plays to {log_path.name}")
        return

    suggested_path = CACHE_DIR / "suggested.json"
    try:
        suggested_all = json.loads(suggested_path.read_text()) if suggested_path.exists() else {}
    except json.JSONDecodeError:
        suggested_all = {}
    # rotation history is kept per playlist, so genres don't affect each other
    rot_key = profile_name or args.playlist_id or args.name or ",".join(want)
    suggested = suggested_all.get(rot_key, {})
    year, week, _ = dt.date.today().isocalendar()
    rng = random.Random(args.shuffle_seed if args.shuffle_seed is not None else year * 100 + week)

    try:
        like_include: list[Candidate] = []
        seeds: dict[str, Seed] = {}
        if not likes or args.with_library or args.list_tags:
            print("1/6 Collecting your tracks")
            listened = load_listening(log_path, lf, args.lastfm_user, args.listened_days, args.min_plays) \
                if (log_path.exists() or args.lastfm_user) else {}
            if listened:
                print(f"  listened tracks counted: {len(listened)}")
            seeds = collect_seeds(sp, args.history, not args.no_liked, not args.no_top, listened)
        if likes and not args.list_tags:
            print(f"1/6 Starting from: {', '.join(a + (' - ' + t if t else '') for a, t in likes)}")
            lseeds, like_include = like_seeds(likes, lf, args.depth, rng, args.artist_tracks)
            for k, sd in lseeds.items():
                _add_seed(seeds, sd.artist, sd.title, sd.weight, "like", sd.uri)
            extra = auto_tags(like_artists, lf, artist_tags)
            want = list(dict.fromkeys(extra + want))
            force |= {norm(a) for a in like_artists}
            print(f"  genre tags: {', '.join(want) if want else '(none found, no tag filter)'}")
            if like_include:
                print(f"  {len(like_include)} songs by the artist(s) you named")
        if not seeds:
            sys.exit("No seed tracks found.")

        if args.list_tags:
            list_tags(seeds, lf, artist_tags)
            return

        print(f"2/6 Filtering to {want}")
        niche = filter_seeds(seeds, lf, want, avoid, artist_tags, force, HERE,
                             trust_untagged_listened=args.trust_listened)
        print(f"  {len(niche)} of {len(seeds)} tracks fit the niche")
        if not niche:
            sys.exit("None of your tracks matched those tags. Run --list-tags and try broader ones.")
        for s in niche[:5]:
            print(f"    e.g. {s.artist} - {s.title}  (weight {s.weight:.1f})")
        if len(niche) < 30:
            print("  ! Few seeds. Run --list-tags and consider broader tags, or --force-artists.")

        print("3/6 Discovering similar tracks")
        print(f"  depth: {args.depth}")
        cands = discover(niche, lf, args.seeds, per_seed=30, depth=args.depth, rng=rng)
        print(f"  {len(cands)} candidates")
        if not cands:
            sys.exit("Last.fm returned no similar music for these seeds. Try broader --tags.")

        print("4/6 Ranking")
        known = set(seeds)
        known_artists = {norm(s.artist) for s in seeds.values()}
        pool = args.size * 2 if args.vibe else int(args.size * 1.4)
        ranked = rank(cands, known, known_artists, lf, want, avoid, artist_tags,
                      args.max_per_artist, args.discover_only, pool, force, suggested)
        if likes and args.artist_tracks:
            named = {norm(a) for a in like_artists}
            ranked = [c for c in ranked if norm(c.artist) not in named]
        print(f"  {len(ranked)} pass tag and artist checks")
        if args.vibe:
            print("  asking Claude to pick for the vibe...")
            try:
                anchors = [f"{a} - {t}" for a, t in likes if t] + \
                          [f"{sd.artist} - {sd.title}" for sd in niche[:12]]
                ranked = llm_rerank(ranked, args.vibe, int(args.size * 1.3), args.llm_model,
                                    list(dict.fromkeys(anchors)))
            except Exception as e:  # missing key, network, etc.: keep the normal ranking
                print(f"  ! Claude rerank skipped ({type(e).__name__}: {str(e)[:120]})")

        print("5/6 Matching on Spotify")
        picks = match_on_spotify(sp, ranked, match_cache)[: args.size]
        if args.include_seeds != "0":
            own_all = [Candidate(s.artist, s.title, s.weight, 0,
                                 ["(your track: " + ", ".join(sorted(s.sources)) + ")"],
                                 uri=s.uri, spotify_name=f"{s.artist} - {s.title}" if s.uri else "")
                       for s in niche]
            if args.include_seeds != "all":
                own_all = own_all[: int(args.include_seeds)]
            missing = [c for c in own_all if not c.uri]
            if missing:  # e.g. tracks only known from Last.fm scrobbles
                match_on_spotify(sp, missing, match_cache)
            own = [c for c in own_all if c.uri]
            print(f"  adding {len(own)} of your own tracks")
            picks = interleave(picks, own)
        if like_include:
            played = {s_.uri for s_ in seeds.values() if s_.uri}
            if log_path.exists():
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        played.add(json.loads(line).get("uri"))
            named = [c for c in like_include if c.via == ["(your pick)"]]
            others = [c for c in like_include if c.via != ["(your pick)"]]
            match_on_spotify(sp, named, match_cache, any_version=True, prefer_uris=played)
            match_on_spotify(sp, others, match_cache)
            got = [c for c in like_include if c.uri]
            # songs you named go first, in the order you gave them;
            # songs from artists you named are spread through the rest
            pinned = [c for c in got if c.via == ["(your pick)"]]
            spread = [c for c in got if c.via != ["(your pick)"]]
            pinned_uris = {c.uri for c in pinned}
            picks = [c for c in picks if c.uri not in pinned_uris]
            for c in pinned:
                print(f"  at the top: {c.spotify_name}")
            missed = [f"{c.artist} - {c.title}" for c in like_include
                      if c.via == ["(your pick)"] and not c.uri]
            for m in missed:
                print(f"  ! couldn't find '{m}' on Spotify, so it isn't at the top")
            if spread:
                print(f"  adding {len(spread)} songs by {', '.join(like_artists)}")
            picks = pinned + interleave(picks, spread)

        if args.album:
            n_top = 0
            while n_top < len(picks) and picks[n_top].via == ["(your pick)"]:
                n_top += 1
            have = {c.uri for c in picks}
            album_cands = []
            for spec in (a.strip() for item in args.album for a in item.split(";") if a.strip()):
                artist, album = parse_like(spec) if " - " in spec else ("", spec)
                al = sp.find_album(artist, album)
                if not al:
                    print(f"  ! couldn't find the album '{spec}' on Spotify")
                    continue
                tracks = [t for t in sp.album_tracks(al["id"]) if t.get("uri") not in have]
                label = f'{al["artists"][0]["name"]} - {al["name"]}'
                skip_tags = [t.strip().lower() for t in
                             (args.album_skip_tags if args.album_skip_tags is not None else args.avoid_tags).split(",")
                             if t.strip()]
                if skip_tags:
                    tracks, dropped, untagged = album_filter(tracks, lf, want, skip_tags)
                    for t, why in dropped:
                        print(f"    skipping {t['name']} ({why})")
                    if untagged:
                        print(f"    kept {untagged} track(s) Last.fm has no tags for")
                print(f"  spreading {len(tracks)} tracks from {label}")
                album_cands.append([Candidate(t["artists"][0]["name"], t["name"], 0, 0,
                                              [f"(album: {label})"], uri=t["uri"],
                                              spotify_name=f'{t["artists"][0]["name"]} - {t["name"]}')
                                    for t in tracks])
                have |= {t["uri"] for t in tracks}
            # several albums: alternate between them, each staying in order
            merged = []
            for group in album_cands:
                merged = interleave(merged, group) if merged else group
            picks = picks[:n_top] + interleave(picks[n_top:], merged)

        report = HERE / "picks.csv"
        write_report(picks, report)
        print(f"  {len(picks)} tracks -> {report.name}")

        print("6/6 Writing playlist")
        uris = [c.uri for c in picks if c.uri]
        if args.dry_run:
            print("  dry run: no playlist changes. Check picks.csv.")
        elif args.playlist_id:
            sp.replace_items(args.playlist_id, uris)
            print(f"  refreshed playlist {args.playlist_id} with {len(uris)} tracks")
        else:
            desc = (f"Similar to {', '.join(like_artists)}. Tags: {', '.join(want)}." if likes
                    else f"Built from my listening. Tags: {', '.join(want)}.")
            pl = sp.create_playlist(args.name, desc, args.public)
            sp.add_items(pl["id"], uris)
            url = pl.get("external_urls", {}).get("spotify", pl["id"])
            print(f"  created '{args.name}' with {len(uris)} tracks: {url}")
            args.playlist_id = pl["id"]
            if profile_name:
                print(f"  saved to profile '{profile_name}': refresh with  python playlistfy.py --profile {profile_name}")
            else:
                print(f"  next time, refresh it with --playlist-id {pl['id']}"
                      f"  (or add --save-profile NAME to save all these options)")
        if profile_name and (args.save_profile or not args.dry_run):
            profiles[profile_name] = profile_from_args(ap, args)
            save_profiles(profiles_path, profiles)
            if args.save_profile:
                print(f"  profile '{profile_name}' saved")
        if not args.dry_run:
            for c in picks:
                if not c.via or not c.via[0].startswith(("(your track", "(album", "(your pick")):
                    k = key(c.artist, c.title)
                    suggested[k] = suggested.get(k, 0) + 1
            CACHE_DIR.mkdir(exist_ok=True)
            suggested_all[rot_key] = suggested
            suggested_path.write_text(json.dumps(suggested_all))
    finally:
        cache.set("__artist_tags__", artist_tags)
        cache.save()
        match_cache.save()


# ==========================================================================
# Interactive mode (run with no options)
# ==========================================================================

class _Ask:
    """Arrow-key prompts via `questionary` if installed, plain input() otherwise."""

    def __init__(self):
        try:
            import questionary
            self.q = questionary
        except ImportError:
            self.q = None

    def select(self, msg, choices):
        if self.q:
            return self.q.select(msg, choices=choices).ask()
        print(f"\n{msg}")
        for i, c in enumerate(choices, 1):
            print(f"  {i}. {c}")
        while True:
            a = input("> ").strip()
            if a.isdigit() and 1 <= int(a) <= len(choices):
                return choices[int(a) - 1]
            if a == "":
                return None

    def text(self, msg, default=""):
        if self.q:
            r = self.q.text(msg, default=default).ask()
            return None if r is None else r.strip()
        r = input(f"{msg}{f' [{default}]' if default else ''}: ").strip()
        return r or default

    def confirm(self, msg, default=True):
        if self.q:
            return self.q.confirm(msg, default=default).ask()
        r = input(f"{msg} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not r else r.startswith("y")


DEPTH_CHOICES = {"Mixed: a few hits plus deeper cuts": "mixed",
                 "Deep: skip each artist's biggest songs": "deep",
                 "Hits: each artist's best-known songs": "hits"}


def _preview(n: int = 25):
    path = HERE / "picks.csv"
    if not path.exists():
        return
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    print(f"\n  Preview ({len(rows)} tracks):")
    for r in rows[:n]:
        why = r["via"].split(" | ")[0]
        why = why if why.startswith("(") else f"like {why.replace(' (via similar artists)', '')}"
        if r.get("claude_note"):
            why = f"Claude: {r['claude_note']}"
        print(f"   {int(r['rank']):3d}. {r['artist']} - {r['title']}   [{why}]")
    if len(rows) > n:
        print(f"   ... and {len(rows) - n} more in picks.csv")


def _run(argv: list[str]) -> bool:
    try:
        main(argv)
        return True
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"\n  ! {e.code}")
        return False
    except KeyboardInterrupt:
        print("\n  (cancelled)")
        return False
    except Exception as e:
        print(f"\n  ! Something went wrong: {type(e).__name__}: {e}")
        return False


def _preview_then_create(ask: _Ask, argv: list[str]):
    print("\nBuilding a preview (nothing changes on Spotify yet)...\n")
    if not _run(argv + ["--dry-run"]):
        return
    _preview()
    if ask.confirm("\nCreate this playlist on Spotify?"):
        _run(argv)


def _common_questions(ask: _Ask, argv: list[str], default_size="40"):
    album = ask.text("Spread an album through it? (optional, e.g. Drake - Honestly, Nevermind)", "")
    if album:
        argv += ["--album", album]
        skip = ask.text("Skip album tracks tagged as (optional, e.g. rap, hip-hop)", "rap, hip-hop")
        if skip:
            argv += ["--album-skip-tags", skip]
    size = ask.text("How many new songs?", default_size)
    if size and size.isdigit():
        argv += ["--size", size]
    depth = ask.select("How deep should it go?", list(DEPTH_CHOICES))
    if depth:
        argv += ["--depth", DEPTH_CHOICES[depth]]
    vibe = ask.text("Describe the vibe for Claude to pick from (optional, needs ANTHROPIC_API_KEY)", "")
    if vibe:
        argv += ["--vibe", vibe]
    name = ask.text("Playlist name (leave empty for automatic)", "")
    if name:
        argv += ["--name", name]
    prof = ask.text("Save as a profile to refresh later? Profile name (optional)", "")
    if prof:
        argv += ["--save-profile", prof]
    return argv


def wizard():
    ask = _Ask()
    if not ask.q:
        print("(Tip: pip install questionary for arrow-key menus)")
    print("\nplaylistfy")
    while True:
        profiles = load_profiles(HERE / "profiles.json")
        choices = []
        if profiles:
            choices.append("Refresh a saved playlist")
        choices += ["New playlist from an artist or song",
                    "New playlist from a genre in my library",
                    "See which genres my library has",
                    "Save what I've been listening to",
                    "Quit"]
        c = ask.select("What do you want to do?", choices)
        if c in (None, "Quit"):
            return

        if c == "Refresh a saved playlist":
            name = ask.select("Which one?", list(profiles))
            if not name:
                continue
            argv = ["--profile", name]
            if ask.confirm("Preview before updating Spotify?", default=False):
                _preview_then_create(ask, argv)
            else:
                _run(argv)

        elif c == "New playlist from an artist or song":
            print("\n  e.g. Da Africa Deep   or   Da Africa Deep - Personal")
            print("  several at once: Da Africa Deep - Personal, &ME - Before The Flood")
            like = ask.text("Artist or song")
            if not like:
                continue
            argv = ["--like", like]
            if " - " not in like:
                n = ask.text("How many songs by them to include?", "10")
                if n and n.isdigit():
                    argv += ["--artist-tracks", n]
            more = ask.text("Other genres to mix in too (optional, e.g. deep house)", "")
            if more:
                argv += ["--tags", more]
            if ask.confirm("Also use your own library to steer it?", default=False):
                argv += ["--with-library"]
            argv = _common_questions(ask, argv, "30")
            _preview_then_create(ask, argv)

        elif c == "New playlist from a genre in my library":
            tags = ask.text("Genre tags, comma separated (e.g. house, deep house)")
            if not tags:
                continue
            argv = ["--tags", tags]
            avoid = ask.text("Tags to keep out (optional, e.g. rap, hip-hop)", "")
            if avoid:
                argv += ["--avoid-tags", avoid]
            force = ask.text("Artists to always count in (optional, comma separated)", "")
            if force:
                argv += ["--force-artists", force]
            if ask.confirm("Include your own songs from this genre?", default=True):
                argv += ["--include-seeds", "all"]
            argv = _common_questions(ask, argv)
            _preview_then_create(ask, argv)

        elif c == "See which genres my library has":
            _run(["--list-tags"])

        elif c == "Save what I've been listening to":
            _run(["--harvest"])



if __name__ == "__main__":
    main()