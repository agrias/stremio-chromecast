# stremio-cast

A small self-hosted API + web UI that finds a show or movie through your Stremio addons and casts it to a
Chromecast, with a choice of audio track, subtitles and subtitle timing.

- **Lookup:** Cinemeta (Stremio's catalog) for titles, seasons and episodes.
- **Streams:** your configured stream addon (e.g. Torrentio with a debrid service), exactly as Stremio uses it.
- **Subtitles:** the OpenSubtitles Stremio addon (instant), or tracks embedded in the file.
- **Casting:** `pychromecast` to the Default Media Receiver. No Stremio app needed on the TV.

## Quick start

```bash
git clone https://github.com/agrias/stremio-chromecast.git && cd stremio-chromecast
cp .env.example .env     # set STREAM_ADDON_URL, CHROMECASTS, DEFAULT_CHROMECAST, PUBLIC_URL
docker compose up -d --build
```

Open `http://<server-ip>:8766`. It works from a phone on the same network.

Requirements: the server must be on the same LAN as your Chromecasts, since the TV fetches subtitles and
remuxed audio from it. Without Docker: install `ffmpeg` and [uv](https://docs.astral.sh/uv/), then run `uv run stremio-cast`.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `STREAM_ADDON_URL` | Your stream addon's manifest URL, including debrid config. **Contains your API key; keep `.env` private.** |
| `CHROMECASTS` | `Name=IP` pairs, comma separated, e.g. `Living Room TV=192.168.1.50,Office TV=192.168.1.51` |
| `DEFAULT_CHROMECAST` | Device used when a request doesn't pass `?device=` |
| `PUBLIC_URL` | URL the TV uses to reach this server (`http://<server LAN IP>:8766`). Auto-detected on a plain Linux host or with `network_mode: host`; set it explicitly under Docker bridge networking, WSL or Docker Desktop |
| `PORT`, `HOST` | Listen address (default `0.0.0.0:8766`) |
| `CAST_CONTENT_TYPE` | Override the MIME type sent to the TV (try `video/mp4` if MKVs won't start) |
| `CAST_AUDIO_CODECS` | Audio codecs played as-is (default `aac,mp3,opus,vorbis,flac`); others are converted to AAC. Add `ac3,eac3` if your TV/soundbar does Dolby passthrough |
| `SUB_CONNECTIONS` | Parallel connections for embedded-subtitle extraction. Keep at `1` (see below) |
| `OPENSUBTITLES_ADDON_URL` | Subtitle addon (default `https://opensubtitles-v3.strem.io`) |

To find a Chromecast's name from its IP: `curl http://<ip>:8008/setup/eureka_info?params=name`.
Give the TVs and the server fixed IPs (DHCP reservations) so the config stays valid.

## How it works

1. `"Show Name S1E4"` is parsed and looked up on Cinemeta to get its IMDb ID.
2. Your stream addon is asked for `/stream/series/<imdb>:1:4.json`. Stream URLs stay server-side, because
   they contain your debrid key.
3. `ffprobe` reads the file header (~1 s) to list audio and subtitle tracks.
4. The TV is told to load the stream.

The Chromecast's Default Media Receiver ignores audio and subtitle tracks embedded in an MKV, so:

- **Default audio:** cast directly from the debrid CDN, with native seeking. The server does nothing during playback.
- **Dolby/DTS audio** (E-AC-3, AC-3, DTS, TrueHD): the Chromecast plays these silently unless the TV
  accepts passthrough, so the server converts just the audio to stereo AAC (video is copied) and streams it.
  Costs a few % of a core. Seeking restarts the stream.
- **Other audio:** the server remuxes (stream copy, no re-encode) video plus the chosen track to fragmented MP4
  and streams it to the TV. Seeking restarts the stream at the new position.
- **Subtitles (OpenSubtitles, default):** fetched in ~1 s, ranked by release-name similarity to your file,
  converted to WebVTT and side-loaded as a text track.
- **Subtitles (from the file):** extracted only if you pick one. MKV subtitles are interleaved through the
  whole file, so all of it must be read first: ~25 s for 1.8 GB on a fast connection, longer on slow ones
  or under Docker Desktop (its network proxy caps a single connection at ~15 MB/s). Image-based subs
  (PGS/VobSub) aren't supported; ASS styling is reduced to plain text. Results are cached.
- **Subtitle timing:** each cast declares 21 pre-shifted copies of the subtitle (±5 s in ½ s steps). A sync
  nudge just switches the active track, so it's instant with no reload. Going past the window re-casts once.
  Offsets are remembered per episode and subtitle.

`SUB_CONNECTIONS > 1` downloads with parallel ranged connections, but debrid CDNs are connection and
rate limited and may refuse your IP for a while, so the default is 1.

## Resource use

Measured with a 1080p, 1.8 GB MKV:

| State | RAM | CPU |
|---|---|---|
| Idle / direct play | ~70 MB | ~0% |
| Alternate-audio remux | +85–200 MB (ffmpeg) | ~10–20% of one core |
| Dolby → AAC audio conversion | +85–200 MB (ffmpeg) | ~15–30% of one core |
| Embedded-subtitle extraction | +85–200 MB (ffmpeg) | ~10% of a core at 15 MB/s; scales with throughput |

Nothing is re-encoded, so a Raspberry Pi 4/5 or any small box with 1 GB RAM is plenty.
Disk: about 1 MB of subtitles per episode where embedded subs were extracted. The Docker image is ~1 GB,
mostly Debian's ffmpeg.

## Stored data

Video is never written to disk. The `cache` volume holds:

- extracted and downloaded subtitles
- `session.json` per stream (track list and the addon URL, **which includes your debrid key**)
- `now_playing.json` (what each TV is playing, so controls survive restarts)
- `recent.json` (recent searches and opened shows, shared across devices)

Nothing expires automatically. Remove the volume (`docker compose down -v`) to clear it.

## API

```bash
curl 'localhost:8766/play?q=Show Name Season 1 Episode 4'               # default TV
curl 'localhost:8766/play?q=Show Name S1E4&device=office'               # another TV (fuzzy name match)
curl 'localhost:8766/play?q=Show Name S1E4&audio=eng&subs=eng'          # pick tracks by language
curl 'localhost:8766/play?q=Show Name S1E4&subs=eng&sub_delay=-1.5'     # subtitles 1.5 s earlier
curl 'localhost:8766/play?q=Show Name S1E4&subs=eng&subs_source=embedded'  # subs from the file (slow)
curl 'localhost:8766/play?q=Movie Title'
curl -X POST localhost:8766/play -H 'content-type: application/json' \
     -d '{"title":"Show Name","season":1,"episode":4,"audio":"eng","subs":"off"}'
curl 'localhost:8766/search?q=Show Name s1e4'   # dry run: what it would play
curl -X POST 'localhost:8766/pause'             # also /resume, /stop
curl -X POST 'localhost:8766/seek?seconds=600'
curl -X POST 'localhost:8766/volume?level=0.4'
curl localhost:8766/status
curl localhost:8766/devices                     # configured TVs
```

Every control endpoint takes `?device=`; without it, `DEFAULT_CHROMECAST` is used.
Query formats: `Season 1 Episode 4`, `S01E04`, `1x04`, `episode 4` (assumes season 1), or a movie title.
Interactive API docs: `http://localhost:8766/docs`.

## Security

There is no authentication. Anyone on your network can use the UI and API. Don't expose the port to the
internet.
