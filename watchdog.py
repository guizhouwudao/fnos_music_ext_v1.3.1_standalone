#!/usr/bin/env python3
import os
import socket
import struct
import subprocess
import time
import shutil

TARGET_SOCKET = '/var/run/trim_music.socket'
UPSTREAM_SOCKET = '/var/run/trim_music_upstream.socket'
STATIC_DIR = '/usr/local/apps/@appcenter/trim.music/static'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_STATIC_DIR = os.path.join(BASE_DIR, 'static_patch')

def get_peer(path):
    if not os.path.exists(path):
        return None, None
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(0.5)
    try:
        s.connect(path)
        pid, _, _ = struct.unpack('3i', s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        exe = os.readlink(f'/proc/{pid}/exe')
        return pid, exe
    except Exception:
        return None, None
    finally:
        s.close()

def ensure_frontend_patched():
    """检测并自动持久化补全前端补丁（防应用重启或覆盖后失效）"""
    try:
        index_file = os.path.join(STATIC_DIR, 'index.html')
        js_file = os.path.join(STATIC_DIR, 'assets', '1bc04b5291c26a46d918139138b992d2-LE4mTIM4.js')
        
        need_patch = False
        if os.path.exists(index_file):
            with open(index_file, 'r', encoding='utf-8') as f:
                content = f.read()
            if 'fnmusic-playlist-modal' not in content or 'openPlaylist' not in content:
                need_patch = True
        else:
            need_patch = True
            
        if os.path.exists(js_file):
            with open(js_file, 'r', encoding='utf-8', errors='ignore') as f:
                js_content = f.read()
            if 'downloadAnyTrack' not in js_content or 'DeleteTrackSong' not in js_content:
                need_patch = True
        else:
            need_patch = True

        if need_patch and os.path.exists(BACKUP_STATIC_DIR):
            print("[watchdog] Official trim.music static files were reset or updated. Re-injecting patches...", flush=True)
            src_index = os.path.join(BACKUP_STATIC_DIR, 'index.html')
            src_js = os.path.join(BACKUP_STATIC_DIR, '1bc04b5291c26a46d918139138b992d2-LE4mTIM4.js')
            src_br = os.path.join(BACKUP_STATIC_DIR, '8a84e406c08ac9594f47222406598f75-BRZmJgho.js')
            
            if os.path.exists(src_index):
                shutil.copy2(src_index, index_file)
            if os.path.exists(src_js) and os.path.exists(os.path.dirname(js_file)):
                shutil.copy2(src_js, js_file)
            if os.path.exists(src_br) and os.path.exists(os.path.dirname(js_file)):
                shutil.copy2(src_br, os.path.join(STATIC_DIR, 'assets', '8a84e406c08ac9594f47222406598f75-BRZmJgho.js'))
            print("[watchdog] Frontend patches re-injected successfully!", flush=True)
    except Exception as e:
        print(f"[watchdog] Error checking/patching frontend: {e}", flush=True)

def main():
    print("[watchdog] fnmusic persistent watchdog started with auto-heal & frontend persistence...", flush=True)
    while True:
        time.sleep(3)
        # 1. 检查前端是否被官方还原
        ensure_frontend_patched()
        
        # 2. 检查后端 socket 是否被官方 trim-music 独占
        pid, exe = get_peer(TARGET_SOCKET)
        if pid and exe and 'trim-music' in exe:
            print(f"[watchdog] Detected official trim-music on target socket (PID={pid}). Re-attaching fnmusic-ext...", flush=True)
            if os.path.exists(UPSTREAM_SOCKET):
                try:
                    os.unlink(UPSTREAM_SOCKET)
                except OSError:
                    pass
            subprocess.run(['systemctl', 'restart', 'fnmusic-ext.service'], check=False)
            time.sleep(15)

if __name__ == '__main__':
    main()
