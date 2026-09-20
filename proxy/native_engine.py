"""
飞牛音乐内置原生自定义音源执行引擎 (Native Custom Source Engine)
彻底解耦 9528 落雪服务，在飞牛音乐本身通过 Node.js 沙箱环境运行用户导入的 JS 脚本。
"""
import os
import json
import logging
import subprocess
import asyncio
from typing import Any, Optional

logger = logging.getLogger("native_source_engine")

BASE_DIR = os.environ.get("FNMUSIC_HOME") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CUSTOM_SOURCES_DIR = os.path.join(BASE_DIR, "custom_sources")
RUNNER_SCRIPT_PATH = os.path.join(BASE_DIR, "proxy", "source_runner.js")
SOURCES_META_FILE = os.path.join(CUSTOM_SOURCES_DIR, "sources.json")

os.makedirs(CUSTOM_SOURCES_DIR, exist_ok=True)

def load_sources_meta() -> list[dict]:
    """读取本地自定义音源列表与启用状态"""
    if os.path.isfile(SOURCES_META_FILE):
        try:
            with open(SOURCES_META_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("read sources.json failed: %s", e)

    # 自动扫描目录下的所有 .js 脚本构建清单
    sources = []
    for fname in sorted(os.listdir(CUSTOM_SOURCES_DIR)):
        if fname.endswith(".js"):
            fpath = os.path.join(CUSTOM_SOURCES_DIR, fname)
            meta = parse_script_header(fpath)
            # 默认不启用，完全由用户自主控制或导入
            meta["enabled"] = False
            sources.append(meta)
    save_sources_meta(sources)
    return sources

def save_sources_meta(sources: list[dict]) -> None:
    try:
        with open(SOURCES_META_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(sources, f, ensure_ascii=False, indent=2)
        os.replace(SOURCES_META_FILE + ".tmp", SOURCES_META_FILE)
    except Exception as e:
        logger.error("save sources.json failed: %s", e)

def parse_script_header(filepath: str) -> dict:
    fname = os.path.basename(filepath)
    name = fname.replace(".js", "")
    author = "内置"
    version = "1.0.0"
    desc = "飞牛音乐原生自定义音源"
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(2048)
            for line in content.splitlines():
                line_str = line.strip()
                if line_str.startswith("* @name") or line_str.startswith("@name"):
                    name = line_str.replace("* @name", "").replace("@name", "").strip()
                elif line_str.startswith("* @author") or line_str.startswith("@author"):
                    author = line_str.replace("* @author", "").replace("@author", "").strip()
                elif line_str.startswith("* @version") or line_str.startswith("@version"):
                    version = line_str.replace("* @version", "").replace("@version", "").strip()
                elif line_str.startswith("* @description") or line_str.startswith("@description"):
                    desc = line_str.replace("* @description", "").replace("@description", "").strip()
    except Exception:
        pass

    return {
        "id": fname,
        "name": name,
        "author": author,
        "version": version,
        "description": desc,
        "size": os.path.getsize(filepath) if os.path.exists(filepath) else 0,
        "enabled": False,
        "supportedSources": ["kw", "kg", "tx", "wy", "mg"]
    }

async def resolve_music_url_native(song_info: dict, quality: str = "flac") -> Optional[dict]:
    """
    通过本地已启用的自定义音源脚本解析真实音频播放直链。
    无需依赖 9528 落雪服务，直接由本地 Node.js 沙箱执行！
    """
    sources = load_sources_meta()
    enabled_sources = [s for s in sources if s.get("enabled")]

    sid = str(song_info.get("songmid") or song_info.get("id") or "").strip()
    src = str(song_info.get("source") or "kw").strip()
    m_info = {
        "id": sid,
        "songmid": sid,
        "name": str(song_info.get("name") or song_info.get("title") or "").strip(),
        "singer": str(song_info.get("singer") or song_info.get("artist") or "").strip(),
        "interval": str(song_info.get("interval") or "04:00").strip()
    }

    # 遍历已启用的音源依次尝试解析
    for s in enabled_sources:
        js_file = os.path.join(CUSTOM_SOURCES_DIR, s["id"])
        if not os.path.isfile(js_file):
            continue

        payload = {
            "source_file": js_file,
            "action": "musicUrl",
            "source": src,
            "music_info": m_info,
            "quality": quality
        }

        try:
            # 异步执行本地 node 进程
            proc = await asyncio.create_subprocess_exec(
                "node", RUNNER_SCRIPT_PATH,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=json.dumps(payload).encode("utf-8")),
                timeout=8.0
            )
            raw_out = stdout.decode("utf-8").strip()
            if raw_out:
                try:
                    res_json = json.loads(raw_out)
                    if res_json.get("ok") and res_json.get("url"):
                        u = str(res_json["url"]).strip()
                        # 过滤无效占位或 None 链接
                        if u and not u.endswith("/None") and "null" not in u:
                            return {
                                "url": u,
                                "format": res_json.get("format", "flac"),
                                "sourceName": s.get("name") or "飞牛内置音源"
                            }
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Source %s resolve error: %s", s["id"], e)

    return None


async def resolve_music_pic_native(song_info: dict) -> Optional[str]:
    """通过本地已启用的自定义音源脚本解析封面大图 URL"""
    sources = load_sources_meta()
    enabled_sources = [s for s in sources if s.get("enabled")]

    sid = str(song_info.get("songmid") or song_info.get("id") or "").strip()
    src = str(song_info.get("source") or "kw").strip()
    m_info = {
        "id": sid,
        "songmid": sid,
        "name": str(song_info.get("name") or song_info.get("title") or "").strip(),
        "singer": str(song_info.get("singer") or song_info.get("artist") or "").strip(),
    }

    for s in enabled_sources:
        js_file = os.path.join(CUSTOM_SOURCES_DIR, s["id"])
        if not os.path.isfile(js_file):
            continue
        payload = {
            "source_file": js_file,
            "action": "pic",
            "source": src,
            "music_info": m_info,
            "quality": "128k"
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", RUNNER_SCRIPT_PATH,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=json.dumps(payload).encode("utf-8")),
                timeout=6.0
            )
            raw_out = stdout.decode("utf-8").strip()
            if raw_out:
                res_json = json.loads(raw_out)
                if res_json.get("ok") and res_json.get("url"):
                    u = str(res_json["url"]).strip()
                    if u.startswith("http"):
                        return u
        except Exception as e:
            logger.debug("Source %s pic resolve error: %s", s["id"], e)

    return None


async def resolve_music_lyric_native(song_info: dict) -> Optional[dict]:
    """通过本地已启用的自定义音源脚本解析同步 LRC 歌词"""
    sources = load_sources_meta()
    enabled_sources = [s for s in sources if s.get("enabled")]

    sid = str(song_info.get("songmid") or song_info.get("id") or "").strip()
    src = str(song_info.get("source") or "kw").strip()
    m_info = {
        "id": sid,
        "songmid": sid,
        "name": str(song_info.get("name") or song_info.get("title") or "").strip(),
        "singer": str(song_info.get("singer") or song_info.get("artist") or "").strip(),
    }

    for s in enabled_sources:
        js_file = os.path.join(CUSTOM_SOURCES_DIR, s["id"])
        if not os.path.isfile(js_file):
            continue
        payload = {
            "source_file": js_file,
            "action": "lyric",
            "source": src,
            "music_info": m_info,
            "quality": "128k"
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", RUNNER_SCRIPT_PATH,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=json.dumps(payload).encode("utf-8")),
                timeout=6.0
            )
            raw_out = stdout.decode("utf-8").strip()
            if raw_out:
                res_json = json.loads(raw_out)
                if res_json.get("ok") and res_json.get("lyric"):
                    return {
                        "lyric": str(res_json["lyric"]).strip(),
                        "tlyric": str(res_json.get("tlyric") or "").strip()
                    }
        except Exception as e:
            logger.debug("Source %s lyric resolve error: %s", s["id"], e)

    return None
