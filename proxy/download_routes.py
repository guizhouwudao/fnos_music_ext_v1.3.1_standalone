import os
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from download_mgr import list_download_records, delete_download_record, clear_download_records

logger = logging.getLogger("fnmusic.ext.download_routes")
router = APIRouter()

@router.get("/music/downloads", response_class=HTMLResponse)
@router.get("/music/downloads/", response_class=HTMLResponse)
async def downloads_page():
    html_content = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>飞牛音乐 · 下载管理</title>
    <style>
        :root {
            --bg-body: #121016;
            --bg-card: rgba(255, 255, 255, 0.04);
            --bg-card-hover: rgba(255, 255, 255, 0.07);
            --border: rgba(255, 255, 255, 0.08);
            --text-main: #f5f5f7;
            --text-sub: #86868b;
            --accent: #f62c55;
            --accent-hover: #ff476e;
            --success: #34c759;
            --danger: #ff453a;
            --warning: #ff9f0a;
            --radius-md: 10px;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-body);
            color: var(--text-main);
            font-family: var(--font);
            display: flex;
            height: 100vh;
            overflow: hidden;
        }
        .sidebar {
            width: 220px;
            background: rgba(18, 16, 22, 0.95);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            padding: 24px 16px;
        }
        .sidebar-brand {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 28px;
            cursor: pointer;
        }
        .brand-logo {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            background: linear-gradient(135deg, #f62c55, #ff6b8b);
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 16px;
            color: #fff;
        }
        .brand-title {
            font-size: 15px;
            font-weight: 700;
            color: #fff;
        }
        .nav-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 14px;
            color: var(--text-sub);
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s;
            margin-bottom: 4px;
        }
        .nav-item:hover {
            background: rgba(255, 255, 255, 0.05);
            color: #fff;
        }
        .nav-item.active {
            background: rgba(246, 44, 85, 0.15);
            color: var(--accent);
            font-weight: 600;
        }
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow-y: auto;
            background: radial-gradient(circle at top right, rgba(246, 44, 85, 0.05), transparent 40%),
                        radial-gradient(circle at top left, rgba(40, 30, 60, 0.3), transparent 50%);
        }
        .top-bar {
            padding: 24px 32px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border);
        }
        .page-title {
            font-size: 24px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .tab-group {
            display: flex;
            gap: 8px;
            background: rgba(255, 255, 255, 0.04);
            padding: 4px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }
        .tab-btn {
            background: transparent;
            border: none;
            color: var(--text-sub);
            padding: 6px 16px;
            font-size: 13px;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .tab-btn.active {
            background: var(--accent);
            color: #fff;
            font-weight: 600;
        }
        .actions-group {
            display: flex;
            gap: 12px;
        }
        .btn {
            background: rgba(255, 255, 255, 0.08);
            color: var(--text-main);
            border: 1px solid var(--border);
            padding: 8px 16px;
            font-size: 13px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .btn:hover {
            background: rgba(255, 255, 255, 0.14);
        }
        .btn-danger {
            background: rgba(255, 69, 58, 0.15);
            border-color: rgba(255, 69, 58, 0.3);
            color: var(--danger);
        }
        .btn-danger:hover {
            background: rgba(255, 69, 58, 0.25);
        }
        .content-area {
            padding: 24px 32px;
            flex: 1;
        }
        .table-container {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 13px;
        }
        th {
            background: rgba(255, 255, 255, 0.02);
            color: var(--text-sub);
            padding: 14px 20px;
            font-weight: 600;
            border-bottom: 1px solid var(--border);
        }
        td {
            padding: 16px 20px;
            border-bottom: 1px solid var(--border);
            color: var(--text-main);
        }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: var(--bg-card-hover); }
        .song-title {
            font-weight: 600;
            font-size: 14px;
            color: #fff;
            margin-bottom: 2px;
        }
        .song-artist {
            color: var(--text-sub);
            font-size: 12px;
        }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.5px;
        }
        .badge-success {
            background: rgba(52, 199, 89, 0.15);
            color: var(--success);
            border: 1px solid rgba(52, 199, 89, 0.3);
        }
        .badge-failed {
            background: rgba(255, 69, 58, 0.15);
            color: var(--danger);
            border: 1px solid rgba(255, 69, 58, 0.3);
        }
        .badge-downloading {
            background: rgba(255, 159, 10, 0.15);
            color: var(--warning);
            border: 1px solid rgba(255, 159, 10, 0.3);
        }
        .empty-state {
            padding: 60px 0;
            text-align: center;
            color: var(--text-sub);
        }
        .empty-icon {
            font-size: 48px;
            margin-bottom: 12px;
            opacity: 0.5;
        }
        .file-info {
            color: var(--text-sub);
            font-family: monospace;
            font-size: 11px;
            max-width: 320px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="sidebar-brand" onclick="window.location.href='/music'">
            <div class="brand-logo">飞</div>
            <div class="brand-title">飞牛音乐</div>
        </div>
        <a class="nav-item" href="/music/library">
            <span>🎵</span> 本地音乐
        </a>
        <a class="nav-item active" href="/music/downloads">
            <span>⤓</span> 下载
        </a>
        <a class="nav-item" href="/music/playlists/board:kw:16">
            <span>🔥</span> 在线排行榜
        </a>
        <a class="nav-item" href="/music/ext">
            <span>⚙️</span> 后台管理
        </a>
    </div>

    <div class="main-content">
        <div class="top-bar">
            <div class="page-title">
                <span>下载管理</span>
            </div>
            <div class="tab-group">
                <button class="tab-btn active" onclick="switchTab('all', this)">全部记录</button>
                <button class="tab-btn" onclick="switchTab('success', this)">下载成功</button>
                <button class="tab-btn" onclick="switchTab('failed', this)">下载失败</button>
            </div>
            <div class="actions-group">
                <button class="btn" onclick="loadDownloads()">🔄 刷新</button>
                <button class="btn btn-danger" onclick="clearRecords()">🗑️ 清空列表</button>
            </div>
        </div>

        <div class="content-area">
            <div class="table-container">
                <table id="downloads-table">
                    <thead>
                        <tr>
                            <th>歌曲信息</th>
                            <th>状态</th>
                            <th>格式 / 大小</th>
                            <th>存储路径 / 错误详情</th>
                            <th>操作时间</th>
                            <th style="text-align: right;">操作</th>
                        </tr>
                    </thead>
                    <tbody id="downloads-tbody">
                        <tr>
                            <td colspan="6" class="empty-state">
                                <div class="empty-icon">⏳</div>
                                <div>正在载入下载记录...</div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        let currentTab = 'all';

        function switchTab(tab, el) {
            currentTab = tab;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            if (el) el.classList.add('active');
            loadDownloads();
        }

        async function loadDownloads() {
            try {
                const resp = await fetch('/music/ext/api/downloads/list?status=' + currentTab);
                const data = await resp.json();
                const tbody = document.getElementById('downloads-tbody');
                
                if (!data || !data.records || data.records.length === 0) {
                    tbody.innerHTML = `
                        <tr>
                            <td colspan="6" class="empty-state">
                                <div class="empty-icon">📭</div>
                                <div>暂无相关下载记录</div>
                            </td>
                        </tr>
                    `;
                    return;
                }

                tbody.innerHTML = data.records.map(r => {
                    let statusBadge = '';
                    if (r.status === 'success') {
                        statusBadge = '<span class="badge badge-success">下载成功</span>';
                    } else if (r.status === 'failed') {
                        statusBadge = '<span class="badge badge-failed">下载失败</span>';
                    } else {
                        statusBadge = '<span class="badge badge-downloading">正在下载</span>';
                    }

                    const formatSize = r.status === 'success' 
                        ? `${(r.ext || 'FLAC').toUpperCase()} / ${r.size_mb ? r.size_mb + ' MB' : '-'}`
                        : '-';

                    const detailText = r.status === 'success'
                        ? `<div class="file-info" title="${r.file_path || ''}">${r.file_path || '默认音乐目录'}</div>`
                        : `<div style="color: var(--danger); font-size: 12px;">${r.error_msg || '未知错误'}</div>`;

                    return `
                        <tr>
                            <td>
                                <div class="song-title">${escapeHtml(r.title || '未知曲目')}</div>
                                <div class="song-artist">${escapeHtml(r.artist || '未知歌手')}</div>
                            </td>
                            <td>${statusBadge}</td>
                            <td>${formatSize}</td>
                            <td>${detailText}</td>
                            <td style="color: var(--text-sub); font-size: 12px;">${r.created_at || '-'}</td>
                            <td style="text-align: right;">
                                <button class="btn btn-danger" style="padding: 4px 10px; font-size: 11px;" onclick="deleteRecord(${r.id})">删除</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            } catch (err) {
                console.error('load downloads error:', err);
            }
        }

        async function deleteRecord(id) {
            if (!confirm('确定删除该条下载记录吗？（不会删除实际磁盘文件）')) return;
            try {
                const resp = await fetch('/music/ext/api/downloads/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id })
                });
                const res = await resp.json();
                if (res.code === 0) {
                    loadDownloads();
                } else {
                    alert(res.msg || '删除失败');
                }
            } catch (err) {
                alert('网络错误');
            }
        }

        async function clearRecords() {
            if (!confirm('确定清空当前视图下的下载记录吗？')) return;
            try {
                const resp = await fetch('/music/ext/api/downloads/clear', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status: currentTab })
                });
                const res = await resp.json();
                if (res.code === 0) {
                    loadDownloads();
                } else {
                    alert(res.msg || '清空失败');
                }
            } catch (err) {
                alert('网络错误');
            }
        }

        function escapeHtml(str) {
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        loadDownloads();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

@router.get("/music/ext/api/downloads/list")
async def api_downloads_list(status: str = "all"):
    records = list_download_records(filter_status=status)
    return JSONResponse(content={"code": 0, "records": records})

@router.post("/music/ext/api/downloads/delete")
async def api_downloads_delete(request: Request):
    try:
        data = await request.json()
        rec_id = int(data.get("id") or 0)
        if delete_download_record(rec_id):
            return JSONResponse(content={"code": 0, "msg": "删除成功"})
        return JSONResponse(content={"code": -1, "msg": "删除失败"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": str(e)})

@router.post("/music/ext/api/downloads/clear")
async def api_downloads_clear(request: Request):
    try:
        data = await request.json()
        status = str(data.get("status") or "all")
        if clear_download_records(filter_status=status):
            return JSONResponse(content={"code": 0, "msg": "清空成功"})
        return JSONResponse(content={"code": -1, "msg": "清空失败"})
    except Exception as e:
        return JSONResponse(content={"code": -1, "msg": str(e)})
