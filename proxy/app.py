"""fnmusic-ext 拦截代理 (FastAPI + httpx).

功能：
1. 通用透传：所有非拦截路径原样转发到 trim-music unix socket
2. 搜索合并：GET /music/api/v1/search/track* （兼容 q/keyword，并行 musicdl）
3. 在线播放：stream + HLS 兜底 + transcode 空操作 + tee 缓存回放（音频与歌词 sidecar）
4. 在线元数据/歌词/封面
5. GET /_ext/healthz
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Coroutine
from urllib.parse import quote
import urllib.parse
from uuid import uuid4

import httpx
import anyio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, FileResponse

try:
    from . import recommend as dailyrec
    from . import features as feats
    from . import download_mgr
    from . import download_routes
    from . import ai_music
    from . import netease_api
    from . import native_engine
    from . import fn_backup
    from . import fn_webdav
    from .version import get_version
except ImportError:  # uvicorn --app-dir proxy
    import recommend as dailyrec  # type: ignore
    import features as feats  # type: ignore
    import download_mgr  # type: ignore
    import download_routes  # type: ignore
    import ai_music  # type: ignore
    import netease_api  # type: ignore
    import native_engine  # type: ignore
    import fn_backup  # type: ignore
    import fn_webdav  # type: ignore
    from version import get_version  # type: ignore

logger = logging.getLogger("fnmusic_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_CURRENT_PLAYING_RECORD: dict[str, Any] = {
    "guid": "",
    "title": "",
    "artist": "",
    "cover_url": "",
    "source_playlist": "",
    "time": 0
}

_HOME = dailyrec.home_dir()

CONF = {
    "musicdl_url": os.environ.get("FNMUSIC_MUSICDL_URL", "http://127.0.0.1:8768"),
    "musicbox_url": os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"),
    "lx_url": os.environ.get("FNMUSIC_LX_URL", "http://127.0.0.1:8772"),
    "musicdl_enabled": os.environ.get("FNMUSIC_MUSICDL_ENABLED", "false").lower() in ("true", "1", "yes"),
    "netease_enabled": os.environ.get("FNMUSIC_NETEASE_ENABLED", "false").lower() in ("true", "1", "yes"),
    "lx_enabled": os.environ.get("FNMUSIC_LX_ENABLED", "true").lower() in ("true", "1", "yes"),
    "lx_search_limit": int(os.environ.get("FNMUSIC_LX_SEARCH_LIMIT", "20")),
    "lx_quality": os.environ.get("FNMUSIC_LX_QUALITY", "lossless"),
    "netease_wait_s": float(os.environ.get("FNMUSIC_NETEASE_WAIT_S", "3.0")),
    "netease_quality": os.environ.get("FNMUSIC_NETEASE_QUALITY", "lossless"),
    "netease_search_limit": int(os.environ.get("FNMUSIC_NETEASE_SEARCH_LIMIT", "50")),
    "upstream_sock": os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music_upstream.socket"),
    "online_limit": int(os.environ.get("FNMUSIC_ONLINE_LIMIT", "30")),
    "search_list_path": os.environ.get("FNMUSIC_SEARCH_LIST_PATH", "data.list"),
    "cache_dir": os.environ.get("FNMUSIC_CACHE_DIR", os.path.join(_HOME, "cache")),
    # 空=从飞牛 shared_library.path 自动探测；测试可覆盖到临时目录
    "library_dir": os.environ.get("FNMUSIC_LIBRARY_DIR", ""),
    "music_db": os.environ.get(
        "FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db"
    ),
    "merge_suggest": os.environ.get("FNMUSIC_MERGE_SUGGEST", "false").lower() in ("true", "1", "yes"),
    "online_sources": os.environ.get("FNMUSIC_ONLINE_SOURCES", "KuwoMusicClient,MiguMusicClient"),
    "lyric_field": os.environ.get("FNMUSIC_LYRIC_FIELD", "data.lyric"),
    "search_timeout": float(os.environ.get("FNMUSIC_SEARCH_TIMEOUT", "15")),
    "search_cache_ttl": float(os.environ.get("FNMUSIC_SEARCH_CACHE_TTL", "604800")),
    "late_page_wait_s": float(os.environ.get("FNMUSIC_LATE_PAGE_WAIT_S", "5.0")),
    "fav_dir": os.environ.get(
        "FNMUSIC_FAV_DIR", os.path.join(_HOME, "online_favorites")
    ),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),
    "llm_model": (os.environ.get("FNMUSIC_LLM_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini",
}

_REDACT_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CACHE_EXTS = ("mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "ape", "wv", "dsf", "dff", "tta")

# 飞牛 Kl() 归一化：mpeg/mp3→mp3，wav/pcm→wav，m4a/aac/mp4→m4a，其余小写原样（flac/ogg/ape/wv…）
_FORMAT_ALIASES = {
    "mp3": "mp3",
    "mpeg": "mp3",
    "mpga": "mp3",
    "flac": "flac",
    "wav": "wav",
    "wave": "wav",
    "pcm": "wav",
    "lpcm": "wav",
    "ogg": "ogg",
    "vorbis": "ogg",
    "opus": "opus",
    "m4a": "m4a",
    "mp4": "m4a",
    "mp4a": "m4a",
    "aac": "m4a",
    "alac": "m4a",
    "ape": "ape",
    "wv": "wv",
    "wavpack": "wv",
    "dsf": "dsf",
    "dff": "dff",
    "dsd": "dsd",
    "tta": "tta",
    "tak": "tak",
    "wma": "wma",
    "aiff": "aiff",
    "aif": "aiff",
}


# 模块级搜索缓存
_SEARCH_CACHE: dict[str, dict] = {}


def _search_ttl(entry: dict) -> float:
    # Backend IDs/URLs are memory scoped; positive results revalidate in 5m.
    if entry.get("partial"):
        return 30.0
    if not entry.get("items"):
        return 10.0
    return min(float(CONF.get("search_cache_ttl", 604800)), 300.0)


def _clean_search_cache() -> None:
    now = time.time()
    expired = [k for k, v in _SEARCH_CACHE.items() if now - v.get("accessed", v.get("ts", 0)) >= 900]
    if len(_SEARCH_CACHE) > 2000:
        expired += sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k].get("accessed", 0))[:1000]
    for key in expired:
        entry = _SEARCH_CACHE.pop(key, {})
        task = entry.get("task")
        if task and not task.done():
            task.cancel()


def _set_search_cache(keyword: str, entry: dict) -> None:
    _clean_search_cache()
    _SEARCH_CACHE[keyword] = entry


def _source_config() -> dict:
    return {k: v for k, v in CONF.items() if k.endswith(("_enabled", "_url", "_limit", "_quality")) or k == "online_sources"}


def _search_scope(request: Request) -> str:
    auth = [request.headers.get(k, "") for k in ("cookie", "authorization", "x-trim-music-temp-token")]
    config = _source_config()
    filters = sorted((k, v) for k, v in request.query_params.multi_items() if k not in ("page", "q", "query", "keyword"))
    return hashlib.sha256(json.dumps([auth, config, filters, request.url.path], sort_keys=True).encode()).hexdigest()


def _source_enabled(guid: str) -> bool:
    source = source_from_online_guid(guid)
    if not CONF.get({"netease": "netease_enabled", "lx": "lx_enabled"}.get(source, "musicdl_enabled"), True):
        return False
    if source not in ("netease", "lx") and CONF.get("online_sources"):
        selected = {name.strip().lower().removesuffix("musicclient") for name in str(CONF["online_sources"]).split(",")}
        return source.lower() in selected
    return True


ONLINE_TRIAL_MARKERS = (
    "(试听)",
    "（试听）",
    "试听片段",
    "片段试听",
    "试听版",
    "[试听]",
    "【试听】",
    "- 试听",
    " - 试听",
)


def is_playable_online_track(item: dict, require_id: bool = False) -> bool:
    """最终防线校验：过滤无音频流或试听标记的不可播曲目。"""
    if not isinstance(item, dict):
        return False

    title = str(item.get("title") or item.get("name") or item.get("song_name") or "").strip()
    if not title:
        return False

    if require_id:
        sid = str(item.get("id") or item.get("song_id") or item.get("guid") or "").strip()
        if not sid:
            return False

    # 1. 标题含试听标记
    if any(marker in title for marker in ONLINE_TRIAL_MARKERS):
        return False

    # 2. 字段试听标记
    if item.get("is_trial") is True or item.get("freeTrialInfo") or item.get("freeTrialPrivilege"):
        return False
    if int(item.get("is_free_part") or 0) != 0 or int(item.get("fail_process") or 0) == 4:
        return False

    # 3. 收费/VIP 拦截（verified 条目已由服务端完成"直链解析+Range探活"验证，可播性有实证，跳过收费元数据拦截）
    if item.get("verified") is not True:
        if int(item.get("pay_type") or 0) != 0:
            return False
        if int(item.get("pkg_price") or 0) != 0 or int(item.get("price") or 0) != 0:
            return False
        fee = item.get("fee")
        if fee is not None:
            try:
                if int(fee) not in (0, 8):
                    return False
            except (ValueError, TypeError):
                pass

    # 4. 显式不可播/无流标记
    if item.get("unplayable") is True or item.get("playable") is False:
        return False
    if item.get("has_stream") is False:
        return False

    # 5. 音频流直链校验：若带有 download_url 或 url 键，则必须合法可用，绝不能是空串或 404
    if "download_url" in item:
        d_url = str(item.get("download_url") or "").strip()
        if not d_url or not d_url.startswith(("http://", "https://")) or "404/error.html" in d_url or "error.html" in d_url:
            return False
    if "url" in item:
        u = str(item.get("url") or "").strip()
        if not u or "404/error.html" in u or "error.html" in u:
            return False

    # 6. 片段时长校验（<=35s 且带有试听迹象）
    duration = item.get("duration_s") or (item.get("duration") or 0)
    try:
        duration_s = float(duration)
        if 0 < duration_s <= 35 and ("试听" in title or item.get("is_trial")):
            return False
    except (ValueError, TypeError):
        pass

    return True


def _same_recording(left: dict, right: dict) -> bool:
    """Conservative identity: never strip live/remix/version markers."""
    for key in ("title", "artist", "version"):
        a, b = (str(x.get(key) or "").strip().casefold() for x in (left, right))
        if a != b or (key != "version" and not a):
            return False
    try:
        a, b = float(left.get("duration_s") or 0), float(right.get("duration_s") or 0)
        return math.isfinite(a) and math.isfinite(b) and a > 0 and b > 0 and abs(a - b) <= 2.0
    except (TypeError, ValueError):
        return False


def deduplicate_online_items(items: list[dict], keyword: str = "") -> list[dict]:
    """Keep the published representative and strict recording alternatives, prioritizing relevance, lossless and rich metadata."""
    result: list[dict] = []
    seen = set()

    kw_clean = (keyword or "").strip().lower()

    # 排序：关键词匹配度（完全包含 > 部分包含） > 无损格式(flac/wav/ape/10MB+) > 有封面 > 文件大小
    def _rank_item(it: dict) -> tuple:
        title = str(it.get("title") or it.get("name") or "").strip().lower()
        artist = str(it.get("artist") or "").strip().lower()
        match_score = 0
        if kw_clean:
            if kw_clean == title:
                match_score = 4
            elif kw_clean in title:
                match_score = 3
            elif kw_clean in artist:
                match_score = 2
            elif any(part in title for part in kw_clean.split() if part):
                match_score = 1
            else:
                match_score = -1  # 与关键词无关的条目降级
        ext = str(it.get("ext") or "").lower()
        size = int(it.get("file_size") or 0)
        is_lossless = 1 if ext in ("flac", "wav", "ape") or size >= 10 * 1024 * 1024 else 0
        has_cover = 1 if bool(it.get("cover_url")) else 0
        return (match_score, is_lossless, has_cover, size)

    sorted_items = sorted(items, key=_rank_item, reverse=True)

    for item in sorted_items:
        if not is_playable_online_track(item):
            continue
        guid = online_guid_from_item(item)
        if guid in seen:
            continue
        seen.add(guid)
        representative = next((x for x in result if _same_recording(x, item)), None)
        if representative is None:
            representative = dict(item)
            representative["_alternatives"] = list(item.get("_alternatives", []))
            result.append(representative)
        else:
            alternatives = representative.setdefault("_alternatives", [])
            if guid not in {online_guid_from_item(x) for x in alternatives}:
                alternatives.append({k: v for k, v in item.items() if k != "_alternatives"})
    return result


def play_format_from_ext(ext: str | None) -> str:
    raw = (ext or "mp3").strip().lower().lstrip(".")
    if raw.startswith("audio/"):
        raw = raw.split("/", 1)[-1]
    return _FORMAT_ALIASES.get(raw, raw or "mp3")


def filter_headers(headers: Any, exclude_keys: set | None = None) -> dict:
    exclude = HOP_BY_HOP | {k.lower() for k in (exclude_keys or set())}
    return {k: v for k, v in headers.items() if k.lower() not in exclude}


def copy_incoming_headers(request: Request) -> dict:
    """透传鉴权 Cookie / Token。Starlette 头名为小写，需显式回填以免丢失 music-token。"""
    headers = filter_headers(request.headers, exclude_keys={"host", "content-length"})
    headers["accept-encoding"] = "identity"
    for key in ("cookie", "authorization", "x-trim-music-temp-token"):
        val = request.headers.get(key)
        if val:
            headers[key] = val
    return headers


def get_by_path(d: Any, path: str) -> Any:
    curr = d
    for p in path.split("."):
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def set_by_path(d: dict, path: str, val: Any):
    parts = path.split(".")
    curr = d
    for p in parts[:-1]:
        if p not in curr or not isinstance(curr[p], dict):
            curr[p] = {}
        curr = curr[p]
    curr[parts[-1]] = val


def extract_keyword(request: Request) -> str:
    """前端打包用 q，部分调用/验收用 keyword。"""
    params = request.query_params
    return (params.get("keyword") or params.get("q") or params.get("query") or "").strip()


def online_guid_from_item(item: dict) -> str:
    raw_id = str(item.get("guid") or item.get("id") or "")
    src = str(item.get("source") or "")
    if raw_id.startswith("online:"):
        return raw_id
    if ":" in raw_id:
        return f"online:{raw_id}"
    return f"online:{src}:{raw_id}"


def song_id_from_online_guid(guid: str) -> str:
    if guid.startswith("online:"):
        return guid[len("online:") :]
    return guid


def is_online_guid(guid: str) -> bool:
    return bool(guid) and guid.startswith("online:")


def source_from_online_guid(guid: str) -> str:
    parts = (guid or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


def build_online_track(item: dict) -> dict:
    """对齐飞牛前端 ZQ 解构 / _h() 期望：artists、album 对象、genres 数组、audioSpec、duration 毫秒。"""
    guid = online_guid_from_item(item)
    src = str(item.get("source") or source_from_online_guid(guid) or "")
    title = str(item.get("title") or item.get("name") or "")
    artist = str(item.get("artist") or "")
    album = str(item.get("album") or "")
    duration_s = item.get("duration_s") or 0
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0
    duration_ms = int(duration_s * 1000)
    ext = str(item.get("ext") or "flac") or "flac"
    play_format = play_format_from_ext(ext)
    file_size = item.get("file_size") or 0
    try:
        file_size = int(file_size or 0)
    except (TypeError, ValueError):
        file_size = 0
    # 若无损格式但未提前探活出具体大小时，提供真实的 10MB+ 无损预估大小 (例如 30MB)
    if play_format in ("flac", "wav", "ape") and file_size < 10 * 1024 * 1024:
        file_size = max(file_size, int(duration_s * 120000) if duration_s > 0 else 31457280)
    cover = str(item.get("cover_url") or "")
    # 路径带真实后缀，飞牛 ll() 用 path 解析 extension；封面走 guid 以便 /static/cover 拦截
    spec_path = f"online/{src}/{guid}.{play_format}"

    artists_list = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists_list,
        "coverId": guid,
    }
    audio_spec = {
        "path": spec_path,
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": file_size,
        "channel": 2,
        "sampleRate": 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": 1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000,
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}

    return {
        "guid": guid,
        "id": guid,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists_list,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": file_size,
        "file_size": file_size,
        "coverId": guid,
        "cover_url": cover,
        "coverUrl": cover,
        "coverURL": cover,
        "source": src,
        "is_online": True,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": bool(item.get("lyric")),
        "genres": [],
        "accessStatus": 0,
        "createdAt": 1700000000,
        "updatedAt": 1700000000,
    }


def artist_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    a = item.get("artist") or item.get("singer") or item.get("singers") or ""
    if isinstance(a, list):
        names = []
        for x in a:
            if isinstance(x, dict):
                names.append(str(x.get("name") or ""))
            else:
                names.append(str(x))
        return " ".join(n for n in names if n).strip().lower()
    if isinstance(a, dict):
        return str(a.get("name") or "").strip().lower()
    return str(a).strip().lower()


def title_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("title") or item.get("name") or "").strip().lower()


def should_cache(range_header: str | None) -> bool:
    """完整拉取才落盘：无 Range，或 bytes=0-（开区间）。Safari bytes=0-1 探测不落盘。"""
    if not range_header:
        return True
    r = range_header.strip().lower()
    return bool(re.match(r"^bytes=0-$", r))


def is_range_from_zero_or_none(range_header: str | None) -> bool:
    return should_cache(range_header)


def cache_safe_guid(guid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)


def online_file_id(guid: str) -> str:
    """online:migu:600929… → 600929…，仅用于查找旧文件，不再写进文件名。"""
    return song_id_from_online_guid(guid).rsplit(":", 1)[-1]


def safe_basename_title(title: str) -> str:
    t = re.sub(r'[/\\:\0]', "_", (title or "").strip()) or "unknown"
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:120]


def library_basename(title: str, artist: str = "") -> str:
    """曲库文件名：歌手 - 歌名（无源站 id）。飞牛无标签时会用文件名当标题。"""
    title_s = safe_basename_title(title)
    artist_s = safe_basename_title(artist) if (artist or "").strip() else ""
    if artist_s and artist_s.lower() != title_s.lower() and artist_s != "unknown":
        return f"{artist_s} - {title_s}"
    return title_s


def media_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.ref")


def _path_stem(path: str) -> str:
    root, ext = os.path.splitext(path)
    known = set(CACHE_EXTS) | {"lrc", "part"}
    if ext.lstrip(".").lower() in known:
        return root
    return path


def remember_media_path(guid: str, media_path: str) -> None:
    """记住曲库里的文件词干（不含扩展名），音频和 .lrc 共用。"""
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(media_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(_path_stem(media_path))
    except Exception as e:
        logger.warning("Failed to remember media path for %s: %s", guid, e)


def recalled_media_stem(guid: str) -> str | None:
    ref = media_ref_path(guid)
    if not os.path.exists(ref):
        return None
    try:
        with open(ref, encoding="utf-8") as f:
            stem = _path_stem(f.read().strip())
        if stem:
            return stem
    except Exception:
        return None
    return None


def recalled_media_path(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if not stem:
        return None
    for ext in CACHE_EXTS:
        path = f"{stem}.{ext}"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def unique_library_path(directory: str, basename: str, ext: str) -> str:
    dest = os.path.join(directory, f"{basename}.{ext}")
    if not os.path.exists(dest):
        return dest
    n = 2
    while os.path.exists(os.path.join(directory, f"{basename} ({n}).{ext}")):
        n += 1
    return os.path.join(directory, f"{basename} ({n}).{ext}")


def write_audio_tags(path: str, title: str, artist: str = "", album: str = "") -> None:
    """写入 title/artist/album，飞牛扫描后用标签而不是文件名显示。"""
    title, artist, album = (title or "").strip(), (artist or "").strip(), (album or "").strip()
    if not title and not artist:
        return
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        logger.warning("Failed to write audio tags for %s: %s", path, e)


def detect_library_dir() -> str:
    """优先环境变量，否则使用本地可靠缓存目录，避免云盘挂载点报错。"""
    explicit = str(CONF.get("library_dir") or "").strip()
    if explicit:
        return explicit
    local_cache_lyric = os.path.join(CONF.get("cache_dir", os.path.join(_HOME, "cache")), "lyrics")
    os.makedirs(local_cache_lyric, exist_ok=True)
    return local_cache_lyric


def iter_media_dirs() -> list[str]:
    dirs: list[str] = []
    lib = detect_library_dir()
    for d in (lib, CONF["cache_dir"]):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def adopt_library_perms(path: str) -> None:
    try:
        parent = os.path.dirname(path) or "."
        st = os.stat(parent)
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)
    except Exception:
        pass


def find_cache_file(guid: str) -> str | None:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        for ext in CACHE_EXTS:
            exact = os.path.join(d, f"{safe}.{ext}")
            if os.path.exists(exact) and os.path.getsize(exact) > 0:
                return exact
    return None


def promote_cache_hit(guid: str, audio_path: str) -> str:
    """旧 cache/ 音频：若曲库已有对应文件或歌词，则对齐过去。"""
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    lib = detect_library_dir()
    try:
        if os.path.abspath(os.path.dirname(audio_path)) == os.path.abspath(lib):
            remember_media_path(guid, audio_path)
            return audio_path
    except Exception:
        return audio_path
    # Bare legacy IDs cannot prove source/track identity.
    return audio_path


def library_media_path(guid: str, title: str, ext: str, artist: str = "") -> str:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    stem = recalled_media_stem(guid)
    if stem:
        return f"{stem}.{ext}"
    lib = detect_library_dir()
    os.makedirs(lib, exist_ok=True)
    return unique_library_path(lib, library_basename(title, artist), ext)


def find_lyric_file(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    audio = find_cache_file(guid)
    if audio:
        sibling = os.path.splitext(audio)[0] + ".lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        exact = os.path.join(d, f"{safe}.lrc")
        if os.path.exists(exact) and os.path.getsize(exact) > 0:
            return exact
    return None


def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    found = find_lyric_file(guid)
    if found:
        return found
    audio = find_cache_file(guid)
    if audio:
        return os.path.splitext(audio)[0] + ".lrc"
    d = detect_library_dir()
    os.makedirs(d, exist_ok=True)
    if (title or "").strip() or (artist or "").strip():
        return os.path.join(d, f"{library_basename(title, artist)}.lrc")
    return os.path.join(d, f"{cache_safe_guid(guid)}.lrc")


def read_lyric_cache(guid: str) -> str:
    path = find_lyric_file(guid)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning("Failed to read lyric cache %s: %s", path, e)
        return ""


def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    if text == read_lyric_cache(guid):
        return
    path = lyric_cache_path(guid, title=title, artist=artist)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        with open(part_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_media_path(guid, path)
    except Exception as e:
        logger.warning("Failed to write lyric cache %s: %s", path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass


async def cache_lyrics_from_musicdl(musicdl_client: httpx.AsyncClient, guid: str) -> dict | None:
    """与音频 tee 并行：把 musicdl /info 里的 LRC 落到曲库同目录 sidecar。"""
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if not (isinstance(data, dict) and data.get("ok") is not False):
            return None
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
        return data
    except Exception as e:
        logger.warning("lyric sidecar fetch failed for %s: %s", guid, e)
        return None


async def resolve_online_lyric(request: Request, guid: str) -> str:
    """本地 .lrc 优先；没有再向源站要，拿到就落盘。"""
    cached = read_lyric_cache(guid)
    if cached:
        return cached

    if not _source_enabled(guid):
        return ""
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    l_data = res_data.get("data")
                    if isinstance(l_data, dict):
                        lyric_text = str(l_data.get("lyric") or "").strip()
                        if lyric_text:
                            info = await _online_info(request, guid)
                            write_lyric_cache(
                                guid,
                                lyric_text,
                                title=str((info or {}).get("title") or ""),
                                artist=str((info or {}).get("artist") or ""),
                            )
                            return lyric_text
        except Exception as e:
            logger.warning("musicbox lyric fetch failed for %s: %s", guid, e)
        return ""

    data = await _online_info(request, guid)
    text = str((data or {}).get("lyric") or "").strip()
    title = str((data or {}).get("title") or "")
    artist = str((data or {}).get("artist") or "")
    if not title:
        title, artist = _find_track_title_artist_from_cache(guid)

    # 1. 尝试从本地已启用的落雪自定义源拉取同步歌词
    if not text:
        try:
            sid = song_id_from_online_guid(guid)
            parts = sid.split(":")
            sub_src = parts[1] if len(parts) >= 2 else "kw"
            sub_id = parts[-1]
            song_info = {
                "id": sub_id,
                "name": title,
                "singer": artist,
                "source": sub_src,
            }
            lx_lrc = await native_engine.resolve_music_lyric_native(song_info)
            if lx_lrc and lx_lrc.get("lyric"):
                text = lx_lrc["lyric"]
        except Exception as e_lx:
            logger.debug("lx lyric resolve error for %s: %s", guid, e_lx)

    # 2. 自动使用网易云千万级曲库跨源智能匹配补全
    if not text and netease_api.load_netease_config().get("auto_enrich", True):
        try:
            if title:
                en = await netease_api.netease_client.enrich_cover_and_lyric(title, artist)
                if en.get("ok") and en.get("lyric"):
                    text = en["lyric"]
        except Exception as e:
            logger.debug("netease lyric enrich failed for %s: %s", guid, e)

    if text:
        write_lyric_cache(
            guid,
            text,
            title=title,
            artist=artist,
        )
    return text


def media_type_for_ext(ext: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ape": "audio/x-ape",
        "wv": "audio/x-wavpack",
        "dsf": "audio/x-dsd",
        "dff": "audio/x-dff",
        "tta": "audio/x-tta",
        "wma": "audio/x-ms-wma",
        "aiff": "audio/aiff",
    }.get(ext.lower(), "application/octet-stream")


def ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "flac" in ct:
        return "flac"
    if "wavpack" in ct or "x-wv" in ct:
        return "wv"
    if "wav" in ct or "wave" in ct:
        return "wav"
    if "opus" in ct:
        return "opus"
    if "ogg" in ct:
        return "ogg"
    if "ape" in ct:
        return "ape"
    if "aiff" in ct:
        return "aiff"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "aac" in ct:
        return "aac"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return play_format_from_ext(ct.split("/")[-1] if "/" in ct else "mp3")


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.I)
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        suffix = int(end_s)
        start = max(file_size - suffix, 0)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start < 0 or start >= file_size or start > end:
        return None
    return start, end


def serve_file_with_range(path: str, range_header: str | None, media_type: str) -> Response:
    file_size = os.path.getsize(path)
    rng = parse_http_range(range_header, file_size)

    def iter_file(offset: int, length: int) -> AsyncGenerator[bytes, None]:
        async def gen() -> AsyncGenerator[bytes, None]:
            remaining = length
            with open(path, "rb") as fp:
                fp.seek(offset)
                while remaining > 0:
                    chunk = fp.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return gen()

    if rng is None:
        return StreamingResponse(
            iter_file(0, file_size),
            status_code=200,
            headers={
                "Content-Type": media_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        iter_file(start, length),
        status_code=206,
        headers={
            "Content-Type": media_type,
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
        },
    )


def get_upstream_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "upstream_client", None)
    if client is None:
        transport = httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"], limits=httpx.Limits(max_keepalive_connections=50, max_connections=100))
        client = httpx.AsyncClient(transport=transport, base_url="http://unix", timeout=30.0)
        fastapi_app.state.upstream_client = client
    return client


def get_musicdl_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicdl_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicdl_url"], timeout=45.0)
        fastapi_app.state.musicdl_client = client
    return client


def get_musicbox_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicbox_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicbox_url"], timeout=20.0)
        fastapi_app.state.musicbox_client = client
    return client


def get_lx_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "lx_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["lx_url"], timeout=25.0)
        fastapi_app.state.lx_client = client
    return client


def get_llm_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "llm_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        fastapi_app.state.llm_client = client
    return client


async def forward_to_upstream(request: Request, client: httpx.AsyncClient) -> Response:
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    headers = copy_incoming_headers(request)
    body = await request.body()

    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req, stream=True)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})

    async def body_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


async def fetch_upstream_envelope(request: Request, client: httpx.AsyncClient) -> Response | dict:
    """透传上游并解析 JSON 信封。失败时返回 Response，成功返回 dict。"""
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)
    body = await request.body()
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    if not isinstance(payload, dict):
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    payload["_ext_headers"] = resp_headers
    return payload


async def fetch_musicdl_search(client: httpx.AsyncClient, keyword: str, limit: int, sources: str | None = None) -> dict | None:
    if not keyword:
        return None
    params: dict[str, Any] = {"keyword": keyword, "limit": limit}
    selected_sources = CONF["online_sources"] if sources is None else sources
    if selected_sources:
        params["sources"] = selected_sources
    timeout = max(float(CONF.get("search_timeout") or 25), 8.0)
    try:
        r = await client.get("/search", params=params, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                if data.get("errors"):
                    logger.warning("musicdl search partial errors: %s", data.get("errors"))
                raw_items = data.get("items")
                if isinstance(raw_items, list):
                    data["items"] = [it for it in raw_items if is_playable_online_track(it)]
                return data
    except Exception as e:
        logger.warning("Failed to fetch online search from musicdl: %s", e)
    return None


async def fetch_musicbox_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict] | None:
    if not keyword:
        return None
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit, "type": "song"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("data")
        if not isinstance(raw_list, list):
            return None
        items = []
        song_ids = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it):
                continue
            sid = str(it.get("song_id") or it.get("id") or "")
            if not sid:
                continue
            title = str(it.get("song_name") or it.get("title") or it.get("name") or "")
            artist = str(it.get("artist") or "")
            album = str(it.get("album_name") or it.get("album") or "")
            duration = it.get("duration") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            quality = str(it.get("quality") or "").upper()
            ext = "flac" if any(q in quality for q in ("SQ", "HR", "无损")) else "mp3"
            items.append({
                "id": f"netease:{sid}",
                "source": "netease",
                "title": title,
                "version": str(it.get("version") or ""),
                "artist": artist,
                "album": album,
                "duration_s": duration_s,
                "ext": ext,
                "cover_url": "",
                "lyric": "",
            })
            song_ids.append(sid)

        if song_ids:
            try:
                detail_resp = await client.get(
                    "/api/v1/songs/detail",
                    params={"ids": ",".join(song_ids)},
                    timeout=15.0,
                )
                if detail_resp.status_code == 200:
                    detail_json = detail_resp.json()
                    if isinstance(detail_json, dict) and detail_json.get("ok") is not False:
                        detail_list = detail_json.get("data")
                        if isinstance(detail_list, list):
                            detail_map = {}
                            for d_item in detail_list:
                                if isinstance(d_item, dict):
                                    d_sid = str(d_item.get("song_id") or d_item.get("id") or "")
                                    if d_sid:
                                        detail_map[d_sid] = d_item
                            for item in items:
                                raw_sid = item["id"].split(":", 1)[-1]
                                d_info = detail_map.get(raw_sid)
                                if d_info:
                                    pic_url = str(d_info.get("album_pic_url") or "")
                                    if pic_url:
                                        item["cover_url"] = pic_url
                                    if d_info.get("has_sq") or d_info.get("has_hr"):
                                        item["ext"] = "flac"
            except Exception as detail_err:
                logger.warning("Failed to fetch songs detail for %s: %s", keyword, detail_err)

        return [it for it in items if is_playable_online_track(it)]
    except Exception as e:
        logger.warning("Failed to fetch musicbox search: %s", e)
        return None


class _SearchItems(list):
    """List-compatible normalized results with source degradation metadata."""
    def __init__(self, items, partial=False):
        super().__init__(items)
        self.partial = partial


async def fetch_lx_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    """洛雪音乐源搜索：返回统一 item（id = "lx:<source>:<identifier>"）。"""
    if not keyword:
        return None  # type: ignore[return-value]
    timeout = max(float(CONF.get("search_timeout") or 25), 8.0)
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("items")
        if not isinstance(raw_list, list):
            return None
        items = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it):
                continue
            tid = str(it.get("id") or "")
            if not tid:
                continue
            duration = it.get("duration_s") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            try:
                file_size = int(it.get("file_size") or 0)
            except (TypeError, ValueError):
                file_size = 0
            items.append({
                "id": tid,
                "source": "lx",
                "lx_source": str(it.get("lx_source") or ""),
                "version": str(it.get("version") or ""),
                "title": str(it.get("title") or it.get("name") or ""),
                "artist": str(it.get("artist") or ""),
                "album": str(it.get("album") or ""),
                "duration_s": duration_s,
                "ext": str(it.get("ext") or "mp3") or "mp3",
                "cover_url": str(it.get("cover_url") or ""),
                "file_size": file_size,
                "lyric": "",
                "verified": it.get("verified") is True,
            })
        return _SearchItems([it for it in items if is_playable_online_track(it)], partial=bool(data.get("errors")))
    except Exception as e:
        logger.warning("Failed to fetch online search from lxmusic: %s", e)
        return None


_RESOLVE_CACHE: dict[str, tuple[float, dict]] = {}

async def resolve_lx_url(client: httpx.AsyncClient, song_id: str, track_info: dict | None = None) -> "dict | None":
    """洛雪音乐源直链解析：song_id 形如 "lx:kg:<hash>"。带本地内存快速缓存（10分钟有效期）。"""
    now = time.time()
    if song_id in _RESOLVE_CACHE:
        ts, cached_val = _RESOLVE_CACHE[song_id]
        if now - ts < 600:
            return cached_val
        _RESOLVE_CACHE.pop(song_id, None)

    # 1. 优先通过落雪自定义音源 (Custom Source) 引擎解析（彻底解耦内置音源，完全尊重用户导入启用的音源）
    try:
        clean_id = song_id
        if clean_id.startswith("online:"):
            clean_id = clean_id.replace("online:", "", 1)
        if clean_id.startswith("lx:"):
            clean_id = clean_id.replace("lx:", "", 1)
        parts = clean_id.split(":")
        if len(parts) >= 2:
            src = parts[0]
            identifier = ":".join(parts[1:])
            cfg = feats.load_settings()
            lx_server = cfg.get("lx_server_url") or "http://127.0.0.1:9528"
            cached_info = None
            try:
                info_resp = await client.get("/api/v1/track/info", params={"id": song_id}, timeout=2.5)
                if info_resp.status_code == 200:
                    cached_info = (info_resp.json() or {}).get("data")
            except Exception:
                pass

            t_meta_name = (track_info or {}).get("title") or (track_info or {}).get("name") or (cached_info or {}).get("title") or ""
            t_meta_singer = (track_info or {}).get("artist") or (track_info or {}).get("singer") or (cached_info or {}).get("artist") or ""
            if not t_meta_name and src == "wy" and identifier.isdigit():
                try:
                    async with httpx.AsyncClient(timeout=4.0) as detail_cli:
                        r_d = await detail_cli.get(f"https://music.163.com/api/song/detail/?id={identifier}&ids=[{identifier}]", headers={"User-Agent": "Mozilla/5.0"})
                        if r_d.status_code == 200:
                            s_list = (r_d.json() or {}).get("songs") or []
                            if s_list:
                                t_meta_name = str(s_list[0].get("name") or "")
                                t_meta_singer = " / ".join(filter(None, [a.get("name", "") for a in (s_list[0].get("artists") or [])]))
                except Exception:
                    pass
            if not t_meta_name:
                t_meta_name = identifier

            song_meta = {
                "id": identifier,
                "songmid": identifier,
                "source": src,
                "name": t_meta_name,
                "singer": t_meta_singer,
            }
            # 优先使用飞牛内置原生自定义音源执行引擎 (彻底不经过 9528)
            custom_res = await native_engine.resolve_music_url_native(
                song_meta,
                quality="flac",
            )
            if custom_res and custom_res.get("url"):
                res = {
                    "url": custom_res["url"],
                    "ext": custom_res.get("format", "flac"),
                    "actual_tier": "lossless",
                    "file_size": 31457280,
                    "source": custom_res.get("sourceName", "飞牛内置音源"),
                }
                _RESOLVE_CACHE[song_id] = (now, res)
                return res
            if not custom_res and t_meta_name and src != "kw":
                # 自动跨源故障转移：网易云/咪咕等无版权时，自动尝试酷我/波点对齐解析
                try:
                    kw_match = await feats.search_kuwo_song(t_meta_name, t_meta_singer)
                    if kw_match and kw_match.get("rid"):
                        kw_meta = {
                            "id": kw_match["rid"],
                            "songmid": kw_match["rid"],
                            "source": "kw",
                            "name": kw_match["title"],
                            "singer": kw_match["artist"],
                        }
                        custom_res = await native_engine.resolve_music_url_native(kw_meta, quality="flac")
                        if custom_res and custom_res.get("url"):
                            res = {
                                "url": custom_res["url"],
                                "ext": custom_res.get("format", "flac"),
                                "actual_tier": "lossless",
                                "file_size": 31457280,
                                "source": custom_res.get("sourceName", "飞牛内置音源"),
                            }
                            _RESOLVE_CACHE[song_id] = (now, res)
                            return res
                except Exception as e_kw:
                    logger.debug("fallback kuwo match error: %s", e_kw)

            if not custom_res:
                custom_res = await feats.resolve_url_by_custom_source(
                    song_meta,
                    quality="flac",
                    lx_server_url=lx_server,
                )
            if custom_res and custom_res.get("url"):
                res = {
                    "url": custom_res["url"],
                    "ext": custom_res.get("format", "flac"),
                    "actual_tier": "lossless",
                    "file_size": 31457280,
                    "source": custom_res.get("sourceName", "飞牛内置音源"),
                }
                _RESOLVE_CACHE[song_id] = (now, res)
                return res
    except Exception as e:
        logger.debug("custom source resolve attempt failed: %s", e)

    # 2. 回退至原有链路
    qualities = []
    primary = str(CONF.get("lx_quality") or "lossless").strip()
    if primary:
        qualities.append(primary)
    for fallback in ("high", "standard"):
        if fallback not in qualities:
            qualities.append(fallback)

    for q in qualities:
        try:
            r = await client.get(
                "/api/v1/track/url",
                params={"id": song_id, "quality": q},
                timeout=15.0,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict) and inner.get("url"):
                        _RESOLVE_CACHE[song_id] = (now, inner)
                        return inner
        except Exception as e:
            logger.warning("resolve_lx_url error for %s (quality=%s): %s", song_id, q, e)

    # 3. 故障转移至网易云音乐 API 直链解析 (当落雪源无法解析或已全部关闭时)
    try:
        t_name = (track_info or {}).get("title") or (track_info or {}).get("name") or (cached_info or {}).get("title") or (cached_info or {}).get("name") or ""
        t_artist = (track_info or {}).get("artist") or (track_info or {}).get("singer") or (cached_info or {}).get("artist") or (cached_info or {}).get("singer") or ""
        if not t_name:
            try:
                r_inf = await client.get("/api/v1/track/info", params={"id": song_id}, timeout=1.5)
                if r_inf.status_code == 200:
                    d_inf = (r_inf.json() or {}).get("data") or {}
                    t_name = d_inf.get("title") or d_inf.get("name") or ""
                    t_artist = d_inf.get("artist") or d_inf.get("singer") or ""
            except Exception:
                pass

        failover_res = await netease_api.netease_client.resolve_failover_track(
            song_id=song_id,
            title=t_name,
            artist=t_artist
        )
        if failover_res.get("ok") and failover_res.get("url"):
            res = {
                "url": failover_res["url"],
                "ext": failover_res.get("ext", "flac"),
                "actual_tier": failover_res.get("quality", "lossless"),
                "file_size": failover_res.get("size", 31457280),
                "source": "网易云音乐 (VIP故障转移)"
            }
            _RESOLVE_CACHE[song_id] = (now, res)
            logger.info(f"resolve_lx_url failover to NetEase API for {song_id} ({t_name} - {t_artist}) success!")
            return res
    except Exception as e:
        logger.warning("resolve_lx_url netease failover error for %s: %s", song_id, e)

    return None


async def resolve_netease_url(client: httpx.AsyncClient, song_id: str) -> str | None:
    # 优先使用原生集成网易云VIP客户端解析
    try:
        n_cfg = netease_api.load_netease_config()
        q = n_cfg.get("quality", "lossless")
        cookie = n_cfg.get("cookie", "")
        res = await netease_api.netease_client.get_song_url(song_id, level=q, cookie=cookie)
        if res.get("ok") and res.get("url"):
            return str(res["url"])
    except Exception as e:
        logger.warning("netease_client.get_song_url direct error for %s: %s", song_id, e)

    # 兜底 fallback
    qualities = []
    primary = str(CONF.get("netease_quality") or "lossless").strip()
    if primary:
        qualities.append(primary)
    if "exhigh" not in qualities:
        qualities.append("exhigh")

    for q in qualities:
        try:
            r = await client.get(f"/api/v1/song/{song_id}/url", params={"quality": q}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        code = inner.get("code")
                        url = inner.get("url")
                        if code == 200 and url:
                            return str(url)
        except Exception as e:
            logger.warning("resolve_netease_url error for %s (quality=%s): %s", song_id, q, e)
    return None


def ensure_search_list(upstream_json: dict) -> list:
    """保证 data.list 存在，本地 0 条时仍能追加在线条目。"""
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {}
        upstream_json["data"] = data
    target = get_by_path(upstream_json, CONF["search_list_path"])
    if isinstance(target, list):
        return target
    for key in ("list", "items", "tracks", "records"):
        if isinstance(data.get(key), list):
            if key != "list":
                data["list"] = data[key]
            return data["list"]
    data["list"] = []
    if "total" not in data:
        data["total"] = 0
    return data["list"]


def merge_online_tracks(
    upstream_json: dict,
    online_data: list[dict] | dict | None,
    page: int = 1,
    size: int = 50,
    selected: bool = False,
) -> dict:
    target_list = ensure_search_list(upstream_json)
    if not online_data:
        return upstream_json

    if isinstance(online_data, dict):
        raw_items = online_data.get("items", [])
    elif isinstance(online_data, list):
        raw_items = online_data
    else:
        raw_items = []

    if not raw_items:
        return upstream_json

    existing_keys = set()
    for item in target_list:
        t = title_from_track(item)
        a = artist_from_track(item)
        if t and a:
            existing_keys.add((t, a))

    online_limit = CONF["online_limit"]
    if not selected:
        start = 0 if page == 1 else online_limit + (page - 2) * size
        raw_page = raw_items[start:start + (online_limit if page == 1 else size)]
    else:
        raw_page = raw_items
    filtered_online = []
    for online_item in raw_page:
        if not is_playable_online_track(online_item, require_id=True):
            continue
        ot = str(online_item.get("title") or online_item.get("name") or "").strip().lower()
        oa = str(online_item.get("artist") or "").strip().lower()
        if ot and oa and (ot, oa) in existing_keys:
            continue
        filtered_online.append(online_item)

    page_online = filtered_online

    for it in page_online:
        target_list.append(build_online_track(it))

    parts = CONF["search_list_path"].split(".")
    parent = upstream_json
    for p in parts[:-1]:
        if isinstance(parent, dict) and p in parent:
            parent = parent[p]
    if isinstance(parent, dict):
        orig_total = parent.get("total")
        if not isinstance(orig_total, int):
            orig_total = len(target_list) - len(page_online)
        parent["total"] = orig_total + sum(1 for item in raw_items if is_playable_online_track(item, require_id=True) and (title_from_track(item), artist_from_track(item)) not in existing_keys)

    return upstream_json


def extract_guid(request: Request, path_guid: str | None = None) -> str:
    if path_guid:
        return path_guid
    return (
        request.query_params.get("guid")
        or request.query_params.get("trackGUID")
        or request.query_params.get("trackGuid")
        or request.query_params.get("coverId")
        or request.query_params.get("id")
        or request.query_params.get("trackId")
        or ""
    )


async def extract_guid_from_body(request: Request) -> str:
    guid = extract_guid(request)
    if guid:
        return guid
    try:
        body = await request.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(
            body.get("guid")
            or body.get("trackGUID")
            or body.get("trackGuid")
            or body.get("id")
            or body.get("trackId")
            or ""
        )
    return ""


def empty_ok() -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {}})


def build_lyric_list_payload(guid: str, lyric_text: str) -> dict:
    """对齐飞牛 $n.lyric.list → xr(list, preferred)。

    每条需有非空 content；source=2 表示 EXTERNAL_LRC（非内嵌，不强制 offset）。
    """
    text = (lyric_text or "").strip()
    if not text:
        return {"code": 0, "msg": "ok", "data": {"list": [], "preferred": ""}}
    lyric_guid = f"{guid}:lyric"
    now = int(time.time())
    item = {
        "guid": lyric_guid,
        "content": text,
        "source": 2,
        "isLRC": True,
        "offset": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {"list": [item], "preferred": lyric_guid},
    }


def stub_online_info(guid: str) -> dict:
    song_id = song_id_from_online_guid(guid)
    return {
        "id": song_id,
        "source": source_from_online_guid(guid),
        "title": "",
        "artist": "",
        "album": "",
        "duration_s": 0,
        "ext": "mp3",
        "file_size": 0,
        "cover_url": "",
        "lyric": "",
    }


def build_metadata_payload(guid: str, data: dict | None) -> dict:
    """飞牛 resolveTrackPlayback._h() 会无防护读取 data.track.genres.join / album / artists。

    缺 genres 或 album 不是对象时直接抛错，播放器跳过且不会请求 stream。
    """
    info = dict(data or {})
    info.setdefault("id", song_id_from_online_guid(guid))
    info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(info)
    album_obj = vo["album"] if isinstance(vo.get("album"), dict) else {
        "name": str(vo.get("album") or ""),
        "guid": f"{guid}:album",
        "artists": vo.get("artists") or [],
        "coverId": guid,
    }
    track = {
        "guid": guid,
        "id": guid,
        "title": vo.get("title") or "",
        "artists": vo.get("artists") or [],
        "album": album_obj,
        "genres": list(vo.get("genres") or []),
        "duration": vo.get("duration") or 0,
        "coverId": guid,
        "coverUrl": vo.get("coverUrl") or "",
        "format": vo.get("format") or "mp3",
        "hasLyric": bool(vo.get("hasLyric") or info.get("lyric")),
        "isFavorite": False,
        "isCue": False,
        "accessStatus": 0,
        "audioSpec": vo["audioSpec"],
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **vo,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": vo["audioSpec"],
            "track": track,
        },
    }


def _conf_log_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _REDACT_KEY_PARTS):
        return "***" if value else ""
    return value


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    logger.info("=== fnmusic-ext v%s configuration ===", get_version())
    for k, v in CONF.items():
        logger.info("  %s = %s", k, _conf_log_value(k, v))
    logger.info("  llm_enabled = %s", dailyrec.llm_enabled())
    logger.info("==================================")

    created_upstream = False
    created_musicdl = False
    created_musicbox = False
    created_lx = False
    created_llm = False

    if getattr(fastapi_app.state, "upstream_client", None) is None:
        fastapi_app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"]),
            base_url="http://unix",
            timeout=30.0,
        )
        created_upstream = True

    if getattr(fastapi_app.state, "musicdl_client", None) is None:
        fastapi_app.state.musicdl_client = httpx.AsyncClient(
            base_url=CONF["musicdl_url"],
            timeout=45.0,
        )
        created_musicdl = True

    if getattr(fastapi_app.state, "musicbox_client", None) is None:
        fastapi_app.state.musicbox_client = httpx.AsyncClient(
            base_url=CONF["musicbox_url"],
            timeout=20.0,
        )
        created_musicbox = True

    if getattr(fastapi_app.state, "lx_client", None) is None:
        fastapi_app.state.lx_client = httpx.AsyncClient(
            base_url=CONF["lx_url"],
            timeout=25.0,
        )
        created_lx = True

    if getattr(fastapi_app.state, "llm_client", None) is None:
        fastapi_app.state.llm_client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        created_llm = True

    try:
        yield
    finally:
        tasks = [entry["task"] for entry in _SEARCH_CACHE.values() if entry.get("task") and not entry["task"].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if created_upstream and getattr(fastapi_app.state, "upstream_client", None):
            await fastapi_app.state.upstream_client.aclose()
            fastapi_app.state.upstream_client = None
        if created_musicdl and getattr(fastapi_app.state, "musicdl_client", None):
            await fastapi_app.state.musicdl_client.aclose()
            fastapi_app.state.musicdl_client = None
        if created_musicbox and getattr(fastapi_app.state, "musicbox_client", None):
            await fastapi_app.state.musicbox_client.aclose()
            fastapi_app.state.musicbox_client = None
        if created_lx and getattr(fastapi_app.state, "lx_client", None):
            await fastapi_app.state.lx_client.aclose()
            fastapi_app.state.lx_client = None
        if created_llm and getattr(fastapi_app.state, "llm_client", None):
            await fastapi_app.state.llm_client.aclose()
            fastapi_app.state.llm_client = None


app = FastAPI(title="fnmusic-ext", lifespan=lifespan)
from starlette.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Accept-Ranges", "Content-Range", "Content-Length", "Content-Type"],
)


@app.get("/_ext/livez")
async def ext_livez():
    return {"ok": True, "service": "fnmusic-ext", "pid": os.getpid()}


@app.get("/_ext/healthz")
async def ext_healthz(request: Request):
    async def probe(name: str, client: httpx.AsyncClient, path: str) -> dict:
        try:
            response = await asyncio.wait_for(client.get(path, timeout=2.0), timeout=2.4)
            healthy = response.status_code < 500 if name == "upstream" else response.status_code == 200
            detail: dict = {"status": "ok" if healthy else "fail", "http_status": response.status_code}
            if name != "upstream" and healthy:
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        detail["dependency"] = payload
                        if payload.get("ok") is False:
                            detail["status"] = "fail"
                    else:
                        detail["status"] = "fail"
                except ValueError:
                    detail["status"] = "fail"
                    detail["error"] = "invalid health JSON"
            return detail
        except Exception as exc:
            return {"status": "fail", "error": type(exc).__name__}

    checks = [("upstream", True, get_upstream_client, "/music/api/v1/search/track?keyword=healthz_probe"),
              ("musicdl", CONF.get("musicdl_enabled", False), get_musicdl_client, "/healthz"),
              ("musicbox", CONF.get("netease_enabled", False), get_musicbox_client, "/healthz"),
              ("lxmusic", CONF.get("lx_enabled", False), get_lx_client, "/healthz")]
    enabled = [(name, getter, path) for name, on, getter, path in checks if on]
    results = await asyncio.gather(*(probe(name, getter(request.app), path) for name, getter, path in enabled))
    details = {name: {"status": "disabled"} for name, on, _, _ in checks if not on}
    details.update({name: result for (name, _, _), result in zip(enabled, results)})
    statuses = {name: value["status"] for name, value in details.items()}
    failed = [name for name, status in statuses.items() if status == "fail"]
    # 独立运行版本内置原生 Node 沙箱引擎 (native_engine) 与多源直连，只要上游官方正常即健康
    native_engine_ok = True
    source_ok = any(statuses.get(name) == "ok" for name in ("musicdl", "musicbox", "lxmusic")) or native_engine_ok
    return {"ok": statuses["upstream"] == "ok" and source_ok, "version": get_version(),
            "native_engine": "ok",
            **statuses, "llm": "enabled" if dailyrec.llm_enabled() else "disabled",
            "degraded": bool(failed), "failures": failed, "details": details}


@app.get("/music/api/v1/search/track")
@app.get("/music/api/v1/search/track/{subpath:path}")
async def search_track(request: Request):
    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    musicbox_client = get_musicbox_client(request.app)
    keyword = extract_keyword(request)

    page_str = request.query_params.get("page")
    try:
        page = int(page_str) if page_str else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    size_str = request.query_params.get("size")
    try:
        size = int(size_str) if size_str else 50
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)

    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    if not keyword:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    key = _search_scope(request) + ":" + keyword
    _clean_search_cache()
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0, "keyword": keyword,
                 "scope": _search_scope(request), "credentials": _credential_scope(request), "config": _source_config(), "task": None}
        _set_search_cache(key, entry)
    entry["accessed"] = time.time()
    task = entry.get("task")
    if (not task or task.done()) and time.time() - entry["ts"] >= _search_ttl(entry):
        task = asyncio.create_task(_aggregate_search(request, keyword, entry))
        entry["task"] = task
    if task and not task.done():
        if page == 1:
            await asyncio.wait({task}, timeout=float(CONF["netease_wait_s"]))
            # Empty/error completions do not exhaust the remaining wait budget.
            if not entry["items"]:
                deadline = asyncio.get_running_loop().time() + float(CONF["late_page_wait_s"])
                while not entry["items"] and not task.done() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.wait({task}, timeout=min(0.02, max(0, deadline - asyncio.get_running_loop().time())))
        else:
            await asyncio.wait({task}, timeout=float(CONF["late_page_wait_s"]))
    local_list = ensure_search_list(upstream_json)
    
    # 搜索过滤与精准匹配重构：
    # 1. 严格检查本地音乐：
    #    - 必须真实存在物理音频文件 (根据 spec.path、spec.size、guid 等校验)
    #    - 且歌名/歌手必须真正匹配搜索词 keyword（防止官方模糊匹配返回无关的《白鸽乌鸦相爱的戏码》等）
    # 2. 如果本地音乐不存在或不符合搜索词，绝不在结果中显示本地文件，直接展示符合的在线音乐；
    # 3. 只有本地真实存在且名称符合搜索词的物理音乐文件，才在搜索结果中优先展示！
    def _is_real_and_matching_local_music(item: dict, kw: str) -> bool:
        if not isinstance(item, dict):
            return False
        # 如果是已知在线注入的条目，直接放行
        if is_online_guid(item.get("guid") or item.get("id") or ""):
            return True

        # 检查是否真实匹配搜索词
        t = str(item.get("title") or item.get("name") or "").strip().lower()
        a = artist_from_track(item).strip().lower()
        kw_clean = kw.strip().lower()
        if kw_clean:
            # 必须在歌名、歌手或专辑中包含关键字，排除无关条目
            album_name = ""
            if isinstance(item.get("album"), dict):
                album_name = str(item["album"].get("name") or "").strip().lower()
            elif isinstance(item.get("album"), str):
                album_name = item.get("album", "").strip().lower()
            if kw_clean not in t and kw_clean not in a and kw_clean not in album_name:
                return False

        # 校验本地物理文件是否存在
        spec = item.get("audioSpec") or {}
        p = spec.get("path") or item.get("path")
        if p and isinstance(p, str):
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return True
            if not p.startswith("/"):
                for prefix in ("/vol1/1000", "/vol1", "/"):
                    cand = os.path.join(prefix, p)
                    if os.path.exists(cand) and os.path.getsize(cand) > 0:
                        return True
        if spec.get("size", 0) > 0 and spec.get("duration", 0) > 0 and spec.get("format"):
            if p and os.path.exists(p):
                return True
        guid = item.get("guid") or item.get("id") or ""
        if guid and recalled_media_path(guid):
            return True
        return False

    # 过滤本地条目：只保留真实存在物理音频文件且名称匹配搜索词的本地歌曲
    real_local_list = [x for x in local_list if _is_real_and_matching_local_music(x, keyword)]
    local_list.clear()
    local_list.extend(real_local_list)

    local_keys = {(title_from_track(x), artist_from_track(x)) for x in local_list}
    total_online = sum(1 for x in entry["items"] if (title_from_track(x), artist_from_track(x)) not in local_keys)
    original_total = len(local_list)
    selected = _session_page(entry, page, size)
    merged = merge_online_tracks(upstream_json, selected, page=1, size=size, selected=True)
    if isinstance(original_total, int):
        merged["data"]["total"] = original_total + total_online
    return JSONResponse(content=merged, status_code=upstream_resp.status_code, headers=resp_headers)


async def fetch_netease_api_search(keyword: str, limit: int = 30) -> list[dict]:
    if not keyword:
        return []
    try:
        cfg = netease_api.load_netease_config()
        if not cfg.get("enabled", True) and not cfg.get("cookie"):
            return []
        songs = await netease_api.netease_client.search_songs(keyword, limit=limit)
        items = []
        for s in songs:
            sid = str(s.get("id"))
            items.append({
                "id": f"netease:{sid}",
                "source": "netease",
                "title": s.get("name") or "",
                "artist": s.get("artist") or "",
                "album": s.get("album") or "",
                "duration_s": float(s.get("duration") or 0),
                "ext": "flac",
                "quality": "FLAC",
                "file_size": 31457280,
                "cover_url": s.get("pic_url") or "",
                "has_stream": True,
                "download_url": "",
            })
        return items
    except Exception as e:
        logger.warning("fetch_netease_api_search failed: %s", e)
        return []


async def fetch_native_suggest_search(keyword: str) -> list[dict]:
    try:
        res = await feats.get_search_suggestions(keyword)
        cands = res.get("candidates") or []
        items = []
        for c in cands:
            if not c.get("is_local"):
                items.append({
                    "id": c["id"],
                    "source": "lx",
                    "title": c["title"],
                    "artist": c["artist"],
                    "album": c["album"],
                    "duration_s": c.get("duration_s", 240),
                    "ext": "flac",
                    "quality": "FLAC",
                    "file_size": 31457280,
                    "cover_url": c.get("cover_url", ""),
                    "has_stream": True,
                })
        return items
    except Exception as e:
        logger.debug("fetch_native_suggest_search failed: %s", e)
        return []


async def _aggregate_search(request: Request, keyword: str, entry: dict) -> None:
    sources = []
    # 1. 优先加入内置原生联想与模糊匹配结果 (网易云 + 酷我并发直连)
    sources.append(fetch_native_suggest_search(keyword))
    # 2. 网易云音乐原生 API 搜索
    try:
        n_cfg = netease_api.load_netease_config()
        if n_cfg.get("enabled", True) or n_cfg.get("cookie"):
            sources.append(fetch_netease_api_search(keyword, int(CONF.get("netease_search_limit", 30))))
    except Exception:
        pass
    if CONF.get("netease_enabled", True):
        sources.append(fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"]))
    if CONF.get("musicdl_enabled", True):
        sources.append(fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"]))
    if CONF.get("lx_enabled", True):
        sources.append(fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"]))
    tasks = [asyncio.create_task(coro) for coro in sources]
    pending = set(tasks)
    partial = False
    results: dict[asyncio.Task, list] = {}
    try:
        deadline = asyncio.get_running_loop().time() + max(1.0, float(CONF["search_timeout"]))
        while pending:
            done, pending = await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                partial = True
                break
            for task in tasks:
                if task not in done:
                    continue
                try:
                    data = task.result()
                except Exception:
                    data = None
                partial |= data is None or bool(getattr(data, "partial", False)) or (isinstance(data, dict) and bool(data.get("errors") or data.get("ok") is False))
                items = data.get("items", []) if isinstance(data, dict) else (data or [])
                results[task] = items
                if not entry["pages"]:
                    ordered = [item for source_task in tasks for item in results.get(source_task, [])]
                else:
                    ordered = entry["items"] + items
                entry["items"] = deduplicate_online_items(ordered, keyword=keyword)[:2000]
        entry["partial"] = partial
        entry["ts"] = time.time()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _session_page(entry: dict, page: int, size: int) -> list[dict]:
    pages = entry["pages"]
    count = int(CONF["online_limit"]) if page == 1 else size
    if page not in pages:
        if len(pages) >= 2000:
            return []
        pages[page] = []
    # Only the trailing page can grow; no published prefix ever moves. This
    # also lets a repeated empty first page recover after its negative TTL.
    if page == max(pages):
        start = entry["cursor"]
        allocated = entry["items"][start:start + max(0, count - len(pages[page]))]
        pages[page].extend(online_guid_from_item(item) for item in allocated)
        entry["cursor"] += len(allocated)
    by_guid = {online_guid_from_item(item): item for item in entry["items"]}
    return [by_guid[guid] for guid in pages[page] if guid in by_guid and _source_enabled(guid)]


@app.get("/music/api/v1/search/suggest")
@app.get("/music/api/v1/search/suggest/{subpath:path}")
@app.get("/music/ext/api/search/suggest")
async def search_suggest(request: Request):
    """
    智能搜索联想与候选音乐选项 (脱离落雪，支持网易云+酷我+本地数据库秒级模糊匹配)
    """
    keyword = extract_keyword(request)
    if not keyword:
        return JSONResponse(content={"code": 0, "msg": "ok", "data": [], "words": [], "candidates": []})

    suggest_res = await feats.get_search_suggestions(keyword)
    words = suggest_res.get("words") or []
    candidates = suggest_res.get("candidates") or []

    return JSONResponse(content={
        "code": 0,
        "msg": "ok",
        "data": words,
        "words": words,
        "candidates": candidates,
    })


def stream_tee_response(
    resp: httpx.Response,
    guid: str,
    range_header: str | None,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    client_to_close: httpx.AsyncClient | None = None,
    resolved_ext: str | None = None,
    pre_info: dict | None = None,
    chunks: Any = None,
    first_chunk: bytes = b"",
) -> Response:
    headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    for key in ("content-type", "content-length", "content-range"):
        if resp.headers.get(key):
            headers[key] = resp.headers[key]
    if resolved_ext:
        headers["content-type"] = media_type_for_ext(resolved_ext)
    length = resp.headers.get("content-length", "")
    expected = int(length) if length.isdigit() else None
    full_resource = resp.status_code == 200
    if resp.status_code == 206:
        # Content-Length proves only this segment, not the entire recording.
        match = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", resp.headers.get("content-range", "").strip(), re.I)
        full_resource = bool(match and int(match[1]) + 1 == int(match[2]) and int(match[2]) > 0)
        if full_resource:
            total = int(match[2])
            full_resource = expected is None or expected == total
            expected = total
    ext = resolved_ext or ext_from_content_type(resp.headers.get("content-type", ""))

    async def body() -> AsyncGenerator[bytes, None]:
        # Pull-through provides backpressure: no unbounded producer queue and no
        # downloader outliving its consumer. Only a clean EOF may finalize.
        part = None
        fp = None
        written = 0
        eof = False
        info_task = None
        try:
            if should_cache(range_header) and full_resource:
                directory = detect_library_dir()
                os.makedirs(directory, exist_ok=True)
                part = os.path.join(directory, f"{cache_safe_guid(guid)}.{uuid4().hex}.part")
                fp = open(part, "wb")
                if pre_info is None and coro_factory:
                    info_task = asyncio.create_task(coro_factory())
            iterator = chunks if chunks is not None else resp.aiter_bytes()
            if first_chunk:
                if fp:
                    fp.write(first_chunk)
                written += len(first_chunk)
                yield first_chunk
            async for chunk in iterator:
                if chunk:
                    if fp:
                        fp.write(chunk)
                    written += len(chunk)
                    yield chunk
            eof = True
            if fp:
                fp.close()
                fp = None
            info = pre_info
            if info is None and info_task:
                try:
                    info = await asyncio.wait_for(info_task, timeout=8.0)
                except Exception:
                    info = None
            if part and eof and written >= 1024 and (expected is None or written == expected):
                title, artist, album = (str((info or {}).get(k) or "") for k in ("title", "artist", "album"))
                dest = library_media_path(guid, title, ext, artist=artist)
                os.replace(part, dest)
                remember_media_path(guid, dest)
                adopt_library_perms(dest)
                write_audio_tags(dest, title, artist, album)
                lyric = str((info or {}).get("lyric") or (info or {}).get("lrc") or "")
                if lyric.strip():
                    write_lyric_cache(guid, lyric, title, artist)
        finally:
            if fp:
                fp.close()
            if part and os.path.exists(part):
                os.remove(part)
            with anyio.CancelScope(shield=True):
                if info_task and not info_task.done():
                    info_task.cancel()
                    await asyncio.gather(info_task, return_exceptions=True)
                await resp.aclose()
                if client_to_close:
                    await client_to_close.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)


def _credential_scope(request: Request) -> str:
    return hashlib.sha256(json.dumps([request.headers.get(k, "") for k in
        ("cookie", "authorization", "x-trim-music-temp-token")]).encode()).hexdigest()


def _retained_track(request: Request, guid: str) -> tuple[dict | None, dict | None]:
    _clean_search_cache()
    for entry in reversed(list(_SEARCH_CACHE.values())):
        if entry.get("credentials") != _credential_scope(request) or entry.get("config", _source_config()) != _source_config():
            continue
        for item in entry.get("items", []):
            if online_guid_from_item(item) == guid:
                return item, entry
            for alternative in item.get("_alternatives", []):
                if online_guid_from_item(alternative) == guid:
                    return alternative, entry
    return None, None


async def _recover_source(request: Request, guid: str, entry: dict | None) -> bool:
    """One bounded source re-search after a backend loses its in-memory IDs."""
    if not entry or not _source_enabled(guid):
        return False
    source = source_from_online_guid(guid)
    recovery_key = "recovered:" + source
    if time.monotonic() - entry.get(recovery_key, -1000) < 30:
        return False
    entry[recovery_key] = time.monotonic()
    keyword = entry.get("keyword", "")
    if source == "netease":
        coro = fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"])
    elif source == "lx":
        coro = fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"])
    else:
        selected = [name.strip() for name in str(CONF.get("online_sources") or "").split(",")
                    if name.strip().lower().removesuffix("musicclient") == source.lower()]
        coro = fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"],
                                   ",".join(selected) or source)
    try:
        result = await asyncio.wait_for(coro, timeout=3.0)
        items = result.get("items", []) if isinstance(result, dict) else (result or [])
        return any(online_guid_from_item(item) == guid for item in items)
    except Exception:
        return False


async def _open_online_stream(request: Request, guid: str, range_header: str | None):
    """Resolve and read first bytes before committing HTTP headers to the client."""
    source = source_from_online_guid(guid)
    info, _ = _retained_track(request, guid)
    headers = {"Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
    owned = None
    resp = None
    ext = None
    try:
        if source in ("netease", "lx"):
            if source == "netease":
                url = await resolve_netease_url(get_musicbox_client(request.app), song_id_from_online_guid(guid).split(":")[-1])
                if not url:
                    return None
            else:
                resolved = await resolve_lx_url(get_lx_client(request.app), song_id_from_online_guid(guid), track_info=info)
                if not resolved:
                    return None
                url = resolved["url"]
                ext = resolved.get("ext")
                for key, value in (resolved.get("headers") or {}).items():
                    if key.lower() in ("referer", "user-agent"):
                        headers[key] = str(value)
                # 针对酷我等音源防盗链防护：若没有提供 UA，补上标准移动端 UA 避免 403
                if "user-agent" not in {k.lower() for k in headers}:
                    headers["User-Agent"] = "okhttp/3.10.0"
            if info is None:
                try:
                    info = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=0.75)
                except Exception:
                    pass
            ext = ext or (info or {}).get("ext")
            owned = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
            client = owned
            req = client.build_request("GET", url, headers=headers)
        else:
            client = get_musicdl_client(request.app)
            req = client.build_request("GET", "/stream", params={"id": song_id_from_online_guid(guid), "proxy": "true"}, headers=headers)
        resp = await client.send(req, stream=True)
        content_type = resp.headers.get("content-type", "").lower()
        if (resp.status_code not in (200, 206)
                or any(x in content_type for x in ("text/", "json"))
                or resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity")):
            # Reject servers ignoring identity: decoded bytes cannot use encoded
            # Content-Length/Range offsets, and must not enter the audio cache.
            _RESOLVE_CACHE.pop(song_id_from_online_guid(guid), None)
            return None
        chunks = resp.aiter_bytes()
        first = await anext(chunks, b"")
        if not first:
            _RESOLVE_CACHE.pop(song_id_from_online_guid(guid), None)
            return None
        result = (resp, owned, ext, info, chunks, first)
        resp = owned = None  # transfer ownership to response iterator
        return result
    finally:
        if resp:
            await resp.aclose()
        if owned:
            await owned.aclose()


@app.api_route("/music/api/v1/track/stream", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/track/stream/{subpath:path}", methods=["GET", "HEAD"])
async def stream_track(request: Request):
    guid = extract_guid(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    range_header = request.headers.get("range")
    cached = find_cache_file(guid)
    if cached:
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        return serve_file_with_range(cached, range_header, media_type_for_ext(ext))

    # 对于 HEAD 请求，快速获取歌曲大小和格式并返回 200/206 头部，避免耗时阻塞
    if request.method == "HEAD":
        ext = "flac"
        size = 31457280
        info, _ = _retained_track(request, guid)
        if info:
            ext = str(info.get("ext") or "flac")
            size = int(info.get("file_size") or size)
        resp_headers = {
            "Content-Type": media_type_for_ext(ext),
            "Content-Length": str(size),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        }
        return Response(status_code=200, headers=resp_headers)

    item, entry = _retained_track(request, guid)
    candidates = [guid]
    # Byte offsets are encoding-specific: do not cross sources on seek/probe.
    if should_cache(range_header) and item and request.query_params.get("_ext_rendition") != "1":
        candidates += [online_guid_from_item(x) for x in item.get("_alternatives", []) if _same_recording(item, x)]
    deadline = asyncio.get_running_loop().time() + 12.0
    for candidate in list(dict.fromkeys(candidates))[:3]:
        if not _source_enabled(candidate):
            continue
        for attempt in range(2):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                # 留足 8 秒超时预算，避免落雪自定义源多层探测时被 4.0s 提前截断导致失败
                opened = await asyncio.wait_for(_open_online_stream(request, candidate, range_header), timeout=min(8.0, remaining))
            except Exception as exc:
                logger.warning("Stream startup failed for %s: %s", candidate, type(exc).__name__)
                opened = None
            if opened:
                resp, owned, ext, info, chunks, first = opened
                if candidate != guid:
                    # Publish the selected source identity before any audio.
                    # The client owns B's URL for later Range requests even if
                    # this proxy's search session expires; never alias B as A.
                    with anyio.CancelScope(shield=True):
                        await resp.aclose()
                        if owned:
                            await owned.aclose()
                    target = request.url.include_query_params(guid=candidate, _ext_rendition="1")
                    return RedirectResponse(str(target), status_code=307, headers={"Cache-Control": "no-store"})
                # Cache the selected source's bytes under its own GUID, never
                # splice a failed stream or alias different encodings for seeks.
                return stream_tee_response(resp, candidate, range_header,
                    coro_factory=lambda: _online_info(request, candidate), client_to_close=owned,
                    resolved_ext=ext, pre_info=info, chunks=chunks, first_chunk=first)
            if attempt or deadline - asyncio.get_running_loop().time() <= 3:
                break
            if not await _recover_source(request, candidate, entry):
                break
    return JSONResponse(content={"code": 404, "msg": "online source unavailable", "data": None}, status_code=404)


@app.get("/music/api/v1/track/hls/{guid}/preset.m3u8")
@app.get("/music/api/v1/track/hls/{guid}/{filename}")
async def track_hls(request: Request, guid: str, filename: str = "preset.m3u8"):
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    info = await _online_info(request, guid)
    duration_s = 0
    if info:
        try:
            duration_s = int(float(info.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration_s = 0
    if duration_s <= 0:
        duration_s = 240

    stream_url = f"/music/api/v1/track/stream?guid={quote(guid, safe='')}"
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{max(duration_s, 1)}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{duration_s:.3f},\n"
        f"{stream_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    return Response(content=playlist, media_type="application/vnd.apple.mpegurl")


@app.api_route("/music/api/v1/track/transcode/heartbeat", methods=["GET", "POST"])
@app.api_route("/music/api/v1/track/transcode/quit", methods=["GET", "POST"])
async def track_transcode_session(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})


@app.api_route("/music/api/v1/track/transcode", methods=["GET", "POST"])
async def track_transcode(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "status": "ready",
            "data": {"guid": guid, "status": "ready"},
        }
    )


async def _online_info(request: Request, guid: str) -> dict | None:
    retained, entry = _retained_track(request, guid)
    if not _source_enabled(guid):
        return retained
    try:
        data = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=4.0)
        if data:
            return data
        if await _recover_source(request, guid, entry):
            data = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=3.0)
            if data:
                return data
    except Exception:
        pass
    return retained


async def _fetch_online_info(request: Request, guid: str) -> dict | None:
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    data = res_data.get("data")
                    if isinstance(data, dict):
                        name = str(data.get("name") or "")
                        ar = data.get("ar") or []
                        ar_names = []
                        if isinstance(ar, list):
                            for x in ar:
                                if isinstance(x, dict) and x.get("name"):
                                    ar_names.append(str(x["name"]))
                                elif isinstance(x, str):
                                    ar_names.append(x)
                        artist = " / ".join(ar_names)
                        al = data.get("al") or {}
                        album_name = str(al.get("name") or "") if isinstance(al, dict) else ""
                        cover_url = str(al.get("picUrl") or "") if isinstance(al, dict) else ""
                        dt = data.get("dt") or 0
                        duration_s = float(dt) / 1000.0 if dt else 0.0
                        sq = data.get("sq")
                        hr = data.get("hr")
                        h = data.get("h") or {}
                        ext = "flac" if (sq or hr) else "mp3"
                        size_obj = sq or h or {}
                        file_size = int(size_obj.get("size", 0) or 0) if isinstance(size_obj, dict) else 0

                        lyric_text = ""
                        try:
                            lr = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
                            if lr.status_code == 200:
                                l_res = lr.json()
                                if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                    l_data = l_res.get("data")
                                    if isinstance(l_data, dict):
                                        lyric_text = str(l_data.get("lyric") or "").strip()
                        except Exception as l_err:
                            logger.warning("musicbox lyric fetch in _online_info failed for %s: %s", guid, l_err)

                        return {
                            "id": f"netease:{song_id}",
                            "source": "netease",
                            "title": name,
                            "artist": artist,
                            "album": album_name,
                            "cover_url": cover_url,
                            "duration_s": duration_s,
                            "ext": ext,
                            "file_size": file_size,
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("musicbox /info failed for %s: %s", guid, e)
        return None

    if src == "lx":
        song_id = song_id_from_online_guid(guid)
        # 彻底脱离落雪 8772：直接使用原生开放接口拉取歌曲元数据、封面与歌词
        try:
            parts = song_id.split(":")
            sub_src = parts[1] if len(parts) >= 2 else "kw"
            sub_id = parts[-1]
            if sub_src == "wy" and sub_id.isdigit():
                from netease_api import NetEaseClient
                ne_cli = NetEaseClient()
                import json
                c_param = json.dumps([{"id": int(sub_id)}])
                payload = {"c": c_param, "csrf_token": ""}
                res = await ne_cli._post_weapi("/weapi/v3/song/detail", payload)
                songs = (res or {}).get("songs") or []
                if songs:
                    s = songs[0]
                    ar_names = [a.get("name", "") for a in (s.get("ar") or []) if a.get("name")]
                    ar_str = " / ".join(filter(None, ar_names)) or "华语群星"
                    al = s.get("al") or {}
                    album_name = str(al.get("name") or "").strip() or "精选专辑"
                    cover_u = str(al.get("picUrl") or "")
                    dur_s = float(s.get("dt") or 240000) / 1000.0
                    return {
                        "id": song_id,
                        "source": "lx",
                        "lx_source": "wy",
                        "title": str(s.get("name") or ""),
                        "artist": ar_str,
                        "album": album_name,
                        "cover_url": cover_u,
                        "duration_s": dur_s,
                        "ext": "flac",
                        "file_size": 31457280,
                        "lyric": "",
                    }
            elif sub_src == "kw" and sub_id.isdigit():
                import ast
                item = {}
                async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
                    url_kw = f"http://search.kuwo.cn/r.s?rid=MUSIC_{sub_id}&ft=music&client=kt&rformat=json&encoding=utf8"
                    r_kw = await client.get(url_kw)
                    if r_kw.status_code == 200:
                        try:
                            data = ast.literal_eval(r_kw.text.replace("&nbsp;", " "))
                            item = (data.get("abslist") or [{}])[0]
                        except Exception:
                            pass
                title = str(item.get("SONGNAME") or "").strip() or f"酷我单曲-{sub_id}"
                artist = str(item.get("ARTIST") or "").strip() or "精选歌手"
                album = str(item.get("ALBUM") or "").strip() or "精选专辑"
                dur_s = float(item.get("DURATION") or 240)
                kw_cover = f"http://artistpicserver.kuwo.cn/pic.web?type=rid_pic&pictype=url&size=500&rid={sub_id}"
                return {
                    "id": song_id,
                    "source": "lx",
                    "lx_source": "kw",
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "cover_url": kw_cover,
                    "duration_s": dur_s,
                    "ext": "flac",
                    "file_size": 31457280,
                    "lyric": "",
                }
        except Exception as e:
            logger.warning("native fetch online info for %s failed: %s", guid, e)
        return None

    musicdl_client = get_musicdl_client(request.app)
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("ok") is not False:
                return data
    except Exception as e:
        logger.warning("musicdl /info failed for %s: %s", guid, e)
    return None


@app.get("/music/api/v1/lyric/list")
@app.get("/music/api/v1/lyric/list/{subpath:path}")
async def lyric_list(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    return JSONResponse(content=build_lyric_list_payload(guid, lyric_text))


@app.get("/music/api/v1/track/lyrics")
@app.get("/music/api/v1/track/lyrics/{subpath:path}")
@app.get("/music/api/v1/detail/lyrics/{subpath:path}")
async def track_lyrics(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    if lyric_text:
        res = {"code": 0, "msg": "ok", "data": {"guid": guid, "lyric": lyric_text}}
        set_by_path(res, CONF["lyric_field"], lyric_text)
        return JSONResponse(content=res)
    return empty_ok()


_KUWO_REAL_COVER_CACHE: dict[str, str] = {}

async def _resolve_kuwo_real_cover(cover_url: str) -> str:
    if not cover_url or "artistpicserver.kuwo.cn" not in cover_url:
        return cover_url
    cached = _KUWO_REAL_COVER_CACHE.get(cover_url)
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(timeout=3.5, follow_redirects=True) as client:
            r = await client.get(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            text = r.text.strip()
            if text.startswith("http"):
                _KUWO_REAL_COVER_CACHE[cover_url] = text
                return text
    except Exception as e:
        logger.debug("resolve kuwo real cover error: %s", e)
    return cover_url


def _find_track_title_artist_from_cache(guid: str) -> tuple[str, str]:
    import glob
    cache_dir = os.path.expanduser("~/.local/state/fnmusic_ext/online_pl_cache")
    if os.path.isdir(cache_dir):
        for fpath in glob.glob(os.path.join(cache_dir, "detail_*.json")):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for t in data.get("tracks", []):
                        if t.get("id") == guid or t.get("guid") == guid or (t.get("song_id") and guid.endswith(str(t.get("song_id")))):
                            return str(t.get("title") or ""), str(t.get("artist") or "")
            except Exception:
                pass
    return "", ""


@app.get("/music/api/v1/track/metadata")
@app.get("/music/api/v1/track/metadata/{subpath:path}")
@app.get("/music/api/v1/track/audio-info")
async def track_metadata(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    logger.info(">>> [TRACK_METADATA_CALLED] guid=%s path=%s query=%s", guid, request.url.path, request.url.query)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid) or stub_online_info(guid)
    
    # 智能补全标题与歌手 (从歌单详情缓存中反查)
    title = str(data.get("title") or "")
    artist = str(data.get("artist") or "")
    if not title or title.startswith("lx:"):
        c_title, c_artist = _find_track_title_artist_from_cache(guid)
        if c_title:
            data["title"] = c_title
            title = c_title
        if c_artist:
            data["artist"] = c_artist
            artist = c_artist

    # 若缺少封面或缺少专辑信息，自动调用落雪源与网易云 API 跨源智能补全
    if (not data.get("cover_url") or data.get("album") in ("", "精选专辑", "未知专辑")) and title:
        try:
            en = await netease_api.netease_client.enrich_cover_and_lyric(title, artist)
            if en.get("ok"):
                if not data.get("cover_url") and en.get("pic_url"):
                    data["cover_url"] = en["pic_url"]
                if (not data.get("album") or data.get("album") in ("精选专辑", "未知专辑")) and en.get("album"):
                    data["album"] = en["album"]
                if not data.get("lyric") and en.get("lyric"):
                    data["lyric"] = en["lyric"]
        except Exception as e:
            logger.debug("metadata enrich failed: %s", e)

    # 酷我 artistpicserver URL 自动解析为真实 JPEG 图片
    if data.get("cover_url") and "artistpicserver.kuwo.cn" in data["cover_url"]:
        try:
            data["cover_url"] = await _resolve_kuwo_real_cover(data["cover_url"])
        except Exception:
            pass

    cached_lyric = read_lyric_cache(guid)
    if cached_lyric:
        data = {**data, "lyric": cached_lyric}
    elif data.get("lyric"):
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
    return JSONResponse(content=build_metadata_payload(guid, data))


@app.api_route("/music/static/cover", methods=["GET", "HEAD"])
@app.api_route("/music/static/cover/{subpath:path}", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/static/cover", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/static/cover/{subpath:path}", methods=["GET", "HEAD"])
async def static_cover(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not guid and subpath.startswith("online:"):
        guid = subpath

    # 优雅音乐艺术高清封面备选池
    cur_time_bucket = int(time.time() // 1800)
    fallback_covers = [
        "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1470225620780-dba8ba36b745?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1514525253161-7a46d19cd819?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1487180144351-b8472da7d491?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1445985543470-41fdd6ce388d?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1465847899084-d164df4dedc6?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1518609878373-06d740f60d8b?w=800&auto=format&fit=crop&q=80"
    ]

    # 1. 💖 我的心动歌曲固定专属高清黑胶封面
    if guid in ("ai:heartbeat:recommend", "ai_heartbeat_recommend"):
        hb_custom = "/usr/local/apps/@appcenter/trim.music/static/assets/heartbeat_cover.png"
        if os.path.exists(hb_custom):
            return FileResponse(hb_custom, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
        chosen = fallback_covers[cur_time_bucket % len(fallback_covers)]
        return RedirectResponse(chosen, status_code=302)

    # 2. 📥 下载管理固定专属科技青绿封面
    if guid in ("local:downloads", "local_downloads"):
        dl_custom = "/usr/local/apps/@appcenter/trim.music/static/assets/downloads_cover.png"
        if os.path.exists(dl_custom):
            return FileResponse(dl_custom, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
        chosen = fallback_covers[cur_time_bucket % len(fallback_covers)]
        return RedirectResponse(chosen, status_code=302)

    # 3. 🌟 每日推荐固定专属晨曦金橙封面
    if dailyrec.is_daily_playlist_guid(guid) or guid in ("daily:recommend", "daily_recommend") or str(guid).startswith("online:playlist:daily"):
        daily_custom = "/usr/local/apps/@appcenter/trim.music/static/assets/daily_cover.png"
        if os.path.exists(daily_custom):
            return FileResponse(daily_custom, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
        chosen = fallback_covers[cur_time_bucket % len(fallback_covers)]
        return RedirectResponse(chosen, status_code=302)

    if feats.is_board_guid(guid):
        b_tracks = await feats.fetch_board_tracks(guid)
        if b_tracks and b_tracks[0].get("cover_url"):
            real_cov = await _resolve_kuwo_real_cover(b_tracks[0]["cover_url"])
            return RedirectResponse(real_cov, status_code=302)

    if feats.is_online_playlist_guid(guid):
        pl_detail = await feats.fetch_online_playlist_detail(guid)
        if pl_detail and pl_detail.get("cover_url"):
            real_cov = await _resolve_kuwo_real_cover(pl_detail["cover_url"])
            return RedirectResponse(real_cov, status_code=302)
        elif pl_detail and pl_detail.get("tracks") and pl_detail["tracks"][0].get("cover_url"):
            real_cov = await _resolve_kuwo_real_cover(pl_detail["tracks"][0]["cover_url"])
            return RedirectResponse(real_cov, status_code=302)

    if feats.is_custom_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        user_pls = feats.load_user_custom_playlists(user_guid if is_authed else "shared")
        for pl in user_pls:
            if pl.get("guid") == guid and pl.get("cover_url"):
                real_cov = await _resolve_kuwo_real_cover(pl["cover_url"])
                return RedirectResponse(real_cov, status_code=302)

    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid)
    cover = (data or {}).get("cover_url") or ""
    sub_src, sub_id = "", ""
    try:
        sid = song_id_from_online_guid(guid)
        parts = sid.split(":")
        sub_src = parts[1] if len(parts) >= 2 else ""
        sub_id = parts[-1]
    except Exception:
        pass

    if not cover:
        # 1. 直接按 guid 模式快速构造音源 CDN 地址
        try:
            if sub_src == "kw" and sub_id.isdigit():
                cover = f"http://artistpicserver.kuwo.cn/pic.web?type=rid_pic&pictype=url&size=500&rid={sub_id}"
            elif sub_src == "wy" and sub_id.isdigit():
                cover = f"https://p2.music.126.net/6y-YsC17nVU90pgnGvgvMQ==/{sub_id}.jpg"
        except Exception:
            pass

    # 2. 尝试从本地已启用的落雪自定义源 Node 沙箱解析封面
    if not cover and sub_id:
        try:
            title = str((data or {}).get("title") or "")
            artist = str((data or {}).get("artist") or "")
            if not title:
                title, artist = _find_track_title_artist_from_cache(guid)
            song_info = {
                "id": sub_id,
                "name": title,
                "singer": artist,
                "source": sub_src or "kw",
            }
            lx_cover = await native_engine.resolve_music_pic_native(song_info)
            if lx_cover and lx_cover.startswith("http"):
                cover = lx_cover
        except Exception as e_lx:
            logger.debug("lx pic resolve error for %s: %s", guid, e_lx)

    # 3. 自动使用网易云千万级官方高清曲库跨源补全
    if not cover and netease_api.load_netease_config().get("auto_enrich", True):
        try:
            title = str((data or {}).get("title") or "")
            artist = str((data or {}).get("artist") or "")
            if not title:
                title, artist = _find_track_title_artist_from_cache(guid)
            if title:
                en = await netease_api.netease_client.enrich_cover_and_lyric(title, artist)
                if en.get("ok") and en.get("pic_url"):
                    cover = en["pic_url"]
        except Exception as e:
            logger.debug("netease cover enrich failed for %s: %s", guid, e)

    # 4. 酷我 URL 自动解析为真实的 JPEG 二进制地址，彻底杜绝返回纯文本导致裂图
    if cover:
        real_cover = await _resolve_kuwo_real_cover(cover)
        return RedirectResponse(real_cover, status_code=302)

    # 5. 兜底返回高质感沉浸式音乐艺术封面，杜绝客户端裂图或灰占位
    chosen = fallback_covers[cur_time_bucket % len(fallback_covers)]
    return RedirectResponse(chosen, status_code=302)


# === online favorites ===

_FAV_LOCK = asyncio.Lock()


def sanitize_user_guid(guid: str | None) -> str:
    """过滤文件名合法字符 [A-Za-z0-9-_]，非法字符替换为 _；为空则返回 'shared'。"""
    raw = str(guid or "").strip()
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", raw)
    return safe or "shared"


def user_fav_path(user_guid: str) -> str:
    fav_dir = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
    safe_name = sanitize_user_guid(user_guid)
    return os.path.join(fav_dir, f"{safe_name}.json")


def load_online_favorites(user_guid: str) -> list[dict]:
    path = user_fav_path(user_guid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning("Failed to load online favorites for %s from %s: %s", user_guid, path, e)
    return []


def save_online_favorites(user_guid: str, items: list[dict]) -> bool:
    path = user_fav_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save online favorites for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def build_favorite_track_obj(guid: str, info: dict | None = None, created_at: int | None = None) -> dict:
    raw_info = dict(info or {})
    raw_info.setdefault("id", song_id_from_online_guid(guid))
    raw_info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(raw_info)

    now = int(time.time())
    ts = created_at or now

    artist_name = vo.get("artist") or ""
    artists_list = [
        {
            "guid": f"{guid}:artist",
            "name": artist_name,
            "coverId": guid,
            "createdAt": ts,
            "updatedAt": ts,
        }
    ] if artist_name else []

    album_name = vo.get("albumName") or (vo.get("album", {}).get("name") if isinstance(vo.get("album"), dict) else "") or ""
    album_obj = {
        "guid": f"{guid}:album",
        "name": album_name,
        "artists": artists_list,
        "coverId": guid,
        "releaseDate": 0,
        "barcode": "",
        "createdAt": ts,
        "updatedAt": ts,
    }

    audio_spec = vo.get("audioSpec") or {}

    return {
        "guid": guid,
        "title": vo.get("title") or "",
        "duration": vo.get("duration") or 0,
        "isFavorite": True,
        "isCue": False,
        "genres": [],
        "artists": artists_list,
        "album": album_obj,
        "audioSpec": audio_spec,
        "accessStatus": 0,
        "coverId": guid,
        "year": 0,
        "discNo": 1,
        "trackNo": 1,
        "isrc": "",
        "createdAt": ts,
        "updatedAt": ts,
    }


async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:
    """向上游探测用户是否已登录。复用当前请求 headers。
    返回 (is_authed, user_guid, error_response)。
    """
    headers = copy_incoming_headers(request)
    try:
        probe_req = client.build_request("GET", "/music/api/v1/user/me", headers=headers)
        probe_resp = await client.send(probe_req)
        resp_headers = filter_headers(probe_resp.headers, exclude_keys={"content-length", "content-encoding"})

        if probe_resp.status_code == 401:
            return False, "", Response(
                content=probe_resp.content,
                status_code=401,
                headers=resp_headers,
                media_type=probe_resp.headers.get("content-type"),
            )

        if probe_resp.status_code == 200:
            try:
                probe_json = probe_resp.json()
                if isinstance(probe_json, dict) and probe_json.get("code") == 99999:
                    return False, "", JSONResponse(
                        content=probe_json,
                        status_code=200,
                        headers=resp_headers,
                    )
                if isinstance(probe_json, dict) and probe_json.get("code") == 0:
                    data = probe_json.get("data")
                    if isinstance(data, dict) and data.get("guid"):
                        return True, str(data["guid"]), None
                    logger.warning("user/me response missing data.guid, falling back to 'shared': %s", probe_json)
                    return True, "shared", None
            except Exception as e:
                logger.warning("Failed to parse user/me json response: %s", e)
                return True, "shared", None
            return True, "shared", None

        # 其他非 200/401 状态码，上游异常
        return True, "shared", None
    except Exception as e:
        logger.warning("Upstream auth probe failed: %s", e)
        # 探测异常时保守放行
        return True, "shared", None


@app.post("/music/api/v1/favorite-track/create")
async def favorite_track_create(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    now = int(time.time())
    info = await _online_info(request, guid)
    if not info:
        cached_lyric = read_lyric_cache(guid)
        title = ""
        artist = ""
        cached_media = find_cache_file(guid)
        if cached_media:
            base = os.path.splitext(os.path.basename(cached_media))[0]
            if " - " in base:
                artist, title = base.split(" - ", 1)
            else:
                title = base
        info = {
            "id": song_id_from_online_guid(guid),
            "source": source_from_online_guid(guid),
            "title": title,
            "artist": artist,
            "lyric": cached_lyric,
        }

    track_obj = build_favorite_track_obj(guid, info, created_at=now)

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            # 查重
            idx = next((i for i, it in enumerate(items) if it.get("guid") == guid), None)
            if idx is not None:
                # 幂等更新
                items[idx]["track"] = track_obj
            else:
                items.append({
                    "guid": guid,
                    "createdAt": now,
                    "track": track_obj,
                })
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/favorite-track/delete")
async def favorite_track_delete(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.get("/music/api/v1/favorite-track/list")
async def favorite_track_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    # 探测当前用户身份
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    # 成功获取官方列表，合并本地在线收藏
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        upstream_json["data"] = data

    official_list = data.get("list")
    if not isinstance(official_list, list):
        official_list = []
        data["list"] = official_list

    # 飞牛音乐前端收藏列表依赖 isFavorite=True 状态判断，遍历补齐官方列表中可能缺失的字段
    for item in official_list:
        if isinstance(item, dict):
            item["isFavorite"] = True

    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official_list)

    async with _FAV_LOCK:
        try:
            fav_items = load_online_favorites(user_guid)
        except Exception as e:
            logger.warning("Error reading online favorites for list for user %s: %s", user_guid, e)
            fav_items = []

    # 按 createdAt 倒序
    fav_items_sorted = sorted(fav_items, key=lambda x: x.get("createdAt", 0), reverse=True)
    online_tracks = []
    for it in fav_items_sorted:
        t = it.get("track")
        if isinstance(t, dict):
            # 确保关键属性为最新或格式完整
            t["isFavorite"] = True
            online_tracks.append(t)
        else:
            g = it.get("guid") or ""
            if g:
                online_tracks.append(build_favorite_track_obj(g, created_at=it.get("createdAt")))

    data["list"] = official_list + online_tracks
    data["total"] = official_total + len(online_tracks)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


# === daily recommend + play history ===

_HISTORY_LOCK = asyncio.Lock()
_DAILY_TASKS: dict[str, asyncio.Task] = {}


def _prune_stale_daily_tasks(day: str) -> None:
    suffix = f":{day}"
    stale = [k for k in list(_DAILY_TASKS) if not str(k).endswith(suffix)]
    for k in stale:
        old = _DAILY_TASKS.pop(k, None)
        if old is not None and not old.done():
            old.cancel()


async def _ensure_daily_task(request: Request, user_guid: str) -> asyncio.Task:
    day = dailyrec.today_key()
    _prune_stale_daily_tasks(day)
    key = f"{user_guid}:{day}"
    task = _DAILY_TASKS.get(key)
    if task is not None and not task.done():
        return task
    if task is not None and task.done():
        try:
            if task.exception() is None:
                result = task.result()
                if isinstance(result, dict) and len(result.get("tracks") or []) >= dailyrec.PLAYLIST_SIZE:
                    return task
        except (asyncio.CancelledError, Exception):
            pass
    async with _FAV_LOCK:
        favs = load_online_favorites(user_guid)
    task = asyncio.create_task(
        dailyrec.get_or_build_daily(
            user_guid=user_guid,
            musicdl_client=get_musicdl_client(request.app) if CONF.get("musicdl_enabled", True) else None,
            musicbox_client=get_musicbox_client(request.app) if CONF["netease_enabled"] else None,
            llm_http=get_llm_client(request.app) if dailyrec.llm_enabled() else None,
            build_track=build_online_track,
            netease_enabled=CONF["netease_enabled"],
            favorite_items=favs,
            lx_client=get_lx_client(request.app) if CONF.get("lx_enabled", True) else None,
            lx_enabled=bool(CONF.get("lx_enabled", True)),
        )
    )
    _DAILY_TASKS[key] = task
    return task


async def _peek_daily_bundle(request: Request, user_guid: str) -> dict:
    """歌单列表用：有缓存立刻返回；否则后台生成，最多等 2s，超时仍返回占位歌单。"""
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached
    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)
    except Exception as e:
        logger.warning("daily recommend peek failed: %s", e)
        return dailyrec.empty_daily_bundle(user_guid)


async def _load_daily_bundle(request: Request, user_guid: str) -> dict:
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached

    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)


def _playlist_public_fields(record: dict) -> dict:
    g = record.get("guid") or ""
    cid = record.get("coverId") or g
    return {
        "guid": g,
        "name": record.get("name") or "每日推荐",
        "coverId": cid,
        "coverUrl": f"/music/api/v1/static/cover?coverId={cid}" if cid else "",
        "createdAt": int(record.get("createdAt") or time.time()),
        "updatedAt": int(record.get("updatedAt") or time.time()),
        "trackCount": int(record.get("trackCount") or 0),
        "isDaily": True,
    }


@app.get("/music/api/v1/playlist/list")
@app.get("/music/api/v1/playlist/list/{subpath:path}")
async def playlist_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    injected: list[dict] = []
    try:
        bundle = await _peek_daily_bundle(request, user_guid)
        rec = _playlist_public_fields(bundle.get("playlist") or {})
        rec["trackCount"] = len(bundle.get("tracks") or [])
        injected.append(rec)
    except Exception as e:
        logger.warning("daily recommend list inject failed: %s", e)

    # 注入「我的心动歌曲」专属歌单
    try:
        ai_cfg = ai_music.load_ai_config()
        ai_cached = {}
        if os.path.exists(ai_music.AI_REC_CACHE_FILE):
            try:
                with open(ai_music.AI_REC_CACHE_FILE, "r", encoding="utf-8") as f:
                    ai_cached = json.load(f)
            except Exception:
                pass
        ai_tracks = ai_cached.get("tracks") or []
        injected.append({
            "guid": "ai:heartbeat:recommend",
            "name": "💖 我的心动歌曲",
            "desc": ai_cached.get("summary") or "AI 根据您的听歌偏好与常播歌曲，为您定制的心动推荐",
            "coverId": "ai:heartbeat:recommend",
            "cover_url": "",
            "trackCount": len(ai_tracks) if ai_tracks else 15,
            "isSystem": True,
            "createdAt": 1700000000,
            "updatedAt": int(ai_cached.get("timestamp") or time.time()),
        })
    except Exception as e:
        logger.warning("ai heartbeat recommend inject failed: %s", e)

    # 注入排行榜、下载管理与自定义导入歌单
    try:
        # 注入下载管理专属歌单
        dl_records = download_mgr.list_download_records("all")
        injected.append({
            "guid": "local:downloads",
            "name": "下载管理",
            "desc": f"在飞牛音乐中下载的音乐 · 共 {len(dl_records)} 首",
            "coverId": "local:downloads",
            "cover_url": "",
            "trackCount": len(dl_records),
            "isSystem": True,
            "createdAt": 1700000000,
            "updatedAt": int(time.time()),
        })

        cfg = feats.load_settings()
        if cfg.get("enable_leaderboards", True):
            for b in cfg.get("boards", feats.DEFAULT_BOARDS):
                if b.get("enabled"):
                    injected.append({
                        "guid": b["id"],
                        "name": b["name"],
                        "desc": f"落雪音源 · {b['name']}",
                        "coverId": b["id"],
                        "cover_url": "",
                        "trackCount": b.get("count", 100),
                        "isSystem": True,
                        "createdAt": 1700000000,
                        "updatedAt": int(time.time()),
                    })
        user_pls = feats.load_user_custom_playlists(user_guid)
        for upl in user_pls:
            injected.append({
                "guid": upl.get("guid"),
                "name": upl.get("name"),
                "desc": upl.get("desc") or f"导入歌单 · 共 {upl.get('trackCount', 0)} 首",
                "coverId": upl.get("guid"),
                "cover_url": upl.get("cover_url", ""),
                "trackCount": upl.get("trackCount", 0),
                "isSystem": False,
                "createdAt": upl.get("createdAt", int(time.time())),
                "updatedAt": upl.get("updatedAt", int(time.time())),
            })
    except Exception as e:
        logger.warning("leaderboards/custom playlists inject failed: %s", e)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    # 过滤可能已经注入过的
    injected_guids = {str(it.get("guid") or "") for it in injected}
    official = [
        it for it in official
        if not (isinstance(it, dict) and (dailyrec.is_daily_playlist_guid(str(it.get("guid") or "")) or str(it.get("guid") or "") in injected_guids))
    ]
    data["list"] = injected + official
    data["total"] = len(data["list"])
    return JSONResponse(content=envelope, headers=headers)


@app.get("/music/api/v1/playlist/detail")
async def playlist_detail(request: Request):
    guid = str(request.query_params.get("guid") or "").strip()
    
    # 专属本地下载歌单
    if guid == "local:downloads":
        records = download_mgr.list_download_records("all")
        success_count = sum(1 for r in records if r.get("status") == "success")
        return JSONResponse(content={
            "code": 0, "msg": "ok",
            "data": {
                "guid": "local:downloads",
                "name": "下载管理",
                "desc": f"在飞牛音乐中下载的音乐 · 共 {len(records)} 条记录 (成功 {success_count} 首)",
                "coverId": "local:downloads",
                "coverUrl": "/music/api/v1/static/cover?coverId=local:downloads",
                "trackCount": len(records),
                "isSystem": True,
                "createdAt": 1700000000,
                "updatedAt": int(time.time()),
            }
        })

    # AI 心动推荐歌单详情
    if guid == "ai:heartbeat:recommend":
        ai_cached = {}
        if os.path.exists(ai_music.AI_REC_CACHE_FILE):
            try:
                with open(ai_music.AI_REC_CACHE_FILE, "r", encoding="utf-8") as f:
                    ai_cached = json.load(f)
            except Exception:
                pass
        ai_tracks = ai_cached.get("tracks") or []
        tags_str = " · ".join(ai_cached.get("taste_tags") or ["AI精选", "心动好歌"])
        return JSONResponse(content={
            "code": 0, "msg": "ok",
            "data": {
                "guid": "ai:heartbeat:recommend",
                "name": "💖 我的心动歌曲",
                "desc": f"【{tags_str}】{ai_cached.get('summary') or 'AI 智能分析常听曲目生成的同类型心动音乐'}",
                "coverId": "ai:heartbeat:recommend",
                "coverUrl": "/music/api/v1/static/cover?coverId=ai:heartbeat:recommend",
                "trackCount": len(ai_tracks) if ai_tracks else 15,
                "isSystem": True,
                "createdAt": 1700000000,
                "updatedAt": int(ai_cached.get("timestamp") or time.time()),
            }
        })

    if feats.is_board_guid(guid):
        cfg = feats.load_settings()
        b_name = "排行榜歌单"
        for b in cfg.get("boards", feats.DEFAULT_BOARDS):
            if b.get("id") == guid:
                b_name = b.get("name", b_name)
                break
        tracks = await feats.fetch_board_tracks(guid)
        return JSONResponse(content={
            "code": 0, "msg": "ok",
            "data": {
                "guid": guid, "name": b_name, "desc": f"落雪音源 · {b_name}",
                "coverId": guid, "trackCount": len(tracks), "createdAt": 1700000000, "updatedAt": int(time.time()),
            }
        })

    if feats.is_online_playlist_guid(guid):
        pl_detail = await feats.fetch_online_playlist_detail(guid)
        if pl_detail:
            return JSONResponse(content={
                "code": 0, "msg": "ok",
                "data": {
                    "guid": guid,
                    "name": pl_detail.get("name") or "在线精选歌单",
                    "desc": pl_detail.get("desc") or "热门在线歌单",
                    "coverId": guid,
                    "trackCount": pl_detail.get("trackCount") or len(pl_detail.get("tracks", [])),
                    "isSystem": True,
                    "createdAt": 1700000000,
                    "updatedAt": int(time.time()),
                }
            })
        return JSONResponse(content={"code": -1, "msg": "在线歌单获取失败"})

    if feats.is_custom_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        keys_to_try = [user_guid, "shared"] if is_authed else ["shared"]
        for u in keys_to_try:
            user_pls = feats.load_user_custom_playlists(u)
            for pl in user_pls:
                if pl.get("guid") == guid:
                    return JSONResponse(content={"code": 0, "msg": "ok", "data": pl})
        return JSONResponse(content={"code": -1, "msg": "歌单未找到"})

    if not dailyrec.is_daily_playlist_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    return JSONResponse(content={"code": 0, "msg": "ok", "data": rec})


@app.post("/music/api/v1/playlist/edit")
async def playlist_edit(request: Request):
    """处理歌单编辑（系统虚拟歌单、第三方导入歌单及官方本地歌单）"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    guid = str(body.get("guid") or "").strip()
    new_name = str(body.get("name") or "").strip()

    # 1. 导入的自定义歌单 (cplaylist:* / imported:*)
    if feats.is_custom_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        user_key = user_guid if is_authed else "shared"
        
        # 尝试更新用户目录下的歌单，若在 shared 下则更新 shared
        updated = False
        for u in [user_key, "shared"]:
            pls = feats.load_user_custom_playlists(u)
            for pl in pls:
                if pl.get("guid") == guid:
                    if new_name:
                        pl["name"] = new_name
                    if "desc" in body:
                        pl["desc"] = body["desc"]
                    pl["updatedAt"] = int(time.time())
                    feats.save_user_custom_playlist(u, pl)
                    updated = True
                    return JSONResponse(content={
                        "code": 0, "msg": "ok",
                        "data": {"guid": guid, "name": pl.get("name"), "desc": pl.get("desc")}
                    })
        return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid, "name": new_name}})

    # 2. 系统虚拟内置歌单 (如 local:downloads, board:*, online_pl:*)
    if guid == "local:downloads" or feats.is_board_guid(guid) or feats.is_online_playlist_guid(guid) or dailyrec.is_daily_playlist_guid(guid):
        return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid, "name": new_name or "歌单"}})

    # 3. 官方原生本地歌单转发给上游
    return await forward_to_upstream(request, get_upstream_client(request.app))


@app.post("/music/api/v1/playlist/delete")
async def playlist_delete_upstream(request: Request):
    """拦截系统虚拟歌单及自定义导入歌单的删除操作"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    guid = str(body.get("guid") or "").strip()

    # 1. 自定义导入歌单删除
    if feats.is_custom_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        user_key = user_guid if is_authed else "shared"
        for u in [user_key, "shared"]:
            feats.delete_user_custom_playlist(u, guid)
        return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})

    # 2. 系统内置歌单禁止删除
    if guid == "local:downloads" or feats.is_board_guid(guid) or feats.is_online_playlist_guid(guid) or dailyrec.is_daily_playlist_guid(guid):
        return JSONResponse(content={"code": -1, "msg": "系统内置歌单不支持删除"})

    # 3. 官方原生本地歌单
    return await forward_to_upstream(request, get_upstream_client(request.app))


@app.get("/music/api/v1/album/detail")
async def album_detail_guard(request: Request):
    """拦截专辑详情接口，当上游报错或为虚拟/外部专辑时优雅返回，防止前端弹出红叉报错"""
    guid = request.query_params.get("guid") or ""
    # 外部在线音源的虚拟专辑 (形如 online_...:album 或 lx:...:album) 直接返回模拟信息
    if ":album" in guid or guid.startswith("online_") or guid.startswith("lx:"):
        return JSONResponse(content={
            "code": 0,
            "msg": "ok",
            "data": {
                "guid": guid,
                "name": "在线专辑",
                "artists": [],
                "releaseDate": "",
                "totalTrack": 0,
                "coverGUID": ""
            }
        })
    try:
        resp = await forward_to_upstream(request, get_upstream_client(request.app))
        if resp.status_code == 200:
            try:
                data = json.loads(resp.body.decode("utf-8")) if hasattr(resp, "body") else {}
            except Exception:
                data = {}
            if data.get("code") == 0 and data.get("data"):
                return resp
    except Exception:
        pass
    return JSONResponse(content={
        "code": 0,
        "msg": "ok",
        "data": {
            "guid": guid,
            "name": "专辑",
            "artists": [],
            "releaseDate": "",
            "totalTrack": 0,
            "coverGUID": ""
        }
    })


@app.get("/music/api/v1/track/album-detail/list")
async def track_album_detail_list(request: Request):
    """拦截专辑歌曲列表请求，当专辑无歌曲或上游返回 404/错误时优雅降级返回空列表，防止前端弹出红叉报错"""
    guid = request.query_params.get("albumGUID") or request.query_params.get("guid") or ""
    if ":album" in guid or guid.startswith("online_") or guid.startswith("lx:"):
        return JSONResponse(content={
            "code": 0,
            "msg": "ok",
            "data": {
                "list": [],
                "total": 0,
                "page": 1,
                "pageSize": int(request.query_params.get("size") or 50),
                "sort": "trackNo,asc"
            }
        })
    try:
        resp = await forward_to_upstream(request, get_upstream_client(request.app))
        if resp.status_code == 200:
            return resp
    except Exception:
        pass
    # 优雅返回空歌曲列表，避免触发 routes.albums.messages.loadTracksFailed
    return JSONResponse(content={
        "code": 0,
        "msg": "ok",
        "data": {
            "list": [],
            "total": 0,
            "page": 1,
            "pageSize": int(request.query_params.get("size") or 50),
            "sort": "trackNo,asc"
        }
    })


@app.get("/music/api/v1/playlist/batch-detail")
async def playlist_batch_detail(request: Request):
    raw = request.query_params.get("guids") or request.query_params.get("guid") or ""
    guids = [g.strip() for g in raw.split(",") if g.strip()]
    special_ids = [g for g in guids if dailyrec.is_daily_playlist_guid(g) or feats.is_board_guid(g) or feats.is_custom_playlist_guid(g) or g == "local:downloads"]
    if not special_ids:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    rest = [g for g in guids if g not in special_ids]
    official_list: list = []
    if rest:
        headers = copy_incoming_headers(request)
        req = upstream_client.build_request(
            "GET",
            f"/music/api/v1/playlist/batch-detail?guids={quote(','.join(rest), safe=',')}",
            headers=headers,
        )
        resp = await upstream_client.send(req)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("code") == 0:
                    data = payload.get("data") or {}
                    if isinstance(data, dict) and isinstance(data.get("list"), list):
                        official_list = data["list"]
                    elif isinstance(data, list):
                        official_list = data
            except Exception:
                official_list = []

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    injected_details = []
    for g in special_ids:
        if dailyrec.is_daily_playlist_guid(g):
            bundle = await _load_daily_bundle(request, user_guid if is_authed else "")
            rec = _playlist_public_fields(bundle.get("playlist") or {})
            rec["trackCount"] = len(bundle.get("tracks") or [])
            injected_details.append(rec)
        elif feats.is_board_guid(g):
            tracks = await feats.fetch_board_tracks(g)
            injected_details.append({
                "guid": g, "name": "排行榜", "desc": "落雪音源榜单", "coverId": g, "trackCount": len(tracks),
            })
        elif g == "local:downloads":
            records = download_mgr.list_download_records("all")
            injected_details.append({
                "guid": "local:downloads",
                "name": "下载管理",
                "desc": f"在飞牛音乐中下载的音乐 · 共 {len(records)} 首",
                "coverId": "local:downloads",
                "trackCount": len(records),
            })
        elif feats.is_custom_playlist_guid(g):
            user_pls = feats.load_user_custom_playlists(user_guid if is_authed else "shared")
            for upl in user_pls:
                if upl.get("guid") == g:
                    injected_details.append(upl)
                    break

    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"list": injected_details + official_list}})


@app.get("/music/api/v1/track/playlist-detail/list")
async def playlist_track_list(request: Request):
    guid = str(
        request.query_params.get("playlistGUID")
        or request.query_params.get("playlistGuid")
        or request.query_params.get("guid")
        or ""
    ).strip()

    tracks: list[dict] = []
    if guid == "local:downloads":
        records = download_mgr.list_download_records("all")
        for r in records:
            status_tag = "【下载完成】" if r.get("status") == "success" else "【下载失败】"
            size_bytes = int((r.get("size_mb") or 0) * 1024 * 1024)
            rec_id = r.get('id')
            item_guid = f"download:{rec_id}" if rec_id else (r.get("guid") or "download:unknown")
            tracks.append({
                "guid": item_guid,
                "id": item_guid,
                "title": f"{status_tag} {r.get('title')}",
                "name": f"{status_tag} {r.get('title')}",
                "artist": r.get("artist") or "未知歌手",
                "artists": [{"name": r.get("artist") or "未知歌手", "guid": "online:artist:unknown"}],
                "album": {"name": f"大小: {r.get('size_mb', 0)}MB | {r.get('status')}", "guid": "download:album"},
                "duration": 200000,
                "coverId": r.get("guid") or "local:downloads",
                "createdAt": 1700000000,
                "updatedAt": 1700000000,
                "isCue": False,
                "audioSpec": {
                    "extension": r.get("ext") or "flac",
                    "format": (r.get("ext") or "flac").upper(),
                    "size": size_bytes,
                    "bitrate": 960000,
                    "channels": 2,
                    "sampleRate": 44100,
                    "bitDepth": 16,
                    "codec": "flac",
                    "path": r.get("file_path") or "",
                }
            })
    elif feats.is_board_guid(guid):
        b_tracks = await feats.fetch_board_tracks(guid)
        tracks = [build_online_track(t) for t in b_tracks]
    elif feats.is_online_playlist_guid(guid):
        pl_detail = await feats.fetch_online_playlist_detail(guid)
        if pl_detail and pl_detail.get("tracks"):
            tracks = [build_online_track(t) for t in pl_detail["tracks"]]
    elif feats.is_custom_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        keys_to_try = [user_guid, "shared"] if is_authed else ["shared"]
        found = False
        for u in keys_to_try:
            user_pls = feats.load_user_custom_playlists(u)
            for pl in user_pls:
                if pl.get("guid") == guid:
                    tracks = [build_online_track(t) for t in pl.get("tracks", [])]
                    found = True
                    break
            if found:
                break
    elif guid == "ai:heartbeat:recommend":
        # 解析并加载 AI 心动推荐歌单中的曲目
        ai_res = await ai_music.generate_ai_recommendations(force_refresh=False)
        raw_tracks = ai_res.get("tracks") or []
        tracks = []
        for t in raw_tracks:
            g = t.get("guid") or ""
            if is_online_guid(g):
                tracks.append(build_online_track(t))
            else:
                # 本地曲目结构对齐
                tracks.append({
                    "guid": g,
                    "name": t.get("title") or "",
                    "title": t.get("title") or "",
                    "artists": [{"name": t.get("artist") or "未知歌手"}],
                    "album": {"name": t.get("album") or ""},
                    "duration": 240,
                    "coverId": t.get("coverId") or g,
                    "is_local": True,
                })
    elif dailyrec.is_daily_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        bundle = await _load_daily_bundle(request, user_guid)
        tracks = dailyrec.stamp_playlist_tracks(list(bundle.get("tracks") or []))
    else:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50
    start = (page - 1) * size
    page_tracks = tracks[start:start + size] if size != -1 else tracks
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
        }
    )


@app.post("/music/api/v1/event/report")
async def event_report(request: Request):
    upstream_client = get_upstream_client(request.app)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except Exception:
        body = {}
    events = body.get("events") if isinstance(body, dict) else None
    online_plays: list[str] = []
    other_events: list = []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("eventType") or ev.get("type") or "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            guid = str(payload.get("trackGUID") or payload.get("guid") or "")
            if et in ("track_play", "TrackPlay") and is_online_guid(guid):
                online_plays.append(guid)
            else:
                other_events.append(ev)
    else:
        return await forward_to_upstream(request, upstream_client)

    if online_plays:
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if is_authed:
            async with _HISTORY_LOCK:
                for guid in online_plays:
                    info = stub_online_info(guid)
                    cached = find_cache_file(guid)
                    title = str(info.get("title") or "")
                    artist = str(info.get("artist") or "")
                    if cached:
                        base = os.path.splitext(os.path.basename(cached))[0]
                        if " - " in base:
                            artist, title = base.split(" - ", 1)
                        elif not title:
                            title = base
                    dailyrec.record_online_play(
                        user_guid,
                        guid,
                        {"guid": guid, "title": title, "artist": artist, "source": source_from_online_guid(guid)},
                    )
        elif auth_resp is not None and not other_events:
            return auth_resp

    if other_events:
        headers = copy_incoming_headers(request)
        fwd = dict(body)
        fwd["events"] = other_events
        req = upstream_client.build_request(
            "POST",
            "/music/api/v1/event/report",
            headers=headers,
            content=json.dumps(fwd).encode("utf-8"),
        )
        resp = await upstream_client.send(req)
        resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    return JSONResponse(content={"code": 0, "msg": "ok", "data": None})


@app.get("/music/api/v1/play-history/list")
async def play_history_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    async with _HISTORY_LOCK:
        online_items = dailyrec.load_online_play_history(user_guid)
    online_tracks = []
    for it in reversed(online_items):
        guid = str(it.get("guid") or "")
        if not guid:
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        obj = build_favorite_track_obj(guid, track, created_at=int(it.get("playedAt") or time.time()))
        obj["isFavorite"] = False
        online_tracks.append(obj)

    seen = {str(x.get("guid")) for x in official if isinstance(x, dict)}
    merged_online = [t for t in online_tracks if t.get("guid") not in seen]
    data["list"] = merged_online + official
    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official)
    data["total"] = official_total + len(merged_online)
    return JSONResponse(content=envelope, headers=headers)


# =====================================================================
# 增强自定义功能路由：扩展管理页面、歌单导入/导出、排行榜配置、下载/缓存
# =====================================================================

@app.get("/music/ext", response_class=Response)
@app.get("/music/ext/", response_class=Response)
@app.get("/music/ext/settings", response_class=Response)
async def ext_settings_page(request: Request):
    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
    stats = feats.get_cache_stats(CONF["cache_dir"])
    settings = feats.load_settings()
    playlists = feats.load_user_custom_playlists(user_guid if is_authed else "shared")
    
    # 飞牛音乐原生媒体库与红心收藏深度联动
    fn_stats = feats.get_fnmusic_stats(user_guid if is_authed else None)
    fav_count = fn_stats.get("favorite_count", 0)
    if is_authed:
        try:
            favs = load_online_favorites(user_guid)
            if len(favs) > 0:
                fav_count = max(fav_count, len(favs))
        except Exception:
            pass
    html = feats.render_console_html(stats, settings, playlists, fav_count, fn_stats=fn_stats)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.get("/music/ext/api/settings")
async def ext_api_get_settings():
    return JSONResponse(content={"code": 0, "msg": "ok", "data": feats.load_settings()})


@app.post("/music/ext/api/settings/boards")
async def ext_api_save_boards(request: Request):
    try:
        body = await request.json()
        boards_in = body.get("boards") or []
        cfg = feats.load_settings()
        cur_boards = cfg.get("boards") or feats.DEFAULT_BOARDS
        id_map = {b["id"]: b.get("enabled", False) for b in boards_in}
        for b in cur_boards:
            if b["id"] in id_map:
                b["enabled"] = bool(id_map[b["id"]])
        cfg["boards"] = cur_boards
        feats.save_settings(cfg)
        return JSONResponse(content={"code": 0, "msg": "排行榜配置保存成功！"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"保存失败: {e}"})


@app.post("/music/ext/api/settings/basic")
async def ext_api_save_basic(request: Request):
    try:
        body = await request.json()
        cfg = feats.load_settings()
        if "download_dir" in body:
            cfg["download_dir"] = str(body["download_dir"]).strip()
        if "preferred_quality" in body:
            cfg["preferred_quality"] = str(body["preferred_quality"]).strip()
        if "max_cache_mb" in body:
            try:
                cfg["max_cache_mb"] = max(100, int(body["max_cache_mb"]))
            except (ValueError, TypeError):
                pass
        feats.save_settings(cfg)

        # 检查当前缓存是否超限并自动修剪
        cache_dir = CONF.get("cache_dir")
        pruned_cnt = 0
        if cache_dir and os.path.exists(cache_dir):
            pruned_cnt = feats.prune_cache_if_needed(cache_dir, cfg.get("max_cache_mb", 5120))

        msg = "常规设置与缓存容量上限保存成功！"
        if pruned_cnt > 0:
            msg += f" (已自动清理超限的 {pruned_cnt} 个旧缓存文件)"
        return JSONResponse(content={"code": 0, "msg": msg})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"保存失败: {e}"})


@app.post("/music/ext/api/settings/appearance")
async def ext_api_save_appearance(request: Request):
    try:
        body = await request.json()
        cfg = feats.load_settings()
        for k in ["theme_mode", "accent_color", "show_quality_badge", "enable_smooth_scroll", "custom_css"]:
            if k in body:
                cfg[k] = body[k]
        feats.save_settings(cfg)
        return JSONResponse(content={"code": 0, "msg": "显示与外观设置保存成功！"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"保存失败: {e}"})


@app.post("/music/ext/api/settings/logic")
async def ext_api_save_logic(request: Request):
    try:
        body = await request.json()
        cfg = feats.load_settings()
        for k in ["preferred_quality", "lx_server_url", "lx_service_url"]:
            if k in body:
                cfg[k] = body[k]
        feats.save_settings(cfg)
        return JSONResponse(content={"code": 0, "msg": "播放与调度逻辑保存成功！"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"保存失败: {e}"})


@app.get("/music/ext/api/logs")
async def ext_api_get_logs(lines: int = 100):
    try:
        log_text = feats.get_recent_logs(lines)
        return JSONResponse(content={"code": 0, "msg": "ok", "data": log_text})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"获取日志失败: {e}"})


@app.get("/music/ext/api/playlist/list")
async def ext_api_playlist_list(request: Request):
    """获取所有已导入的自定义歌单列表 (供后台管理前端实时刷新渲染)"""
    try:
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        user_key = user_guid if is_authed else "shared"
        pls = feats.load_user_custom_playlists(user_key)
        return JSONResponse(content={"code": 0, "msg": "ok", "data": pls})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"获取歌单失败: {e}", "data": []})


@app.post("/music/ext/api/playlist/import")
async def ext_api_import_playlist(request: Request):
    try:
        body = await request.json()
        url = str(body.get("url") or "").strip()
        name = str(body.get("name") or "").strip()
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        res = await feats.import_external_playlist(
            url,
            user_guid=user_guid if is_authed else "shared",
            custom_name=name,
        )
        return JSONResponse(content={"code": 0 if res.get("ok") else -1, "msg": res.get("msg"), "data": res.get("data")})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"导入失败: {e}"})


@app.post("/music/ext/api/playlist/delete")
async def ext_api_delete_playlist(request: Request):
    try:
        body = await request.json()
        guid = str(body.get("guid") or "").strip()
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        ok = feats.delete_user_custom_playlist(user_guid if is_authed else "shared", guid)
        return JSONResponse(content={"code": 0 if ok else -1, "msg": "删除成功" if ok else "删除失败或歌单不存在"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"操作异常: {e}"})


@app.post("/music/ext/api/song/delete")
async def ext_api_delete_song(request: Request):
    try:
        body = await request.json()
        guid = str(body.get("guid") or "").strip()
        title = str(body.get("title") or "").strip()
        delete_file = bool(body.get("delete_file", True))
        
        ok = download_mgr.delete_download_by_guid_or_title(guid, title=title, delete_file=delete_file)
        if ok:
            return JSONResponse(content={"code": 0, "msg": "歌曲及下载记录已删除"})
        return JSONResponse(content={"code": -1, "msg": "未找到相关下载记录或删除失败"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"删除异常: {e}"})


@app.get("/music/ext/api/online_playlists/list")
async def ext_api_online_playlists_list(request: Request):
    """获取各平台热门精选歌单列表"""
    source = str(request.query_params.get("source") or "wy").strip()
    tag_id = str(request.query_params.get("tag_id") or "").strip()
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except Exception:
        page = 1
    
    cfg = feats.load_settings()
    lx_server = cfg.get("lx_server_url") or "http://127.0.0.1:9528"
    items = await feats.fetch_online_playlists_list(source=source, page=page, tag_id=tag_id, lx_server_url=lx_server)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"list": items, "page": page, "source": source}})


@app.post("/music/ext/api/song/download")
async def ext_api_download_song(request: Request):
    try:
        body = await request.json()
        guid = str(body.get("guid") or "").strip()
        title = str(body.get("title") or "").strip()
        artist = str(body.get("artist") or "").strip()

        # 如果没有传有效 online guid，但传了 title，尝试在线搜索匹配真实音源 (脱离 8772 原生直连)
        if (not is_online_guid(guid)) and title:
            matched = await feats.search_best_online_song(title, artist)
            if matched and matched.get("guid"):
                guid = matched["guid"]

        if not is_online_guid(guid):
            return JSONResponse(content={"code": -1, "msg": "未找到对应的在线音源歌曲，无法下载"})

        cfg = feats.load_settings()
        t_dir = cfg.get("download_dir") or detect_library_dir()
        track_meta = {"title": title, "artist": artist} if title else None
        
        # 写入下载开始记录
        rec_id = download_mgr.record_download(guid=guid, title=title or "未知曲目", artist=artist, status="downloading")
        
        res = await feats.download_online_song(guid, target_dir=t_dir, track_info=track_meta)
        
        # 更新下载结果状态
        if res.get("ok"):
            download_mgr.update_download_status(
                record_id=rec_id,
                status="success",
                ext="flac",
                size_mb=res.get("size_mb") or 0,
                file_path=res.get("path") or "",
            )
            # 结合飞牛音乐：下载成功后自动触发飞牛音乐全库重新扫描
            if cfg.get("auto_scan_on_download", True):
                try:
                    upstream_client = get_upstream_client(request.app)
                    scan_req = upstream_client.build_request("POST", "/music/api/v1/shared-library/scan-all", headers=copy_incoming_headers(request))
                    await upstream_client.send(scan_req)
                    logger.info("Auto scan triggered for trim.music after download of %s", title)
                except Exception as ex:
                    logger.warning("Auto scan after download error: %s", ex)
        else:
            download_mgr.update_download_status(
                record_id=rec_id,
                status="failed",
                error_msg=res.get("msg") or "下载失败",
            )

        return JSONResponse(content={"code": 0 if res.get("ok") else -1, "msg": res.get("msg"), "data": res})
    except Exception as e:
        if 'rec_id' in locals() and rec_id:
            download_mgr.update_download_status(record_id=rec_id, status="failed", error_msg=str(e))
        return JSONResponse(content={"code": -1, "msg": f"下载失败: {e}"})


@app.post("/music/ext/api/cache/clear")
async def ext_api_clear_cache():
    try:
        cnt = feats.clear_cache_files(CONF["cache_dir"])
        return JSONResponse(content={"code": 0, "msg": f"已清理 {cnt} 个缓存文件"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"清理失败: {e}"})


@app.post("/music/ext/api/library/scan")
async def ext_api_trigger_scan(request: Request):
    try:
        upstream_client = get_upstream_client(request.app)
        headers = copy_incoming_headers(request)
        # 确保附带飞牛官方有效 token
        cur_cookies = headers.get("cookie", "")
        if "music-token=" not in cur_cookies:
            try:
                db_p = "/usr/local/apps/@appdata/trim.music/db/music.db"
                if os.path.exists(db_p):
                    con = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True, timeout=2.0)
                    cur = con.cursor()
                    cur.execute("SELECT token FROM user_token ORDER BY id DESC LIMIT 1")
                    r_tok = cur.fetchone()
                    con.close()
                    if r_tok and r_tok[0]:
                        headers["cookie"] = f"music-token={r_tok[0]}; {cur_cookies}".strip("; ")
            except Exception:
                pass
        req = upstream_client.build_request("POST", "/music/api/v1/shared-library/scan-all", headers=headers)
        resp = await upstream_client.send(req)
        return JSONResponse(content={"code": 0, "msg": "已向飞牛原生后端发送全量媒体库重新扫描指令！"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"触发失败: {e}"})


# =====================================================================
# 飞牛音乐原生自定义音源 (Native Custom Source) 管理接口 (彻底不经 9528)
# =====================================================================

@app.get("/music/ext/api/custom_source/list")
async def ext_api_custom_source_list(request: Request):
    try:
        sources = native_engine.load_sources_meta()
        return JSONResponse(content={"code": 0, "msg": "ok", "data": sources})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"获取音源列表失败: {e}"})


@app.post("/music/ext/api/custom_source/toggle")
async def ext_api_custom_source_toggle(request: Request):
    try:
        body = await request.json()
        sid = str(body.get("id") or "").strip()
        enabled = bool(body.get("enabled", True))
        if not sid:
            return JSONResponse(content={"code": -1, "msg": "缺少音源 id"})
        sources = native_engine.load_sources_meta()
        updated = False
        for s in sources:
            if s.get("id") == sid:
                s["enabled"] = enabled
                updated = True
        if updated:
            native_engine.save_sources_meta(sources)
            return JSONResponse(content={"code": 0, "msg": "音源状态已更新"})
        return JSONResponse(content={"code": -1, "msg": "未找到指定音源"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"操作异常: {e}"})


@app.post("/music/ext/api/custom_source/delete")
async def ext_api_custom_source_delete(request: Request):
    try:
        body = await request.json()
        sid = str(body.get("id") or "").strip()
        if not sid:
            return JSONResponse(content={"code": -1, "msg": "缺少音源 id"})
        sources = native_engine.load_sources_meta()
        sources = [s for s in sources if s.get("id") != sid]
        native_engine.save_sources_meta(sources)
        fpath = os.path.join(native_engine.CUSTOM_SOURCES_DIR, sid)
        if os.path.isfile(fpath):
            os.remove(fpath)
        return JSONResponse(content={"code": 0, "msg": "音源已成功移除"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"删除异常: {e}"})


@app.post("/music/ext/api/custom_source/import")
async def ext_api_custom_source_import(request: Request):
    try:
        body = await request.json()
        url = str(body.get("url") or "").strip()
        if not url:
            return JSONResponse(content={"code": -1, "msg": "请输入音源脚本 URL"})
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return JSONResponse(content={"code": -1, "msg": f"下载脚本失败: HTTP {r.status_code}"})
            content = r.text
            fname = os.path.basename(url.split("?")[0]) or f"source_{int(time.time())}.js"
            if not fname.endswith(".js"):
                fname += ".js"
            fpath = os.path.join(native_engine.CUSTOM_SOURCES_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            meta = native_engine.parse_script_header(fpath)
            meta["enabled"] = True
            sources = native_engine.load_sources_meta()
            sources = [s for s in sources if s.get("id") != fname]
            sources.append(meta)
            native_engine.save_sources_meta(sources)
            return JSONResponse(content={"code": 0, "msg": f"音源 [{meta.get('name')}] 导入并启用成功！", "data": meta})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"导入异常: {e}"})


@app.post("/music/ext/api/custom_source/upload")
async def ext_api_custom_source_upload(request: Request):
    try:
        body = await request.json()
        name = str(body.get("name") or "").strip() or f"custom_{int(time.time())}.js"
        content = str(body.get("content") or "").strip()
        if not content:
            return JSONResponse(content={"code": -1, "msg": "脚本内容不能为空"})
        if not name.endswith(".js"):
            name += ".js"
        fpath = os.path.join(native_engine.CUSTOM_SOURCES_DIR, name)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        meta = native_engine.parse_script_header(fpath)
        meta["enabled"] = True
        sources = native_engine.load_sources_meta()
        sources = [s for s in sources if s.get("id") != name]
        sources.append(meta)
        native_engine.save_sources_meta(sources)
        return JSONResponse(content={"code": 0, "msg": f"音源 [{meta.get('name')}] 保存成功！", "data": meta})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"上传异常: {e}"})


@app.post("/music/ext/api/custom_source/reload")
async def ext_api_custom_source_reload():
    try:
        sources = native_engine.load_sources_meta()
        return JSONResponse(content={"code": 0, "msg": f"已重新加载 {len(sources)} 个内置音源！"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"重启服务失败: {e}"})


# =====================================================================
# AI 音乐助手 API
# =====================================================================

@app.get("/music/ext/api/ai/config")
async def ext_api_get_ai_config():
    """获取 AI 助手配置（脱敏 api_key）"""
    cfg = ai_music.load_ai_config()
    safe_cfg = dict(cfg)
    safe_cfg["api_key_masked"] = ai_music.mask_key(cfg.get("api_key", ""))
    safe_cfg.pop("api_key", None)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": safe_cfg})


@app.post("/music/ext/api/ai/config")
async def ext_api_save_ai_config(request: Request):
    """保存 AI 助手配置"""
    try:
        body = await request.json()
        ok = ai_music.save_ai_config(body)
        if ok:
            return JSONResponse(content={"code": 0, "msg": "AI 音乐助手配置保存成功！"})
        return JSONResponse(content={"code": -1, "msg": "保存失败"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"保存异常: {e}"})


@app.post("/music/ext/api/ai/test")
async def ext_api_test_ai(request: Request):
    """测试模型连通性"""
    try:
        body = await request.json() if (request.headers.get("content-length") or "0") != "0" else {}
        res = await ai_music.test_ai_connection(body)
        return JSONResponse(content={"code": 0 if res.get("ok") else -1, "msg": res.get("msg"), "data": res})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"测试异常: {e}"})


@app.get("/music/ext/api/ai/recommend")
@app.post("/music/ext/api/ai/recommend/generate")
async def ext_api_get_ai_recommend(refresh: bool = False):
    """获取或生成 AI 心动音乐推荐列表"""
    try:
        res = await ai_music.generate_ai_recommendations(force_refresh=refresh)
        return JSONResponse(content={"code": 0 if res.get("ok") else -1, "msg": res.get("msg"), "data": res})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"生成推荐失败: {e}"})


# ===== 网易云音乐 (其他音源) 专属 API 接口 =====
@app.get("/music/ext/api/netease/config")
async def ext_api_get_netease_config():
    """获取网易云音源与会员配置"""
    cfg = netease_api.load_netease_config()
    safe_cfg = dict(cfg)
    cookie = str(safe_cfg.get("cookie") or "").strip()
    if cookie:
        # 对 Cookie 进行脱敏显示
        if len(cookie) > 24:
            safe_cfg["cookie_masked"] = f"{cookie[:10]}...{cookie[-8:]}"
        else:
            safe_cfg["cookie_masked"] = "***"
    else:
        safe_cfg["cookie_masked"] = ""
    return JSONResponse(content={"code": 0, "msg": "ok", "data": safe_cfg})


@app.post("/music/ext/api/netease/config")
async def ext_api_save_netease_config(request: Request):
    """保存网易云音源与会员配置"""
    try:
        body = await request.json()
        cookie = str(body.get("cookie") or "").strip()
        cur_cfg = netease_api.load_netease_config()
        
        # 如果传入了新的非脱敏 Cookie，立即更新并检验用户信息
        if cookie and "..." not in cookie and cookie != cur_cfg.get("cookie"):
            user_info = await netease_api.netease_client.fetch_user_profile(cookie)
            body["user_info"] = user_info
            if user_info.get("is_logged_in"):
                body["enabled"] = True
                
        # 安全性校验：若未启用 SVIP 音源却选择了 SVIP 专属音质，自动降级为 lossless
        if not body.get("enable_svip") and body.get("quality") in ("jymaster", "sky", "jyeffect"):
            body["quality"] = "lossless"
        
        saved = netease_api.save_netease_config(body)
        return JSONResponse(content={"code": 0, "msg": "网易云音乐配置已保存！", "data": saved})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"保存异常: {e}"})


@app.post("/music/ext/api/netease/qr/create")
async def ext_api_netease_qr_create():
    """生成网易云登录二维码"""
    try:
        res = await netease_api.netease_client.create_qr_key()
        return JSONResponse(content={"code": 0 if res.get("ok") else -1, "msg": res.get("msg", "ok"), "data": res})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"生成二维码失败: {e}"})


@app.get("/music/ext/api/netease/qr/check")
async def ext_api_netease_qr_check(unikey: str):
    """轮询网易云二维码扫码状态"""
    try:
        res = await netease_api.netease_client.check_qr_status(unikey)
        return JSONResponse(content={"code": res.get("code", 0), "msg": res.get("msg", ""), "data": res})
    except Exception as e:
        return JSONResponse(content={"code": 500, "msg": f"扫码状态查询异常: {e}"})


@app.post("/music/ext/api/netease/logout")
async def ext_api_netease_logout():
    """退出网易云账号登录并清除 Cookie"""
    try:
        cfg = netease_api.load_netease_config()
        cfg["cookie"] = ""
        cfg["user_info"] = {
            "is_logged_in": False,
            "user_id": 0,
            "nickname": "未登录",
            "avatar_url": "",
            "vip_type": 0,
            "vip_level": "未登录",
            "is_svip": False,
            "vip_expire_time": 0,
            "vip_expire_str": ""
        }
        netease_api.save_netease_config(cfg)
        return JSONResponse(content={"code": 0, "msg": "已退出登录并清除 Cookie"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"退出失败: {e}"})


@app.post("/music/ext/api/netease/test")
async def ext_api_netease_test():
    """连通性与 VIP/SVIP 音源解析测试"""
    import time
    t0 = time.time()
    try:
        cfg = netease_api.load_netease_config()
        cookie = cfg.get("cookie", "")
        
        # 0. 刷新用户信息与 VIP/SVIP 状态
        user_info = await netease_api.netease_client.fetch_user_profile(cookie)
        if cookie and user_info.get("is_logged_in"):
            cfg["user_info"] = user_info
            netease_api.save_netease_config(cfg)
            
        # 测试 1: 搜索测试
        search_res = await netease_api.netease_client.search_songs("七里香", limit=2)
        # 测试 2: 歌曲播放链接解析测试 (测试首曲或 Beyond 经典 347230)
        test_song_id = search_res[0]["id"] if search_res else "347230"
        url_res = await netease_api.netease_client.get_song_url(test_song_id, level=cfg.get("quality", "lossless"), cookie=cookie)
        # 测试 3: 歌词获取测试
        lyric_res = await netease_api.netease_client.get_lyric(test_song_id)
        
        cost_ms = int((time.time() - t0) * 1000)
        return JSONResponse(content={
            "code": 0 if (search_res and (url_res.get("ok") or lyric_res.get("ok"))) else -1,
            "msg": f"网易云 API 连通正常 (耗时 {cost_ms}ms)",
            "data": {
                "latency_ms": cost_ms,
                "search_ok": bool(search_res),
                "stream_ok": url_res.get("ok", False),
                "stream_level": url_res.get("level", "standard"),
                "stream_level_desc": url_res.get("level_desc", "标准品质"),
                "lyric_ok": lyric_res.get("ok", False),
                "user_info": user_info
            }
        })
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": f"测试失败: {e}"})


@app.post("/music/ext/api/now_playing")
async def api_update_now_playing(request: Request):
    try:
        data = await request.json()
        global _CURRENT_PLAYING_RECORD
        _CURRENT_PLAYING_RECORD = {
            "guid": str(data.get("guid") or ""),
            "title": str(data.get("title") or ""),
            "artist": str(data.get("artist") or ""),
            "cover_url": str(data.get("cover_url") or data.get("coverUrl") or ""),
            "source_playlist": str(data.get("source_playlist") or data.get("playlist_id") or data.get("playlistId") or ""),
            "time": time.time()
        }
        return JSONResponse({"code": 0, "msg": "ok", "data": _CURRENT_PLAYING_RECORD})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": str(e)})


@app.get("/music/ext/api/now_playing")
async def api_get_now_playing():
    return JSONResponse({"code": 0, "data": _CURRENT_PLAYING_RECORD})



# =========================================================================
# 🛠️ 落雪管理代理接口 (/music/ext/api/lx/*)
# =========================================================================

LX_BASE_API = "http://127.0.0.1:9528"
LX_AUTH = {"x-frontend-auth": os.environ.get("FNMUSIC_AUTH_TOKEN", "")}

@app.get("/music/ext/api/lx/config")
async def ext_api_lx_get_config():
    """获取飞牛音乐系统控制核心配置"""
    try:
        cfg = feats.load_settings()
        data = {
            "serverName": cfg.get("serverName") or cfg.get("server_name") or "飞牛音乐",
            "maxSnapshotNum": cfg.get("maxSnapshotNum") or cfg.get("max_snapshots") or 10,
            "list.addMusicLocationType": cfg.get("list.addMusicLocationType") or cfg.get("add_music_location") or "top",
            "proxy.enabled": bool(cfg.get("proxy.enabled", cfg.get("proxy_enabled", False))),
            "proxy.header": cfg.get("proxy.header") or cfg.get("proxy_header") or "x-real-ip",
            "user.enablePath": bool(cfg.get("user.enablePath", cfg.get("custom_path_enabled", True))),
            "download_dir": cfg.get("download_dir") or detect_library_dir(),
            "auto_scan_on_download": bool(cfg.get("auto_scan_on_download", True)),
        }
        return JSONResponse({"code": 0, "data": data})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"获取系统配置失败: {e}"})

@app.post("/music/ext/api/lx/config")
async def ext_api_lx_save_config(request: Request):
    """保存飞牛音乐系统控制配置并立即在后端和底层生效"""
    try:
        body = await request.json()
        cfg = feats.load_settings()

        server_name = str(body.get("serverName") or "飞牛音乐").strip()
        max_snapshots = int(body.get("maxSnapshotNum") or 10)
        add_location = str(body.get("list.addMusicLocationType") or "top").strip()
        proxy_enabled = bool(body.get("proxy.enabled", False))
        proxy_header = str(body.get("proxy.header") or "x-real-ip").strip()
        user_enable_path = bool(body.get("user.enablePath", True))
        auto_scan = bool(body.get("auto_scan_on_download", True))

        cfg["serverName"] = server_name
        cfg["server_name"] = server_name
        cfg["maxSnapshotNum"] = max_snapshots
        cfg["max_snapshots"] = max_snapshots
        cfg["list.addMusicLocationType"] = add_location
        cfg["add_music_location"] = add_location
        cfg["proxy.enabled"] = proxy_enabled
        cfg["proxy_enabled"] = proxy_enabled
        cfg["proxy.header"] = proxy_header
        cfg["proxy_header"] = proxy_header
        cfg["user.enablePath"] = user_enable_path
        cfg["custom_path_enabled"] = user_enable_path
        cfg["auto_scan_on_download"] = auto_scan

        if "download_dir" in body and str(body["download_dir"]).strip():
            new_dl = str(body["download_dir"]).strip()
            cfg["download_dir"] = new_dl
            os.makedirs(new_dl, exist_ok=True)

        feats.save_settings(cfg)

        # 立即修剪超额快照
        try:
            fn_backup.prune_snapshots(max_snapshots)
        except Exception as pe:
            logger.warning("prune_snapshots failed on save: %s", pe)

        return JSONResponse({"code": 0, "msg": "飞牛音乐系统控制配置已保存并立即生效！", "data": cfg})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"保存配置失败: {e}"})

@app.post("/music/ext/api/service/reload")
async def ext_api_service_reload():
    """重新加载飞牛音乐扩展服务与音源沙箱"""
    try:
        feats.load_settings()
        native_engine.load_sources_meta()
        return JSONResponse({"code": 0, "msg": "飞牛音乐扩展服务与内置引擎已成功重载！"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"重载失败: {e}"})

@app.get("/music/ext/api/lx/webdav/config")
async def ext_api_lx_get_webdav_config():
    try:
        cfg = fn_webdav.load_webdav_config()
        return JSONResponse({"code": 0, "data": cfg})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"获取 WebDAV 配置失败: {e}"})

@app.post("/music/ext/api/lx/webdav/config")
async def ext_api_lx_save_webdav_config(request: Request):
    try:
        body = await request.json()
        target_path = body.get("path", "").strip() or "/music_backup"
        cfg = {
            "url": body.get("url", "").strip(),
            "username": body.get("username", "").strip(),
            "password": body.get("password", "").strip(),
            "path": target_path,
            "auto_backup": bool(body.get("auto_backup", False)),
            "cron": body.get("cron", "").strip() or "0 4 * * *"
        }
        fn_webdav.save_webdav_config(cfg)
        fn_webdav.add_log(f"已更新 WebDAV 设置，云端目录: {target_path}")
        return JSONResponse({"code": 0, "msg": "飞牛音乐 WebDAV 设置保存成功！"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"保存 WebDAV 失败: {e}"})

@app.post("/music/ext/api/lx/webdav/test")
async def ext_api_lx_test_webdav(request: Request):
    try:
        body = await request.json()
        target_url = body.get("url", "").strip()
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()
        path = body.get("path", "").strip() or "/"

        if not target_url or not target_url.startswith("http"):
            return JSONResponse({"code": -1, "msg": "WebDAV 服务器地址 (URL) 格式不正确，必须以 http:// 或 https:// 开头！"})

        # 使用 PROPFIND 方法真实探测 WebDAV 终端连通性与凭证
        auth = (username, password) if username or password else None
        test_url = target_url.rstrip("/") + "/"
        
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            try:
                # 优先发送 WebDAV PROPFIND 测试目录
                resp = await client.request("PROPFIND", test_url, auth=auth, headers={"Depth": "0"})
                if resp.status_code in (200, 207):
                    return JSONResponse({"code": 0, "msg": f"✅ WebDAV 服务连接与凭证校验成功！(HTTP {resp.status_code})"})
                elif resp.status_code in (401, 403):
                    return JSONResponse({"code": -1, "msg": f"❌ WebDAV 认证失败 (HTTP {resp.status_code})：请检查用户名和密码！"})
                elif resp.status_code == 404:
                    return JSONResponse({"code": -1, "msg": f"❌ WebDAV 路径不存在 (HTTP 404)：请检查服务器地址与路径！"})
                
                # 尝试备用 OPTIONS 方法
                resp_opt = await client.options(test_url, auth=auth)
                if resp_opt.status_code in (200, 204) and ("dav" in resp_opt.headers.get("dav", "").lower() or resp_opt.status_code == 200):
                    return JSONResponse({"code": 0, "msg": "✅ WebDAV 服务连接成功！"})
                
                return JSONResponse({"code": -1, "msg": f"❌ WebDAV 连接失败 (HTTP {resp.status_code})"})
            except httpx.ConnectError:
                return JSONResponse({"code": -1, "msg": f"❌ 无法连接到 WebDAV 服务器：请检查主机地址与端口是否正确！"})
            except httpx.TimeoutException:
                return JSONResponse({"code": -1, "msg": "❌ 连接超时：无法访问该 WebDAV 服务器地址，请检查网络！"})
            except Exception as net_err:
                return JSONResponse({"code": -1, "msg": f"❌ WebDAV 探测异常: {net_err}"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"测试失败: {e}"})

@app.post("/music/ext/api/lx/webdav/backup")
async def ext_api_lx_trigger_webdav_backup():
    try:
        zip_path = fn_backup.create_fn_music_backup_zip("webdav")
        ok = await fn_webdav.upload_backup_to_webdav(zip_path)
        if ok:
            return JSONResponse({"code": 0, "msg": f"✅ 飞牛音乐全量数据已成功打包并上传至 WebDAV！"})
        return JSONResponse({"code": -1, "msg": "备份上传失败，请查看下方日志"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"触发备份失败: {e}"})

@app.get("/music/ext/api/lx/webdav/logs")
async def ext_api_lx_webdav_logs():
    try:
        logs = fn_webdav.get_logs()
        return JSONResponse({"code": 0, "data": {"logs": logs}})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"获取日志失败: {e}"})

@app.post("/music/ext/api/lx/webdav/restore")
async def ext_api_lx_webdav_restore():
    try:
        fn_webdav.add_log("从 WebDAV 云端检索并恢复最新备份...")
        cfg = fn_webdav.load_webdav_config()
        base_url = cfg.get("url", "").rstrip("/")
        username = cfg.get("username", "")
        password = cfg.get("password", "")
        target_dir = cfg.get("path", "").strip().strip("/")
        auth = (username, password) if username or password else None

        # 尝试通过 PROPFIND 列出云端目录下的最新备份
        list_url = f"{base_url}/{target_dir}/" if target_dir else f"{base_url}/"
        async with httpx.AsyncClient(timeout=60.0, verify=False, follow_redirects=True) as client:
            resp = await client.request("PROPFIND", list_url, auth=auth, headers={"Depth": "1"})
            if resp.status_code in (200, 207):
                import re
                zips = re.findall(r'href>([^<]+\.zip)<', resp.text)
                if zips:
                    # 去重并排序，选取时间戳最新的 zip
                    unique_zips = sorted(list(dict.fromkeys([z.strip() for z in zips])))
                    latest_zip = unique_zips[-1]
                    if not latest_zip.startswith("http"):
                        from urllib.parse import urljoin
                        latest_zip = urljoin(list_url, latest_zip)
                    fn_webdav.add_log(f"发现云端最新备份包: {os.path.basename(latest_zip)}，开始下载...")
                    r_get = await client.get(latest_zip, auth=auth)
                    if r_get.status_code == 200:
                        tmp_zip = f"/tmp/webdav-restore-{int(time.time())}.zip"
                        with open(tmp_zip, "wb") as f:
                            f.write(r_get.content)
                        ok = fn_backup.restore_from_zip(tmp_zip)
                        if ok:
                            fn_webdav.add_log(f"✅ 成功从云端恢复备份包 [{os.path.basename(latest_zip)}]！")
                            return JSONResponse({"code": 0, "msg": f"✅ 已成功从云端恢复飞牛音乐数据 [{os.path.basename(latest_zip)}]！"})
                        else:
                            fn_webdav.add_log("❌ 解压还原失败：备份包数据损坏或格式不符")
                            return JSONResponse({"code": -1, "msg": "备份包解压还原失败"})
                    else:
                        fn_webdav.add_log(f"❌ 下载备份包失败 (HTTP {r_get.status_code})")
                        return JSONResponse({"code": -1, "msg": f"下载备份包失败 (HTTP {r_get.status_code})"})
            fn_webdav.add_log("未在云端目录找到有效的 .zip 备份包")
            return JSONResponse({"code": -1, "msg": "未在云端目录找到有效的 .zip 备份文件"})
    except Exception as e:
        fn_webdav.add_log(f"恢复异常: {e}")
        return JSONResponse({"code": -1, "msg": f"恢复异常: {e}"})

@app.post("/music/ext/api/lx/webdav/sync")
async def ext_api_lx_webdav_sync():
    try:
        # 同步飞牛音乐数据至 WebDAV
        zip_path = fn_backup.create_fn_music_backup_zip("sync")
        ok = await fn_webdav.upload_backup_to_webdav(zip_path)
        if ok:
            return JSONResponse({"code": 0, "msg": "✅ 飞牛音乐数据已成功同步至 WebDAV！"})
        return JSONResponse({"code": -1, "msg": "同步失败，请检查连接与日志"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"同步失败: {e}"})

@app.get("/music/ext/api/lx/backup/download")
async def ext_api_lx_backup_download():
    """下载飞牛音乐自身全量数据备份包 (db, 配置, 脚本, 下载记录)"""
    try:
        zip_path = fn_backup.create_fn_music_backup_zip("manual")
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=os.path.basename(zip_path)
        )
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"生成飞牛音乐备份失败: {e}"})

@app.post("/music/ext/api/lx/backup/upload")
async def ext_api_lx_backup_upload(request: Request):
    """上传飞牛音乐备份包并覆盖还原"""
    try:
        form = await request.form()
        file_obj = form.get("file")
        if not file_obj:
            return JSONResponse({"code": -1, "msg": "请选择要上传的 ZIP 备份文件！"})
        content = await file_obj.read()
        tmp_zip = f"/tmp/upload-restore-{int(time.time())}.zip"
        with open(tmp_zip, "wb") as f:
            f.write(content)
        ok = fn_backup.restore_from_zip(tmp_zip)
        if ok:
            return JSONResponse({"code": 0, "msg": "✅ 飞牛音乐数据已成功覆盖还原！"})
        return JSONResponse({"code": -1, "msg": "还原失败，ZIP 结构可能不符合规范"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"上传异常: {e}"})


@app.get("/music/ext/api/lx/snapshots")
async def ext_api_lx_get_snapshots(user: str = "admin"):
    """获取飞牛音乐自身版本历史快照"""
    try:
        snaps = fn_backup.list_snapshots()
        return JSONResponse({"code": 0, "data": snaps})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"获取快照失败: {e}"})

@app.post("/music/ext/api/lx/snapshot/restore")
async def ext_api_lx_restore_snapshot(id: str, user: str = "admin"):
    """回滚至指定快照"""
    try:
        ok = fn_backup.restore_snapshot_by_id(id)
        if ok:
            return JSONResponse({"code": 0, "msg": f"已成功恢复至飞牛音乐快照 [{id}]！"})
        return JSONResponse({"code": -1, "msg": "快照文件不存在或还原异常"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"回滚失败: {e}"})

@app.post("/music/ext/api/lx/snapshot/delete")
async def ext_api_lx_delete_snapshot(id: str, user: str = "admin"):
    """删除快照"""
    try:
        zip_path = os.path.join(fn_backup.SNAPSHOT_DIR, f"{id}.zip")
        if os.path.exists(zip_path):
            os.remove(zip_path)
            return JSONResponse({"code": 0, "msg": "快照已成功删除！"})
        return JSONResponse({"code": -1, "msg": "未找到指定快照"})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"删除快照失败: {e}"})

@app.get("/music/ext/api/lx/data")
async def ext_api_lx_get_data(user: str = "admin"):
    """查看飞牛音乐媒体库曲目、歌单与下载记录"""
    try:
        summary = fn_backup.get_fn_music_data_summary()
        return JSONResponse({"code": 0, "data": summary})
    except Exception as e:
        return JSONResponse({"code": -1, "msg": f"获取飞牛音乐数据失败: {e}"})


app.include_router(download_routes.router)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    return await forward_to_upstream(request, get_upstream_client(request.app))
