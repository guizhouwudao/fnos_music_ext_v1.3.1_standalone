import os

target_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_patch", "index.html")
with open(target_html, "r", encoding="utf-8") as f:
    html = f.read()

# 1. 注入 CSS 样式
css_to_add = """
    /* AI 音乐助手模态框与主页心动卡片样式 */
    #fnmusic-ai-modal {
        display: none;
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        background: rgba(0, 0, 0, 0.7);
        backdrop-filter: blur(16px);
        z-index: 99999;
        align-items: center;
        justify-content: center;
    }
    .ai-modal-box {
        width: 720px;
        max-width: 92vw;
        max-height: 88vh;
        background: #1a1725;
        border: 1px solid rgba(255, 255, 255, 0.14);
        border-radius: 18px;
        box-shadow: 0 20px 50px rgba(0, 0, 0, 0.75);
        display: flex;
        flex-direction: column;
        overflow: hidden;
        animation: aiPop .25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes aiPop {
        from { opacity: 0; transform: scale(0.95) translateY(10px); }
        to { opacity: 1; transform: scale(1) translateY(0); }
    }
    .ai-modal-header {
        padding: 18px 24px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        display: flex;
        align-items: center;
        justify-content: space-between;
        background: linear-gradient(90deg, rgba(246, 44, 85, 0.12) 0%, rgba(26, 23, 37, 0) 100%);
    }
    .ai-modal-title {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 18px;
        font-weight: 700;
        color: #fff;
    }
    .ai-modal-badge {
        font-size: 11px;
        font-weight: 600;
        background: rgba(246, 44, 85, 0.25);
        color: #ff5e7e;
        border: 1px solid rgba(246, 44, 85, 0.4);
        padding: 2px 8px;
        border-radius: 12px;
    }
    .ai-modal-body {
        padding: 24px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 20px;
    }
    .ai-section {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 12px;
        padding: 16px;
    }
    .ai-section-title {
        font-size: 14px;
        font-weight: 600;
        color: #fff;
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .ai-form-grid {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 12px;
    }
    .ai-form-group {
        display: flex;
        flex-direction: column;
        gap: 6px;
    }
    .ai-form-group.full {
        grid-column: span 2;
    }
    .ai-form-label {
        font-size: 12px;
        color: #9a94ab;
    }
    .ai-input {
        background: rgba(0, 0, 0, 0.35);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 8px;
        padding: 8px 12px;
        color: #fff;
        font-size: 13px;
        outline: none;
        transition: border-color .2s;
    }
    .ai-input:focus {
        border-color: #f62c55;
    }
    .ai-quick-presets {
        display: flex;
        gap: 8px;
        margin-top: 10px;
        flex-wrap: wrap;
    }
    .ai-preset-btn {
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 6px;
        padding: 4px 10px;
        color: #c7c2d6;
        font-size: 12px;
        cursor: pointer;
        transition: all .2s;
    }
    .ai-preset-btn:hover {
        background: rgba(246, 44, 85, 0.2);
        color: #fff;
        border-color: #f62c55;
    }
    .ai-actions-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-top: 14px;
    }
    .ai-btn-group {
        display: flex;
        gap: 10px;
    }
    .ai-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        padding: 8px 16px;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        transition: all .2s;
        border: none;
    }
    .ai-btn-primary {
        background: #f62c55;
        color: #fff;
    }
    .ai-btn-primary:hover {
        background: #ff476e;
    }
    .ai-btn-secondary {
        background: rgba(255, 255, 255, 0.08);
        color: #fff;
        border: 1px solid rgba(255, 255, 255, 0.14);
    }
    .ai-btn-secondary:hover {
        background: rgba(255, 255, 255, 0.15);
    }
    .ai-test-status {
        font-size: 12px;
        color: #9a94ab;
        display: flex;
        align-items: center;
        gap: 6px;
    }
    .ai-rec-banner {
        background: linear-gradient(135deg, rgba(246, 44, 85, 0.15) 0%, rgba(138, 43, 226, 0.15) 100%);
        border: 1px solid rgba(246, 44, 85, 0.3);
        border-radius: 12px;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 10px;
    }
    .ai-rec-summary {
        font-size: 13px;
        color: #eae6f5;
        line-height: 1.5;
    }
    .ai-tag-group {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
    }
    .ai-tag {
        font-size: 11px;
        background: rgba(255, 255, 255, 0.1);
        color: #ff9ebb;
        padding: 2px 8px;
        border-radius: 6px;
    }
    .ai-song-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        max-height: 280px;
        overflow-y: auto;
    }
    .ai-song-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: rgba(255, 255, 255, 0.04);
        border-radius: 8px;
        transition: all .2s;
    }
    .ai-song-item:hover {
        background: rgba(255, 255, 255, 0.08);
        transform: translateX(4px);
    }
    .ai-song-info {
        display: flex;
        flex-direction: column;
        gap: 2px;
        overflow: hidden;
    }
    .ai-song-title {
        font-size: 13px;
        font-weight: 600;
        color: #fff;
        display: flex;
        align-items: center;
        gap: 6px;
    }
    .ai-song-artist {
        font-size: 12px;
        color: #9a94ab;
    }
    .ai-song-reason {
        font-size: 11px;
        color: #ff7d99;
    }
    .ai-song-play-btn {
        background: rgba(246, 44, 85, 0.2);
        border: 1px solid #f62c55;
        color: #fff;
        border-radius: 50%;
        width: 28px;
        height: 28px;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: all .2s;
        flex-shrink: 0;
    }
    .ai-song-play-btn:hover {
        background: #f62c55;
        transform: scale(1.1);
    }

    /* 飞牛主界面「💖 我的心动歌曲推荐」卡片样式 */
    #fnmusic-home-ai-card {
        margin: 16px 24px;
        background: linear-gradient(135deg, rgba(246, 44, 85, 0.12) 0%, rgba(90, 30, 180, 0.15) 100%);
        border: 1px solid rgba(246, 44, 85, 0.25);
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
        display: flex;
        flex-direction: column;
        gap: 14px;
        backdrop-filter: blur(10px);
        position: relative;
    }
    .home-ai-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .home-ai-title-wrap {
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .home-ai-title {
        font-size: 18px;
        font-weight: 700;
        color: #fff;
        letter-spacing: 0.5px;
    }
    .home-ai-desc {
        font-size: 13px;
        color: #c7c2d6;
        line-height: 1.4;
    }
    .home-ai-scroll {
        display: flex;
        gap: 14px;
        overflow-x: auto;
        padding-bottom: 6px;
    }
    .home-ai-scroll::-webkit-scrollbar {
        height: 4px;
    }
    .home-ai-scroll::-webkit-scrollbar-thumb {
        background: rgba(246, 44, 85, 0.4);
        border-radius: 4px;
    }
    .home-ai-item {
        flex: 0 0 130px;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 10px;
        padding: 10px;
        cursor: pointer;
        display: flex;
        flex-direction: column;
        gap: 6px;
        transition: all .2s;
    }
    .home-ai-item:hover {
        background: rgba(246, 44, 85, 0.18);
        border-color: #f62c55;
        transform: translateY(-3px);
    }
    .home-ai-item-cover {
        width: 100%;
        height: 110px;
        border-radius: 8px;
        background: #252033;
        background-size: cover;
        background-position: center;
        display: flex;
        align-items: flex-end;
        justify-content: flex-end;
        padding: 6px;
        box-shadow: 0 4px 10px rgba(0, 0, 0, 0.3);
    }
    .home-ai-item-name {
        font-size: 13px;
        font-weight: 600;
        color: #fff;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .home-ai-item-singer {
        font-size: 11px;
        color: #9a94ab;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
"""

# 在 </style> 前插入 CSS
html = html.replace("</style>\n\n<!-- 歌词卡片 DOM -->", css_to_add + "\n</style>\n\n<!-- 歌词卡片 DOM -->")

# 2. 注入 AI 音乐助手 Modal DOM
ai_modal_dom = """
<!-- AI 音乐助手模态框 DOM -->
<div id="fnmusic-ai-modal" onclick="closeAiModal(event)">
    <div class="ai-modal-box" onclick="event.stopPropagation()">
        <div class="ai-modal-header">
            <div class="ai-modal-title">
                <span>🤖 AI 音乐助手</span>
                <span class="ai-modal-badge">智能化推荐与选曲</span>
            </div>
            <div class="lyric-card-close" onclick="closeAiModal()">✕</div>
        </div>
        <div class="ai-modal-body">
            <!-- 模型配置面板 -->
            <div class="ai-section">
                <div class="ai-section-title">
                    <span>⚙️ 大模型服务配置</span>
                    <span id="aiModelStatus" class="ai-test-status">未检测</span>
                </div>
                <div class="ai-form-grid">
                    <div class="ai-form-group">
                        <label class="ai-form-label">模型名称</label>
                        <input id="aiModelName" class="ai-input" placeholder="例如: DeepSeek / ChatGPT" />
                    </div>
                    <div class="ai-form-group">
                        <label class="ai-form-label">Model ID</label>
                        <input id="aiModelId" class="ai-input" placeholder="例如: deepseek-chat / gpt-4o-mini" />
                    </div>
                    <div class="ai-form-group full">
                        <label class="ai-form-label">API Base URL</label>
                        <input id="aiBaseUrl" class="ai-input" placeholder="https://api.deepseek.com/v1" />
                    </div>
                    <div class="ai-form-group full">
                        <label class="ai-form-label">API Key</label>
                        <input id="aiApiKey" type="password" class="ai-input" placeholder="sk-xxxxxxxxxxxxxxxx" />
                    </div>
                </div>
                <div class="ai-quick-presets">
                    <span style="font-size:12px;color:#8a839e;line-height:26px;">快捷预置:</span>
                    <div class="ai-preset-btn" onclick="fillAiPreset('deepseek')">DeepSeek官方</div>
                    <div class="ai-preset-btn" onclick="fillAiPreset('sensenova')">商汤日日新</div>
                    <div class="ai-preset-btn" onclick="fillAiPreset('openai')">OpenAI官方</div>
                    <div class="ai-preset-btn" onclick="fillAiPreset('local')">本地网关(7863)</div>
                </div>
                <div class="ai-actions-bar">
                    <button class="ai-btn ai-btn-secondary" onclick="testAiConnection()">
                        <span id="aiTestIcon">🔌</span><span>模型连通性检测</span>
                    </button>
                    <div class="ai-btn-group">
                        <button class="ai-btn ai-btn-primary" onclick="saveAiConfig()">💾 保存配置</button>
                    </div>
                </div>
            </div>

            <!-- 心动歌曲生成与分析面板 -->
            <div class="ai-section">
                <div class="ai-section-title">
                    <span>💖 偏好分析与心动推荐</span>
                    <button class="ai-btn ai-btn-primary" style="padding:4px 12px;font-size:12px;" onclick="refreshAiRecommend()">
                        <span id="aiRefreshIcon">🔄</span><span>重新分析生成</span>
                    </button>
                </div>
                <div class="ai-rec-banner" id="aiRecBanner" style="display:none;">
                    <div class="ai-tag-group" id="aiTagGroup"></div>
                    <div class="ai-rec-summary" id="aiRecSummary"></div>
                </div>
                <div style="font-size:13px;font-weight:600;color:#fff;margin:12px 0 8px 0;">推荐歌曲列表 (自动检索本地库与在线音源)</div>
                <div class="ai-song-list" id="aiSongList">
                    <div style="color:#8a839e;font-size:12px;text-align:center;padding:20px;">点击上方「重新分析生成」获取专属心动歌曲</div>
                </div>
            </div>
        </div>
    </div>
</div>
"""

html = html.replace('<div id="fnmusic-lyric-modal">', ai_modal_dom + '\n<div id="fnmusic-lyric-modal">')

# 3. 注入 JS 逻辑
js_to_add = """
    // =========================================================================
    // AI 音乐助手交互与心动歌曲管理
    // =========================================================================
    var aiConfigCache = null;

    window.showAiModal = function() {
        var modal = document.getElementById("fnmusic-ai-modal");
        if (modal) {
            modal.style.display = "flex";
            loadAiConfig();
            loadAiRecommend();
        }
    };

    window.closeAiModal = function(e) {
        var modal = document.getElementById("fnmusic-ai-modal");
        if (modal) modal.style.display = "none";
    };

    window.fillAiPreset = function(type) {
        var n = document.getElementById("aiModelName");
        var m = document.getElementById("aiModelId");
        var u = document.getElementById("aiBaseUrl");
        if (type === "deepseek") {
            n.value = "DeepSeek";
            m.value = "deepseek-chat";
            u.value = "https://api.deepseek.com/v1";
        } else if (type === "sensenova") {
            n.value = "商汤日日新";
            m.value = "deepseek-v4-flash";
            u.value = "https://token.sensenova.cn/v1";
        } else if (type === "openai") {
            n.value = "OpenAI";
            m.value = "gpt-4o-mini";
            u.value = "https://api.openai.com/v1";
        } else if (type === "local") {
            n.value = "本地网关";
            m.value = "gpt-4o";
            u.value = "http://127.0.0.1:7863/v1";
        }
    };

    async function loadAiConfig() {
        try {
            var res = await fetch("/music/ext/api/ai/config");
            var json = await res.json();
            if (json.code === 0 && json.data) {
                var d = json.data;
                aiConfigCache = d;
                document.getElementById("aiModelName").value = d.model_name || "";
                document.getElementById("aiModelId").value = d.model_id || "";
                document.getElementById("aiBaseUrl").value = d.base_url || "";
                if (d.api_key_masked) {
                    document.getElementById("aiApiKey").placeholder = "已保存: " + d.api_key_masked;
                }
            }
        } catch(e) {
            console.warn("[ai] load config failed:", e);
        }
    }

    window.saveAiConfig = async function() {
        var payload = {
            model_name: document.getElementById("aiModelName").value.trim(),
            model_id: document.getElementById("aiModelId").value.trim(),
            base_url: document.getElementById("aiBaseUrl").value.trim(),
            api_key: document.getElementById("aiApiKey").value.trim(),
        };
        try {
            var res = await fetch("/music/ext/api/ai/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            var json = await res.json();
            showToast(json.msg || "保存成功！");
            loadAiConfig();
        } catch(e) {
            showToast("保存异常: " + e);
        }
    };

    window.testAiConnection = async function() {
        var statusEl = document.getElementById("aiModelStatus");
        var iconEl = document.getElementById("aiTestIcon");
        statusEl.innerHTML = "<span style='color:#ffc107'>⏳ 正在检测连接...</span>";
        iconEl.innerText = "⌛";

        var payload = {
            model_name: document.getElementById("aiModelName").value.trim(),
            model_id: document.getElementById("aiModelId").value.trim(),
            base_url: document.getElementById("aiBaseUrl").value.trim(),
            api_key: document.getElementById("aiApiKey").value.trim(),
        };

        try {
            var res = await fetch("/music/ext/api/ai/test", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            var json = await res.json();
            iconEl.innerText = "🔌";
            if (json.code === 0) {
                statusEl.innerHTML = "<span style='color:#4caf50'>✅ " + (json.msg || "连接成功") + "</span>";
                showToast("✅ 模型连通检测成功！");
            } else {
                statusEl.innerHTML = "<span style='color:#f44336'>❌ " + (json.msg || "连接失败") + "</span>";
                showToast("❌ 检测失败: " + (json.msg || ""));
            }
        } catch(e) {
            iconEl.innerText = "🔌";
            statusEl.innerHTML = "<span style='color:#f44336'>❌ 异常: " + e + "</span>";
        }
    };

    async function loadAiRecommend() {
        try {
            var res = await fetch("/music/ext/api/ai/recommend");
            var json = await res.json();
            if (json.code === 0 && json.data) {
                renderAiRecommendData(json.data);
            }
        } catch(e) {
            console.warn("[ai] load recommend failed:", e);
        }
    }

    window.refreshAiRecommend = async function() {
        var icon = document.getElementById("aiRefreshIcon");
        icon.innerText = "⌛";
        showToast("AI 正在分析您的听歌偏好并推荐歌曲...");
        try {
            var res = await fetch("/music/ext/api/ai/recommend?refresh=true");
            var json = await res.json();
            icon.innerText = "🔄";
            if (json.code === 0 && json.data) {
                renderAiRecommendData(json.data);
                showToast("💖 心动好歌已生成！");
                renderHomeAiCard(json.data);
            } else {
                showToast("生成失败: " + (json.msg || ""));
            }
        } catch(e) {
            icon.innerText = "🔄";
            showToast("生成异常: " + e);
        }
    };

    function renderAiRecommendData(data) {
        var banner = document.getElementById("aiRecBanner");
        var tagGroup = document.getElementById("aiTagGroup");
        var summaryEl = document.getElementById("aiRecSummary");
        var songListEl = document.getElementById("aiSongList");

        if (data.taste_tags && data.taste_tags.length > 0) {
            banner.style.display = "flex";
            tagGroup.innerHTML = data.taste_tags.map(function(t) {
                return '<span class="ai-tag">#' + t + '</span>';
            }).join("");
            summaryEl.innerText = data.summary || "AI 为您定制的心动歌单。";
        }

        var tracks = data.tracks || [];
        if (!tracks.length) {
            songListEl.innerHTML = '<div style="color:#8a839e;font-size:12px;text-align:center;padding:20px;">暂无推荐歌曲，请点击上方重新生成</div>';
            return;
        }

        songListEl.innerHTML = tracks.map(function(t) {
            var badgeColor = t.is_local ? "#4caf50" : "#ff5e7e";
            return '<div class="ai-song-item">' +
                '  <div class="ai-song-info">' +
                '    <div class="ai-song-title">' +
                '      <span>' + t.title + '</span>' +
                '      <span style="font-size:10px;background:' + badgeColor + '22;color:' + badgeColor + ';border:1px solid ' + badgeColor + '44;padding:1px 5px;border-radius:4px;">' + (t.badge || "推荐") + '</span>' +
                '    </div>' +
                '    <div class="ai-song-artist">' + t.artist + (t.album ? (' · ' + t.album) : '') + '</div>' +
                '    <div class="ai-song-reason">' + (t.reason ? ('💡 ' + t.reason) : '') + '</div>' +
                '  </div>' +
                '  <div class="ai-song-play-btn" title="播放" onclick="playSongByGuid(\'' + t.guid + '\')">▶</div>' +
                '</div>';
        }).join("");
    }

    window.playSongByGuid = function(guid) {
        if (!guid) return;
        if (window.__FN_ROUTER__ && typeof window.__FN_ROUTER__.navigate === "function") { window.__FN_ROUTER__.navigate({ to: "/playlists/ai:heartbeat:recommend" }); } else if (window.fnNavigate) { window.fnNavigate("/playlists/ai:heartbeat:recommend"); } else { window.location.href = "/music/playlists/ai:heartbeat:recommend"; }
    };

    // =========================================================================
    // 飞牛主界面「💖 我的心动歌曲推荐」卡片自动装载器
    // =========================================================================
    function tryInjectHomeAiCard() {
        var isHomePage = window.location.pathname === "/music/" || window.location.pathname === "/music" || window.location.pathname === "/";
        if (!isHomePage) {
            var existing = document.getElementById("fnmusic-home-ai-card");
            if (existing) existing.remove();
            return;
        }
        if (document.getElementById("fnmusic-home-ai-card")) return;

        // 查找首页的主内容区域
        var mainContainer = document.querySelector(".semi-layout-content, main, .music-content, [role='main']");
        if (!mainContainer) return;

        fetch("/music/ext/api/ai/recommend")
            .then(function(r) { return r.json(); })
            .then(function(json) {
                if (json.code === 0 && json.data) {
                    renderHomeAiCard(json.data, mainContainer);
                }
            })
            .catch(function() {});
    }

    function renderHomeAiCard(data, container) {
        container = container || document.querySelector(".semi-layout-content, main, .music-content, [role='main']");
        if (!container) return;

        var existing = document.getElementById("fnmusic-home-ai-card");
        if (existing) existing.remove();

        var card = document.createElement("div");
        card.id = "fnmusic-home-ai-card";

        var tags = (data.taste_tags || ["AI精选", "心动推荐"]).map(function(t) {
            return '<span class="ai-tag" style="background:rgba(255,255,255,0.15);color:#fff;">#' + t + '</span>';
        }).join(" ");

        var tracks = (data.tracks || []).slice(0, 10);
        var scrollHtml = tracks.map(function(t) {
            var coverStyle = t.cover_url ? ('background-image:url(' + t.cover_url + ')') : 'background:#2d2640';
            return '<div class="home-ai-item" onclick="(window.__FN_ROUTER__ ? window.__FN_ROUTER__.navigate({ to: \'/playlists/ai:heartbeat:recommend\' }) : (window.fnNavigate ? window.fnNavigate(\'/playlists/ai:heartbeat:recommend\') : window.location.href=\'/music/playlists/ai:heartbeat:recommend\'))">' +
                '  <div class="home-ai-item-cover" style="' + coverStyle + '">' +
                '    <span style="font-size:10px;background:rgba(0,0,0,0.6);color:#ff7d99;padding:1px 4px;border-radius:4px;">' + (t.badge || "AI") + '</span>' +
                '  </div>' +
                '  <div class="home-ai-item-name" title="' + t.title + '">' + t.title + '</div>' +
                '  <div class="home-ai-item-singer">' + t.artist + '</div>' +
                '</div>';
        }).join("");

        card.innerHTML = 
            '<div class="home-ai-header">' +
            '  <div class="home-ai-title-wrap">' +
            '    <span class="home-ai-title">💖 我的心动歌曲推荐</span>' +
            '    <div class="ai-tag-group">' + tags + '</div>' +
            '  </div>' +
            '  <div style="display:flex;gap:8px;">' +
            '    <button class="ai-btn ai-btn-primary" style="padding:6px 14px;font-size:12px;" onclick="(window.__FN_ROUTER__ ? window.__FN_ROUTER__.navigate({ to: \'/playlists/ai:heartbeat:recommend\' }) : (window.fnNavigate ? window.fnNavigate(\'/playlists/ai:heartbeat:recommend\') : window.location.href=\'/music/playlists/ai:heartbeat:recommend\'))">▶ 播放心动歌单</button>' +
            '    <button class="ai-btn ai-btn-secondary" style="padding:6px 12px;font-size:12px;" onclick="showAiModal()">⚙️ 配置</button>' +
            '  </div>' +
            '</div>' +
            '<div class="home-ai-desc">' + (data.summary || "AI 音乐助手根据您的常播歌曲与音乐品味，专属定制的心动好歌。") + '</div>' +
            '<div class="home-ai-scroll">' + (scrollHtml || '<div style="color:#9a94ab;font-size:12px;">点击右上角「⚙️ 配置」启动 AI 音乐助手并生成心动歌曲</div>') + '</div>';

        container.insertBefore(card, container.firstChild);
    }

    // 定时探活注入首页卡片
    setInterval(tryInjectHomeAiCard, 2000);
"""

# 在路由拦截前插入 AI 路由拦截与 JS 函数
router_patch = """
        if (href.includes("ai") || href.includes("ai_music") || text.includes("AI音乐助手")) {
            e.preventDefault();
            e.stopPropagation();
            showAiModal();
            return;
        }
"""

html = html.replace('if (href === "/music/ext" || href.includes("/ext") || text.includes("后台管理")) {', router_patch + '\n        if (href === "/music/ext" || href.includes("/ext") || text.includes("后台管理")) {')

html = html.replace('// 路由及菜单点击代理拦截', js_to_add + '\n    // 路由及菜单点击代理拦截')

target_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_patch", "index.html")
with open(target_html, "w", encoding="utf-8") as f:
    f.write(html)

with open("/usr/local/apps/@appcenter/trim.music/static/index.html", "w", encoding="utf-8") as f:
    f.write(html)

print("index.html successfully patched!")
