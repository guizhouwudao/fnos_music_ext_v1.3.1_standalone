"""
飞牛音乐原生数据备份与快照管理服务 (FNOS Music Backup & Snapshot Service)
彻底服务于飞牛音乐本身：
1. 飞牛原生数据库 (/usr/local/apps/@appdata/trim.music/db/music.db)
2. 下载记录数据库 (/vol1/1000/tools/fnmusic_ext/downloads_db/downloads.db)
3. 扩展设置、AI配置、网易云音源配置、自定义音源JS脚本
4. 生成版本化快照并支持秒级回滚与本地 ZIP 导入导出
"""
import os
import io
import time
import json
import zipfile
import shutil
import sqlite3
import logging
from typing import Any, Optional

logger = logging.getLogger("fn_music_backup")

BASE_DIR = os.environ.get("FNMUSIC_HOME") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
SNAPSHOT_DIR = os.path.join(BACKUP_DIR, "snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

FN_MUSIC_DB = "/usr/local/apps/@appdata/trim.music/db/music.db"
DOWNLOADS_DB = os.path.join(BASE_DIR, "downloads_db", "downloads.db")
SETTINGS_FILE = "/root/.local/state/fnmusic_ext/settings.json"
AI_CONFIG_FILE = "/root/.local/state/fnmusic_ext/ai_config.json"
NETEASE_CONFIG_FILE = "/root/.local/state/fnmusic_ext/netease_config.json"
CUSTOM_SOURCES_DIR = os.path.join(BASE_DIR, "custom_sources")

def create_fn_music_backup_zip(backup_type: str = "manual") -> str:
    """创建飞牛音乐全量数据备份包"""
    ts = int(time.time())
    zip_name = f"fnmusic-backup-{time.strftime('%Y%m%d-%H%M%S')}-{ts}.zip"
    zip_path = os.path.join(SNAPSHOT_DIR, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. 备份原生数据库
        if os.path.exists(FN_MUSIC_DB):
            zf.write(FN_MUSIC_DB, arcname="db/music.db")
        # 2. 备份下载数据库
        if os.path.exists(DOWNLOADS_DB):
            zf.write(DOWNLOADS_DB, arcname="db/downloads.db")
        # 3. 备份配置
        if os.path.exists(SETTINGS_FILE):
            zf.write(SETTINGS_FILE, arcname="config/settings.json")
        if os.path.exists(AI_CONFIG_FILE):
            zf.write(AI_CONFIG_FILE, arcname="config/ai_config.json")
        if os.path.exists(NETEASE_CONFIG_FILE):
            zf.write(NETEASE_CONFIG_FILE, arcname="config/netease_config.json")
        # 4. 备份自定义音源
        if os.path.exists(CUSTOM_SOURCES_DIR):
            for f in os.listdir(CUSTOM_SOURCES_DIR):
                fp = os.path.join(CUSTOM_SOURCES_DIR, f)
                if os.path.isfile(fp):
                    zf.write(fp, arcname=f"custom_sources/{f}")

        # 元数据
        meta = {
            "version": "1.0.2",
            "time": ts,
            "datetime": time.strftime('%Y-%m-%d %H:%M:%S'),
            "type": backup_type,
            "filename": zip_name
        }
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    logger.info("Created FN Music backup zip: %s (%d bytes)", zip_path, os.path.getsize(zip_path))
    try:
        from features import load_settings
        cfg = load_settings()
        max_num = int(cfg.get("maxSnapshotNum") or cfg.get("max_snapshots") or 10)
        prune_snapshots(max_num)
    except Exception as pe:
        logger.warning("Auto prune snapshots error: %s", pe)
    return zip_path

def prune_snapshots(max_num: int = 10) -> int:
    """清理超出数量上限的飞牛音乐历史快照，保留最新的 max_num 份"""
    if max_num <= 0:
        max_num = 10
    snaps = list_snapshots()
    if len(snaps) <= max_num:
        return 0
    to_delete = snaps[max_num:]
    deleted = 0
    for s in to_delete:
        fp = os.path.join(SNAPSHOT_DIR, s["filename"])
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                deleted += 1
                logger.info("Pruned old snapshot: %s", s["filename"])
        except Exception as e:
            logger.warning("Failed to remove old snapshot %s: %s", fp, e)
    return deleted

def list_snapshots() -> list[dict]:
    """列出所有飞牛音乐历史快照"""
    snaps = []
    if not os.path.exists(SNAPSHOT_DIR):
        return []

    for f in sorted(os.listdir(SNAPSHOT_DIR), reverse=True):
        if f.endswith(".zip"):
            fp = os.path.join(SNAPSHOT_DIR, f)
            st = os.stat(fp)
            snap_id = f.replace(".zip", "")
            snaps.append({
                "id": snap_id,
                "time": int(st.st_mtime * 1000),
                "size": st.st_size,
                "filename": f
            })
    return snaps

def restore_snapshot_by_id(snap_id: str) -> bool:
    """按快照 ID 恢复飞牛音乐数据"""
    zip_path = os.path.join(SNAPSHOT_DIR, f"{snap_id}.zip")
    if not os.path.isfile(zip_path):
        return False
    return restore_from_zip(zip_path)

def restore_from_zip(zip_path: str) -> bool:
    """解压并还原全量飞牛音乐数据"""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()
            if "db/music.db" in namelist and os.path.exists(os.path.dirname(FN_MUSIC_DB)):
                with open(FN_MUSIC_DB + ".bak", "wb") as bf:
                    bf.write(open(FN_MUSIC_DB, "rb").read())
                with open(FN_MUSIC_DB, "wb") as df:
                    df.write(zf.read("db/music.db"))

            if "db/downloads.db" in namelist and os.path.exists(os.path.dirname(DOWNLOADS_DB)):
                with open(DOWNLOADS_DB, "wb") as df:
                    df.write(zf.read("db/downloads.db"))

            if "config/settings.json" in namelist:
                os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
                with open(SETTINGS_FILE, "wb") as sf:
                    sf.write(zf.read("config/settings.json"))

            if "config/ai_config.json" in namelist:
                with open(AI_CONFIG_FILE, "wb") as sf:
                    sf.write(zf.read("config/ai_config.json"))

            if "config/netease_config.json" in namelist:
                with open(NETEASE_CONFIG_FILE, "wb") as sf:
                    sf.write(zf.read("config/netease_config.json"))

            for n in namelist:
                if n.startswith("custom_sources/") and not n.endswith("/"):
                    fname = os.path.basename(n)
                    if fname:
                        os.makedirs(CUSTOM_SOURCES_DIR, exist_ok=True)
                        with open(os.path.join(CUSTOM_SOURCES_DIR, fname), "wb") as f:
                            f.write(zf.read(n))
        return True
    except Exception as e:
        logger.error("Restore from zip failed: %s", e)
        return False

def get_fn_music_data_summary() -> dict:
    """获取飞牛音乐自身的数据概览（媒体库曲目、歌单、下载与自定义音源）"""
    tracks_list = []
    playlists_list = []
    downloads_list = []

    # 1. 飞牛音乐数据库曲目
    if os.path.exists(FN_MUSIC_DB):
        try:
            conn = sqlite3.connect(FN_MUSIC_DB)
            c = conn.cursor()
            c.execute("SELECT id, title, year, cover_guid FROM track LIMIT 200")
            for r in c.fetchall():
                tracks_list.append({
                    "id": str(r[0]),
                    "name": str(r[1] or ""),
                    "singer": "本地媒体库",
                    "source": "FNOS",
                    "interval": "--:--"
                })

            c.execute("SELECT id, name FROM playlist LIMIT 50")
            for r in c.fetchall():
                playlists_list.append({
                    "id": str(r[0]),
                    "name": str(r[1] or "未命名歌单"),
                    "list": []
                })
            conn.close()
        except Exception as e:
            logger.warning("read music.db summary failed: %s", e)

    # 2. 下载记录
    if os.path.exists(DOWNLOADS_DB):
        try:
            conn = sqlite3.connect(DOWNLOADS_DB)
            c = conn.cursor()
            c.execute("SELECT title, artist, source, quality, status FROM download_records ORDER BY id DESC LIMIT 100")
            dl_tracks = []
            for r in c.fetchall():
                dl_tracks.append({
                    "name": str(r[0] or ""),
                    "singer": str(r[1] or ""),
                    "source": str(r[2] or "在线下载").upper(),
                    "interval": str(r[3] or "flac"),
                })
            playlists_list.append({
                "id": "downloads",
                "name": "📥 在线已下载与缓存歌曲",
                "list": dl_tracks
            })
            conn.close()
        except Exception as e:
            logger.warning("read downloads.db failed: %s", e)

    return {
        "defaultList": tracks_list,
        "userList": playlists_list
    }
