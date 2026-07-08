import os
import secrets
import shutil
import subprocess
import threading
import time
from glob import glob
from html import escape
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse
from urllib.parse import quote
from urllib.parse import urlencode

import requests
from flask import Flask, Response, abort, redirect, render_template_string, request, url_for
from yt_dlp import YoutubeDL

app = Flask(__name__)

SOCKS_PROXY = os.getenv("SOCKS_PROXY", "").strip()
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("BIND_PORT", "8098"))
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))
SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "10"))
CHANNEL_FEED_LIMIT = int(os.getenv("CHANNEL_FEED_LIMIT", "12"))
FOLLOWING_FILE = os.getenv("FOLLOWING_FILE", "following_channels.txt")
MAX_FORMAT_OPTIONS = int(os.getenv("MAX_FORMAT_OPTIONS", "80"))
VLC_TRANSCODE_ENABLED = (os.getenv("VLC_TRANSCODE_ENABLED", "1").strip().lower() not in {"0", "false", "no"})
FFMPEG_BIN = (os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg")
TRANSCODE_HEIGHT = int(os.getenv("TRANSCODE_HEIGHT", "0"))


@dataclass
class StreamToken:
    url: str
    title: str
    ext: str
    source_video: str
    format_id: str
    http_headers: Dict[str, str]
    created_at: float


TOKENS: Dict[str, StreamToken] = {}
TOKENS_LOCK = threading.Lock()
FOLLOW_LOCK = threading.Lock()


def cleanup_tokens() -> None:
    now = time.time()
    expired: List[str] = []
    with TOKENS_LOCK:
        for token, item in TOKENS.items():
            if now - item.created_at > TOKEN_TTL_SECONDS:
                expired.append(token)
        for token in expired:
            TOKENS.pop(token, None)


def store_stream_token(
    url: str,
    title: str,
    ext: str,
    source_video: str,
    format_id: str,
    http_headers: Optional[Dict[str, str]] = None,
) -> str:
    cleanup_tokens()
    token = secrets.token_urlsafe(18)
    with TOKENS_LOCK:
        TOKENS[token] = StreamToken(
            url=url,
            title=title,
            ext=ext,
            source_video=source_video,
            format_id=format_id,
            http_headers=http_headers or {},
            created_at=time.time(),
        )
    return token


def get_stream_token(token: str) -> Optional[StreamToken]:
    cleanup_tokens()
    with TOKENS_LOCK:
        item = TOKENS.get(token)
    return item


def resolve_ffmpeg_bin() -> Optional[str]:
    # If user provides an explicit executable path, respect it.
    if os.path.isabs(FFMPEG_BIN) and os.path.exists(FFMPEG_BIN):
        return FFMPEG_BIN

    found = shutil.which(FFMPEG_BIN)
    if found:
        return found

    # Fallback to Python-managed FFmpeg binary when available.
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass

    # Windows fallback: resolve common winget package install path directly.
    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if local_appdata:
        pattern = os.path.join(
            local_appdata,
            "Microsoft",
            "WinGet",
            "Packages",
            "Gyan.FFmpeg_*",
            "**",
            "bin",
            "ffmpeg.exe",
        )
        matches = glob(pattern, recursive=True)
        if matches:
            matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            return matches[0]

    return None


def ffmpeg_available() -> bool:
    return resolve_ffmpeg_bin() is not None


def choose_source_video(raw: str, info: Dict) -> str:
    webpage = (info.get("webpage_url") or "").strip()
    if webpage:
        return webpage
    video_id = (info.get("id") or "").strip()
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return raw


def build_upstream_headers(item: StreamToken, include_range: bool = True) -> Dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }

    # yt-dlp often provides required request headers for the extracted CDN URL.
    for key, value in (item.http_headers or {}).items():
        if key and value:
            headers[key] = value

    header_names = ["Referer", "Origin"]
    if include_range:
        header_names.insert(0, "Range")

    for name in header_names:
        value = request.headers.get(name, "")
        if value:
            headers[name] = value

    return headers


def refresh_token_stream(token: str, item: StreamToken) -> Optional[StreamToken]:
    if not item.source_video:
        return None

    try:
        info = extract_video_info(item.source_video)
    except Exception:
        return None

    playable = collect_playable_formats(info)
    if not playable:
        return None

    chosen = None
    for fmt in playable:
        fmt_id = str(fmt.get("format_id") or "")
        if item.format_id and fmt_id == item.format_id:
            chosen = fmt
            break
    if chosen is None:
        chosen = playable[0]

    new_url = chosen.get("url")
    if not new_url:
        return None

    updated = StreamToken(
        url=new_url,
        title=item.title,
        ext=(chosen.get("ext") or item.ext or "bin").lower(),
        source_video=item.source_video,
        format_id=str(chosen.get("format_id") or item.format_id or ""),
        http_headers=dict(chosen.get("http_headers") or item.http_headers or {}),
        created_at=time.time(),
    )

    with TOKENS_LOCK:
        TOKENS[token] = updated

    return updated


def load_followed_channels() -> Dict[str, str]:
    with FOLLOW_LOCK:
        if not os.path.exists(FOLLOWING_FILE):
            return {}

        channels: Dict[str, str] = {}
        with open(FOLLOWING_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                row = line.strip()
                if not row:
                    continue
                parts = row.split("\t", 1)
                url = parts[0].strip()
                if not url:
                    continue
                name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else url
                channels[url] = name
        return channels


def save_followed_channels(channels: Dict[str, str]) -> None:
    with FOLLOW_LOCK:
        temp_file = FOLLOWING_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as handle:
            for url in sorted(channels.keys()):
                handle.write(f"{url}\t{channels[url]}\n")
        os.replace(temp_file, FOLLOWING_FILE)


def add_followed_channel(url: str, name: str) -> None:
    channels = load_followed_channels()
    channels[url] = name or url
    save_followed_channels(channels)


def remove_followed_channel(url: str) -> None:
    channels = load_followed_channels()
    channels.pop(url, None)
    save_followed_channels(channels)


def ytdlp_options() -> Dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "noplaylist": True,
        "socket_timeout": 20,
        "cachedir": "cache",
    }
    if SOCKS_PROXY:
        opts["proxy"] = SOCKS_PROXY
    return opts


def proxy_dict() -> Optional[Dict[str, str]]:
    if not SOCKS_PROXY:
        return None
    return {"http": SOCKS_PROXY, "https": SOCKS_PROXY}


def extract_video_info(video_query: str) -> Dict:
    with YoutubeDL(ytdlp_options()) as ydl:
        return ydl.extract_info(video_query, download=False)


def search_videos(query: str, limit: int) -> List[Dict]:
    search_expr = f"ytsearch{limit}:{query}"
    info = extract_video_info(search_expr)
    entries = info.get("entries") or []

    results: List[Dict] = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        results.append(
            {
                "id": video_id,
                "title": entry.get("title") or "Untitled",
                "duration": entry.get("duration") or 0,
                "uploader": entry.get("uploader") or "Unknown",
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "channel_url": entry.get("channel_url") or entry.get("uploader_url") or "",
            }
        )
    return results


def canonical_channel_videos_url(raw: str) -> str:
    return canonical_channel_tab_url(raw, "videos", "")


def normalize_channel_tab(value: str) -> str:
    tab = (value or "videos").strip().lower()
    allowed = {"videos", "streams", "shorts", "playlists", "search"}
    if tab in allowed:
        return tab
    return "videos"


def canonical_channel_tab_url(raw: str, tab: str, search_query: str) -> str:
    tab_name = normalize_channel_tab(tab)
    value = raw.strip()
    if not value:
        return ""

    base = ""

    # Raw channel id passed directly.
    if value.startswith("UC") and "/" not in value:
        base = f"https://www.youtube.com/channel/{value}"
    elif value.startswith("@"):
        base = f"https://www.youtube.com/{value}"
    elif value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        path = (parsed.path or "").strip("/")
        if path.startswith("channel/"):
            parts = path.split("/")
            if len(parts) >= 2:
                base = f"https://www.youtube.com/channel/{parts[1]}"
        elif path.startswith("@"):
            handle = path.split("/", 1)[0]
            base = f"https://www.youtube.com/{handle}"
        elif path.startswith("user/"):
            parts = path.split("/")
            if len(parts) >= 2:
                base = f"https://www.youtube.com/user/{parts[1]}"
        elif path.startswith("c/"):
            parts = path.split("/")
            if len(parts) >= 2:
                base = f"https://www.youtube.com/c/{parts[1]}"
        elif path:
            base = f"https://www.youtube.com/{path.split('/', 1)[0]}"
        else:
            base = "https://www.youtube.com"
    elif value.startswith("channel/"):
        parts = value.split("/")
        if len(parts) >= 2:
            base = f"https://www.youtube.com/channel/{parts[1]}"
    else:
        base = f"https://www.youtube.com/{value}"

    target = f"{base}/{tab_name}"
    if tab_name == "search":
        query_text = (search_query or "").strip()
        if query_text:
            target = target + "?" + urlencode({"query": query_text})
    return target


def channel_videos(
    channel_url: str,
    limit: int,
    tab: str = "videos",
    search_query: str = "",
    page: int = 1,
) -> tuple[List[Dict], bool]:
    tab_name = normalize_channel_tab(tab)
    target_url = canonical_channel_tab_url(channel_url, tab_name, search_query)
    opts = ytdlp_options()
    opts["extract_flat"] = True
    safe_page = max(1, int(page))
    start_index = ((safe_page - 1) * limit) + 1
    # Request one extra item so we can detect whether another page exists.
    opts["playliststart"] = start_index
    opts["playlistend"] = start_index + limit
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(target_url, download=False)
        except Exception:
            # Fallbacks for rare channel_url shapes (e.g., raw UC id carried through metadata).
            uc_id = ""
            if channel_url.startswith("UC") and "/" not in channel_url:
                uc_id = channel_url
            elif "/channel/UC" in channel_url:
                uc_id = channel_url.rsplit("/channel/", 1)[-1].split("/", 1)[0]

            if not uc_id:
                raise

            fallback_target = canonical_channel_tab_url(
                f"https://www.youtube.com/channel/{uc_id}", tab_name, search_query
            )
            info = ydl.extract_info(fallback_target, download=False)

    channel_name = (
        (info.get("channel") or "").strip()
        or (info.get("uploader") or "").strip()
        or (info.get("title") or "").strip()
    )

    entries = info.get("entries") or []
    has_more = len(entries) > limit
    results: List[Dict] = []
    for entry in entries[:limit]:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        uploader = (
            (entry.get("uploader") or "").strip()
            or (entry.get("channel") or "").strip()
            or channel_name
            or "Unknown"
        )
        results.append(
            {
                "id": video_id,
                "title": entry.get("title") or "Untitled",
                "duration": entry.get("duration") or 0,
                "uploader": uploader,
            }
        )
    return results, has_more


def normalize_channel_input(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("@"):
        return f"https://www.youtube.com/{value}"
    if value.startswith("UC"):
        return f"https://www.youtube.com/channel/{value}"
    return f"https://www.youtube.com/{value}"


def is_channel_locator(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.startswith("http://") or text.startswith("https://"):
        return True
    if text.startswith("@"):
        return True
    if text.startswith("UC") and " " not in text:
        return True
    if text.startswith("channel/"):
        return True
    if "/" in text:
        return True
    return False


def discover_channels(query: str, limit: int) -> List[Dict[str, str]]:
    videos = search_videos(query, limit)
    found: Dict[str, str] = {}

    for item in videos:
        video_id = item.get("id") or ""
        if not video_id:
            continue
        try:
            info = extract_video_info(f"https://www.youtube.com/watch?v={video_id}")
        except Exception:
            continue

        channel_url = (info.get("channel_url") or info.get("uploader_url") or "").strip()
        channel_name = (info.get("channel") or info.get("uploader") or channel_url).strip()
        if not channel_url:
            continue
        if channel_url not in found:
            found[channel_url] = channel_name or channel_url

    channels = [{"url": url, "name": found[url]} for url in sorted(found.keys())]
    return channels


def format_duration(seconds: int) -> str:
    if not seconds:
        return "?:??"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def compatibility_rank(fmt: Dict) -> tuple[int, int, int, int, int, int]:
    ext = (fmt.get("ext") or "").lower()
    protocol = (fmt.get("protocol") or "").lower()
    height = int(fmt.get("height") or 0)
    tbr = int(fmt.get("tbr") or 0)
    audio_missing = 1 if fmt.get("acodec") == "none" else 0
    vcodec = (fmt.get("vcodec") or "").lower()
    acodec = (fmt.get("acodec") or "").lower()

    ext_score = {
        "mp4": 0,
        "3gp": 1,
        "flv": 2,
        "m4v": 3,
        "webm": 5,
    }.get(ext, 9)

    protocol_penalty = 0 if ("http" in protocol or "https" in protocol) else 3

    # Heavily prefer formats that older players commonly decode.
    legacy_video_ok = any(tag in vcodec for tag in ["avc", "h264", "mp4v", "h263"])
    legacy_audio_ok = any(tag in acodec for tag in ["aac", "mp4a", "mp3"])
    legacy_penalty = 0 if (legacy_video_ok and legacy_audio_ok and audio_missing == 0) else 3

    # Favor lower resolutions first for legacy hardware, then bitrate.
    if height <= 240:
        resolution_band = 0
    elif height <= 360:
        resolution_band = 1
    elif height <= 480:
        resolution_band = 2
    else:
        resolution_band = 3

    return (legacy_penalty, audio_missing, ext_score, protocol_penalty, resolution_band, tbr)


def is_legacy_av_format(fmt: Dict) -> bool:
    if fmt.get("acodec") == "none" or fmt.get("vcodec") == "none":
        return False

    vcodec = (fmt.get("vcodec") or "").lower()
    acodec = (fmt.get("acodec") or "").lower()
    ext = (fmt.get("ext") or "").lower()

    ext_ok = ext in {"mp4", "m4v", "3gp", "flv"}
    video_ok = any(tag in vcodec for tag in ["avc", "h264", "mp4v", "h263"])
    audio_ok = any(tag in acodec for tag in ["aac", "mp4a", "mp3"])
    return ext_ok and video_ok and audio_ok


def collect_playable_formats(info: Dict) -> List[Dict]:
    formats = info.get("formats") or []
    playable = []
    audio_only = []
    seen_ids = set()
    for fmt in formats:
        if not fmt:
            continue
        if not fmt.get("url"):
            continue
        has_video = fmt.get("vcodec") != "none"
        fmt_id = str(fmt.get("format_id") or "")
        if fmt_id and fmt_id in seen_ids:
            continue
        if fmt_id:
            seen_ids.add(fmt_id)
        if has_video:
            playable.append(fmt)
        else:
            audio_only.append(fmt)

    # If a video has no direct video tracks available, still expose audio links.
    if not playable and audio_only:
        playable = audio_only

    playable.sort(key=compatibility_rank)
    return playable


def choose_preferred_format(formats: List[Dict]) -> Optional[Dict]:
    if not formats:
        return None

    muxed_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    legacy_muxed = [f for f in muxed_formats if is_legacy_av_format(f)]

    if legacy_muxed:
        return legacy_muxed[0]
    if muxed_formats:
        return muxed_formats[0]
    return formats[0]


def find_format_by_id(formats: List[Dict], format_id: str) -> Optional[Dict]:
    wanted = (format_id or "").strip()
    if not wanted:
        return None
    for fmt in formats:
        if str(fmt.get("format_id") or "") == wanted:
            return fmt
    return None


def is_muxed_format(fmt: Optional[Dict]) -> bool:
    if not fmt:
        return False
    return fmt.get("vcodec") != "none" and fmt.get("acodec") != "none"


def make_label(fmt: Dict) -> str:
    ext = (fmt.get("ext") or "bin").upper()
    height = fmt.get("height") or "?"
    fps = fmt.get("fps") or "?"
    tbr = int(fmt.get("tbr") or 0)
    note = fmt.get("format_note") or ""
    vcodec = (fmt.get("vcodec") or "?").lower()
    acodec = (fmt.get("acodec") or "?").lower()
    has_video = fmt.get("vcodec") != "none"
    has_audio = fmt.get("acodec") != "none"
    if has_video and has_audio:
        stream_kind = "video+audio"
    elif has_video:
        stream_kind = "video-only"
    else:
        stream_kind = "audio-only"
    legacy_tag = " [Legacy A/V]" if is_legacy_av_format(fmt) else ""
    return (
        f"{ext} {height}p @ {fps}fps, {tbr} kbps {note} "
        f"({stream_kind}, v:{vcodec}, a:{acodec}){legacy_tag}"
    ).strip()


BASE_TEMPLATE = """<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\" \"http://www.w3.org/TR/html4/loose.dtd\">
<html>
<head>
<meta http-equiv=\"Content-Type\" content=\"text/html; charset=utf-8\">
<title>{{ title }}</title>
<style type=\"text/css\">
body {
  margin: 0;
  padding: 0;
  background: #f0f0e8;
  color: #111;
  font-family: Tahoma, Verdana, Arial, sans-serif;
  font-size: 14px;
}
#wrap {
  width: 900px;
  margin: 16px auto;
  border: 1px solid #999;
  background: #fff;
}
#header {
  background: #b30000;
  color: #fff;
  padding: 12px;
}
#header h1 {
  margin: 0;
  font-size: 22px;
}
#content {
  padding: 14px;
}
.search-row {
  margin-bottom: 14px;
}
label {
  font-weight: bold;
}
input.text {
  width: 520px;
  padding: 4px;
  border: 1px solid #666;
}
input.button {
  border: 1px solid #333;
  background: #ececec;
  padding: 4px 10px;
}
.notice {
  border: 1px solid #dad2a4;
  background: #fff8c8;
  padding: 8px;
  margin-bottom: 14px;
}
.result {
  border-bottom: 1px solid #ddd;
  padding: 8px 0;
}
.result h3 {
  margin: 0 0 4px 0;
  font-size: 16px;
}
.meta {
  color: #555;
  font-size: 12px;
}
.video-box {
  border: 1px solid #444;
  background: #000;
  width: 640px;
  height: 360px;
}
.small {
  color: #666;
  font-size: 12px;
}
ul.formats {
  margin: 8px 0 0 20px;
  padding: 0;
}
ul.formats li {
  margin: 4px 0;
}
a { color: #003399; }
a:visited { color: #663366; }
</style>
<!--[if lte IE 6]>
<style type=\"text/css\">
#wrap { width: 96%; }
input.text { width: 70%; }
.video-box { width: 100%; height: 320px; }
</style>
<![endif]-->
</head>
<body>
<div id=\"wrap\">
  <div id=\"header\">
    <h1>YouTube Legacy Proxy</h1>
  </div>
  <div id=\"content\">
    {{ body|safe }}
  </div>
</div>
</body>
</html>
"""


HOME_BODY = """
<div class=\"notice\">
  Enter a search term, video URL, or video ID. This proxy fetches data through SOCKS if configured.
    <br><a href=\"{{ url_for('following') }}\">View Followed Channels</a>
</div>
<form method=\"get\" action=\"{{ url_for('search') }}\" class=\"search-row\">
  <label for=\"q\">Search:</label>
  <input id=\"q\" name=\"q\" type=\"text\" class=\"text\" value=\"{{ query }}\">
  <input type=\"submit\" class=\"button\" value=\"Find\">
</form>
<form method=\"get\" action=\"{{ url_for('watch') }}\">
  <label for=\"v\">Video URL or ID:</label>
  <input id=\"v\" name=\"v\" type=\"text\" class=\"text\" value=\"{{ video_query }}\">
  <input type=\"submit\" class=\"button\" value=\"Open\">
</form>
<form method=\"get\" action=\"{{ url_for('channel_feed') }}\" class=\"search-row\">
    <label for=\"channel\">Open Channel (URL, @handle, or UC ID):</label>
    <input id=\"channel\" name=\"channel\" type=\"text\" class=\"text\" value=\"\">
    <input type=\"submit\" class=\"button\" value=\"Browse\">
</form>
<form method=\"get\" action=\"{{ url_for('browse_channels') }}\" class=\"search-row\">
    <label for=\"cq\">Find Channels:</label>
    <input id=\"cq\" name=\"q\" type=\"text\" class=\"text\" value=\"\">
    <input type=\"submit\" class=\"button\" value=\"Discover\">
</form>
"""


@app.route("/")
def home() -> str:
    body = render_template_string(HOME_BODY, query="", video_query="")
    return render_template_string(BASE_TEMPLATE, title="YouTube Legacy Proxy", body=body)


@app.route("/search")
def search() -> str:
    query = (request.args.get("q") or "").strip()
    if not query:
        return redirect(url_for("home"))

    try:
        results = search_videos(query, SEARCH_LIMIT)
    except Exception as exc:  # pragma: no cover
        body = (
            "<div class='notice'><b>Search failed.</b><br>"
            + f"Error: {exc}"
            + "</div>"
            + render_template_string(HOME_BODY, query=query, video_query="")
        )
        return render_template_string(BASE_TEMPLATE, title="Search Error", body=body)

    block = [
        "<div class='notice'>",
        f"Search results for <b>{query}</b>",
        "</div>",
    ]
    block.append(render_template_string(HOME_BODY, query=query, video_query=""))

    if not results:
        block.append("<p>No results.</p>")
    else:
        for item in results:
            block.append("<div class='result'>")
            block.append(f"<h3><a href='{url_for('watch')}?v={quote(item['id'])}'>{item['title']}</a></h3>")
            block.append(
                "<div class='meta'>"
                + f"Uploader: {item['uploader']} | Duration: {format_duration(item['duration'])}"
                + "</div>"
            )
            direct_channel_url = (item.get("channel_url") or "").strip()
            if direct_channel_url:
                link = url_for("channel_feed") + "?url=" + quote(direct_channel_url) + "&tab=videos"
            else:
                uploader_hint = item.get("channel") or item.get("uploader") or ""
                link = (
                    url_for("go_channel")
                    + "?video="
                    + quote(item["id"])
                    + "&q="
                    + quote(uploader_hint)
                )
            block.append(f"<div class='small'><a href='{link}'>Find this channel</a></div>")
            block.append("</div>")

    body = "\n".join(block)
    return render_template_string(BASE_TEMPLATE, title=f"Search: {query}", body=body)


@app.route("/watch")
def watch() -> str:
    raw = (request.args.get("v") or "").strip()
    if not raw:
        return redirect(url_for("home"))

    video_query = raw
    if "youtube.com" in raw or "youtu.be" in raw:
        video_query = raw

    try:
        info = extract_video_info(video_query)
    except Exception as exc:  # pragma: no cover
        body = (
            "<div class='notice'><b>Load failed.</b><br>"
            + f"Error: {exc}"
            + "</div>"
            + render_template_string(HOME_BODY, query="", video_query=raw)
        )
        return render_template_string(BASE_TEMPLATE, title="Watch Error", body=body)

    title = info.get("title") or "Untitled"
    uploader = info.get("uploader") or "Unknown"
    duration = format_duration(int(info.get("duration") or 0))
    channel_name = info.get("channel") or uploader
    channel_url = info.get("channel_url") or info.get("uploader_url") or ""
    followed_channels = load_followed_channels()
    is_followed = bool(channel_url and channel_url in followed_channels)

    inline_mode = (request.args.get("inline") or "0") == "1"
    source_video = choose_source_video(raw, info)

    formats = collect_playable_formats(info)
    muxed_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    legacy_muxed_formats = [f for f in muxed_formats if is_legacy_av_format(f)]

    if legacy_muxed_formats:
        top_formats = (legacy_muxed_formats + [f for f in muxed_formats if f not in legacy_muxed_formats])[
            :MAX_FORMAT_OPTIONS
        ]
        format_notice = "Showing only muxed video+audio formats (legacy-friendly first)."
    elif muxed_formats:
        top_formats = muxed_formats[:MAX_FORMAT_OPTIONS]
        format_notice = "Showing only muxed video+audio formats."
    else:
        top_formats = []
        format_notice = "No muxed video+audio formats found for this video."

    preferred_first = None
    if legacy_muxed_formats:
        preferred_first = legacy_muxed_formats[0]
    if preferred_first is None and muxed_formats:
        preferred_first = muxed_formats[0]
    if preferred_first is None and top_formats:
        preferred_first = top_formats[0]

    format_rows = []
    first_token = None
    for fmt in top_formats:
        stream_url = fmt.get("url")
        ext = (fmt.get("ext") or "bin").lower()
        format_id = str(fmt.get("format_id") or "")
        http_headers = dict(fmt.get("http_headers") or {})
        label = make_label(fmt)
        token = store_stream_token(
            stream_url,
            title,
            ext,
            source_video=source_video,
            format_id=format_id,
            http_headers=http_headers,
        )
        if preferred_first is fmt:
            first_token = token
        proxy_link = url_for("relay", token=token)
        vlc_link = url_for("vlc_playlist", token=token)
        vlc_safe_link = vlc_link + "?safe=1"
        format_rows.append(
            f"<li><a href='{proxy_link}'>{label}</a> | <a href='{vlc_link}'>VLC M3U</a> | <a href='{vlc_safe_link}'>VLC Safe A/V</a></li>"
        )
    if first_token is None and top_formats:
        # Fallback if identity comparison misses equivalent dict objects.
        first_fmt_id = str((preferred_first or top_formats[0]).get("format_id") or "")
        for fmt in top_formats:
            if str(fmt.get("format_id") or "") == first_fmt_id:
                stream_url = fmt.get("url")
                ext = (fmt.get("ext") or "bin").lower()
                format_id = str(fmt.get("format_id") or "")
                http_headers = dict(fmt.get("http_headers") or {})
                first_token = store_stream_token(
                    stream_url,
                    title,
                    ext,
                    source_video=source_video,
                    format_id=format_id,
                    http_headers=http_headers,
                )
                break

    if not first_token:
        video_block = "<p>No browser-playable muxed formats were found for this video.</p>"
    else:
        first_link = url_for("relay", token=first_token)
        if inline_mode:
            # object/embed keeps compatibility for older plugin-based playback paths in IE6.
            video_block = (
                "<div class='video-box'>"
                f"<object width='640' height='360' data='{first_link}' type='video/mp4'>"
                f"<embed src='{first_link}' width='640' height='360'></embed>"
                "</object>"
                "</div>"
            )
        else:
            inline_link = url_for("watch") + f"?v={quote(raw)}&inline=1"
            video_block = (
                "<div class='notice'>"
                "Page load optimization is active for old browsers. "
                f"<a href='{inline_link}'>Click to try inline playback</a>."
                "</div>"
            )

    body = "\n".join(
        [
            render_template_string(HOME_BODY, query="", video_query=raw),
            "<div class='result'>",
            f"<h3>{escape(title)}</h3>",
            f"<div class='meta'>Uploader: {escape(uploader)} | Duration: {duration}</div>",
            (
                f"<div class='small'><a href='{url_for('channel_feed')}?channel={quote(channel_url)}'>Browse channel feed</a></div>"
                if channel_url
                else ""
            ),
            "</div>",
            (
                "<div class='notice'>"
                + (
                    "<form method='post' action='"
                    + url_for("unfollow_channel")
                    + "'>"
                    + f"<input type='hidden' name='channel_url' value='{escape(channel_url)}'>"
                    + f"<input type='hidden' name='next' value='{escape(request.path + '?v=' + quote(raw))}'>"
                    + "<input type='submit' class='button' value='Unfollow Channel'>"
                    + "</form>"
                    if channel_url and is_followed
                    else (
                        "<form method='post' action='"
                        + url_for("follow_channel")
                        + "'>"
                        + f"<input type='hidden' name='channel_url' value='{escape(channel_url)}'>"
                        + f"<input type='hidden' name='channel_name' value='{escape(channel_name)}'>"
                        + f"<input type='hidden' name='next' value='{escape(request.path + '?v=' + quote(raw))}'>"
                        + (
                            "<input type='submit' class='button' value='Follow Channel'>"
                            if channel_url
                            else "No channel URL available for this video."
                        )
                        + "</form>"
                    )
                )
                + "</div>"
            ),
            video_block,
            f"<p class='small'>{format_notice}</p>",
            "<p class='small'>Tip: if embedded playback fails in IE6, use a Proxy Stream Link below to open/download in an external player.</p>",
            "<h3>Proxy Stream Links</h3>",
            "<ul class='formats'>",
            "\n".join(format_rows) if format_rows else "<li>No compatible stream links found.</li>",
            "</ul>",
        ]
    )

    return render_template_string(BASE_TEMPLATE, title=title, body=body)


@app.route("/relay/<token>", methods=["GET", "HEAD"])
def relay(token: str):
    item = get_stream_token(token)
    if not item:
        abort(404, description="Expired or invalid stream token")

    method = request.method
    vlc_linear = (request.args.get("vlc") or "0") == "1"

    def fetch_once(stream_item: StreamToken):
        return requests.request(
            method=method,
            url=stream_item.url,
            stream=(method != "HEAD"),
            timeout=(10, None),
            headers=build_upstream_headers(stream_item, include_range=not vlc_linear),
            proxies=proxy_dict(),
            allow_redirects=True,
        )

    try:
        upstream = fetch_once(item)
        if upstream.status_code in (401, 403, 410):
            upstream.close()
            refreshed = refresh_token_stream(token, item)
            if refreshed is not None:
                upstream = fetch_once(refreshed)
    except requests.RequestException as exc:
        return Response(f"Upstream fetch failed: {exc}", status=502, mimetype="text/plain")

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response_headers = {
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
        "Cache-Control": "no-store",
        "Accept-Ranges": "none" if vlc_linear else upstream.headers.get("Accept-Ranges", "bytes"),
        "Connection": "close",
    }

    passthrough_headers = [
        "Content-Length",
        "Content-Range",
        "Content-Disposition",
        "Last-Modified",
        "ETag",
    ]
    if vlc_linear:
        passthrough_headers = [h for h in passthrough_headers if h != "Content-Range"]
    for header_name in passthrough_headers:
        if header_name in upstream.headers:
            response_headers[header_name] = upstream.headers[header_name]

    status = upstream.status_code
    if method == "HEAD":
        upstream.close()
        return Response(status=status, headers=response_headers)
    return Response(generate(), status=status, headers=response_headers)


@app.route("/health")
def health() -> Dict[str, str]:
    ffmpeg_path = resolve_ffmpeg_bin()
    return {
        "status": "ok",
        "socks_proxy": "configured" if SOCKS_PROXY else "not-configured",
        "ffmpeg": "available" if ffmpeg_path else "not-found",
        "ffmpeg_bin": ffmpeg_path or "",
    }


@app.route("/transcode/<token>.ts", methods=["GET", "HEAD"])
def transcode(token: str):
    item = get_stream_token(token)
    if not item:
        abort(404, description="Expired or invalid stream token")

    fallback_url = url_for("relay", token=token) + "?vlc=1"
    ffmpeg_exe = resolve_ffmpeg_bin()
    if not VLC_TRANSCODE_ENABLED or not ffmpeg_exe:
        return redirect(fallback_url)

    response_headers = {
        "Content-Type": "video/mp2t",
        "Cache-Control": "no-store",
        "Connection": "close",
        "Accept-Ranges": "none",
    }

    if request.method == "HEAD":
        return Response(status=200, headers=response_headers)

    source_url = request.url_root.rstrip("/") + url_for("relay", token=token) + "?vlc=1"
    ffmpeg_cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source_url,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-profile:v",
        "baseline",
        "-level",
        "3.0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-ar",
        "44100",
    ]
    if TRANSCODE_HEIGHT > 0:
        ffmpeg_cmd.extend(["-vf", f"scale=-2:{TRANSCODE_HEIGHT}"])
    ffmpeg_cmd.extend(["-f", "mpegts", "pipe:1"])

    try:
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        return Response(f"FFmpeg start failed: {exc}", status=502, mimetype="text/plain")

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(128 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            if proc.stdout:
                proc.stdout.close()

    return Response(generate(), status=200, headers=response_headers)


@app.route("/vlc/<token>.m3u")
def vlc_playlist(token: str):
    item = get_stream_token(token)
    if not item:
        abort(404, description="Expired or invalid stream token")

    playlist_token = token
    safe_mode = (request.args.get("safe") or "0") == "1"

    # In safe mode, VLC playlist is remapped to a preferred muxed stream.
    # Default mode keeps the exact selected format so quality links stay distinct.
    if safe_mode and item.source_video:
        try:
            info = extract_video_info(item.source_video)
            formats = collect_playable_formats(info)
            selected = find_format_by_id(formats, item.format_id)
            chosen = selected if is_muxed_format(selected) else choose_preferred_format(formats)
            if chosen and chosen.get("url"):
                playlist_token = store_stream_token(
                    url=chosen.get("url"),
                    title=item.title,
                    ext=(chosen.get("ext") or item.ext or "bin").lower(),
                    source_video=item.source_video,
                    format_id=str(chosen.get("format_id") or ""),
                    http_headers=dict(chosen.get("http_headers") or {}),
                )
        except Exception:
            # Keep original token path if refresh fails.
            playlist_token = token

    if safe_mode:
        stream_url = request.url_root.rstrip("/") + url_for("relay", token=playlist_token)
    else:
        stream_url = request.url_root.rstrip("/") + url_for("transcode", token=playlist_token)
    content = "#EXTM3U\n" + f"#EXTINF:-1,{item.title}\n{stream_url}\n"
    return Response(content, mimetype="audio/x-mpegurl")


@app.route("/following")
def following() -> str:
    channels = load_followed_channels()

    rows: List[str] = [
        "<div class='notice'>",
        "Followed channels are stored locally on this proxy host.",
        "</div>",
        render_template_string(HOME_BODY, query="", video_query=""),
        "<h3>Followed Channels</h3>",
    ]

    if not channels:
        rows.append("<p>No channels followed yet.</p>")
    else:
        rows.append("<ul class='formats'>")
        for channel_url in sorted(channels.keys()):
            name = channels[channel_url]
            feed_link = url_for("channel_feed") + "?url=" + quote(channel_url)
            rows.append(
                "<li>"
                + f"<a href='{feed_link}'>{escape(name)}</a>"
                + " &nbsp;"
                + "<form method='post' action='"
                + url_for("unfollow_channel")
                + "' style='display:inline;'>"
                + f"<input type='hidden' name='channel_url' value='{escape(channel_url)}'>"
                + f"<input type='hidden' name='next' value='{url_for('following')}'>"
                + "<input type='submit' class='button' value='Unfollow'>"
                + "</form>"
                + "</li>"
            )
        rows.append("</ul>")

    body = "\n".join(rows)
    return render_template_string(BASE_TEMPLATE, title="Followed Channels", body=body)


@app.route("/channel-feed")
def channel_feed() -> str:
    channel_url = (request.args.get("url") or "").strip()
    channel_input = (request.args.get("channel") or "").strip()
    tab = normalize_channel_tab(request.args.get("tab", "videos"))
    channel_search = (request.args.get("channel_search") or "").strip()
    page = max(1, int(request.args.get("page", "1") or "1"))
    if channel_input:
        if not is_channel_locator(channel_input):
            return redirect(url_for("browse_channels") + "?q=" + quote(channel_input))
        channel_url = normalize_channel_input(channel_input)

    if tab == "search" and not channel_search:
        tab = "videos"

    if not channel_url:
        return redirect(url_for("following"))

    try:
        videos, has_more = channel_videos(
            channel_url,
            CHANNEL_FEED_LIMIT,
            tab=tab,
            search_query=channel_search,
            page=page,
        )
    except Exception as exc:  # pragma: no cover
        body = (
            "<div class='notice'><b>Channel load failed.</b><br>"
            + f"Error: {escape(str(exc))}"
            + "</div>"
            + f"<p><a href='{url_for('following')}'>Back to followed channels</a></p>"
        )
        return render_template_string(BASE_TEMPLATE, title="Channel Feed Error", body=body)

    channels = load_followed_channels()
    channel_name = channels.get(channel_url, channel_url)

    tab_routes = {
        "videos": url_for("channel_videos_page"),
        "streams": url_for("channel_streams_page"),
        "shorts": url_for("channel_shorts_page"),
        "playlists": url_for("channel_playlists_page"),
    }
    tab_links = []
    for tab_name, label in [
        ("videos", "Videos"),
        ("streams", "Streams"),
        ("shorts", "Shorts"),
        ("playlists", "Playlists"),
    ]:
        link = tab_routes[tab_name] + "?url=" + quote(channel_url) + "&page=1"
        tab_links.append(f"<a href='{link}'>{label}</a>")

    search_tab_form = (
        "<form method='get' action='"
        + url_for("channel_feed")
        + "' class='search-row'>"
        + f"<input type='hidden' name='url' value='{escape(channel_url)}'>"
        + "<input type='hidden' name='tab' value='search'>"
        + "<input type='hidden' name='page' value='1'>"
        + "<label for='channel_search'>Search In Channel:</label>"
        + f"<input id='channel_search' name='channel_search' type='text' class='text' value='{escape(channel_search)}'>"
        + "<input type='submit' class='button' value='Go'>"
        + "</form>"
    )

    active_tab_text = tab.capitalize() if tab != "search" else "Search Results"
    if tab == "search" and channel_search:
        active_tab_text = f"Search: {escape(channel_search)}"

    prev_page = page - 1
    next_page = page + 1
    if tab == "search":
        prev_link = (
            url_for("channel_feed")
            + "?url="
            + quote(channel_url)
            + "&tab=search&channel_search="
            + quote(channel_search)
            + "&page="
            + str(prev_page)
        )
        next_link = (
            url_for("channel_feed")
            + "?url="
            + quote(channel_url)
            + "&tab=search&channel_search="
            + quote(channel_search)
            + "&page="
            + str(next_page)
        )
    else:
        base_route = tab_routes.get(tab, url_for("channel_videos_page"))
        prev_link = base_route + "?url=" + quote(channel_url) + "&page=" + str(prev_page)
        next_link = base_route + "?url=" + quote(channel_url) + "&page=" + str(next_page)

    block = [
        "<div class='notice'>",
        f"Latest videos from <b>{escape(channel_name)}</b>",
        "</div>",
        "<div class='notice'>",
        "Browse: " + " | ".join(tab_links),
        "<br>Now viewing: <b>" + active_tab_text + "</b>",
        "<br>Page: <b>" + str(page) + "</b>",
        "</div>",
        search_tab_form,
        f"<p><a href='{url_for('following')}'>Back to followed channels</a></p>",
    ]

    if not videos:
        block.append("<p>No videos found in this channel feed.</p>")
    else:
        for item in videos:
            block.append("<div class='result'>")
            block.append(
                f"<h3><a href='{url_for('watch')}?v={quote(item['id'])}'>{escape(item['title'])}</a></h3>"
            )
            block.append(
                "<div class='meta'>"
                + f"Uploader: {escape(item['uploader'])} | Duration: {format_duration(item['duration'])}"
                + "</div>"
            )
            block.append("</div>")

    pager = ["<div class='notice'>"]
    if page > 1:
        pager.append(f"<a href='{prev_link}'>Previous Page</a>")
    else:
        pager.append("Previous Page")
    pager.append(" | ")
    if has_more:
        pager.append(f"<a href='{next_link}'>Next Page</a>")
    else:
        pager.append("Next Page")
    pager.append("</div>")
    block.append("".join(pager))

    body = "\n".join(block)
    return render_template_string(BASE_TEMPLATE, title=f"Channel: {channel_name}", body=body)


@app.route("/channel-videos")
def channel_videos_page() -> str:
    url = (request.args.get("url") or "").strip()
    page = max(1, int(request.args.get("page", "1") or "1"))
    query = "?url=" + quote(url) + "&tab=videos&page=" + str(page)
    return redirect(url_for("channel_feed") + query)


@app.route("/channel-streams")
def channel_streams_page() -> str:
    url = (request.args.get("url") or "").strip()
    page = max(1, int(request.args.get("page", "1") or "1"))
    query = "?url=" + quote(url) + "&tab=streams&page=" + str(page)
    return redirect(url_for("channel_feed") + query)


@app.route("/channel-shorts")
def channel_shorts_page() -> str:
    url = (request.args.get("url") or "").strip()
    page = max(1, int(request.args.get("page", "1") or "1"))
    query = "?url=" + quote(url) + "&tab=shorts&page=" + str(page)
    return redirect(url_for("channel_feed") + query)


@app.route("/channel-playlists")
def channel_playlists_page() -> str:
    url = (request.args.get("url") or "").strip()
    page = max(1, int(request.args.get("page", "1") or "1"))
    query = "?url=" + quote(url) + "&tab=playlists&page=" + str(page)
    return redirect(url_for("channel_feed") + query)


@app.route("/go-channel")
def go_channel() -> str:
    video_id = (request.args.get("video") or "").strip()
    fallback_query = (request.args.get("q") or "").strip()
    if not video_id:
        if fallback_query:
            return redirect(url_for("browse_channels") + "?q=" + quote(fallback_query))
        return redirect(url_for("home"))

    try:
        info = extract_video_info(f"https://www.youtube.com/watch?v={video_id}")
    except Exception:
        if fallback_query:
            return redirect(url_for("browse_channels") + "?q=" + quote(fallback_query))
        return redirect(url_for("browse_channels") + "?q=" + quote(video_id))

    channel_url = (info.get("channel_url") or info.get("uploader_url") or "").strip()
    channel_name = (info.get("channel") or info.get("uploader") or "").strip()

    if channel_url:
        return redirect(url_for("channel_feed") + "?url=" + quote(channel_url) + "&tab=videos")

    if channel_name:
        return redirect(url_for("browse_channels") + "?q=" + quote(channel_name))

    if fallback_query:
        return redirect(url_for("browse_channels") + "?q=" + quote(fallback_query))

    return redirect(url_for("browse_channels") + "?q=" + quote(video_id))


@app.route("/browse-channels")
def browse_channels() -> str:
    query = (request.args.get("q") or "").strip()

    if not query:
        body = (
            render_template_string(HOME_BODY, query="", video_query="")
            + "<div class='notice'>Enter a channel name or topic in Find Channels.</div>"
        )
        return render_template_string(BASE_TEMPLATE, title="Browse Channels", body=body)

    try:
        channels = discover_channels(query, SEARCH_LIMIT)
    except Exception as exc:  # pragma: no cover
        body = (
            "<div class='notice'><b>Channel discovery failed.</b><br>"
            + f"Error: {escape(str(exc))}"
            + "</div>"
            + render_template_string(HOME_BODY, query="", video_query="")
        )
        return render_template_string(BASE_TEMPLATE, title="Browse Channels", body=body)

    rows = [
        "<div class='notice'>",
        f"Discovered channels for <b>{escape(query)}</b>",
        "</div>",
        render_template_string(HOME_BODY, query="", video_query=""),
    ]

    if not channels:
        rows.append("<p>No channels discovered from current search results.</p>")
    else:
        rows.append("<ul class='formats'>")
        for channel in channels:
            url = channel["url"]
            name = channel["name"]
            feed_link = url_for("channel_feed") + "?url=" + quote(url)
            rows.append(f"<li><a href='{feed_link}'>{escape(name)}</a></li>")
        rows.append("</ul>")

    body = "\n".join(rows)
    return render_template_string(BASE_TEMPLATE, title=f"Browse Channels: {query}", body=body)


@app.route("/follow", methods=["POST"])
def follow_channel():
    channel_url = (request.form.get("channel_url") or "").strip()
    channel_name = (request.form.get("channel_name") or channel_url).strip()
    next_url = (request.form.get("next") or url_for("following")).strip()

    if channel_url:
        add_followed_channel(channel_url, channel_name)

    return redirect(next_url)


@app.route("/unfollow", methods=["POST"])
def unfollow_channel():
    channel_url = (request.form.get("channel_url") or "").strip()
    next_url = (request.form.get("next") or url_for("following")).strip()

    if channel_url:
        remove_followed_channel(channel_url)

    return redirect(next_url)


if __name__ == "__main__":
    app.run(host=BIND_HOST, port=BIND_PORT, debug=False)
