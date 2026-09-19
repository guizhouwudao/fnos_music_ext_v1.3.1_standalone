# -*- coding: utf-8 -*-
"""
NetEase Cloud Music (网易云音乐) API & VIP/SVIP Audio / Resource Service
Based on encryption algorithms from darknessomi/musicbox
"""

import os
import io
import re
import json
import base64
import random
import string
import hashlib
import binascii
import logging
from typing import Any, Optional, Tuple, Dict
from http.cookies import SimpleCookie
import httpx
from Crypto.Cipher import AES
import qrcode
import qrcode.image.svg

logger = logging.getLogger("fnmusic_netease")

MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b"
    "3ece0462db0a22b8e7"
)
PUBKEY = "010001"
NONCE = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

NETEASE_CONFIG_PATH = "/root/.local/state/fnmusic_ext/netease_config.json"

QUALITY_LEVEL_LABELS = {
    "jymaster": "超清母带 (Master)",
    "sky": "沉浸环绕声 (Surround Audio)",
    "jyeffect": "高清臻音 (HD Spatial)",
    "lossless": "无损品质 (FLAC / Hi-Res)",
    "exhigh": "极高品质 (320Kbps MP3)",
    "standard": "标准品质 (128Kbps MP3)"
}

DEFAULT_NETEASE_CONFIG = {
    "enabled": False,
    "enable_svip": False,   # 启用网易云音乐 SVIP 专属音源
    "cookie": "",
    "quality": "lossless",  # jymaster, sky, jyeffect, lossless, exhigh, standard
    "auto_enrich": True,    # 自动使用网易云封面和歌词补全缺失资源
    "enable_failover": True,# 落雪源无法解析时自动由网易云音乐 API 解析播放与下载
    "user_info": {
        "is_logged_in": False,
        "user_id": 0,
        "nickname": "未登录",
        "avatar_url": "",
        "vip_type": 0,
        "vip_level": "未登录",
        "is_svip": False,
        "vip_expire_time": 0,
        "vip_expire_str": ""
    },
    "updated_at": 0
}


def create_key(size: int = 16) -> bytes:
    return binascii.hexlify(os.urandom(size))[:size]


def aes_cbc(text: bytes, key: bytes, iv: bytes = IV) -> bytes:
    pad = 16 - len(text) % 16
    text = text + bytes([pad] * pad)
    encryptor = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = encryptor.encrypt(text)
    return base64.b64encode(ciphertext)


def rsa_mod(text: bytes, pubkey: str, modulus: str) -> str:
    text_rev = text[::-1]
    rs = pow(int(binascii.hexlify(text_rev), 16), int(pubkey, 16), int(modulus, 16))
    return format(rs, "x").zfill(256)


def weapi_encrypt(payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    secret = create_key(16)
    params = aes_cbc(aes_cbc(data, NONCE), secret)
    encseckey = rsa_mod(secret, PUBKEY, MODULUS)
    return {"params": params.decode("utf-8"), "encSecKey": encseckey}


def get_headers(cookie: str = "") -> dict:
    h = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://music.163.com/",
        "Origin": "https://music.163.com",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    cookie_str = "os=pc; osver=Microsoft-Windows-10; appver=8.9.70;"
    if cookie:
        cookie_str += " " + cookie.strip()
    h["Cookie"] = cookie_str
    return h


def load_netease_config() -> dict:
    os.makedirs(os.path.dirname(NETEASE_CONFIG_PATH), exist_ok=True)
    if not os.path.exists(NETEASE_CONFIG_PATH):
        save_netease_config(DEFAULT_NETEASE_CONFIG)
        return dict(DEFAULT_NETEASE_CONFIG)
    try:
        with open(NETEASE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            cur = dict(DEFAULT_NETEASE_CONFIG)
            cur.update(data)
            return cur
    except Exception as e:
        logger.warning(f"Error loading netease config: {e}")
        return dict(DEFAULT_NETEASE_CONFIG)


def save_netease_config(cfg: dict) -> dict:
    os.makedirs(os.path.dirname(NETEASE_CONFIG_PATH), exist_ok=True)
    cur = load_netease_config() if os.path.exists(NETEASE_CONFIG_PATH) else dict(DEFAULT_NETEASE_CONFIG)
    cur.update(cfg)
    cur["updated_at"] = int(os.times().system or 0)
    with open(NETEASE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return cur


class NetEaseClient:
    def __init__(self):
        self.base_url = "https://music.163.com"

    async def _post_weapi_with_response(self, path: str, payload: dict, cookie: str = "") -> Tuple[dict, httpx.Response | None]:
        url = f"{self.base_url}{path}"
        headers = get_headers(cookie)
        body = weapi_encrypt(payload)
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                r = await client.post(url, headers=headers, data=body)
                if r.status_code == 200:
                    return r.json(), r
            except Exception as e:
                logger.error(f"NetEase WeAPI POST {path} failed: {e}")
        return {}, None

    async def _post_weapi(self, path: str, payload: dict, cookie: str = "") -> dict:
        data, _ = await self._post_weapi_with_response(path, payload, cookie)
        return data

    async def create_qr_key(self) -> dict:
        """生成二维码 key 和展示用 SVG / URL"""
        payload = {"type": 3, "csrf_token": ""}
        res = await self._post_weapi("/weapi/login/qrcode/unikey", payload)
        unikey = res.get("unikey") or res.get("data", {}).get("unikey")
        if not unikey:
            return {"ok": False, "msg": "获取二维码失败，请稍后重试"}
        
        qr_url = f"https://music.163.com/login?codekey={unikey}"
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(qr_url, image_factory=factory)
        buf = io.BytesIO()
        img.save(buf)
        svg_content = buf.getvalue().decode("utf-8")
        
        return {
            "ok": True,
            "unikey": unikey,
            "qr_url": qr_url,
            "qr_svg": svg_content
        }

    async def check_qr_status(self, unikey: str) -> dict:
        """
        检查二维码扫码状态并精确提取登录 Cookie
        800: 过期
        801: 等待扫码
        802: 待手机确认
        803: 授权登录成功，返回 cookie
        """
        payload = {"type": 3, "key": unikey, "csrf_token": ""}
        res, resp = await self._post_weapi_with_response("/weapi/login/qrcode/client/login", payload)
        code = res.get("code")
        msg = res.get("message", "")
        
        if code == 803:
            # 兼容多种 cookie 返回方式：res['cookie'] 或 HTTP 响应头 Set-Cookie
            cookie_parts = []
            if res.get("cookie"):
                cookie_parts.append(str(res["cookie"]).strip())
            
            if resp is not None:
                for sc in resp.headers.get_list("set-cookie"):
                    # 提取形如 NAME=VALUE 的核心 Cookie
                    parts = sc.split(";", 1)[0].strip()
                    if parts and not parts.lower().startswith("expires=") and not parts.lower().startswith("path="):
                        cookie_parts.append(parts)
            
            # 去重并组装完整 Cookie 字符串
            cookie_dict = {}
            for item in cookie_parts:
                for segment in item.split(";"):
                    if "=" in segment:
                        k, v = segment.strip().split("=", 1)
                        if k and v:
                            cookie_dict[k.strip()] = v.strip()
            
            final_cookie = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
            logger.info(f"NetEase QR Login 803 success! Extracted cookie keys: {list(cookie_dict.keys())}")
            
            # 立即联网验证与提取用户信息
            user_info = await self.fetch_user_profile(final_cookie)
            cfg = load_netease_config()
            cfg["cookie"] = final_cookie
            cfg["user_info"] = user_info
            cfg["enabled"] = True
            save_netease_config(cfg)
            
            return {
                "code": 803,
                "msg": "登录成功！",
                "cookie": final_cookie,
                "user_info": user_info
            }
        elif code == 802:
            return {"code": 802, "msg": "已扫描，请在网易云音乐手机客户端点击确认登录"}
        elif code == 801:
            return {"code": 801, "msg": "等待扫码中..."}
        elif code == 800:
            return {"code": 800, "msg": "二维码已过期，请刷新"}
        return {"code": code or 500, "msg": msg or "未知状态"}

    async def fetch_user_profile(self, cookie: str) -> dict:
        """获取网易云用户信息、精准区分 VIP / SVIP 会员状态及有效期"""
        import time
        now_ts = int(time.time() * 1000)

        if not cookie:
            return {
                "is_logged_in": False,
                "nickname": "未登录",
                "vip_level": "未登录",
                "is_svip": False,
                "vip_expire_time": 0,
                "vip_expire_str": ""
            }
        
        # 1. 查询账号基础信息
        res = await self._post_weapi("/weapi/nuser/account/get", {"csrf_token": ""}, cookie=cookie)
        account = res.get("account") or {}
        profile = res.get("profile") or {}
        
        if not profile and not account:
            return {
                "is_logged_in": False,
                "nickname": "Cookie已失效",
                "vip_level": "已失效",
                "is_svip": False,
                "vip_expire_time": 0,
                "vip_expire_str": ""
            }
        
        user_id = profile.get("userId") or account.get("id") or 0
        nickname = profile.get("nickname") or "网易云用户"
        avatar_url = profile.get("avatarUrl") or ""
        vip_type = profile.get("vipType") or account.get("vipType") or 0
        
        # 2. 查询更详细的 VIP / SVIP 会员信息
        vip_res = await self._post_weapi("/weapi/music-vip-membership/front/vip/info", {"csrf_token": ""}, cookie=cookie)
        vip_data = vip_res.get("data") or {}
        
        is_svip = False
        vip_level = "普通用户"
        expire_time = 0
        
        # 检查各级别会员数据
        associator = vip_data.get("associator") or {}
        music_package = vip_data.get("musicPackage") or {}
        redplus = vip_data.get("redplus") or {}
        
        redplus_code = redplus.get("vipCode") or 0
        redplus_expire = redplus.get("expireTime") or 0
        redplus_level = redplus.get("vipLevel") or 0
        
        assoc_code = associator.get("vipCode") or 0
        assoc_expire = associator.get("expireTime") or 0
        assoc_level = associator.get("vipLevel") or 0
        
        pkg_code = music_package.get("vipCode") or 0
        pkg_expire = music_package.get("expireTime") or 0
        
        # 精确判断 SVIP (必须拥有有效的 redplus 权限且未过期)
        if redplus_code > 0 and redplus_expire > now_ts and redplus_level > 0:
            is_svip = True
            vip_level = "👑 黑胶SVIP会员"
            expire_time = redplus_expire
        elif (assoc_code == 100 and assoc_expire > now_ts) or (vip_type in (11, 110) and assoc_expire > now_ts):
            # 真实黑胶 VIP
            vip_level = "👑 黑胶VIP"
            expire_time = assoc_expire
        elif assoc_code == 100 or vip_type in (11, 110):
            vip_level = "👑 黑胶VIP"
            expire_time = assoc_expire or now_ts
        elif pkg_code in (10, 220) and pkg_expire > now_ts:
            vip_level = "🎵 音乐包会员"
            expire_time = pkg_expire
        elif vip_type > 0:
            vip_level = f"VIP会员 (Type:{vip_type})"
            expire_time = assoc_expire or pkg_expire or 0
            
        expire_str = ""
        if expire_time and expire_time > 0:
            try:
                expire_str = time.strftime("%Y-%m-%d", time.localtime(expire_time / 1000))
            except Exception:
                pass

        return {
            "is_logged_in": True,
            "user_id": user_id,
            "nickname": nickname,
            "avatar_url": avatar_url,
            "vip_type": vip_type,
            "vip_level": vip_level,
            "is_svip": is_svip,
            "vip_expire_time": expire_time,
            "vip_expire_str": expire_str
        }

    async def get_song_url(self, song_id: str | int, level: str = "lossless", cookie: str = "") -> dict:
        """
        解析单曲播放音频直链（全面支持 超清母带、沉浸环绕声、高清臻音、无损品质、极高、标准）
        """
        # 优先使用 /weapi/song/enhance/player/url/v1 现代化格式接口
        v1_payload = {
            "ids": [int(song_id)],
            "level": level,
            "encodeType": "flac" if level in ("lossless", "jymaster", "sky", "jyeffect") else "mp3",
            "csrf_token": ""
        }
        res_v1 = await self._post_weapi("/weapi/song/enhance/player/url/v1", v1_payload, cookie=cookie)
        data_list = res_v1.get("data", [])
        if data_list:
            item = data_list[0]
            url = item.get("url")
            code = item.get("code")
            if url and (code == 200 or str(code) == "200"):
                return {
                    "ok": True,
                    "url": url,
                    "br": item.get("br"),
                    "size": item.get("size"),
                    "level": item.get("level") or level,
                    "type": item.get("type") or ("flac" if level in ("lossless", "jymaster") else "mp3"),
                    "level_desc": QUALITY_LEVEL_LABELS.get(item.get("level") or level, level)
                }

        # 降级 fallback 到老版接口
        br_map = {
            "jymaster": 999000,
            "sky": 999000,
            "jyeffect": 999000,
            "lossless": 999000,
            "hires": 999000,
            "exhigh": 320000,
            "standard": 128000
        }
        br = br_map.get(level, 999000)
        payload = {
            "ids": f"[{song_id}]",
            "br": br,
            "csrf_token": ""
        }
        res = await self._post_weapi("/weapi/song/enhance/player/url", payload, cookie=cookie)
        data_list = res.get("data", [])
        if data_list:
            item = data_list[0]
            url = item.get("url")
            code = item.get("code")
            if url and (code == 200 or str(code) == "200"):
                return {
                    "ok": True,
                    "url": url,
                    "br": item.get("br"),
                    "size": item.get("size"),
                    "level": item.get("level") or level,
                    "type": item.get("type") or "flac",
                    "level_desc": QUALITY_LEVEL_LABELS.get(item.get("level") or level, level)
                }
        return {"ok": False, "msg": "未能获取该歌曲的音频播放直链（可能需网易云黑胶VIP/SVIP或版权受限）"}

    async def get_lyric(self, song_id: str | int) -> dict:
        """获取歌曲滚动 LRC 歌词及翻译"""
        url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://music.163.com/"
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    res = r.json()
                    lrc = (res.get("lrc") or {}).get("lyric", "")
                    tlyric = (res.get("tlyric") or {}).get("lyric", "")
                    if lrc:
                        return {
                            "ok": True,
                            "lyric": lrc,
                            "tlyric": tlyric
                        }
            except Exception as e:
                logger.warning(f"NetEase get_lyric failed for {song_id}: {e}")
        return {"ok": False, "lyric": "", "tlyric": ""}

    async def search_songs(self, keyword: str, limit: int = 30) -> list:
        """云搜索歌曲，返回包含歌曲ID、名称、歌手、专辑、高清封面图的列表"""
        payload = {
            "s": keyword,
            "type": 1,
            "limit": limit,
            "offset": 0,
            "total": True,
            "csrf_token": ""
        }
        res = await self._post_weapi("/weapi/cloudsearch/pc", payload)
        songs = res.get("result", {}).get("songs", [])
        results = []
        for s in songs:
            artists = [a.get("name") for a in s.get("ar", []) if a.get("name")]
            artist_str = " & ".join(artists) if artists else "群星"
            al = s.get("al", {})
            results.append({
                "id": str(s.get("id")),
                "name": s.get("name"),
                "artist": artist_str,
                "album": al.get("name") or "",
                "pic_url": al.get("picUrl") or "",
                "duration": int((s.get("dt") or 0) / 1000),
                "fee": s.get("fee", 0)
            })
        return results

    async def enrich_cover_and_lyric(self, title: str, artist: str = "") -> dict:
        """
        利用网易云音乐海量曲库，为缺失封面与歌词的歌曲进行智能匹配与高清资源填补
        """
        kw = f"{title} {artist}".strip()
        songs = await self.search_songs(kw, limit=5)
        if not songs:
            return {"ok": False, "msg": "未检索到匹配的网易云歌曲"}
        
        # 优先匹配歌手完全对齐的歌曲
        clean_ar = artist.lower().strip() if artist else ""
        selected_song = songs[0]
        selected_lrc = {}
        
        for s in songs:
            s_art = (s.get("artist") or "").lower()
            if clean_ar and (clean_ar in s_art or s_art in clean_ar):
                lrc = await self.get_lyric(s["id"])
                if lrc.get("ok") and lrc.get("lyric"):
                    selected_song = s
                    selected_lrc = lrc
                    break
                    
        if not selected_lrc.get("lyric"):
            # 兜底测试第一首
            selected_lrc = await self.get_lyric(selected_song["id"])

        return {
            "ok": True,
            "song_id": selected_song["id"],
            "title": selected_song["name"],
            "artist": selected_song["artist"],
            "album": selected_song["album"],
            "pic_url": selected_song["pic_url"],
            "lyric": selected_lrc.get("lyric", ""),
            "tlyric": selected_lrc.get("tlyric", "")
        }

    async def resolve_failover_track(self, song_id: str = "", title: str = "", artist: str = "") -> dict:
        """
        当落雪源解析失败或无直链时，无缝通过网易云 API 故障转移解析直链并补全资源 (播放与下载通用)
        """
        cfg = load_netease_config()
        if not cfg.get("enable_failover", True):
            return {"ok": False, "msg": "网易云故障转移未启用"}
            
        cookie = cfg.get("cookie", "")
        quality = cfg.get("quality", "lossless")
        
        target_song_id = None
        target_meta = {}
        
        # 1. 尝试直接从 song_id 提取网易云歌曲 ID
        sid = str(song_id or "")
        if "wy:" in sid:
            parts = sid.split("wy:")[-1].split(":")
            if parts and parts[0].isdigit():
                target_song_id = parts[0]
        elif sid.isdigit() and len(sid) >= 4:
            target_song_id = sid
            
        # 2. 如果没有直接的网易云 ID，使用歌名与歌手在网易云检索匹配
        if not target_song_id and title:
            kw = f"{title} {artist}".strip()
            songs = await self.search_songs(kw, limit=5)
            if songs:
                clean_title = re.sub(r"[\s\(\)（）\[\]【】]", "", title.lower())
                clean_ar = re.sub(r"[\s\(\)（）\[\]【】]", "", artist.lower()) if artist else ""
                best_song = songs[0]

                # 优先匹配歌手与歌名双重对齐的曲目
                found_exact = False
                if clean_ar:
                    for s in songs:
                        s_name = re.sub(r"[\s\(\)（）\[\]【】]", "", (s.get("name") or "").lower())
                        s_art = re.sub(r"[\s\(\)（）\[\]【】]", "", (s.get("artist") or "").lower())
                        if (clean_title in s_name or s_name in clean_title) and (clean_ar in s_art or s_art in clean_ar):
                            best_song = s
                            found_exact = True
                            break
                if not found_exact:
                    for s in songs:
                        s_clean = re.sub(r"[\s\(\)（）\[\]【】]", "", (s.get("name") or "").lower())
                        if clean_title in s_clean or s_clean in clean_title:
                            best_song = s
                            break
                target_song_id = best_song["id"]
                target_meta = best_song
                
        if not target_song_id:
            return {"ok": False, "msg": f"未能通过网易云曲库匹配到《{title} - {artist}》"}
            
        # 3. 获取播放直链
        url_res = await self.get_song_url(target_song_id, level=quality, cookie=cookie)
        if not url_res.get("ok") or not url_res.get("url"):
            # 降级尝试标准品质
            if quality != "standard":
                url_res = await self.get_song_url(target_song_id, level="standard", cookie=cookie)
            if not url_res.get("ok") or not url_res.get("url"):
                return {"ok": False, "msg": url_res.get("msg") or "获取网易云音频直链失败"}
            
        # 4. 获取歌词与封面
        lrc_res = await self.get_lyric(target_song_id)
        final_lrc = lrc_res.get("lyric") or ""
        final_tlrc = lrc_res.get("tlyric") or ""
        final_pic = target_meta.get("pic_url") or ""

        # 智能补全：若当前曲目歌词或封面缺失，从同名优质曲目中提取补全
        if (not final_lrc or not final_pic) and songs:
            for s in songs:
                if not final_pic and s.get("pic_url"):
                    final_pic = s["pic_url"]
                if not final_lrc:
                    try:
                        cand_lrc = await self.get_lyric(s["id"])
                        if cand_lrc.get("ok") and cand_lrc.get("lyric"):
                            final_lrc = cand_lrc["lyric"]
                            final_tlrc = cand_lrc.get("tlyric") or ""
                    except Exception:
                        pass
                if final_lrc and final_pic:
                    break

        return {
            "ok": True,
            "song_id": target_song_id,
            "url": url_res["url"],
            "ext": url_res.get("type") or "flac",
            "quality": url_res.get("level") or quality,
            "quality_desc": url_res.get("level_desc") or QUALITY_LEVEL_LABELS.get(quality, quality),
            "size": url_res.get("size") or 31457280,
            "title": target_meta.get("name") or title,
            "artist": target_meta.get("artist") or artist,
            "album": target_meta.get("album") or "",
            "pic_url": final_pic,
            "lyric": final_lrc,
            "tlyric": final_tlrc,
            "source": "网易云音乐 (VIP故障转移)"
        }


netease_client = NetEaseClient()
