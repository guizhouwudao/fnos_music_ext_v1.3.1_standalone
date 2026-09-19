import os
import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger("fnmusic.ext.downloads")

BASE_DIR = os.environ.get("FNMUSIC_HOME") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_DIR = os.path.join(BASE_DIR, "downloads_db")
DB_PATH = os.path.join(DB_DIR, "downloads.db")

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute('''
        CREATE TABLE IF NOT EXISTS download_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT,
            album TEXT,
            status TEXT NOT NULL, -- 'success', 'failed', 'downloading'
            ext TEXT,
            size_mb REAL DEFAULT 0,
            file_path TEXT,
            error_msg TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        conn.commit()

def record_download(guid: str, title: str, artist: str = "", album: str = "", status: str = "downloading", ext: str = "", size_mb: float = 0, file_path: str = "", error_msg: str = "") -> int:
    try:
        init_db()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute('''
            INSERT INTO download_records (guid, title, artist, album, status, ext, size_mb, file_path, error_msg, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (guid, title, artist, album, status, ext, size_mb, file_path, error_msg, now, now))
            conn.commit()
            return cur.lastrowid
    except Exception as e:
        logger.error("record_download error: %s", e)
        return 0

def update_download_status(record_id: int, status: str, ext: str = "", size_mb: float = 0, file_path: str = "", error_msg: str = ""):
    try:
        if not record_id:
            return
        init_db()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute('''
            UPDATE download_records
            SET status = ?, ext = COALESCE(NULLIF(?, ''), ext), size_mb = CASE WHEN ? > 0 THEN ? ELSE size_mb END,
                file_path = COALESCE(NULLIF(?, ''), file_path), error_msg = ?, updated_at = ?
            WHERE id = ?
            ''', (status, ext, size_mb, size_mb, file_path, error_msg, now, record_id))
            conn.commit()
    except Exception as e:
        logger.error("update_download_status error: %s", e)

def list_download_records(filter_status: str = "all", limit: int = 200):
    try:
        init_db()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if filter_status == "success":
                cur.execute("SELECT * FROM download_records WHERE status = 'success' ORDER BY id DESC LIMIT ?", (limit,))
            elif filter_status == "failed":
                cur.execute("SELECT * FROM download_records WHERE status = 'failed' ORDER BY id DESC LIMIT ?", (limit,))
            else:
                cur.execute("SELECT * FROM download_records ORDER BY id DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("list_download_records error: %s", e)
        return []

def delete_download_record(record_id: int, delete_file: bool = False):
    try:
        init_db()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM download_records WHERE id = ?", (record_id,))
            row = cur.fetchone()
            if row and delete_file:
                fp = row["file_path"]
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                        # 同时尝试删除同名歌词
                        lrc_p = os.path.splitext(fp)[0] + ".lrc"
                        if os.path.exists(lrc_p):
                            os.remove(lrc_p)
                    except Exception as e_rm:
                        logger.warning("remove file failed: %s", e_rm)
            cur.execute("DELETE FROM download_records WHERE id = ?", (record_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error("delete_download_record error: %s", e)
        return False

def delete_download_by_guid_or_title(guid: str, title: str = "", delete_file: bool = True) -> bool:
    try:
        init_db()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            clean_title = (title or "").replace("【下载完成】", "").replace("【下载失败】", "").strip()
            
            # 支持通过 record_id (如 "download:8" 或 8) 删除
            record_id = None
            if guid and guid.startswith("download:"):
                try:
                    record_id = int(guid.split(":", 1)[1])
                except Exception:
                    pass
            
            query = "SELECT * FROM download_records WHERE 1=0"
            params = []
            if record_id is not None:
                query += " OR id = ?"
                params.append(record_id)
            if guid:
                query += " OR guid = ?"
                params.append(guid)
            if clean_title:
                query += " OR title = ? OR title = ?"
                params.extend([clean_title, title])
            
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            for r in rows:
                if delete_file and r["file_path"] and os.path.exists(r["file_path"]):
                    try:
                        os.remove(r["file_path"])
                        lrc_p = os.path.splitext(r["file_path"])[0] + ".lrc"
                        if os.path.exists(lrc_p):
                            os.remove(lrc_p)
                    except Exception as e_rm:
                        logger.warning("remove file failed: %s", e_rm)
            
            del_query = "DELETE FROM download_records WHERE 1=0"
            del_params = []
            if record_id is not None:
                del_query += " OR id = ?"
                del_params.append(record_id)
            if guid:
                del_query += " OR guid = ?"
                del_params.append(guid)
            if clean_title:
                del_query += " OR title = ? OR title = ?"
                del_params.extend([clean_title, title])
                
            cur.execute(del_query, tuple(del_params))
            conn.commit()
            return True
    except Exception as e:
        logger.error("delete_download_by_guid_or_title error: %s", e)
        return False

def clear_download_records(filter_status: str = "all"):
    try:
        init_db()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            if filter_status == "success":
                cur.execute("DELETE FROM download_records WHERE status = 'success'")
            elif filter_status == "failed":
                cur.execute("DELETE FROM download_records WHERE status = 'failed'")
            else:
                cur.execute("DELETE FROM download_records")
            conn.commit()
            return True
    except Exception as e:
        logger.error("clear_download_records error: %s", e)
        return False
