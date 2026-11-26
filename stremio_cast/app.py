"""Local HTTP API + web UI: find a show via Stremio addons and cast it, with audio/subtitle choice."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import asdict
from pathlib import Path
from urllib.parse import unquote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query as Q, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

load_dotenv()

from .cast import Caster, Devices, discover  # noqa: E402
from .media import (  # noqa: E402
    CACHE, LANG2, Session, Sessions, Track, audio_needs_transcode, external_sub_path, fetch_external, remux_stream, save_session,
    shifted_vtt,
)
from .stremio import Query, StremioClient, parse_query  # noqa: E402

log = logging.getLogger("stremio-cast")

ADDON_URL = os.environ.get("STREAM_ADDON_URL", "")
if not ADDON_URL:
    raise SystemExit("STREAM_ADDON_URL is not set (see .env.example)")

PORT = int(os.environ.get("PORT", "8766"))
stremio = StremioClient(ADDON_URL)
_spec = os.environ.get("CHROMECASTS") or (
    f"{os.environ.get('CHROMECAST_NAME', '')}={os.environ.get('CHROMECAST_HOST', '')}"  # legacy single-device config
)
devices = Devices(_spec, os.environ.get("DEFAULT_CHROMECAST") or None)
DeviceParam = Q(None, description="Chromecast name, e.g. 'office'. Defaults to DEFAULT_CHROMECAST.")
FOLLOW_REDIRECT = os.environ.get("RESOLVE_REDIRECT", "true").lower() != "false"
CONTENT_TYPE = os.environ.get("CAST_CONTENT_TYPE") or None
STATIC = Path(__file__).parent / "static"


def _public_url() -> str:
    """Base URL the Chromecast uses to fetch subtitles / remuxed streams from this server."""
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    host = next((c.host for c in devices.casters.values() if c.host), "8.8.8.8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        ip = s.getsockname()[0]
    return f"http://{ip}:{PORT}"


PUBLIC_URL = _public_url()

sessions = Sessions()
sessions.resolve_final = lambda url: _final(url)  # _final is defined below
_streams_cache: dict[str, list[dict]] = {}
# What each device is currently playing (needed for remux-aware seeking and track switching)
_NP_FILE = CACHE / "now_playing.json"
_np_lock = threading.RLock()


def _load_now_playing() -> dict[str, dict]:
    try:
        return json.loads(_NP_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_now_playing() -> None:
    try:
        _NP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _NP_FILE.with_suffix(".part")
        tmp.write_text(json.dumps(now_playing))
        tmp.replace(_NP_FILE)
    except OSError as e:
        log.warning("couldn't save playback state: %s", e)


now_playing: dict[str, dict] = _load_now_playing()

app = FastAPI(title="stremio-cast")
# Chromecast requires CORS on side-loaded text tracks
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _caster(device: str | None) -> Caster:
    try:
        return devices.get(device)
    except LookupError as e:
        raise HTTPException(400, str(e))


def _final(url: str) -> str:
    return stremio.final_url(url) if FOLLOW_REDIRECT else url


# --------------------------------------------------------------------------- casting core


def _pick(tracks: list[Track], lang: str | None, subtitle: bool) -> Track | None:
    """Pick a track by language code/name ('eng', 'english', 'en'), preferring full over forced subs."""
    if not lang:
        return None
    l = lang.lower()
    def matches(t: Track) -> bool:
        return l in (t.lang.lower(), LANG2.get(t.lang, "").lower(), t.label.split(" ")[0].lower())
    cands = [t for t in tracks if matches(t) and (not subtitle or t.to_dict()["supported"])]
    if subtitle:
        cands.sort(key=lambda t: (t.forced, "cc" in t.title.lower() or "sdh" in t.title.lower()))
    return cands[0] if cands else None


def _attach_external(sess: Session, type_: str, stremio_id: str) -> None:
    """Look up OpenSubtitles results once per session (fast; doesn't touch the video file)."""
    if not sess.external:
        filename = unquote(sess.addon_url.rstrip("/").rsplit("/", 1)[-1])
        sess.external = stremio.subtitles(type_, stremio_id, filename)
    save_session(sess)


def _sub_file(sess: Session, sub: str) -> tuple[Path, str]:
    """Resolve a subtitle id ('e:<stream index>' embedded, 'o:<id>' OpenSubtitles) to a local VTT + language."""
    kind, _, ident = sub.partition(":")
    if kind == "o":
        ext = next((e for e in sess.external if e["id"] == sub), None)
        if not ext:
            raise HTTPException(400, f"Unknown subtitle {sub}")
        try:
            return fetch_external(sess, ext), ext["lang"]
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Couldn't download subtitle: {e}")
    if kind == "e" and ident.isdigit():
        track = next((t for t in sess.subs if t.index == int(ident)), None)
        if not track:
            raise HTTPException(400, f"No subtitle track {ident}")
        if not sessions.wait_subs(sess) or not sess.sub_path(track.index).exists():
            raise HTTPException(504, f"Embedded subtitles not available: {sess.sub_error or sess.sub_status}")
        return sess.sub_path(track.index), track.lang
    raise HTTPException(400, f"Bad subtitle id {sub!r}")


# Subtitle sync without reloading: the receiver can only switch between text tracks declared at load,
# so each cast declares copies of the subtitle pre-shifted around the chosen delay. A sync nudge then
# just activates a neighbouring copy (instant); going past the window re-casts around the new delay.
SYNC_STEP = 0.5
SYNC_SPAN = 10  # copies each side -> +/-5s window, 21 tracks


def _sync_tracks(sess: Session, sub: str, lang: str, base_delay: float, stream_start: float) -> list[dict]:
    key = sub.replace(":", "-")
    tracks = []
    for k in range(-SYNC_SPAN, SYNC_SPAN + 1):
        d = round(base_delay + k * SYNC_STEP, 3)
        offset = stream_start - d  # cue times shift by -offset; positive delay shows subtitles later
        tracks.append({"id": k + SYNC_SPAN + 1, "lang": lang, "name": f"{lang} {d:+.1f}s",
                       "url": f"{PUBLIC_URL}/media/{sess.id}/sub/{key}.vtt?offset={offset:.3f}"})
    return tracks


def cast_session(
    sess: Session, caster: Caster, audio: int | None, sub: str | None, start: float = 0.0,
    poster: str | None = None, sub_delay: float = 0.0,
) -> dict:
    default_audio = sess.default_audio.index if sess.default_audio else None
    if audio is None or not any(a.index == audio for a in sess.audio):
        audio = default_audio
    track = next((a for a in sess.audio if a.index == audio), None)
    transcode = audio_needs_transcode(track)
    remux = audio is not None and (audio != default_audio or transcode)

    tracks, active = None, None
    if sub:
        _, lang = _sub_file(sess, sub)
        tracks = _sync_tracks(sess, sub, LANG2.get(lang, lang), sub_delay, start if remux else 0.0)
        active = SYNC_SPAN + 1  # the middle copy = sub_delay exactly

    if remux:
        url = f"{PUBLIC_URL}/media/{sess.id}/stream.mp4?audio={audio}&start={start:.3f}"
        if transcode:
            url += "&aac=1"
        ctype, cast_start = "video/mp4", None
    else:
        url, ctype, cast_start = sess.url, CONTENT_TYPE, start

    try:
        caster.play(url, sess.title, content_type=ctype, tracks=tracks, active_track=active,
                    start=cast_start, thumb=poster)
    except LookupError as e:
        raise HTTPException(503, str(e))
    state = {"session": sess.id, "audio": audio, "sub": sub, "sub_delay": sub_delay, "sync_base": sub_delay,
             "remux": remux, "transcode": transcode,
             "offset": start if remux else 0.0, "title": sess.title, "poster": poster}
    with _np_lock:
        now_playing[caster.name] = state
        _save_now_playing()
    log.info("casting %s to %s (audio=%s%s sub=%s delay=%+.2f remux=%s start=%.0f)",
             sess.title, caster.name, audio, " ->aac" if transcode else "", sub, sub_delay, remux, start)
    return state


def _position(caster: Caster) -> float:
    st = caster.status()
    np = now_playing.get(caster.name) or {}
    return (st.get("position") or 0.0) + (np.get("offset") or 0.0)


# --------------------------------------------------------------------------- quick API (curl / shortcuts)


class PlayRequest(BaseModel):
    query: str | None = None  # "Show Name Season 1 Episode 4"
    title: str | None = None  # or structured fields
    season: int | None = None
    episode: int | None = None
    device: str | None = None  # e.g. "office"; defaults to DEFAULT_CHROMECAST
    audio: str | None = None  # language, e.g. "eng"
    subs: str | None = None  # language, e.g. "eng", or "off"
    subs_source: str | None = None  # "opensubtitles" (default, instant) or "embedded" (needs full download)
    sub_delay: float = 0.0  # seconds; positive shows subtitles later
    dry_run: bool = False


def _query(req: PlayRequest) -> Query:
    if req.query:
        return parse_query(req.query)
    if req.title:
        season = req.season if req.season is not None else (1 if req.episode is not None else None)
        return Query(req.title, season, req.episode)
    raise HTTPException(400, "Provide 'query' or 'title'")


def _label(res) -> str:
    label = res.name
    if res.episode is not None:
        label += f" S{res.season:02d}E{res.episode:02d}"
        if res.episode_name:
            label += f" - {res.episode_name}"
    return label


def _play(req: PlayRequest) -> dict:
    q = _query(req)
    caster = _caster(req.device)
    try:
        res = stremio.resolve(q)
    except LookupError as e:
        raise HTTPException(404, str(e))
    label = _label(res)
    out = {"label": label, "device": caster.name, **{k: v for k, v in asdict(res).items() if k != "url"}}
    if req.dry_run:
        return out
    wants_tracks = req.audio or (req.subs and req.subs.lower() != "off")
    if not wants_tracks:
        try:
            caster.play(_final(res.url), label, content_type=CONTENT_TYPE)
        except LookupError as e:
            raise HTTPException(503, str(e))
        with _np_lock:
            now_playing.pop(caster.name, None)
            _save_now_playing()
        return {**out, "casting": True}
    sess = sessions.prepare(label, res.url, _final)
    sid = f"{res.imdb_id}:{res.season}:{res.episode}" if res.episode is not None else res.imdb_id
    _attach_external(sess, res.type, sid)
    audio = _pick(sess.audio, req.audio, False)
    if req.audio and not audio:
        raise HTTPException(404, f"No {req.audio} audio track. Available: {[a.label for a in sess.audio]}")
    sub_id = sub_label = None
    if req.subs and req.subs.lower() != "off":
        want = req.subs.lower()
        ext = next((e for e in sess.external if want in (e["lang"], LANG2.get(e["lang"], ""))), None)
        if ext and req.subs_source != "embedded":
            sub_id, sub_label = ext["id"], f"{ext['lang']} (OpenSubtitles: {ext['release']})"
        elif (emb := _pick(sess.subs, req.subs, True)):
            sub_id, sub_label = f"e:{emb.index}", f"{emb.label} (embedded)"
        else:
            raise HTTPException(404, f"No {req.subs} subtitles found")
    state = cast_session(sess, caster, audio.index if audio else None, sub_id, sub_delay=req.sub_delay)
    return {**out, "casting": True, "audio": audio.label if audio else None, "subs": sub_label,
            "remux": state["remux"]}


@app.post("/play")
def play(req: PlayRequest, device: str | None = DeviceParam):
    if device and not req.device:
        req.device = device
    return _play(req)


@app.get("/play")
def play_get(q: str, device: str | None = DeviceParam, audio: str | None = None, subs: str | None = None,
             subs_source: str | None = None, sub_delay: float = 0.0, dry_run: bool = False):
    """GET /play?q=Show Name s1e4&device=office&audio=eng&subs=eng&sub_delay=-1.5"""
    return _play(PlayRequest(query=q, device=device, audio=audio, subs=subs, subs_source=subs_source,
                             sub_delay=sub_delay, dry_run=dry_run))


@app.get("/search")
def search(q: str):
    return _play(PlayRequest(query=q, dry_run=True))


def _control(device: str | None, fn, *args):
    try:
        fn(_caster(device), *args)
    except LookupError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@app.post("/pause")
def pause(device: str | None = DeviceParam):
    return _control(device, Caster.pause)


@app.post("/resume")
def resume(device: str | None = DeviceParam):
    return _control(device, Caster.resume)


@app.post("/stop")
def stop(device: str | None = DeviceParam):
    caster = _caster(device)
    with _np_lock:
        now_playing.pop(caster.name, None)
        _save_now_playing()
    return _control(device, Caster.stop)


@app.post("/seek")
def seek(seconds: float, device: str | None = DeviceParam):
    caster = _caster(device)
    np = now_playing.get(caster.name)
    if np and np["remux"]:  # remuxed stream isn't seekable on the receiver; restart it at the new point
        sess = sessions.get(np["session"])
        if sess:
            cast_session(sess, caster, np["audio"], np["sub"], max(0.0, seconds), np.get("poster"),
                         np.get("sub_delay", 0.0))
            return {"ok": True, "restarted": True}
    return _control(device, Caster.seek, seconds)


@app.post("/volume")
def volume(level: float, device: str | None = DeviceParam):
    return _control(device, Caster.volume, level)


@app.get("/status")
def status(device: str | None = DeviceParam):
    caster = _caster(device)
    try:
        st = caster.status()
    except LookupError as e:
        raise HTTPException(503, str(e))
    np = now_playing.get(caster.name)
    if np and st.get("title") == np["title"]:
        sess = sessions.get(np["session"])
        st["position"] = (st.get("position") or 0.0) + np["offset"]
        if sess and sess.duration:
            st["duration"] = sess.duration
        st.update({k: np.get(k) for k in ("session", "audio", "sub", "sub_delay", "remux")})
    return st


@app.get("/devices")
def list_devices():
    """Configured devices."""
    return devices.list()


@app.get("/devices/discover")
def discover_devices():
    """mDNS discovery (usually finds nothing under WSL2 NAT networking)."""
    return discover()


# --------------------------------------------------------------------------- web UI API


@app.get("/api/config")
def api_config():
    return {"devices": devices.list(), "public_url": PUBLIC_URL}


@app.get("/api/search")
def api_search(q: str):
    results = stremio.catalog_search(q)
    if results and q.strip():
        _remember("queries", q.strip(), key=lambda x: x.lower())
    return results


# Recent searches + recently opened shows, stored server-side so phone and PC share them
_RECENT_FILE = CACHE / "recent.json"
_recent_lock = threading.Lock()
RECENT_MAX = 12


def _read_recent() -> dict:
    try:
        d = json.loads(_RECENT_FILE.read_text())
        return {"queries": d.get("queries", []), "shows": d.get("shows", [])}
    except (OSError, ValueError):
        return {"queries": [], "shows": []}


def _write_recent(d: dict) -> None:
    _RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RECENT_FILE.with_suffix(".part")
    tmp.write_text(json.dumps(d))
    tmp.replace(_RECENT_FILE)


def _remember(kind: str, item, key=lambda x: x) -> None:
    with _recent_lock:
        d = _read_recent()
        d[kind] = [item] + [x for x in d[kind] if key(x) != key(item)][: RECENT_MAX - 1]
        _write_recent(d)


@app.get("/api/recent")
def api_recent():
    return _read_recent()


class RecentShow(BaseModel):
    id: str
    type: str
    name: str
    poster: str | None = None
    year: str | None = None


@app.post("/api/recent/show")
def api_recent_show(show: RecentShow):
    _remember("shows", show.model_dump(), key=lambda x: x["id"])
    return {"ok": True}


@app.delete("/api/recent")
def api_recent_clear(kind: str | None = None):
    """Clear recent queries/shows (kind=queries|shows), or both."""
    with _recent_lock:
        d = _read_recent()
        for k in ([kind] if kind in ("queries", "shows") else ["queries", "shows"]):
            d[k] = []
        _write_recent(d)
    return d


@app.delete("/api/recent/{kind}/{index}")
def api_recent_remove(kind: str, index: int):
    if kind not in ("queries", "shows"):
        raise HTTPException(400, "kind must be queries or shows")
    with _recent_lock:
        d = _read_recent()
        if 0 <= index < len(d[kind]):
            d[kind].pop(index)
            _write_recent(d)
    return d


@app.get("/api/meta/{type_}/{imdb_id}")
def api_meta(type_: str, imdb_id: str):
    return stremio.meta(type_, imdb_id)


@app.get("/api/streams/{type_}/{stremio_id}")
def api_streams(type_: str, stremio_id: str):
    """Streams from the addon. URLs stay server-side (they contain the debrid key)."""
    streams = stremio.streams(type_, stremio_id)
    _streams_cache[f"{type_}/{stremio_id}"] = streams
    return [{"i": i, "name": s.get("name", ""), "title": s.get("title", "")} for i, s in enumerate(streams)]


class PrepareRequest(BaseModel):
    type: str
    id: str
    stream: int = 0
    title: str


@app.post("/api/prepare")
def api_prepare(req: PrepareRequest):
    key = f"{req.type}/{req.id}"
    streams = _streams_cache.get(key) or stremio.streams(req.type, req.id)
    _streams_cache[key] = streams
    if not 0 <= req.stream < len(streams):
        raise HTTPException(404, "Stream not found")
    try:
        sess = sessions.prepare(req.title, streams[req.stream]["url"], _final)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't open stream: {e}")
    _attach_external(sess, req.type, req.id)
    return sess.to_dict()


@app.post("/api/session/{sid}/extract")
def api_extract(sid: str):
    """Start extracting embedded subtitles (reads the whole file; slow)."""
    sess = sessions.get(sid)
    if not sess:
        raise HTTPException(404, "Unknown session")
    sessions.ensure_extraction(sess)
    return sess.to_dict()


@app.get("/api/session/{sid}")
def api_session(sid: str):
    sess = sessions.get(sid)
    if not sess:
        raise HTTPException(404, "Unknown session")
    return sess.to_dict()


class CastRequest(BaseModel):
    session: str
    device: str | None = None
    audio: int | None = None
    sub: str | None = None  # "e:<index>" embedded or "o:<id>" OpenSubtitles
    sub_delay: float = 0.0
    start: float = 0.0
    poster: str | None = None


@app.post("/api/cast")
def api_cast(req: CastRequest):
    sess = sessions.get(req.session)
    if not sess:
        raise HTTPException(404, "Unknown session")
    return cast_session(sess, _caster(req.device), req.audio, req.sub, req.start, req.poster, req.sub_delay)


class TracksRequest(BaseModel):
    device: str | None = None
    audio: int | None = None
    sub: str | None = None
    sub_delay: float = 0.0


@app.post("/api/tracks")
def api_tracks(req: TracksRequest):
    """Switch audio/subtitles on what's playing: re-casts from the current position."""
    caster = _caster(req.device)
    np = now_playing.get(caster.name)
    sess = sessions.get(np["session"]) if np else None
    if not sess:
        raise HTTPException(409, "Nothing started from the web UI is playing on this device")
    pos = max(0.0, _position(caster) - 1)
    return cast_session(sess, caster, req.audio, req.sub, pos, np.get("poster"), req.sub_delay)


class DelayRequest(BaseModel):
    device: str | None = None
    delay: float  # seconds; positive shows subtitles later


@app.post("/api/sub_delay")
def api_sub_delay(req: DelayRequest):
    """Change subtitle timing on what's playing. Instant within the pre-loaded window, else re-casts."""
    caster = _caster(req.device)
    np = now_playing.get(caster.name)
    sess = sessions.get(np["session"]) if np else None
    if not sess or not np.get("sub"):
        raise HTTPException(409, "No subtitles from the web UI are playing on this device")
    k = round((req.delay - np["sync_base"]) / SYNC_STEP)
    on_grid = abs(np["sync_base"] + k * SYNC_STEP - req.delay) < 1e-6
    if abs(k) <= SYNC_SPAN and on_grid:
        try:
            caster.set_track(k + SYNC_SPAN + 1)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Couldn't switch subtitle track: {e}")
        with _np_lock:
            np["sub_delay"] = req.delay
            _save_now_playing()
        log.info("subtitle delay %+.2fs on %s (instant)", req.delay, caster.name)
        return {"instant": True, "sub_delay": req.delay}
    pos = max(0.0, _position(caster) - 1)
    cast_session(sess, caster, np["audio"], np["sub"], pos, np.get("poster"), req.delay)
    return {"instant": False, "sub_delay": req.delay}


# --------------------------------------------------------------------------- media served to the Chromecast


@app.api_route("/media/{sid}/stream.mp4", methods=["GET", "HEAD"])
async def media_stream(sid: str, request: Request, audio: int, start: float = 0.0, aac: bool = False):
    sess = sessions.get(sid)
    if not sess:
        raise HTTPException(404)
    headers = {"Accept-Ranges": "none", "Cache-Control": "no-store"}
    if request.method == "HEAD":
        return Response(media_type="video/mp4", headers=headers)
    return StreamingResponse(remux_stream(sess.url, audio, start, aac), media_type="video/mp4", headers=headers)


@app.get("/media/{sid}/sub/{key}.vtt")
def media_sub(sid: str, key: str, offset: float = 0.0):
    sess = sessions.get(sid)
    if not sess:
        raise HTTPException(404)
    kind, _, ident = key.partition("-")
    path = sess.sub_path(int(ident)) if kind == "e" and ident.isdigit() else external_sub_path(sess, f"o:{ident}")
    if not path.exists():
        raise HTTPException(404)
    return PlainTextResponse(shifted_vtt(path, offset), media_type="text/vtt; charset=utf-8")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines include debrid tokens
    print(f"Web UI: http://localhost:{PORT}   (Chromecast fetches media via {PUBLIC_URL})")
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=PORT)
