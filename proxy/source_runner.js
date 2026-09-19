const fs = require('fs');
const http = require('http');
const https = require('https');
const { URL } = require('url');

const inputData = fs.readFileSync(0, 'utf-8');
const reqData = JSON.parse(inputData);

const sourceFilePath = reqData.source_file;
const action = reqData.action || 'musicUrl';
const platform = reqData.source || 'kw';
const musicInfo = reqData.music_info || {};
const quality = reqData.quality || 'flac';

if (!fs.existsSync(sourceFilePath)) {
  process.stdout.write(JSON.stringify({ ok: false, msg: `音源文件不存在: ${sourceFilePath}` }));
  process.exit(0);
}

const handlers = {};
let initedInfo = null;

// 静默脚本内部的普通 console.log，只保留 stderr 或最终结果
console.log = function(...args) {
  // process.stderr.write(args.join(' ') + '\n');
};

globalThis.lx = {
  EVENT_NAMES: {
    request: 'request',
    inited: 'inited',
    updateAlert: 'updateAlert'
  },
  version: '2.0.0',
  env: 'mobile',
  currentScriptInfo: {
    name: 'custom_source',
    version: '1.0.0'
  },
  on: (event, handler) => {
    handlers[event] = handler;
  },
  send: (event, data) => {
    if (event === 'inited') initedInfo = data;
  },
  request: (targetUrl, options, callback) => {
    if (typeof options === 'function') {
      callback = options;
      options = {};
    }
    options = options || {};
    try {
      const u = new URL(targetUrl);
      const mod = u.protocol === 'https:' ? https : http;
      const headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        ...(options.headers || {})
      };
      const req = mod.request(u, {
        method: options.method || 'GET',
        headers,
        timeout: options.timeout || 10000
      }, (res) => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => {
          const raw = Buffer.concat(chunks);
          const bodyStr = raw.toString('utf-8');
          let parsedBody = bodyStr;
          const trimmed = bodyStr.trim();
          if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
            try { parsedBody = JSON.parse(trimmed); } catch (e) {}
          }
          callback(null, {
            statusCode: res.statusCode,
            headers: res.headers,
            body: parsedBody
          });
        });
      });
      req.on('error', (err) => callback(err, null));
      req.on('timeout', () => {
        req.destroy();
        callback(new Error('请求超时'), null);
      });
      if (options.body) {
        req.write(typeof options.body === 'string' ? options.body : JSON.stringify(options.body));
      }
      req.end();
    } catch (e) {
      callback(e, null);
    }
  }
};

try {
  const code = fs.readFileSync(sourceFilePath, 'utf-8');
  eval(code);

  const reqHandler = handlers['request'];
  if (!reqHandler) {
    process.stdout.write(JSON.stringify({ ok: false, msg: '脚本未注册 request 监听器' }));
    process.exit(0);
  }

  reqHandler({
    action,
    source: platform,
    info: {
      type: quality,
      musicInfo
    }
  }).then(res => {
    if (action === 'pic') {
      const picUrl = (typeof res === 'string' ? res : (res?.url || res?.pic || '')) || '';
      process.stdout.write(JSON.stringify({
        ok: !!picUrl,
        url: String(picUrl).trim()
      }));
    } else if (action === 'lyric') {
      const lyric = (typeof res === 'string' ? res : (res?.lyric || '')) || '';
      const tlyric = res?.tlyric || '';
      process.stdout.write(JSON.stringify({
        ok: !!lyric,
        lyric: String(lyric).trim(),
        tlyric: String(tlyric).trim()
      }));
    } else if (res && typeof res === 'string') {
      process.stdout.write(JSON.stringify({
        ok: true,
        url: res.trim(),
        format: quality.includes('flac') ? 'flac' : 'mp3',
        sourceName: initedInfo?.name || '飞牛内置音源'
      }));
    } else if (res && typeof res === 'object' && res.url) {
      process.stdout.write(JSON.stringify({
        ok: true,
        url: String(res.url).trim(),
        format: res.type || (quality.includes('flac') ? 'flac' : 'mp3'),
        sourceName: initedInfo?.name || '飞牛内置音源'
      }));
    } else {
      process.stdout.write(JSON.stringify({ ok: false, msg: '未能返回有效数据' }));
    }
  }).catch(err => {
    process.stdout.write(JSON.stringify({ ok: false, msg: err?.message || String(err) }));
  });

} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, msg: `脚本执行异常: ${e.message}` }));
}
