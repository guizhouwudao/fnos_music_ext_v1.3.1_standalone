"""
飞牛音乐原生 WebDAV 同步与云端备份模块 (FNOS Native WebDAV Sync)
直接将飞牛音乐自身的全量备份 ZIP 包上传至用户指定的 WebDAV 目录 (/115云盘-152/文件备份)。
完全独立运行，不依赖 9528！
"""
import os
import time
import json
import logging
import httpx
from typing import Optional

logger = logging.getLogger("fn_webdav")

CONFIG_FILE = "/root/.local/state/fnmusic_ext/webdav_config.json"
LOGS_FILE = "/root/.local/state/fnmusic_ext/webdav_logs.json"

DEFAULT_CONFIG = {
    "url": "",
    "username": "",
    "password": "",
    "path": "/music_backup",
    "auto_backup": False,
    "cron": "0 4 * * *"
}

def load_webdav_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_webdav_config(cfg: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("save webdav config failed: %s", e)

def add_log(msg: str) -> None:
    logs = get_logs()
    now_str = time.strftime('%Y-%m-%d %H:%M:%S')
    logs.insert(0, f"[{now_str}] {msg}")
    logs = logs[:50]
    try:
        os.makedirs(os.path.dirname(LOGS_FILE), exist_ok=True)
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"logs": logs}, f, ensure_ascii=False)
    except Exception:
        pass

def get_logs() -> list[str]:
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("logs", [])
        except Exception:
            pass
    return []

async def upload_backup_to_webdav(zip_path: str) -> bool:
    """上传本地生成的飞牛音乐全量备份至 WebDAV"""
    cfg = load_webdav_config()
    base_url = cfg.get("url", "").rstrip("/")
    username = cfg.get("username", "")
    password = cfg.get("password", "")
    target_dir = cfg.get("path", "").strip().strip("/")

    if not base_url or not base_url.startswith("http"):
        add_log("上传失败：未配置正确的 WebDAV URL")
        return False

    auth = (username, password) if username or password else None
    fname = os.path.basename(zip_path)
    remote_url = f"{base_url}/{target_dir}/{fname}" if target_dir else f"{base_url}/{fname}"

    add_log(f"开始上传备份包 {fname} 至 WebDAV: {remote_url}")

    try:
        with open(zip_path, "rb") as f:
            data = f.read()

        async with httpx.AsyncClient(timeout=60.0, verify=False, follow_redirects=True) as client:
            resp = await client.put(remote_url, content=data, auth=auth, headers={"Content-Type": "application/zip"})
            if resp.status_code in (200, 201, 204):
                add_log(f"✅ 备份包 {fname} 上传成功！({round(len(data)/1024, 1)} KB)")
                return True
            else:
                add_log(f"❌ 上传失败 (HTTP {resp.status_code}): {resp.text[:100]}")
                return False
    except Exception as e:
        add_log(f"❌ 上传异常: {e}")
        return False
