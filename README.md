# YouTube Legacy SOCKS Proxy for IE6 / Win9x Clients

This project runs a local web proxy that:

- Looks up YouTube videos and formats with `yt-dlp`
- Pulls stream bytes through an optional SOCKS proxy
- Serves a simple HTML 4 + IE6-friendly UI
- Exposes direct proxy stream links so old browsers can hand off to external players
- Supports channel following with a simple local feed view
- Supports channel browsing (by URL, handle, or discovery search)

## What this solves

Older browsers (IE6 on Windows 98/ME/2000-era setups) cannot run modern YouTube JS/CSS or modern TLS flows reliably. This proxy moves that complexity to a modern host and gives the old browser a very simple page.

## Requirements (host machine)

- Python 3.10+
- Network access to YouTube
- Optional: a SOCKS5 proxy endpoint (for example `socks5h://127.0.0.1:1080`)

## Install

```powershell
python -m venv .venv
.\.venv\bin\Activate.ps1
pip install -r requirements.txt
```

## Run

Without SOCKS:

```powershell
python app.py
```

With SOCKS:

```powershell
$env:SOCKS_PROXY = "socks5h://127.0.0.1:1080"
python app.py
```

Optional environment variables:

- `BIND_HOST` (default `0.0.0.0`)
- `BIND_PORT` (default `8098`)
- `TOKEN_TTL_SECONDS` (default `3600`)
- `SEARCH_LIMIT` (default `10`)
- `CHANNEL_FEED_LIMIT` (default `12`)
- `FOLLOWING_FILE` (default `following_channels.txt`)
- `MAX_FORMAT_OPTIONS` (default `80`)

## Use with old Windows 9x + IE6

1. Run this app on a newer machine in the same network.
2. In IE6 on Windows 98 SE, open: `http://<proxy-host-ip>:8098/`
3. Search videos or paste a full YouTube URL.
4. The watch page now avoids auto-start inline embed by default so the page can finish loading on old browsers.
5. Use the `Click to try inline playback` link only when you want embedded playback.
6. If inline playback fails in IE6, use one of the `Proxy Stream Links`.
7. For VLC specifically:
	- `VLC M3U` uses direct relay for the selected stream.
	- `VLC Safe A/V` uses direct relay of a safer muxed stream.
8. Use `Follow Channel` on a video page to save that channel.
9. Open `View Followed Channels` to browse followed channels and see latest uploads.
10. Use `Open Channel` to browse any channel by URL, `@handle`, or `UC...` ID.
11. Use `Find Channels` to discover channels related to a keyword and open their feeds.
12. Inside a channel feed, use separate pages for `Videos`, `Streams`, `Shorts`, and `Playlists`.
13. Use `Search In Channel` to find uploads within a specific channel.
14. Use `Previous Page` and `Next Page` to browse all results in each method.

## Notes on compatibility

- The UI intentionally uses old-school HTML/CSS and avoids modern layout features.
- Streams are relayed from the host machine, so the old client does not need direct TLS support to YouTube.
- Some videos may not offer legacy-friendly muxed formats; in those cases, available links are still shown when possible.

## Troubleshooting

- If logs show `GET /relay/<token> ... 403`, the proxy now auto-refreshes stale YouTube stream URLs once and retries.
- If it still fails, reload the watch page to generate fresh links and try another listed format.
- `Find this channel` now prefers direct channel URL metadata when available, then resolves from video ID, then falls back to channel-name discovery.
- `VLC M3U` uses the current relay token from the selected format link.
- If a stream plays only audio, pick a format marked `[Legacy A/V]` (H.264 + AAC) in the format label.

## Security warning

- Do not expose this service directly to the public internet.
- This is a local network utility and has minimal hardening.
# YT98
# YT98
