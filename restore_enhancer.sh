#!/usr/bin/env bash
# ==============================================================================
# 飞牛音乐增强版 一键自愈与恢复脚本
# ==============================================================================
set -euo pipefail

STATIC_DIR="/usr/local/apps/@appcenter/trim.music/static"
BACKUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "\033[34m[1/4] 正在检查并补全前端静态资源补丁...\033[0m"
if [ -d "${STATIC_DIR}" ] && [ -d "${BACKUP_DIR}/static_patch" ]; then
    cp -f "${BACKUP_DIR}/static_patch/index.html" "${STATIC_DIR}/index.html"
    cp -f "${BACKUP_DIR}/static_patch/1bc04b5291c26a46d918139138b992d2-LE4mTIM4.js" "${STATIC_DIR}/assets/1bc04b5291c26a46d918139138b992d2-LE4mTIM4.js"
    cp -f "${BACKUP_DIR}/static_patch/8a84e406c08ac9594f47222406598f75-BRZmJgho.js" "${STATIC_DIR}/assets/8a84e406c08ac9594f47222406598f75-BRZmJgho.js"
    echo -e "\033[32m  -> 前端补丁注入完成！\033[0m"
fi

echo -e "\033[34m[2/4] 确保 systemd 守护配置生效...\033[0m"
cp -f "${BACKUP_DIR}/systemd/fnmusic-ext.service" /etc/systemd/system/
cp -f "${BACKUP_DIR}/systemd/fnmusic-watchdog.service" /etc/systemd/system/
systemctl daemon-reload

echo -e "\033[34m[3/4] 启动并恢复所有增强服务...\033[0m"
systemctl restart fnmusic-ext.service
systemctl restart fnmusic-watchdog.service

echo -e "\033[34m[4/4] 验证服务健康状态...\033[0m"
sleep 1
if curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz | grep -q '"ok":true'; then
    echo -e "\033[32m  -> 飞牛音乐增强版服务运行正常，健康检查通过！\033[0m"
else
    echo -e "\033[33m  -> 警告: 健康检查未返回 ok，请检查 journalctl -u fnmusic-ext -n 20\033[0m"
fi
cp -f "${BACKUP_DIR}/static_patch/8a84e406c08ac9594f47222406598f75-BRm4WlC2.js" "${STATIC_DIR}/assets/8a84e406c08ac9594f47222406598f75-BRm4WlC2.js"
cp -f "${BACKUP_DIR}/proxy/ai_music.py" "/vol1/1000/tools/fnmusic_ext/proxy/ai_music.py"
cp -f "${BACKUP_DIR}/static_patch/d371538581ad9966b984819f9af416a8-SNgk48eL.js" "${STATIC_DIR}/assets/d371538581ad9966b984819f9af416a8-SNgk48eL.js"
