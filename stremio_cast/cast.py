"""Thin wrapper around pychromecast for a single configured device."""

from __future__ import annotations

import mimetypes
import threading
from urllib.parse import unquote, urlparse

import pychromecast

CONTENT_TYPES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".m3u8": "application/x-mpegURL",
}


def guess_content_type(url: str) -> str:
    path = unquote(urlparse(url).path).lower()
    for ext, ct in CONTENT_TYPES.items():
        if path.endswith(ext):
            return ct
    return mimetypes.guess_type(path)[0] or "video/mp4"


def discover(timeout: float = 5, known_hosts: list[str] | None = None) -> list[dict]:
    casts, browser = pychromecast.get_chromecasts(timeout=timeout, known_hosts=known_hosts)
    browser.stop_discovery()
    return [
        {"name": c.cast_info.friendly_name, "host": c.cast_info.host, "model": c.cast_info.model_name}
        for c in casts
    ]


class Caster:
    def __init__(self, name: str | None, host: str | None):
        if not name and not host:
            raise ValueError("Set CHROMECAST_NAME and/or CHROMECAST_HOST")
        self.name, self.host = name, host
        self._cast: pychromecast.Chromecast | None = None
        self._lock = threading.Lock()

    def device(self) -> pychromecast.Chromecast:
        with self._lock:
            if self._cast is not None and self._cast.socket_client.is_connected:
                return self._cast
            casts, browser = pychromecast.get_listed_chromecasts(
                friendly_names=[self.name] if self.name else None,
                known_hosts=[self.host] if self.host else None,
                discovery_timeout=10,
            )
            browser.stop_discovery()
            if not casts:
                raise LookupError(f"Chromecast not found (name={self.name!r}, host={self.host!r})")
            cast = casts[0]
            cast.wait(timeout=10)
            self._cast = cast
            return cast

    def play(
        self,
        url: str,
        title: str,
        content_type: str | None = None,
        tracks: list[dict] | None = None,
        active_track: int | None = None,
        start: float | None = None,
        thumb: str | None = None,
    ):
        """Load media. `tracks` are side-loaded text tracks: [{"id", "url", "lang", "name"}].

        Every track must be declared at load time; switching between them later (set_track) is instant.
        """
        media_info = None
        if tracks:
            media_info = {
                "tracks": [
                    {"trackId": t["id"], "trackContentId": t["url"], "language": t["lang"], "subtype": "SUBTITLES",
                     "type": "TEXT", "trackContentType": "text/vtt", "name": t["name"]}
                    for t in tracks
                ],
                "textTrackStyle": {"backgroundColor": "#FFFFFF00", "edgeType": "OUTLINE", "edgeColor": "#000000FF"},
            }
        mc = self.device().media_controller
        mc.play_media(
            url,
            content_type or guess_content_type(url),
            title=title,
            thumb=thumb,
            current_time=start or None,
            stream_type="BUFFERED",
            media_info=media_info,
        )
        mc.block_until_active(timeout=20)
        if tracks and active_track is not None:
            self.set_track(active_track)

    def set_track(self, track_id: int | None):
        mc = self.device().media_controller
        if track_id is None:
            mc.disable_subtitle()
        else:
            mc.enable_subtitle(track_id)

    def pause(self):
        self.device().media_controller.pause()

    def resume(self):
        self.device().media_controller.play()

    def stop(self):
        self.device().media_controller.stop()

    def seek(self, seconds: float):
        self.device().media_controller.seek(seconds)

    def volume(self, level: float):
        self.device().set_volume(max(0.0, min(1.0, level)))

    def status(self) -> dict:
        cast = self.device()
        s = cast.media_controller.status
        return {
            "device": cast.cast_info.friendly_name,
            "app": cast.app_display_name,
            "state": s.player_state,
            "title": s.title,
            "position": s.adjusted_current_time,
            "duration": s.duration,
            "volume": cast.status.volume_level if cast.status else None,
        }


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


class Devices:
    """Named Chromecasts from config, e.g. CHROMECASTS="Living Room TV=192.168.1.50,Office TV=192.168.1.51".

    Lookup is forgiving: "office", "Office TV" and "office-tv" all match "Office TV".
    """

    def __init__(self, spec: str, default: str | None = None):
        self.casters: dict[str, Caster] = {}
        for entry in filter(None, (e.strip() for e in spec.split(","))):
            name, _, host = entry.partition("=")
            self.casters[name.strip()] = Caster(name.strip(), host.strip() or None)
        if not self.casters:
            raise ValueError("No Chromecasts configured (set CHROMECASTS)")
        self.default = self._match(default) if default else next(iter(self.casters))

    def _match(self, name: str) -> str:
        n = _norm(name)
        exact = [k for k in self.casters if _norm(k) == n]
        partial = [k for k in self.casters if n and n in _norm(k)]
        hits = exact or partial
        if len(hits) != 1:
            known = ", ".join(self.casters)
            raise LookupError(f"Unknown or ambiguous device {name!r} (known: {known})")
        return hits[0]

    def get(self, name: str | None = None) -> Caster:
        return self.casters[self._match(name) if name else self.default]

    def list(self) -> list[dict]:
        return [{"name": k, "host": c.host, "default": k == self.default} for k, c in self.casters.items()]
