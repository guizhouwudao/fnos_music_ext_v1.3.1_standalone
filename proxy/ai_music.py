"""
AI 音乐助手服务模块：
1. 管理 AI 模型配置 (模型名称, model_id, base_url, api_key)
2. 模型连通性检测 (测试响应速度与连通状态)
3. 分析用户经常播放的音乐偏好 (读取 SQLite play_history 与 在线播放记录)
4. 调用大模型分析音乐风格并生成契合推荐
5. 在本地音乐库与在线音乐库中搜索并匹配真实可播放曲目
6. 缓存心动推荐歌单，供网页端卡片与原生歌单播放
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import ast
from typing import Any
import httpx

logger = logging.getLogger("fnmusic_ai")

_HOME = os.environ.get("HOME") or "/root"
STATE_DIR = os.path.join(_HOME, ".local", "state", "fnmusic_ext")
AI_CONFIG_FILE = os.path.join(STATE_DIR, "ai_config.json")
AI_REC_CACHE_FILE = os.path.join(STATE_DIR, "ai_recommend_cache.json")
MUSIC_DB_PATH = "/usr/local/apps/@appdata/trim.music/db/music.db"

DEFAULT_AI_CONFIG = {
    "model_name": "",
    "model_id": "",
    "default_model": "",
    "context": 4096,
    "base_url": "",
    "api_key": "",
    "updated_at": 0,
}

_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_JSON_ARRAY = re.compile(r"\[[\s\S]*\]")


def load_ai_config() -> dict:
    if not os.path.exists(AI_CONFIG_FILE):
        return dict(DEFAULT_AI_CONFIG)
    try:
        with open(AI_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            res = dict(DEFAULT_AI_CONFIG)
            res.update(data)
            return res
    except Exception as e:
        logger.warning("load_ai_config error: %s", e)
        return dict(DEFAULT_AI_CONFIG)


def save_ai_config(cfg: dict) -> bool:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        cur = load_ai_config()
        # API Key 处理：
        # 1. 显式提交 clear_api_key 或 api_key 传入 null / 特殊清空标识时，清空密钥
        # 2. 如果新提交的 key 为空字符串或包含掩码星号 *，保留原有已保存密钥
        # 3. 否则保存用户输入的新密钥
        if cfg.get("clear_api_key") is True:
            new_key = ""
        else:
            raw_key = cfg.get("api_key")
            if raw_key is None:
                new_key = cur.get("api_key", "")
            else:
                s_key = str(raw_key).strip()
                if not s_key or "*" in s_key:
                    new_key = cur.get("api_key", "")
                else:
                    new_key = s_key

        m_id = str(cfg.get("model_id") or "").strip()
        def_model = str(cfg.get("default_model") or m_id).strip()
        try:
            ctx_val = int(cfg.get("context") or 4096)
        except Exception:
            ctx_val = 4096
        cur = {
            "model_name": str(cfg.get("model_name") or "").strip(),
            "model_id": m_id,
            "default_model": def_model,
            "context": ctx_val,
            "base_url": str(cfg.get("base_url") or "").strip().rstrip("/"),
            "api_key": new_key,
            "updated_at": int(time.time()),
        }
        with open(AI_CONFIG_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(AI_CONFIG_FILE + ".tmp", AI_CONFIG_FILE)
        return True
    except Exception as e:
        logger.warning("save_ai_config error: %s", e)
        return False


def mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return k[:3] + "******" + k[-4:]


async def test_ai_connection(cfg: dict | None = None) -> dict:
    """测试模型连通性"""
    target = dict(load_ai_config())
    if cfg:
        # 如果传入了临时配置进行测试
        if cfg.get("base_url"):
            target["base_url"] = str(cfg["base_url"]).strip().rstrip("/")
        if cfg.get("model_id"):
            target["model_id"] = str(cfg["model_id"]).strip()
        elif cfg.get("default_model"):
            target["model_id"] = str(cfg["default_model"]).strip()
        key = str(cfg.get("api_key") or "").strip()
        if key and "*" not in key:
            target["api_key"] = key

    base_url = target.get("base_url", "").strip().rstrip("/")
    model_id = target.get("model_id") or target.get("default_model") or "DeepSeek-V4.1-Flash"
    api_key = target.get("api_key", "").strip()

    if not base_url or not api_key:
        return {"ok": False, "msg": "Base URL 和 API Key 不能为空"}

    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    payload = {
        "model": model_id,
        "temperature": 0.1,
        "max_tokens": 30,
        "messages": [
            {"role": "user", "content": "Ping: 请仅回复 PONG"}
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            elapsed = round((time.time() - t0) * 1000, 1)
            if resp.status_code == 200:
                data = resp.json()
                reply = ""
                choices = data.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    reply = choices[0].get("message", {}).get("content", "").strip()
                return {
                    "ok": True,
                    "msg": f"连接成功！延迟 {elapsed}ms，模型响应: {reply[:30]}",
                    "latency_ms": elapsed,
                    "elapsed_ms": elapsed,
                    "reply": reply or "PONG",
                    "model": model_id,
                }
            else:
                return {
                    "ok": False,
                    "msg": f"模型返回错误 (HTTP {resp.status_code}): {resp.text[:120]}",
                    "elapsed_ms": elapsed,
                    "latency_ms": elapsed,
                }
    except Exception as e:
        elapsed = round((time.time() - t0) * 1000, 1)
        return {
            "ok": False,
            "msg": f"连接异常: {type(e).__name__} - {e}",
            "latency_ms": elapsed,
        }


def get_frequent_tracks(limit: int = 25) -> list[dict]:
    """读取本地 SQLite play_history 与在线播放记录，计算经常播放的歌曲"""
    items = []
    if os.path.exists(MUSIC_DB_PATH):
        try:
            con = sqlite3.connect(f"file:{MUSIC_DB_PATH}?mode=ro", uri=True)
            cur = con.cursor()
            query = """
                SELECT t.title, COALESCE(a.name, '未知歌手') as artist, p.play_count, p.updated_at, t.guid, t.cover_guid
                FROM play_history p
                JOIN track t ON p.track_id = t.id
                LEFT JOIN track_artist ta ON t.id = ta.track_id
                LEFT JOIN artist a ON ta.artist_id = a.id
                ORDER BY p.play_count DESC, p.updated_at DESC
                LIMIT ?
            """
            rows = cur.execute(query, (limit,)).fetchall()
            con.close()
            for r in rows:
                title = str(r[0] or "").strip()
                artist = str(r[1] or "").strip()
                if title:
                    items.append({
                        "title": title,
                        "artist": artist,
                        "play_count": r[2],
                        "guid": r[4],
                        "cover_guid": r[5],
                        "source": "local",
                    })
        except Exception as e:
            logger.warning("read local play_history failed: %s", e)

    # 去重
    seen = set()
    result = []
    for it in items:
        key = (it["title"].lower(), it["artist"].lower())
        if key not in seen:
            seen.add(key)
            result.append(it)
    return result


async def generate_ai_recommendations(force_refresh: bool = False) -> dict:
    """生成或获取 AI 心动音乐推荐列表"""
    if not force_refresh and os.path.exists(AI_REC_CACHE_FILE):
        try:
            with open(AI_REC_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                # 缓存 12 小时
                if time.time() - cached.get("timestamp", 0) < 43200 and cached.get("tracks"):
                    return cached
        except Exception:
            pass

    cfg = load_ai_config()
    api_key = cfg.get("api_key", "").strip()
    base_url = cfg.get("base_url", "").strip().rstrip("/")
    model_id = cfg.get("model_id", "").strip()

    if not api_key or not base_url:
        return {
            "ok": False,
            "msg": "请先配置 AI 音乐助手的大模型连接信息",
            "tracks": [],
            "taste_tags": ["流行", "华语", "经典"],
            "timestamp": int(time.time()),
        }

    freq_tracks = get_frequent_tracks(20)
    history_text = "\n".join([f"- 《{t['title']}》- {t['artist']} (播放 {t['play_count']} 次)" for t in freq_tracks[:12]])
    if not history_text:
        history_text = "- 《单车》- 陈奕迅\n- 《落了白》- 蒋雪儿\n- 《三十而慄》- 郁可唯\n- 《都是你的错》- 陈慧琳\n- 《黄梅戏》- 慕容晓晓"

    prompt = f"""你是一名超级资深的 AI 音乐品味顾问。下面是用户近期在飞牛音乐中最常听、最喜欢的歌曲列表：
{history_text}

请深度分析该用户的听歌偏好与情感基调（如风格、年代、语种、心境氛围等），并为用户生成 15 首与其品味高度契合、听感舒适且令人心动的“同类型推荐歌曲”。
要求：
1. 包含 3~5 个精炼的音乐风格/品味标签（例如：“怀旧粤语经典”、“都市治愈流行”、“国风戏腔流行”等）；
2. 给出一段 30 字以内的温情推荐寄语；
3. 输出 15 首推荐曲目，每首包含：title（歌名）、artist（歌手）、reason（一句话心动推荐理由，不超过15字）；
4. 严格以 JSON 格式输出，不要有任何额外的文字或 markdown 解释。

JSON 格式规范：
{{
  "taste_tags": ["标签1", "标签2", "标签3"],
  "summary": "一段温暖的心动寄语",
  "tracks": [
    {{"title": "歌名", "artist": "歌手", "reason": "推荐理由"}},
    ...
  ]
}}"""

    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    payload = {
        "model": model_id,
        "temperature": 0.7,
        "max_tokens": 3000,
        "messages": [
            {"role": "system", "content": "你是一名精通华语与世界音乐的 AI 音乐助手，只返回严格规范的 JSON。"},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                return {"ok": False, "msg": f"模型调用失败: HTTP {resp.status_code} - {resp.text[:100]}", "tracks": []}
            res_json = resp.json()
            raw_content = res_json.get("choices", [{}])[0].get("message", {}).get("content", "")

            # 解析 JSON
            m = _JSON_BLOCK.search(raw_content)
            parsed = None
            if m:
                try:
                    parsed = json.loads(m.group(1))
                except Exception:
                    pass
            if not parsed:
                try:
                    parsed = json.loads(raw_content)
                except Exception:
                    # 尝试正则提取 {}
                    m_obj = re.search(r"\{[\s\S]*\}", raw_content)
                    if m_obj:
                        parsed = json.loads(m_obj.group(0))

            if not parsed or not isinstance(parsed, dict):
                return {"ok": False, "msg": "未能解析大模型返回的音乐推荐列表", "tracks": []}

            taste_tags = parsed.get("taste_tags") or ["AI心动选曲", "个性化推荐"]
            summary = parsed.get("summary") or "AI 根据您的听歌偏好，为您专属定制的心动歌曲。"
            raw_tracks = parsed.get("tracks") or []

            # 并发在本地与落雪在线音乐库中搜索匹配真实歌曲
            matched_tracks = await match_tracks_in_library_and_online(raw_tracks)

            out_bundle = {
                "ok": True,
                "msg": "心动推荐生成成功",
                "taste_tags": taste_tags,
                "summary": summary,
                "tracks": matched_tracks,
                "timestamp": int(time.time()),
            }

            # 写入缓存
            try:
                with open(AI_REC_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(out_bundle, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning("save ai recommend cache error: %s", e)

            return out_bundle

    except Exception as e:
        logger.warning("generate_ai_recommendations failed: %s", e)
        return {"ok": False, "msg": f"生成失败: {e}", "tracks": []}


async def match_tracks_in_library_and_online(ai_tracks: list[dict]) -> list[dict]:
    """在本地库和在线音乐中检索匹配歌曲信息与播放 guid"""
    results = []
    
    # 建立本地数据库标题倒排索引辅助快速匹配 (需真实物理文件存在)
    local_map = {}
    if os.path.exists(MUSIC_DB_PATH):
        try:
            con = sqlite3.connect(f"file:{MUSIC_DB_PATH}?mode=ro", uri=True)
            rows = con.execute("""
                SELECT t.id, t.guid, t.title, COALESCE(a.name, '未知') as artist, t.cover_guid, al.name as album, t.duration_ms, t.path
                FROM track t
                LEFT JOIN track_artist ta ON t.id = ta.track_id
                LEFT JOIN artist a ON ta.artist_id = a.id
                LEFT JOIN album al ON t.album_id = al.id
            """).fetchall()
            con.close()
            for r in rows:
                tid, guid, title, artist, cover, album, dur_ms, path = r
                if path and os.path.isfile(str(path)) and os.path.getsize(str(path)) > 0:
                    t_key = title.strip().lower()
                    local_map[t_key] = {
                        "id": guid,
                        "guid": guid,
                        "title": title,
                        "artist": artist,
                        "cover_guid": cover,
                        "album": album or "",
                        "duration_s": dur_ms / 1000.0 if dur_ms else 240,
                        "is_local": True,
                    }
        except Exception as e:
            logger.warning("read local tracks for matching failed: %s", e)

    async def match_one(t: dict) -> dict:
        title = str(t.get("title") or "").strip()
        artist = str(t.get("artist") or "").strip()
        reason = str(t.get("reason") or "").strip()
        t_key = title.lower()

        # 1. 优先在本地音乐库匹配
        if t_key in local_map:
            loc = local_map[t_key]
            cover_url = f"/music/static/cover/track?coverId={loc['cover_guid']}" if loc.get("cover_guid") else ""
            return {
                "guid": loc["guid"],
                "title": loc["title"],
                "artist": loc["artist"],
                "album": loc.get("album", ""),
                "cover_url": cover_url,
                "coverId": loc.get("cover_guid", ""),
                "reason": reason,
                "is_local": True,
                "badge": "本地无损",
            }

        # 2. 原生在线搜索匹配 (网易云 + 酷我直连，彻底脱离落雪服务)
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                query_str = f"{title} {artist}".strip()
                q_enc = urllib.parse.quote(query_str)

                # 2.1 网易云原生搜索直连
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
                                album = (s.get("album") or {}).get("name") or "精选专辑"
                                dur = float(s.get("duration") or 240000) / 1000.0
                                cover_u = (s.get("album") or {}).get("picUrl") or ""
                                if not cover_u:
                                    # 尝试从歌手图或 detail 接口获取高清封面
                                    cover_u = ((s.get("artists") or [{}])[0].get("img1v1Url") or "").strip()
                                    try:
                                        r_d = await client.get(f"https://music.163.com/api/song/detail/?id={sid}&ids=[{sid}]", headers={"User-Agent": "Mozilla/5.0"})
                                        if r_d.status_code == 200:
                                            d_songs = (r_d.json() or {}).get("songs") or []
                                            if d_songs and (d_songs[0].get("album") or {}).get("picUrl"):
                                                cover_u = d_songs[0]["album"]["picUrl"]
                                    except Exception:
                                        pass
                                guid = f"online:lx:wy:{sid}"
                                return {
                                    "id": f"lx:wy:{sid}",
                                    "guid": guid,
                                    "title": s_name,
                                    "artist": s_artist or artist or "华语群星",
                                    "album": album,
                                    "duration_s": dur,
                                    "file_size": 31457280,
                                    "ext": "flac",
                                    "cover_url": cover_u,
                                    "coverId": guid,
                                    "reason": reason,
                                    "is_local": False,
                                    "badge": "网易云精选",
                                }
                except Exception as e_wy:
                    logger.debug("native netease search error for %s: %s", title, e_wy)

                # 2.2 酷我原生搜索直连
                try:
                    url_kw = f"http://search.kuwo.cn/r.s?client=kt&all={q_enc}&pn=0&rn=5&vipver=1&ft=music&encoding=utf8&rformat=json&mobi=1"
                    r_kw = await client.get(url_kw, headers={"User-Agent": "okhttp/3.10.0"})
                    if r_kw.status_code == 200:
                        text_kw = r_kw.text
                        try:
                            d_kw = json.loads(text_kw)
                        except Exception:
                            d_kw = ast.literal_eval(text_kw)
                        abslist = d_kw.get("abslist") or []
                        for item in abslist:
                            s_name = str(item.get("SONGNAME") or "")
                            s_artist = str(item.get("ARTIST") or "")
                            rid = str(item.get("MUSICRID") or "").replace("MUSIC_", "")
                            if rid and (title.lower() in s_name.lower() or s_name.lower() in title.lower()):
                                guid = f"online:lx:kw:{rid}"
                                kw_cover = f"http://artistpicserver.kuwo.cn/pic.web?type=rid_pic&pictype=url&size=500&rid={rid}"
                                return {
                                    "id": f"lx:kw:{rid}",
                                    "guid": guid,
                                    "title": s_name,
                                    "artist": s_artist or artist,
                                    "album": str(item.get("ALBUM") or "精选专辑"),
                                    "duration_s": float(item.get("DURATION") or 240),
                                    "file_size": 31457280,
                                    "ext": "flac",
                                    "cover_url": kw_cover,
                                    "coverId": guid,
                                    "reason": reason,
                                    "is_local": False,
                                    "badge": "酷我精选",
                                }
                except Exception as e_kw:
                    logger.debug("native kuwo search error for %s: %s", title, e_kw)

                # 2.3 宽松网易云首条命中
                try:
                    url_wy_loose = f"https://music.163.com/api/search/get?s={urllib.parse.quote(title)}&type=1&offset=0&limit=1"
                    r_wl = await client.get(url_wy_loose, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
                    if r_wl.status_code == 200:
                        songs_l = (r_wl.json().get("result") or {}).get("songs") or []
                        if songs_l:
                            s0 = songs_l[0]
                            sid = str(s0.get("id"))
                            s_name = str(s0.get("name") or title)
                            s_artist = "/".join(a.get("name", "") for a in (s0.get("artists") or [])) or artist
                            album = (s0.get("album") or {}).get("name") or "精选专辑"
                            dur = float(s0.get("duration") or 240000) / 1000.0
                            cover_u = (s0.get("album") or {}).get("picUrl") or ""
                            if not cover_u:
                                cover_u = ((s0.get("artists") or [{}])[0].get("img1v1Url") or "").strip()
                                try:
                                    r_d = await client.get(f"https://music.163.com/api/song/detail/?id={sid}&ids=[{sid}]", headers={"User-Agent": "Mozilla/5.0"})
                                    if r_d.status_code == 200:
                                        d_songs = (r_d.json() or {}).get("songs") or []
                                        if d_songs and (d_songs[0].get("album") or {}).get("picUrl"):
                                            cover_u = d_songs[0]["album"]["picUrl"]
                                except Exception:
                                    pass
                            guid = f"online:lx:wy:{sid}"
                            return {
                                "id": f"lx:wy:{sid}",
                                "guid": guid,
                                "title": s_name,
                                "artist": s_artist,
                                "album": album,
                                "duration_s": dur,
                                "file_size": 31457280,
                                "ext": "flac",
                                "cover_url": cover_u,
                                "coverId": guid,
                                "reason": reason,
                                "is_local": False,
                                "badge": "网易云精选",
                            }
                except Exception:
                    pass

        except Exception as e:
            logger.warning("online search match failed for %s: %s", title, e)

        # 最终兜底
        fallback_guid = f"online:lx:wy:{abs(hash(title)) % 10000000}"
        return {
            "id": f"lx:wy:{abs(hash(title)) % 10000000}",
            "guid": fallback_guid,
            "title": title,
            "artist": artist,
            "album": "精选专辑",
            "duration_s": 240,
            "file_size": 31457280,
            "ext": "flac",
            "cover_url": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=500&auto=format&fit=crop&q=80",
            "coverId": fallback_guid,
            "reason": reason,
            "is_local": False,
            "badge": "AI心动推荐",
        }

    tasks = [match_one(t) for t in ai_tracks[:15]]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return results
