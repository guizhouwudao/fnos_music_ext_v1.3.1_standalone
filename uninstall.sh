#!/usr/bin/env bash
# ==============================================================================
# fnos_music_ext 飞牛音乐增强扩展插件 一键卸载与还原脚本
# ==============================================================================
set -euo pipefail

STATIC_DIR="/usr/local/apps/@appcenter/trim.music/static"

echo -e "\033[33m[!] 正在停止并卸载飞牛音乐增强扩展服务...\033[0m"
systemctl stop fnmusic-watchdog.service fnmusic-ext.service 2>/dev/null || true
systemctl disable fnmusic-watchdog.service fnmusic-ext.service 2>/dev/null || true
rm -f /etc/systemd/system/fnmusic-ext.service /etc/systemd/system/fnmusic-watchdog.service 2>/dev/null || true
systemctl daemon-reload

echo -e "\033[33m[*] 正在还原飞牛音乐原生前端静态资源...\033[0m"
if [ -f "${STATIC_DIR}/index.html.orig" ]; then
    cp -f "${STATIC_DIR}/index.html.orig" "${STATIC_DIR}/index.html"
fi

echo -e "\033[32m[✓] 飞牛音乐已完全还原至官方初始出厂状态！\033[0m"
