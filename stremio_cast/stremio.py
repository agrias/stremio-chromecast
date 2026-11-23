"""Find a playable stream for a show/movie using Stremio addons (Cinemeta + a stream addon)."""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass

import httpx

CINEMETA = "https://v3-cinemeta.strem.io"
OPENSUBS = os.environ.get("OPENSUBTITLES_ADDON_URL", "https://opensubtitles-v3.strem.io").removesuffix("/manifest.json").rstrip("/")

# OpenSubtitles addon uses mostly ISO 639-2/B codes plus a few of its own
_OS_LANG = {"zht": "chi", "zhe": "chi", "pob": "por", "spn": "spa", "ze": "chi"}


def _lang3(code: str) -> str:
    return _OS_LANG.get(code, code)


def _release_key(name: str) -> str:
    """Normalise a release name for fuzzy matching ('Show.Name.S01E04.1080p.WEB-DL-Group.mkv')."""
    name = re.sub(r"\.(mkv|mp4|avi|srt|ass)$", "", name.lower())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()

_EP_PATTERNS = [
    re.compile(r"\bs(?:eason)?\s*(\d{1,2})\s*,?\s*e(?:p(?:isode)?)?\s*(\d{1,4})\b", re.I),
    re.compile(r"\b(\d{1,2})x(\d{1,4})\b", re.I),
]


@dataclass
class Query:
    title: str
    season: int | None = None
    episode: int | None = None

    @property
    def is_series(self) -> bool:
        return self.episode is not None


def parse_query(text: str) -> Query:
    """'Show Name Season 1 Episode 4' / 'Show Name S01E04' / 'Show Name 1x04' / 'Movie Title'."""
    for pat in _EP_PATTERNS:
        m = pat.search(text)
        if m:
            title = (text[: m.start()] + text[m.end():]).strip(" -:,")
            return Query(title, int(m.group(1)), int(m.group(2)))
    # "Show episode 4" with no season -> season 1
    m = re.search(r"\b(?:ep(?:isode)?|e)\s*(\d{1,4})\b", text, re.I)
    if m:
        return Query(text[: m.start()].strip(" -:,"), 1, int(m.group(1)))
    return Query(text.strip())


@dataclass
class Resolved:
    imdb_id: str
    name: str
    type: str
    season: int | None
    episode: int | None
    episode_name: str | None
    stream_title: str
    url: str


class StremioClient:
    def __init__(self, stream_addon_url: str, timeout: float = 30):
        # Accept either the manifest URL or the base URL.
        base = stream_addon_url.strip().removesuffix("/manifest.json").rstrip("/")
        if base.startswith("stremio://"):
            base = "https://" + base.removeprefix("stremio://")
        self.addon_base = base
        self.http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) stremio-cast/0.1"},
        )

    def search(self, title: str, type_: str) -> dict:
        r = self.http.get(f"{CINEMETA}/catalog/{type_}/top/search={title}.json")
        r.raise_for_status()
        metas = r.json().get("metas", [])
        if not metas:
            raise LookupError(f"No {type_} found for '{title}'")
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        exact = [m for m in metas if norm(m["name"]) == norm(title)]
        return (exact or metas)[0]

    def catalog_search(self, title: str) -> list[dict]:
        out = []
        for type_ in ("series", "movie"):
            try:
                r = self.http.get(f"{CINEMETA}/catalog/{type_}/top/search={title}.json")
                r.raise_for_status()
                for m in r.json().get("metas", [])[:20]:
                    out.append({"id": m["id"], "type": type_, "name": m["name"],
                                "year": m.get("releaseInfo"), "poster": m.get("poster")})
            except httpx.HTTPError:
                pass
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        q = norm(title)
        out.sort(key=lambda m: (norm(m["name"]) != q, not norm(m["name"]).startswith(q)))
        return out

    def meta(self, type_: str, imdb_id: str) -> dict:
        r = self.http.get(f"{CINEMETA}/meta/{type_}/{imdb_id}.json")
        r.raise_for_status()
        m = r.json()["meta"]
        videos = sorted(
            ({"season": v.get("season"), "episode": v.get("episode"), "name": v.get("name") or v.get("title"),
              "released": v.get("released"), "thumbnail": v.get("thumbnail"), "overview": v.get("overview")}
             for v in m.get("videos", []) if v.get("episode") is not None),
            key=lambda v: (v["season"] == 0, v["season"], v["episode"]),
        )
        return {"id": m["id"], "type": type_, "name": m["name"], "poster": m.get("poster"),
                "background": m.get("background"), "description": m.get("description"),
                "year": m.get("releaseInfo"), "videos": videos}

    def subtitles(self, type_: str, stremio_id: str, filename: str | None = None) -> list[dict]:
        """Subtitles from the OpenSubtitles Stremio addon, best release-name match first."""
        try:
            r = self.http.get(f"{OPENSUBS}/subtitles/{type_}/{stremio_id}.json")
            r.raise_for_status()
            subs = r.json().get("subtitles", [])
        except (httpx.HTTPError, ValueError):
            return []
        target = _release_key(filename or "")
        out = []
        for s in subs:
            if not s.get("url"):
                continue
            release = s.get("movieReleaseName") or s.get("subtitleFileName") or ""
            score = difflib.SequenceMatcher(None, target, _release_key(release)).ratio() if target else 0.0
            out.append({"id": f"o:{s.get('id')}", "lang": _lang3(s.get("lang", "und")), "release": release,
                        "file": s.get("subtitleFileName"), "match": round(score, 2),
                        "hash_match": s.get("m") == "h", "url": s["url"]})
        out.sort(key=lambda x: (not x["hash_match"], -x["match"]))
        return out

    def episode_name(self, imdb_id: str, season: int, episode: int) -> str | None:
        try:
            r = self.http.get(f"{CINEMETA}/meta/series/{imdb_id}.json")
            for v in r.json()["meta"].get("videos", []):
                if v.get("season") == season and v.get("episode") == episode:
                    return v.get("name") or v.get("title")
        except Exception:
            pass
        return None

    def streams(self, type_: str, stremio_id: str) -> list[dict]:
        r = self.http.get(f"{self.addon_base}/stream/{type_}/{stremio_id}.json")
        r.raise_for_status()
        return [s for s in r.json().get("streams", []) if s.get("url")]

    def resolve(self, q: Query) -> Resolved:
        type_ = "series" if q.is_series else "movie"
        meta = self.search(q.title, type_)
        imdb_id = meta["id"]
        sid = f"{imdb_id}:{q.season}:{q.episode}" if q.is_series else imdb_id
        streams = self.streams(type_, sid)
        if not streams:
            raise LookupError(
                f"No playable streams for {meta['name']} ({sid}). "
                "Is your debrid key configured in the addon URL?"
            )
        s = streams[0]  # addon config already sorts by quality/size
        return Resolved(
            imdb_id=imdb_id,
            name=meta["name"],
            type=type_,
            season=q.season,
            episode=q.episode,
            episode_name=self.episode_name(imdb_id, q.season, q.episode) if q.is_series else None,
            stream_title=s.get("title") or s.get("name", ""),
            url=s["url"],
        )

    def final_url(self, url: str) -> str:
        """Follow the addon's /resolve redirect to the debrid CDN link.

        This also makes the debrid service fetch the torrent if it isn't cached yet,
        and gives the Chromecast a direct URL (some receivers dislike redirects).
        """
        with self.http.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
            r.raise_for_status()
            return str(r.url)
