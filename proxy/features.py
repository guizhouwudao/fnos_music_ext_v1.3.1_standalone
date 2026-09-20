#!/usr/bin/env python3
"""
fnmusic-ext 增强特性模块：
1. 排行榜 (Leaderboards) 接入与动态注入
2. 精选与外部歌单导入 (Playlists & Import)
3. 本地音乐与下载/缓存管理 (Download & Cache)
4. 增强我的收藏 (Favorites Sync)
5. 现代化 Web 设置与管理控制台 (Settings & Console UI)
   - 深度对标 LX MUSIC Web 布局：显示-外观设置、播放与逻辑控制、实时运行日志等
"""

import asyncio
import hashlib
import json
import logging

LX_AUTH_HEADER = {"User-Agent": "Mozilla/5.0"}

import os
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Callable
from urllib.parse import quote, unquote

import httpx

logger = logging.getLogger("fnmusic_features")

# ---------------------------------------------------------------------
# 音频字节嗅探：只接受明文音频容器，拒绝酷我 mflac/mgg 等加密/混淆流
# ---------------------------------------------------------------------
_PLAIN_AUDIO_MAGICS = (
    (b"fLaC", "flac"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
    (b"\xff\xfa", "mp3"),
    (b"OggS", "ogg"),
    (b"RIFF", "wav"),
)


def sniff_audio_ext(path: str) -> str | None:
    """读取文件头判断真实明文音频格式；加密/混淆流返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return None
    if len(head) < 8:
        return None
    for magic, ext in _PLAIN_AUDIO_MAGICS:
        if head.startswith(magic):
            return ext
    if head[4:8] == b"ftyp":
        return "m4a"
    return None


def _encrypted_audio_url(url: str) -> bool:
    """判断直链是否指向酷我/QQ 等平台的加密音频（.mflac/.mgg/kwm 等）。"""
    u = (url or "").lower()
    if not u:
        return True
    if ".mflac" in u or ".mgg" in u or ".mflac" in u:
        return True
    if ".kwm" in u and ".kwmp3" not in u and ".mp3" not in u:
        return True
    return False

# 基础目录配置
_HOME = os.environ.get("HOME") or "/root"
STATE_DIR = os.path.join(_HOME, ".local", "state", "fnmusic_ext")
BOARD_CACHE_DIR = os.path.join(STATE_DIR, "board_cache")
PLAYLISTS_DIR = os.path.join(STATE_DIR, "custom_playlists")
DOWNLOADS_DIR = os.path.join(STATE_DIR, "downloads")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")

# 确保目录存在
for d in [STATE_DIR, BOARD_CACHE_DIR, PLAYLISTS_DIR, DOWNLOADS_DIR]:
    os.makedirs(d, exist_ok=True)

# 默认内置推荐榜单定义
DEFAULT_BOARDS = [
    {"id": "board:kw:16", "name": "🔥 酷我热歌榜", "source": "kw", "bangid": "16", "enabled": True, "count": 100},
    {"id": "board:kw:17", "name": "✨ 酷我新歌榜", "source": "kw", "bangid": "17", "enabled": True, "count": 100},
    {"id": "board:kw:93", "name": "🚀 酷我飙升榜", "source": "kw", "bangid": "93", "enabled": True, "count": 100},
    {"id": "board:kw:26", "name": "📻 经典怀旧榜", "source": "kw", "bangid": "26", "enabled": True, "count": 100},
    {"id": "board:kw:158", "name": "🎵 抖音热歌榜", "source": "kw", "bangid": "158", "enabled": True, "count": 100},
    {"id": "board:kw:22", "name": "🌍 欧美流行榜", "source": "kw", "bangid": "22", "enabled": True, "count": 100},
    {"id": "board:wy:3778678", "name": "🔴 网易云热歌榜", "source": "wy", "bangid": "3778678", "enabled": True, "count": 100},
    {"id": "board:wy:19723756", "name": "⚡ 网易云飙升榜", "source": "wy", "bangid": "19723756", "enabled": True, "count": 100},
    {"id": "board:wy:3779629", "name": "🌟 网易云新歌榜", "source": "wy", "bangid": "3779629", "enabled": True, "count": 100},
]

# 默认设置项（包含外观、播放逻辑、搜索与音源控制）
DEFAULT_SETTINGS = {
    # 播放与逻辑
    "preferred_quality": "lossless",  # lossless (无损FLAC) / high (320k) / standard (128k)
    "enable_leaderboards": True,
    "enable_daily_recommend": True,
    "auto_cache_song": True,
    "max_cache_mb": 5120,  # 默认 5GB 缓存
    "download_dir": "",
    "lx_server_url": "",
    "lx_service_url": "",
    "boards": DEFAULT_BOARDS,

    # 显示与外观设置 (对标 LX MUSIC Web)
    "theme_mode": "dark",  # dark / black / auto
    "accent_color": "#f62c55",  # 飞牛红/落雪粉
    "show_quality_badge": True,
    "enable_smooth_scroll": True,
    "custom_css": "",
}


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            res = dict(DEFAULT_SETTINGS)
            res.update(data)
            return res
    except Exception as e:
        logger.warning("load_settings error: %s", e)
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> bool:
    try:
        with open(SETTINGS_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)
        return True
    except Exception as e:
        logger.warning("save_settings error: %s", e)
        return False


# =====================================================================
# 1. 排行榜 (Leaderboards)
# =====================================================================

def is_board_guid(guid: str | None) -> bool:
    return bool(guid and str(guid).startswith("board:"))


def parse_board_guid(guid: str) -> tuple[str, str]:
    parts = guid.split(":")
    if len(parts) >= 3:
        return parts[1], parts[2]
    return "", ""


async def fetch_board_tracks(board_id: str, lx_server_url: str = "") -> list[dict]:
    source, bangid = parse_board_guid(board_id)
    if not source or not bangid:
        return []

    cache_file = os.path.join(BOARD_CACHE_DIR, f"{source}_{bangid}.json")
    now = time.time()
    if os.path.exists(cache_file):
        try:
            mtime = os.path.getmtime(cache_file)
            if now - mtime < 6 * 3600:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    tracks = []
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # 1. 酷我原生排行榜 (彻底不依赖 9528)
            if source == "kw":
                url = f"http://kbangserver.kuwo.cn/ksong.s?from=pc&fmt=json&type=bang&data=content&id={bangid}&pn=0&rn=100"
                r = await client.get(url, headers={"User-Agent": "okhttp/3.10.0"})
                if r.status_code == 200:
                    raw_list = (r.json() or {}).get("musiclist") or []
                    for item in raw_list:
                        sid = str(item.get("id") or "").strip()
                        if not sid:
                            continue
                        pic = str(item.get("pic") or "")
                        if pic and not pic.startswith("http"):
                            pic = f"http://img1.kwcdn.kuwo.cn/star/albumcover/{pic}"
                        tracks.append({
                            "id": f"lx:kw:{sid}",
                            "source": "lx",
                            "lx_source": "kw",
                            "song_id": sid,
                            "title": str(item.get("name") or "").strip(),
                            "artist": str(item.get("artist") or "").strip(),
                            "album": str(item.get("album") or "").strip(),
                            "duration_s": float(item.get("duration") or 240),
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": pic,
                            "lyric": "",
                        })

            # 2. 网易云原生排行榜 (彻底不依赖 9528)
            elif source == "wy":
                url = f"https://music.163.com/api/playlist/detail?id={bangid}"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
                if r.status_code == 200:
                    raw_list = (r.json() or {}).get("result", {}).get("tracks") or []
                    for item in raw_list:
                        sid = str(item.get("id") or "").strip()
                        if not sid:
                            continue
                        artists = item.get("artists") or []
                        art_name = "/".join(a.get("name", "") for a in artists if a.get("name")) or "群星"
                        album = (item.get("album") or {}).get("name") or ""
                        cover = (item.get("album") or {}).get("picUrl") or ""
                        dur = float(item.get("duration") or 240000) / 1000.0
                        tracks.append({
                            "id": f"lx:wy:{sid}",
                            "source": "lx",
                            "lx_source": "wy",
                            "song_id": sid,
                            "title": str(item.get("name") or "").strip(),
                            "artist": art_name,
                            "album": album,
                            "duration_s": dur,
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": cover,
                            "lyric": "",
                        })

            # 3. 酷狗原生排行榜
            elif source == "kg":
                url = f"http://mobilecdnbj.kugou.com/api/v3/rank/song?rankid={bangid}&page=1&pagesize=100"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    raw_list = (r.json() or {}).get("data", {}).get("info") or []
                    for item in raw_list:
                        hash_id = str(item.get("hash") or "").strip()
                        filename = str(item.get("filename") or "")
                        parts = filename.split(" - ", 1)
                        singer = parts[0].strip() if len(parts) > 1 else ""
                        name = parts[1].strip() if len(parts) > 1 else filename
                        tracks.append({
                            "id": f"lx:kg:{hash_id}",
                            "source": "lx",
                            "lx_source": "kg",
                            "song_id": hash_id,
                            "title": name,
                            "artist": singer,
                            "album": str(item.get("album_name") or ""),
                            "duration_s": float(item.get("duration") or 240),
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": "",
                            "lyric": "",
                        })
    except Exception as e:
        logger.warning("fetch_board_tracks failed for %s: %s", board_id, e)

    if tracks:
        try:
            with open(cache_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump(tracks, f, ensure_ascii=False)
            os.replace(cache_file + ".tmp", cache_file)
        except Exception:
            pass

    return tracks


def _parse_duration(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str) and ":" in val:
        parts = val.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except Exception:
            pass
    return 240.0


# =====================================================================
# 2. 自定义与导入歌单 (Playlists)
# =====================================================================

def is_custom_playlist_guid(guid: str | None) -> bool:
    return bool(guid and (str(guid).startswith("cplaylist:") or str(guid).startswith("imported:")))


def get_user_playlist_dir(user_guid: str) -> str:
    safe_user = re.sub(r"[^A-Za-z0-9\-_]", "_", user_guid) or "shared"
    d = os.path.join(PLAYLISTS_DIR, safe_user)
    os.makedirs(d, exist_ok=True)
    return d


def load_user_custom_playlists(user_guid: str) -> list[dict]:
    playlists_map: dict[str, dict] = {}
    # 同时扫描当前用户和 shared 共享目录，彻底避免多端账号孤岛
    dirs_to_check = [get_user_playlist_dir("shared")]
    if user_guid and user_guid != "shared":
        dirs_to_check.append(get_user_playlist_dir(user_guid))

    for p_dir in dirs_to_check:
        if not os.path.exists(p_dir):
            continue
        for fname in os.listdir(p_dir):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(p_dir, fname), "r", encoding="utf-8") as f:
                        pl = json.load(f)
                        if isinstance(pl, dict) and pl.get("guid"):
                            playlists_map[pl["guid"]] = pl
                except Exception:
                    pass

    res = list(playlists_map.values())
    res.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)
    return res


def save_user_custom_playlist(user_guid: str, playlist: dict) -> bool:
    guid = playlist.get("guid")
    if not guid:
        return False
    safe_id = re.sub(r"[^A-Za-z0-9\-_]", "_", guid)
    # 同时持久化至 shared 和当前用户目录
    dirs = [get_user_playlist_dir("shared")]
    if user_guid and user_guid != "shared":
        dirs.append(get_user_playlist_dir(user_guid))

    success = False
    for p_dir in dirs:
        target = os.path.join(p_dir, f"{safe_id}.json")
        try:
            with open(target + ".tmp", "w", encoding="utf-8") as f:
                json.dump(playlist, f, ensure_ascii=False, indent=2)
            os.replace(target + ".tmp", target)
            success = True
        except Exception as e:
            logger.warning("save_user_custom_playlist error in %s: %s", p_dir, e)
    return success


def delete_user_custom_playlist(user_guid: str, guid: str) -> bool:
    safe_id = re.sub(r"[^A-Za-z0-9\-_]", "_", guid)
    dirs = [get_user_playlist_dir("shared")]
    if user_guid and user_guid != "shared":
        dirs.append(get_user_playlist_dir(user_guid))

    deleted = False
    for p_dir in dirs:
        target = os.path.join(p_dir, f"{safe_id}.json")
        if os.path.exists(target):
            try:
                os.remove(target)
                deleted = True
            except Exception:
                pass
    return deleted


async def import_external_playlist(
    url_or_id: str,
    user_guid: str = "shared",
    custom_name: str = "",
    lx_server_url: str = "",
) -> dict:
    raw = url_or_id.strip()
    source = "wy"
    playlist_id = ""

    if "music.163.com" in raw:
        source = "wy"
        m = re.search(r"id=(\d+)", raw)
        if m:
            playlist_id = m.group(1)
    elif "kuwo.cn" in raw:
        source = "kw"
        m = re.search(r"(\d+)", raw)
        if m:
            playlist_id = m.group(1)
    elif raw.isdigit():
        source = "wy"
        playlist_id = raw
    else:
        m = re.search(r"(\d{5,12})", raw)
        if m:
            playlist_id = m.group(1)

    if not playlist_id:
        return {"ok": False, "msg": "未能从输入中识别有效的歌单 ID 或链接"}

    try:
        # 原生直连获取歌单详情（彻底不经过 9528）
        detail = await fetch_online_playlist_detail(source, playlist_id)
        raw_list = detail.get("tracks") or []
        if not raw_list:
            return {"ok": False, "msg": "该歌单为空或获取歌单详情失败"}

        pl_name = custom_name.strip() or str(detail.get("name") or f"导入歌单_{source}_{playlist_id}").strip()
        pl_desc = str(detail.get("desc") or "").strip()
        cover_url = str(detail.get("cover_url") or (raw_list[0].get("cover_url") if raw_list else "") or "").strip()

        guid = f"imported:{source}:{playlist_id}"
        now = int(time.time())

        tracks = []
        for item in raw_list:
            songmid = str(item.get("song_id") or item.get("id") or "").strip()
            if not songmid:
                continue
            tracks.append({
                "id": f"lx:{source}:{songmid}",
                "source": "lx",
                "lx_source": source,
                "song_id": songmid,
                "title": str(item.get("title") or item.get("name") or "").strip(),
                "artist": str(item.get("artist") or item.get("singer") or "").strip(),
                "album": str(item.get("album") or item.get("albumName") or "").strip(),
                "duration_s": int(item.get("duration_s") or 240),
                "ext": "flac",
                "file_size": 31457280,
                "cover_url": str(item.get("cover_url") or cover_url).strip(),
                "lyric": "",
            })

        playlist_obj = {
            "guid": guid,
            "name": pl_name,
            "desc": pl_desc,
            "coverId": guid,
            "cover_url": cover_url,
            "source": source,
            "external_id": playlist_id,
            "createdAt": now,
            "updatedAt": now,
            "trackCount": len(tracks),
            "tracks": tracks,
        }

        save_user_custom_playlist(user_guid, playlist_obj)
        return {"ok": True, "msg": f"成功导入歌单《{pl_name}》，共 {len(tracks)} 首歌曲！", "data": playlist_obj}
    except Exception as e:
        logger.warning("import_external_playlist error: %s", e)
        return {"ok": False, "msg": f"导入过程发生异常: {e}"}


# =====================================================================
# 3. 本地音乐下载与缓存管理 (Download & Cache)
# =====================================================================

def get_cache_stats(cache_root: str) -> dict:
    total_size = 0
    file_count = 0
    if os.path.exists(cache_root):
        for root, _, files in os.walk(cache_root):
            for f in files:
                try:
                    p = os.path.join(root, f)
                    total_size += os.path.getsize(p)
                    file_count += 1
                except Exception:
                    pass
    return {
        "file_count": file_count,
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
    }


def clear_cache_files(cache_root: str) -> int:
    deleted = 0
    if os.path.exists(cache_root):
        for item in os.listdir(cache_root):
            p = os.path.join(cache_root, item)
            try:
                if os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
                    deleted += 1
                elif os.path.isdir(p) and item != "lyrics":
                    shutil.rmtree(p)
                    deleted += 1
            except Exception:
                pass
    return deleted


def prune_cache_if_needed(cache_root: str, max_mb: int) -> int:
    """当缓存总大小超过 max_mb 上限时，按修改时间自动清理最旧的音频缓存"""
    if not os.path.exists(cache_root) or max_mb <= 0:
        return 0
    max_bytes = max_mb * 1024 * 1024
    files = []
    total_size = 0
    for root, dirs, filenames in os.walk(cache_root):
        if "lyrics" in dirs:
            dirs.remove("lyrics")
        for f in filenames:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                files.append((st.st_mtime, st.st_size, p))
                total_size += st.st_size
            except Exception:
                pass

    if total_size <= max_bytes:
        return 0

    files.sort(key=lambda x: x[0])
    deleted = 0
    for _, size, p in files:
        if total_size <= max_bytes:
            break
        try:
            os.remove(p)
            total_size -= size
            deleted += 1
        except Exception:
            pass
    logger.info("prune_cache_if_needed: removed %d old cache files, new size: %.2f MB", deleted, total_size / (1024 * 1024))
    return deleted


def get_fnmusic_stats(user_guid: str | None = None) -> dict:
    """获取飞牛音乐原生的实时统计指标（包含曲目、专辑、歌手、歌单、红心收藏、播放历史）"""
    db_path = "/usr/local/apps/@appdata/trim.music/db/music.db"
    res = {
        "ok": False,
        "track_count": 0,
        "album_count": 0,
        "artist_count": 0,
        "playlist_count": 0,
        "favorite_count": 0,
        "history_count": 0,
        "user_name": "管理员",
        "user_id": 1,
    }
    if not os.path.exists(db_path):
        return res
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM track WHERE is_audio_file_deleted = 0")
        res["track_count"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM album")
        res["album_count"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM artist")
        res["artist_count"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM playlist")
        res["playlist_count"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM play_history")
        res["history_count"] = c.fetchone()[0]

        uid = None
        if user_guid and user_guid != "shared":
            c.execute("SELECT id, name FROM user WHERE guid = ?", (user_guid,))
            u_row = c.fetchone()
            if u_row:
                uid, res["user_name"] = u_row[0], u_row[1]
                res["user_id"] = uid

        if uid is not None:
            c.execute("SELECT COUNT(*) FROM favorite_track WHERE user_id = ?", (uid,))
            res["favorite_count"] = c.fetchone()[0]
        else:
            c.execute("SELECT COUNT(*) FROM favorite_track")
            res["favorite_count"] = c.fetchone()[0]
            c.execute("SELECT name FROM user WHERE role = 'admin' LIMIT 1")
            admin_row = c.fetchone()
            if admin_row:
                res["user_name"] = admin_row[0]

        conn.close()
        res["ok"] = True
    except Exception as e:
        logger.warning("get_fnmusic_stats error: %s", e)
    return res


async def download_online_song(
    song_guid: str,
    target_dir: str,
    lx_service_url: str = "http://127.0.0.1:8772",
    track_info: dict | None = None,
) -> dict:
    # 兼容 online:lx:kw:xxx 与 lx:kw:xxx
    raw_id = song_guid
    if raw_id.startswith("online:"):
        raw_id = raw_id[len("online:"):]

    if not os.path.exists(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            return {"ok": False, "msg": f"无法创建目标下载目录: {e}"}

    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            info = track_info or {}
            if not info or not info.get("title"):
                r_info = await client.get(f"{lx_service_url}/api/v1/track/info", params={"id": raw_id})
                if r_info.status_code == 200:
                    info = r_info.json().get("data") or {}

            # 尝试从歌词或者 info 中解析真实的歌名歌手
            title = str(info.get("title") or "").replace("/", "_").strip()
            artist = str(info.get("artist") or "").replace("/", "_").strip()
            album = str(info.get("album") or "").strip()

            # 如果没有歌名，尝试从歌词标签中提取 [ti:歌名] 和 [ar:歌手]
            lrc_text = ""
            try:
                r_lrc = await client.get(f"{lx_service_url}/api/v1/track/lyric", params={"id": raw_id})
                if r_lrc.status_code == 200:
                    lrc_text = r_lrc.json().get("data", {}).get("lyric") or ""
                    if not title:
                        m_ti = re.search(r"\[ti:(.*?)\]", lrc_text)
                        if m_ti:
                            title = m_ti.group(1).strip()
                    if not artist:
                        m_ar = re.search(r"\[ar:(.*?)\]", lrc_text)
                        if m_ar:
                            artist = m_ar.group(1).strip()
            except Exception:
                pass

            title = title or "Unknown_Song"
            artist = artist or "Unknown_Artist"

            stream_url = None
            ext = "flac"
            download_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            cover_pic_url = ""

            # 1. 优先使用本地导入并已启用的自定义音源 (波点、全豆要、长青等)
            try:
                import native_engine
                src = "wy"
                clean_sid = raw_id
                if clean_sid.startswith("lx:"):
                    clean_sid = clean_sid[len("lx:"):]
                if ":" in clean_sid:
                    p_parts = clean_sid.split(":")
                    if len(p_parts) >= 2:
                        src = p_parts[0]
                        clean_sid = ":".join(p_parts[1:])
                
                target_q = (track_info or {}).get("quality") or (track_info or {}).get("preferred_quality") or "flac"
                
                candidate_metas = [{
                    "id": clean_sid,
                    "songmid": clean_sid,
                    "source": src,
                    "name": title,
                    "singer": artist,
                }]

                # 增强：跨源全网检索（网易云 + 酷我/酷狗/QQ），聚合多源候选，确保必有可用源
                if title and title != "Unknown_Song":
                    try:
                        import netease_api
                        kw_query = f"{title} {artist}".strip()
                        s_res = await netease_api.netease_client.search_songs(kw_query, limit=5)
                        songs_list = s_res if isinstance(s_res, list) else (s_res.get("songs") if isinstance(s_res, dict) else [])
                        for s_item in songs_list:
                            real_wy_id = str(s_item.get("id") or "")
                            if real_wy_id:
                                candidate_metas.append({
                                    "id": real_wy_id,
                                    "songmid": real_wy_id,
                                    "source": "wy",
                                    "name": s_item.get("name") or title,
                                    "singer": s_item.get("artist") or artist,
                                })
                    except Exception as e_srch:
                        logger.debug("search real wy id failed: %s", e_srch)

                    # 补充酷我候选（酷我的无损FLAC直链解析成功率极高）
                    try:
                        q_kw = urllib.parse.quote(f"{title} {artist}".strip())
                        url_kw = f"http://search.kuwo.cn/r.s?client=kt&all={q_kw}&pn=0&rn=5&vipver=1&ft=music&encoding=utf8&rformat=json&mobi=1"
                        r_kw = await client.get(url_kw, headers={"User-Agent": "okhttp/3.10.0"}, timeout=3.0)
                        if r_kw.status_code == 200:
                            import ast
                            try:
                                d_kw = json.loads(r_kw.text)
                            except Exception:
                                d_kw = ast.literal_eval(r_kw.text)
                            for item in (d_kw.get("abslist") or []):
                                rid = str(item.get("MUSICRID") or "").replace("MUSIC_", "")
                                if rid:
                                    candidate_metas.append({
                                        "id": rid,
                                        "songmid": rid,
                                        "source": "kw",
                                        "name": str(item.get("SONGNAME") or title),
                                        "singer": str(item.get("ARTIST") or artist),
                                    })
                    except Exception as e_kw:
                        logger.debug("search kuwo candidate failed: %s", e_kw)

                candidate_urls = []
                encrypted_urls = []
                for song_meta in candidate_metas:
                    for q_try in [target_q, "flac", "320k", "128k"]:
                        nat_res = await native_engine.resolve_music_url_native(song_meta, quality=q_try)
                        if nat_res and nat_res.get("url"):
                            u_test = str(nat_res["url"]).strip()
                            # 过滤已知失效域名、占位或死链
                            if u_test and not u_test.endswith("/None") and "null" not in u_test and "失败" not in u_test and "175.27.166.236" not in u_test and "nxinxz.com" not in u_test:
                                if _encrypted_audio_url(u_test):
                                    if u_test not in encrypted_urls:
                                        encrypted_urls.append(u_test)
                                    logger.info("跳过加密音频流(无法直接播放): %s", u_test[:130])
                                    continue
                                if u_test not in candidate_urls:
                                    candidate_urls.append(u_test)
                                if not stream_url:
                                    stream_url = u_test
                                    ext = nat_res.get("format") or ("flac" if "flac" in u_test else "mp3")
                                    logger.info("download_online_song native_engine matched: %s via %s (id=%s, q=%s)", title, nat_res.get("sourceName"), song_meta.get("id"), q_try)
                                    break
                    if stream_url:
                        break
                if not stream_url and encrypted_urls:
                    candidate_urls.extend(encrypted_urls)
                    logger.warning("全部候选源均为加密音频流，无明文可播直链: %s", title)
            except Exception as e_nat:
                logger.warning("download_online_song native_engine error: %s", e_nat)

            if not stream_url:
                try:
                    r_url = await client.get(f"{lx_service_url}/api/v1/track/url", params={"id": raw_id, "quality": "lossless"})
                    if r_url.status_code == 200:
                        u_data = r_url.json().get("data") or {}
                        stream_url = u_data.get("url")
                        ext = u_data.get("ext") or "flac"
                        if u_data.get("headers"):
                            download_headers = u_data.get("headers")
                except Exception:
                    pass

            # 若落雪源未导入/已停用/无法获取直链，自动无缝接入网易云音乐 API 进行解析与下载
            if not stream_url:
                logger.info("未导入或停用落雪源，无缝接入网易云音乐 API 下载: %s - %s", title, artist)
                try:
                    import netease_api
                    # 若直接包含网易云纯数字 ID，直接调用 get_song_url
                    clean_sid = raw_id
                    if clean_sid.startswith("lx:"):
                        clean_sid = clean_sid[len("lx:"):]
                    if clean_sid.startswith("wy:"):
                        clean_sid = clean_sid[len("wy:"):]
                    
                    if clean_sid.isdigit() and len(clean_sid) >= 4:
                        n_cfg = netease_api.load_netease_config()
                        u_res = await netease_api.netease_client.get_song_url(clean_sid, level=n_cfg.get("quality", "lossless"), cookie=n_cfg.get("cookie", ""))
                        if u_res.get("ok") and u_res.get("url"):
                            stream_url = u_res["url"]
                            ext = u_res.get("type") or "flac"
                            download_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

                    if not stream_url:
                        wy_res = await netease_api.netease_client.resolve_failover_track(
                            song_id=raw_id,
                            title=title,
                            artist=artist
                        )
                        if wy_res.get("ok") and wy_res.get("url"):
                            stream_url = wy_res["url"]
                            ext = wy_res.get("ext") or "flac"
                            download_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                            if wy_res.get("pic_url"):
                                cover_pic_url = wy_res["pic_url"]
                            if wy_res.get("lyric") and not lrc_text.strip():
                                lrc_text = wy_res["lyric"]
                            if wy_res.get("album") and not album:
                                album = wy_res["album"]
                except Exception as e_wy:
                    logger.warning("netease download failover failed: %s", e_wy)

            if not stream_url:
                return {"ok": False, "msg": "所有音源均未获取到可独立播放的明文直链（候选可能全为加密音频流）"}

            base_name = f"{artist} - {title}"
            file_name = f"{base_name}.{ext}"
            file_path = os.path.join(target_dir, file_name)
            part_path = file_path + ".part"
            tmp_part = os.path.join(target_dir, base_name + ".download.part")

            download_ok = False
            urls_to_try = [stream_url]
            if 'candidate_urls' in locals():
                for cu in candidate_urls:
                    if cu not in urls_to_try:
                        urls_to_try.append(cu)

            for cur_u in urls_to_try:
                try:
                    async with client.stream("GET", cur_u, headers=download_headers) as resp:
                        if resp.status_code in (200, 206):
                            with open(tmp_part, "wb") as f:
                                async for chunk in resp.aiter_bytes():
                                    f.write(chunk)
                            _real_ext = sniff_audio_ext(tmp_part)
                            if not _real_ext:
                                logger.warning("拒绝加密/未知音频流(无明文音频头)，尝试下一候选源: %s", cur_u[:140])
                                try:
                                    os.remove(tmp_part)
                                except Exception:
                                    pass
                                continue
                            ext = _real_ext
                            file_name = f"{base_name}.{ext}"
                            file_path = os.path.join(target_dir, file_name)
                            os.replace(tmp_part, file_path)
                            download_ok = True
                            stream_url = cur_u
                            logger.info("下载完成(明文校验通过): %s [%s]", file_path, ext)
                            break
                        else:
                            logger.warning("源站拒绝下载 (HTTP %s): %s", resp.status_code, cur_u)
                except Exception as e_stream:
                    logger.warning("流式下载发生网络异常: %s", e_stream)

            if not download_ok:
                logger.warning("所有本地音源直链均无法下载，尝试网易云API重新解析")
                try:
                    import netease_api
                    wy_res = await netease_api.netease_client.resolve_failover_track(
                        song_id=raw_id,
                        title=title,
                        artist=artist
                    )
                    if wy_res.get("ok") and wy_res.get("url") and wy_res["url"] != stream_url:
                        stream_url = wy_res["url"]
                        ext = wy_res.get("ext") or "flac"
                        file_name = f"{base_name}.{ext}"
                        file_path = os.path.join(target_dir, file_name)
                        part_path = file_path + ".part"
                        async with client.stream("GET", stream_url, headers={"User-Agent": "Mozilla/5.0"}) as resp2:
                            if resp2.status_code in (200, 206):
                                with open(tmp_part, "wb") as f:
                                    async for chunk in resp2.aiter_bytes():
                                        f.write(chunk)
                                _real_ext2 = sniff_audio_ext(tmp_part)
                                if not _real_ext2:
                                    logger.warning("网易云兜底直链仍为加密/未知流，放弃: %s", stream_url[:140])
                                    try:
                                        os.remove(tmp_part)
                                    except Exception:
                                        pass
                                else:
                                    ext = _real_ext2
                                    file_name = f"{base_name}.{ext}"
                                    file_path = os.path.join(target_dir, file_name)
                                    os.replace(tmp_part, file_path)
                                    download_ok = True
                                if wy_res.get("pic_url"):
                                    cover_pic_url = wy_res["pic_url"]
                                if wy_res.get("lyric") and not lrc_text.strip():
                                    lrc_text = wy_res["lyric"]
                except Exception as e_retry:
                    logger.warning("netease retry failed: %s", e_retry)

            if not download_ok or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                for _p in (part_path, tmp_part):
                    try:
                        if os.path.exists(_p):
                            os.remove(_p)
                    except Exception:
                        pass
                return {"ok": False, "msg": "音频下载失败：所有音源均为加密流或源站响应异常（无明文音频）"}

            # 歌词保存与补全：每个通道独立容错，任一命中即落盘 .lrc
            lrc_path = os.path.join(target_dir, f"{base_name}.lrc")

            async def _lyric_from_lx() -> str:
                try:
                    r_lrc = await client.get(f"{lx_service_url}/api/v1/track/lyric", params={"id": raw_id})
                    if r_lrc.status_code == 200:
                        return (r_lrc.json().get("data", {}) or {}).get("lyric") or ""
                except Exception as e:
                    logger.debug("lx lyric channel failed: %s", e)
                return ""

            async def _lyric_from_native() -> str:
                try:
                    import native_engine
                    _sid = raw_id
                    _src = "wy"
                    if _sid.startswith("lx:"):
                        _sid = _sid[len("lx:"):]
                    if ":" in _sid:
                        _parts = _sid.split(":")
                        if len(_parts) >= 2:
                            _src = _parts[0]
                            _sid = ":".join(_parts[1:])
                    _res = await native_engine.resolve_music_lyric_native(
                        {"songmid": _sid, "id": _sid, "name": title, "singer": artist, "source": _src})
                    if _res and _res.get("lyric"):
                        return _res["lyric"]
                except Exception as e:
                    logger.debug("native lyric channel failed: %s", e)
                return ""

            async def _lyric_from_netease() -> tuple:
                try:
                    import netease_api
                    _en = await netease_api.netease_client.enrich_cover_and_lyric(title, artist)
                    if _en.get("ok"):
                        return (_en.get("lyric") or ""), (_en.get("pic_url") or "")
                except Exception as e:
                    logger.debug("netease lyric channel failed: %s", e)
                return "", ""

            if not lrc_text.strip():
                lrc_text = await _lyric_from_lx()
            if not lrc_text.strip():
                lrc_text = await _lyric_from_native()
            if not lrc_text.strip() or not cover_pic_url:
                try:
                    _n_lrc, _n_pic = await _lyric_from_netease()
                    if _n_lrc and not lrc_text.strip():
                        lrc_text = _n_lrc
                    if _n_pic and not cover_pic_url:
                        cover_pic_url = _n_pic
                except Exception as e:
                    logger.debug("netease enrich failed: %s", e)

            if lrc_text.strip():
                try:
                    with open(lrc_path, "w", encoding="utf-8") as lf:
                        lf.write(lrc_text)
                    logger.info("lyrics saved: %s (%d chars)", lrc_path, len(lrc_text))
                except Exception as e_lrc:
                    logger.warning("save lyric error: %s", e_lrc)
            else:
                logger.warning("no lyrics found for %s - %s", artist, title)

            # 写入 ID3 标签与专辑内嵌封面 (自动使用网易云官方高清封面)
            try:
                import mutagen
                from mutagen.flac import FLAC, Picture

                pic_data = b""
                if not cover_pic_url:
                    try:
                        import netease_api
                        en = await netease_api.netease_client.enrich_cover_and_lyric(title, artist)
                        if en.get("ok") and en.get("pic_url"):
                            cover_pic_url = en["pic_url"]
                    except Exception:
                        pass

                if cover_pic_url:
                    try:
                        r_pic = await client.get(cover_pic_url, timeout=8.0)
                        if r_pic.status_code == 200:
                            pic_data = r_pic.content
                    except Exception:
                        pass

                if ext.lower() == "flac":
                    flac_audio = FLAC(file_path)
                    flac_audio["title"] = title
                    flac_audio["artist"] = artist
                    if album:
                        flac_audio["album"] = album
                    if pic_data:
                        pic = Picture()
                        pic.data = pic_data
                        pic.type = 3  # Cover (front)
                        pic.mime = "image/jpeg"
                        flac_audio.clear_pictures()
                        flac_audio.add_picture(pic)
                    flac_audio.save()
                else:
                    audio = mutagen.File(file_path, easy=True)
                    if audio is not None:
                        audio["title"] = title
                        audio["artist"] = artist
                        if album:
                            audio["album"] = album
                        audio.save()
            except Exception as e_tag:
                logger.warning("write tags warning: %s", e_tag)

            file_size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 2)
            return {
                "ok": True,
                "msg": f"成功下载《{title} - {artist}》({ext.upper()}, {file_size_mb}MB)",
                "path": file_path,
                "size_mb": file_size_mb,
            }
        except Exception as e:
            logger.warning("download_online_song error: %s", e)
            return {"ok": False, "msg": f"下载失败: {e}"}


# =====================================================================
# 4. 实时日志读取 (System Logs)
# =====================================================================

def get_recent_logs(lines: int = 100) -> str:
    try:
        res = subprocess.run(
            ["journalctl", "-u", "fnmusic-ext.service", "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.stdout or "暂无日志"
    except Exception as e:
        return f"获取日志失败: {e}"


# =====================================================================
# 5. 现代化 Web 控制台页面渲染 (Console UI - 按照落雪音乐界面全新改版)
# =====================================================================

def render_console_html(stats: dict, settings: dict, playlists: list, favorites_count: int, fn_stats: dict | None = None) -> str:
    if fn_stats is None:
        fn_stats = get_fnmusic_stats()
    user_display = fn_stats.get("user_name") or "管理员"
    if favorites_count <= 0:
        favorites_count = fn_stats.get("favorite_count", 0)
    boards = settings.get("boards") or DEFAULT_BOARDS
    boards_html = ""
    for b in boards:
        checked = "checked" if b.get("enabled") else ""
        boards_html += f"""
        <div class="board-item">
            <label class="switch">
                <input type="checkbox" name="board_{b['id']}" {checked}>
                <span class="slider"></span>
            </label>
            <span class="board-name">{b['name']}</span>
            <span class="badge">{b['source'].upper()}</span>
        </div>
        """

    playlists_html = ""
    if not playlists:
        playlists_html = '<div class="empty-tip">暂无导入或自定义歌单，可通过下方歌单导入管理立即添加！</div>'
    else:
        for p in playlists:
            cover = p.get("cover_url") or "/music/static/assets/img/logo.png"
            playlists_html += f"""
            <div class="pl-card">
                <img src="{cover}" class="pl-cover" alt="" onerror="this.src='/music/static/assets/img/logo.png'">
                <div class="pl-info">
                    <div class="pl-title">{p.get('name')}</div>
                    <div class="pl-meta">{p.get('trackCount', 0)} 首歌曲 · 来自 {str(p.get('source', 'ext')).upper()}</div>
                </div>
                <button class="btn btn-sm btn-danger" onclick="deletePlaylist('{p.get('guid')}')">删除</button>
            </div>
            """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>飞牛音乐 · 后台管理控制台 (LX MUSIC 风格)</title>
    <style>
        :root {{
            --bg-body: #100e17;
            --bg-sidebar: #171422;
            --bg-card: #1f1b2d;
            --bg-card-hover: #29243a;
            --bg-input: #151220;
            --accent: #f62c55;
            --accent-hover: #e02449;
            --accent-blue: #1890ff;
            --text-main: #f0edf6;
            --text-sub: #9a94ab;
            --border: rgba(255, 255, 255, 0.08);
            --radius: 10px;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg-body);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
            display: flex;
            height: 100vh;
            overflow: hidden;
        }}
        /* 经典左侧侧边栏布局（完全对标 LX MUSIC Web） */
        .sidebar {{
            width: 220px;
            background: var(--bg-sidebar);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            user-select: none;
        }}
        .brand {{
            height: 64px;
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 0 20px;
            border-bottom: 1px solid var(--border);
        }}
        .brand-logo {{
            width: 32px;
            height: 32px;
            border-radius: 8px;
            background: linear-gradient(135deg, #f62c55, #ff6b8b);
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 18px;
            color: #fff;
        }}
        .brand-title {{
            font-size: 16px;
            font-weight: 700;
            letter-spacing: 0.5px;
            color: #fff;
        }}
        .brand-ver {{
            font-size: 10px;
            background: rgba(246, 44, 85, 0.18);
            color: var(--accent);
            padding: 1px 6px;
            border-radius: 4px;
            margin-left: auto;
        }}
        .nav-list {{
            flex: 1;
            padding: 16px 10px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}
        .nav-item {{
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 11px 16px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            color: var(--text-sub);
            cursor: pointer;
            transition: all .2s ease;
        }}
        .nav-item:hover {{
            background: rgba(255, 255, 255, 0.05);
            color: #fff;
        }}
        .nav-item.active {{
            background: rgba(246, 44, 85, 0.15);
            color: var(--accent);
            font-weight: 600;
        }}
        .nav-icon {{
            font-size: 17px;
            width: 20px;
            text-align: center;
        }}
        .nav-divider {{
            height: 1px;
            background: var(--border);
            margin: 10px 6px;
        }}
        .sidebar-footer {{
            padding: 16px;
            border-top: 1px solid var(--border);
            font-size: 12px;
            color: var(--text-sub);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        /* 右侧主视窗 */
        .main-container {{
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            background: var(--bg-body);
        }}
        /* 顶部栏 */
        .topbar {{
            height: 64px;
            padding: 0 28px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border);
            background: rgba(16, 14, 23, 0.85);
            backdrop-filter: blur(10px);
        }}
        .topbar-left {{
            display: flex;
            align-items: center;
            gap: 16px;
        }}
        .page-title {{
            font-size: 18px;
            font-weight: 700;
            color: #fff;
        }}
        .topbar-right {{
            display: flex;
            align-items: center;
            gap: 14px;
        }}
        .user-tag {{
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 13px;
        }}
        .content-scroll {{
            flex: 1;
            overflow-y: auto;
            padding: 24px 28px 60px;
        }}
        .tab-content {{
            display: none;
        }}
        .tab-content.active {{
            display: block;
        }}

        /* 卡片与组件 */
        .grid-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 16px 20px;
        }}
        .stat-card .label {{ font-size: 13px; color: var(--text-sub); }}
        .stat-card .value {{ font-size: 24px; font-weight: 700; margin-top: 8px; color: #fff; }}
        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 24px;
            margin-bottom: 24px;
        }}
        .card-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border);
        }}
        .card-title {{ font-size: 16px; font-weight: 600; display: flex; align-items: center; gap: 8px; }}
        .form-group {{ margin-bottom: 20px; }}
        .form-group label {{ display: block; font-size: 13px; color: var(--text-sub); margin-bottom: 8px; font-weight: 500; }}
        .form-group small {{ display: block; font-size: 12px; color: #7f7893; margin-top: 6px; }}
        input[type="text"], input[type="number"], input[type="password"], select, textarea {{
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 14px;
            color: #fff;
            font-size: 14px;
            outline: none;
            transition: border-color .2s, box-shadow .2s;
            box-sizing: border-box;
        }}
        input[type="text"]:focus, input[type="number"]:focus, input[type="password"]:focus, select:focus, textarea:focus {{
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(246, 44, 85, 0.2);
        }}
        .form-control {{
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 14px;
            color: #fff;
            font-size: 14px;
            outline: none;
            transition: border-color .2s, box-shadow .2s;
            box-sizing: border-box;
        }}
        .form-control:focus {{
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(246, 44, 85, 0.2);
        }}
        .form-hint {{ display: block; font-size: 12px; color: #7f7893; margin-top: 6px; }}
        textarea {{ font-family: monospace; font-size: 13px; resize: vertical; min-height: 100px; }}
        .btn {{
            background: var(--accent);
            color: #fff;
            border: none;
            border-radius: 8px;
            padding: 9px 18px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: background .2s, transform .1s;
        }}
        .btn:hover {{ background: var(--accent-hover); }}
        .btn:active {{ transform: scale(0.98); }}
        .btn-sm {{ padding: 6px 12px; font-size: 12px; }}
        .btn-secondary {{ background: #2f2a40; color: #eee; }}
        .btn-secondary:hover {{ background: #3d3753; }}
        .btn-danger {{ background: #dc3545; }}
        .btn-danger:hover {{ background: #bd2130; }}
        .btn-success {{ background: #28a745; }}
        .btn-success:hover {{ background: #218838; }}

        .board-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 12px;
        }}
        .board-item {{
            display: flex;
            align-items: center;
            gap: 12px;
            background: var(--bg-input);
            padding: 12px 16px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .board-name {{ flex: 1; font-size: 14px; font-weight: 500; }}
        .badge {{
            font-size: 11px;
            background: rgba(255,255,255,0.06);
            color: var(--text-sub);
            padding: 2px 6px;
            border-radius: 4px;
        }}
        /* switch */
        .switch {{ position: relative; display: inline-block; width: 42px; height: 22px; flex-shrink: 0; }}
        .switch input {{ opacity: 0; width: 0; height: 0; }}
        .slider {{
            position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
            background-color: #312c40; transition: .3s; border-radius: 22px;
        }}
        .slider:before {{
            position: absolute; content: ""; height: 16px; width: 16px; left: 3px; bottom: 3px;
            background-color: white; transition: .3s; border-radius: 50%;
        }}
        input:checked + .slider {{ background-color: var(--accent); }}
        input:checked + .slider:before {{ transform: translateX(20px); }}
        /* playlist card */
        .pl-card {{
            display: flex;
            align-items: center;
            gap: 14px;
            background: var(--bg-input);
            padding: 10px 14px;
            border-radius: 8px;
            border: 1px solid var(--border);
            margin-bottom: 8px;
        }}
        .pl-cover {{ width: 44px; height: 44px; border-radius: 6px; object-fit: cover; background: #222; }}
        .pl-info {{ flex: 1; }}
        .pl-title {{ font-size: 14px; font-weight: 600; color: #fff; }}
        .pl-meta {{ font-size: 12px; color: var(--text-sub); }}
        .empty-tip {{ color: var(--text-sub); font-size: 13px; text-align: center; padding: 24px; }}
        /* 日志窗口 */
        .log-box {{
            background: #09080e;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px;
            font-family: Consolas, Monaco, "Courier New", monospace;
            font-size: 12px;
            line-height: 1.6;
            color: #d1cddb;
            max-height: 480px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        /* 落雪自定义音源管理 */
        .source-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 16px 18px;
            margin-bottom: 12px;
            display: flex;
            align-items: flex-start;
            gap: 16px;
            transition: all 0.25s ease;
        }}
        .source-card:hover {{
            border-color: rgba(246, 44, 85, 0.35);
            box-shadow: 0 4px 18px rgba(0,0,0,0.3);
        }}
        .source-card.is-disabled {{
            opacity: 0.6;
            background: rgba(23, 20, 34, 0.6);
        }}
        .source-main {{ flex: 1; }}
        .source-header-row {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 6px;
            flex-wrap: wrap;
        }}
        .source-name {{
            font-size: 15px;
            font-weight: 700;
            color: #fff;
        }}
        .source-author {{
            font-size: 12px;
            color: var(--text-sub);
        }}
        .source-desc {{
            font-size: 13px;
            color: var(--text-sub);
            margin: 6px 0 10px 0;
            line-height: 1.5;
        }}
        .source-platforms {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .platform-tag {{
            font-size: 11px;
            font-weight: 600;
            padding: 2px 7px;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.08);
            color: #d1cddb;
            text-transform: uppercase;
        }}
        .platform-tag.kw {{ background: rgba(255, 170, 0, 0.18); color: #ffb822; }}
        .platform-tag.wy {{ background: rgba(230, 0, 38, 0.18); color: #ff4d6a; }}
        .platform-tag.tx {{ background: rgba(30, 204, 148, 0.18); color: #20e0a5; }}
        .platform-tag.kg {{ background: rgba(0, 160, 255, 0.18); color: #38bdf8; }}
        .platform-tag.mg {{ background: rgba(235, 47, 150, 0.18); color: #f472b6; }}
        .platform-tag.bd {{ background: rgba(168, 85, 247, 0.18); color: #c084fc; }}
        .source-actions {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 12px;
            min-width: 90px;
        }}
        .subtab-nav {{
            display: flex;
            gap: 8px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 12px;
            margin-bottom: 16px;
        }}
        .subtab-btn {{
            background: transparent;
            border: 1px solid transparent;
            color: var(--text-sub);
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .subtab-btn.active {{
            background: rgba(246, 44, 85, 0.15);
            color: var(--accent);
            border-color: rgba(246, 44, 85, 0.3);
            font-weight: 600;
        }}
        .quick-item {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--bg-input);
            border: 1px solid var(--border);
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 12px;
            color: var(--text-main);
            cursor: pointer;
            transition: all 0.2s;
            margin: 4px 6px 4px 0;
        }}
        .quick-item:hover {{
            border-color: var(--accent);
            color: var(--accent);
        }}
    </style>
</head>
<body>
    <!-- 左侧导航栏 -->
    <aside class="sidebar">
        <div class="brand">
            <div class="brand-logo">♫</div>
            <div class="brand-title">飞牛音乐</div>
            <span class="brand-ver">LX v2.0.2</span>
        </div>
        <div class="nav-list">
            <div class="nav-item active" onclick="switchTab('overview')">
                <span class="nav-icon">📊</span> 系统概览
            </div>
            <div class="nav-item" onclick="switchTab('custom_source')">
                <span class="nav-icon">📻</span> 落雪音源设置
            </div>
            <div class="nav-item" onclick="switchTab('other_sources')">
                <span class="nav-icon">🎵</span> 其他音源配置
            </div>
            <div class="nav-item" onclick="switchTab('ai_model')">
                <span class="nav-icon">🤖</span> 飞牛音乐AI助手
            </div>
            <div class="nav-item" onclick="switchTab('leaderboards')">
                <span class="nav-icon">🏆</span> 排行榜管理
            </div>
            <div class="nav-item" onclick="switchTab('playlists')">
                <span class="nav-icon">🎵</span> 歌单导入管理
            </div>
            <div class="nav-item" onclick="switchTab('download')">
                <span class="nav-icon">📥</span> 本地与下载缓存
            </div>
            <div class="nav-divider"></div>
            <div class="nav-item" onclick="switchTab('lx_sys')">
                <span class="nav-icon">⚙️</span> 飞牛音乐的系统控制
            </div>
            <div class="nav-item" onclick="switchTab('lx_webdav')">
                <span class="nav-icon">☁️</span> WebDAV 同步
            </div>
            <div class="nav-item" onclick="switchTab('lx_snapshots')">
                <span class="nav-icon">📸</span> 快照管理
            </div>
            <div class="nav-item" onclick="switchTab('lx_data')">
                <span class="nav-icon">🗄️</span> 数据查看
            </div>
            <div class="nav-divider"></div>
            <div class="nav-item" onclick="switchTab('appearance')">
                <span class="nav-icon">🎨</span> 显示 · 外观设置
            </div>
            <div class="nav-item" onclick="switchTab('logs')">
                <span class="nav-icon">📜</span> 实时运行日志
            </div>
            <div class="nav-divider"></div>
            <a href="/music" class="nav-item" style="text-decoration:none;" onclick="return returnToMusic(event);">
                <span class="nav-icon">🚪</span> 返回飞牛音乐
            </a>
        </div>
        <div class="sidebar-footer">
            <span>fnmusic-ext 增强版</span>
            <span style="color:#00e676;">● 运行中</span>
        </div>
    </aside>

    <!-- 主视窗 -->
    <main class="main-container">
        <header class="topbar">
            <div class="topbar-left">
                <h2 class="page-title" id="tabTitle">系统概览</h2>
            </div>
            <div class="topbar-right">
                <div class="user-tag">
                    <span>👤</span>
                    <span>{user_display} (飞牛已授权)</span>
                </div>
                <a href="/music" class="btn btn-secondary btn-sm" style="text-decoration:none;" onclick="return returnToMusic(event);">打开播放器</a>
            </div>
        </header>

        <div class="content-scroll">
            <!-- 1. 系统概览 -->
            <div class="tab-content active" id="tab-overview">
                <div class="grid-stats">
                    <div class="stat-card">
                        <div class="label">官方原生服务 (trim.music)</div>
                        <div class="value" style="color:#00e676;">运行正常 🟢</div>
                        <div style="font-size:12px; color:var(--text-sub); margin-top:5px;">已入库 {fn_stats.get('track_count', 0)} 首 · {fn_stats.get('album_count', 0)} 张专辑</div>
                    </div>
                    <div class="stat-card">
                        <div class="label">飞牛内置音源引擎</div>
                        <div class="value" style="color:#00e676;">独立就绪 🟢</div>
                        <div style="font-size:12px; color:var(--text-sub); margin-top:5px;">酷我/网易云/咪咕多源直连</div>
                    </div>
                    <div class="stat-card">
                        <div class="label">飞牛红心收藏歌曲</div>
                        <div class="value" style="color:#f62c55;">{favorites_count} 首 ❤️</div>
                        <div style="font-size:12px; color:var(--text-sub); margin-top:5px;">与飞牛原生媒体库实时联动</div>
                    </div>
                    <div class="stat-card">
                        <div class="label">本地音频缓存占用</div>
                        <div class="value">{stats.get('total_size_mb', 0)} MB</div>
                        <div style="font-size:12px; color:var(--text-sub); margin-top:5px;">容量上限: {settings.get('max_cache_mb', 5120)} MB</div>
                    </div>
                </div>

                <!-- 飞牛音乐原生媒体库实时联动看板 -->
                <div class="card" style="margin-top:20px;">
                    <div class="card-header" style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                            <div class="card-title">🎵 飞牛音乐原生媒体库联动看板</div>
                            <span style="font-size:12px; color:var(--text-sub);">实时直通飞牛原生 music.db 数据库，全量掌握媒体库曲目、歌手、专辑与收藏动态</span>
                        </div>
                        <div style="display:flex; gap:10px;">
                            <button class="btn btn-secondary btn-sm" onclick="triggerScan()">🔄 立即触发全库重新扫描</button>
                            <a href="/music" class="btn btn-primary btn-sm" style="text-decoration:none;" onclick="return returnToMusic(event);">▶ 打开播放器</a>
                        </div>
                    </div>
                    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:15px; margin-top:15px;">
                        <div style="background:var(--bg-input); padding:14px 16px; border-radius:8px; border:1px solid var(--border);">
                            <div style="font-size:12px; color:var(--text-sub);">媒体库入库曲目</div>
                            <div style="font-size:22px; font-weight:bold; margin-top:6px; color:var(--text-main);">{fn_stats.get('track_count', 0)} <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">首</span></div>
                        </div>
                        <div style="background:var(--bg-input); padding:14px 16px; border-radius:8px; border:1px solid var(--border);">
                            <div style="font-size:12px; color:var(--text-sub);">收录歌手总数</div>
                            <div style="font-size:22px; font-weight:bold; margin-top:6px; color:var(--text-main);">{fn_stats.get('artist_count', 0)} <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">位</span></div>
                        </div>
                        <div style="background:var(--bg-input); padding:14px 16px; border-radius:8px; border:1px solid var(--border);">
                            <div style="font-size:12px; color:var(--text-sub);">收录专辑总数</div>
                            <div style="font-size:22px; font-weight:bold; margin-top:6px; color:var(--text-main);">{fn_stats.get('album_count', 0)} <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">张</span></div>
                        </div>
                        <div style="background:var(--bg-input); padding:14px 16px; border-radius:8px; border:1px solid var(--border);">
                            <div style="font-size:12px; color:var(--text-sub);">累计播放历史</div>
                            <div style="font-size:22px; font-weight:bold; margin-top:6px; color:var(--text-main);">{fn_stats.get('history_count', 0)} <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">次</span></div>
                        </div>
                        <div style="background:var(--bg-input); padding:14px 16px; border-radius:8px; border:1px solid var(--border);">
                            <div style="font-size:12px; color:var(--text-sub);">飞牛红心收藏</div>
                            <div style="font-size:22px; font-weight:bold; margin-top:6px; color:#f62c55;">{favorites_count} <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">首</span></div>
                        </div>
                    </div>
                </div>

                <div class="card" style="margin-top:20px;">
                    <div class="card-header">
                        <div class="card-title">💡 系统状态与架构指南</div>
                    </div>
                    <p style="font-size:14px; line-height:1.8; color:var(--text-sub);">
                        飞牛音乐扩展模块（fnmusic-ext）通过透明劫持 <code>/var/run/trim_music.socket</code>，深度整合飞牛原生服务（trim.music），无缝实现酷我、网易云等高品质无损音源聚合。支持原生红心收藏联动、排行榜注入、歌单导入、无损下载与实时媒体库自动扫描。
                    </p>
                </div>
            </div>

            <!-- 2. 落雪音源设置 (Custom Sources) -->
            <div class="tab-content" id="tab-custom_source">
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">📥 导入落雪自定义音源</div>
                        <span style="font-size:12px; color:var(--text-sub);">音源与系统彻底分离，支持任意导入/切换/删除落雪 JS 脚本</span>
                    </div>

                    <div class="subtab-nav">
                        <button type="button" class="subtab-btn active" id="subtab-btn-url" onclick="switchSourceSubtab('url')">🌐 在线链接导入</button>
                        <button type="button" class="subtab-btn" id="subtab-btn-upload" onclick="switchSourceSubtab('upload')">📁 本地文件上传</button>
                        <button type="button" class="subtab-btn" id="subtab-btn-code" onclick="switchSourceSubtab('code')">📝 代码粘贴导入</button>
                    </div>

                    <!-- 1. 链接导入 -->
                    <div id="source-subtab-url">
                        <div class="form-group">
                            <label>音源脚本网络 URL：</label>
                            <input type="text" id="sourceImportUrl" placeholder="支持 GitHub raw、ghproxy、CDN 等音源 .js 链接">
                            <small>输入落雪规范的自定义源 URL，系统将自动拉取、校验语法并注册进本地音源池。</small>
                        </div>
                        <div style="text-align:right;">
                            <button type="button" class="btn" id="btnImportUrl" onclick="doImportSourceUrl()">立即导入并生效</button>
                        </div>
                    </div>

                    <!-- 2. 文件上传 -->
                    <div id="source-subtab-upload" style="display:none;">
                        <div class="form-group">
                            <label>选择音源脚本文件 (.js)：</label>
                            <input type="file" id="sourceFileInput" accept=".js" style="padding:8px 0;">
                            <small>支持导入符合 LX Music 规范的单文件自定义源脚本。</small>
                        </div>
                        <div style="text-align:right; margin-top:10px;">
                            <button type="button" class="btn" id="btnUploadFile" onclick="doUploadSourceFile()">上传并安装</button>
                        </div>
                    </div>

                    <!-- 3. 代码粘贴 -->
                    <div id="source-subtab-code" style="display:none;">
                        <div class="form-group">
                            <label>音源文件名 (选填)：</label>
                            <input type="text" id="sourceCodeName" placeholder="例如：我的专属音源.js (为空则从代码@name提取)">
                        </div>
                        <div class="form-group">
                            <label>音源 JavaScript 源代码：</label>
                            <textarea id="sourceCodeContent" style="width:100%; height:200px; background:var(--bg-input); border:1px solid var(--border); border-radius:8px; color:var(--text-main); padding:10px; font-family:Consolas, monospace; font-size:12px;" placeholder="粘贴落雪自定义源完整 JS 代码..."></textarea>
                        </div>
                        <div style="text-align:right; margin-top:10px;">
                            <button type="button" class="btn" id="btnSaveCode" onclick="doSaveSourceCode()">保存并安装</button>
                        </div>
                    </div>
                </div>

                <!-- 音源列表卡片 -->
                <div class="card" style="margin-top:20px;">
                    <div class="card-header">
                        <div>
                            <div class="card-title">🎛️ 已安装落雪音源列表</div>
                            <span style="font-size:12px; color:var(--text-sub);" id="sourceCountTip">正在加载音源列表...</span>
                        </div>
                        <div style="display:flex; gap:10px;">
                            <button type="button" class="btn btn-secondary btn-sm" onclick="loadCustomSources()">🔄 刷新列表</button>
                            <button type="button" class="btn btn-secondary btn-sm" onclick="reloadSourceService()">⚡ 重启落雪服务</button>
                        </div>
                    </div>

                    <div id="sourceListContainer" style="margin-top:14px;">
                        <div class="empty-tip">正在获取音源列表...</div>
                    </div>
                </div>
            </div>

            <!-- 2.2 其他音源配置 (网易云音乐 VIP 音源与资源补全) -->
            <div class="tab-content" id="tab-other_sources">
                <div class="card">
                    <div class="card-header">
                        <div>
                            <div class="card-title">🔴 网易云音乐 API (VIP 会员音源与资源补全)</div>
                            <span style="font-size:12px; color:var(--text-sub);">参考 musicbox 核心算法，接入网易云会员音源、无损直链解析与封面歌词补全</span>
                        </div>
                        <div id="neteaseStatusBadge" style="font-size:12px; padding:4px 10px; border-radius:12px; background:rgba(255,255,255,0.08); color:var(--text-sub);">未登录</div>
                    </div>

                    <!-- 用户信息横幅 -->
                    <div id="neteaseUserCard" style="display:flex; align-items:center; gap:16px; padding:16px; background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06); border-radius:8px; margin-bottom:20px;">
                        <img id="neteaseAvatar" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='48' height='48' viewBox='0 0 24 24' fill='none' stroke='%238a839e' stroke-width='1.5'%3E%3Ccircle cx='12' cy='8' r='4'/%3E%3Cpath d='M20 21a8 8 0 1 0-16 0'/%3E%3C/svg%3E" style="width:52px; height:52px; border-radius:50%; object-fit:cover; border:2px solid rgba(255,255,255,0.1); background:#1a1924;">
                        <div style="flex:1;">
                            <div style="display:flex; align-items:center; gap:8px;">
                                <span id="neteaseNickname" style="font-size:15px; font-weight:600; color:#fff;">未登录网易云账号</span>
                                <span id="neteaseVipBadge" class="platform-tag" style="background:rgba(255,255,255,0.1); color:var(--text-sub); font-size:11px;">游客</span>
                            </div>
                            <div id="neteaseVipInfo" style="font-size:12px; color:var(--text-sub); margin-top:4px;">请通过下方扫码或填入 Cookie 完成账号接入，享受黑胶 VIP 无损音质与自动封面歌词补全</div>
                        </div>
                        <div id="neteaseLogoutBtn" style="display:none;">
                            <button type="button" class="btn btn-secondary btn-sm" onclick="logoutNetease()">🚪 退出登录</button>
                        </div>
                    </div>

                    <!-- 接入方式 Subtab 切换 -->
                    <div style="margin-bottom:16px;">
                        <label style="font-weight:600; color:#fff; display:block; margin-bottom:8px;">选择接入方式：</label>
                        <div class="subtab-nav" style="display:flex; gap:8px; border-bottom:1px solid rgba(255,255,255,0.08); padding-bottom:8px;">
                            <button type="button" id="netease-tab-btn-qr" class="btn btn-secondary btn-sm active" onclick="switchNeteaseSubtab('qr')">📱 扫码接入 (推荐)</button>
                            <button type="button" id="netease-tab-btn-cookie" class="btn btn-secondary btn-sm" onclick="switchNeteaseSubtab('cookie')">📋 手动填入 Cookie</button>
                            <button type="button" id="netease-tab-btn-web" class="btn btn-secondary btn-sm" onclick="switchNeteaseSubtab('web')">🌐 网页跳转获取</button>
                        </div>
                    </div>

                    <!-- 方式 1: 扫码接入 -->
                    <div id="netease-subtab-qr" style="padding:12px 0;">
                        <div style="display:flex; gap:20px; align-items:flex-start; flex-wrap:wrap;">
                            <div style="width:160px; height:160px; background:#fff; border-radius:8px; padding:8px; display:flex; align-items:center; justify-content:center; box-shadow:0 4px 12px rgba(0,0,0,0.3);" id="neteaseQrContainer">
                                <div style="color:#666; font-size:12px; text-align:center;">点击下方按钮<br>生成二维码</div>
                            </div>
                            <div style="flex:1; min-width:240px;">
                                <div style="font-size:14px; font-weight:600; color:#fff; margin-bottom:6px;">使用网易云音乐 App 扫码</div>
                                <div id="neteaseQrStatus" style="font-size:13px; color:var(--text-sub); margin-bottom:12px; line-height:1.6;">
                                    打开手机网易云音乐 App ➔ 扫一扫 ➔ 扫描左侧二维码并在手机上确认授权。
                                </div>
                                <div style="display:flex; gap:10px;">
                                    <button type="button" class="btn btn-primary btn-sm" onclick="startNeteaseQrLogin()">✨ 生成登录二维码</button>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- 方式 2: 手动填入 Cookie -->
                    <div id="netease-subtab-cookie" style="display:none; padding:12px 0;">
                        <div class="form-group">
                            <label>网易云 Cookie (包含 MUSIC_U)：</label>
                            <textarea id="neteaseCookieInput" rows="3" placeholder="例如：MUSIC_U=8a798f01...; __csrf=... 或直接填入 MUSIC_U 字段值" style="width:100%; background:var(--bg-input); border:1px solid rgba(255,255,255,0.1); border-radius:6px; color:#fff; padding:8px; font-family:monospace; font-size:12px;"></textarea>
                            <small>建议填入完整 Cookie 或核心授权凭据 <code>MUSIC_U</code>。保存后系统将自动联网验证有效性。</small>
                        </div>
                        <div style="display:flex; gap:10px;">
                            <button type="button" class="btn btn-primary btn-sm" onclick="saveNeteaseManualCookie()">💾 保存并验证 Cookie</button>
                        </div>
                    </div>

                    <!-- 方式 3: 网页跳转获取 -->
                    <div id="netease-subtab-web" style="display:none; padding:12px 0;">
                        <div style="font-size:13px; color:var(--text-sub); line-height:1.8;">
                            1. 点击 <a href="https://music.163.com/#/login" target="_blank" style="color:var(--accent); text-decoration:underline;">🌐 网易云音乐官网登录</a> 在新页面中完成账号登录；<br>
                            2. 登录成功后在浏览器按 <code>F12</code> 打开开发者工具；<br>
                            3. 切换至 <b>Application (应用程序) ➔ Cookies ➔ https://music.163.com</b>；<br>
                            4. 找到 <code>MUSIC_U</code> 项并复制其 Value 值；<br>
                            5. 切换回【手动填入 Cookie】粘贴保存即可！
                        </div>
                    </div>

                    <div class="nav-divider" style="margin:20px 0;"></div>

                    <!-- 音质与资源补全设置 -->
                    <div style="font-weight:600; color:#fff; margin-bottom:12px;">网易云音源与资源补全偏好：</div>
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:16px;">
                        <div class="form-group">
                            <label>音质偏好：</label>
                            <select id="neteaseQuality" onchange="onNeteaseQualityChange()">
                                <optgroup label="👑 黑胶VIP 音质">
                                    <option value="lossless">🎵 无损品质 (FLAC / Hi-Res / 990Kbps+)</option>
                                    <option value="exhigh">🎧 极高品质 (320Kbps MP3)</option>
                                    <option value="standard">📻 标准品质 (128Kbps MP3)</option>
                                </optgroup>
                                <optgroup id="neteaseSvipOptGroup" label="✨ 黑胶SVIP 专属旗舰音质 (需开启下方SVIP选项)">
                                    <option value="jymaster" class="svip-opt">💎 超清母带 (Master / 最高 192kHz/24bit)</option>
                                    <option value="sky" class="svip-opt">🌌 沉浸环绕声 (Surround Audio / 全景声效)</option>
                                    <option value="jyeffect" class="svip-opt">✨ 高清臻音 (HD Spatial Audio)</option>
                                </optgroup>
                            </select>
                            <small id="neteaseQualityHint" style="color:var(--text-sub); display:block; margin-top:4px;">黑胶 VIP 默认支持标准、极高与无损品质</small>
                        </div>
                        <div class="form-group" style="display:flex; flex-direction:column; justify-content:center; gap:8px;">
                            <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                                <input type="checkbox" id="neteaseEnabled" style="width:16px; height:16px; accent-color:var(--accent);" onchange="onNeteaseVipToggle()">
                                <span style="font-weight:600; color:#fff;">启用网易云音乐 VIP 音源</span>
                            </label>
                            <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                                <input type="checkbox" id="neteaseSvipEnabled" style="width:16px; height:16px; accent-color:#ff9100;" onchange="onNeteaseSvipToggle()">
                                <span style="font-weight:600; color:#ffb300;">启用网易云音乐 SVIP 音源及偏好选项</span>
                            </label>
                            <small style="color:var(--text-sub); margin-left:24px;">开启 SVIP 专属选项后，方可选择超清母带、沉浸环绕声及高清臻音</small>
                        </div>
                    </div>

                    <div class="form-group">
                        <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                            <input type="checkbox" id="neteaseFailover" style="width:16px; height:16px; accent-color:var(--accent);" checked>
                            <span style="font-weight:600; color:#fff;">落雪源无法解析时使用网易云音乐 API 解析播放与下载</span>
                        </label>
                        <small style="margin-left:24px;">播放在线音乐或下载单曲时，若落雪自定义源解析失败、404 或无版权，自动故障转移至网易云千万级官方曲库解析播放直链及下载 FLAC 无损音频。</small>
                    </div>

                    <div class="form-group">
                        <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                            <input type="checkbox" id="neteaseAutoEnrich" style="width:16px; height:16px; accent-color:var(--accent);" checked>
                            <span style="font-weight:600; color:#fff;">自动使用网易云封面与逐字 LRC 歌词补全缺失资源</span>
                        </label>
                        <small style="margin-left:24px;">当本地入库音乐或其它在线音源缺少专辑封面或 LRC 歌词时，自动从网易云千万级官方曲库精准抓取并写入缓存及音频标签。</small>
                    </div>

                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:20px;">
                        <button type="button" class="btn btn-secondary" onclick="testNeteaseConnection()">
                            <span id="neteaseTestIcon">⚡</span> 一键测试网易云连通性与 VIP/SVIP 音源解析
                        </button>
                        <button type="button" class="btn btn-primary" onclick="saveNeteaseSettings()">💾 保存音源配置</button>
                    </div>

                    <div id="neteaseTestResultBox" style="display:none; margin-top:14px; padding:12px; background:rgba(0,0,0,0.25); border-radius:6px; font-size:12px; font-family:monospace; line-height:1.6;"></div>
                </div>
            </div>

            <!-- 2.5 飞牛音乐AI助手 -->
            <div class="tab-content" id="tab-ai_model">
                <div class="card">
                    <div class="card-header">
                        <div>
                            <div class="card-title">🤖 飞牛音乐AI助手 (AI Music Assistant)</div>
                            <span style="font-size:12px; color:var(--text-sub);">配置用于分析听歌偏好与生成心动歌曲推荐的大语言模型服务</span>
                        </div>
                        <div id="aiStatusBadge" style="font-size:12px; padding:4px 10px; border-radius:12px; background:rgba(255,255,255,0.08); color:var(--text-sub);">未检测</div>
                    </div>

                    <div class="form-group">
                        <label>模型显示名称：</label>
                        <input type="text" id="aiModelName" placeholder="例如：本地网关 / DeepSeek / 商汤日日新">
                        <small>便于识别和切换的友好显示名称</small>
                    </div>

                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:16px;">
                        <div class="form-group">
                            <label>Model ID (模型标识)：</label>
                            <input type="text" id="aiModelId" placeholder="例如：DeepSeek-V4.1-Flash">
                        </div>
                        <div class="form-group">
                            <label>Default Model (默认模型)：</label>
                            <input type="text" id="aiDefaultModel" placeholder="例如：DeepSeek-V4.1-Flash">
                        </div>
                    </div>

                    <div style="display:grid; grid-template-columns: 2fr 1fr; gap:16px;">
                        <div class="form-group">
                            <label>API Base URL (端点接口)：</label>
                            <input type="text" id="aiBaseUrl" placeholder="例如：https://api.deepseek.com/v1 或 https://api.openai.com/v1">
                            <small>兼容 OpenAI /v1/chat/completions 标准协议的 API 地址</small>
                        </div>
                        <div class="form-group">
                            <label>Context (上下文长度)：</label>
                            <input type="number" id="aiContext" placeholder="4096" value="4096">
                            <small>模型支持的最大上下文 Tokens 数</small>
                        </div>
                    </div>

                    <div class="form-group">
                        <label>API Key (密钥)：</label>
                        <div style="display:flex; gap:10px;">
                            <input type="password" id="aiApiKey" placeholder="请输入 API Key（若留空则保持已保存密钥不变）" style="flex:1;">
                            <button type="button" class="btn btn-secondary btn-sm" onclick="clearAiApiKey()" style="flex-shrink:0;">清空密钥</button>
                        </div>
                        <small id="aiKeyTip" style="color:var(--text-sub);">未配置密钥</small>
                    </div>

                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:20px; flex-wrap:wrap; gap:12px;">
                        <div style="display:flex; gap:8px; flex-wrap:wrap;">
                            <button type="button" class="btn btn-secondary btn-sm" onclick="setAiPreset('deepseek')">DeepSeek官方</button>
                            <button type="button" class="btn btn-secondary btn-sm" onclick="setAiPreset('sensenova')">商汤日日新</button>
                            <button type="button" class="btn btn-secondary btn-sm" onclick="setAiPreset('openai')">OpenAI官方</button>
                        </div>
                        <div style="display:flex; gap:10px;">
                            <button type="button" class="btn btn-secondary" id="btnTestAi" onclick="testAiConfig()">⚡ 模型连通性检测</button>
                            <button type="button" class="btn" id="btnSaveAi" onclick="saveAiConfig()">💾 保存配置</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 3. 排行榜管理 -->
            <div class="tab-content" id="tab-leaderboards">
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">🏆 飞牛音乐官方排行榜歌单注入</div>
                        <span style="font-size:12px; color:var(--text-sub);">勾选的榜单将直接同步在飞牛 App / Web 的「歌单」中</span>
                    </div>
                    <form id="boardForm">
                        <div class="board-grid">
                            {boards_html}
                        </div>
                        <div style="margin-top:20px; text-align:right;">
                            <button type="button" class="btn" onclick="saveBoardSettings()">保存排行榜配置</button>
                        </div>
                    </form>
                </div>
            </div>

            <!-- 3. 歌单导入管理 -->
            <div class="tab-content" id="tab-playlists">
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">🎵 歌单导入管理（网易云 / 酷我 / QQ 歌单）</div>
                    </div>
                    <div class="form-group">
                        <label>输入外部歌单分享链接或纯数字歌单 ID：</label>
                        <div style="display:flex; gap:10px;">
                            <input type="text" id="importUrl" placeholder="如：https://music.163.com/#/playlist?id=3778678 或 3778678">
                            <input type="text" id="importName" placeholder="自定义名称（选填）" style="max-width:200px;">
                            <button class="btn" style="flex-shrink:0;" onclick="doImport()">立即导入</button>
                        </div>
                        <small>导入后将自动提取完整曲目、专辑封面与歌手信息，飞牛音乐全平台立即可见！</small>
                    </div>
                    <div style="margin-top:24px;">
                        <label style="font-size:13px; color:var(--text-sub); margin-bottom:10px; display:block;">已导入与自定义歌单：</label>
                        <div id="playlistContainer">
                            {playlists_html}
                        </div>
                    </div>
                </div>
            </div>

            <!-- 4. 本地音乐与下载/缓存中心 -->
            <div class="tab-content" id="tab-download">
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">📥 本地下载与缓存维护中心</div>
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px;">
                        <div>
                            <div class="form-group">
                                <label>NAS 音乐库一键下载保存目录：</label>
                                <input type="text" id="downloadDir" value="{settings.get('download_dir', '')}">
                                <small>在线歌曲点击下载后将保存为标准 FLAC 文件并写入 ID3 标签与同名 LRC 歌词。</small>
                            </div>
                            <div class="form-group">
                                <label>最大缓存容量上限 (MB)：</label>
                                <input type="number" id="maxCacheMb" value="{settings.get('max_cache_mb', 5120)}">
                            </div>
                            <button class="btn" onclick="saveBasicSettings()">保存下载与缓存设置</button>
                        </div>
                        <div>
                            <div class="form-group">
                                <label>试听缓存概览：</label>
                                <p style="font-size:13px; color:var(--text-sub); margin-bottom:16px;">当前已无感离线分片缓存 {stats.get('file_count', 0)} 个音频文件，共计 {stats.get('total_size_mb', 0)} MB。</p>
                                <div style="display:flex; gap:10px;">
                                    <button class="btn btn-secondary" onclick="clearCache()">一键清空试听缓存</button>
                                    <button class="btn btn-success" onclick="triggerScan()">触发飞牛扫描媒体库</button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 7.1 飞牛音乐的系统控制 (LX System Config) -->
            <div class="tab-content" id="tab-lx_sys">
                <div class="card">
                    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                        <div>
                            <div class="card-title">飞牛音乐的系统控制</div>
                            <span style="font-size:12px;color:var(--text-sub);">深度结合飞牛音乐核心服务，配置广播服务名、列表行为、网络代理、下载存储及媒体库自动同步</span>
                        </div>
                        <button class="btn btn-primary btn-sm" onclick="saveLxSysConfig()">💾 保存配置并应用</button>
                    </div>
                    <div class="card-body">
                        <div class="settings-grid">
                            <div class="form-group">
                                <label class="form-label">服务名称 (serverName)</label>
                                <input type="text" id="lxCfgServerName" class="form-control" placeholder="飞牛音乐">
                                <span class="form-hint">飞牛音乐扩展对外广播与系统识别名称</span>
                            </div>
                            <div class="form-group">
                                <label class="form-label">快照保留数量 (maxSnapshotNum)</label>
                                <input type="number" id="lxCfgMaxSnapshots" class="form-control" placeholder="10" min="1" max="100">
                                <span class="form-hint">历史快照版本保留上限，超出自动清理旧版本</span>
                            </div>
                            <div class="form-group">
                                <label class="form-label">添加音乐默认位置</label>
                                <select id="lxCfgAddLocation" class="form-control">
                                    <option value="top">列表顶部 (Top)</option>
                                    <option value="bottom">列表底部 (Bottom)</option>
                                </select>
                                <span class="form-hint">新添加或下载歌曲加入列表/队列时的默认插入位置</span>
                            </div>
                            <div class="form-group">
                                <label class="form-label">反向代理穿透设置</label>
                                <div style="display:flex;align-items:center;gap:10px;margin-top:6px;">
                                    <input type="checkbox" id="lxCfgProxyEnabled" style="width:18px;height:18px;">
                                    <label for="lxCfgProxyEnabled" style="margin:0;cursor:pointer;font-size:13px;color:#cbd5e1;">启用反代真实 IP 透传</label>
                                </div>
                                <span class="form-hint">通过 FRP/Nginx 等外网反代访问时识别真实客户端 IP</span>
                            </div>
                            <div class="form-group">
                                <label class="form-label">代理 IP 请求头 (proxy.header)</label>
                                <input type="text" id="lxCfgProxyHeader" class="form-control" placeholder="x-real-ip">
                                <span class="form-hint">指定反代透传真实 IP 的 HTTP 请求头</span>
                            </div>
                            <div class="form-group">
                                <label class="form-label">用户自定义音乐目录</label>
                                <div style="display:flex;align-items:center;gap:10px;margin-top:6px;">
                                    <input type="checkbox" id="lxCfgUserPath" style="width:18px;height:18px;" onchange="toggleCustomPathInput()">
                                    <label for="lxCfgUserPath" style="margin:0;cursor:pointer;font-size:13px;color:#cbd5e1;">允许配置独立下载存储路径</label>
                                </div>
                                <span class="form-hint">启用后下载歌曲将直接落盘至指定的 NAS 媒体目录</span>
                            </div>
                            <div class="form-group" id="customPathGroup" style="grid-column: 1 / -1;">
                                <label class="form-label">自定义音乐下载与入库目录 (Download Dir)</label>
                                <input type="text" id="lxCfgDownloadDir" class="form-control" placeholder="留空默认自动探测飞牛本地音乐库目录">
                                <span class="form-hint">当前下载歌曲物理保存路径（支持 CloudDrive2、OpenList 挂载点或 NAS 本地卷）</span>
                            </div>
                            <div class="form-group" style="grid-column: 1 / -1;">
                                <div style="display:flex;align-items:center;gap:10px;">
                                    <input type="checkbox" id="lxCfgAutoScan" style="width:18px;height:18px;">
                                    <label for="lxCfgAutoScan" style="margin:0;cursor:pointer;font-size:13px;color:#cbd5e1;font-weight:600;">⚡ 歌曲下载完成后自动触发飞牛音乐原生全库扫描 (Auto Scan)</label>
                                </div>
                                <span class="form-hint">无需前往飞牛系统手动点击重新扫描，无损音频落盘后自动通知飞牛原生服务完成入库更新</span>
                            </div>
                        </div>

                        <!-- 飞牛系统快捷操作区 -->
                        <div style="margin-top:20px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.06);">
                            <div style="font-size:13px;font-weight:600;color:#fff;margin-bottom:10px;">🛠️ 飞牛音乐原生系统联动控制</div>
                            <div style="display:flex;flex-wrap:wrap;gap:12px;">
                                <button type="button" class="btn btn-secondary" onclick="triggerFnScanNow()" style="padding:8px 16px;">
                                    🔄 立即全量扫描飞牛音乐媒体库
                                </button>
                                <button type="button" class="btn btn-secondary" onclick="reloadFnExtService()" style="padding:8px 16px;">
                                    ⚡ 重新加载飞牛音乐扩展引擎
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 7.2 WebDAV 同步 (LX WebDAV Sync 对标落雪原生面板) -->
            <div class="tab-content" id="tab-lx_webdav">
                <div class="card">
                    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                        <div>
                            <div class="card-title">☁️ WebDAV 同步与云端/本地备份</div>
                            <span style="font-size:12px;color:var(--text-sub);">支持坚果云、Alist、群晖或自建 WebDAV 网关的双向同步、云端备份与本地 ZIP 还原</span>
                        </div>
                        <button class="btn btn-primary btn-sm" onclick="saveLxWebdavConfig()">💾 保存服务配置</button>
                    </div>
                    <div class="card-body">
                        <!-- 1. 配置项网格 -->
                        <div class="settings-grid">
                            <div class="form-group">
                                <label class="form-label">WebDAV 服务器地址 (URL)</label>
                                <input type="text" id="lxWdUrl" class="form-control" placeholder="例如：http://192.168.1.100:5244/dav">
                                <span class="form-hint">支持 HTTP/HTTPS，例如 http://ip:5544/dav</span>
                            </div>
                            <div class="form-group">
                                <label class="form-label">登录用户名</label>
                                <input type="text" id="lxWdUser" class="form-control" placeholder="username">
                            </div>
                            <div class="form-group">
                                <label class="form-label">登录密码 / 访问凭证</label>
                                <input type="password" id="lxWdPass" class="form-control" placeholder="••••••••">
                            </div>
                            <div class="form-group">
                                <label class="form-label">云端备份目录 (Path)</label>
                                <input type="text" id="lxWdPath" class="form-control" placeholder="例如：/backup/music">
                            </div>
                            <div class="form-group">
                                <label class="form-label">定时自动备份</label>
                                <div style="display:flex;align-items:center;gap:10px;margin-top:6px;">
                                    <input type="checkbox" id="lxWdAutoBackup" style="width:18px;height:18px;">
                                    <label for="lxWdAutoBackup" style="margin:0;cursor:pointer;font-size:13px;color:#cbd5e1;">启用每天定时全量备份至 WebDAV</label>
                                </div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">定时周期 (Cron)</label>
                                <input type="text" id="lxWdCron" class="form-control" placeholder="0 4 * * *">
                            </div>
                        </div>

                        <!-- 2. 功能操作区 (对标落雪云端操作与本地备份) -->
                        <div style="margin-top:24px;border-top:1px solid rgba(255,255,255,0.06);padding-top:20px;">
                            <div style="font-size:14px;font-weight:600;color:#fff;margin-bottom:12px;">云端操作 (Cloud Operations)</div>
                            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;">
                                <button type="button" class="btn btn-secondary" onclick="testLxWebdav()" style="padding:12px;text-align:left;display:flex;align-items:center;gap:10px;">
                                    <span style="font-size:20px;">⚡</span>
                                    <div>
                                        <div style="font-weight:600;font-size:13px;">测试连接</div>
                                        <div style="font-size:11px;color:#94a3b8;">验证地址与鉴权有效性</div>
                                    </div>
                                </button>
                                <button type="button" class="btn btn-secondary" onclick="triggerLxWebdavBackup()" style="padding:12px;text-align:left;display:flex;align-items:center;gap:10px;">
                                    <span style="font-size:20px;">📦</span>
                                    <div>
                                        <div style="font-weight:600;font-size:13px;">立即备份</div>
                                        <div style="font-size:11px;color:#94a3b8;">打包全量 ZIP 并上传云端</div>
                                    </div>
                                </button>
                                <button type="button" class="btn btn-secondary" onclick="triggerLxWebdavRestore()" style="padding:12px;text-align:left;display:flex;align-items:center;gap:10px;">
                                    <span style="font-size:20px;">☁️</span>
                                    <div>
                                        <div style="font-weight:600;font-size:13px;">从云端恢复</div>
                                        <div style="font-size:11px;color:#94a3b8;">下载最新备份覆盖还原</div>
                                    </div>
                                </button>
                                <button type="button" class="btn btn-secondary" onclick="triggerLxWebdavSync()" style="padding:12px;text-align:left;display:flex;align-items:center;gap:10px;">
                                    <span style="font-size:20px;">🔄</span>
                                    <div>
                                        <div style="font-weight:600;font-size:13px;">同步文件</div>
                                        <div style="font-size:11px;color:#94a3b8;">强制同步所有数据文件</div>
                                    </div>
                                </button>
                            </div>
                        </div>

                        <!-- 3. 本地备份区 -->
                        <div style="margin-top:24px;border-top:1px solid rgba(255,255,255,0.06);padding-top:20px;">
                            <div style="font-size:14px;font-weight:600;color:#fff;margin-bottom:12px;">本地备份与还原 (Local Backup & Restore)</div>
                            <div style="display:flex;gap:12px;flex-wrap:wrap;">
                                <a href="/music/ext/api/lx/backup/download" class="btn btn-secondary" style="text-decoration:none;padding:10px 16px;display:flex;align-items:center;gap:8px;">
                                    <span>💾</span>
                                    <span>本地备份 (下载 ZIP 包)</span>
                                </a>
                                <button type="button" class="btn btn-secondary" onclick="document.getElementById('lxLocalBackupInput').click()" style="padding:10px 16px;display:flex;align-items:center;gap:8px;">
                                    <span>📤</span>
                                    <span>本地恢复 (上传 ZIP 还原)</span>
                                </button>
                                <input type="file" id="lxLocalBackupInput" accept=".zip" style="display:none;" onchange="handleLxLocalRestore(event)">
                            </div>
                        </div>

                        <!-- 4. 同步状态视窗 -->
                        <div style="margin-top:24px;border-top:1px solid rgba(255,255,255,0.06);padding-top:20px;">
                            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                                <span style="font-size:13px;font-weight:600;color:#94a3b8;">WebDAV 同步与执行日志</span>
                                <span id="lxWdStatusText" style="font-size:12px;color:#38bdf8;">就绪</span>
                            </div>
                            <div class="log-box" id="lxWdLogs" style="height:120px;">正在获取同步日志...</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 7.3 快照管理 (LX Snapshot Management) -->
            <div class="tab-content" id="tab-lx_snapshots">
                <div class="card">
                    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                        <div class="card-title">歌单历史快照列表 (自动保留多版本)</div>
                        <div style="display:flex;align-items:center;gap:10px;">
                            <span style="font-size:13px;color:#94a3b8;">选择用户:</span>
                            <select id="lxSnapshotUser" class="form-control" style="width:140px;padding:4px 8px;font-size:13px;" onchange="loadLxSnapshots()">
                                
                                <option value="admin">admin</option>
                            </select>
                            <button class="btn btn-secondary btn-sm" onclick="loadLxSnapshots()">🔄 刷新列表</button>
                        </div>
                    </div>
                    <div class="card-body">
                        <div style="overflow-x:auto;">
                            <table style="width:100%;border-collapse:collapse;font-size:13px;text-align:left;">
                                <thead>
                                    <tr style="border-bottom:1px solid rgba(255,255,255,0.08);color:#94a3b8;">
                                        <th style="padding:10px;">快照标识 (ID)</th>
                                        <th style="padding:10px;">生成时间</th>
                                        <th style="padding:10px;">文件大小</th>
                                        <th style="padding:10px;text-align:right;">操作</th>
                                    </tr>
                                </thead>
                                <tbody id="lxSnapshotList">
                                    <tr><td colspan="4" style="padding:20px;text-align:center;color:#64748b;">正在加载快照数据...</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 7.4 数据查看 (LX Data & Songs) -->
            <div class="tab-content" id="tab-lx_data">
                <div class="card">
                    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                        <div class="card-title">同步歌曲与歌单数据一览</div>
                        <div style="display:flex;align-items:center;gap:10px;">
                            <span style="font-size:13px;color:#94a3b8;">用户:</span>
                            <select id="lxDataUser" class="form-control" style="width:140px;padding:4px 8px;font-size:13px;" onchange="loadLxData()">
                                
                                <option value="admin">admin</option>
                            </select>
                            <button class="btn btn-secondary btn-sm" onclick="loadLxData()">🔄 重新加载</button>
                        </div>
                    </div>
                    <div class="card-body">
                        <div style="display:flex;gap:20px;min-height:360px;">
                            <!-- 歌单列表侧栏 -->
                            <div style="width:240px;border-right:1px solid rgba(255,255,255,0.08);padding-right:15px;" id="lxPlaylistTabs">
                                <div style="color:#94a3b8;font-size:12px;margin-bottom:10px;">歌单列表</div>
                                <div id="lxPlaylistsContainer">加载中...</div>
                            </div>
                            <!-- 歌曲明细视窗 -->
                            <div style="flex:1;overflow-x:auto;">
                                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                                    <h4 id="lxCurrentPlaylistName" style="margin:0;font-size:15px;color:#fff;">全部默认列表</h4>
                                    <span id="lxSongCountBadge" class="badge" style="background:rgba(255,255,255,0.1);color:#38bdf8;">0 首歌曲</span>
                                </div>
                                <table style="width:100%;border-collapse:collapse;font-size:13px;text-align:left;">
                                    <thead>
                                        <tr style="border-bottom:1px solid rgba(255,255,255,0.08);color:#94a3b8;">
                                            <th style="padding:8px;">序号</th>
                                            <th style="padding:8px;">歌曲名称</th>
                                            <th style="padding:8px;">歌手</th>
                                            <th style="padding:8px;">音源</th>
                                            <th style="padding:8px;">时长</th>
                                        </tr>
                                    </thead>
                                    <tbody id="lxSongListTable">
                                        <tr><td colspan="5" style="padding:20px;text-align:center;color:#64748b;">请选择左侧歌单查看曲目</td></tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 5. 显示 · 外观设置 (对标 LX MUSIC Web) -->
            <div class="tab-content" id="tab-appearance">
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">🎨 显示与外观定制 (Appearance)</div>
                    </div>
                    <div class="form-group">
                        <label>主题色彩模式 (Theme Mode)：</label>
                        <select id="themeMode">
                            <option value="dark" {"selected" if settings.get('theme_mode') == 'dark' else ""}>极夜暗黑 (LX Dark - 默认推荐)</option>
                            <option value="black" {"selected" if settings.get('theme_mode') == 'black' else ""}>纯黑极简 (OLED Pure Black)</option>
                            <option value="auto" {"selected" if settings.get('theme_mode') == 'auto' else ""}>跟随系统浅色/深色切换</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>主色调高亮颜色 (Accent Color)：</label>
                        <div style="display:flex; gap:12px; align-items:center;">
                            <input type="text" id="accentColor" value="{settings.get('accent_color', '#f62c55')}" style="max-width:160px;">
                            <button class="btn btn-sm btn-secondary" onclick="setAccent('#f62c55')">飞牛红/粉</button>
                            <button class="btn btn-sm btn-secondary" onclick="setAccent('#1890ff')">洛雪蓝</button>
                            <button class="btn btn-sm btn-secondary" onclick="setAccent('#00e676')">律动绿</button>
                            <button class="btn btn-sm btn-secondary" onclick="setAccent('#faad14')">琥珀黄</button>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>显示控制：</label>
                        <div style="display:flex; flex-direction:column; gap:10px; margin-top:8px;">
                            <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                                <input type="checkbox" id="showQualityBadge" {"checked" if settings.get('show_quality_badge', True) else ""}>
                                <span>在播放界面显示无损音质标签（如无损 FLAC、320k 徽章）</span>
                            </label>
                            <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                                <input type="checkbox" id="enableSmoothScroll" {"checked" if settings.get('enable_smooth_scroll', True) else ""}>
                                <span>启用高刷平滑滚动与歌词平滑缓动动画</span>
                            </label>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>自定义 CSS 样式覆盖 (Custom CSS)：</label>
                        <textarea id="customCss" placeholder="/* 在此处输入自定义 CSS，保存后立即注入飞牛 Web 页面 */">{settings.get('custom_css', '')}</textarea>
                    </div>
                    <button class="btn" onclick="saveAppearanceSettings()">保存外观设置</button>
                </div>
            </div>

            <!-- 7. 实时运行日志 -->
            <div class="tab-content" id="tab-logs">
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">📜 飞牛音乐扩展实时运行日志</div>
                        <div>
                            <button class="btn btn-secondary btn-sm" onclick="refreshLogs()">刷新日志</button>
                        </div>
                    </div>
                    <div class="log-box" id="logContent">正在拉取系统实时日志...</div>
                </div>
            </div>
        </div>
    </main>

    <script>
        function returnToMusic(e) {{
            if (e) {{
                e.preventDefault();
                e.stopPropagation();
            }}
            // 1. 如果是从飞牛音乐标签页点进来的 (拥有 window.opener)，激活并聚焦原音乐窗口，然后关闭当前后台标签页
            if (window.opener && !window.opener.closed) {{
                try {{
                    window.opener.focus();
                    window.close();
                    return false;
                }} catch (err) {{
                    console.log("opener focus error:", err);
                }}
            }}
            // 2. 如果当前窗口历史有上一页来自 /music，直接后退即可回到原音乐界面
            if (document.referrer && document.referrer.includes('/music') && !document.referrer.includes('/music/ext')) {{
                window.history.back();
                return false;
            }}
            // 3. 命名窗口导航：使用指定的 target 名称导航，防止盲目创建新窗口
            const win = window.open('/music', 'FN_MUSIC_MAIN_WINDOW');
            if (win) {{
                win.focus();
            }} else {{
                window.location.href = '/music';
            }}
            return false;
        }}

        function switchTab(tabId) {{
            document.querySelectorAll('.nav-list .nav-item').forEach(el => {{
                if (el.getAttribute('onclick') && el.getAttribute('onclick').includes(`'${{tabId}}'`)) {{
                    el.classList.add('active');
                }} else {{
                    el.classList.remove('active');
                }}
            }});
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            const targetContent = document.getElementById('tab-' + tabId);
            if (targetContent) targetContent.classList.add('active');
            
            const titles = {{
                'overview': '系统概览',
                'custom_source': '落雪音源设置',
                'other_sources': '其他音源配置',
                'ai_model': '飞牛音乐AI助手',
                'leaderboards': '排行榜管理',
                'playlists': '歌单导入管理',
                'download': '本地音乐与下载缓存',
                'lx_sys': '飞牛音乐的系统控制',
                'lx_webdav': 'WebDAV 备份与同步',
                'lx_snapshots': '歌单快照管理',
                'lx_data': '同步数据查看与歌曲管理',
                'appearance': '显示 · 外观设置',
                'logs': '实时运行日志'
            }};
            document.getElementById('tabTitle').textContent = titles[tabId] || '控制中心';
            
            if (tabId === 'logs') {{
                refreshLogs();
            }}
            if (tabId === 'custom_source') {{
                loadCustomSources();
            }}
            if (tabId === 'lx_sys') {{
                loadLxSysConfig();
            }}
            if (tabId === 'lx_webdav') {{
                loadLxWebdavConfig();
            }}
            if (tabId === 'lx_snapshots') {{
                loadLxSnapshots();
            }}
            if (tabId === 'lx_data') {{
                loadLxData();
            }}
            if (tabId === 'other_sources') {{
                loadNeteaseConfig();
            }}
            if (tabId === 'ai_model') {{
                loadAiConfig();
            }}
            if (tabId === 'playlists') {{
                loadCustomPlaylists();
            }}
            event?.currentTarget?.classList?.add('active');
        }}

        function switchSourceSubtab(type) {{
            ['url', 'upload', 'code'].forEach(t => {{
                const el = document.getElementById('source-subtab-' + t);
                const btn = document.getElementById('subtab-btn-' + t);
                if (el) el.style.display = (t === type) ? 'block' : 'none';
                if (btn) {{
                    if (t === type) btn.classList.add('active');
                    else btn.classList.remove('active');
                }}
            }});
        }}

        async function loadCustomSources() {{
            const tip = document.getElementById('sourceCountTip');
            const container = document.getElementById('sourceListContainer');
            if (tip) tip.textContent = '正在获取音源列表...';
            try {{
                const res = await fetch('/music/ext/api/custom_source/list');
                const data = await res.json();
                if (data.code === 0 && Array.isArray(data.data)) {{
                    renderSourceCards(data.data);
                }} else {{
                    container.innerHTML = '<div class="empty-tip">获取音源列表失败: ' + (data.msg || '未知错误') + '</div>';
                }}
            }} catch (e) {{
                container.innerHTML = '<div class="empty-tip">网络请求错误: ' + e + '</div>';
            }}
        }}

        function renderSourceCards(sources) {{
            const container = document.getElementById('sourceListContainer');
            const tip = document.getElementById('sourceCountTip');
            if (!sources || sources.length === 0) {{
                if (tip) tip.textContent = '当前未安装任何自定义音源';
                container.innerHTML = '<div class="empty-tip">当前未安装任何自定义音源，请从上方导入或填入推荐音源！</div>';
                return;
            }}

            const activeCount = sources.filter(s => s.enabled).length;
            if (tip) tip.textContent = `已安装 ${{sources.length}} 个音源 · 其中 ${{activeCount}} 个正在生效`;

            let html = '';
            sources.forEach(s => {{
                const isEnabled = !!s.enabled;
                const supported = Array.isArray(s.supportedSources) ? s.supportedSources : [];
                let tagsHtml = '';
                supported.forEach(p => {{
                    const cls = p.toLowerCase();
                    tagsHtml += `<span class="platform-tag ${{cls}}">${{p.toUpperCase()}}</span>`;
                }});

                const sizeKb = s.size ? Math.round(s.size / 1024) + ' KB' : '';
                const author = s.author || '社区开发者';
                const version = s.version || 'v1.0';
                const desc = s.description || '暂无描述信息';

                html += `
                <div class="source-card ${{isEnabled ? '' : 'is-disabled'}}" id="sc-${{s.id}}">
                    <div class="source-main">
                        <div class="source-header-row">
                            <span class="source-name">${{s.name || s.id}}</span>
                            <span class="badge" style="background:rgba(246,44,85,0.15); color:var(--accent); font-weight:bold;">${{version}}</span>
                            <span class="source-author">👤 ${{author}}</span>
                            ${{sizeKb ? `<span class="badge">${{sizeKb}}</span>` : ''}}
                        </div>
                        <div class="source-desc">${{desc}}</div>
                        <div class="source-platforms">
                            <span style="font-size:11px; color:var(--text-sub); margin-right:4px;">支持平台:</span>
                            ${{tagsHtml || '<span class="platform-tag">通用</span>'}}
                        </div>
                    </div>
                    <div class="source-actions">
                        <label class="switch" title="${{isEnabled ? '点击停用该音源' : '点击启用该音源'}}">
                            <input type="checkbox" ${{isEnabled ? 'checked' : ''}} onchange="toggleSource('${{s.id}}', this.checked)">
                            <span class="slider"></span>
                        </label>
                        <button class="btn btn-sm btn-danger" style="padding:3px 8px; font-size:11px;" onclick="deleteSource('${{s.id}}')">删除</button>
                    </div>
                </div>
                `;
            }});
            container.innerHTML = html;
        }}

        async function toggleSource(id, enabled) {{
            try {{
                const res = await fetch('/music/ext/api/custom_source/toggle', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ id, enabled }})
                }});
                const data = await res.json();
                if (data.code === 0) {{
                    const card = document.getElementById('sc-' + id);
                    if (card) {{
                        if (enabled) card.classList.remove('is-disabled');
                        else card.classList.add('is-disabled');
                    }}
                }} else {{
                    alert('切换音源状态失败: ' + data.msg);
                    loadCustomSources();
                }}
            }} catch (e) {{
                alert('网络请求失败: ' + e);
                loadCustomSources();
            }}
        }}

        async function deleteSource(id) {{
            if (!confirm('确定要删除自定义音源【' + id + '】吗？\\n删除后将无法恢复。')) return;
            try {{
                const res = await fetch('/music/ext/api/custom_source/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ id }})
                }});
                const data = await res.json();
                alert(data.msg);
                loadCustomSources();
            }} catch (e) {{
                alert('删除失败: ' + e);
            }}
        }}

        async function doImportSourceUrl() {{
            const url = document.getElementById('sourceImportUrl').value.trim();
            if (!url) return alert('请输入有效的音源脚本 URL');
            const btn = document.getElementById('btnImportUrl');
            btn.textContent = '正在下载并校验...';
            btn.disabled = true;
            try {{
                const res = await fetch('/music/ext/api/custom_source/import', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url }})
                }});
                const data = await res.json();
                alert(data.msg);
                if (data.code === 0) {{
                    document.getElementById('sourceImportUrl').value = '';
                    loadCustomSources();
                }}
            }} catch (e) {{
                alert('导入错误: ' + e);
            }} finally {{
                btn.textContent = '立即导入并生效';
                btn.disabled = false;
            }}
        }}

        async function doUploadSourceFile() {{
            const fileInput = document.getElementById('sourceFileInput');
            if (!fileInput.files || fileInput.files.length === 0) {{
                return alert('请先选择要上传的 .js 音源脚本文件');
            }}
            const file = fileInput.files[0];
            const btn = document.getElementById('btnUploadFile');
            btn.textContent = '正在上传...';
            btn.disabled = true;
            try {{
                const text = await file.text();
                const res = await fetch('/music/ext/api/custom_source/upload', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: file.name, content: text }})
                }});
                const data = await res.json();
                alert(data.msg);
                if (data.code === 0) {{
                    fileInput.value = '';
                    loadCustomSources();
                }}
            }} catch (e) {{
                alert('上传错误: ' + e);
            }} finally {{
                btn.textContent = '上传并安装';
                btn.disabled = false;
            }}
        }}

        async function doSaveSourceCode() {{
            const name = document.getElementById('sourceCodeName').value.trim();
            const content = document.getElementById('sourceCodeContent').value.trim();
            if (!content) return alert('请先粘贴音源 JavaScript 源代码');
            const btn = document.getElementById('btnSaveCode');
            btn.textContent = '正在保存校验...';
            btn.disabled = true;
            try {{
                const res = await fetch('/music/ext/api/custom_source/upload', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name, content }})
                }});
                const data = await res.json();
                alert(data.msg);
                if (data.code === 0) {{
                    document.getElementById('sourceCodeContent').value = '';
                    loadCustomSources();
                }}
            }} catch (e) {{
                alert('保存失败: ' + e);
            }} finally {{
                btn.textContent = '保存并安装';
                btn.disabled = false;
            }}
        }}

        async function reloadSourceService() {{
            if (!confirm('确定要重启落雪音源容器吗？这通常耗时约 2-3 秒。')) return;
            try {{
                const res = await fetch('/music/ext/api/custom_source/reload', {{ method: 'POST' }});
                const data = await res.json();
                alert(data.msg);
                setTimeout(loadCustomSources, 3000);
            }} catch (e) {{
                alert('操作失败: ' + e);
            }}
        }}

        // 页面初始化时自动加载音源列表
        document.addEventListener('DOMContentLoaded', () => {{
            loadCustomSources();
        }});

        function setAccent(color) {{
            document.getElementById('accentColor').value = color;
        }}

        async function saveBoardSettings() {{
            const form = document.getElementById('boardForm');
            const boards = [];
            const checkboxes = form.querySelectorAll('input[type="checkbox"]');
            checkboxes.forEach(cb => {{
                const id = cb.name.replace('board_', '');
                boards.push({{ id, enabled: cb.checked }});
            }});
            const res = await fetch('/music/ext/api/settings/boards', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ boards }})
            }});
            const data = await res.json();
            alert(data.msg || '保存成功');
        }}

        function notifyMainWindowSync() {{
            try {{
                if (window.BroadcastChannel) {{
                    const bc = new BroadcastChannel('fnmusic_channel');
                    bc.postMessage({{ type: 'REFRESH_PLAYLISTS', time: Date.now() }});
                    bc.close();
                }}
                localStorage.setItem('fnmusic_playlist_refresh', Date.now());
                if (window.opener && !window.opener.closed) {{
                    window.opener.postMessage({{ type: 'REFRESH_PLAYLISTS', time: Date.now() }}, '*');
                }}
            }} catch(e) {{
                console.warn('sync notification failed', e);
            }}
        }}

        async function loadCustomPlaylists() {{
            try {{
                const res = await fetch('/music/ext/api/playlist/list');
                const data = await res.json();
                const container = document.getElementById('playlistContainer');
                if (!container) return;
                const list = data.data || [];
                if (list.length === 0) {{
                    container.innerHTML = '<div class="empty-tip">暂无导入或自定义歌单，可通过下方歌单导入管理立即添加！</div>';
                    return;
                }}
                let html = '';
                for (let i = 0; i < list.length; i++) {{
                    const p = list[i];
                    const cover = p.cover_url || '/music/static/assets/img/logo.png';
                    const src = (p.source || 'ext').toUpperCase();
                    html += '<div class=\"pl-card\">' +
                        '<img src=\"' + cover + '\" class=\"pl-cover\" alt=\"\" onerror=\"this.src=\\'/music/static/assets/img/logo.png\\'\">' +
                        '<div class=\"pl-info\">' +
                            '<div class=\"pl-title\">' + p.name + '</div>' +
                            '<div class=\"pl-meta\">' + (p.trackCount || 0) + ' 首歌曲 · 来自 ' + src + '</div>' +
                        '</div>' +
                        '<button class=\"btn btn-sm btn-danger\" onclick=\"deletePlaylist(\\'' + p.guid + '\\')\">删除</button>' +
                    '</div>';
                }}
                container.innerHTML = html;
            }} catch(e) {{
                console.warn('load playlists failed', e);
            }}
        }}

        async function doImport() {{
            const urlInput = document.getElementById('importUrl');
            const nameInput = document.getElementById('importName');
            const url = urlInput.value.trim();
            const name = nameInput.value.trim();
            if(!url) return alert('请输入歌单链接或 ID');
            const btn = event.target;
            btn.textContent = '导入中...';
            btn.disabled = true;
            try {{
                const res = await fetch('/music/ext/api/playlist/import', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url, name }})
                }});
                const data = await res.json();
                alert(data.msg);
                if(data.code === 0 || data.ok) {{
                    urlInput.value = '';
                    nameInput.value = '';
                    await loadCustomPlaylists();
                    notifyMainWindowSync();
                }}
            }} catch(e) {{
                alert('网络错误: ' + e);
            }} finally {{
                btn.textContent = '立即导入';
                btn.disabled = false;
            }}
        }}

        async function deletePlaylist(guid) {{
            if(!confirm('确定要删除这个歌单吗？')) return;
            try {{
                const res = await fetch('/music/ext/api/playlist/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ guid }})
                }});
                const data = await res.json();
                alert(data.msg);
                await loadCustomPlaylists();
                notifyMainWindowSync();
            }} catch(e) {{
                alert('删除失败: ' + e);
            }}
        }}

        async function saveBasicSettings() {{
            const downloadDir = document.getElementById('downloadDir').value.trim();
            const maxCacheMb = parseInt(document.getElementById('maxCacheMb').value) || 5120;
            const res = await fetch('/music/ext/api/settings/basic', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ download_dir: downloadDir, max_cache_mb: maxCacheMb }})
            }});
            const data = await res.json();
            alert(data.msg || '设置已更新');
        }}

        async function saveAppearanceSettings() {{
            const themeMode = document.getElementById('themeMode').value;
            const accentColor = document.getElementById('accentColor').value.trim();
            const showQualityBadge = document.getElementById('showQualityBadge').checked;
            const enableSmoothScroll = document.getElementById('enableSmoothScroll').checked;
            const customCss = document.getElementById('customCss').value;

            const res = await fetch('/music/ext/api/settings/appearance', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    theme_mode: themeMode,
                    accent_color: accentColor,
                    show_quality_badge: showQualityBadge,
                    enable_smooth_scroll: enableSmoothScroll,
                    custom_css: customCss
                }})
            }});
            const data = await res.json();
            alert(data.msg || '外观设置保存成功！');
        }}

        async function saveLogicSettings() {{
            const preferredQuality = document.getElementById('preferredQuality').value;
            const lxServerUrl = document.getElementById('lxServerUrl').value.trim();
            const lxServiceUrl = document.getElementById('lxServiceUrl').value.trim();

            const res = await fetch('/music/ext/api/settings/logic', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    preferred_quality: preferredQuality,
                    lx_server_url: lxServerUrl,
                    lx_service_url: lxServiceUrl
                }})
            }});
            const data = await res.json();
            alert(data.msg || '调度逻辑保存成功！');
        }}

        async function refreshLogs() {{
            const logBox = document.getElementById('logContent');
            logBox.textContent = '加载最新日志中...';
            try {{
                const res = await fetch('/music/ext/api/logs');
                const data = await res.json();
                logBox.textContent = data.data || '暂无日志';
                logBox.scrollTop = logBox.scrollHeight;
            }} catch(e) {{
                logBox.textContent = '获取日志失败: ' + e;
            }}
        }}

        async function clearCache() {{
            if(!confirm('确定清空本地在线试听缓存吗？')) return;
            const res = await fetch('/music/ext/api/cache/clear', {{ method: 'POST' }});
            const data = await res.json();
            alert(data.msg);
            location.reload();
        }}

        // ===== 其他音源配置 (网易云音乐 VIP 音源与资源补全) =====
        let neteasePollTimer = null;

        function switchNeteaseSubtab(type) {{
            ['qr', 'cookie', 'web'].forEach(t => {{
                const el = document.getElementById('netease-subtab-' + t);
                const btn = document.getElementById('netease-tab-btn-' + t);
                if (el) el.style.display = (t === type) ? 'block' : 'none';
                if (btn) {{
                    if (t === type) btn.classList.add('active');
                    else btn.classList.remove('active');
                }}
            }});
        }}

        function updateSvipOptionsState() {{
            const isSvipEnabled = document.getElementById('neteaseSvipEnabled').checked;
            const qualitySelect = document.getElementById('neteaseQuality');
            const hint = document.getElementById('neteaseQualityHint');
            const svipGroup = document.getElementById('neteaseSvipOptGroup');
            const svipOpts = svipGroup ? svipGroup.querySelectorAll('option') : [];

            svipOpts.forEach(opt => {{
                opt.disabled = !isSvipEnabled;
            }});

            if (!isSvipEnabled) {{
                if (['jymaster', 'sky', 'jyeffect'].includes(qualitySelect.value)) {{
                    qualitySelect.value = 'lossless';
                    if (hint) hint.innerHTML = '<span style="color:#ffb300;">⚠️ 已自动切换至黑胶 VIP 支持的无损品质 (FLAC)。如需使用超清母带/环绕声/臻音，请勾选启用 SVIP 音源。</span>';
                }} else {{
                    if (hint) hint.textContent = '黑胶 VIP 支持：标准品质 (128k)、极高品质 (320k)、无损品质 (FLAC)';
                }}
            }} else {{
                if (hint) hint.innerHTML = '<span style="color:#00e676;">✨ 已解锁黑胶 SVIP 专属：超清母带 (192kHz/24bit)、沉浸环绕声及高清臻音</span>';
            }}
        }}

        function onNeteaseVipToggle() {{
            const vipEnabled = document.getElementById('neteaseEnabled').checked;
            if (!vipEnabled) {{
                document.getElementById('neteaseSvipEnabled').checked = false;
            }}
            updateSvipOptionsState();
        }}

        function onNeteaseSvipToggle() {{
            const svipEnabled = document.getElementById('neteaseSvipEnabled').checked;
            if (svipEnabled) {{
                document.getElementById('neteaseEnabled').checked = true;
            }}
            updateSvipOptionsState();
        }}

        function onNeteaseQualityChange() {{
            const qualitySelect = document.getElementById('neteaseQuality');
            const isSvipEnabled = document.getElementById('neteaseSvipEnabled').checked;
            if (['jymaster', 'sky', 'jyeffect'].includes(qualitySelect.value) && !isSvipEnabled) {{
                alert('超清母带、沉浸环绕声与高清臻音属于黑胶 SVIP 专属音质，请先勾选「启用网易云音乐 SVIP 音源及偏好选项」！');
                document.getElementById('neteaseSvipEnabled').checked = true;
                document.getElementById('neteaseEnabled').checked = true;
                updateSvipOptionsState();
            }}
        }}

        async function loadNeteaseConfig() {{
            try {{
                const res = await fetch('/music/ext/api/netease/config');
                const json = await res.json();
                if (json.code === 0 && json.data) {{
                    const d = json.data;
                    document.getElementById('neteaseEnabled').checked = !!d.enabled;
                    document.getElementById('neteaseSvipEnabled').checked = !!d.enable_svip;
                    document.getElementById('neteaseQuality').value = d.quality || 'lossless';
                    document.getElementById('neteaseFailover').checked = d.enable_failover !== false;
                    document.getElementById('neteaseAutoEnrich').checked = d.auto_enrich !== false;
                    
                    updateSvipOptionsState();

                    const u = d.user_info || {{}};
                    const isLogged = !!u.is_logged_in;
                    
                    const badge = document.getElementById('neteaseStatusBadge');
                    const nick = document.getElementById('neteaseNickname');
                    const vipBadge = document.getElementById('neteaseVipBadge');
                    const vipInfo = document.getElementById('neteaseVipInfo');
                    const avatar = document.getElementById('neteaseAvatar');
                    const logoutBtn = document.getElementById('neteaseLogoutBtn');
                    
                    if (isLogged) {{
                        badge.textContent = u.is_svip ? '👑 已接入 SVIP' : '🟢 已接入 VIP';
                        badge.style.color = u.is_svip ? '#ff9100' : '#00e676';
                        badge.style.background = u.is_svip ? 'rgba(255,145,0,0.12)' : 'rgba(0,230,118,0.12)';
                        nick.textContent = u.nickname || '网易云用户';
                        
                        vipBadge.textContent = u.vip_level || (u.is_svip ? '👑 黑胶SVIP' : '👑 黑胶VIP');
                        if (u.is_svip) {{
                            vipBadge.style.background = 'rgba(255,107,0,0.2)';
                            vipBadge.style.color = '#ff9100';
                        }} else {{
                            vipBadge.style.background = 'rgba(230,0,38,0.2)';
                            vipBadge.style.color = '#ff4d6a';
                        }}
                        
                        let infoText = '会员状态：' + (u.vip_level || '正常') + ' (UID: ' + (u.user_id || '--') + ')';
                        if (u.vip_expire_str) {{
                            infoText += ' · 有效期至 ' + u.vip_expire_str;
                        }}
                        vipInfo.textContent = infoText;
                        if (u.avatar_url) avatar.src = u.avatar_url;
                        logoutBtn.style.display = 'block';
                    }} else {{
                        badge.textContent = '⚪ 未登录';
                        badge.style.color = 'var(--text-sub)';
                        badge.style.background = 'rgba(255,255,255,0.08)';
                        nick.textContent = '未登录网易云账号';
                        vipBadge.textContent = '游客';
                        vipBadge.style.background = 'rgba(255,255,255,0.1)';
                        vipBadge.style.color = 'var(--text-sub)';
                        vipInfo.textContent = '请通过下方扫码或填入 Cookie 完成账号接入，享受黑胶 VIP/SVIP 无损音质与自动封面歌词补全';
                        avatar.src = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='48' height='48' viewBox='0 0 24 24' fill='none' stroke='%238a839e' stroke-width='1.5'%3E%3Ccircle cx='12' cy='8' r='4'/%3E%3Cpath d='M20 21a8 8 0 1 0-16 0'/%3E%3C/svg%3E";
                        logoutBtn.style.display = 'none';
                    }}
                }}
            }} catch(e) {{
                console.error('loadNeteaseConfig failed:', e);
            }}
        }}

        async function startNeteaseQrLogin() {{
            if (neteasePollTimer) clearInterval(neteasePollTimer);
            const container = document.getElementById('neteaseQrContainer');
            const statusEl = document.getElementById('neteaseQrStatus');
            container.innerHTML = '<div style="color:#666; font-size:12px;">正在请求二维码...</div>';
            statusEl.textContent = '正在获取网易云登录二维码...';
            
            try {{
                const res = await fetch('/music/ext/api/netease/qr/create', {{ method: 'POST' }});
                const json = await res.json();
                if (json.code === 0 && json.data && json.data.unikey) {{
                    const unikey = json.data.unikey;
                    container.innerHTML = json.data.qr_svg;
                    const svg = container.querySelector('svg');
                    if (svg) {{
                        svg.setAttribute('width', '144');
                        svg.setAttribute('height', '144');
                    }}
                    statusEl.innerHTML = '<span style="color:#ff4d6a;">📱 请打开网易云音乐 App 扫一扫</span><br><small style="color:var(--text-sub);">等待扫码确认中...</small>';
                    
                    // 开始轮询状态
                    let pollCount = 0;
                    neteasePollTimer = setInterval(async () => {{
                        pollCount++;
                        if (pollCount > 90) {{ // 3分钟超时
                            clearInterval(neteasePollTimer);
                            statusEl.textContent = '二维码已超时，请重新生成。';
                            return;
                        }}
                        try {{
                            const cRes = await fetch('/music/ext/api/netease/qr/check?unikey=' + unikey);
                            const cJson = await cRes.json();
                            if (cJson.code === 803) {{
                                clearInterval(neteasePollTimer);
                                statusEl.innerHTML = '<b style="color:#00e676;">🎉 登录成功！已自动保存授权凭据。</b>';
                                alert('网易云账号登录成功！');
                                loadNeteaseConfig();
                            }} else if (cJson.code === 802) {{
                                statusEl.innerHTML = '<b style="color:#ffca28;">📲 已成功扫描，请在手机上点击「确认登录」</b>';
                            }} else if (cJson.code === 800) {{
                                clearInterval(neteasePollTimer);
                                statusEl.innerHTML = '<span style="color:#ff5252;">二维码已过期，请点击按钮重新生成。</span>';
                            }}
                        }} catch(err) {{}}
                    }}, 2000);
                }} else {{
                    container.innerHTML = '<div style="color:#ff5252; font-size:12px;">生成失败</div>';
                    statusEl.textContent = json.msg || '获取二维码失败';
                }}
            }} catch(e) {{
                container.innerHTML = '<div style="color:#ff5252; font-size:12px;">网络异常</div>';
                statusEl.textContent = '请求异常: ' + e;
            }}
        }}

        async function saveNeteaseManualCookie() {{
            const cookieVal = (document.getElementById('neteaseCookieInput').value || '').trim();
            if (!cookieVal) {{
                alert('请先输入 Cookie 或 MUSIC_U 的值！');
                return;
            }}
            try {{
                const res = await fetch('/music/ext/api/netease/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        cookie: cookieVal,
                        enabled: true
                    }})
                }});
                const json = await res.json();
                if (json.code === 0) {{
                    alert('网易云 Cookie 保存成功！');
                    document.getElementById('neteaseCookieInput').value = '';
                    loadNeteaseConfig();
                }} else {{
                    alert('保存失败: ' + json.msg);
                }}
            }} catch(e) {{
                alert('请求异常: ' + e);
            }}
        }}

        async function logoutNetease() {{
            if (!confirm('确定要退出网易云账号登录并清除 Cookie 凭据吗？')) return;
            try {{
                const res = await fetch('/music/ext/api/netease/logout', {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg);
                loadNeteaseConfig();
            }} catch(e) {{
                alert('退出异常: ' + e);
            }}
        }}

        async function saveNeteaseSettings() {{
            const enabled = document.getElementById('neteaseEnabled').checked;
            const enableSvip = document.getElementById('neteaseSvipEnabled').checked;
            const quality = document.getElementById('neteaseQuality').value;
            const enableFailover = document.getElementById('neteaseFailover').checked;
            const autoEnrich = document.getElementById('neteaseAutoEnrich').checked;
            try {{
                const res = await fetch('/music/ext/api/netease/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        enabled: enabled,
                        enable_svip: enableSvip,
                        quality: quality,
                        enable_failover: enableFailover,
                        auto_enrich: autoEnrich
                    }})
                }});
                const json = await res.json();
                alert(json.msg || '音源配置已保存！');
                loadNeteaseConfig();
            }} catch(e) {{
                alert('保存异常: ' + e);
            }}
        }}

        async function testNeteaseConnection() {{
            const icon = document.getElementById('neteaseTestIcon');
            const box = document.getElementById('neteaseTestResultBox');
            icon.textContent = '⏳';
            box.style.display = 'block';
            box.innerHTML = '正在测试网易云 API 连通性、VIP/SVIP 音源直链解析与歌词匹配...';
            
            try {{
                const res = await fetch('/music/ext/api/netease/test', {{ method: 'POST' }});
                const json = await res.json();
                icon.textContent = '⚡';
                if (json.code === 0 && json.data) {{
                    const d = json.data;
                    const u = d.user_info || {{}};
                    box.innerHTML = `
                        <div style="color:#00e676; font-weight:600;">✓ 网易云 API 连通测试通过 (总延迟: ${{d.latency_ms}}ms)</div>
                        <div style="color:var(--text-sub); margin-top:4px;">
                            • 云端曲库检索：<span style="color:#00e676;">正常 (${{d.search_ok ? '已命中' : '无'}})</span><br>
                            • 音频直链解析：<span style="color:${{d.stream_ok ? '#00e676' : '#ffca28'}};">${{d.stream_ok ? '成功 (' + (d.stream_level_desc || d.stream_level) + ')' : '受限/需VIP'}}</span><br>
                            • LRC歌词获取：<span style="color:#00e676;">正常 (${{d.lyric_ok ? '获取成功' : '空'}})</span><br>
                            • 会员账号状态：<span style="color:#fff; font-weight:600;">${{u.nickname || '未登录'}}</span> <span class="platform-tag" style="background:${{u.is_svip ? 'rgba(255,107,0,0.2)' : 'rgba(230,0,38,0.2)'}}; color:${{u.is_svip ? '#ff9100' : '#ff4d6a'}}; font-size:11px;">${{u.vip_level || '游客'}}</span> ${{u.vip_expire_str ? '<span style=\"color:var(--text-sub);\">(' + u.vip_expire_str + ' 到期)</span>' : ''}}
                        </div>
                    `;
                    // 同步刷新头部卡片状态
                    loadNeteaseConfig();
                }} else {{
                    box.innerHTML = `<div style="color:#ff5252;">✗ 测试失败: ${{json.msg}}</div>`;
                }}
            }} catch(e) {{
                icon.textContent = '⚡';
                box.innerHTML = `<div style="color:#ff5252;">✗ 网络请求异常: ${{e}}</div>`;
            }}
        }}

        // ===== 飞牛音乐AI助手 (AI Assistant Config) =====
        // 检查 URL hash 是否指定了初始选项卡，支持 /music/ext/#other_sources / #ai_model 直达
        window.addEventListener('DOMContentLoaded', () => {{
            const hash = (window.location.hash || '').replace('#', '');
            if (hash && ['overview', 'custom_source', 'other_sources', 'ai_model', 'leaderboards', 'playlists', 'download', 'appearance', 'logs'].includes(hash)) {{
                switchTab(hash);
            }}
        }});

        let _clearAiKeyRequested = false;

        function clearAiApiKey() {{
            document.getElementById('aiApiKey').value = '';
            document.getElementById('aiKeyTip').textContent = '⚠️ 已标记为清空密钥（点击“保存配置”后生效）';
            document.getElementById('aiKeyTip').style.color = '#f62c55';
            _clearAiKeyRequested = true;
        }}

        async function loadAiConfig() {{
            try {{
                _clearAiKeyRequested = false;
                const res = await fetch('/music/ext/api/ai/config');
                const json = await res.json();
                if (json.code === 0 && json.data) {{
                    const d = json.data;
                    document.getElementById('aiModelName').value = d.model_name || '';
                    document.getElementById('aiModelId').value = d.model_id || '';
                    document.getElementById('aiDefaultModel').value = d.default_model || d.model_id || '';
                    document.getElementById('aiBaseUrl').value = d.base_url || '';
                    document.getElementById('aiContext').value = d.context || 4096;
                    document.getElementById('aiApiKey').value = '';
                    if (d.api_key_masked) {{
                        document.getElementById('aiKeyTip').textContent = '当前已配置密钥：' + d.api_key_masked + '（留空保持不变）';
                        document.getElementById('aiKeyTip').style.color = 'var(--text-sub)';
                    }} else {{
                        document.getElementById('aiKeyTip').textContent = '未配置密钥';
                        document.getElementById('aiKeyTip').style.color = 'var(--text-sub)';
                    }}
                }}
            }} catch(e) {{
                console.error('loadAiConfig error:', e);
            }}
        }}

        function setAiPreset(type) {{
            if (type === 'deepseek') {{
                document.getElementById('aiModelName').value = 'DeepSeek官方';
                document.getElementById('aiModelId').value = 'deepseek-chat';
                document.getElementById('aiDefaultModel').value = 'deepseek-chat';
                document.getElementById('aiBaseUrl').value = 'https://api.deepseek.com/v1';
                document.getElementById('aiContext').value = 64000;
            }} else if (type === 'sensenova') {{
                document.getElementById('aiModelName').value = '商汤日日新';
                document.getElementById('aiModelId').value = 'SenseChat-5';
                document.getElementById('aiDefaultModel').value = 'SenseChat-5';
                document.getElementById('aiBaseUrl').value = 'https://api.sensenova.cn/compatible-mode/v1';
                document.getElementById('aiContext').value = 32000;
            }} else if (type === 'openai') {{
                document.getElementById('aiModelName').value = 'OpenAI官方';
                document.getElementById('aiModelId').value = 'gpt-4o-mini';
                document.getElementById('aiDefaultModel').value = 'gpt-4o-mini';
                document.getElementById('aiBaseUrl').value = 'https://api.openai.com/v1';
                document.getElementById('aiContext').value = 128000;
            }}
        }}

        async function testAiConfig() {{
            const btn = document.getElementById('btnTestAi');
            const badge = document.getElementById('aiStatusBadge');
            const payload = {{
                model_name: document.getElementById('aiModelName').value.trim(),
                model_id: document.getElementById('aiModelId').value.trim(),
                default_model: document.getElementById('aiDefaultModel').value.trim(),
                base_url: document.getElementById('aiBaseUrl').value.trim(),
                context: parseInt(document.getElementById('aiContext').value) || 4096,
                api_key: document.getElementById('aiApiKey').value.trim()
            }};
            btn.disabled = true;
            btn.textContent = '⏳ 检测中...';
            badge.style.background = 'rgba(255, 170, 0, 0.18)';
            badge.style.color = '#ffb822';
            badge.textContent = '检测中...';
            try {{
                const res = await fetch('/music/ext/api/ai/test', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }});
                const json = await res.json();
                if (json.code === 0 && json.data && json.data.ok) {{
                    const ms = json.data.elapsed_ms || json.data.latency_ms || 0;
                    const respText = json.data.reply || 'PONG';
                    badge.style.background = 'rgba(0, 230, 118, 0.18)';
                    badge.style.color = '#00e676';
                    badge.textContent = '连通正常 (' + ms + 'ms)';
                    alert('✅ 连通性测试成功！\\n耗时：' + ms + 'ms\\n响应：' + respText);
                }} else {{
                    badge.style.background = 'rgba(246, 44, 85, 0.18)';
                    badge.style.color = '#f62c55';
                    badge.textContent = '连通失败';
                    alert('❌ 连通性测试失败：\\n' + (json.data?.msg || json.msg || '无法连接该端点'));
                }}
            }} catch(e) {{
                badge.style.background = 'rgba(246, 44, 85, 0.18)';
                badge.style.color = '#f62c55';
                badge.textContent = '网络异常';
                alert('❌ 请求异常：' + e);
            }} finally {{
                btn.disabled = false;
                btn.textContent = '⚡ 模型连通性检测';
            }}
        }}

        async function saveAiConfig() {{
            const btn = document.getElementById('btnSaveAi');
            const keyInput = document.getElementById('aiApiKey').value.trim();
            const payload = {{
                model_name: document.getElementById('aiModelName').value.trim(),
                model_id: document.getElementById('aiModelId').value.trim(),
                default_model: document.getElementById('aiDefaultModel').value.trim(),
                base_url: document.getElementById('aiBaseUrl').value.trim(),
                context: parseInt(document.getElementById('aiContext').value) || 4096,
                api_key: keyInput,
                clear_api_key: _clearAiKeyRequested && !keyInput
            }};
            btn.disabled = true;
            btn.textContent = '保存中...';
            try {{
                const res = await fetch('/music/ext/api/ai/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }});
                const json = await res.json();
                alert(json.msg || '保存成功！');
                loadAiConfig();
            }} catch(e) {{
                alert('保存失败：' + e);
            }} finally {{
                btn.disabled = false;
                btn.textContent = '💾 保存配置';
            }}
        }}

        async function triggerScan() {{
            const res = await fetch('/music/ext/api/library/scan', {{ method: 'POST' }});
            const data = await res.json();
            alert(data.msg);
        }}

        // ==========================================
        // 🛠️ 飞牛音乐系统控制与数据管理模块
        // ==========================================
        let _lxCurrentFullData = null;

        function toggleCustomPathInput() {{
            const el = document.getElementById('lxCfgUserPath');
            const group = document.getElementById('customPathGroup');
            if (group && el) {{
                group.style.display = el.checked ? 'block' : 'none';
            }}
        }}

        async function loadLxSysConfig() {{
            try {{
                const res = await fetch('/music/ext/api/lx/config');
                const json = await res.json();
                if (json.code === 0 && json.data) {{
                    const d = json.data;
                    document.getElementById('lxCfgServerName').value = d.serverName || '飞牛音乐';
                    document.getElementById('lxCfgMaxSnapshots').value = d.maxSnapshotNum || 10;
                    document.getElementById('lxCfgAddLocation').value = d['list.addMusicLocationType'] || 'top';
                    document.getElementById('lxCfgProxyEnabled').checked = !!d['proxy.enabled'];
                    document.getElementById('lxCfgProxyHeader').value = d['proxy.header'] || 'x-real-ip';
                    document.getElementById('lxCfgUserPath').checked = d['user.enablePath'] !== false;
                    const dlInput = document.getElementById('lxCfgDownloadDir');
                    if (dlInput) dlInput.value = d.download_dir || '';
                    const autoScan = document.getElementById('lxCfgAutoScan');
                    if (autoScan) autoScan.checked = d.auto_scan_on_download !== false;
                    toggleCustomPathInput();
                }}
            }} catch (e) {{
                console.error("loadLxSysConfig err:", e);
            }}
        }}

        async function saveLxSysConfig() {{
            const dlInput = document.getElementById('lxCfgDownloadDir');
            const autoScan = document.getElementById('lxCfgAutoScan');
            const payload = {{
                serverName: document.getElementById('lxCfgServerName').value.trim() || '飞牛音乐',
                maxSnapshotNum: parseInt(document.getElementById('lxCfgMaxSnapshots').value, 10) || 10,
                'list.addMusicLocationType': document.getElementById('lxCfgAddLocation').value,
                'proxy.enabled': document.getElementById('lxCfgProxyEnabled').checked,
                'proxy.header': document.getElementById('lxCfgProxyHeader').value.trim() || 'x-real-ip',
                'user.enablePath': document.getElementById('lxCfgUserPath').checked,
                download_dir: dlInput ? dlInput.value.trim() : '',
                auto_scan_on_download: autoScan ? autoScan.checked : true
            }};
            try {{
                const res = await fetch('/music/ext/api/lx/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }});
                const json = await res.json();
                if (json.code === 0) {{
                    alert(json.msg || '飞牛音乐系统控制配置已保存并立即生效！');
                }} else {{
                    alert('保存系统控制失败: ' + (json.msg || '未知错误'));
                }}
            }} catch (e) {{
                alert('保存系统设置失败: ' + e);
            }}
        }}

        async function triggerFnScanNow() {{
            if (!confirm('确定向飞牛音乐原生服务发送全量媒体库重新扫描指令吗？')) return;
            try {{
                const res = await fetch('/music/ext/api/library/scan', {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg || '已向飞牛原生媒体库触发全量扫描！');
            }} catch (e) {{
                alert('触发扫描失败: ' + e);
            }}
        }}

        async function reloadFnExtService() {{
            if (!confirm('确定重新加载飞牛音乐扩展服务引擎与配置吗？')) return;
            try {{
                const res = await fetch('/music/ext/api/service/reload', {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg || '飞牛音乐扩展服务与内置引擎已成功重载！');
            }} catch (e) {{
                alert('重载失败: ' + e);
            }}
        }}

        async function loadLxWebdavConfig() {{
            try {{
                const res = await fetch('/music/ext/api/lx/webdav/config');
                const json = await res.json();
                if (json.code === 0 && json.data) {{
                    const d = json.data;
                    document.getElementById('lxWdUrl').value = d.url || '';
                    document.getElementById('lxWdUser').value = d.username || '';
                    document.getElementById('lxWdPass').value = d.password || '';
                    document.getElementById('lxWdPath').value = d.path || '/music_backup';
                    document.getElementById('lxWdAutoBackup').checked = !!d.auto_backup;
                    document.getElementById('lxWdCron').value = d.cron || '0 4 * * *';
                }}
                const logRes = await fetch('/music/ext/api/lx/webdav/logs');
                const logJson = await logRes.json();
                if (logJson.code === 0) {{
                    const logs = logJson.data?.logs || [];
                    document.getElementById('lxWdLogs').textContent = logs.length ? logs.join('\\\\n') : '暂无 WebDAV 失败或异常日志，同步服务健康正常。';
                }}
            }} catch (e) {{
                console.error("loadLxWebdavConfig err:", e);
            }}
        }}

        async function saveLxWebdavConfig() {{
            const payload = {{
                url: document.getElementById('lxWdUrl').value.trim(),
                username: document.getElementById('lxWdUser').value.trim(),
                password: document.getElementById('lxWdPass').value.trim(),
                path: document.getElementById('lxWdPath').value.trim(),
                auto_backup: document.getElementById('lxWdAutoBackup').checked,
                cron: document.getElementById('lxWdCron').value.trim()
            }};
            try {{
                const res = await fetch('/music/ext/api/lx/webdav/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }});
                const json = await res.json();
                alert(json.msg || 'WebDAV 设置已保存！');
            }} catch (e) {{
                alert('保存 WebDAV 设置失败: ' + e);
            }}
        }}

        async function testLxWebdav() {{
            const payload = {{
                url: document.getElementById('lxWdUrl').value.trim(),
                username: document.getElementById('lxWdUser').value.trim(),
                password: document.getElementById('lxWdPass').value.trim(),
                path: document.getElementById('lxWdPath').value.trim()
            }};
            try {{
                const res = await fetch('/music/ext/api/lx/webdav/test', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }});
                const json = await res.json();
                alert(json.msg);
            }} catch (e) {{
                alert('测试连接失败: ' + e);
            }}
        }}

        async function triggerLxWebdavBackup() {{
            if (!confirm('确定要创建全量备份并上传到 WebDAV 吗？')) return;
            document.getElementById('lxWdStatusText').textContent = '正在备份并上传云端...';
            try {{
                const res = await fetch('/music/ext/api/lx/webdav/backup', {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg);
                document.getElementById('lxWdStatusText').textContent = '备份指令已触发';
                loadLxWebdavConfig();
            }} catch (e) {{
                alert('触发 WebDAV 备份失败: ' + e);
                document.getElementById('lxWdStatusText').textContent = '备份失败';
            }}
        }}

        async function triggerLxWebdavRestore() {{
            if (!confirm('⚠️ 警告：从云端恢复将覆盖本地所有数据！\\n\\n确定要继续从云端拉取最新备份还原吗？')) return;
            document.getElementById('lxWdStatusText').textContent = '正在从云端拉取还原...';
            try {{
                const res = await fetch('/music/ext/api/lx/webdav/restore', {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg);
                document.getElementById('lxWdStatusText').textContent = '还原已触发';
                loadLxWebdavConfig();
            }} catch (e) {{
                alert('云端恢复异常: ' + e);
                document.getElementById('lxWdStatusText').textContent = '恢复异常';
            }}
        }}

        async function triggerLxWebdavSync() {{
            if (!confirm('确定要强制同步所有文件至 WebDAV 吗？')) return;
            document.getElementById('lxWdStatusText').textContent = '正在全量同步文件...';
            try {{
                const res = await fetch('/music/ext/api/lx/webdav/sync', {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg);
                document.getElementById('lxWdStatusText').textContent = '同步已触发';
                loadLxWebdavConfig();
            }} catch (e) {{
                alert('文件同步失败: ' + e);
                document.getElementById('lxWdStatusText').textContent = '同步失败';
            }}
        }}

        async function handleLxLocalRestore(event) {{
            const file = event.target.files?.[0];
            if (!file) return;
            if (!confirm(`确定要上传本地备份文件 [${{file.name}}] 并覆盖还原现有数据吗？`)) {{
                event.target.value = '';
                return;
            }}
            const formData = new FormData();
            formData.append('file', file);
            document.getElementById('lxWdStatusText').textContent = '正在上传还原本地备份...';
            try {{
                const res = await fetch('/music/ext/api/lx/backup/upload', {{
                    method: 'POST',
                    body: formData
                }});
                const json = await res.json();
                alert(json.msg);
                document.getElementById('lxWdStatusText').textContent = '本地还原完成';
            }} catch (e) {{
                alert('本地还原失败: ' + e);
                document.getElementById('lxWdStatusText').textContent = '还原失败';
            }} finally {{
                event.target.value = '';
            }}
        }}

        async function loadLxSnapshots() {{
            const user = document.getElementById('lxSnapshotUser').value;
            const tbody = document.getElementById('lxSnapshotList');
            tbody.innerHTML = '<tr><td colspan="4" style="padding:20px;text-align:center;color:#64748b;">正在检索快照版本...</td></tr>';
            try {{
                const res = await fetch(`/music/ext/api/lx/snapshots?user=${{encodeURIComponent(user)}}`);
                const json = await res.json();
                if (json.code === 0 && Array.isArray(json.data)) {{
                    if (json.data.length === 0) {{
                        tbody.innerHTML = '<tr><td colspan="4" style="padding:20px;text-align:center;color:#64748b;">暂无历史快照</td></tr>';
                        return;
                    }}
                    tbody.innerHTML = json.data.map(s => {{
                        const dateStr = new Date(s.time).toLocaleString();
                        const sizeKb = (s.size / 1024).toFixed(1) + ' KB';
                        return `
                            <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
                                <td style="padding:10px;font-family:monospace;color:#38bdf8;">${{s.id}}</td>
                                <td style="padding:10px;color:#e2e8f0;">${{dateStr}}</td>
                                <td style="padding:10px;color:#94a3b8;">${{sizeKb}}</td>
                                <td style="padding:10px;text-align:right;">
                                    <button class="btn btn-secondary btn-sm" style="padding:2px 8px;font-size:12px;margin-right:6px;" onclick="restoreLxSnapshot('${{s.id}}')">↩️ 回滚恢复</button>
                                    <button class="btn btn-danger btn-sm" style="padding:2px 8px;font-size:12px;" onclick="deleteLxSnapshot('${{s.id}}')">🗑️ 删除</button>
                                </td>
                            </tr>
                        `;
                    }}).join('');
                }} else {{
                    tbody.innerHTML = `<tr><td colspan="4" style="padding:20px;text-align:center;color:#f87171;">${{json.msg}}</td></tr>`;
                }}
            }} catch (e) {{
                tbody.innerHTML = `<tr><td colspan="4" style="padding:20px;text-align:center;color:#f87171;">加载异常: ${{e}}</td></tr>`;
            }}
        }}

        async function restoreLxSnapshot(id) {{
            const user = document.getElementById('lxSnapshotUser').value;
            if (!confirm(`确认要将用户 [${{user}}] 的音乐数据恢复到快照 [${{id}}] 吗？`)) return;
            try {{
                const res = await fetch(`/music/ext/api/lx/snapshot/restore?id=${{encodeURIComponent(id)}}&user=${{encodeURIComponent(user)}}`, {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg);
                loadLxSnapshots();
            }} catch (e) {{
                alert('回滚快照失败: ' + e);
            }}
        }}

        async function deleteLxSnapshot(id) {{
            const user = document.getElementById('lxSnapshotUser').value;
            if (!confirm(`确定删除快照 [${{id}}] 吗？`)) return;
            try {{
                const res = await fetch(`/music/ext/api/lx/snapshot/delete?id=${{encodeURIComponent(id)}}&user=${{encodeURIComponent(user)}}`, {{ method: 'POST' }});
                const json = await res.json();
                alert(json.msg);
                loadLxSnapshots();
            }} catch (e) {{
                alert('删除快照失败: ' + e);
            }}
        }}

        async function loadLxData() {{
            const user = document.getElementById('lxDataUser').value;
            const container = document.getElementById('lxPlaylistsContainer');
            container.innerHTML = '<div style="color:#64748b;font-size:13px;">正在加载歌单...</div>';
            try {{
                const res = await fetch(`/music/ext/api/lx/data?user=${{encodeURIComponent(user)}}`);
                const json = await res.json();
                if (json.code === 0 && json.data) {{
                    _lxCurrentFullData = json.data;
                    const defaultList = _lxCurrentFullData.defaultList || [];
                    const userLists = _lxCurrentFullData.userList || [];
                    
                    let html = `
                        <div onclick="renderLxPlaylistSongs('default')" class="nav-item active" id="plTab_default" style="padding:8px 12px;margin-bottom:4px;border-radius:6px;cursor:pointer;">
                            <span>❤️ 试听/默认列表 (${{defaultList.length}})</span>
                        </div>
                    `;
                    userLists.forEach((l, idx) => {{
                        html += `
                            <div onclick="renderLxPlaylistSongs(${{idx}})" class="nav-item" id="plTab_${{idx}}" style="padding:8px 12px;margin-bottom:4px;border-radius:6px;cursor:pointer;">
                                <span>📁 ${{l.name || '未命名歌单'}} (${{(l.list || []).length}})</span>
                            </div>
                        `;
                    }});
                    container.innerHTML = html;
                    renderLxPlaylistSongs('default');
                }} else {{
                    container.innerHTML = `<div style="color:#f87171;font-size:13px;">${{json.msg}}</div>`;
                }}
            }} catch (e) {{
                container.innerHTML = `<div style="color:#f87171;font-size:13px;">加载失败: ${{e}}</div>`;
            }}
        }}

        function renderLxPlaylistSongs(target) {{
            if (!_lxCurrentFullData) return;
            document.querySelectorAll('#lxPlaylistsContainer .nav-item').forEach(el => el.classList.remove('active'));
            const tabEl = document.getElementById(`plTab_${{target}}`);
            if (tabEl) tabEl.classList.add('active');

            let list = [];
            let name = '';
            if (target === 'default') {{
                list = _lxCurrentFullData.defaultList || [];
                name = '❤️ 试听/默认列表';
            }} else {{
                const uList = (_lxCurrentFullData.userList || [])[target];
                if (uList) {{
                    list = uList.list || [];
                    name = uList.name || '未命名歌单';
                }}
            }}

            document.getElementById('lxCurrentPlaylistName').textContent = name;
            document.getElementById('lxSongCountBadge').textContent = `${{list.length}} 首歌曲`;

            const tbody = document.getElementById('lxSongListTable');
            if (list.length === 0) {{
                tbody.innerHTML = '<tr><td colspan="5" style="padding:20px;text-align:center;color:#64748b;">该列表中暂无歌曲</td></tr>';
                return;
            }}

            tbody.innerHTML = list.map((s, idx) => {{
                const srcBadge = (s.source || 'kw').toUpperCase();
                return `
                    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
                        <td style="padding:8px;color:#64748b;">${{idx + 1}}</td>
                        <td style="padding:8px;color:#f8fafc;font-weight:500;">${{s.name || '--'}}</td>
                        <td style="padding:8px;color:#cbd5e1;">${{s.singer || '--'}}</td>
                        <td style="padding:8px;"><span class="badge" style="background:rgba(56,189,248,0.15);color:#38bdf8;font-size:11px;">${{srcBadge}}</span></td>
                        <td style="padding:8px;color:#94a3b8;">${{s.interval || '--:--'}}</td>
                    </tr>
                `;
            }}).join('');
        }}

    </script>
</body>
</html>
    """

# ==========================================
# 在线歌单集成 (网易云 / 酷我 / 抖音 / 酷狗 / 企鹅)
# ==========================================
ONLINE_PL_CACHE_DIR = os.path.join(STATE_DIR, "online_pl_cache")
os.makedirs(ONLINE_PL_CACHE_DIR, exist_ok=True)

def is_online_playlist_guid(guid: str | None) -> bool:
    if not guid:
        return False
    return str(guid).startswith("online_pl:")

def parse_online_playlist_guid(guid: str) -> tuple[str, str]:
    # format: online_pl:{source}:{playlist_id}
    parts = str(guid).split(":", 2)
    if len(parts) >= 3:
        return parts[1], parts[2]
    return "wy", parts[-1]

async def fetch_online_playlists_list(source: str = "wy", page: int = 1, tag_id: str = "", lx_server_url: str = "") -> list[dict]:
    """获取指定平台的精选/热门歌单列表 (原生直连，不经 9528，支持 wy, kw, kg, tx, mg)"""
    cache_file = os.path.join(ONLINE_PL_CACHE_DIR, f"list_{source}_{tag_id}_{page}.json")
    now = time.time()
    if os.path.exists(cache_file):
        try:
            if now - os.path.getmtime(cache_file) < 1800: # 30分钟缓存
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    results = []
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            if source == "wy":
                cat = quote(tag_id) if tag_id else quote("全部")
                offset = max(0, (page - 1) * 30)
                url = f"https://music.163.com/api/playlist/list?cat={cat}&order=hot&offset={offset}&limit=30"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
                if r.status_code == 200:
                    data = r.json()
                    for it in data.get("playlists") or []:
                        pid = str(it.get("id") or "").strip()
                        if not pid:
                            continue
                        results.append({
                            "guid": f"online_pl:wy:{pid}",
                            "id": pid,
                            "source": "wy",
                            "name": str(it.get("name") or "").strip(),
                            "author": str((it.get("creator") or {}).get("nickname") or "网易云精选").strip(),
                            "play_count": str(it.get("playCount") or ""),
                            "total": it.get("trackCount") or 0,
                            "cover_url": str(it.get("coverImgUrl") or "").strip(),
                            "desc": str(it.get("description") or "").strip(),
                        })
            elif source == "kw":
                url = f"http://wapi.kuwo.cn/api/pc/classify/playlist/getRcmPlayList?pn={page}&rn=30&order=hot"
                r = await client.get(url, headers={"User-Agent": "okhttp/3.10.0"})
                if r.status_code == 200:
                    data = r.json()
                    for it in ((data.get("data") or {}).get("data") or []):
                        pid = str(it.get("id") or "").strip()
                        if not pid:
                            continue
                        results.append({
                            "guid": f"online_pl:kw:{pid}",
                            "id": pid,
                            "source": "kw",
                            "name": str(it.get("name") or "").strip(),
                            "author": str(it.get("uname") or "酷我精选").strip(),
                            "play_count": str(it.get("listencnt") or ""),
                            "total": it.get("total") or 0,
                            "cover_url": str(it.get("img") or "").strip(),
                            "desc": str(it.get("info") or "").strip(),
                        })
            elif source == "kg":
                url = f"http://m.kugou.com/plist/index&json=true&page={page}"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    data = r.json()
                    for it in ((data.get("plist") or {}).get("list", {}).get("info", []) or []):
                        pid = str(it.get("specialid") or "").strip()
                        if not pid:
                            continue
                        pic = str(it.get("imgurl") or "").replace("{size}", "400")
                        results.append({
                            "guid": f"online_pl:kg:{pid}",
                            "id": pid,
                            "source": "kg",
                            "name": str(it.get("specialname") or "").strip(),
                            "author": str(it.get("nickname") or "酷狗精选").strip(),
                            "play_count": str(it.get("playcount") or ""),
                            "total": it.get("songcount") or 0,
                            "cover_url": pic,
                            "desc": str(it.get("intro") or "").strip(),
                        })
            elif source == "tx":
                sin = max(0, (page - 1) * 30)
                ein = sin + 29
                url = f"https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg?sin={sin}&ein={ein}&categoryId=10000000&sortId=5&format=json"
                r = await client.get(url, headers={"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    try:
                        text = r.content.decode("gbk")
                    except Exception:
                        text = r.content.decode("utf-8", "ignore")
                    data = json.loads(text)
                    for it in ((data.get("data") or {}).get("list") or []):
                        pid = str(it.get("dissid") or "").strip()
                        if not pid:
                            continue
                        results.append({
                            "guid": f"online_pl:tx:{pid}",
                            "id": pid,
                            "source": "tx",
                            "name": str(it.get("dissname") or "").strip(),
                            "author": str((it.get("creator") or {}).get("name") or "QQ音乐精选").strip(),
                            "play_count": str(it.get("listennum") or ""),
                            "total": it.get("songnum") or 0,
                            "cover_url": str(it.get("imgurl") or "").strip(),
                            "desc": str(it.get("introduction") or "").strip(),
                        })
            elif source == "mg":
                url = f"https://app.c.nf.migu.cn/pc/bmw/page-data/playlist-square-recommend/v1.0?templateVersion=2&pageNo={page}"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X)"})
                if r.status_code == 200:
                    contents = (r.json().get("data") or {}).get("contents", [])
                    def filter_mg(items, res=None, ids=None):
                        if res is None: res = []
                        if ids is None: ids = set()
                        for item in items:
                            if "contents" in item:
                                filter_mg(item["contents"], res, ids)
                            elif str(item.get("resType")) == "2021" and str(item.get("resId")) not in ids:
                                ids.add(str(item.get("resId")))
                                res.append({
                                    "guid": f"online_pl:mg:{item.get('resId')}",
                                    "id": str(item.get("resId")),
                                    "source": "mg",
                                    "name": str(item.get("txt") or "").strip(),
                                    "author": "咪咕精选",
                                    "play_count": "",
                                    "total": 30,
                                    "cover_url": str(item.get("img") or "").strip(),
                                    "desc": str(item.get("txt2") or "").strip(),
                                })
                        return res
                    results = filter_mg(contents)
    except Exception as e:
        logger.warning("fetch_online_playlists_list failed for %s: %s", source, e)

    if results:
        try:
            with open(cache_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False)
            os.replace(cache_file + ".tmp", cache_file)
        except Exception:
            pass
    return results

async def fetch_online_playlist_detail(guid: str, pid: str = "", lx_server_url: str = "") -> dict | None:
    """获取单个在线歌单详情及其歌曲 (原生直连，不经 9528)"""
    if pid:
        source = guid
    else:
        source, pid = parse_online_playlist_guid(guid)
    cache_file = os.path.join(ONLINE_PL_CACHE_DIR, f"detail_{source}_{pid}.json")
    now = time.time()
    if os.path.exists(cache_file):
        try:
            if now - os.path.getmtime(cache_file) < 3600 * 2: # 2小时缓存
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    playlist_data = None
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            if source == "wy":
                url = f"https://music.163.com/api/playlist/detail?id={pid}"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
                if r.status_code == 200:
                    p_info = (r.json() or {}).get("result") or {}
                    raw_list = p_info.get("tracks") or []
                    tracks = []
                    for item in raw_list:
                        songmid = str(item.get("id") or "").strip()
                        if not songmid:
                            continue
                        artists = item.get("artists") or []
                        art_name = "/".join(a.get("name", "") for a in artists if a.get("name")) or "群星"
                        album = (item.get("album") or {}).get("name") or ""
                        cover = (item.get("album") or {}).get("picUrl") or ""
                        dur = float(item.get("duration") or 240000) / 1000.0
                        tracks.append({
                            "id": f"lx:wy:{songmid}",
                            "source": "lx",
                            "lx_source": "wy",
                            "song_id": songmid,
                            "title": str(item.get("name") or "").strip(),
                            "artist": art_name,
                            "album": album,
                            "duration_s": dur,
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": cover,
                            "lyric": "",
                        })
                    playlist_data = {
                        "guid": guid,
                        "id": pid,
                        "source": source,
                        "name": str(p_info.get("name") or "在线精选歌单").strip(),
                        "desc": str(p_info.get("description") or "").strip(),
                        "cover_url": str(p_info.get("coverImgUrl") or "").strip(),
                        "tracks": tracks
                    }
            elif source == "kw":
                url = f"http://nplserver.kuwo.cn/pl.svc?op=getlistinfo&pid={pid}&pn=0&rn=100&encode=utf8&keyset=pl2012&identity=kuwo&pcmp4=1&vipver=MUSIC_9.0.5.0_W1&newver=1"
                r = await client.get(url, headers={"User-Agent": "okhttp/3.10.0"})
                if r.status_code == 200:
                    p_info = r.json() or {}
                    raw_list = p_info.get("musiclist") or []
                    tracks = []
                    for item in raw_list:
                        songmid = str(item.get("id") or "").strip()
                        if not songmid:
                            continue
                        title = str(item.get("name") or item.get("FSONGNAME") or "").strip()
                        artist = str(item.get("artist") or item.get("FARTIST") or "").strip() or "精选歌手"
                        album = str(item.get("album") or item.get("FALBUM") or "").strip() or title
                        # 高清封面提取：优先 albumpic / musicPic，自动转 500px 高清大图
                        pic = str(item.get("albumpic") or item.get("musicPic") or item.get("pic") or "").strip()
                        if pic:
                            pic = pic.replace("/120/", "/500/")
                        if not pic:
                            artist_pic = str(item.get("artistPic") or "").strip()
                            if artist_pic:
                                pic = artist_pic.replace("/120/", "/500/")
                        if not pic and songmid.isdigit():
                            pic = f"http://artistpicserver.kuwo.cn/pic.web?type=rid_pic&pictype=url&size=500&rid={songmid}"
                        tracks.append({
                            "id": f"lx:kw:{songmid}",
                            "source": "lx",
                            "lx_source": "kw",
                            "song_id": songmid,
                            "title": title,
                            "artist": artist,
                            "album": album,
                            "duration_s": float(item.get("duration") or 240),
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": pic,
                            "lyric": "",
                        })
                    playlist_data = {
                        "guid": guid,
                        "id": pid,
                        "source": source,
                        "name": str(p_info.get("title") or "在线精选歌单").strip(),
                        "desc": str(p_info.get("info") or "").strip(),
                        "cover_url": str(p_info.get("pic") or "").strip(),
                        "tracks": tracks
                    }
            elif source == "kg":
                url = f"http://www2.kugou.kugou.com/yueku/v9/special/single/{pid}-5-9999.html"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    m = re.search(r"global\.data\s*=\s*(\[.+?\]);", r.text)
                    m_info = re.search(r"global\s*=\s*\{[\s\S]+?name:\s*\"(.+?)\"[\s\S]+?pic:\s*\"(.+?)\"", r.text)
                    name = m_info.group(1) if m_info else "酷狗歌单"
                    pic = m_info.group(2) if m_info else ""
                    raw_list = json.loads(m.group(1)) if m else []
                    tracks = []
                    for item in raw_list:
                        songmid = str(item.get("hash") or item.get("audio_id") or "").strip()
                        if not songmid:
                            continue
                        dur = float(item.get("duration") or 240000) / 1000.0 if float(item.get("duration") or 0) > 1000 else float(item.get("duration") or 240)
                        tracks.append({
                            "id": f"lx:kg:{songmid}",
                            "source": "lx",
                            "lx_source": "kg",
                            "song_id": songmid,
                            "title": str(item.get("songname") or "").strip(),
                            "artist": str(item.get("singername") or "").strip(),
                            "album": str(item.get("album_name") or "").strip(),
                            "duration_s": dur,
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": "",
                            "lyric": "",
                        })
                    playlist_data = {
                        "guid": guid,
                        "id": pid,
                        "source": source,
                        "name": name,
                        "desc": "",
                        "cover_url": pic,
                        "tracks": tracks
                    }
            elif source == "tx":
                url = f"https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?type=1&json=1&utf8=1&onlysong=0&new_format=1&disstid={pid}&loginUin=0&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0"
                r = await client.get(url, headers={"Referer": f"https://y.qq.com/n/yqq/playsquare/{pid}.html", "Origin": "https://y.qq.com", "User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    d = json.loads(r.content.decode("utf-8", "ignore"))
                    cd = d.get("cdlist", [{}])[0]
                    raw_list = cd.get("songlist", [])
                    tracks = []
                    for item in raw_list:
                        songmid = str(item.get("mid") or item.get("songmid") or "").strip()
                        if not songmid:
                            continue
                        singer = "/".join(s.get("name", "") for s in (item.get("singer") or []) if s.get("name")) or "群星"
                        album_obj = item.get("album") or {}
                        album = album_obj.get("name") or ""
                        album_mid = album_obj.get("mid") or ""
                        cover = f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{album_mid}.jpg" if album_mid else ""
                        dur = float(item.get("interval") or 240)
                        tracks.append({
                            "id": f"lx:tx:{songmid}",
                            "source": "lx",
                            "lx_source": "tx",
                            "song_id": songmid,
                            "title": str(item.get("title") or item.get("name") or "").strip(),
                            "artist": singer,
                            "album": album,
                            "duration_s": dur,
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": cover,
                            "lyric": "",
                        })
                    playlist_data = {
                        "guid": guid,
                        "id": pid,
                        "source": source,
                        "name": str(cd.get("dissname") or "QQ音乐歌单").strip(),
                        "desc": str(cd.get("desc") or "").strip(),
                        "cover_url": str(cd.get("logo") or "").strip(),
                        "tracks": tracks
                    }
            elif source == "mg":
                url = f"https://app.c.nf.migu.cn/MIGUM3.0/resource/playlist/song/v2.0?pageNo=1&pageSize=50&playlistId={pid}"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X)"})
                if r.status_code == 200:
                    raw_list = (r.json().get("data") or {}).get("songList", [])
                    tracks = []
                    for item in raw_list:
                        songmid = str(item.get("copyrightId") or item.get("songId") or "").strip()
                        if not songmid:
                            continue
                        tracks.append({
                            "id": f"lx:mg:{songmid}",
                            "source": "lx",
                            "lx_source": "mg",
                            "song_id": songmid,
                            "title": str(item.get("songName") or "").strip(),
                            "artist": str(item.get("singer") or "群星").strip(),
                            "album": str(item.get("album") or "").strip(),
                            "duration_s": 240.0,
                            "ext": "flac",
                            "file_size": 31457280,
                            "cover_url": str(item.get("albumPicL") or item.get("picL") or "").strip(),
                            "lyric": "",
                        })
                    playlist_data = {
                        "guid": guid,
                        "id": pid,
                        "source": source,
                        "name": "咪咕音乐歌单",
                        "desc": "",
                        "cover_url": "",
                        "tracks": tracks
                    }
    except Exception as e:
        logger.warning("fetch_online_playlist_detail failed for %s: %s", guid, e)

    if playlist_data and playlist_data.get("tracks"):
        try:
            with open(cache_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump(playlist_data, f, ensure_ascii=False)
            os.replace(cache_file + ".tmp", cache_file)
        except Exception:
            pass
    return playlist_data

async def get_custom_sources_list(lx_server_url: str = "http://127.0.0.1:9528") -> list[dict]:
    """获取所有已导入的落雪自定义音源列表"""
    # 1. 优先调用 9528 API
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(
                f"{lx_server_url}/api/custom-source/list?username=_open",
                headers=LX_AUTH_HEADER,
            )
            if resp.status_code == 200:
                items = resp.json()
                if isinstance(items, list):
                    return items
    except Exception as e:
        logger.warning("get_custom_sources_list via API failed: %s, falling back to local file", e)

    # 2. 本地文件系统回退
    src_json = os.path.join(LX_SOURCE_DIR_OPEN, "sources.json")
    if os.path.isfile(src_json):
        try:
            with open(src_json, "r", encoding="utf-8") as f:
                items = json.load(f)
                if isinstance(items, list):
                    return items
        except Exception as e:
            logger.error("read local sources.json failed: %s", e)

    return []


async def toggle_custom_source(source_id: str, enabled: bool, lx_server_url: str = "http://127.0.0.1:9528") -> dict:
    """启用/停用指定的落雪自定义源"""
    # 同步调用 9528 API
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            await client.post(
                f"{lx_server_url}/api/custom-source/toggle",
                headers={**LX_AUTH_HEADER, "Content-Type": "application/json"},
                json={"id": source_id, "enabled": enabled, "username": "_open"},
            )
            await client.post(
                f"{lx_server_url}/api/custom-source/toggle",
                headers={**LX_AUTH_HEADER, "Content-Type": "application/json"},
                json={"id": source_id, "enabled": enabled, "username": "admin"},
            )
    except Exception as e:
        logger.warning("toggle_custom_source api call warning: %s", e)

    # 物理更新 _open/sources.json 与 admin/states.json
    for base_dir in [LX_SOURCE_DIR_OPEN, LX_SOURCE_DIR_PROTOKC]:
        s_file = os.path.join(base_dir, "sources.json")
        if os.path.isfile(s_file):
            try:
                with open(s_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                updated = False
                for it in data:
                    if it.get("id") == source_id:
                        it["enabled"] = enabled
                        updated = True
                if updated:
                    with open(s_file + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(s_file + ".tmp", s_file)
            except Exception as ex:
                logger.warning("update %s failed: %s", s_file, ex)

    # 同步更新 states.json
    state_file = os.path.join(LX_SOURCE_DIR_PROTOKC, "states.json")
    try:
        states = {}
        if os.path.isfile(state_file):
            with open(state_file, "r", encoding="utf-8") as f:
                states = json.load(f)
        states[source_id] = {"enabled": enabled}
        with open(state_file + ".tmp", "w", encoding="utf-8") as f:
            json.dump(states, f, ensure_ascii=False, indent=2)
        os.replace(state_file + ".tmp", state_file)
    except Exception as ex:
        logger.warning("update states.json failed: %s", ex)

    return {"ok": True, "msg": f"音源已{'启用' if enabled else '停用'}"}


async def delete_custom_source(source_id: str, lx_server_url: str = "http://127.0.0.1:9528") -> dict:
    """删除指定的落雪自定义源"""
    # 尝试调用 9528 API
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            await client.post(
                f"{lx_server_url}/api/custom-source/delete",
                headers={**LX_AUTH_HEADER, "Content-Type": "application/json"},
                json={"id": source_id, "username": "_open"},
            )
            await client.post(
                f"{lx_server_url}/api/custom-source/delete",
                headers={**LX_AUTH_HEADER, "Content-Type": "application/json"},
                json={"id": source_id, "username": "admin"},
            )
    except Exception as e:
        logger.warning("delete_custom_source api warning: %s", e)

    # 物理删除文件与注册表
    for base_dir in [LX_SOURCE_DIR_OPEN, LX_SOURCE_DIR_PROTOKC]:
        script_path = os.path.join(base_dir, source_id)
        if os.path.isfile(script_path):
            try:
                os.remove(script_path)
            except Exception:
                pass
        s_file = os.path.join(base_dir, "sources.json")
        if os.path.isfile(s_file):
            try:
                with open(s_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data = [it for it in data if it.get("id") != source_id]
                with open(s_file + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(s_file + ".tmp", s_file)
            except Exception:
                pass

    return {"ok": True, "msg": "音源已成功移除"}


async def import_custom_source_from_url(url: str, lx_server_url: str = "http://127.0.0.1:9528") -> dict:
    """通过 URL 在线拉取并导入落雪音源脚本"""
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return {"ok": False, "msg": "请输入有效的 http/https 音源脚本链接"}

    # 尝试多轮下载（支持直连与 ghproxy 镜像兜底）
    urls_to_try = [url]
    if "raw.githubusercontent.com" in url and "ghproxy.net" not in url:
        urls_to_try.append(f"https://ghproxy.net/{url}")

    content = None
    last_err = ""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for u in urls_to_try:
            try:
                r = await client.get(u)
                if r.status_code == 200 and len(r.text) > 100:
                    content = r.text
                    break
                else:
                    last_err = f"HTTP {r.status_code}"
            except Exception as ex:
                last_err = str(ex)

    if not content:
        return {"ok": False, "msg": f"音源脚本下载失败: {last_err}"}

    # 提取并保存音源
    filename = url.split("?")[0].rstrip("/").split("/")[-1] or "custom_source.js"
    if not filename.endswith(".js"):
        filename += ".js"

    return await upload_custom_source_content(content, filename=filename, source_url=url, lx_server_url=lx_server_url)


async def upload_custom_source_content(content: str, filename: str = "", source_url: str = "", lx_server_url: str = "http://127.0.0.1:9528") -> dict:
    """保存并注册音源脚本内容"""
    if not content or len(content.strip()) < 50:
        return {"ok": False, "msg": "音源脚本内容为空或过短"}

    if "globalThis.lx" not in content and "lx." not in content and "EVENT_NAMES" not in content:
        return {"ok": False, "msg": "无效的落雪音源脚本：未检测到 globalThis.lx 或 EVENT_NAMES 契约"}

    meta = _parse_source_script_metadata(content, fallback_filename=filename)
    safe_name = re.sub(r'[\\/:*?"<>|]', "", meta["name"])
    target_filename = f"{safe_name}.js" if safe_name else (filename or "custom_source.js")
    if not target_filename.endswith(".js"):
        target_filename += ".js"

    entry = {
        "id": target_filename,
        "name": meta["name"],
        "version": meta["version"],
        "author": meta["author"],
        "description": meta["description"],
        "homepage": meta.get("homepage", ""),
        "size": len(content.encode("utf-8")),
        "supportedSources": meta["supportedSources"],
        "enabled": True,
        "uploadTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "sourceUrl": source_url,
        "allowUnsafeVM": False,
        "requireUnsafe": False,
    }

    # 写入 _open 与 admin 目录
    for base_dir in [LX_SOURCE_DIR_OPEN, LX_SOURCE_DIR_PROTOKC]:
        os.makedirs(base_dir, exist_ok=True)
        # 写 JS 文件
        js_path = os.path.join(base_dir, target_filename)
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 更新 sources.json
        s_file = os.path.join(base_dir, "sources.json")
        items = []
        if os.path.isfile(s_file):
            try:
                with open(s_file, "r", encoding="utf-8") as f:
                    items = json.load(f)
            except Exception:
                items = []

        # 去重更新
        items = [it for it in items if it.get("id") != target_filename]
        items.insert(0, entry)
        with open(s_file + ".tmp", "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(s_file + ".tmp", s_file)

    # 触发容器热载或重启
    asyncio.create_task(reload_custom_sources(lx_server_url))

    return {
        "ok": True,
        "msg": f"成功导入音源【{meta['name']}】({meta['version']})！已自动设为启用",
        "data": entry,
    }


async def reload_custom_sources(lx_server_url: str = "http://127.0.0.1:9528") -> dict:
    """重启落雪容器 lx-sync-server 以确保全部自定义音源重新加载"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", "lx-sync-server",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return {"ok": True, "msg": "落雪音源服务已成功重启并重载全部自定义源"}
    except Exception as e:
        logger.warning("reload_custom_sources failed: %s", e)
        return {"ok": False, "msg": f"重启服务失败: {e}"}


async def resolve_url_by_custom_source(
    song_info: dict,
    quality: str = "flac",
    lx_server_url: str = "http://127.0.0.1:9528",
) -> dict | None:
    """
    通过 9528 落雪自定义源引擎（UserApi）解析真实无损直链。
    彻底解耦内置音源，完全尊重用户导入并启用的自定义音源！
    """
    try:
        # 1. 检查是否有启用的自定义音源，若用户已全部关闭，直接跳过
        try:
            cur_sources = await get_custom_sources_list(lx_server_url)
            has_enabled_source = any(bool(s.get("enabled")) for s in cur_sources)
            if not has_enabled_source:
                return None
        except Exception:
            pass

        sid = str(song_info.get("songmid") or song_info.get("id") or "").strip()
        src = str(song_info.get("source") or "kw").strip()
        payload = {
            "songInfo": {
                "id": sid,
                "name": str(song_info.get("name") or song_info.get("title") or "").strip(),
                "singer": str(song_info.get("singer") or song_info.get("artist") or "").strip(),
                "source": src,
                "songmid": sid,
                "interval": str(song_info.get("interval") or "04:00").strip(),
            },
            "quality": quality,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{lx_server_url}/api/music/url",
                headers={
                    **LX_AUTH_HEADER,
                    "Content-Type": "application/json",
                    "x-user-name": "_open",
                },
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    raw_url = str(data.get("url") or "").strip()
                    # 严格校验：排除假直链与空链接（例如 http://music.nxinxz.com/None）
                    if raw_url and not raw_url.endswith("/None") and "/None?" not in raw_url and "null" not in raw_url:
                        return {
                            "url": raw_url,
                            "format": data.get("type") or "flac",
                            "sourceName": data.get("sourceName") or "自定义音源",
                        }
    except Exception as e:
        logger.warning("resolve_url_by_custom_source failed for %s: %s", song_info, e)
    return None


async def search_kuwo_song(title: str, artist: str = "") -> dict | None:
    """直接在酷我原生检索歌曲，返回真实 rid 与封面"""
    import re
    clean_t = re.sub(r"[\(（\[【].*?[\)）\]】]", "", title).strip() or title
    queries = [f"{clean_t} {artist}".strip(), clean_t]
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            for q_str in queries:
                q_enc = quote(q_str)
                url_kw = f"http://search.kuwo.cn/r.s?client=kt&all={q_enc}&pn=0&rn=5&vipver=1&ft=music&encoding=utf8&rformat=json&mobi=1"
                r_kw = await client.get(url_kw, headers={"User-Agent": "okhttp/3.10.0"})
                if r_kw.status_code == 200:
                    text_kw = r_kw.text
                    try:
                        d_kw = json.loads(text_kw)
                    except Exception:
                        import ast
                        d_kw = ast.literal_eval(text_kw)
                    abslist = d_kw.get("abslist") or []
                    for item in abslist:
                        s_name = str(item.get("SONGNAME") or "")
                        s_artist = str(item.get("ARTIST") or "")
                        rid = str(item.get("MUSICRID") or "").replace("MUSIC_", "")
                        if rid:
                            cover = f"http://artistpicserver.kuwo.cn/pic.web?type=rid_pic&pictype=url&size=500&rid={rid}"
                            return {
                                "id": f"lx:kw:{rid}",
                                "guid": f"online:lx:kw:{rid}",
                                "rid": rid,
                                "title": s_name,
                                "artist": s_artist or artist,
                                "album": str(item.get("ALBUM") or "精选专辑"),
                                "cover_url": cover,
                            }
    except Exception as e:
        logger.debug("search_kuwo_song failed for %s: %s", title, e)
    return None

    """脱离落雪服务：原生直连检索网易云与酷我最佳匹配歌曲"""
    query_str = f"{title} {artist}".strip()
    q_enc = quote(query_str)
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            # 1. 优先网易云原生直连
            try:
                url_wy = f"https://music.163.com/api/search/get?s={q_enc}&type=1&offset=0&limit=5"
                r_wy = await client.get(url_wy, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
                if r_wy.status_code == 200:
                    songs = (r_wy.json().get("result") or {}).get("songs") or []
                    for s in songs:
                        s_name = str(s.get("name") or "")
                        s_artist = "/".join(a.get("name", "") for a in (s.get("artists") or []))
                        if title.lower() in s_name.lower() or s_name.lower() in title.lower():
                            sid = str(s.get("id"))
                            return {
                                "id": f"lx:wy:{sid}",
                                "guid": f"online:lx:wy:{sid}",
                                "title": s_name,
                                "artist": s_artist or artist or "华语群星",
                                "album": (s.get("album") or {}).get("name") or "精选专辑",
                                "cover_url": (s.get("album") or {}).get("picUrl") or "",
                            }
            except Exception:
                pass

            # 2. 酷我原生直连
            try:
                url_kw = f"http://search.kuwo.cn/r.s?client=kt&all={q_enc}&pn=0&rn=5&vipver=1&ft=music&encoding=utf8&rformat=json&mobi=1"
                r_kw = await client.get(url_kw, headers={"User-Agent": "okhttp/3.10.0"})
                if r_kw.status_code == 200:
                    text_kw = r_kw.text
                    try:
                        d_kw = json.loads(text_kw)
                    except Exception:
                        import ast
                        d_kw = ast.literal_eval(text_kw)
                    abslist = d_kw.get("abslist") or []
                    for item in abslist:
                        s_name = str(item.get("SONGNAME") or "")
                        s_artist = str(item.get("ARTIST") or "")
                        rid = str(item.get("MUSICRID") or "").replace("MUSIC_", "")
                        if rid and (title.lower() in s_name.lower() or s_name.lower() in title.lower()):
                            return {
                                "id": f"lx:kw:{rid}",
                                "guid": f"online:lx:kw:{rid}",
                                "title": s_name,
                                "artist": s_artist or artist,
                                "album": str(item.get("ALBUM") or "精选专辑"),
                                "cover_url": "",
                            }
            except Exception:
                pass
    except Exception as e:
        logger.warning("search_best_online_song error for %s: %s", title, e)
    return None


async def get_search_suggestions(keyword: str) -> dict:
    """
    搜索建议与候选音乐模糊匹配 (网易云 + 酷我开放接口 + 本地媒体库并发直连)
    返回: {"words": [...], "candidates": [...]}
    """
    kw = (keyword or "").strip()
    if not kw:
        return {"words": [], "candidates": []}

    words: list[str] = []
    candidates: list[dict] = []
    seen_titles: set[str] = set()

    # 1. 本地数据库模糊检索 (必须物理文件真实存在)
    music_db = "/usr/local/apps/@appdata/trim.music/db/music.db"
    if os.path.exists(music_db):
        try:
            con = sqlite3.connect(f"file:{music_db}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute("""
                SELECT t.id, t.guid, t.title, COALESCE(a.name, '未知') as artist, t.cover_guid, al.name as album, t.duration_ms, af.path
                FROM track t
                JOIN audio_file af ON t.audio_file_id = af.id
                LEFT JOIN track_artist ta ON t.id = ta.track_id
                LEFT JOIN artist a ON ta.artist_id = a.id
                LEFT JOIN album al ON t.album_id = al.id
                WHERE t.is_audio_file_deleted = 0 AND af.is_physical_file_deleted = 0
                  AND (t.title LIKE ? OR a.name LIKE ? OR t.title_latin_full LIKE ?)
                LIMIT 5
            """, (f"%{kw}%", f"%{kw}%", f"%{kw}%"))
            for r in cur.fetchall():
                tid, guid, title, artist, cover_guid, album, dur_ms, fpath = r
                if fpath and os.path.isfile(str(fpath)) and os.path.getsize(str(fpath)) > 0:
                    t_clean = str(title or "").strip()
                    if t_clean.lower() not in seen_titles:
                        seen_titles.add(t_clean.lower())
                        words.append(t_clean)
                        cover_url = f"/music/static/cover/track?coverId={cover_guid}" if cover_guid else ""
                        candidates.append({
                            "id": guid,
                            "guid": guid,
                            "title": t_clean,
                            "artist": artist or "本地音乐",
                            "album": album or "本地曲库",
                            "cover_url": cover_url,
                            "duration_s": float(dur_ms or 240000) / 1000.0,
                            "badge": "本地无损",
                            "is_local": True,
                        })
            con.close()
        except Exception as e:
            logger.debug("Local db suggest error: %s", e)

    # 2. 网易云与酷我开放接口并发联想与候选歌曲提取
    async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
        # 2.1 网易云 suggest (并发直连批量补全高清专辑封面)
        try:
            url_wy = f"https://music.163.com/api/search/suggest/web?s={quote(kw)}&limit=6"
            r_wy = await client.get(url_wy, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
            if r_wy.status_code == 200:
                songs = (r_wy.json().get("result") or {}).get("songs") or []
                song_ids = [str(s.get("id")) for s in songs if s.get("id")]
                wy_covers = {}
                if song_ids:
                    try:
                        d_url = f"https://music.163.com/api/song/detail/?id={song_ids[0]}&ids=[{','.join(song_ids)}]"
                        r_d = await client.get(d_url, headers={"User-Agent": "Mozilla/5.0"})
                        if r_d.status_code == 200:
                            for ds in (r_d.json() or {}).get("songs") or []:
                                sid_str = str(ds.get("id"))
                                p_url = (ds.get("album") or {}).get("picUrl") or ""
                                if p_url:
                                    wy_covers[sid_str] = p_url
                    except Exception:
                        pass
                for s in songs:
                    s_name = str(s.get("name") or "").strip()
                    s_id = str(s.get("id"))
                    ar_names = [a.get("name", "") for a in (s.get("artists") or [])]
                    ar = " / ".join(filter(None, ar_names)) or "华语群星"
                    album = (s.get("album") or {}).get("name") or "精选专辑"
                    cover_u = wy_covers.get(s_id) or (s.get("album") or {}).get("picUrl") or ""
                    dur = float(s.get("duration") or 240000) / 1000.0
                    if s_name:
                        if s_name not in words:
                            words.append(s_name)
                        if f"{s_name} {ar}" not in words and len(words) < 8:
                            words.append(f"{s_name} {ar}")
                        key = s_name.lower()
                        if key not in seen_titles:
                            seen_titles.add(key)
                            candidates.append({
                                "id": f"lx:wy:{s_id}",
                                "guid": f"online:lx:wy:{s_id}",
                                "title": s_name,
                                "artist": ar,
                                "album": album,
                                "cover_url": cover_u,
                                "duration_s": dur,
                                "badge": "网易云",
                                "is_local": False,
                            })
        except Exception as e:
            logger.debug("WY suggest error: %s", e)

        # 2.2 酷我开放搜索候选提取
        try:
            url_kw = f"http://search.kuwo.cn/r.s?client=kt&all={quote(kw)}&pn=0&rn=6&vipver=1&ft=music&encoding=utf8&rformat=json&mobi=1"
            r_kw = await client.get(url_kw, headers={"User-Agent": "okhttp/3.10.0"})
            if r_kw.status_code == 200:
                text_kw = r_kw.text
                try:
                    d_kw = json.loads(text_kw)
                except Exception:
                    import ast
                    d_kw = ast.literal_eval(text_kw)
                abslist = d_kw.get("abslist") or []
                for item in abslist:
                    s_name = str(item.get("SONGNAME") or "").strip()
                    s_artist = str(item.get("ARTIST") or "").strip()
                    rid = str(item.get("MUSICRID") or "").replace("MUSIC_", "")
                    dur = float(item.get("DURATION") or 240)
                    if s_name and rid:
                        if s_name not in words and len(words) < 10:
                            words.append(s_name)
                        key = s_name.lower()
                        if key not in seen_titles and len(candidates) < 8:
                            seen_titles.add(key)
                            candidates.append({
                                "id": f"lx:kw:{rid}",
                                "guid": f"online:lx:kw:{rid}",
                                "title": s_name,
                                "artist": s_artist or "精选歌手",
                                "album": str(item.get("ALBUM") or "精选专辑"),
                                "cover_url": f"http://artistpicserver.kuwo.cn/pic.web?type=rid_pic&pictype=url&size=500&rid={rid}",
                                "duration_s": dur,
                                "badge": "酷我",
                                "is_local": False,
                            })
        except Exception as e:
            logger.debug("KW suggest error: %s", e)

    return {
        "words": words[:8],
        "candidates": candidates[:8]
    }



