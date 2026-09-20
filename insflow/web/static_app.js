
function toggleTheme(){
  var dark = document.documentElement.getAttribute('data-theme') === 'dark';
  if (dark) { document.documentElement.removeAttribute('data-theme'); }
  else { document.documentElement.setAttribute('data-theme','dark'); }
  try{ localStorage.setItem('if_theme', dark ? 'light' : 'dark'); }catch(e){}
}
function toast(text, type){
  var box = document.getElementById('toast'); if(!box) return;
  var el = document.createElement('div');
  el.textContent = text;
  el.style.cssText = 'position:fixed;right:22px;bottom:22px;z-index:99999;padding:12px 18px;border-radius:12px;font-size:13px;min-width:240px;box-shadow:0 12px 32px rgba(0,0,0,.25);background:' +
    (type==='error' ? 'var(--danger)' : type==='warn' ? 'var(--warn)' : 'var(--ok)') + ';color:var(--on-accent)';
  box.appendChild(el);
  setTimeout(function(){ el.remove(); }, 3200);
}

/* ── 实时流（SSE）：指标快照 / 告警 / 心跳 ── */
var IFLIVE = {es: null, on: false, metrics: []};
function ifLiveMetrics(){
  var el = document.querySelector('[data-live-metrics]');
  return el ? (el.getAttribute('data-live-metrics') || '').split(',').filter(Boolean)
            : ['ga4_sessions', 'gsc_clicks'];
}
function ifToggleLive(){
  if(IFLIVE.on) ifStopLive(); else ifStartLive();
}
function ifStartLive(){
  if(!('EventSource' in window)){ toast('浏览器不支持实时流', 'warn'); return; }
  var ws = document.body.getAttribute('data-ws') || '';
  IFLIVE.metrics = ifLiveMetrics();
  var url = '/api/v1/stream?workspace_id=' + encodeURIComponent(ws) +
            '&metrics=' + encodeURIComponent(IFLIVE.metrics.join(',')) +
            '&days=' + encodeURIComponent(new URL(location.href).searchParams.get('days') || '7');
  IFLIVE.es = new EventSource(url);
  IFLIVE.on = true;
  var box = document.getElementById('if-live'); if(box) box.hidden = false;
  document.getElementById('if-live-btn').style.color = 'var(--ok)';
  IFLIVE.es.addEventListener('metrics', function(e){
    try{ ifPaintLive(JSON.parse(e.data)); }catch(err){}
  });
  IFLIVE.es.addEventListener('alert', function(e){
    try{ var d = JSON.parse(e.data); toast('告警：' + (d.payload && d.payload.metric || d.type), 'warn'); }catch(err){}
  });
  IFLIVE.es.addEventListener('heartbeat', function(){
    var at = document.getElementById('if-live-at');
    if(at) at.textContent = '心跳 ' + new Date().toLocaleTimeString();
  });
  IFLIVE.es.onerror = function(){
    var at = document.getElementById('if-live-at');
    if(at) at.textContent = '连接中断，自动重连中…';
  };
}
function ifPaintLive(snap){
  var box = document.getElementById('if-live-cards'); if(!box || !snap.metrics) return;
  var html = '';
  Object.keys(snap.metrics).forEach(function(k){
    var m = snap.metrics[k] || {};
    var vals = m.spark || [];
    var lo = Math.min.apply(null, vals.concat([0])), hi = Math.max.apply(null, vals.concat([1]));
    var pts = vals.map(function(v, i){
      var x = vals.length > 1 ? (i / (vals.length - 1)) * 90 : 0;
      var y = 20 - ((v - lo) / Math.max(1e-9, hi - lo)) * 18;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    html += '<div class="card kpi"><div class="num">' + Number(m.value).toLocaleString() +
            '</div><div class="label">' + k + '</div>' +
            '<svg width="90" height="22" aria-hidden="true"><polyline points="' + pts +
            '" fill="none" stroke="var(--accent)" stroke-width="1.4"/></svg></div>';
  });
  box.innerHTML = html;
  var at = document.getElementById('if-live-at');
  if(at) at.textContent = '更新于 ' + String(snap.at || '').slice(11, 19);
}
function ifStopLive(){
  if(IFLIVE.es){ IFLIVE.es.close(); IFLIVE.es = null; }
  IFLIVE.on = false;
  var box = document.getElementById('if-live'); if(box) box.hidden = true;
  var btn = document.getElementById('if-live-btn'); if(btn) btn.style.color = '';
}
document.addEventListener('visibilitychange', function(){
  if(document.hidden && IFLIVE.on) ifStopLive();     // 页面隐藏暂停，省资源
});

/* ── 在线协同（presence）：谁在看 + 光标 ── */
var IFPRES = {conn: Math.random().toString(36).slice(2, 10), timer: null, last: 0};
function ifPresenceTick(send){
  var ws = document.body.getAttribute('data-ws') || '';
  if(!ws) return;
  var now = Date.now();
  var payload = {workspace_id: ws, conn_id: IFPRES.conn, path: location.pathname};
  if(send || now - IFPRES.last > 9000){
    IFPRES.last = now;
    if(send && IFPRES.lastCursor) payload.cursor = IFPRES.lastCursor;
    fetch('/api/v1/presence', {method:'POST', headers:{'Content-Type':'application/json'},
      credentials:'same-origin', body: JSON.stringify(payload)})
      .then(function(r){ return r.json(); })
      .then(function(j){ ifPaintPresence(j.online || []); })
      .catch(function(){});
  }
}
function ifPaintPresence(online){
  var el = document.getElementById('if-online');
  if(el){
    if(online.length > 1){
      el.hidden = false;
      el.innerHTML = '<i></i>' + online.length + ' 人在看：' +
        online.map(function(p){ return p.user; }).slice(0, 3).join('、');
    } else { el.hidden = true; }
  }
  var box = document.getElementById('if-cursors');
  if(!box) return;
  var others = online.filter(function(p){ return p.conn_id !== IFPRES.conn && p.cursor; });
  box.innerHTML = others.slice(0, 5).map(function(p){
    return '<span class="if-cursor" style="left:' + (p.cursor.x * 100).toFixed(1) +
           '%;top:' + (p.cursor.y * 100).toFixed(1) + '%">' +
           String(p.user).split('@')[0].slice(0, 8) + '</span>';
  }).join('');
}
document.addEventListener('mousemove', function(e){
  IFPRES.lastCursor = {x: e.clientX / Math.max(1, window.innerWidth),
                       y: e.clientY / Math.max(1, window.innerHeight)};
});
document.addEventListener('DOMContentLoaded', function(){ ifPresenceTick(true); });
setInterval(function(){ ifPresenceTick(false); }, 10000);
window.addEventListener('beforeunload', function(){
  var ws = document.body.getAttribute('data-ws') || '';
  try{
    navigator.sendBeacon('/api/v1/presence/leave',
      new Blob([JSON.stringify({workspace_id: ws, conn_id: IFPRES.conn})],
               {type:'application/json'}));
  }catch(e){}
});

/* ── 布局历史（协作可回溯到上一版） ── */
async function ifLayoutHistory(){
  var r = await fetch('/api/v1/ui/layout/history?workspace_id=' +
    encodeURIComponent(document.body.getAttribute('data-ws') || '') +
    '&path=' + encodeURIComponent(location.pathname), {credentials:'same-origin'});
  var j = await r.json();
  var list = j.history || [];
  if(!list.length){ alert('暂无历史版本（改动并保存布局后会记录）'); return; }
  var pick = prompt('历史版本（数字=回溯到该版本）：\n' +
    list.map(function(h, i){ return (i+1) + '. ' + h.at + ' → ' + h.order.join(' , '); }).join('\n'));
  var idx = parseInt(pick, 10) - 1;
  if(isNaN(idx) || idx < 0 || idx >= list.length) return;
  ifApplyLayout(list[idx].order);
  ifSaveLayout(list[idx].order);
}

/* ── 角色能力：按 /api/v1/auth/me 隐藏无权限入口（fail-closed 由后端保证） ── */
(async function(){
  try{
    var r = await fetch('/api/v1/auth/me', {credentials:'same-origin'});
    if(!r.ok) return;
    var j = await r.json();
    var caps = j.capabilities || [];
    window.__ifRole = j.role;
    document.querySelectorAll('[data-cap]').forEach(function(el){
      var need = el.getAttribute('data-cap');
      var ok = caps.indexOf('*') >= 0 || caps.indexOf(need) >= 0;
      if(!ok) el.style.display = 'none';
    });
  }catch(e){}
})();

/* ── 顶栏工作区切换（OPC 多客户快切；偏好记忆） ── */
function ifCurrentWs(){
  return document.body.getAttribute('data-ws') || '';
}
function ifSwitchWs(id){
  if(!id) return;
  try{ localStorage.setItem('if_ws', id); }catch(e){}
  var u = new URL(location.href);
  u.searchParams.set('workspace_id', id);
  location.href = u.toString();
}
function ifToggleWs(ev){
  if(ev) ev.stopPropagation();
  var pop = document.getElementById('if-ws');
  if(!pop) return;
  var open = pop.hidden;
  document.querySelectorAll('.more-pop').forEach(function(p){ p.hidden = true; });
  pop.hidden = !open;
  var btn = document.getElementById('if-ws-btn');
  if(btn) btn.setAttribute('aria-expanded', String(!pop.hidden));
  if(!open) return;
  var cur = ifCurrentWs();
  pop.innerHTML = '<div class="more-item" style="opacity:.6;cursor:default">切换工作区</div>';
  fetch('/api/v1/workspaces', {credentials:'same-origin'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      (j.workspaces || []).forEach(function(w){
        var b = document.createElement('button');
        b.className = 'more-item' + (w.id === cur ? ' ws-on' : '');
        b.textContent = (w.id === cur ? '● ' : '○ ') + (w.name || w.id) +
                        '（' + w.id + '）';
        b.onclick = function(){ ifSwitchWs(w.id); };
        pop.appendChild(b);
      });
      if(!(j.workspaces || []).length){
        pop.innerHTML += '<div class="more-item" style="opacity:.6;cursor:default">无可用工作区</div>';
      }
      var mine = document.createElement('button');
      mine.className = 'more-item';
      mine.textContent = '＋ 新建工作区（用向导创建）';
      mine.onclick = function(){
        location.href = '/console/onboarding?workspace_id=' + encodeURIComponent(cur);
      };
      pop.appendChild(mine);
    })
    .catch(function(){});
}

/* ── 顶栏「更多」下拉（低频操作收纳，带文字标签） ── */
function ifToggleMore(ev){
  if(ev) ev.stopPropagation();
  var pop = document.getElementById('if-more');
  if(!pop) return;
  var open = pop.hidden;
  document.querySelectorAll('.more-pop').forEach(function(p){ p.hidden = true; });
  pop.hidden = !open;
  var btn = pop.parentNode.querySelector('.icon-btn');
  if(btn) btn.setAttribute('aria-expanded', String(!pop.hidden));
}
document.addEventListener('click', function(e){
  if(e.target.closest('.more-menu')) return;
  document.querySelectorAll('.more-pop').forEach(function(p){ p.hidden = true; });
});

/* ── 交互打磨 v2：抽屉可达性 / toast 关闭 / 顶栏阴影 ── */
function ifCloseDrawer(){
  var d = document.getElementById('if-drawer');
  if(d) d.classList.remove('on');
}
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape') ifCloseDrawer();
});
document.addEventListener('click', function(e){
  var d = document.getElementById('if-drawer');
  if(!d || !d.classList.contains('on')) return;
  // 点击抽屉外部区域关闭（抽屉内容自身不关）
  if(!e.target.closest('#if-drawer') && !e.target.closest('[data-keep-drawer]')) ifCloseDrawer();
}, true);
window.addEventListener('scroll', function(){
  document.body.classList.toggle('scrolled', window.scrollY > 4);
}, { passive: true });
/* toast：最多堆 4 条、可点击关闭、按类型给 title 提示 */
(function(){
  var box = document.getElementById('toast');
  if(!box) return;
  new MutationObserver(function(){
    while(box.children.length > 4) box.removeChild(box.firstChild);
    Array.prototype.forEach.call(box.children, function(el){
      if(el.__ifBound) return;
      el.__ifBound = true;
      el.setAttribute('role', 'status');
      el.style.cursor = 'pointer';
      el.title = '点击关闭';
      el.addEventListener('click', function(){ el.remove(); });
    });
  }).observe(box, { childList: true });
})();

/* ── 全局问数（⌘K）：NL → Agent 问数（无 LLM 也可用规则解析）── */
function ifDecorateDrawer(){
  var d = document.getElementById('if-drawer');
  if(!d || d.querySelector('.dr-close')) return;
  var h3 = d.querySelector('h3');
  if(h3){
    var wrap = document.createElement('div');
    wrap.className = 'dr-head';
    h3.parentNode.insertBefore(wrap, h3);
    wrap.appendChild(h3);
    var btn = document.createElement('button');
    btn.className = 'dr-close';
    btn.type = 'button';
    btn.setAttribute('aria-label', '关闭');
    btn.textContent = '✕';
    btn.onclick = ifCloseDrawer;
    wrap.appendChild(btn);
    wrap.style.position = 'relative';
  }
}
new MutationObserver(ifDecorateDrawer).observe(document.body, { childList: true, subtree: true });

function ifAsk(){
  var d = document.getElementById('if-drawer');
  if(!d){ d = document.createElement('div'); d.id='if-drawer'; document.body.appendChild(d); }
  d.classList.add('on');
  d.innerHTML = '<h3>问数</h3>'
    + '<div class="dr-sub">例：近 30 天 ga4_sessions 趋势 / 数据质量怎么样 / 哪个渠道贡献最大 / 动作增量如何</div>'
    + '<div style="margin:8px 0"><input id="if-ask-q" style="width:100%" '
    + 'placeholder="输入问题后回车（⌘K 打开）"></div>'
    + '<div class="toolbar"><button class="dp-btn" onclick="ifAskRun()">提问</button>'
    + '<span id="if-ask-status" class="dp-btn" style="border:0"></span></div>'
    + '<div id="if-ask-out" style="white-space:pre-wrap;font-size:12.5px;margin-top:8px"></div>';
  var input = document.getElementById('if-ask-q');
  if(input){
    input.focus();
    input.addEventListener('keydown', function(e){ if(e.key === 'Enter') ifAskRun(); });
  }
}
async function ifAskRun(){
  var ws = document.body.getAttribute('data-ws') || '';
  var q = (document.getElementById('if-ask-q') || {}).value || '';
  var st = document.getElementById('if-ask-status'), out = document.getElementById('if-ask-out');
  if(!q.trim()){ if(st) st.textContent = '请输入问题'; return; }
  if(st) st.textContent = '思考中…';
  try{
    var r = await fetch('/api/v1/agent/ask?workspace_id=' + encodeURIComponent(ws),
      {method:'POST', headers:{'Content-Type':'application/json'},
       credentials:'same-origin', body: JSON.stringify({question: q})});
    var j = await r.json();
    if(st) st.textContent = j.mode === 'llm' ? 'LLM 回答' : '检索/规则回答';
    if(out) out.textContent = j.answer || '（无内容）';
  }catch(e){ if(st) st.textContent = '失败：' + e; }
}
document.addEventListener('keydown', function(e){
  if((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')){
    e.preventDefault(); ifAsk();
  }
});

/* ── 图表级批注（协作）：面板头部 💬 打开抽屉，可看可评 ── */
async function ifPanelComments(panelKey, el){
  var d = document.getElementById('if-drawer');
  if(!d){ d = document.createElement('div'); d.id='if-drawer'; document.body.appendChild(d); }
  d.classList.add('on');
  var title = el && el.closest('.dp-title') ? el.closest('.dp-title').childNodes[0].textContent.trim() : panelKey;
  d.innerHTML = '<h3>批注 · ' + title + '</h3><div class="empty">加载中…</div>';
  var ws = document.body.getAttribute('data-ws') || '';
  function paint(items){
    var html = '<h3>批注 · ' + title + '</h3>';
    html += '<div class="dr-sub">' + items.length + ' 条 · 讨论不触发动作（动作仍走"派发 → 14 天验证"）</div>';
    html += '<div style="margin:8px 0"><textarea id="if-annot-body" rows="2" style="width:100%" placeholder="写下你的判断/异议…"></textarea>'
         + '<div class="toolbar" style="margin-top:6px"><button class="dp-btn" onclick="ifPostAnnot(\'' + panelKey + '\')">发表</button>'
         + '<span id="if-annot-status" class="dp-btn" style="border:0"></span></div></div>';
    html += items.length ? items.map(function(c){
      return '<div style="padding:7px 0;border-bottom:1px solid var(--border)">'
        + '<b>' + String(c.author || '匿名') + '</b> · '
        + String(c.created_at || '').slice(5,16).replace('T',' ')
        + '<div>' + String(c.body || '').replace(/</g,'&lt;') + '</div></div>';
    }).join('') : '<div class="empty">还没有批注</div>';
    d.innerHTML = html;
  }
  try{
    var r = await fetch('/api/v1/comments?workspace_id=' + encodeURIComponent(ws) +
      '&target_type=chart&target_id=' + encodeURIComponent(panelKey), {credentials:'same-origin'});
    paint((await r.json()).comments || []);
  }catch(e){ d.innerHTML = '<h3>批注</h3><div class="empty">加载失败</div>'; }
}
async function ifPostAnnot(panelKey){
  var ws = document.body.getAttribute('data-ws') || '';
  var body = (document.getElementById('if-annot-body') || {}).value || '';
  var st = document.getElementById('if-annot-status');
  if(!body.trim()){ if(st) st.textContent = '内容为空'; return; }
  if(st) st.textContent = '发表中…';
  var r = await fetch('/api/v1/comments', {method:'POST',
    headers:{'Content-Type':'application/json'}, credentials:'same-origin',
    body: JSON.stringify({workspace_id: ws, target_type:'chart', target_id: panelKey,
                          body: body})});
  if(!r.ok){ if(st) st.textContent = '失败'; return; }
  if(st) st.textContent = '已发表 ✓';
  var btn = document.querySelector('[data-annot="' + panelKey + '"]');
  ifPanelComments(panelKey, btn);
}
(async function ifLoadAnnotCounts(){
  var ws = document.body.getAttribute('data-ws') || '';
  if(!ws) return;
  try{
    var r = await fetch('/api/v1/comments/counts?workspace_id=' + encodeURIComponent(ws) +
      '&target_type=chart', {credentials:'same-origin'});
    var counts = (await r.json()).counts || {};
    document.querySelectorAll('[data-annot]').forEach(function(b){
      var n = counts[b.getAttribute('data-annot')] || 0;
      var span = b.querySelector('.annot-n');
      if(span) span.textContent = n;
      b.style.opacity = n ? '1' : '.55';
    });
  }catch(e){}
})();

/* ── 记住上次工作区：URL 无 workspace_id 时补上（避免每次都回到 default） ── */
(function(){
  try{
    if(document.body.getAttribute('data-ws') !== 'default') return;
    var u = new URL(location.href);
    if(u.searchParams.get('workspace_id')) return;
    var last = localStorage.getItem('if_ws');
    if(last && last !== 'default'){ u.searchParams.set('workspace_id', last); location.replace(u.toString()); }
  }catch(e){}
})();

/* ── PWA 安装引导（移动端把"添加到主屏"做成显式按钮）── */
var IFINSTALL = {evt: null};
window.addEventListener('beforeinstallprompt', function(e){
  e.preventDefault();
  IFINSTALL.evt = e;
  var btn = document.getElementById('m-install');
  if (btn) btn.hidden = false;
});
async function ifInstall(){
  if (!IFINSTALL.evt) { toast('浏览器未提供安装入口（iOS 请用「分享 → 添加到主屏」）', 'warn'); return; }
  IFINSTALL.evt.prompt();
  var res = await IFINSTALL.evt.userChoice;
  IFINSTALL.evt = null;
  toast(res && res.outcome === 'accepted' ? '已添加到主屏' : '已取消');
}

/* ── Cross-filter（跨图联动）：点条形/格子/箱体 → 按维度筛选全屏 ── */
function ifCrossFilter(dim, value){
  var u = new URL(location.href);
  var key = 'cf.' + dim;
  if (u.searchParams.get(key) === value) u.searchParams.delete(key);
  else u.searchParams.set(key, value);
  location.href = u.toString();
}
function ifClearCrossFilter(){
  var u = new URL(location.href);
  Array.from(u.searchParams.keys()).forEach(function(k){
    if (k.indexOf('cf.') === 0) u.searchParams.delete(k);
  });
  location.href = u.toString();
}
document.addEventListener('click', function(e){
  var el = e.target.closest('[data-cf]');
  if(!el) return;
  var raw = String(el.getAttribute('data-cf') || ''), i = raw.indexOf(':');
  if(i < 0) return;
  e.stopPropagation();
  ifCrossFilter(raw.slice(0, i), raw.slice(i + 1));
});
document.addEventListener('keydown', function(e){
  if(e.key !== 'Enter' && e.key !== ' ') return;
  var el = e.target.closest && e.target.closest('[data-cf]');
  if(!el) return;
  e.preventDefault(); el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
});

/* ── 看板自定义布局（拖拽排序 + 服务端持久化，失败回落 localStorage） ── */
function ifLayoutKey(){ return 'layout:' + location.pathname; }
function ifSaveLayoutLocal(order){
  try{ localStorage.setItem(ifLayoutKey(), JSON.stringify(order)); }catch(e){}
}
function ifLoadLayoutLocal(){
  try{ return JSON.parse(localStorage.getItem(ifLayoutKey()) || 'null'); }catch(e){ return null; }
}
async function ifSaveLayout(order){
  ifSaveLayoutLocal(order);
  try{
    await fetch('/api/v1/ui/layout', {
      method:'PUT', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({workspace_id: document.body.getAttribute('data-ws') || '',
                            path: location.pathname, order: order}),
    });
    toast('布局已保存');
  }catch(e){ toast('已保存到本机（服务端不可用）', 'warn'); }
}
function ifApplyLayout(order){
  if(!order || !order.length) return;
  var host = document.getElementById('if-content') || document.body;
  var blocks = Array.prototype.slice.call(host.querySelectorAll('[data-block]'));
  order.forEach(function(id){
    var el = blocks.filter(function(b){ return b.getAttribute('data-block') === id; })[0];
    if(el) host.appendChild(el);
  });
}
function ifSetupLayout(){
  var host = document.getElementById('if-content') || document.body;
  var blocks = Array.prototype.slice.call(host.querySelectorAll('[data-block]'));
  if(blocks.length < 2) return;
  blocks.forEach(function(b){
    b.setAttribute('draggable','true');
    b.style.cursor = 'grab';
    b.addEventListener('dragstart', function(e){
      e.dataTransfer.setData('text/plain', b.getAttribute('data-block'));
      b.style.opacity = .5;
    });
    b.addEventListener('dragend', function(){ b.style.opacity = 1; });
  });
  host.addEventListener('dragover', function(e){
    var dragging = host.querySelector('[data-block][style*="opacity: 0.5"]');
    if(!dragging) return;
    e.preventDefault();
    var after = Array.prototype.slice.call(
      host.querySelectorAll('[data-block]:not([style*="opacity: 0.5"])'))
      .reduce(function(closest, el){
        var box = el.getBoundingClientRect(), off = e.clientY - box.top - box.height/2;
        return (off < 0 && off > closest.off) ? {off: off, el: el} : closest;
      }, {off: -Infinity}).el;
    if(after) host.insertBefore(dragging, after); else host.appendChild(dragging);
  });
  host.addEventListener('drop', function(){
    ifSaveLayout(Array.prototype.slice.call(host.querySelectorAll('[data-block]'))
      .map(function(b){ return b.getAttribute('data-block'); }));
  });
  if(window.__ifLayoutLoaded) return;
  window.__ifLayoutLoaded = 1;
  fetch('/api/v1/ui/layout?workspace_id=' +
        encodeURIComponent(document.body.getAttribute('data-ws') || '') +
        '&path=' + encodeURIComponent(location.pathname), {credentials:'same-origin'})
    .then(function(r){ return r.json(); })
    .then(function(j){ ifApplyLayout((j && j.order && j.order.length) ? j.order : ifLoadLayoutLocal()); })
    .catch(function(){ ifApplyLayout(ifLoadLayoutLocal()); });
}
document.addEventListener('DOMContentLoaded', function(){ try{ ifSetupLayout(); }catch(e){} });
(function(){
  if (document.getElementById('if-pwa')) return;
  var l = document.createElement('link');
  l.rel = 'manifest'; l.id = 'if-pwa';
  l.href = '/console/manifest.webmanifest';
  document.head.appendChild(l);
  var m = document.createElement('meta'); m.name = 'theme-color'; m.content = '#2563eb';
  document.head.appendChild(m);
  if ('serviceWorker' in navigator && location.protocol === 'https:'){
    navigator.serviceWorker.register('/console/sw.js', {scope: ((document.body && document.body.dataset.basePath) || '') + '/console/'}).catch(function(){});
  }
})();

/* ── 图例开关（P1）：点击图例隐藏/显示序列（a11y：role=button + 键盘） ── */
function ifToggleSeries(name, el){
  var off = el.classList.toggle('off');
  el.setAttribute('aria-pressed', off ? 'false' : 'true');
  var host = el.closest('.dp-chart') || document;
  host.querySelectorAll('[data-series]').forEach(function(n){
    if(n.getAttribute('data-series') === name) n.style.display = off ? 'none' : '';
  });
}
function ifLegendKey(ev, name, el){
  if(ev.key === 'Enter' || ev.key === ' '){ ev.preventDefault(); ifToggleSeries(name, el); }
}

/* ── P0 图表交互：tooltip / 十字准线 / 下钻抽屉 / 图-表切换 ── */
function dpView(uid, mode){
  var box = document.getElementById(uid); if(!box) return;
  var chart = box.querySelector('.dp-chart'), table = box.querySelector('.dp-tablewrap');
  chart.hidden = (mode !== 'chart'); table.hidden = (mode !== 'table');
  box.querySelectorAll('.dp-btn').forEach(function(b){
    if(b.tagName === 'BUTTON') b.classList.toggle('dp-on',
      (mode === 'chart' && b.textContent.trim() === '图') ||
      (mode === 'table' && b.textContent.trim() === '表'));
  });
}
var _tip = null;
function _tipEl(){
  if(!_tip){ _tip = document.createElement('div'); _tip.id = 'if-tip'; document.body.appendChild(_tip); }
  return _tip;
}
function _showTip(html, ev){
  var t = _tipEl(); t.innerHTML = html; t.classList.add('on');
  var pad = 14, w = t.offsetWidth, h = t.offsetHeight;
  var x = Math.min(ev.clientX + pad, window.innerWidth - w - 8);
  var y = Math.max(8, ev.clientY - h - pad);
  t.style.left = x + 'px'; t.style.top = y + 'px';
}
function _hideTip(){ if(_tip) _tip.classList.remove('on'); }
document.addEventListener('mouseover', function(e){
  var el = e.target.closest('[data-tip]'); if(!el) return;
  _showTip(String(el.getAttribute('data-tip')).replace(/·/g, '·'), e);
});
document.addEventListener('mousemove', function(e){
  if(_tip && _tip.classList.contains('on')) _showTip(_tip.innerHTML, e);
});
document.addEventListener('mouseout', function(e){ if(e.target.closest('[data-tip]')) _hideTip(); });

/* 十字准线：折线图 hover 时显示竖线 + 各序列值（服务端已写 data-chart） */
document.addEventListener('mousemove', function(e){
  var svg = e.target.closest('svg[data-chart]');
  document.querySelectorAll('svg[data-chart] .if-cross').forEach(function(n){ n.remove(); });
  if(!svg) return;
  var meta; try { meta = JSON.parse(svg.getAttribute('data-chart')); } catch(err){ return; }
  var rect = svg.getBoundingClientRect();
  var vb = svg.viewBox.baseVal, sx = vb.width / rect.width;
  var x = (e.clientX - rect.left) * sx;
  var x0 = meta.plot[0], w = meta.plot[2], ptW = meta.plot[3];
  if(x < x0 || x > x0 + w) return;
  var n = meta.labels.length; if(n < 2) return;
  var i = Math.round((x - x0) / w * (n - 1));
  i = Math.max(0, Math.min(n - 1, i));
  var g = document.createElementNS('http://www.w3.org/2000/svg','g');
  g.setAttribute('class','if-cross');
  var gx = x0 + w * i / (n - 1);
  var line = document.createElementNS('http://www.w3.org/2000/svg','line');
  line.setAttribute('x1',gx); line.setAttribute('x2',gx);
  line.setAttribute('y1',meta.plot[1]); line.setAttribute('y2',meta.plot[1]+ptW);
  line.setAttribute('stroke','var(--border-strong)'); line.setAttribute('stroke-dasharray','3 3');
  g.appendChild(line);
  var lines = ['<b>' + meta.labels[i] + '</b>'];
  meta.series.forEach(function(s){
    var v = s.values[i]; if(v === undefined) return;
    lines.push(s.name + ': ' + Number(v).toLocaleString());
  });
  svg.appendChild(g);
  _showTip(lines.join('<br>'), e);
});
document.addEventListener('mouseleave', function(){
  document.querySelectorAll('svg[data-chart] .if-cross').forEach(function(n){ n.remove(); });
});

/* ══ 图表引擎增强：Canvas 渲染 + 缩放/框选（跨图联动）+ 区间下钻 ══
   - 大数据量（data-canvas）与缩放叠加都用 Canvas 重绘（同一渲染器）
   - 拖拽 = 框选缩放；双击 = 重置；滚轮 = 以光标为中心缩放；Shift+拖拽 = 平移
   - 框选后给出"区间洞察"按钮（拉该区间的指标 + 关联洞察，服务验证闭环） */
var IFVIEW = { win: null, charts: [] };

function ifParseMeta(el){
  try { return JSON.parse(el.getAttribute('data-chart')); } catch(e){ return null; }
}
function ifDrawScatter(g, meta, W, H){
  var colBorder = getComputedStyle(document.documentElement).getPropertyValue('--border') || '#ddd';
  var colAccent = getComputedStyle(document.documentElement).getPropertyValue('--accent') || '#2563eb';
  var colOk = getComputedStyle(document.documentElement).getPropertyValue('--ok') || '#16a34a';
  var pad = meta.plot || [46,16,660,256];
  var sx = W / 720;
  var x0 = pad[0]*sx, y0 = pad[1], pw = pad[2]*sx, ph = pad[3];
  var xmax = meta.x_max || 1, ymax = meta.y_max || 1;
  var xm = meta.x_mid, ym = meta.y_mid;
  g.strokeStyle = colBorder; g.lineWidth = 1;
  for (var t=0;t<=4;t++){ var yy=y0+ph-ph*t/4; g.globalAlpha=t===0?0.9:0.3;
    g.beginPath(); g.moveTo(x0,yy); g.lineTo(x0+pw,yy); g.stroke(); }
  g.globalAlpha = 1;
  var gx = x0 + pw*Math.min(1, xm/xmax), gy = y0 + ph - ph*Math.min(1, ym/ymax);
  g.fillStyle = colOk; g.globalAlpha = 0.07;
  g.fillRect(gx, y0, x0+pw-gx, gy-y0); g.globalAlpha = 1;
  g.strokeStyle = colBorder; g.beginPath(); g.moveTo(gx,y0); g.lineTo(gx,y0+ph); g.stroke();
  g.beginPath(); g.moveTo(x0,gy); g.lineTo(x0+pw,gy); g.stroke();
  meta.points.forEach(function(p){
    var cx = x0 + pw*Math.min(1, p[0]/xmax), cy = y0 + ph - ph*Math.min(1, p[1]/ymax);
    var good = (p[0] >= xm) === (meta.good_quadrant === 'tr' || meta.good_quadrant === 'br')
            && (p[1] >= ym) === (meta.good_quadrant === 'tl' || meta.good_quadrant === 'tr');
    g.fillStyle = good ? colOk : colAccent; g.globalAlpha = 0.8;
    g.beginPath(); g.arc(cx, cy, 3.4, 0, 6.2832); g.fill();
  });
  g.globalAlpha = 1;
  return { x0: x0, y0: y0, pw: pw, ph: ph, xmax: xmax, ymax: ymax };
}
function ifDrawHeatmap(g, meta, W){
  var cs = getComputedStyle(document.documentElement);
  var colAccent = cs.getPropertyValue('--accent') || '#2563eb';
  var colFg = cs.getPropertyValue('--fg') || '#111';
  var colFaint = cs.getPropertyValue('--faint') || '#999';
  var pad = meta.plot || [121,18,22,26];
  var sx = W / 720;
  var rowW = pad[0]*sx, cw = pad[2]*sx, ch = pad[3];
  g.font = '9px sans-serif';
  meta.cols.forEach(function(c, j){
    g.fillStyle = colFaint; g.fillText(String(c).slice(0,6), rowW + cw*j + cw/2 - 12, 12);
  });
  meta.rows.forEach(function(r, i){
    var y = 18 + i*ch;
    g.fillStyle = colFg; g.fillText(String(r).slice(0,16), 0, y + ch/2 + 4);
    meta.cols.forEach(function(c, j){
      var v = Math.max(0, Math.min(1, (meta.matrix[i]||[])[j] || 0));
      g.fillStyle = colAccent; g.globalAlpha = 0.12 + 0.88*v;
      g.fillRect(rowW + cw*j + 1, y, cw - 2, ch - 4);
    });
    g.globalAlpha = 1;
  });
}
/* ══ WebGL 渲染（零依赖手写 GL）：大数据量散点/折线走 GPU ══
   阈值：散点 > 8000 或折线总点 > 40000 且 WebGL 可用；否则保持 2D Canvas。
   着色器只做正交投影 + 属性色，不引入任何库；上下文创建失败即回退。 */
var IFGL = { prog: null, gl: null, threshold_points: 8000, threshold_line: 40000 };
function ifGLEnabled(){ return !!IFGL.threshold_points; }
function ifGetGL(canvas){
  if (IFGL.gl && IFGL.gl.canvas === canvas) return IFGL.gl;
  var gl = null;
  try { gl = canvas.getContext('webgl', {antialias: true, alpha: false,
                                        preserveDrawingBuffer: true}); } catch(e){}
  if (!gl) return null;
  gl.clearColor(1,1,1,0);
  IFGL.gl = gl; IFGL.prog = null;
  return gl;
}
function ifCompile(gl, type, src){
  var sh = gl.createShader(type);
  gl.shaderSource(sh, src); gl.compileShader(sh);
  return gl.getShaderParameter(sh, gl.COMPILE_STATUS) ? sh : null;
}
function ifProgram(gl){
  if (IFGL.prog) return IFGL.prog;
  var vs = ifCompile(gl, gl.VERTEX_SHADER,
    'attribute vec2 a_p; attribute vec3 a_c; varying vec3 v_c;' +
    'void main(){ v_c = a_c; gl_Position = vec4(a_p, 0.0, 1.0); }');
  var fs = ifCompile(gl, gl.FRAGMENT_SHADER,
    'precision mediump float; varying vec3 v_c;' +
    'void main(){ gl_FragColor = vec4(v_c, 1.0); }');
  if (!vs || !fs) return null;
  var p = gl.createProgram();
  gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) return null;
  IFGL.prog = p;
  return p;
}
function ifHexRGB(hex){
  var h = String(hex || '#2563eb').trim();
  if (h.charAt(0) !== '#' || (h.length !== 7 && h.length !== 4)) return [0.15,0.4,0.9];
  if (h.length === 4) h = '#' + h[1]+h[1]+h[2]+h[2]+h[3]+h[3];
  return [parseInt(h.substr(1,2),16)/255, parseInt(h.substr(3,2),16)/255,
          parseInt(h.substr(5,2),16)/255];
}
function ifGLDraw(gl, points, colors, mode, size){
  var prog = ifProgram(gl); if (!prog) return false;
  gl.useProgram(prog);
  var buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(points), gl.STATIC_DRAW);
  var a_p = gl.getAttribLocation(prog, 'a_p');
  gl.enableVertexAttribArray(a_p);
  gl.vertexAttribPointer(a_p, 2, gl.FLOAT, false, 0, 0);
  var buf2 = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf2);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(colors), gl.STATIC_DRAW);
  var a_c = gl.getAttribLocation(prog, 'a_c');
  gl.enableVertexAttribArray(a_c);
  gl.vertexAttribPointer(a_c, 3, gl.FLOAT, false, 0, 0);
  if (mode === 'points') gl.drawArrays(gl.POINTS, 0, points.length / 2);
  else gl.drawArrays(gl.LINES, 0, points.length / 2);
  gl.deleteBuffer(buf); gl.deleteBuffer(buf2);
  return true;
}
function ifDrawGL(canvas, meta, W, H){
  var gl = ifGetGL(canvas); if (!gl) return false;
  var cs = getComputedStyle(document.documentElement);
  var bg = cs.getPropertyValue('--bg') || '#fff';
  var c = ifHexRGB(bg.trim());
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(c[0], c[1], c[2], 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  var pad = meta.plot || [44, 14, 660, 180];
  if (meta.kind === 'scatter'){
    var pts = meta.points || [], coords = [], colors = [];
    var col = ifHexRGB((cs.getPropertyValue('--accent') || '').trim());
    pts.forEach(function(p){
      var x0 = pad[0] + pad[2] * Math.min(1, p[0] / (meta.x_max || 1));
      var y0 = pad[1] + pad[3] - pad[3] * Math.min(1, p[1] / (meta.y_max || 1));
      coords.push((x0 / 720) * 2 - 1, 1 - (y0 / canvas.height) * 2);
      colors.push(col[0], col[1], col[2]);
    });
    return ifGLDraw(gl, coords, colors, 'points');
  }
  return false;
}
function ifShouldGL(meta){
  if (!ifGLEnabled()) return false;
  if (meta.kind === 'scatter') return (meta.points || []).length > IFGL.threshold_points;
  var n = 0; (meta.series || []).forEach(function(s){ n += (s.values || []).length; });
  return meta.kind !== 'heatmap' && n > IFGL.threshold_line;
}
function ifDrawChart(canvas, meta, win, progress){
  var dpr = window.devicePixelRatio || 1;
  var W = canvas.clientWidth || canvas.width, H = canvas.height;
  canvas.width = W * dpr; canvas.height = H * dpr;
  var g = canvas.getContext('2d');
  g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,W,H);
  var cs = getComputedStyle(document.documentElement);
  var colBorder = cs.getPropertyValue('--border') || '#ddd';
  var colFaint = cs.getPropertyValue('--faint') || '#999';
  if (ifShouldGL(meta) && ifDrawGL(canvas, meta, W, H)){
    canvas.setAttribute('data-renderer', 'webgl');
    return;                                   // GPU 路径：散点/超大折线
  }
  canvas.setAttribute('data-renderer', '2d');
  if (meta.kind === 'scatter'){ ifDrawScatter(g, meta, W, H); return; }
  if (meta.kind === 'heatmap'){ ifDrawHeatmap(g, meta, W); return; }
  var labels = meta.labels || [], series = meta.series || [];
  var pad = meta.plot || [44,14,668,180];
  var sx = W / 720;
  var x0 = pad[0]*sx, y0 = pad[1], pw = pad[2]*sx, ph = pad[3];
  var i0 = win ? win[0] : 0, i1 = win ? win[1] : Math.max(0, labels.length-1);
  var n = Math.max(1, i1 - i0);
  var ymax = meta.y_max || 1;
  var P = (progress === undefined || progress === null) ? 1 : Math.max(0, Math.min(1, progress));
  // 网格
  g.strokeStyle = colBorder; g.lineWidth = 1;
  for (var t=0; t<=4; t++){
    var yy = y0 + ph - ph*t/4;
    g.globalAlpha = (t===0 ? 0.9 : 0.35) * Math.min(1, P*4); g.beginPath(); g.moveTo(x0, yy); g.lineTo(x0+pw, yy); g.stroke();
  }
  g.globalAlpha = 1;
  function px(i){ return x0 + pw * (i - i0) / n; }
  function py(v){ return y0 + ph - Math.min(1, Math.max(0, v/ymax)) * ph; }
  // 对比（上期）虚线
  if (meta.compare) meta.compare.series.forEach(function(s){
    g.strokeStyle = s.color || colFaint; g.lineWidth = 1.4; g.setLineDash([5,4]);
    g.beginPath();
    for (var i=i0; i<=i1; i++){ if(s.values[i]===undefined) continue; var X=px(i),Y=py(s.values[i]); i===i0?g.moveTo(X,Y):g.lineTo(X,Y); }
    g.stroke(); g.setLineDash([]);
  });
  // 序列（P<1 时按进度描线，形成"生长"动画）
  var last = i0 + Math.max(1, (i1-i0) * P);
  series.forEach(function(s){
    g.strokeStyle = s.color || '#2563eb'; g.lineWidth = 2; g.beginPath();
    for (var i=i0; i<=Math.min(i1, Math.round(last)); i++){
      if(s.values[i]===undefined) continue;
      var X=px(i),Y=py(s.values[i]); (i===i0)?g.moveTo(X,Y):g.lineTo(X,Y);
    }
    g.stroke();
  });
  // 预测虚线
  if (meta.forecast) Object.keys(meta.forecast).forEach(function(k){
    var f = meta.forecast[k], s = series.filter(function(s){return s.name===k;})[0] || {};
    g.strokeStyle = s.color || colFaint; g.setLineDash([6,4]); g.beginPath();
    f.preds.forEach(function(v, idx){
      var i = i1 + idx + 1; var X = px(i), Y = py(v);
      idx===0 ? g.moveTo(X,Y) : g.lineTo(X,Y);
    });
    g.stroke(); g.setLineDash([]);
  });
  // 标注（洞察/动作/验证钉在时间轴）
  (meta.annotations||[]).forEach(function(a){
    if (a.x < 0) return;
    g.strokeStyle = '#e11d48'; g.globalAlpha=0.7; g.setLineDash([4,3]);
    g.beginPath(); g.moveTo(a.x*sx, y0); g.lineTo(a.x*sx, y0+ph); g.stroke();
    g.setLineDash([]); g.globalAlpha=1;
  });
}

/* ── PNG 导出：把 CSS 变量内联进 SVG，再经 Image 光栅化下载 ── */
function ifCssVarMap(){
  var cs = getComputedStyle(document.documentElement), m = {};
  ['--accent','--ok','--warn','--danger','--muted','--faint','--fg','--border',
   '--bg','--card','--border-strong'].forEach(function(k){
    m[k] = (cs.getPropertyValue(k) || '').trim();
  });
  return m;
}
function ifInlineSvgVars(svg, vars){
  var nodes = [svg].concat(Array.prototype.slice.call(svg.querySelectorAll('*')));
  nodes.forEach(function(n){
    ['fill','stroke','stop-color','color'].forEach(function(attr){
      var v = n.getAttribute && n.getAttribute(attr);
      if (v && v.indexOf('var(') >= 0){
        n.setAttribute(attr, v.replace(/var\((--[a-z-]+)\)/g, function(_, k){
          return vars[k] || '#888';
        }));
      }
    });
    var st = n.getAttribute && n.getAttribute('style');
    if (st && st.indexOf('var(') >= 0){
      n.setAttribute('style', st.replace(/var\((--[a-z-]+)\)/g, function(_, k){
        return vars[k] || '#888';
      }));
    }
  });
  return svg;
}
function ifDownloadPNG(canvas, name){
  try{
    var url = canvas.toDataURL('image/png');
    var a = document.createElement('a');
    a.href = url; a.download = (name || 'chart') + '.png';
    document.body.appendChild(a); a.click(); a.remove();
    if(window.toast) toast('PNG 已导出');
  }catch(e){ if(window.toast) toast('导出失败：' + e, 'error'); }
}
function ifExportPNG(uid, name){
  var box = document.getElementById(uid); if(!box) return;
  var htmlCanvas = box.querySelector('.dp-chart canvas');
  if (htmlCanvas && box.querySelector('[data-canvas]')){ ifDownloadPNG(htmlCanvas, name); return; }
  var svgEl = box.querySelector('.dp-chart svg');
  if(!svgEl){ if(window.toast) toast('无可导出的图表', 'warn'); return; }
  var clone = svgEl.cloneNode(true);
  var vars = ifCssVarMap();
  ifInlineSvgVars(clone, vars);
  clone.setAttribute('xmlns','http://www.w3.org/2000/svg');
  var w = parseInt(svgEl.getAttribute('width') || svgEl.viewBox.baseVal.width || 720, 10);
  var h = parseInt(svgEl.getAttribute('height') || svgEl.viewBox.baseVal.height || 220, 10);
  var scale = 2, light = document.documentElement.getAttribute('data-theme') !== 'dark';
  var bg = light ? '#ffffff' : (vars['--bg'] || '#0f1115');
  var txt = serializeSvg(clone, w, h, bg, scale);
  var img = new Image();
  img.onload = function(){
    var cv = document.createElement('canvas');
    cv.width = w*scale; cv.height = h*scale;
    var g = cv.getContext('2d');
    g.fillStyle = bg; g.fillRect(0,0,cv.width,cv.height);
    g.drawImage(img, 0, 0, cv.width, cv.height);
    ifDownloadPNG(cv, name);
  };
  img.onerror = function(){ if(window.toast) toast('PNG 渲染失败（可改用 XLSX/CSV）','error'); };
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(txt);
}
function serializeSvg(node, w, h, bg, scale){
  var s = new XMLSerializer().serializeToString(node);
  return s.replace('<svg', '<svg width="' + (w*scale) + '" height="' + (h*scale) +
    '" preserveAspectRatio="xMidYMid meet"');
}

function ifAnimate(){
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var t0 = null;
  function step(ts){
    if(!t0) t0 = ts;
    var p = Math.min(1, (ts - t0) / 460);
    IFVIEW.charts.forEach(function(c){
      if (c._canvas && !c._svg){ ifDrawChart(c._canvas, c._meta, null, p); }
    });
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
function ifRenderAll(){
  IFVIEW.charts.forEach(function(c){
    var m = c._meta; if(!m) return;
    if (c._canvas){ ifDrawChart(c._canvas, m, IFVIEW.win); c._canvas.hidden = false; }
    if (c._svg) c._svg.style.visibility = IFVIEW.win ? 'hidden' : 'visible';
  });
  document.querySelectorAll('.if-zoominfo').forEach(function(el){
    if (IFVIEW.win){
      var L = (IFVIEW.charts[0]._meta.labels || []);
      el.textContent = '区间 ' + (L[IFVIEW.win[0]]||'') + ' ~ ' + (L[IFVIEW.win[1]]||'');
      el.hidden = false;
    } else { el.hidden = true; }
  });
  document.querySelectorAll('[data-drill-range]').forEach(function(b){ b.hidden = !IFVIEW.win; });
}
function ifSetupCharts(){
  IFVIEW.charts = [];
  document.querySelectorAll('svg[data-chart], canvas[data-chart]').forEach(function(el){
    var meta = ifParseMeta(el); if(!meta) return;
    var entry = { _meta: meta };
    if (el.tagName === 'CANVAS'){ entry._canvas = el; }
    else {
      entry._svg = el;
      // SVG 图上叠加 canvas（缩放时使用）
      var wrap = document.createElement('div');
      wrap.style.position = 'relative';
      el.parentNode.insertBefore(wrap, el); wrap.appendChild(el);
      var cv = document.createElement('canvas');
      cv.width = 720; cv.height = el.getAttribute('height') || 220;
      cv.style.cssText = 'width:100%;height:' + (el.getAttribute('height')||220) + 'px;display:none';
      wrap.appendChild(cv);
      entry._canvas = cv;
    }
    IFVIEW.charts.push(entry);
  });
  if(!IFVIEW.charts.length) return;
  // 大数据量折线/散点/热力（服务端直出 canvas）首屏做生长动画
  if (!window.__ifAnimated){
    window.__ifAnimated = 1;
    if (IFVIEW.charts.some(function(c){ return c._canvas && !c._svg; })) ifAnimate();
  }
  var host = IFVIEW.charts[0]._svg || IFVIEW.charts[0]._canvas;
  var box = host.closest('.dp-chart') || host.parentNode;
  // 区间操作条（一次性注入）
  if (!document.getElementById('if-zoombar')){
    var bar = document.createElement('div');
    bar.id = 'if-zoombar';
    bar.style.cssText = 'display:flex;gap:8px;align-items:center;margin:4px 0 0;font-size:11.5px;color:var(--faint)';
    bar.innerHTML = '<span class="if-zoominfo" hidden></span>' +
      '<button class="dp-btn" hidden data-drill-range onclick="ifRangeDrill()">区间洞察</button>' +
      '<button class="dp-btn" hidden onclick="ifResetZoom()">重置缩放</button>' +
      '<span>拖拽框选 · 双击重置 · 滚轮缩放</span>';
    box.parentNode.insertBefore(bar, box.nextSibling);
  }
}
function ifResetZoom(){ IFVIEW.win = null; ifRenderAll(); }
function ifRangeDrill(){
  if(!IFVIEW.win) return;
  var m = IFVIEW.charts[0]._meta, L = m.labels || [];
  var from = L[IFVIEW.win[0]], to = L[IFVIEW.win[1]];
  var metric = (m.series[0]||{}).name || '';
  var entity = (m.annotations && m.annotations[0] && m.annotations[0].label) || '区间';
  ifDrawerRange(from, to, metric, entity);
}
function ifDrawerRange(from, to, metric, entity){
  var d = document.getElementById('if-drawer');
  if(!d){ d = document.createElement('div'); d.id='if-drawer'; document.body.appendChild(d); }
  d.classList.add('on');
  d.innerHTML = '<h3>区间洞察</h3><div class="dr-sub">' + (from||'') + ' ~ ' + (to||'') + '</div><div class="empty">加载中…</div>';
  var ws = document.body.getAttribute('data-ws') || '';
  fetch('/api/v1/charts/range?workspace_id=' + encodeURIComponent(ws) +
        '&from=' + encodeURIComponent(String(from||'')) + '&to=' + encodeURIComponent(String(to||'')))
    .then(function(r){ return r.json(); })
    .then(function(dd){
      var html = '<h3>区间洞察</h3><div class="dr-sub">' + (from||'') + ' ~ ' + (to||'') +
                 ' · ' + (dd.insights||[]).length + ' 条洞察 · ' + (dd.actions||[]).length + ' 个动作</div>';
      html += '<div class="dr-sec">洞察</div>';
      if((dd.insights||[]).length){
        html += '<table class="dp-table"><thead><tr><th>时间</th><th>严重度</th><th>标题</th></tr></thead><tbody>';
        dd.insights.forEach(function(i){ html += '<tr><td>'+String(i.created_at).slice(5,10)+'</td><td><span class="badge b-'+i.severity+'">'+i.severity+'</span></td><td>'+i.title+'</td></tr>'; });
        html += '</tbody></table>';
      } else html += '<div class="empty">该区间无洞察</div>';
      html += '<div class="dr-sec">动作与验证</div>';
      if((dd.actions||[]).length){
        html += '<table class="dp-table"><thead><tr><th>状态</th><th>动作</th><th>验证结论</th></tr></thead><tbody>';
        dd.actions.forEach(function(a){ html += '<tr><td>'+a.state+'</td><td>'+a.action_type+'</td><td>'+(a.verdict||'—')+'</td></tr>'; });
        html += '</tbody></table>';
      } else html += '<div class="empty">该区间无动作</div>';
      html += '<div class="dr-sec"><button class="dp-btn" onclick="document.getElementById(\'if-drawer\').classList.remove(\'on\')">关闭</button></div>';
      d.innerHTML = html;
    });
}
/* 缩放/框选交互（作用于所有图表，联动） */
(function(){
  var drag = null;
  document.addEventListener('mousedown', function(e){
    var host = e.target.closest('svg[data-chart], canvas[data-chart]');
    if(!host) return;
    var wrap = host.closest('.dp-chart') || host.parentNode;
    var rect = wrap.getBoundingClientRect();
    drag = { x: e.clientX, rect: rect, host: host, shift: e.shiftKey, moved: false };
  });
  document.addEventListener('mousemove', function(e){
    if(!drag) return;
    if(Math.abs(e.clientX - drag.x) > 6) drag.moved = true;
  });
  document.addEventListener('mouseup', function(e){
    if(!drag) return;
    var d = drag; drag = null;
    if(!d.moved) return;
    var metaIsCanvas = d.host.tagName === 'CANVAS';
    var entry = IFVIEW.charts.filter(function(c){ return c._canvas === d.host || c._svg === d.host; })[0];
    if(!entry) return;
    var L = entry._meta.labels || [];
    var pad = entry._meta.plot || [44,14,668,180];
    var sx = d.rect.width / 720;
    var x0 = pad[0]*sx, pw = pad[2]*sx;
    var a = Math.min(e.clientX, d.x), b = Math.max(e.clientX, d.x);
    var ia = Math.round((a - d.rect.left - x0) / pw * (L.length-1));
    var ib = Math.round((b - d.rect.left - x0) / pw * (L.length-1));
    ia = Math.max(0, Math.min(L.length-1, ia)); ib = Math.max(0, Math.min(L.length-1, ib));
    if(ib - ia < 1){ ifResetZoom(); return; }
    IFVIEW.win = [ia, ib];
    ifRenderAll();
  });
  document.addEventListener('dblclick', function(e){
    if(e.target.closest('svg[data-chart], canvas[data-chart]')) ifResetZoom();
  });
  document.addEventListener('wheel', function(e){
    var host = e.target.closest('svg[data-chart], canvas[data-chart]');
    if(!host || !e.ctrlKey) return;
    e.preventDefault();
    var entry = IFVIEW.charts.filter(function(c){ return c._canvas===host || c._svg===host; })[0];
    if(!entry) return;
    var L = entry._meta.labels || [];
    var cur = IFVIEW.win || [0, L.length-1];
    var span = Math.max(4, cur[1] - cur[0]);
    var mid = Math.round((cur[0] + cur[1]) / 2);
    var next = Math.round(span * (e.deltaY > 0 ? 1.3 : 0.77));
    var half = Math.round(next / 2);
    IFVIEW.win = [Math.max(0, mid-half), Math.min(L.length-1, mid+half)];
    ifRenderAll();
  }, { passive: false });
  // a11y：键盘导航（聚焦图表后用方向键移动十字准线读数）
  document.addEventListener('keydown', function(e){
    if(!['ArrowLeft','ArrowRight','Escape'].includes(e.key)) return;
    var el = document.activeElement;
    if(!el || !el.classList || !el.classList.contains('pt')) return;
    if(e.key === 'Escape'){ el.blur(); return; }
    e.preventDefault();
    var pts = Array.prototype.slice.call(el.ownerSVGElement.querySelectorAll('.pt'));
    var idx = pts.indexOf(el) + (e.key === 'ArrowRight' ? 1 : -1);
    if(idx >= 0 && idx < pts.length) pts[idx].focus();
  });
  document.addEventListener('focusin', function(e){
    if(e.target.classList && e.target.classList.contains('pt')){
      var t = document.getElementById('if-tip');
      if(!t){ t = document.createElement('div'); t.id='if-tip'; document.body.appendChild(t); }
      t.innerHTML = e.target.getAttribute('aria-label') || '';
      var r = e.target.getBoundingClientRect();
      t.classList.add('on');
      t.style.left = Math.min(r.left, window.innerWidth-330) + 'px';
      t.style.top = Math.max(8, r.top - 40) + 'px';
    }
  });
  document.addEventListener('focusout', function(){ if(_tip) _tip.classList.remove('on'); });
  if (document.readyState !== 'loading') ifSetupCharts();
  else document.addEventListener('DOMContentLoaded', ifSetupCharts);
})();

/* 下钻抽屉：点击带 data-drill-entity 的元素 → 拉原始记录 + 关联洞察 */
function ifDrill(metric, entity){
  var d = document.getElementById('if-drawer');
  if(!d){
    d = document.createElement('div'); d.id = 'if-drawer'; document.body.appendChild(d);
    d.addEventListener('click', function(e){ if(e.target === d) d.classList.remove('on'); });
  }
  d.classList.add('on');
  d.innerHTML = '<h3>' + entity + '</h3><div class="dr-sub">加载中…</div>';
  var ws = document.body.getAttribute('data-ws') || '';
  fetch('/api/v1/charts/drill?workspace_id=' + encodeURIComponent(ws) +
        '&metric=' + encodeURIComponent(metric) + '&entity_id=' + encodeURIComponent(entity))
    .then(function(r){ return r.json(); })
    .then(function(dd){
      var rows = dd.rows || [], ins = dd.insights || [];
      var html = '<h3>' + entity + '</h3><div class="dr-sub">指标 ' + metric +
                 ' · ' + rows.length + ' 个数据点 · ' + ins.length + ' 条关联洞察</div>';
      html += '<div class="dr-sec">时间序列</div>';
      if(rows.length){
        html += '<table class="dp-table"><thead><tr><th>时间</th><th>值</th></tr></thead><tbody>';
        rows.slice(0, 40).forEach(function(r){
          html += '<tr><td>' + String(r.ts).slice(0,16).replace('T',' ') + '</td><td>' + r.value + '</td></tr>';
        });
        html += '</tbody></table>';
      } else { html += '<div class="empty">无数据</div>'; }
      html += '<div class="dr-sec">关联洞察</div>';
      if(ins.length){
        html += '<table class="dp-table"><thead><tr><th>严重度</th><th>标题</th></tr></thead><tbody>';
        ins.forEach(function(i){
          html += '<tr><td><span class="badge b-' + i.severity + '">' + i.severity + '</span></td><td>' + i.title + '</td></tr>';
        });
        html += '</tbody></table>';
      } else { html += '<div class="empty">无关联洞察</div>'; }
      html += '<div class="dr-sec"><button class="dp-btn" onclick="document.getElementById(\'if-drawer\').classList.remove(\'on\')">关闭</button></div>';
      d.innerHTML = html;
    })
    .catch(function(){ d.innerHTML = '<h3>' + entity + '</h3><div class="empty">加载失败</div>'; });
}
document.addEventListener('click', function(e){
  var el = e.target.closest('[data-drill-entity]'); if(!el) return;
  var svg = el.closest('[data-chart]');
  var metric = svg ? (el.closest('.dp') || {}).getAttribute?.('data-drill-metric') : null;
  metric = metric || el.getAttribute('data-drill-metric') ||
           (el.closest('.dp') && el.closest('.dp').getAttribute('data-drill-metric')) || '';
  ifDrill(metric, el.getAttribute('data-drill-entity'));
});

/* 自动刷新（性能守则·学 OpenFlow 心跳教训）：
   ① 延迟 60s 才启动（避免一进页面就打接口）
   ② 间隔 120s（不频繁）
   ③ 单次会话最多 20 次（防长挂页面无限刷）
   ④ 页面隐藏/不可见时暂停（后台标签页零负载）
   ⑤ 服务端结果有 90s TTL 缓存，刷新命中缓存几乎零成本 */
var __af = { timer: null, count: 0, MAX: 20, DELAY: 60000, INTERVAL: 120000 };
function toggleAutoRefresh(){
  var on = localStorage.getItem('if_auto') === '1';
  if (on) { localStorage.setItem('if_auto','0'); stopAutoRefresh(); toast('自动刷新已关闭'); }
  else { localStorage.setItem('if_auto','1'); scheduleAutoRefresh(); toast('自动刷新已开启（60s 后首刷）'); }
  paintAutoBtn();
}
function paintAutoBtn(){
  var b = document.getElementById('auto-btn'); if(!b) return;
  var on = localStorage.getItem('if_auto') === '1';
  b.style.color = on ? 'var(--accent)' : '';
}
function scheduleAutoRefresh(){
  stopAutoRefresh();
  __af.timer = setTimeout(function(){
    if (document.hidden) { scheduleAutoRefresh(); return; }   // 隐藏则顺延
    if (__af.count >= __af.MAX) { toast('已达自动刷新上限（20 次），手动刷新可继续'); return; }
    __af.count++;
    location.reload();
  }, __af.DELAY);
}
function stopAutoRefresh(){ if (__af.timer) { clearTimeout(__af.timer); __af.timer = null; } }
document.addEventListener('visibilitychange', function(){
  if (document.hidden) { stopAutoRefresh(); }
  else if (localStorage.getItem('if_auto') === '1') { scheduleAutoRefresh(); }
});
if (localStorage.getItem('if_auto') === '1') { scheduleAutoRefresh(); }
paintAutoBtn();
