"""Inspect a stream's tracks, extract subtitles to WebVTT, and remux with a chosen audio track.

The Chromecast Default Media Receiver doesn't expose MKV-embedded audio/subtitle tracks, so:
  * subtitles are extracted (one ffmpeg pass for all text tracks) and side-loaded as WebVTT
  * a non-default audio track is selected by remuxing (no re-encode) to fragmented MP4 on the fly
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CACHE = Path(os.environ.get("CACHE_DIR", Path.home() / ".cache" / "stremio-cast"))

TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}

LANGS = {
    "eng": "English", "jpn": "Japanese", "ara": "Arabic", "fre": "French", "fra": "French",
    "ger": "German", "deu": "German", "hin": "Hindi", "ita": "Italian", "pol": "Polish",
    "por": "Portuguese", "spa": "Spanish", "fil": "Filipino", "tam": "Tamil", "tel": "Telugu",
    "tha": "Thai", "rus": "Russian", "chi": "Chinese", "zho": "Chinese", "kor": "Korean",
    "vie": "Vietnamese", "ind": "Indonesian", "may": "Malay", "msa": "Malay", "tur": "Turkish",
    "dut": "Dutch", "nld": "Dutch", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "gre": "Greek", "ell": "Greek", "heb": "Hebrew", "hun": "Hungarian",
    "cze": "Czech", "ces": "Czech", "rum": "Romanian", "ron": "Romanian", "ukr": "Ukrainian",
    "und": "Unknown",
}
# ISO 639-2 -> BCP-47-ish for the Cast text track
LANG2 = {"eng": "en", "jpn": "ja", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es",
         "ita": "it", "por": "pt", "rus": "ru", "chi": "zh", "zho": "zh", "kor": "ko", "ara": "ar",
         "hin": "hi", "pol": "pl", "tha": "th", "fil": "fil", "tam": "ta", "tel": "te"}


@dataclass
class Track:
    index: int  # absolute ffmpeg stream index
    kind: str  # audio | subtitle
    codec: str
    lang: str
    title: str
    default: bool
    forced: bool
    channels: int | None = None

    @property
    def label(self) -> str:
        name = LANGS.get(self.lang, self.lang or "Unknown")
        bits = [name]
        if self.title and self.title.lower() not in name.lower():
            bits.append(f"({self.title})")
        if self.kind == "audio" and self.channels:
            bits.append({1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(self.channels, f"{self.channels}ch"))
        if self.forced and "forced" not in self.title.lower():
            bits.append("[forced]")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {"index": self.index, "kind": self.kind, "codec": self.codec, "lang": self.lang,
                "title": self.title, "default": self.default, "forced": self.forced, "label": self.label,
                "supported": self.kind != "subtitle" or self.codec in TEXT_SUB_CODECS}


@dataclass
class Session:
    id: str
    title: str
    addon_url: str
    url: str  # direct (post-redirect) media URL
    duration: float | None
    video_codec: str | None
    audio: list[Track]
    subs: list[Track]
    sub_status: str = "idle"  # idle | running | done | error | none  (embedded-track extraction)
    sub_progress: float = 0.0
    sub_error: str | None = None
    created: float = field(default_factory=time.time)
    external: list[dict] = field(default_factory=list)  # OpenSubtitles results
    _sub_done: threading.Event = field(default_factory=threading.Event)
    _extract_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def dir(self) -> Path:
        return CACHE / self.id

    @property
    def default_audio(self) -> Track | None:
        return next((a for a in self.audio if a.default), self.audio[0] if self.audio else None)

    def sub_path(self, index: int) -> Path:
        return self.dir / f"sub{index}.vtt"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "duration": self.duration, "video_codec": self.video_codec,
            "audio": [a.to_dict() for a in self.audio],
            "subtitles": [s.to_dict() for s in self.subs],
            "default_audio": self.default_audio.index if self.default_audio else None,
            "sub_status": self.sub_status, "sub_progress": round(self.sub_progress, 3),
            "sub_error": self.sub_error,
            "external": [{k: v for k, v in e.items() if k != "url"} for e in self.external],
        }


def session_id(addon_url: str) -> str:
    return hashlib.sha1(addon_url.encode()).hexdigest()[:16]


def probe(url: str) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", url],
        capture_output=True, text=True, timeout=90,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout)


def build_session(sid: str, title: str, addon_url: str, url: str) -> Session:
    info = probe(url)
    audio, subs, vcodec = [], [], None
    for s in info.get("streams", []):
        kind = s.get("codec_type")
        tags, disp = s.get("tags", {}), s.get("disposition", {})
        if kind == "video" and vcodec is None and not disp.get("attached_pic"):
            vcodec = s.get("codec_name")
        if kind not in ("audio", "subtitle"):
            continue
        t = Track(
            index=s["index"], kind=kind, codec=s.get("codec_name", "?"),
            lang=tags.get("language", "und"), title=tags.get("title", ""),
            default=bool(disp.get("default")), forced=bool(disp.get("forced")),
            channels=s.get("channels"),
        )
        (audio if kind == "audio" else subs).append(t)
    dur = info.get("format", {}).get("duration")
    sess = Session(sid, title, addon_url, url, float(dur) if dur else None, vcodec, audio, subs)
    text = [t for t in subs if t.codec in TEXT_SUB_CODECS]
    if text and all(sess.sub_path(t.index).exists() for t in text):
        sess.sub_status, sess.sub_progress = "done", 1.0  # extracted on an earlier run
        sess._sub_done.set()
    return sess


# >1 downloads with several ranged connections (faster under Docker Desktop's slow network proxy).
# Off by default: TorBox's CDN is connection/rate limited and parallel full-file reads got this IP
# temporarily refused during testing.
SUB_CONNECTIONS = int(os.environ.get("SUB_CONNECTIONS", "1"))
CHUNK = 4 * 1024 * 1024  # in flight at once: ~(connections + 2) chunks
CHUNK_DEADLINE = 30.0  # seconds per chunk before retrying on a fresh connection


def _content_length(url: str) -> int | None:
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        with c.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
            cr = r.headers.get("content-range", "")
            return int(cr.rsplit("/", 1)[1]) if "/" in cr and not cr.endswith("*") else None


def _parallel_feed(url: str, size: int, out, on_bytes) -> None:
    """Download `url` with several ranged connections, writing to `out` in order.

    A single connection is slow under Docker Desktop's network proxy (~15 MB/s vs ~75 on the host),
    and subtitles are interleaved through the whole file, so the full file has to be read.
    """
    def fetch(start: int) -> bytes:
        end = min(size, start + CHUNK) - 1
        for attempt in range(5):
            buf = bytearray()
            deadline = time.monotonic() + CHUNK_DEADLINE
            try:
                with client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as r:
                    r.raise_for_status()
                    for part in r.iter_raw(256 * 1024):
                        buf += part
                        if time.monotonic() > deadline:  # CDN connection trickling/stalled: retry
                            raise httpx.ReadTimeout("chunk deadline exceeded")
                if len(buf) != end - start + 1:
                    raise httpx.ReadError(f"short read {len(buf)}/{end - start + 1}")
                return bytes(buf)
            except httpx.HTTPError:
                if attempt == 4:
                    raise
                time.sleep(1 + attempt)
        raise AssertionError

    offsets = list(range(0, size, CHUNK))
    with httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True) as client, \
            ThreadPoolExecutor(SUB_CONNECTIONS) as pool:
        window: deque = deque()
        it = iter(offsets)
        for off in it:
            window.append(pool.submit(fetch, off))
            if len(window) >= SUB_CONNECTIONS + 2:
                break
        while window:
            data = window.popleft().result()
            out.write(data)
            on_bytes(len(data))
            nxt = next(it, None)
            if nxt is not None:
                window.append(pool.submit(fetch, nxt))


def extract_subs(sess: Session) -> None:
    """One pass over the file writing every text subtitle track to WebVTT. Runs in a thread."""
    targets = [s for s in sess.subs if s.codec in TEXT_SUB_CODECS]
    if not targets:
        sess.sub_status = "none"
        sess._sub_done.set()
        return
    if all(sess.sub_path(s.index).exists() for s in targets):
        sess.sub_status, sess.sub_progress = "done", 1.0
        sess._sub_done.set()
        return
    sess.dir.mkdir(parents=True, exist_ok=True)
    sess.sub_status = "running"
    tmp = {s.index: sess.dir / f"sub{s.index}.vtt.part" for s in targets}
    try:
        size = _content_length(sess.url) if SUB_CONNECTIONS > 1 else None
    except httpx.HTTPError:
        size = None
    src = "pipe:0" if size else sess.url
    args = [FFMPEG, "-v", "error", "-y", "-progress", "pipe:1", "-i", src]
    for s in targets:
        args += ["-map", f"0:{s.index}", "-c:s", "webvtt", "-f", "webvtt", str(tmp[s.index])]
    feeder_err: list[BaseException] = []
    try:
        p = subprocess.Popen(args, stdin=subprocess.PIPE if size else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        if size:
            done = [0]

            def progress(n: int) -> None:
                done[0] += n
                sess.sub_progress = min(1.0, done[0] / size)

            def feed() -> None:
                try:
                    _parallel_feed(sess.url, size, p.stdin, progress)
                except BrokenPipeError:
                    pass  # ffmpeg finished/failed early; its exit code tells us which
                except BaseException as e:  # noqa: BLE001
                    feeder_err.append(e)
                finally:
                    try:
                        p.stdin.close()  # type: ignore[union-attr]
                    except OSError:
                        pass

            threading.Thread(target=feed, daemon=True).start()
        for line in p.stdout:  # type: ignore[union-attr]
            m = re.match(rb"out_time_us=(\d+)", line)
            if not size and m and sess.duration:
                sess.sub_progress = min(1.0, int(m.group(1)) / 1e6 / sess.duration)
        err = p.stderr.read().decode(errors="replace")  # type: ignore[union-attr]
        if p.wait() != 0 or feeder_err:
            raise RuntimeError(str(feeder_err[0]) if feeder_err else (err.strip()[-300:] or f"ffmpeg exit {p.returncode}"))
        for idx, path in tmp.items():
            path.replace(sess.sub_path(idx))
        sess.sub_status, sess.sub_progress = "done", 1.0
    except Exception as e:  # noqa: BLE001
        sess.sub_status, sess.sub_error = "error", str(e)
        for path in tmp.values():
            path.unlink(missing_ok=True)
    finally:
        sess._sub_done.set()
        _release_memory()


def _release_memory() -> None:
    """Return freed download buffers to the OS (glibc otherwise keeps them mapped)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


_TS = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")


def _fmt(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def shifted_vtt(path: Path, offset: float) -> str:
    """Shift cue times by -offset (used when the remuxed stream starts mid-file)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not offset:
        return text

    def conv(m: re.Match) -> str:
        h, mi, s, ms = m.groups()
        return _fmt(int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000 - offset)

    cue_lines = []
    for block in text.split("\n\n"):
        if "-->" in block:
            lines = block.split("\n")
            i = next(i for i, ln in enumerate(lines) if "-->" in ln)
            start_end = lines[i].split("-->")
            end = _TS.search(start_end[1])
            if end and conv(end) == "00:00:00.000":
                continue  # cue entirely before the start point
            lines[i] = _TS.sub(conv, lines[i])
            block = "\n".join(lines)
        cue_lines.append(block)
    return "\n\n".join(cue_lines)


def save_session(sess: Session) -> None:
    """Persist session metadata so controls keep working across server restarts."""
    sess.dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": sess.id, "title": sess.title, "addon_url": sess.addon_url, "duration": sess.duration,
        "video_codec": sess.video_codec, "external": sess.external,
        "audio": [asdict(t) for t in sess.audio], "subs": [asdict(t) for t in sess.subs],
    }
    tmp = sess.dir / "session.json.part"
    tmp.write_text(json.dumps(data))
    tmp.replace(sess.dir / "session.json")


def load_session(sid: str, resolve_final) -> Session | None:
    path = CACHE / re.sub(r"[^0-9a-f]", "", sid) / "session.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    # the direct debrid URL may have expired while we were down, so resolve it again
    url = resolve_final(d["addon_url"])
    sess = Session(d["id"], d["title"], d["addon_url"], url, d["duration"], d["video_codec"],
                   [Track(**t) for t in d["audio"]], [Track(**t) for t in d["subs"]], external=d["external"])
    text = [t for t in sess.subs if t.codec in TEXT_SUB_CODECS]
    if text and all(sess.sub_path(t.index).exists() for t in text):
        sess.sub_status, sess.sub_progress = "done", 1.0
        sess._sub_done.set()
    return sess


class Sessions:
    def __init__(self):
        self._by_id: dict[str, Session] = {}
        self._lock = threading.Lock()
        self.resolve_final = None  # set by the app; used to revive sessions saved before a restart

    def get(self, sid: str) -> Session | None:
        sess = self._by_id.get(sid)
        if sess is None and self.resolve_final is not None:
            try:
                sess = load_session(sid, self.resolve_final)
            except Exception:  # noqa: BLE001
                return None
            if sess:
                with self._lock:
                    sess = self._by_id.setdefault(sid, sess)
        return sess

    def prepare(self, title: str, addon_url: str, resolve_final) -> Session:
        sid = session_id(addon_url)
        with self._lock:
            sess = self._by_id.get(sid)
        if sess:
            return sess
        url = resolve_final(addon_url)
        sess = build_session(sid, title, addon_url, url)
        with self._lock:
            sess = self._by_id.setdefault(sid, sess)
        return sess

    def ensure_extraction(self, sess: Session) -> None:
        """Start the (full-file) embedded subtitle extraction if it isn't running/done."""
        with sess._extract_lock:
            if sess.sub_status in ("idle", "error"):
                sess.sub_status, sess.sub_error, sess.sub_progress = "running", None, 0.0
                sess._sub_done.clear()
                threading.Thread(target=extract_subs, args=(sess,), daemon=True).start()

    def wait_subs(self, sess: Session, timeout: float = 300) -> bool:
        self.ensure_extraction(sess)
        return sess._sub_done.wait(timeout)


async def remux_stream(url: str, audio_index: int, start: float = 0.0):
    """Async generator yielding fragmented MP4 with video + one chosen audio track (stream copy)."""
    args = [FFMPEG, "-v", "error", "-nostdin"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += [
        "-i", url, "-map", "0:v:0", "-map", f"0:{audio_index}", "-c", "copy",
        "-sn", "-dn", "-map_chapters", "-1",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        while True:
            chunk = await proc.stdout.read(256 * 1024)  # type: ignore[union-attr]
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


# --------------------------------------------------------------------------- external (OpenSubtitles) subtitles


def external_sub_path(sess: Session, ext_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ext_id)
    return sess.dir / f"os_{safe}.vtt"


def fetch_external(sess: Session, ext: dict) -> Path:
    """Download an OpenSubtitles file and convert it to WebVTT with ffmpeg (handles srt/ass/sub)."""
    out = external_sub_path(sess, ext["id"])
    if out.exists():
        return out
    sess.dir.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".src")
    r = httpx.get(ext["url"], timeout=30, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 stremio-cast/0.1"})
    r.raise_for_status()
    raw.write_bytes(r.content)
    p = subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-sub_charenc", "UTF-8", "-i", str(raw), "-f", "webvtt", str(out) + ".part"],
        capture_output=True, text=True, timeout=60,
    )
    raw.unlink(missing_ok=True)
    if p.returncode != 0:
        Path(str(out) + ".part").unlink(missing_ok=True)
        raise RuntimeError(f"Couldn't convert subtitle: {p.stderr.strip()[-200:]}")
    Path(str(out) + ".part").replace(out)
    return out
