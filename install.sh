#!/usr/bin/env bash
# ==============================================================================
# fnos_music_ext 飞牛音乐全功能无损增强扩展插件 一键安装脚本 (v${FNMUSIC_VERSION})
# 适用环境：任意安装了「飞牛音乐 (trim.music)」的 fnOS NAS 设备
# ==============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${FNMUSIC_TARGET_DIR:-/vol1/1000/tools/fnmusic_ext}"
STATIC_DIR="/usr/local/apps/@appcenter/trim.music/static"
FNMUSIC_VERSION="$(head -n 1 "${DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"
[ ! -d "${STATIC_DIR}" ] && [ -d "/var/apps/trim.music/target/static" ] && STATIC_DIR="/var/apps/trim.music/target/static"

echo -e "\033[32m=================================================================\033[0m"
echo -e "\033[32m[+] 开始导入部署飞牛音乐增强扩展插件 (fnmusic-ext v${FNMUSIC_VERSION})... \033[0m"
echo -e "\033[32m=================================================================\033[0m"

# 1. 探测与检测飞牛官方音乐服务
if [ ! -d "${STATIC_DIR}" ]; then
    echo -e "\033[31m[!] 错误：未检测到飞牛音乐静态资源目录 (${STATIC_DIR})\033[0m"
    echo -e "\033[33m    请先在 fnOS「应用中心」中安装并启动【飞牛音乐】应用！\033[0m"
    exit 1
fi

if [ ! -S "/var/run/trim_music.socket" ] && [ ! -S "/var/run/trim_music_upstream.socket" ]; then
    echo -e "\033[31m[!] 错误：未检测到飞牛音乐官方 Unix Socket (/var/run/trim_music.socket)！\033[0m"
    echo -e "\033[33m    请确认已在 fnOS「应用中心」启动了【飞牛音乐】应用。\033[0m"
    exit 1
fi

# 2. 创建插件核心目录并同步文件
echo -e "\033[34m[*] 正在同步插件服务端文件到 ${TARGET_DIR} ...\033[0m"
mkdir -p "${TARGET_DIR}"
cp -r "${DIR}/"* "${TARGET_DIR}/" 2>/dev/null || true

# 确保运行时关键目录具备
mkdir -p "${TARGET_DIR}/cache/lyrics" \
         "${TARGET_DIR}/online_favorites" \
         "${TARGET_DIR}/play_history" \
         "${TARGET_DIR}/recommend_cache" \
         "${TARGET_DIR}/custom_sources" \
         "${TARGET_DIR}/downloads_db" \
         /root/.local/state/fnmusic_ext

# 3. 检查并创建独立的 Python 运行环境
echo -e "\033[34m[*] 正在检查并初始化 Python 虚拟运行环境与依赖库...\033[0m"
if [ ! -x "${TARGET_DIR}/.venv-proxy/bin/python" ]; then
    echo "[*] 创建 Python 虚拟环境..."
    python3 -m venv "${TARGET_DIR}/.venv-proxy"
fi

echo "[*] 正在同步/更新 Python 依赖库 (清华源镜像)..."
"${TARGET_DIR}/.venv-proxy/bin/pip" install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet || true
"${TARGET_DIR}/.venv-proxy/bin/pip" install -i https://pypi.tuna.tsinghua.edu.cn/simple -r "${TARGET_DIR}/proxy/requirements.txt"

# 4. 注入前端定制补丁 (已内置硬编码中文字符串，杜绝 layout.sidebar 键名乱码)
echo -e "\033[34m[*] 正在注入前端静态补丁到飞牛音乐系统...\033[0m"
if [ -d "${DIR}/static_patch" ]; then
    # 备份原始官方前端资源
    [ ! -f "${STATIC_DIR}/index.html.orig" ] && cp "${STATIC_DIR}/index.html" "${STATIC_DIR}/index.html.orig" 2>/dev/null || true
    
    cp -f "${DIR}/static_patch/index.html" "${STATIC_DIR}/index.html"
    cp -f "${DIR}/static_patch/"*.js "${STATIC_DIR}/assets/" 2>/dev/null || true
    cp -f "${DIR}/static_patch/"*.png "${STATIC_DIR}/assets/" 2>/dev/null || true

    # 若官方存在多语言 json，一并注入中文键名兜底
    if [ -f "${STATIC_DIR}/locales/zh-CN/music.json" ]; then
        python3 -c '
import json
p = "'"${STATIC_DIR}"'/locales/zh-CN/music.json"
try:
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)
    if "sidebar" in d:
        d["sidebar"]["admin"] = "后台管理"
        d["sidebar"]["leaderboards"] = "在线排行榜"
        d["sidebar"]["downloads"] = "下载"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
except Exception:
    pass
' 2>/dev/null || true
    fi
    echo -e "\033[32m[✓] 前端静态补丁注入完成！\033[0m"
fi

# 5. 配置并注册 systemd 服务 (规范化 Socket Takeover 劫持流程)
echo -e "\033[34m[*] 正在注册并启动 systemd 系统守护服务...\033[0m"
/usr/bin/python3 "${TARGET_DIR}/proxy/takeover.py" render-unit --base "${TARGET_DIR}" > /etc/systemd/system/fnmusic-ext.service

cat << 'EOF' > /etc/systemd/system/fnmusic-watchdog.service
[Unit]
Description=fnmusic-ext frontend & socket watchdog
After=network.target fnmusic-ext.service

[Service]
Type=simple
User=root
WorkingDirectory=${TARGET_DIR}
ExecStart=${TARGET_DIR}/.venv-proxy/bin/python watchdog.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fnmusic-ext.service fnmusic-watchdog.service
echo -e "\033[34m[*] 启动 fnmusic-ext 服务并接管官方套接字...\033[0m"
# 先确保老实例停止，防止 socket 争抢
systemctl stop fnmusic-ext.service 2>/dev/null || true
rm -rf /run/fnmusic-ext/* 2>/dev/null || true
systemctl start fnmusic-ext.service || systemctl restart fnmusic-ext.service
systemctl restart fnmusic-watchdog.service 2>/dev/null || true

# 6. 验证服务接管健康状态
echo -e "\033[34m[*] 正在校验扩展代理服务健康状态...\033[0m"
sleep 2
if curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz 2>/dev/null | grep -q '"ok":true'; then
    echo -e "\033[32m  -> 扩展网关健康检查通过！Socket 劫持接管成功！\033[0m"
else
    echo -e "\033[33m  -> 提示: 健康检查未立即响应，可能仍在初始化，请检查: journalctl -u fnmusic-ext.service -n 20\033[0m"
fi

echo -e "\033[32m=================================================================\033[0m"
echo -e "\033[32m[✓] 恭喜！飞牛音乐全功能无损增强扩展插件已成功部署并激活！\033[0m"
echo -e "\033[32m  1. 全网无损音源聚合：酷我 / 网易云 / 酷狗 / QQ / 咪咕 五网直连\033[0m"
echo -e "\033[32m  2. 在线精选歌单广场：卡片式流式网格，一键起播与无损入库\033[0m"
echo -e "\033[32m  3. 官方排行榜实时注入：热歌榜 / 飙升榜 / 新歌榜 原生歌单整合\033[0m"
echo -e "\033[32m  4. 独立下载与缓存中心：智能边播边存，已下载/缓存完全与本地音乐解耦\033[0m"
echo -e "\033[32m  5. 1060px 黄金播放控制栏：音质自由切换，图标完美嵌合杜绝重叠\033[0m"
echo -e "\033[32m  6. 飞牛原生系统深度联动：直通原生曲库数据库，收藏与播放历史实时感知\033[0m"
echo -e "\033[32m  7. 专属后台控制中心：浏览器访问 http://<NAS_IP>:5666/music/ext/ 管理\033[0m"
echo -e "\033[32m请在浏览器中按 Ctrl + F5 强制刷新飞牛音乐页面即可享受全新体验！\033[0m"
echo -e "\033[32m=================================================================\033[0m"
