/* Vixiq · Web 版前端逻辑 (原生 JS, 零依赖) */
'use strict';

// ============ 工具 ============
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
// 某个 Tab 页当前是不是显示着 (事件驱动刷新用: 没人看的页面不必去拉数据)
const isTabActive = (name) => {
  const p = $('#tab-' + name);
  return !!(p && p.classList.contains('active'));
};

// 切换顶部 Tab (search/tasks/reverse/library/trash/authors/stats/settings), 进入时刷新该页数据
window.switchTab = function (name) {
  $$('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  $$('.tabpage').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  actPush('NAV', '切换页面 → ' + name, {});   // 活动日志: 记下用户去哪一页了
  if (name === 'library') { libSync(); loadStats(); loadLibrary(); loadLibFacets(); loadTrashBadge(); wallWatch(); }
  else if (name === 'trash') loadTrash();
  else if (name === 'authors') loadAuthors();
  else if (name === 'stats') loadStatsPage();
  else if (name === 'reverse') loadReverseStatus();
  else if (name === 'tasks') refreshCart();
  else if (name === 'settings') { loadThumbsStats(); loadOperations(); paintSelfCheckCard(); loadActivityStats(); loadPixivActions(); loadLauncher(); loadWallDir(); loadWallLib(); loadAiText(); loadPaths(); }
};

// ============ 🖼 当前壁纸 (桌面幻灯片) 信息窗 —— 2026-09-24 ============
// 桌面幻灯片「现在播的是哪一张」Windows 写在注册表里 (HKCU\Control Panel\Desktop\WallPaper),
// 后端读得到 → 这里定时问一句: 轮播时看到中意的图, 当场就知道是谁画的、库内对应哪一页、
// 有没有更高清的一版、这张多余的壁纸要不要删。全程**只读**; 删壁纸必须用户亲手点, 且只进
// Windows 电脑回收站 (图库和数据库一律不碰)。
// 刷新讲究: 按「路径+修改时间」比对 —— 同一张不重画(否则 8 秒一次闪烁、点击被打断)。
let wallTimer = null, wallKey = '', wallImgUrl = '', wallData = null;

async function wallRefresh(opts) {
  const box = $('#wall-card');
  if (!box) return;
  const deep = !!(opts && opts.deep);
  let d = null;
  try { d = await api('/api/wallpaper/current' + (deep ? '?deep=1' : '')); }
  catch (e) { return; }                     // 轮询失败静默: 一个读注册表的接口不该把界面弄成报错页
  if (!d || !d.active) { box.classList.add('hidden'); wallKey = ''; return; }
  wallData = d;
  box.classList.remove('hidden');
  const key = d.file.path + '|' + d.file.mtime + '|' + (d.deep ? 1 : 0);
  if (key !== wallKey) { wallKey = key; renderWallCard(d); }
}

// 收起 / 展开 (用户嫌它占地方时就收成一行) —— 收起来了就记住, 下次开页面还是收着的
const WALL_FOLD_KEY = 'vixiq.wall.folded';

function wallApplyFold(folded) {
  const box = $('#wall-card');
  if (!box) return;
  box.classList.toggle('folded', !!folded);
  const b = $('#wall-fold');
  if (b) b.textContent = folded ? '展开' : '收起';
}

function wallFold() {
  const box = $('#wall-card');
  if (!box) return;
  const folded = !box.classList.contains('folded');
  try { localStorage.setItem(WALL_FOLD_KEY, folded ? '1' : '0'); } catch (e) {}
  wallApplyFold(folded);
}

try { wallApplyFold(localStorage.getItem(WALL_FOLD_KEY) === '1'); } catch (e) {}
window.wallFold = wallFold;

function wallWatch() {
  // 图库页开着才轮询, 离开就停 (没人看着的页面不该白跑请求)
  if (wallTimer) { clearInterval(wallTimer); wallTimer = null; }
  wallRefresh();
  wallTimer = setInterval(() => { if (isTabActive('library')) wallRefresh(); }, 8000);
}

// 溯源结论 → 一句话 + 徽章 (用户只关心: 这是谁画的 / 库里有没有 / 是不是重复的一份)
function wallState(d) {
  const m = d.match || {}, a = d.artwork;
  const relTxt = { same: '库内那张与它一模一样', rotate: '库内那张被转过 90°',
                   crop: '库内那张被裁过', rescale: '库内那张被缩放过' }[m.relation] || '';
  if (!a) {
    if (m.kind === 'weak')
      return { txt: '库内最接近的一张相似度 ' + (m.confidence * 100).toFixed(0) + '%，仅作疑似',
               badge: '<span class="wall-badge warn">疑似</span>' };
    return { txt: '库内未找到出处（文件名无作品 ID，内容指纹未命中）',
             badge: '<span class="wall-badge bad">未溯源</span>' };
  }
  const who = '#' + a.artwork_id + ' · ' + (a.author_name || '未知作者');
  const tail = relTxt ? '，' + relTxt : '';
  if (m.kind === 'library')
    return { txt: '壁纸文件就在图库里（' + who + '）',
             badge: '<span class="wall-badge ok">库内文件</span>' };
  if (m.kind === 'md5')
    return { txt: '库内已有字节完全相同的页（' + who + '）',
             badge: '<span class="wall-badge ok">内容重复</span>' };
  if (m.kind === 'id')
    return { txt: '按文件名中的作品 ID 定位（' + who + '）' + tail,
             badge: '<span class="wall-badge ok">按ID溯源</span>' };
  if (m.kind === 'orb')
    return { txt: '按图像特征匹配（' + who + '，相似度 ' + (m.confidence * 100).toFixed(0) + '%）' + tail,
             badge: '<span class="wall-badge ok">图像溯源</span>' };
  return { txt: who, badge: '' };
}

// 作品标题看着像**程序自己拼的**? (路径味 / 就是文件名 / 纯哈希或纯数字)
// 自定义作品与合并产物入库时标题取的是「来源夹 / 文件名」这类串, 一眼就是没做完 —— 这种不算真标题
function machineTitle(t, fname) {
  t = String(t || '').trim();
  if (!t) return true;
  if (/[/\\#]/.test(t)) return true;
  const stem = String(fname || '').replace(/\.[^.]+$/, '');
  if (stem && t === stem) return true;
  const head = t.split('_p')[0];
  return /^\d+$/.test(head) || (/^[0-9a-fA-F]{8,}$/.test(head));
}

// 壁纸卡标题: 真标题优先; 程序拼出来的标题不算真标题, 让 AI 那句顶上来
function wallTitle(a, d) {
  const f = (d && d.file) || {}, ai = (d && d.ai) || {};
  const t = a ? String(a.title || '').trim() : '';
  if (t && !machineTitle(t, f.name)) return t;
  if (String(ai.state || '') === 'ready' && String(ai.title || '').trim())
    return String(ai.title).trim();
  return a ? '（无标题）' : '未溯源';
}

function renderWallCard(d) {
  const f = d.file, m = d.match || {}, a = d.artwork, st = wallState(d);
  const r18 = a ? a.is_r18 : 0;
  const dim = (f.width && f.height) ? (f.width + '×' + f.height) : '尺寸未知';
  wallImgUrl = '/api/local-img?p=' + encodeURIComponent(f.name)
             + '&root=' + (f.in_library ? 'img' : 'wall');
  const big = (d.pages || []).filter(p => p.biggest)[0];
  // 库内最高清的那一页 —— 给的是「跟当前这张比谁更清楚」的结论, 不是让人去数像素
  const bigTxt = big
    ? '库内最高清的一页 ' + big.width + '×' + big.height + '（第 ' + (big.page_num + 1) + ' 页）· '
      + (d.bigger ? '比当前壁纸更清晰' : '清晰度不及当前壁纸')
    : '';
  $('#wall-name').textContent = f.name;
  $('#wall-badges').innerHTML = st.badge;
  $('#wall-body').innerHTML =
    '<img class="wall-thumb" src="' + imgSrc(f.thumb, { is_r18: r18 }) + '" alt=""'
      + ' title="点开看大图（可左右看库内其它页）" onclick="wallPreview()">'
    + '<div class="wall-info">'
    +   '<div class="wall-line"><b>' + esc(wallTitle(a, d)) + '</b>'
    +     (a ? ' <span class="muted">#' + esc(a.artwork_id) + ' · ' + esc(a.author_name || '') + '</span>' : '')
    +   '</div>'
    +   '<div class="wall-line muted">壁纸 ' + esc(dim) + ' · ' + esc(fmtSize(f.size)) + ' · ' + esc(f.dir) + '</div>'
    +   '<div class="wall-line">' + esc(st.txt) + '</div>'
    +   (bigTxt ? '<div class="wall-line muted">' + esc(bigTxt) + '</div>' : '')
    +   (d.deep_error ? '<div class="wall-line err-text">图像溯源失败：' + esc(d.deep_error) + '</div>' : '')
    +   '<div class="btn-row" style="margin-top:6px">'
    +     (a && a.author_id ? '<button class="btn" onclick="openAuthorDetail(\'' + esc(a.author_id) + '\')">👤 作者</button>' : '')
    +     (a ? '<button class="btn" onclick="libOnlyThisWork(\'' + esc(a.artwork_id) + '\')">📚 在库中查看这个作品</button>' : '')
    +     '<button class="btn" onclick="wallAct(\'next\')">⏭️ 下一张壁纸</button>'
    +     (a ? '<button class="btn" onclick="wallCopyLink(\'' + esc(a.artwork_id) + '\')">🔗 复制作品链接</button>' : '')
    +     '<button class="btn ghost" onclick="wallAct(\'open-folder\')">📂 打开所在文件夹</button>'
    +     (f.in_library ? '' : '<button class="btn danger" onclick="wallAct(\'recycle\')">🗑️ 移出壁纸（进电脑回收站）</button>')
    +     ((m.kind === 'none' || m.kind === 'weak')
           ? '<button class="btn ghost" onclick="wallRefresh({deep:1})"'
             + ' title="用 ORB 图像特征在库里找最像的一张（首次要加载整库特征，可能较慢）">🔍 图像溯源</button>' : '')
    +   '</div>'
    + '</div>';
}

// 点缩略图看大图: 复用图库的 Lightbox (库内页一起给它, 就能左右翻页对比清晰度)
function wallPreview() {
  const d = wallData || null, f = d && d.file;
  if (!wallImgUrl || !f) return;
  const a = d.artwork;
  const r18 = a ? a.is_r18 : 0;
  const wp = { page_num: 0, url: wallImgUrl, is_r18: r18, width: f.width, height: f.height,
               title: (a ? a.title : '') || f.name, author_name: a ? a.author_name : '',
               artwork_id: a ? a.artwork_id : '' };
  const pages = [wp].concat((a && d.pages) ? d.pages.map(p => Object.assign({}, p, { is_r18: r18 })) : []);
  window.openLightbox(wp, pages, null);
}

async function wallAct(what) {
  if (what === 'open-folder') {
    const r = await api('/api/wallpaper/open-folder', { method: 'POST' });
    if (apiErr(r)) toast(apiErr(r), 'err');
    return;
  }
  if (what === 'next') {
    // 只是「跳到壁纸夹里的下一张」看看, 幻灯片的间隔/乱序/目录都不动
    const r = await api('/api/wallpaper/next', { method: 'POST' });
    const e = apiErr(r);
    if (e) { toast(e, 'err'); return; }
    toast('已切到下一张：' + (r.name || ''), 'ok');
    wallKey = ''; wallRefresh();
    return;
  }
  if (what === 'recycle') {
    if (!confirm('把当前这张壁纸送进 Windows 电脑回收站？\n\n'
      + '· 只删壁纸夹里的这一个文件，图库和数据库都不动\n'
      + '· 回收站里随时可以还原\n'
      + '· 桌面会先继续显示已缓存的那张，下一次轮播就会换掉')) return;
    const r = await api('/api/wallpaper/recycle', { method: 'POST' });
    const e = apiErr(r);
    if (e) { toast(e, 'err'); return; }
    toast('已把这张壁纸送进电脑回收站（可还原）', 'ok');
    wallKey = ''; wallRefresh();
  }
}
// 复制当前壁纸对应作品的 pixiv 链接 (小控件那颗「复制作品链接」同口径)
async function wallCopyLink(aid) {
  const url = 'https://www.pixiv.net/artworks/' + aid;
  try {
    await navigator.clipboard.writeText(url);
    toast('已复制：' + url, 'ok');
  } catch (e) {
    window.prompt('复制失败，请手动复制：', url);
  }
}
// ============ 🧹 壁纸副本清理 (「省空间」) —— 2026-09-24 ============
// 壁纸夹里有 784 张和图库某页**字节一致**的副本 (约 5.37 GB): 删掉壁纸夹里那份不丢任何东西。
// 设计约定的两条硬规矩就落在这里 —— ① 绝不自动删, 逐条勾选; ② 只走 Windows 电脑回收站, 可还原。
let wallDupesTimer = null, wallDupesSel = new Set(), wallDupesLast = null;

async function wallDupesOpen() {
  wallDupesSel = new Set();
  openLibModal('🧹 壁纸副本清理', '<div class="hint">正在读扫描结果…</div>', { lg: true });
  await wallDupesTick();
}

function wallDupesClose() {
  if (wallDupesTimer) { clearInterval(wallDupesTimer); wallDupesTimer = null; }
  closeLibModal();
}

async function wallDupesScan() {
  const r = await api('/api/wallpaper/dupes/scan', { method: 'POST' });
  const e = apiErr(r);
  if (e) { toast(e, 'err'); return; }
  wallDupesTick();
}

async function wallDupesTick() {
  if ($('#lib-modal').style.display === 'none') {   // 面板被关了 (含右上角 ✕) 就停轮询
    if (wallDupesTimer) { clearInterval(wallDupesTimer); wallDupesTimer = null; }
    return;
  }
  let d = null;
  try { d = await api('/api/wallpaper/dupes'); } catch (e) { return; }
  wallDupesLast = d;
  const scanning = d.status === 'scanning';
  if (!scanning && wallDupesTimer) { clearInterval(wallDupesTimer); wallDupesTimer = null; }
  if (scanning && !wallDupesTimer) wallDupesTimer = setInterval(wallDupesTick, 1000);
  wallDupesPaint(d);
}

function wallDupesPaint(d) {
  const body = $('#lib-modal-body');
  if (!body) return;
  const res = d.result || null;
  let head = '';
  if (d.status === 'scanning') {
    const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
    head = '<div class="hint">扫描中 ' + pct + '%（' + d.done + '/' + d.total + '）'
         + (d.current ? ' · ' + esc(d.current) : '') + '</div>';
  } else if (d.status === 'error') {
    head = '<div class="hint err-text">扫描失败：' + esc(d.error || '') + '</div>';
  } else if (!res) {
    head = '<div class="hint">还没扫过。'
         + '<button class="btn accent" onclick="wallDupesScan()">扫描副本</button></div>';
    body.innerHTML = head;
    return;
  } else if (!res.count) {
    head = '<div class="hint">扫了 ' + res.scanned + ' 张，没有发现与图库字节一致的副本 ✓ '
         + '<button class="btn ghost" onclick="wallDupesScan()">重扫</button></div>';
    body.innerHTML = head;
    return;
  } else {
    head = '<div class="hint">扫了 ' + res.scanned + ' 张，其中 <b>' + res.count + '</b> 张与图库里的文件'
         + '字节一致，共占 <b>' + fmtSize(res.bytes) + '</b>。删掉壁纸夹里这份不会丢图（库内那份还在）。'
         + '<button class="btn ghost" onclick="wallDupesScan()">重扫</button></div>';
  }
  const rows = (res.items || []).map(it => {
    const who = it.artwork_id ? ('#' + esc(it.artwork_id) + ' · ' + esc(it.author_name || '')) : '';
    const cur = it.current ? ' <span class="wall-badge ok">正显示</span>' : '';
    return '<tr>'
      + '<td><input type="checkbox" ' + (wallDupesSel.has(it.name) ? 'checked' : '')
      + ' onchange="wallDupesPick(this.checked, \'' + esc(it.name).replace(/'/g, "\\'") + '\')"></td>'
      + '<td class="dupe-name" title="' + esc(it.name) + '">' + esc(it.name) + cur + '</td>'
      + '<td>' + esc(fmtSize(it.size)) + '</td>'
      + '<td>' + who + '<div class="muted dupe-title">' + esc(it.title || '') + '</div></td>'
      + '</tr>';
  }).join('');
  const selBytes = (res.items || []).filter(it => wallDupesSel.has(it.name))
    .reduce((s, it) => s + (it.size || 0), 0);
  body.innerHTML = head
    + '<div style="max-height:52vh;overflow:auto"><table class="dupe-table">'
    + '<thead><tr><th style="width:34px"><input type="checkbox" onchange="wallDupesPickAll(this.checked)"></th>'
    + '<th>壁纸文件</th><th>大小</th><th>库内对应的那一页</th></tr></thead>'
    + '<tbody>' + rows + '</tbody></table></div>'
    + '<div class="modal-actions">'
    +   '<span class="muted">已勾选 ' + wallDupesSel.size + ' 项 · ' + fmtSize(selBytes) + '</span>'
    +   '<span class="lib-spacer"></span>'
    +   '<button class="btn ghost" onclick="wallDupesClose()">关闭</button>'
    +   '<button class="btn danger" onclick="wallDupesDelete()">🗑️ 移出勾选的（进回收站）</button>'
    + '</div>';
}

// 勾选态只改这一行文字, 不整体重画 (重画会把滚动位置和鼠标焦点一起弄丢)
function wallDupesCount() {
  const items = ((wallDupesLast || {}).result || {}).items || [];
  const bytes = items.filter(it => wallDupesSel.has(it.name))
    .reduce((s, it) => s + (it.size || 0), 0);
  $$('#lib-modal-body .modal-actions .muted')
    .forEach(x => x.textContent = '已勾选 ' + wallDupesSel.size + ' 项 · ' + fmtSize(bytes));
}

function wallDupesPick(checked, name) {
  if (checked) wallDupesSel.add(name); else wallDupesSel.delete(name);
  wallDupesCount();
}

function wallDupesPickAll(checked) {
  const res = (wallDupesLast || {}).result || null;
  wallDupesSel = new Set(checked ? ((res || {}).items || []).map(it => it.name) : []);
  $$('#lib-modal-body tbody input[type=checkbox]').forEach(b => b.checked = checked);
  wallDupesCount();
}

async function wallDupesDelete() {
  if (!wallDupesSel.size) { toast('先勾选要移出的副本', 'err'); return; }
  if (!confirm('把勾选的 ' + wallDupesSel.size + ' 个壁纸副本送进 Windows 电脑回收站？\n\n'
    + '· 这些文件和图库里那份字节完全一致，删掉壁纸夹里这份不丢图\n'
    + '· 只动壁纸夹，图库和数据库都不碰\n'
    + '· 回收站里随时可以还原')) return;
  const r = await api('/api/wallpaper/dupes/recycle',
    { method: 'POST', body: JSON.stringify({ names: Array.from(wallDupesSel) }) });
  const e = apiErr(r);
  if (e) { toast(e, 'err'); return; }
  toast('已移出 ' + r.recycled + ' 个副本，释放 ' + fmtSize(r.freed || 0), 'ok');
  wallDupesSel = new Set();
  wallDupesTick();
}

window.wallRefresh = wallRefresh;
window.wallPreview = wallPreview;
window.wallAct = wallAct;
window.wallCopyLink = wallCopyLink;
window.wallDupesOpen = wallDupesOpen;
window.wallDupesClose = wallDupesClose;
window.wallDupesScan = wallDupesScan;
window.wallDupesPick = wallDupesPick;
window.wallDupesPickAll = wallDupesPickAll;
window.wallDupesDelete = wallDupesDelete;

// ============ 📊 统计页 ============
const SOURCE_COLORS = { pixiv: '#26a69a', danbooru: '#8e44ad', twitter: '#1da1f2', bilibili: '#fb7299', ggac: '#e67e22', other: '#95a5a6' };
const STATUS_LABELS = { alive: '活跃中', gone: '已注销', unknown: '未知' };

function statsBars(data, { key, countKey, colors, labels }) {
  const total = data.reduce((s, d) => s + (d[countKey] || 0), 0) || 1;
  return `<div class="bars-list">` + data.map(d => {
    const v = d[countKey] || 0;
    const pct = (v / total * 100).toFixed(1);
    const k = String(d[key]);
    const c = (colors && colors[k]) || '#95a5a6';
    const lb = (labels && labels[k]) || k;
    return `<div class="bar-row">
      <span class="bar-label">${esc(lb)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${c}"></div></div>
      <span class="bar-num">${v}（${pct}%）</span>
    </div>`;
  }).join('') + `</div>`;
}

function authorStatusTag(st) {
  const s = st || 'unknown';
  const cls = s === 'alive' ? 'alive' : (s === 'gone' ? 'gone' : '');
  const tip = s === 'alive'
    ? 'pixiv 账号存在 → 活跃中（作品来源可能来自 bilibili/X/GGAC 等其他站点，但作者本体是 pixiv 画师）'
    : (s === 'gone' ? 'pixiv 账号已注销/不存在 → 已注销' : '未联网核对过 pixiv 账号；非 pixiv 作者（无 pixiv 账号）恒为未知');
  return `<span class="tag auth-status ${cls}" title="${tip}">${STATUS_LABELS[s] || s}</span>`;
}

async function loadStatsPage() {
  const cards = $('#stats-cards');
  try {
    const [s, a] = await Promise.all([
      api('/api/library/stats'),
      api('/api/library/breakdown'),
    ]);
    cards.innerHTML = `
      <div class="stat-card"><div class="num">${s.total_artworks ?? 0}</div><div class="lbl">作品</div></div>
      <div class="stat-card"><div class="num">${s.total_pages ?? 0}</div><div class="lbl">图片页</div></div>
      <div class="stat-card"><div class="num">${s.total_authors ?? 0}</div><div class="lbl">画师</div></div>
      <div class="stat-card"><div class="num">${fmtSize(s.disk_size_bytes)}</div><div class="lbl">磁盘占用</div></div>`;
    $('#stats-source').innerHTML = statsBars(a.source_dist || [], {
      key: 'source', countKey: 'count', colors: SOURCE_COLORS,
      labels: SOURCE_LABELS,
    });
    $('#stats-status').innerHTML = statsBars(a.status_dist || [], {
      key: 'status', countKey: 'count', labels: STATUS_LABELS,
      colors: { alive: '#2ecc71', gone: '#95a5a6', unknown: '#f39c12' },
    });
    $('#stats-type').innerHTML = statsBars(a.type_dist || [], {
      key: 'type', countKey: 'count',
      labels: { 0: '插画', 1: '漫画', 2: '动图' },
      colors: { 0: '#3498db', 1: '#9b59b6', 2: '#e74c3c' },
    });
    $('#stats-r18').innerHTML = statsBars(a.r18_dist || [], {
      key: 'r18', countKey: 'count',
      labels: { 0: '非 R18', 1: 'R18' },
      colors: { 0: '#2ecc71', 1: '#e74c3c' },
    });
    $('#stats-author-works').innerHTML = statsBars(a.author_works_dist || [], {
      key: 'label', countKey: 'count',
      labels: { '1 张': '1 张', '2-5 张': '2-5 张', '6-20 张': '6-20 张', '21-100 张': '21-100 张', '>100 张': '>100 张' },
      colors: { '1 张': '#3498db', '2-5 张': '#2ecc71', '6-20 张': '#9b59b6', '21-100 张': '#e67e22', '>100 张': '#e74c3c' },
    });
    $('#stats-tags').innerHTML = statsBars(a.tag_dist || [], {
      key: 'tag', countKey: 'count',
    });
    $('#stats-refresh-line').textContent = `共 ${s.total_authors ?? 0} 位有作品的画师`;
  } catch (e) {
    cards.innerHTML = '<div class="stat-card"><div class="lbl">统计读取失败</div></div>';
    $('#stats-refresh-line').textContent = '';
  }
}

function toast(msg, type = '') {
  let t = $('.toast');
  if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.className = 'toast', 2600);
}

// ============ 👤 作者管理页 ============
const AUTHORS_PAGE_SIZE = 50;
let authorsPage = 1, authorsPages = 1, authorsTotal = 0, authorsSort = 'count', authorsOrder = 'desc';

async function loadAuthors(opts = {}) {
  if (opts.resetPage !== false) authorsPage = 1;
  const q = $('#authors-search-input').value.trim();
  const status = $('#authors-f-status').value;
  const source = $('#authors-f-source').value;
  const cont = $('#authors-result');
  // 静默刷新: 已有表格内容时不清空(避免闪烁/跳滚动), 只在首次/空容器时显示「加载中」
  const hasList = !!(cont.querySelector('.table, .cards, .card-grid, .lib-list'));
  const silent = hasList && !opts.forceLoading;
  const prevScroll = window.scrollY;
  if (!silent) cont.innerHTML = '<div class="hint">加载中…</div>';
  const p = new URLSearchParams();
  p.set('q', q); p.set('status', status || ''); p.set('source', source || '');
  p.set('sort', authorsSort); p.set('order', authorsOrder);
  p.set('limit', AUTHORS_PAGE_SIZE); p.set('offset', (authorsPage - 1) * AUTHORS_PAGE_SIZE);
  try {
    const r = await api('/api/library/authors?' + p.toString());
    const items = r.items || [];
    authorsTotal = r.total ?? 0;
    authorsPages = Math.max(1, Math.ceil(authorsTotal / AUTHORS_PAGE_SIZE));
    if (authorsPage > authorsPages) authorsPage = authorsPages;
    const pos = authorsTotal > AUTHORS_PAGE_SIZE ? ` · 第 ${authorsPage}/${authorsPages} 页` : '';
    $('#authors-total').textContent = `共 ${authorsTotal} 位`;
    $('#authors-total-inline').textContent = `共 ${authorsTotal} 位${pos}`;
    renderAuthorsPagers();
    renderAuthors(items);
    if (silent) window.scrollTo(0, prevScroll);
  } catch (e) {
    if (!silent) cont.innerHTML = '<div class="hint">加载失败。</div>';
    else toast('刷新失败: ' + e.message, 'err');
  }
}

function renderAuthorsPagers() {
  const html = authorsTotal > AUTHORS_PAGE_SIZE ? `
    <div class="pager-inner">
      <button class="btn ghost" onclick="goAuthorsPage(1)" ${authorsPage <= 1 ? 'disabled' : ''} title="首页">«</button>
      <button class="btn ghost" onclick="goAuthorsPage(${authorsPage - 1})" ${authorsPage <= 1 ? 'disabled' : ''} title="上一页">‹</button>
      <span class="pager-info">第
        <input class="lib-page-input" type="number" min="1" max="${authorsPages}" value="${authorsPage}"
               onkeydown="if(event.key==='Enter')goAuthorsPageFromInput(this)"> / ${authorsPages} 页
      </span>
      <button class="btn ghost" onclick="goAuthorsPage(${authorsPage + 1})" ${authorsPage >= authorsPages ? 'disabled' : ''} title="下一页">›</button>
      <button class="btn ghost" onclick="goAuthorsPage(${authorsPages})" ${authorsPage >= authorsPages ? 'disabled' : ''} title="尾页">»</button>
    </div>` : '';
  $('#authors-pager-top').innerHTML = html;
  $('#authors-pager-bottom').innerHTML = html;
}

function goAuthorsPage(n) {
  n = Math.min(Math.max(1, n | 0), authorsPages || 1);
  if (n === authorsPage) return;
  authorsPage = n;
  loadAuthors({ resetPage: false });
  $('#authors-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function goAuthorsPageFromInput(input) {
  const n = parseInt(input.value, 10);
  if (Number.isFinite(n)) goAuthorsPage(n);
  else input.value = authorsPage;
}

function sortAuthors(sort) {
  if (authorsSort === sort) authorsOrder = authorsOrder === 'asc' ? 'desc' : 'asc';
  else { authorsSort = sort; authorsOrder = 'desc'; }
  loadAuthors();
}

function authorsSortArrow(key) {
  if (authorsSort !== key) return '';
  return authorsOrder === 'asc' ? ' ↑' : ' ↓';
}

function renderAuthors(items) {
  const rows = items.map(x => {
    const aliases = (x.aliases || []).length ? `<div class="c-alias">曾用名：${x.aliases.map(esc).join('、')}</div>` : '';
    // 来源列 = 名下作品的来源 ∪ 作者主页链接的站点。
    // 链接带来的站点加 * 并用虚线框标出, 与"真的有这个来源的作品"区分开。
    const workSrc = (x.sources || []);
    const linkSites = [...new Set((x.urls || []).map(u => u.site).filter(Boolean))];
    const onlyLink = linkSites.filter(s => !workSrc.includes(s));
    const srcBadges = workSrc.map(s => `<span class="tag src-tag">${esc(sourceLabel(s))}</span>`).join(' ')
      + onlyLink.map(s => `<span class="tag src-tag link-src" title="来自「编辑作者」里记的主页链接">${esc(sourceLabel(s))}*</span>`).join(' ');
    const lastUpd = x.last_updated ? x.last_updated.replace('T', ' ').slice(0, 19) : '—';
    // 作者名点击 = 查看该作者的图片 
    const disp = esc(x.name || x.author_id);
    const aidEsc = esc(x.author_id);
    const nameCell = `<span class="w-author" data-name="${disp}" title="点击：在图库中只看该作者的作品"` +
      ` onclick="libFilterByAuthor('${aidEsc}', this)">${disp}</span>`;
    // 同名 ≠ 同一人 : 本地作者(站点账号, local_xxx)
    // 与真 pixiv 作者同名时会各占一行 —— 这是对的, 但要让用户看得见并给「一键绑定」的出路。
    const same = x.same_name_pixiv;
    const sameWarn = same ? `<div class="c-warn">⚠ 与 pixiv 作者《${esc(same.name)}》同名（${same.count ?? 0} 作品）` +
      `<button class="btn ghost" onclick="bindAuthorPixiv('${aidEsc}','${esc(same.author_id)}')"` +
      ` title="本地作者与 pixiv 作者是两种身份，同名不代表同一人；确认是同一人可一键绑定">🔗 若为同一人，一键绑定</button></div>` : '';

    const home = authorHomeUrl(x);
    const homeBtn = home
      ? `<a class="btn ghost icon-btn" href="${esc(home)}" target="_blank" rel="noopener" title="打开作者主页">🌐</a>`
      : '';
    // 关注按钮 : 只有真 pixiv 作者(纯数字 ID)能关注, 本地作者(站点账号)没有可关注的账号
    const isPixivAuthor = /^\d+$/.test(String(x.author_id));
    const followBtn = isPixivAuthor
      ? `<button class="btn ghost icon-btn" data-follow-aid="${aidEsc}" data-follow-icon="1"` +
        ` onclick="toggleFollowAuthor('${aidEsc}', this)"` +
        ` title="关注这位画师（pixiv 账号；已关注时点击可取消）">♡</button>`
      : '';
    return `<tr>
      <td>${nameCell}${aliases}${sameWarn}</td>
      <td class="mono">${esc(x.author_id)}</td>
      <td class="num">${x.count ?? 0}</td>
      <td>${authorStatusTag(x.status)}</td>
      <td>${srcBadges || '—'}</td>
      <td class="cell-path none" style="white-space:nowrap">${esc(lastUpd)}</td>
      <td>${followBtn}<button class="btn ghost icon-btn" data-name="${disp}" onclick="libFilterByAuthor('${aidEsc}', this)" title="查看我库里该作者的图片（切到图库并按该作者筛选）">📚</button><button class="btn ghost icon-btn" onclick="openReconcile('${aidEsc}')" title="🗂️ 作者对账台：把 TA 在 pixiv 主页的作品与「我库里来源未定的图」并排对照，点一下就认领转正（不需要登录也能看本地清单）">🗂️</button><button class="btn ghost icon-btn" onclick="refreshAuthor('${aidEsc}')" title="🔄 只刷新这一个作者：联网核对现用名与存续状态（轻量定向，不用跑全量自检）">🔄</button>${homeBtn}<button class="btn ghost icon-btn" onclick="showAuthorDetail('${aidEsc}')" title="作者详情（主页链接/曾用名）">📇</button><button class="btn ghost icon-btn" onclick="editAuthor('${aidEsc}')" title="编辑作者">✏️</button></td>
    </tr>`;
  }).join('');
  $('#authors-result').innerHTML = `<table class="table lib-list">
    <thead><tr>
      <th class="sortable" onclick="sortAuthors('name')">画师（曾用名）${authorsSortArrow('name')}</th>
      <th class="sortable" onclick="sortAuthors('id')">ID${authorsSortArrow('id')}</th>
      <th class="sortable" onclick="sortAuthors('count')">作品数${authorsSortArrow('count')}</th>
      <th title="状态按 pixiv 账号存续判定：作者有 pixiv 账号则看该账号是否注销；非 pixiv 作者（无 pixiv 账号）恒为未知">状态</th><th title="来源 = 名下作品的来源 ∪ 作者管理里登记的主页链接站点；带 * 的是仅来自主页链接（名下作品暂无该来源）">来源</th>
      <th class="sortable" onclick="sortAuthors('last_updated')">最后更新${authorsSortArrow('last_updated')}</th>
      <th>操作</th>
    </tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// 作者主页链接 (danbooru 式): 优先用**你记的第一条来源链接**, 其次 pixiv 官方主页。
// (链接是多条的, 顺序即优先级 —— 想让它打开哪个站就把它放第一条)
function authorHomeUrl(x) {
  const first = ((x && x.urls) || []).find(u => u && u.url);
  if (first) return first.url;
  if (x && x.pixiv_url) return x.pixiv_url;
  if (x && /^\d+$/.test(String(x.author_id))) return 'https://www.pixiv.net/users/' + x.author_id;
  return '';
}

// 作者详情弹窗: 现用名 + 所有曾用名 + 各主页链接 (对齐 danbooru 的作者卡片)
async function showAuthorDetail(authorId) {
  let a = null;
  try {
    const r = await api('/api/library/authors?q=' + encodeURIComponent(authorId) + '&limit=20');
    a = (r.items || []).find(x => String(x.author_id) === String(authorId));
  } catch (e) {}
  if (!a) { toast('未找到作者', 'err'); return; }
  const isPixiv = /^\d+$/.test(String(a.author_id));
  // 来源链接 (可多条): 站点标签 + @账号, 可点开; 顺序即优先级 (🌐 按钮用第一条)
  const urlList = (a.urls || []);
  const siteLabelOf = (u) => u.label || sourceLabel(u.site) || u.site || '链接';
  const links = urlList.map(u =>
    `<a href="${esc(u.url)}" target="_blank" rel="noopener">${esc(siteLabelOf(u))}${u.handle ? ' @' + esc(u.handle) : ''}</a>`);
  if (!urlList.length) {
    // 兼容老数据: 只有 pixiv_url / twitter 两列时也照常显示
    if (isPixiv) links.push(`<a href="https://www.pixiv.net/users/${esc(a.author_id)}" target="_blank" rel="noopener">pixiv 主页</a>`);
    if (a.pixiv_url) links.push(`<a href="${esc(a.pixiv_url.startsWith('http') ? a.pixiv_url : 'https://' + a.pixiv_url)}" target="_blank" rel="noopener">主页</a>`);
    if (a.twitter) {
      const tw = a.twitter.trim().replace(/^@/, '');
      links.push(`<a href="${/^https?:/.test(a.twitter) ? a.twitter : 'https://x.com/' + esc(tw)}" target="_blank" rel="noopener">X（推特）</a>`);
    }
  }
  // 记了 pixiv 主页链接 (users/数字) 且现在还是「本地作者」→ 提供一键绑定成真 pixiv 作者
  const pixivLink = urlList.find(u => u.site === 'pixiv' && /^\d+$/.test(String(u.handle || '')));
  const bindBtn = (!isPixiv && pixivLink)
    ? `<button class="btn accent" onclick="bindAuthorPixiv('${esc(a.author_id)}','${esc(pixivLink.handle)}')"
         title="把该作者的全部作品迁到 pixiv 作者名下（作者行合并、磁盘文件夹一起改名）">🔗 绑定为 pixiv 作者（ID ${esc(pixivLink.handle)}）</button>`
    : '';
  const aliases = (a.aliases || []).length ? `<span>${a.aliases.map(esc).join('、')}</span>` : '<span class="muted">—</span>';
  openLibModal(`作者详情 · ${esc(a.name || authorId)}`, `
    <div class="lib-edit-form">
      <label>作者名 <b>${esc(a.name || a.author_id)}</b></label>
      <label>ID <span class="mono">${esc(a.author_id)}</span></label>
      <label>曾用名 ${aliases}</label>
      <label>状态 ${authorStatusTag(a.status)}</label>
      <label>作品数 ${a.count ?? 0}</label>
      <label>来源链接 <span class="auth-links">${links.length ? links.join(' · ') : '<span class="muted">无（可在「✏️ 编辑作者」里添加，支持多条）</span>'}</span></label>
      <div class="btn-row">
        <button class="btn accent" data-name="${esc(a.name || a.author_id)}"
          onclick="closeLibModal(); libFilterByAuthor('${esc(a.author_id)}', this)">📚 查看我库里该作者的图片（${a.count ?? 0}）</button>
        ${bindBtn}
        <button class="btn ghost" onclick="closeLibModal(); openReconcile('${esc(a.author_id)}')"
          title="把 TA 在 pixiv 主页的作品与「我库里来源未定的图」并排对照，点一下就认领转正">🗂️ 作者对账台</button>
        <button class="btn ghost" onclick="closeLibModal()">关闭</button>
      </div>
    </div>`, { lg: true });
}

// 🔄 定向刷新**单个作者** (轻量自检): 只联网核对这一个人的现用名与存续状态。
// 用户 2026-09-17: 编辑完作者还要等全量自检才更新状态 —— 不合理; 这里 1 次 HTTP 就够。
window.refreshAuthor = async function (authorId, opts) {
  const silent = !!(opts && opts.silent);
  if (!/^\d+$/.test(String(authorId))) {
    if (!silent) toast('本地作者没有 pixiv 账号可核对（可记上 pixiv 主页链接后绑定）', 'err');
    return false;
  }
  try {
    const r = await api('/api/library/author-refresh', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ author_id: String(authorId) }) });
    const err = apiErr(r);
    if (err) throw new Error(err);
    if (!silent) {
      toast(`🔄 已刷新「${r.name || authorId}」：`
        + (r.author_status === 'alive' ? '活跃中' : '已注销')
        + (r.renamed ? '（已同步新名字）' : '')
        + (r.folder_renamed ? '，磁盘文件夹已改名' : ''), 'ok');
    }
    loadAuthors({ resetPage: false }); loadLibFacets();
    setLastAction('author', [authorId], '刚刷新的作者');
    return true;
  } catch (e) {
    if (!silent) toast('刷新失败: ' + e.message, 'err');
    return false;
  }
};

// ===== 🗂️ 作者对账台  =====
//   场景: 一批外来源图挂着本地 ID (ext_xxx)、作者已指定成 pixiv 画师, 但作品不是正式 pixiv 卡片。
//   全站以图搜图太慢、逐张翻 pixiv 太贵 → 把候选集限定在**这个作者**:
//   左列"我库里来源未定的图", 右列"TA 在 pixiv 主页的作品", 点一下就认领转正。
//   多选(用户 2026-09-17 追加, 两种都要):
//     ① 合并认领: 左选 N 张 + 右点 1 个 → 依次挂到该作品的连续页
//        (适用于"一组散图其实来自同一个 pixiv 多页作品")
//     ② 顺序配对: 开「⇄ 顺序配对」后左右各选 N 个 → 按**点击顺序** 1:1 配对
//        (适用于"一批散图各属于不同作品"; 顺序错了会张冠李戴, 所以选中项带 ①②③ 角标, 顺序看得见)
//   点选只切换选中态(不重建 DOM), 滚动条不会跳回顶部。
let _rcState = { authorId: '', locals: [], pixiv: [], selLocals: [], selPixivs: [],
                 pairMode: false, pairPage: 0, last: null, lastResult: null };

const _rcIndex = (arr, id) => arr.indexOf(String(id));

window.openReconcile = async function (authorId) {
  const aid = String(authorId || '').trim();
  if (!aid) return;
  _rcState = { authorId: aid, locals: [], pixiv: [], selLocals: [], selPixivs: [],
               pairMode: false, pairPage: 0, last: null, lastResult: null };
  openLibModal('🗂️ 作者对账台',
    '<div id="rc-body" class="hint">正在拉取作者主页作品…（首次约几秒）</div>', { lg: true });
  await reloadReconcile();
};

window.reloadReconcile = async function () {
  const box = $('#rc-body');
  if (!box) return;
  box.innerHTML = '<div class="hint">正在拉取作者主页作品…（HTTP 直连，不开浏览器）</div>';
  let r;
  try {
    r = await api('/api/library/author-reconcile', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ author_id: _rcState.authorId }) });
  } catch (e) {
    box.innerHTML = `<div class="err-text">加载失败：${esc(e.message)}</div>`; return;
  }
  const err = apiErr(r);
  if (err) { box.innerHTML = `<div class="err-text">${esc(err)}</div>`; return; }
  _rcState.locals = r.locals || [];
  _rcState.pixiv = r.pixiv || [];
  _rcState.last = r;
  paintReconcile();
};

// 弹窗是整块 innerHTML 重画的 → 重建 DOM 会让两个网格的滚动条归零。
// (2026-09-17): 只是点选一张图, 左右两边都跳回顶部。两手处理:
//   ① 点选只切换 .on 类, 根本不重建 DOM  → paintReconcileSelection();
//   ② 真要整块重画时(重新拉取/认领后), 按"第几个网格"记住并还原滚动位置。
function _rcGrids(box) { return Array.from(box.querySelectorAll('.rc-grid')); }
function _rcScrollSnapshot(box) { return _rcGrids(box).map(g => g.scrollTop); }
function _rcRestoreScroll(box, tops) {
  _rcGrids(box).forEach((g, i) => { if (tops[i] != null) g.scrollTop = tops[i]; });
}

// 只改选中态与底部操作条 (不碰网格 DOM) —— 点选时用它, 滚动条纹丝不动
function paintReconcileSelection() {
  const box = $('#rc-body');
  if (!box) return;
  // 序号角标 = 认领顺序 (顺序配对靠它核对, 不靠记忆)
  box.querySelectorAll('[data-rc-local]').forEach(el => {
    const i = _rcIndex(_rcState.selLocals, el.dataset.rcLocal);
    el.classList.toggle('on', i >= 0);
    if (i >= 0) el.dataset.ord = String(i + 1); else delete el.dataset.ord;
  });
  box.querySelectorAll('[data-rc-pixiv]').forEach(el => {
    const i = _rcIndex(_rcState.selPixivs, el.dataset.rcPixiv);
    el.classList.toggle('on', i >= 0);
    if (i >= 0) el.dataset.ord = String(i + 1); else delete el.dataset.ord;
  });
  paintReconcileBar();   // 计数与按钮可用性一起在操作条里重画
}

// 底部操作条: 选择计数 + 配对模式开关 + 批量认领按钮 + 上次结果
function paintReconcileBar() {
  const bar = $('#rc-bar');
  if (!bar) return;
  const nL = _rcState.selLocals.length, nR = _rcState.selPixivs.length;
  const pair = _rcState.pairMode;
  const btns = [];
  btns.push(`<button class="btn ghost${pair ? ' accent' : ''}" onclick="rcTogglePair()"
    title="顺序配对模式：左右各选 N 个，按点击顺序 1:1 认领（适合一批散图各属于不同作品）">⇄ 顺序配对${pair ? '（已开）' : ''}</button>`);
  if (pair) {
    // 每对挂到目标的第几页: 只给到"选中的目标里最小的页数", 保证每一对都合法
    const counts = _rcState.selPixivs.map(id => {
      const w = _rcState.pixiv.find(x => x.artwork_id === id) || {};
      return Math.max(1, parseInt(w.page_count || 1, 10));
    });
    const maxPage = counts.length ? Math.max(1, Math.min(...counts)) : 1;
    const opts = Array.from({ length: maxPage },
      (_, i) => `<option value="${i}"${i === _rcState.pairPage ? ' selected' : ''}>第 ${i + 1} 页</option>`).join('');
    btns.push(`<span class="hint">每对挂到目标的 <select id="rc-pair-page" onchange="rcSetPairPage(this.value)">${opts}</select></span>`);
    const ok = nL > 0 && nL === nR;
    btns.push(`<button class="btn accent" ${ok ? '' : 'disabled'} onclick="rcDoClaimPairs()"
      title="${ok ? '按点击顺序 1:1 认领' : '左边选中的张数要和右边选中的个数一样（且不为 0）'}">✅ 执行配对认领（${Math.min(nL, nR)} 对）</button>`);
  }
  if (nL || nR || pair) {
    btns.push(`<button class="btn ghost" onclick="rcClearSelection()">🧹 清空选择</button>`);
  }
  const res = _rcState.lastResult;
  let resHtml = '';
  if (res) {
    const failLines = (res.fail || []).slice(0, 3)
      .map(f => `<div class="hint">✖ ${esc(f.label || f.lid)} → #${esc(f.tid)}：${esc(f.error)}</div>`).join('');
    const more = (res.fail || []).length > 3
      ? `<div class="hint">…还有 ${res.fail.length - 3} 条失败</div>` : '';
    const nEx = (res.ok || []).filter(x => x.extra).length;
    resHtml = `<div class="rc-result">
      <b>上次认领：成功 ${(res.ok || []).length} 张${nEx ? `（其中 ${nEx} 张附加页）` : ''}${(res.fail || []).length ? `，失败 ${res.fail.length} 张` : '，全部成功 ✓'}</b>
      ${failLines}${more}</div>`;
  }
  bar.innerHTML = `<span class="hint" id="rc-sel-info">已选：左 ${nL} 张 ｜ 右 ${nR} 个</span>
    <span class="hint">${pair
      ? '配对模式：左右各选 N 个（点一下选中），按① → ①、② → ② …的顺序认领'
      : '点左边的图选中（可多选）；再点右边作品 → 选 1 张=单张认领，选多张=合并认领到连续页'}</span>
    ${btns.join('')}${resHtml}`;
}

window.rcTogglePair = function () {
  _rcState.pairMode = !_rcState.pairMode;
  _rcState.selPixivs = [];          // 切模式时清掉右侧选择, 免得"选着选着不知道自己在哪个模式"
  paintReconcileSelection();
};
window.rcSetPairPage = function (v) { _rcState.pairPage = parseInt(v || '0', 10) || 0; };
window.rcClearSelection = function () {
  _rcState.selLocals = []; _rcState.selPixivs = [];
  paintReconcileSelection();
};

function paintReconcile() {
  const box = $('#rc-body');
  if (!box) return;
  const keep = _rcScrollSnapshot(box);   // 重画前记下两个网格滚到哪了
  const r = _rcState.last || {};
  const L = _rcState.locals, P = _rcState.pixiv;
  const author = r.author || {};
  const localCard = (x) => {
    const on = _rcState.selLocal === x.artwork_id;
    const thumb = x.local_path
      ? `/api/local-img?p=${encodeURIComponent(x.local_path)}&thumb=1` : '';
    return `<div class="rc-item${on ? ' on' : ''}" data-rc-local="${esc(x.artwork_id)}"
        onclick="rcPickLocal('${esc(x.artwork_id)}')"
        title="${esc(x.local_path || '无本地文件')}">
      ${thumb ? `<img src="${esc(imgSrc(thumb, x))}" loading="lazy" alt="">` : '<div class="rc-noimg">无图</div>'}
      <div class="rc-meta"><b>${esc(x.title || x.artwork_id)}</b>
        <span class="hint">${esc(x.artwork_id)} · 库内 ${x.pages || 0} 页${x.source ? ' · ' + esc(sourceLabel(x.source)) : ''}</span></div>
    </div>`;
  };
  const pixivCard = (w) => `
    <div class="rc-item${w.in_library ? ' inlib' : ''}${_rcIndex(_rcState.selPixivs, w.artwork_id) >= 0 ? ' on' : ''}"
      data-rc-pixiv="${esc(w.artwork_id)}"
      onclick="rcPickPixiv('${esc(w.artwork_id)}')"
      title="${esc(w.title)}${w.in_library ? '（已在库）' : ''}">
      <img src="${esc(imgSrc(w.thumb_url, w))}" loading="lazy" alt="">
      <div class="rc-meta"><b>#${esc(w.artwork_id)}</b>
        <span class="hint">${w.page_count} 页 · ${esc(w.title)}</span>
        ${w.in_library ? '<span class="tag ok">已在库</span>' : ''}</div>
    </div>`;
  box.innerHTML = `
    <div class="rc-head">
      <span>作者 <b>${esc(author.name || _rcState.authorId)}</b>（${esc(_rcState.authorId)}）</span>
      <span class="hint">左：来源未定的图 ${L.length} 张 ｜ 右：TA 在 pixiv 的作品 ${P.length} 个</span>
      <button class="btn ghost" onclick="reloadReconcile()">🔄 重新拉取</button>
    </div>
    ${r.note ? `<div class="hint warn-text">${esc(r.note)}</div>` : ''}
    <div class="rc-bar" id="rc-bar"></div>
    <div class="rc-cols">
      <div class="rc-col">
        <div class="rc-col-title">🗂️ 我库里来源未定的图（${L.length}）</div>
        <div class="rc-grid">${L.map(localCard).join('') || '<div class="hint">没有待认领的图 ✓</div>'}</div>
      </div>
      <div class="rc-col">
        <div class="rc-col-title">🎨 TA 在 pixiv 的作品（${P.length}）
          <span class="hint">（缩略图按新→旧；未开配对模式时点一下即认领左边的选择）</span></div>
        <div class="rc-grid">${P.map(pixivCard).join('') || '<div class="hint">没有拉取到作品列表（见上方提示）</div>'}</div>
      </div>
    </div>
    <div class="btn-row"><button class="btn ghost" onclick="closeLibModal()">关闭</button></div>`;
  _rcRestoreScroll(box, keep);   // 还原两个网格的滚动位置 (不再跳回顶部)
  paintReconcileSelection();     // 画选中态角标 + 底部操作条
}

// 点左边的图: 多选开关 (选中顺序 = 认领顺序)
window.rcPickLocal = function (aid) {
  const i = _rcIndex(_rcState.selLocals, aid);
  if (i >= 0) _rcState.selLocals.splice(i, 1); else _rcState.selLocals.push(String(aid));
  paintReconcileSelection();   // 只切选中态: 不重建 DOM → 滚动条不动
};

// 点右边的作品: 配对模式下=选中; 否则=把左边选中的图认领过去
window.rcPickPixiv = function (tid) {
  tid = String(tid);
  if (_rcState.pairMode) {
    const i = _rcIndex(_rcState.selPixivs, tid);
    if (i >= 0) _rcState.selPixivs.splice(i, 1); else _rcState.selPixivs.push(tid);
    paintReconcileSelection();
    return;
  }
  const L = _rcState.selLocals;
  if (!L.length) { toast('请先点左边的图（可多选），再点右边的作品认领', 'err'); return; }
  const w = _rcState.pixiv.find(x => x.artwork_id === tid) || {};
  const total = Math.max(1, parseInt(w.page_count || 1, 10));
  if (L.length === 1) {
    // 单张: 选页确认。除了固定的"第 N 页", 必须再给一条**自动追加**的路 ——
    // 目标若是「已入库」的作品(前 total 页全占着), 只给固定页号的话: 选第 N 页会撞已有文件,
    // 选"附加页"那张塞到第 2 张时又会撞上刚加的附加页 → 满屏 409 失败, 看着就像"错乱"
    //。「自动」= 交给服务端挑第一个空位, 没有空位就往后追加为附加页。
    const opts = Array.from({ length: total },
      (_, i) => `<option value="${i}">第 ${i + 1} 页</option>`).join('')
      + `<option value="${total}">附加页（超出原 ${total} 页）</option>`
      + '<option value="">自动：下一个空位（没有空位就往后追加为附加页）</option>';
    openSrcModal('✅ 认领转正', `
      <div class="hint">把左边这张图挂到 <b>#${esc(tid)}</b> 的哪一页？<br>
        文件会改名为 <code>${esc(tid)}_pN.扩展名</code> 并移入该作者目录；
        本地占位作品被搬空后会自动删除。目标那一页若已有文件会明确拒绝，绝不覆盖。<br>
        该作品在 pixiv 上共 <b>${total} 页</b>；<b>选「自动」</b>适合"已入库、还想再追加几张"
        的情况（程序挑下一个空位，超出原页数的自动标成附加页，以后可再拆出去）。</div>
      <div class="modal-field"><label>目标页</label><select id="rc-page">${opts}</select></div>
      <div class="modal-actions">
        <button class="btn accent" onclick="rcDoClaim('${esc(tid)}')">✅ 认领</button>
        <button class="btn ghost" onclick="closeSrcModal()">取消</button>
      </div>`);
    return;
  }
  // 多张: 合并认领 —— 依次挂到连续页; 超出该作品原页数的部分自动成为「附加页」。
  // 起始页除了原页, 还给「从附加页开始」和「自动」两条路: 目标是已入库作品时,
  // 从第 1 页开始会一路撞已有文件 → 一片 409 失败。
  const n = L.length;
  const startOpts = Array.from({ length: Math.max(1, total) },
    (_, i) => `<option value="${i}">从第 ${i + 1} 页开始</option>`).join('')
    + `<option value="${total}">从附加页开始（第 ${total + 1} 张起）</option>`
    + '<option value="">自动：从下一个空位开始依次排</option>';
  openSrcModal(`🔗 合并认领 ${n} 张 → #${esc(tid)}`, `
    <div class="hint">把左边选中的 <b>${n} 张</b>依次挂到 <b>#${esc(tid)}</b> 的连续页上
      （适合：这一组散图其实是同一个 pixiv 多页作品）。<br>
      文件会改名为 <code>${esc(tid)}_pN.扩展名</code> 并移入该作者目录；某一页已被占用会单独报出，绝不覆盖。<br>
      该作品在 pixiv 上共 <b>${total} 页</b>，<b>超出原页数的会自动作为附加页</b>（标 origin=merged，以后可再拆出去）。
      该作品已入库、只想把这几张追加进去时，起始页请选<b>「附加页开始」或「自动」</b>。</div>
    <div class="modal-field"><label>起始页</label>
      <select id="rc-start-page" onchange="rcPaintMergePreview('${esc(tid)}')">${startOpts}</select></div>
    <div class="rc-preview" id="rc-preview"></div>
    <div class="modal-actions">
      <button class="btn accent" onclick="rcDoClaimMulti('${esc(tid)}')">🔗 合并认领</button>
      <button class="btn ghost" onclick="closeSrcModal()">取消</button>
    </div>`);
  rcPaintMergePreview(tid);
};

// 合并认领的映射预览: 随「起始页」实时重画, 并把超出的那几张标成附加页
window.rcPaintMergePreview = function (tid) {
  const box = $('#rc-preview');
  if (!box) return;
  const w = _rcState.pixiv.find(x => String(x.artwork_id) === String(tid)) || {};
  const total = Math.max(1, parseInt(w.page_count || 1, 10));
  const sel = $('#rc-start-page');
  const auto = !!sel && sel.value === '';      // 「自动」: 页号由服务端挑, 前端画不出定数
  const start = auto ? 0 : (sel ? parseInt(sel.value || '0', 10) || 0 : 0);
  box.innerHTML = _rcState.selLocals.map((lid, i) => {
    const x = _rcState.locals.find(v => v.artwork_id === lid) || {};
    const label = `${i + 1}. ${esc((x.title || lid).slice(0, 30))}`;
    if (auto) {
      return `<div class="hint">${label} → <b>自动</b>`
        + '（挑下一个空位；没有空位就往后追加为附加页）</div>';
    }
    const p = start + i;
    return `<div class="hint">${label} → <b>p${p}</b>`
      + (p >= total ? ' <span class="tag">附加页</span>' : '') + '</div>';
  }).join('');
};

// 认领多个 (逐张串行: 单飞更容易看清"第几张失败"), 失败不中断, 最后汇总
async function _rcClaimSeq(pairs) {
  const ok = [], fail = [];
  for (const p of pairs) {
    try {
      const r = await api('/api/library/claim-image', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ local_artwork_id: p.lid, target_artwork_id: p.tid, page: p.page }) });
      const err = apiErr(r);
      if (err) throw new Error(err);
      // r.is_extra: 后端判定这张超出了 pixiv 原页数, 已作为 origin='merged' 的附加页落库
      ok.push({ ...p, page: (r.page != null ? r.page : p.page), extra: !!r.is_extra });
    } catch (e) {
      fail.push({ ...p, error: e.message || String(e) });
    }
  }
  return { ok, fail };
}

// 认领成功后统一收尾: 清选择 + 记录结果 + 按域刷新 + 留着结果给用户核对
async function _rcAfterClaims(res) {
  _rcState.selLocals = []; _rcState.selPixivs = [];
  _rcState.lastResult = {
    ok: res.ok,
    fail: res.fail.map(f => ({
      ...f, label: ((_rcState.locals.find(v => v.artwork_id === f.lid) || {}).title || f.lid),
    })),
  };
  closeSrcModal();
  const nOk = res.ok.length, nFail = res.fail.length;
  const nExtra = res.ok.filter(x => x.extra).length;
  const extraTxt = nExtra ? `（其中 ${nExtra} 张作为附加页）` : '';
  toast(nFail ? `认领完成：成功 ${nOk} 张${extraTxt}，失败 ${nFail} 张（明细见对账台底部）`
              : `认领完成：${nOk} 张全部成功 ✓${extraTxt}`, nFail ? 'info' : 'ok');
  if (nOk) setLastAction('work', res.ok.map(p => p.tid), `刚认领的 ${nOk} 个作品`);
  await reloadReconcile();
  loadLibrary(); loadStats(); loadLibFacets();
  loadAuthors({ resetPage: false });
}

window.rcDoClaim = async function (tid) {
  const lid = _rcState.selLocals[0];
  if (!lid) return;
  const sel = $('#rc-page');
  const raw = sel ? sel.value : '0';
  // 空值 = 「自动」→ 不传 page, 由服务端挑下一个空位 (绝不能当 0: 已入库作品的第 1 页会被撞)
  const page = raw === '' ? undefined : (parseInt(raw, 10) || 0);
  const res = await _rcClaimSeq([{ lid, tid: String(tid), page }]);
  await _rcAfterClaims(res);
};

// 合并认领: 选中的 N 张 → 目标作品的连续页
window.rcDoClaimMulti = async function (tid) {
  const L = _rcState.selLocals.slice();
  if (!L.length) return;
  const sel = $('#rc-start-page');
  const raw = sel ? sel.value : '0';
  const auto = raw === '';
  const start = auto ? 0 : (parseInt(raw, 10) || 0);
  // 「自动」时每张都不传 page → 服务端逐张挑下一个空位, 天然连续, 不会撞已有文件
  const pairs = L.map((lid, i) => ({ lid, tid: String(tid),
                                    page: auto ? undefined : start + i }));
  await _rcAfterClaims(await _rcClaimSeq(pairs));
};

// 顺序配对: 左右各 N 个, 按点击顺序 1:1
window.rcDoClaimPairs = async function () {
  const L = _rcState.selLocals.slice(), P = _rcState.selPixivs.slice();
  if (!L.length || L.length !== P.length) {
    toast('左边选中的张数要和右边选中的个数一样（且不为 0）', 'err'); return;
  }
  const page = parseInt(_rcState.pairPage || 0, 10) || 0;
  const pairs = L.map((lid, i) => ({ lid, tid: P[i], page }));
  await _rcAfterClaims(await _rcClaimSeq(pairs));
};

// 把本地作者绑定到真实 pixiv 作者 ID (链接产生关联的核心动作)
window.bindAuthorPixiv = async function (authorId, pixivId) {
  if (!confirm(`把该作者的全部作品绑定到 pixiv 作者 ID ${pixivId}？\n\n`
    + `· 作品会从本地作者迁到这个 pixiv 作者名下（作者行合并，曾用名/链接跟着走）\n`
    + `· 磁盘文件夹会按新身份改名（名字_local_xxx → 名字_${pixivId}）\n`
    + `· 文件会归拢到作者文件夹（如 #其他来源#/… → 作者名_${pixivId}/ 下的来源子夹）\n`
    + `· 之后体检的「作者改名同步 / 存续状态」也会认得他\n\n`
    + `不确定是不是同一人时请先取消 —— 同名不代表同一人。`)) return;
  try {
    const r = await api('/api/library/author-bind-pixiv', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ author_id: authorId, pixiv_id: String(pixivId) }) });
    const err = apiErr(r) || (r.error || '');
    if (err) throw new Error(err);
    toast(`已绑定到 pixiv 作者「${r.name || pixivId}」（迁移 ${r.artworks || 0} 个作品`
      + (r.folder_renamed ? '，文件夹已改名' : '')
      + (r.files_gathered ? `，归拢 ${r.files_gathered} 个文件` : '') + '）✓', 'ok');
    const gf = r.gather_failed || [];
    if (gf.length) {
      toast(`有 ${gf.length} 个文件没能归拢（多半是同名文件已存在，绝不覆盖）：`
        + gf.slice(0, 2).join('；'), 'err');
    }
    closeLibModal();
    loadAuthors({ resetPage: false }); loadLibFacets(); loadLibrary();
  } catch (e) { toast('绑定失败: ' + e.message, 'err'); }
};

// 作者「来源链接」编辑行 (可多条): 站点/账号由后端保存时自动识别, 这里先把已识别的显示出来
function authorUrlRowHtml(u) {
  const url = (u && u.url) || '';
  const site = (u && u.site) || '';
  const handle = (u && u.handle) || '';
  const label = u && u.label ? u.label : (site ? (sourceLabel(site) || site) : '自动识别');
  const tag = site
    ? `${esc(label)}${handle ? ' @' + esc(handle) : ''}`
    : '保存时自动识别站点';
  return `<div class="auth-url-row">
    <input type="url" class="auth-url-input" value="${esc(url)}" spellcheck="false"
      placeholder="https://…（作者主页 / 作品列表页）">
    <span class="auth-url-tag${site ? ' ok' : ''}">${tag}</span>
    <button class="btn danger icon-btn" title="删掉这条链接"
      onclick="this.closest('.auth-url-row').remove()">✕</button>
  </div>`;
}
window.addAuthorUrlRow = function () {
  const box = $('#author-edit-urls');
  if (box) box.insertAdjacentHTML('beforeend', authorUrlRowHtml(null));
};

async function editAuthor(authorId) {
  let a = null;
  const r = await api('/api/library/authors?q=' + encodeURIComponent(authorId) + '&limit=20');
  a = (r.items || []).find(x => String(x.author_id) === String(authorId));
  if (!a) { toast('未找到作者', 'err'); return; }
  const aliasesVal = esc((a.aliases || []).join('\n'));
  const urlRows = (a.urls || []).map(authorUrlRowHtml).join('');
  openLibModal(`编辑作者 · ${esc(a.name || authorId)}`, `
    <div class="lib-edit-form">
      <label>作者名<input id="author-edit-name" type="text" value="${esc(a.name || '')}"></label>
      <label>状态
        <select id="author-edit-status">
          <option value="">（保持不变）</option>
          <option value="alive"${a.status === 'alive' ? ' selected' : ''}>活跃中</option>
          <option value="gone"${a.status === 'gone' ? ' selected' : ''}>已注销</option>
          <option value="unknown"${a.status && a.status !== 'alive' && a.status !== 'gone' ? ' selected' : ''}>未知（未核对过）</option>
        </select>
        <div class="muted" style="font-size:12px;margin-top:2px">状态 = pixiv 账号存续判定。作品来源可能是其他站点（bilibili/X/GGAC…），但只要作者有 pixiv 账号，就按 pixiv 账号是否注销判定。</div>
      </label>
      <label>曾用名（每行一个，留空则清空）<textarea id="author-edit-aliases" rows="5" placeholder="一个曾用名占一行">${aliasesVal}</textarea></label>
      <div class="modal-field">
        <label>来源链接（可多条：pixiv / X / bilibili / GGAC / danbooru… 的主页）</label>
        <div id="author-edit-urls">${urlRows}</div>
        <div class="btn-row" style="margin-top:6px">
          <button class="btn ghost" onclick="addAuthorUrlRow()">＋ 添加一条链接</button>
        </div>
        <div class="muted" style="font-size:12px;margin-top:4px">
          链接是<strong>有用的</strong>，不只是记录：保存时会自动识别站点与账号 ——
          ① 记上 <code>https://x.com/xxx</code>，该作者名下「来源=X 但没记账号」的作品会自动补上 <code>@xxx</code>（归档子夹/显示随之正确）；
          ② 记上 pixiv 主页 <code>pixiv.net/users/12345</code>，本地作者可一键<strong>绑定成真正的 pixiv 作者</strong>（作品迁移、文件夹改名、参与改名与存续同步）；
          ③ 作者管理里可直接按链接/账号搜索；🌐 按钮打开排在第一位的链接。
        </div>
      </div>
      <div class="btn-row">
        <button class="btn accent" onclick="saveAuthorEdit('${esc(authorId)}')">💾 保存</button>
        <button class="btn ghost" onclick="refreshAuthor('${esc(authorId)}')"
          title="只刷新这一个作者：联网核对现用名与存续状态（1 次请求，不用跑全量自检）">🔄 联网刷新状态</button>
        <button class="btn ghost" onclick="closeLibModal()">取消</button>
      </div>
    </div>`, { lg: true });
}

async function saveAuthorEdit(authorId) {
  const name = $('#author-edit-name').value.trim();
  const status = $('#author-edit-status').value || null;
  const aliasesText = $('#author-edit-aliases').value.trim();
  const aliases = aliasesText.split('\n').map(s => s.trim()).filter(Boolean);
  const urlInputs = $$('#author-edit-urls .auth-url-input');
  const urls = urlInputs.map(i => i.value.trim()).filter(Boolean);
  const hadUrls = urlInputs.length > 0;   // 原本就有链接行 = 这次可能是在清空/修改
  if (!name && !status && !aliasesText && !hadUrls && !urls.length) {
    toast('没有任何修改', 'err'); return;
  }
  try {
    const r = await api('/api/library/author-edit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ author_id: authorId, name: name || null, status,
                             aliases, urls }),
    });
    const err = apiErr(r);
    if (err) throw new Error(err);
    const filled = r.source_author_filled || 0;
    toast(`已保存 ✓（来源链接 ${(r.urls || []).length} 条`
      + (filled ? `，已按链接回填 ${filled} 个作品的账号` : '') + '）', 'ok');
    closeLibModal(); loadAuthors({ resetPage: false }); loadLibFacets(); loadLibrary();
    // 保存后**自动定向刷新这一个作者**（1 次 HTTP，静默）: 不用再等全量自检更新状态
    if (/^\d+$/.test(String(authorId))) refreshAuthor(authorId, { silent: true });
    else setLastAction('author', [authorId], '刚编辑的作者');
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function api(url, opts = {}) {
  const resp = await fetch(url, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
  if (!resp.ok && !data) throw new Error('HTTP ' + resp.status);
  return data;
}

function apiErr(r) {
  // 带 body 的写接口在非 2xx 时 api() 不抛异常(只解析 JSON), 必须显式判断,
  // 否则会把「数据库被占用/校验失败」当成保存成功提示给用户 (2026-09-16 实测)。
  if (!r || r.status === 'error' || r.locked) {
    return (r && (r.error || r.message)) || '操作失败（服务端未返回原因）';
  }
  return '';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// 安全模式下的 R18 占位图: 纯文字 SVG, 不含任何图片内容。
// 关键点: 安全模式开启时 R18 **连原图/缩略图都不去请求**(而不是下载后再靠 CSS 模糊) ——
// 浏览器里从不出现 R18 像素, 截图/录屏/他人瞥屏都不可能泄露。
// 只有用户在 Lightbox 里主动点「🔒 显示」才临时加载该张原图 (见 urlsOf / revealBtn)。
const R18_MASK_SVG = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="640">'
  + '<rect width="100%" height="100%" fill="#2a2d34"/>'
  + '<circle cx="240" cy="250" r="86" fill="none" stroke="#d9a441" stroke-width="6"/>'
  + '<text x="240" y="272" font-family="sans-serif" font-size="62" fill="#d9a441"'
  + ' text-anchor="middle">R18</text>'
  + '<text x="240" y="396" font-family="sans-serif" font-size="30" fill="#9fa6b2"'
  + ' text-anchor="middle">安全模式已打码</text>'
  + '<text x="240" y="444" font-family="sans-serif" font-size="24" fill="#7d848f"'
  + ' text-anchor="middle">双击大图后可点「显示」</text></svg>');

function imgSrc(u, w) {
  // 安全模式 + R18 → 返回占位图, 绝不把真实图片地址交给浏览器
  if (safemode && w && w.is_r18) return R18_MASK_SVG;
  if (!u) return '';
  // 本地已由后端生成好的代理地址(/api/local-img) 原样返回; 远端(http) 走 /api/img 防盗链代理
  if (String(u).indexOf('/api/') === 0) return u;
  return '/api/img?url=' + encodeURIComponent(u);
}

// 反搜页等「不知道 R18 与否」的位置: 安全模式下**一律**不加载真实图片, 只给占位图。
// 判断原则是「不确定就当 R18」(fail-safe) —— 宁可多遮, 也不能因为缺个标记就漏出内容。
function safeThumb(u, w) {
  if (safemode && !(w && w.is_r18 === 0)) return R18_MASK_SVG;
  return imgSrc(u, w);
}

// 是否可在 pixiv 原站补下: 非 pixiv 来源(ext_/merged_/其他来源)点了加入清单也是
// 必然失败(pixiv 无此作品, crawler 检测 404 后标 ERROR), 直接不给「加入清单」入口。
// 双保险: ① 非数字 ID (ext_/merged_) 一律排除; ② 有 source 且非 pixiv 的排除;
// ③ 作品页已确认失效(404, deleted_at) 的也排除 —— 原站再也拿不到, 只能去别处找来源
//。
function canRedownload(w) {
  if (!w) return false;
  if (/^(ext|merged)_/.test(String(w.artwork_id || ''))) return false;
  if (w.deleted) return false;
  return !w.source || w.source === 'pixiv';
}

function fmtSize(bytes) {
  if (!bytes) return '0 B';
  const g = 1024, u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (bytes >= g && i < u.length - 1) { bytes /= g; i++; }
  return bytes.toFixed(bytes >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}

function statusTag(s) {
  const map = { pending: '待下载', running: '进行中', done: '✓ 完成', failed: '✖ 失败', ok: '✓ 完成' };
  return `<span class="tag-state ${esc(s || 'pending')}">${esc(map[s] || s || '')}</span>`;
}

// ============ SSE 实时事件 ============
function connectEvents() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    handleEvent(data);
  };
  es.onerror = () => { /* EventSource 自动重连 */ };
}

function handleEvent(ev) {
  switch (ev.type) {
    case 'log':
      appendLog(ev.level || 'info', ev.msg || '');
      break;
    case 'cart_progress':
      // 总进度条只认「任务条数」口径 (后端 run_cart 发)。页级的 'progress' 事件
      // (爬虫每下一页发一次, total 是作品的页数) 不再驱动它 —— 否则批量下载时
      // 第一个作品下完就把条子冲到 100% 再跳回来, 看着像摆设。
      // 页级进度仍走 item_progress 喂行内「进度」那一格。
      updateOverallProgress(ev.cur, ev.total, ev.status);
      break;
    case 'item_progress':
      updateItemProgress(ev.item_id, ev.cur, ev.total, ev.status);
      break;
    case 'dirty':
      // 🔔 事件驱动刷新: 后端登记"哪些数据域变了" → 只刷新对应视图 (见 SCOPE_REFRESH)
      if (typeof onDirty === 'function') onDirty(ev);
      break;
    case 'app_quit':
      // 其他标签页收到退出广播: 各自弹遮罩 + 尝试关闭, 一次退出全部关闭
      showExitMask();
      break;
    case 'app_focus':
      // 桌面双击 exe 时单实例进程发来「激活窗口」: 把本标签页/窗口带到前台
      // (window.focus 尽量唤起窗口; 标题短暂闪烁提示已激活)
      if (document.hasFocus && !document.hasFocus()) window.focus();
      activateTabByFocus();
      break;
    default:
      // 自定义事件: 忽略未知类型, 兼容扩展
      break;
  }
}

// 收到「激活窗口」(桌面双击 exe) 时的响应: 尽力唤起本标签页, 标题闪烁 3 下提示已激活
// 用全局 timer 句柄: 连续双击多次 exe 时先清掉上一个闪烁, 不会两个定时器抢改标题
function activateTabByFocus() {
  try {
    if (document.hidden) {
      const base = document.title;
      if (window.__focusBlinkTimer) { clearInterval(window.__focusBlinkTimer); window.__focusBlinkTimer = null; }
      let n = 0;
      window.__focusBlinkTimer = setInterval(() => {
        document.title = (n % 2 === 0) ? '✦ 已激活 — ' + base : base;
        if (++n >= 6) { clearInterval(window.__focusBlinkTimer); window.__focusBlinkTimer = null; document.title = base; }
      }, 350);
    }
  } catch (e) { /* 忽略 */ }
}

// ============ 日志条 ============
function appendLog(level, msg) {
  const panel = $('#log-panel');
  const empty = panel.querySelector('.log-empty');
  if (empty) empty.remove();
  const line = document.createElement('div');
  line.className = level === 'error' ? 'error' : level === 'warn' ? 'warn' : '';
  line.textContent = msg;
  panel.appendChild(line);
  while (panel.childNodes.length > 500) panel.removeChild(panel.firstChild);
  panel.scrollTop = panel.scrollHeight;
}

// ============ 搜索 (作品 / 作者 双子页) ============
let searchSeq = 0;
let searchScope = 'artwork';           // 当前激活子页: 'artwork' | 'author'
const searchCache = {};                // { scope: { q, html } } 切页恢复, 避免重复查询
// 🚫 隐藏 AI 作品 :
// 判定一律用 pixiv 官方 aiType (ai_type===2); 0(没标记) 按非 AI 处理 —— 绝不误藏。
let searchHideAi = true;
try { searchHideAi = localStorage.getItem('vixiq.searchHideAi') !== '0'; } catch (e) {}

// 把作品列表按「隐藏 AI」拆成 [保留的, 被藏起来的个数]
function splitAiWorks(works) {
  const list = works || [];
  if (!searchHideAi) return [list, 0];
  const keep = list.filter(w => Number((w || {}).ai_type || 0) !== 2);
  return [keep, list.length - keep.length];
}
// 藏了东西必须说出来 —— 静默少几张卡用户会以为查询坏了
function aiHiddenHint(n) {
  return n > 0 ? `<div class="hint ai-hidden-hint">🤖 已隐藏 ${n} 个 AI 作品（点右上「🚫 隐藏 AI 作品」可显示）</div>` : '';
}

// ===== 没输入内容时的推荐  =====
// 两个来源的差别: 🏆 排行榜 = pixiv 日榜(内容确定, 但接口不给 AI 标记);
//                ✨ 为你推荐 = 按你的浏览/收藏算(自带 AI 标记)。
let feedKind = 'ranking';
try { const k = localStorage.getItem('vixiq.feedKind'); if (k === 'ranking' || k === 'discovery') feedKind = k; } catch (e) {}
const feedCache = {};      // { kind: res } —— 切来源/拨 AI 开关时就地重渲染, 不重新联网
// 排行榜接口不给 AI 标记 → 只有**带 hide_ai=1 取过一次**才算"这批的 AI 标记已补齐"。
// 不记这个 flag 的话, 拨开「隐藏 AI」时会拿一份全是"未知"的缓存去过滤 → 一张都藏不掉。
const feedAiDone = {};

function paintFeedKind() {
  $$('.feed-kind').forEach(b => b.classList.toggle('active', b.dataset.kind === feedKind));
}
function hideFeed() { const el = $('#search-feed'); if (el) el.hidden = true; }
function showFeedBox() { const el = $('#search-feed'); if (el) el.hidden = false; }

// 空输入 = 回到推荐 (以前只弹「请输入搜索内容」, 用户没有回到推荐的路)
async function backToFeed() {
  showFeedBox();
  const sbox = $('#search-result');
  if (sbox) sbox.innerHTML = fullHintHtml();
  delete searchCache[searchScope];
  await loadFeed();
}

// 这批缓存里的 AI 标记够不够用来过滤? (discovery 自带; ranking 要专门取一次)
function feedNeedsAi(kind) {
  return searchHideAi && kind === 'ranking' && !feedAiDone[kind];
}

async function loadFeed(force = false) {
  const box = $('#search-feed-body');
  if (!box) return;
  showFeedBox();
  paintFeedKind();
  const kind = feedKind;
  if (!force && feedCache[kind] && !feedNeedsAi(kind)) {
    renderFeed(feedCache[kind]);
    return;
  }
  box.innerHTML = '<div class="hint">⏳ 正在取推荐…</div>';
  try {
    const res = await api('/api/search/feed?kind=' + kind
      + '&hide_ai=' + (searchHideAi ? '1' : '0'));
    if (kind !== feedKind) return;              // 用户已经切到另一个来源了
    feedCache[kind] = res;
    if (searchHideAi) feedAiDone[kind] = true;
    renderFeed(res);
  } catch (e) {
    box.innerHTML = `<div class="hint err-line">✖ ${esc(e.message || '取推荐失败')}</div>`;
  }
}

function renderFeed(res) {
  const box = $('#search-feed-body');
  if (!box || !res) return;
  const [works, hidden] = splitAiWorks(res.works);
  if (!works.length) {
    box.innerHTML = aiHiddenHint(hidden)
      + `<div class="hint">${hidden > 0 ? '这批推荐全是 AI 作品。' : '这批没取到内容。'}换个来源或点「↻ 刷新」试试。</div>`;
    return;
  }
  const note = res.kind === 'ranking'
    ? '🏆 pixiv 日榜 · 今天大家在看这些'
    : '✨ 按浏览 / 收藏记录推荐';
  box.innerHTML = `<div class="hint">${note}</div>`
    + renderWorksGrid(works) + aiHiddenHint(hidden);
}

window.setFeedKind = async function (kind) {
  if (kind === feedKind) return;
  feedKind = kind;
  try { localStorage.setItem('vixiq.feedKind', kind); } catch (e) {}
  paintFeedKind();
  await loadFeed();
};

function setSearchScope(scope) {
  // tab 只负责「选类型」, 不触发查询 —— 查询由顶栏回车/搜索按钮发起
  // 注意认一下 scope: 推荐区的 .feed-kind 按钮也复用了 .sstab 样式, 但它们没有
  // data-scope —— 不拦的话会把 searchScope 设成 undefined, 整个搜索就废了
  // (2026-09-20 加推荐区时踩到过)。
  if (!scope || scope === searchScope) return;   // 已激活/无效, 忽略
  authorDetailBack = null;   // 手动切子页 → 不再是「从关键词结果进作者详情」, 返回条撤掉
  const box = $('#search-result');
  // 切走前先把当前页渲染内容记到旧 scope 缓存
  if (searchCache[searchScope]) searchCache[searchScope] = { q: searchCache[searchScope].q, html: box.innerHTML };
  else searchCache[searchScope] = { q: $('#search-input').value.trim(), html: box.innerHTML };
  searchScope = scope;
  $$('.sstab[data-scope]').forEach(b => b.classList.toggle('active', b.dataset.scope === scope));
  // 恢复该 scope 之前的缓存; 有则直接显示(不重复请求), 没有则只给空提示(不强搜)
  const cached = searchCache[scope];
  if (cached && cached.html && cached.html !== fullHintHtml()) {
    box.innerHTML = cached.html;
    // 缓存里的按钮态可能过时 (如下载已完成): 强制重查一次刷新文案/状态
    box.querySelectorAll('button[data-cart-aid],button[data-cart-author]')
      .forEach(b => { delete b.dataset.dec; });
    decorateCartButtons(box);
  } else {
    box.innerHTML = '<div class="hint">当前选择「' + (scope === 'author' ? '搜索作者' : '搜索作品') +
      '」。在顶栏输入内容后点 🔎 搜索或按回车发起查询。</div>';
  }
}
function fullHintHtml() {
  return '<div class="hint">输入作品ID / 作者URL / 关键字后回车，或切换「搜索作品」/「搜索作者」。</div>';
}

async function doSearch(text, opts = {}) {
  const q = (text || '').trim();
  if (!q) {
    // 空输入 → 回到推荐 (2026-09-20)。翻批(带 offset)时不该走这条, 那是异常调用
    if (opts.offset) { toast('请输入搜索内容', 'err'); return; }
    await backToFeed();
    return;
  }
  hideFeed();                       // 开始真搜索 → 收起推荐区
  const mySeq = ++searchSeq;
  const scope = searchScope;
  const box = $('#search-result');
  // offset: 作者结果里作品列表的「换一批」起点 (0 = 最新一批)
  const off = Math.max(0, parseInt(opts.offset || 0, 10) || 0);
  if (!opts.keep) box.innerHTML = '<div class="hint">⏳ 正在查询…</div>';
  try {
    // scope: artwork/author 分开搜, 不做自动猜测/二次 fallback → 省一次查询
    const res = await api('/api/search?q=' + encodeURIComponent(q) + '&scope=' + scope
      + (off ? '&offset=' + off : ''));
    if (mySeq !== searchSeq || scope !== searchScope) return; // 过期/已切页结果丢弃
    renderSearch(box, res);
    // 连**原始响应**一起缓存: 打码态切换时要能就地重渲染(不能只留 HTML 串 —— 那里面
    // 的 <img src> 已经把打码与否写死了, 重放它等于什么都没变)。
    searchCache[scope] = { q, res, html: box.innerHTML };
    // 换一批后回到结果顶部, 否则用户还停在上一批的底部
    if (off) box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    if (mySeq !== searchSeq || scope !== searchScope) return;
    const msg = (e.message || '查询失败');
    box.innerHTML = `<div class="hint err-line">✖ ${esc(msg)}</div>`;
  }
}

// ===== 作者结果里的作品列表: 共 N 件 + 上一批/下一批  =====
// 以前后端写死只取最新 12 个、界面也没有翻页入口, 用户看到的是"为什么不是全部作品"。
const AUTHOR_WORKS_PAGE = 60;

function worksPagerHtml(res, bottom = false) {
  const a = (res || {}).author || null;
  if (!a) return '';
  const total = (a.works_total == null ? null : parseInt(a.works_total, 10));
  const hasTotal = (total != null && !isNaN(total));
  const off = Math.max(0, parseInt((res || {}).works_offset || 0, 10) || 0);
  const n = ((res || {}).works || []).length;
  const hasPrev = off > 0;
  // 拿不到总数(DOM 兜底)时: 本批满页就当还有更多
  const hasNext = hasTotal ? (off + AUTHOR_WORKS_PAGE < total) : (n >= AUTHOR_WORKS_PAGE);
  // 只有一批 → 不摆那两个常驻禁用的按钮, 只在顶部留一行作品数 (用户 2026-09-18 问「一直显示吗」)
  if (!hasPrev && !hasNext) {
    if (bottom) return '';
    return `<div class="works-pager wp-single"><span class="wp-info">${
      hasTotal ? `共 <b>${total}</b> 件作品` : `本批 ${n} 件作品`}</span></div>`;
  }
  let info;
  if (hasTotal) {
    const pages = Math.max(1, Math.ceil(total / AUTHOR_WORKS_PAGE));
    const cur = Math.floor(off / AUTHOR_WORKS_PAGE) + 1;
    info = `共 <b>${total}</b> 件作品 · 本批第 ${off + 1}–${off + n} 件（第 ${cur}/${pages} 批）`;
  } else {
    info = `本批 ${n} 件作品`;
  }
  return `<div class="works-pager${bottom ? ' wp-bottom' : ''}">
    <span class="wp-info">${info}</span>
    <span class="wp-btns">
      <button class="btn ghost" ${hasPrev ? '' : 'disabled'} onclick="authorWorksPage(-1)">‹ 上一批</button>
      <button class="btn ghost" ${hasNext ? '' : 'disabled'} onclick="authorWorksPage(1)">下一批 ›</button>
    </span>
  </div>`;
}

// 换一批: 用**作者 ID** 重查(后端按 offset 切片), 关键字的原始输入不再需要
window.authorWorksPage = async function (dir) {
  const cached = searchCache[searchScope] || {};
  const res = cached.res || {};
  const aid = ((res.author || {}).author_id) || '';
  if (!aid) return;
  const off = Math.max(0, (parseInt(res.works_offset || 0, 10) || 0)
    + (dir > 0 ? AUTHOR_WORKS_PAGE : -AUTHOR_WORKS_PAGE));
  await doSearch(String(aid), { offset: off, keep: true });
};

// ===== 关键字搜索的「换一批」 =====
// 以前后端 URL 里写死 `p=1` → 同一个词每次搜到的永远是同一批固定结果。
// 现在按 24 条一步往后翻; 后端从 pixiv 分页(每页 60 条)里**连续**切片,
// 所以既不重复也不跳过 —— 一直点「换一批」能把整站结果翻完。
const KEYWORD_WORKS_PAGE = 24;

// batchN = 后端本批返回的条数(**AI 过滤前**) —— 用户没勾隐藏 AI 时 == 可见张数
function keywordPagerHtml(res, batchN, bottom = false) {
  const off = Math.max(0, parseInt((res || {}).works_offset || 0, 10) || 0);
  const hasPrev = off > 0;
  const hasNext = batchN >= KEYWORD_WORKS_PAGE;   // 满页 = 很可能还有下一批
  if (!hasPrev && !hasNext) return '';            // 只有一批 → 不摆两个常驻禁用按钮
  const info = batchN ? `本批第 ${off + 1}–${off + batchN} 件` : '本批没有更多了';
  return `<div class="works-pager${bottom ? ' wp-bottom' : ''}">
    <span class="wp-info">${info}</span>
    <span class="wp-btns">
      <button class="btn ghost" ${hasPrev ? '' : 'disabled'} onclick="keywordWorksPage(-1)">‹ 上一批</button>
      <button class="btn ghost" ${hasNext ? '' : 'disabled'} onclick="keywordWorksPage(1)">换一批 ›</button>
    </span>
  </div>`;
}

// 关键字翻批: 用**当初搜的那个词**重查 (offset 变化), 不是拿作品 ID/作者 ID
window.keywordWorksPage = async function (dir) {
  const cached = searchCache[searchScope] || {};
  const q = cached.q || $('#search-input').value.trim();
  if (!q) return;
  const off = Math.max(0, (parseInt((cached.res || {}).works_offset || 0, 10) || 0)
    + dir * KEYWORD_WORKS_PAGE);
  await doSearch(q, { offset: off, keep: true });
};

function renderSearch(box, res) {
  if (res.status === 'not_found') {
    box.innerHTML = `<div class="hint">✖ ${esc(res.text || '未找到')}</div>`;
    return;
  }
  if (res.status === 'error') {
    box.innerHTML = `<div class="hint">✖ ${esc(res.text || '查询出错')}</div>`;
    return;
  }
  let html = '';
  const type = res.type;

  if (type === 'artwork') {
    // 作者卡 + 单作品大卡
    // withButton=true: 作品页的作者卡也要有「♡ 关注 / ➕ 全部作品加入清单」——
    // 这里以前漏传第二个参数(其它三条分支都传了), 导致作品查询结果里作者信息只有名字/粉丝数,
    // 没有可点的操作按钮。
    // withDetail=true: 按作品 ID 直查出来的作者卡也要能「📄 查看详情」看他的其它作品
    // 返回条会显示「作品 #xxx 的作者」。
    if (res.author) html += renderAuthorCard(res.author, true, true);
    if (res.artwork && searchHideAi && Number(res.artwork.ai_type || 0) === 2) {
      // 按作品ID 直查到的就是 AI 作品: 整张大卡收起, 只留一句说明(别让页面空得像查不到)
      html += aiHiddenHint(1);
    } else if (res.artwork) {
      const a = res.artwork;
      html += `<div class="panel"><div class="artwork-big">` +
        `<div class="artwork-thumb-wrap">` +
        `<img class="thumb${r18BlurCls(a)}" src="${imgSrc((a.image_urls || [])[0], a)}" alt="" onerror="this.style.visibility='hidden'">` +
        `${r18FlagHtml(a)}</div>` +
        `<div style="flex:1;min-width:240px"><h2>${esc(a.title)}</h2>` +
        `<div class="w-meta" style="margin:6px 0">ID: ${esc(a.artwork_id)} · 作者: ${esc(a.author_name || a.author_id)} · ${esc(a.page_count || 1)} 页${a.is_r18 ? ' · R18' : ''}</div>` +
        `<div class="w-tags" style="margin:6px 0">${(a.tags || []).slice(0, 10).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>` +
        `<div class="btn-row">` +
        cartActionHtml(a) +
        `<button class="btn ghost" onclick="jumpAuthor('${esc(a.author_id)}')">👤 作者主页</button>` +
        `<a class="btn ghost" target="_blank" rel="noopener" href="${esc(a.pixiv_url)}">🔗 打开原图</a>` +
        `</div></div></div></div>`;
    }
  } else if (type === 'author') {
    // 从关键词结果点「查看详情」进来的 → 顶部给一条返回条 (直接输 ID 查的没有这条)
    html += authorBackBarHtml();
    if (res.author) html += renderAuthorCard(res.author, true);
    const [works, hidden] = splitAiWorks(res.works);
    if (works.length) {
      html += worksPagerHtml(res);
      html += renderWorksGrid(works);
      html += aiHiddenHint(hidden);
      html += worksPagerHtml(res, true);
    } else if (hidden > 0) {
      // 本批全是 AI 作品 → 不能只说"暂无作品", 否则用户以为画师没作品
      html += aiHiddenHint(hidden) + worksPagerHtml(res, true);
    } else if ((parseInt(res.works_offset || 0, 10) || 0) > 0) {
      html += '<div class="hint">后面没有更多作品了。</div>' + worksPagerHtml(res, true);
    } else {
      html += '<div class="hint">该作者暂无作品。</div>';
    }
  } else if (type === 'keyword_works') {
    // 关键字 → 只在「搜索作品」页显示作品
    const all = res.works || [];
    const [works, hidden] = splitAiWorks(all);
    const off = Math.max(0, parseInt(res.works_offset || 0, 10) || 0);
    if (works.length) {
      html += '<h3 style="margin:4px 0 8px">作品</h3>' + keywordPagerHtml(res, all.length) +
        renderWorksGrid(works) + aiHiddenHint(hidden) + keywordPagerHtml(res, all.length, true);
    } else if (hidden > 0) {
      // 本批全是 AI 作品 → 不能只说"未找到", 否则用户以为这个词搜不出东西
      html += aiHiddenHint(hidden) + keywordPagerHtml(res, all.length, true);
    } else if (off > 0) {
      html += '<div class="hint">后面没有更多作品了。</div>' + keywordPagerHtml(res, 0, true);
    } else {
      html += '<div class="hint">未找到相关作品。</div>';
    }
  } else if (type === 'keyword_authors') {
    // 关键字 → 只在「搜索作者」页显示相关作者
    if (res.authors && res.authors.length) {
      html += '<h3 style="margin:4px 0 8px">相关作者</h3>' +
        res.authors.map(a => renderAuthorCard(a, true, true)).join('');
    } else {
      html += '<div class="hint">未找到相关作者。</div>';
    }
  } else {
    // keyword (兼容旧 auto)
    if (res.related_authors && res.related_authors.length) {
      html += '<h3 style="margin:10px 0 8px">相关作者</h3>' +
        res.related_authors.map(a => renderAuthorCard(a, true, true)).join('');
    }
    const [works, hidden] = splitAiWorks(res.works);
    if (works.length) {
      html += '<h3 style="margin:14px 0 8px">作品</h3>' + renderWorksGrid(works) + aiHiddenHint(hidden);
    } else if (hidden > 0) {
      html += aiHiddenHint(hidden);
    } else {
      html += '<div class="hint">未找到相关作品。</div>';
    }
  }
  box.innerHTML = html;
  // 结果里若有接口未带状态的按钮(如部分作者卡), 异步补齐一次
  decorateCartButtons(box);
  // 关注状态: 只有「确定到某一位作者」的结果才预取(1 次请求), 关键字作者列表不预取
  // (10 张卡会打 10 次), 那些按钮点一下时会先查状态再动作。
  // artwork 也算"确定到某一位"(作品页就一个作者), 预取后按钮能直接显示「♥ 已关注」。
  hydrateFollowButtons(box, type === 'author' || type === 'artwork');
}

// withDetail=true: 再挂一个「📄 查看详情」——
//   关键词查作者时一屏十几位画师, 以前只有「关注 / 加入清单」, 想看"这人画了什么"只能点卡片
//   去 pixiv 外部页; 而直接输入作者 ID 却能进站内作者页(作者卡+作品列表+翻批)。
//   现在关键词结果里每一位也能一键进同一个页面, 并留「‹ 返回关键词结果」。
function renderAuthorCard(a, withButton = false, withDetail = false) {
  const avatar = a.avatar_url ? `<img class="avatar" src="${imgSrc(a.avatar_url)}" alt="" onerror="this.style.visibility='hidden'">`
                              : '<div class="avatar" style="display:flex;align-items:center;justify-content:center;background:#e3f0fd;color:var(--accent);font-weight:700">' + esc((a.name || '?')[0]) + '</div>';
  return `<div class="author-card" style="cursor:pointer" title="点击打开 pixiv 作者主页" onclick="window.open('https://www.pixiv.net/users/${esc(a.author_id)}','_blank')">
    ${avatar}
    <div style="flex:1">
      <div class="a-name">${esc(a.name)} <span style="color:var(--muted);font-weight:400;font-size:12px">#${esc(a.author_id)}</span></div>
      <div class="a-meta">粉丝 ${esc(a.followers ?? '-')} · 关注 ${esc(a.following ?? '-')}${a.twitter ? ' · ' + esc(a.twitter) : ''}</div>
      ${a.bio ? `<div class="a-bio">${esc(a.bio)}</div>` : ''}
    </div>
    ${withButton ? `<div class="a-actions">
      ${withDetail ? `<button class="btn ghost" onclick="event.stopPropagation();openAuthorDetail('${esc(a.author_id)}')" title="在站内看这位画师的作品列表（和直接输入作者 ID 一样）">📄 查看详情</button>` : ''}
      <button class="btn ghost" data-follow-aid="${esc(a.author_id)}" onclick="event.stopPropagation();toggleFollowAuthor('${esc(a.author_id)}', this)" title="关注这位画师（pixiv 账号；已关注时点一下可取消）">♡ 关注</button>
      <button class="btn accent" data-cart-author="${esc(a.author_id)}" onclick="event.stopPropagation();addAuthorToCart(${JSON.stringify(a).replace(/"/g, '&quot;')})">➕ 全部作品加入清单</button>
    </div>` : ''}
  </div>`;
}

// ===== 关键词/作品结果 → 某位作者的「详情」 =====
// 详情页 = 与「直接输入作者 ID」完全同一条路 (scope=author + 作者 ID 查询 → type 'author'),
// 所以作品列表/翻批/关注/加清单全都现成。这里只多记一个「返回点」。
// 只记第一次: 在作者页里再点别的作者不该把返回点覆盖成作者页(否则回不去上一个结果页)。
// kind: 'artwork' = 从「按作品 ID 直查」的作者卡进来; 'keyword' = 从关键词结果的作者卡进来。
let authorDetailBack = null;   // { q, off, scope, kind } | null

window.openAuthorDetail = function (authorId) {
  const aid = String(authorId || '').trim();
  if (!aid) return;
  if (!authorDetailBack) {
    const cached = searchCache[searchScope] || {};
    const prevRes = cached.res || {};
    authorDetailBack = {
      q: cached.q || $('#search-input').value.trim(),
      off: Math.max(0, parseInt(prevRes.works_offset || 0, 10) || 0),
      scope: searchScope,
      // 上一次结果是「按作品 ID 直查」的话带 artwork 字段; 关键词查询是 works + 相关作者
      kind: prevRes.artwork ? 'artwork' : 'keyword',
    };
  }
  $('#search-input').value = aid;
  switchTab('search');
  searchScope = 'author';
  $$('.sstab[data-scope]').forEach(b => b.classList.toggle('active', b.dataset.scope === 'author'));
  doSearch(aid);
};

window.backToKeywordResults = async function () {
  const back = authorDetailBack;
  authorDetailBack = null;
  if (!back || !back.q) return;
  searchScope = back.scope || 'author';
  $$('.sstab[data-scope]').forEach(b => b.classList.toggle('active', b.dataset.scope === searchScope));
  $('#search-input').value = back.q;
  await doSearch(back.q, { offset: back.off });
};

// 详情页顶部的返回条 (只在「从关键词/作品结果点进来」时才出现)
function authorBackBarHtml() {
  if (!authorDetailBack) return '';
  const b = authorDetailBack;
  const isWork = b.kind === 'artwork';
  return `<div class="back-bar">
    <button class="btn ghost" onclick="backToKeywordResults()">${isWork ? '‹ 返回作品结果' : '‹ 返回关键词结果'}</button>
    <span class="hint">${isWork ? `作品 #${esc(b.q)} 的作者` : `关键词「${esc(b.q)}」的作者列表`}</span>
  </div>`;
}

// ===== 关注画师  =====
// 状态不预取也行: 点一下先查当前关注状态, 已关注→二次确认后取关, 未关注→直接关注。
// 这样绝不会出现"我以为在关注, 结果把人家取关了"。
function paintFollowBtn(btn, followed) {
  if (!btn) return;
  const icon = followed ? '♥' : '♡';
  const iconOnly = btn.dataset.followIcon === '1';
  btn.textContent = iconOnly ? icon : (icon + (followed ? ' 已关注' : ' 关注'));
  btn.classList.toggle('accent', !followed);
  btn.title = followed ? '已关注（点击可取消关注）' : '关注这位画师（pixiv 账号）';
}

window.toggleFollowAuthor = async function (authorId, btn) {
  if (!authorId) return;
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  try {
    const st = await api('/api/pixiv/follow-status?author_id=' + encodeURIComponent(authorId));
    if (st && st.ok === false) throw new Error(st.error || '查不到关注状态');
    const followed = !!(st && st.followed);
    if (followed && !confirm(`取消关注这位画师？\n\npixiv 用户 ${authorId}\n（取消后其新作品不再出现在关注动态里）`)) return;
    const r = await api('/api/pixiv/follow', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ author_id: String(authorId), follow: !followed }),
    });
    if (r && r.ok === false) throw new Error(r.error || '操作失败');
    const now = !!(r && r.followed);
    toast(now ? `✅ 已关注 pixiv 用户 ${authorId}` : `↩ 已取消关注 ${authorId}`, 'ok');
    document.querySelectorAll(`[data-follow-aid="${authorId}"]`)
      .forEach(b => paintFollowBtn(b, now));
  } catch (e) {
    toast('关注操作失败: ' + (e.message || e), 'err');
  } finally { if (btn) btn.disabled = false; }
};

// 预取关注状态 (只对"确定到某一位作者"的结果用, 见 renderSearch 末尾)
async function hydrateFollowButtons(root, enabled) {
  if (!enabled || !root) return;
  const btns = Array.from(root.querySelectorAll('[data-follow-aid]'));
  for (const b of btns) {
    if (b.dataset.followInit) continue;
    b.dataset.followInit = '1';
    try {
      const st = await api('/api/pixiv/follow-status?author_id='
        + encodeURIComponent(b.dataset.followAid));
      if (st && st.ok) paintFollowBtn(b, !!st.followed);
    } catch (e) { /* 未登录/网络失败: 保持默认「♡ 关注」, 点一下会给出明确提示 */ }
  }
}

// 文件缺失时的缩略图兜底: 优先显示删前的稳定缓存缩略图 (能认出删的是什么),
// 只有缓存也不存在才替换成灰色「缺失」占位
window.libThumbMissing = function (el) {
  const wrap = document.createElement('div');
  wrap.className = 'thumb thumb-missing';
  wrap.title = '本地文件已缺失，且没有缓存的缩略图';
  wrap.innerHTML = '⚠<span>缺失</span>';
  el.replaceWith(wrap);
};

// 文件大小人类可读 (字节 → KB/MB/GB), 空值返回空串
function fmtSize(bytes) {
  if (bytes == null || isNaN(bytes) || bytes < 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(2) + ' MB';
  return (bytes / 1073741824).toFixed(2) + ' GB';
}

// 安全模式下的 R18 打码: 给缩略图加模糊 class + 🔞 徽标 (避免公共场合尴尬)
function r18BlurCls(w) { return (safemode && w && w.is_r18) ? ' blur-r18' : ''; }
function r18FlagHtml(w) { return (safemode && w && w.is_r18) ? '<div class="r18-flag">🔞</div>' : ''; }
// 「作品页已失效」= 数据库 deleted_at 字段 (pixiv 404 时自动记录), 与标题文案无关
function deletedTagHtml(w) {
  return (w && w.deleted)
    ? '<div class="tag deleted" title="作品页在 pixiv 已确认不存在">🗑️ 原作品已失效</div>'
    : '';
}
// ℹ️ 版本与构建信息: 用于确认当前跑的是不是最新打包版 
async function loadVersionInfo() {
  const el = $('#settings-version');
  if (!el) return;
  let v = null;
  try { v = await api('/api/version'); } catch (e) { /* 忽略 */ }
  if (!v || v.error) {
    el.innerHTML = '<span class="err-text">版本信息读取失败</span>';
    return;
  }
  // 前端资源版本串读当前页面的 script 标签 (服务端会注入成「手写标签-mtime指纹」)。
  // 只显示前面的**手写标签**(如 20260920j): 指纹尾巴是给浏览器做缓存失效用的,
  // 两个文件 mtime 相同时会重复成 `20260920j-1789967658-1789967658`, 摆给人看没意义
  //。
  const src = (document.querySelector('script[src*="app.js?v="]') || {}).src || '';
  const m = src.match(/[?&]v=([^&]+)/);
  const feTag = (m ? m[1] : '').split('-')[0];
  el.innerHTML =
    `后端构建时间：<b>${esc(v.built_at || '未知')}</b>`
    + `　·　运行方式：${v.frozen ? '打包版 (exe)' : '源码运行'}`
    + `<br>前端资源版本：<b>${esc(feTag || '(未知)')}</b>`
    + `　·　Python ${esc(v.python || '')}`;
}

// 安全模式状态: 从 /api/safemode 加载, 设置页可切换 (写回 user.json 持久化)
// 首次加载标志: 打码态"真的变了"才需要重画缩略图视图, 首次加载不算(列表本来就要画)
let _safemodeInited = false;
async function loadSafemode() {
  const prev = safemode;
  try {
    const r = await api('/api/safemode');
    safemode = !!r.enabled;
  } catch (e) { safemode = false; }
  const st = $('#safemode-state');
  const btn = $('#safemode-toggle');
  if (st) st.textContent = safemode ? '🛡 已开启（R18 已打码）' : '未开启';
  if (btn) { btn.textContent = safemode ? '🛡 关闭' : '🛡 开启'; btn.classList.toggle('danger', safemode); }
  // 打码态一变, **所有**画缩略图的视图都得按新状态重画。放在这里(而不是各自调用方)
  // 是为了覆盖全部路径: 手动切换 / SSE 从别处改了设置 / 未来别的新入口。
  // 
  if (_safemodeInited && prev !== safemode) await refreshAllThumbViews();
  _safemodeInited = true;
  return safemode;
}
async function toggleSafemode() {
  const btn = $('#safemode-toggle');
  // 防重入: 一次切换要"改配置 → 重读状态 → 重画所有缩略图", 期间连点会来回反复切
  // (浏览器实测: 连点三次才看到按钮变化)。禁用按钮, 完成后恢复。
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  const next = !safemode;
  try {
    await api('/api/safemode', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: next }) });
    toast(next ? '🛡 安全模式已开启（R18 内容打码）' : '安全模式已关闭', 'ok');
    // 不在这里预先改 safemode: 交给 loadSafemode 检测"变了"并统一重画所有缩略图视图
    await loadSafemode();
  } catch (e) { toast('设置失败: ' + e.message, 'err'); }
  finally { if (btn) btn.disabled = false; }
}

// ===== 下载后自动收藏 : 设置页开关 + 公开/非公开 =====
let autoBookmark = false;

async function loadPixivActions() {
  const st = $('#autobook-state');
  if (!st) return;
  try {
    const r = await api('/api/pixiv-actions');
    autoBookmark = !!r.auto_bookmark;
    const vis = $('#autobook-vis');
    if (vis) vis.value = r.bookmark_private ? 'private' : 'public';
    paintPixivActions(r.logged_in);
  } catch (e) { st.textContent = '读取失败'; }
}

function paintPixivActions(loggedIn) {
  const st = $('#autobook-state'), btn = $('#autobook-toggle');
  if (btn) {
    btn.textContent = autoBookmark ? '♡ 关闭' : '♡ 开启';
    btn.classList.toggle('danger', autoBookmark);
  }
  if (st) {
    st.innerHTML = esc(autoBookmark ? '已开启' : '未开启')
      + (loggedIn === false
        ? '　·　<span class="err-text">未登录 pixiv，登录后才会生效</span>'
        : '');
  }
}

async function toggleAutoBookmark() {
  const btn = $('#autobook-toggle');
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  const next = !autoBookmark;
  try {
    const r = await api('/api/pixiv-actions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_bookmark: next }),
    });
    if (r && r.ok === false) throw new Error(r.error || '保存失败');
    autoBookmark = next;
    toast(next ? '♡ 已开启：单个作品下载成功后会自动收藏到 pixiv'
               : '已关闭自动收藏', 'ok');
    await loadPixivActions();
  } catch (e) { toast('设置失败: ' + (e.message || e), 'err'); }
  finally { if (btn) btn.disabled = false; }
}

async function setBookmarkVisibility(v) {
  const vis = $('#autobook-vis');
  if (vis && vis.disabled) return;
  if (vis) vis.disabled = true;
  try {
    const r = await api('/api/pixiv-actions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bookmark_private: v === 'private' }),
    });
    if (r && r.ok === false) throw new Error(r.error || '保存失败');
    toast(v === 'private' ? '已设为「非公开收藏」（只有自己可见）' : '已设为「公开收藏」', 'ok');
  } catch (e) { toast('设置失败: ' + (e.message || e), 'err'); }
  finally { if (vis) vis.disabled = false; }
}

// ===== 启动与托盘 (2026-09-24): 托盘常驻 / 自动开浏览器 / 壁纸小控件等开关 =====
// 键集与后端 utils/config.py 的 LAUNCHER_DEFAULTS 一一对应, 界面文案写在这里,
// 免得新增一个开关要同时改 HTML 的按钮和 JS 的状态表。
const LAUNCHER_KEYS = {
  tray_enabled:           { on: '🖥 托盘常驻',           off: '🖥 托盘常驻' },
  auto_open_browser:      { on: '🌐 启动自动开浏览器',   off: '🌐 启动自动开浏览器' },
  wall_widget:            { on: '🖼️ 桌面壁纸小控件',     off: '🖼️ 桌面壁纸小控件' },
  notify_on_wall_change:  { on: '🔔 壁纸切换提示',       off: '🔔 壁纸切换提示' },
  exit_on_browser_close:  { on: '🚪 关页面即退出',       off: '🚪 关页面即退出' },
};
let launcherState = {};

async function loadLauncher() {
  const box = $('#launcher-switches');
  if (!box) return;
  try { launcherState = await api('/api/launcher'); }
  catch (e) { launcherState = {}; }
  paintLauncher();
}

function paintLauncher() {
  const box = $('#launcher-switches');
  if (!box) return;
  box.querySelectorAll('button[data-k]').forEach(btn => {
    const k = btn.dataset.k, meta = LAUNCHER_KEYS[k] || {};
    const on = !!launcherState[k];
    btn.textContent = on ? (meta.on || k) : (meta.off || k);
    btn.classList.toggle('accent', on);      // 亮起 = 已开启
    btn.classList.toggle('ghost', !on);
  });
  const st = $('#launcher-state');
  if (st) {
    const onNames = Object.keys(LAUNCHER_KEYS).filter(k => launcherState[k])
      .map(k => (LAUNCHER_KEYS[k].on || k).replace(/^\S+\s*/, ''));
    st.textContent = onNames.length ? ('已开启：' + onNames.join('、')) : '全部关闭';
  }
}

async function toggleLauncher(key) {
  if (!(key in LAUNCHER_KEYS)) return;
  const btn = $(`#launcher-switches button[data-k="${key}"]`);
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  const next = !launcherState[key];
  try {
    const r = await api('/api/launcher', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: next }),
    });
    if (r && r.ok === false) throw new Error(r.error || '保存失败');
    launcherState[key] = next;
    paintLauncher();
    // 2026-10-20: 除「启动自动开浏览器」外都在存盘后立刻生效 (后端 main._apply_launcher
    // 回报 applied)。只有这一项是启动动作, 如实说清它下次启动才起作用。
    const name = (LAUNCHER_KEYS[key].on || key) + (next ? ' 已开启' : ' 已关闭');
    const eff = (r && r.applied) ? r.applied[key] : '';
    if (eff === 'failed') toast(name + '，但没能立刻生效，重启 Vixiq 后生效', 'err');
    else if (key === 'auto_open_browser') toast(name + '（下次启动生效）', 'ok');
    else if (key === 'tray_enabled' && !next)
      // 托盘一关, 右下角就没有退出入口了 —— 顺嘴指一下还剩哪两个 (不是解释"为什么", 是告诉他去哪儿点)
      toast(name + '（已立即生效）；之后从页面「退出程序」或 Ctrl+C 退出', 'ok');
    else toast(name + '（已立即生效）', 'ok');
  } catch (e) { toast('设置失败: ' + (e.message || e), 'err'); }
  finally { if (btn) btn.disabled = false; }
}

// ===== 壁纸夹 (2026-09-26): 「上一张/下一张/幻灯片」的候选目录 =====
// 未设置 = 跟随当前桌面壁纸所在文件夹 (老口径); 设置了就听用户的。存到 paths.wallpaper_dir。
// 目录**只能由服务端弹系统对话框选** —— 浏览器拿不到用户选中目录的绝对路径
// (<input webkitdirectory> 只给相对路径), 之前让用户手打路径 + 再点保存, 用户实测报障:
// 「选择文件夹为什么要有两个按钮, 一个不就足够了, 而且为什么点了没反应」。
async function loadWallDir() {
  const inp = $('#wall-dir-input'), st = $('#wall-dir-state'), clr = $('#wall-dir-clear');
  if (!inp) return;
  try {
    const r = await api('/api/wallpaper/dir');
    inp.value = r.dir || '';
    if (clr) clr.hidden = !r.dir;      // 没设置过就没有可清除的东西, 不留一个点了没事发生的键
    if (st) st.textContent = r.effective || '（当前没有壁纸，读不到目录）';
  } catch (e) {
    if (st) st.textContent = '读取失败: ' + (e.message || e);
  }
}

async function pickWallDir() {
  const btn = $('#wall-dir-pick');
  if (btn) btn.disabled = true;
  // 这个请求会一直挂着, 直到用户在那个系统对话框里选完或取消。先给一句反馈,
  // 否则对话框要是弹在别的窗口后面, 看着就是「点了没反应」。
  toast('已打开系统「选择文件夹」窗口，请在弹出的窗口中选择', 'ok');
  try {
    const r = await api('/api/wallpaper/dir/pick', { method: 'POST' });
    if (r && r.cancelled) return;      // 取消: 静默, 不要把「什么都没做」包装成一句提示
    // 服务端出错时 api() 因为拿得到 JSON 而**不会抛**, 只返回 {ok:false, error} ——
    // 必须显式判 ok, 否则会把错误体当成功往下走, 拿着 undefined 把输入框清空、
    // 还弹一句「壁纸夹已设为 」(2026-09-26 真机: 端点 500, 用户看到的就是「反而清空了」)。
    if (!r || r.ok !== true) throw new Error((r && r.error) || '服务端没有返回结果');
    const inp = $('#wall-dir-input'), st = $('#wall-dir-state'), clr = $('#wall-dir-clear');
    if (inp) inp.value = r.dir || '';
    if (clr) clr.hidden = !r.dir;
    if (st) st.textContent = r.effective || '（当前没有壁纸，读不到目录）';
    toast('壁纸夹已设为 ' + (r.dir || ''), 'ok');
  } catch (e) { toast('选择文件夹失败: ' + (e.message || e), 'err'); }
  finally { if (btn) btn.disabled = false; }
}

// ===== 📁 路径 
//       以及「数据、配置、缓存要向正常软件看齐, 但别占 C 盘太多」) =====
// 按「体积 × 增量」分层: 图片单独可指定(大且会涨, 建议非系统盘) / 数据根(数据库+日志+缩略图,
// 默认随程序目录, 也可整根搬) / 临时目录(可一键清)。选目录只能由服务端弹系统对话框 ——
// 浏览器拿不到选中目录的绝对路径。改动**要重启**才生效 (utils/paths 有进程内缓存,
// SQLite 连接也已建好), 所以每处都带一句「重启 Vixiq 后生效」。
let pathsState = {};
// 三处可改目录 → 输入框/清除钮/配置键/中文名, 一处定义三处复用
const PATH_EDITABLE = {
  img:  { input: 'path-img-input',  clear: 'path-img-clear',  key: 'img_subdir',         label: '图片目录' },
  data: { input: 'path-data-input', clear: 'path-data-clear', key: 'data_root_override', label: '数据目录' },
  temp: { input: 'path-temp-input', clear: 'path-temp-clear', key: 'temp_root_override', label: '临时目录' },
};

async function loadPaths() {
  try { pathsState = await api('/api/paths'); }
  catch (e) {
    pathsState = {};
    const box = $('#path-positions');
    if (box) box.textContent = '读取失败: ' + (e.message || e);
    return;
  }
  paintPaths();
}

function paintPaths() {
  const saved = pathsState.saved || {}, eff = pathsState.effective || {},
        sz = pathsState.sizes || {};
  const mbHint = (v) => (v == null ? '' : '（' + v + ' MB）');
  Object.values(PATH_EDITABLE).forEach((it) => {
    const inp = $('#' + it.input), clr = $('#' + it.clear);
    const v = saved[it.key] || '';
    if (inp) inp.value = v;
    if (clr) clr.hidden = !v;      // 没设置过就没有可清除的东西
  });
  const box = $('#path-positions');
  if (!box) return;
  if (!eff.data_root) { box.textContent = '读取失败'; return; }
  const wp = eff.wallpaper_dir || '';
  box.textContent = [
    ['图片目录', eff.img + mbHint(sz.img)],
    ['数据目录', eff.data_root],
    ['数据库', eff.db + mbHint(sz.db)],
    ['日志目录', eff.logs],
    ['缩略图', eff.thumbs + mbHint(sz.thumbs)],
    ['反搜特征库', eff.feature_db + mbHint(sz.feature_db)],
    ['临时目录', eff.temp_root + mbHint(sz.temp)],
    ['壁纸夹', wp || '（未设置，跟随当前壁纸所在文件夹）'],
    ['配置文件', eff.config_file],
    ['程序目录', eff.install_root],
    ['内置资源', eff.resource_root],
  ].map(([k, v]) => k + '  ' + v).join('\n');
}

async function pickPathDir(which) {
  const it = PATH_EDITABLE[which];
  const btn = $('#' + (which === 'img' ? 'path-img-pick'
                      : which === 'data' ? 'path-data-pick' : 'path-temp-pick'));
  if (btn) btn.disabled = true;
  // 对话框会一直挂着直到用户选完 —— 先给一句反馈, 否则弹在别的窗口后面看着像「点了没反应」
  toast('已打开系统「选择文件夹」窗口，请在弹出的窗口中选择', 'ok');
  try {
    const r = await api('/api/paths/pick', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ which }),
    });
    if (r && r.cancelled) { pathsState = r; paintPaths(); return; }   // 取消: 静默
    if (!r || r.ok !== true) throw new Error((r && r.error) || '服务端没有返回结果');
    pathsState = r; paintPaths();
    toast(it.label + '已设为 ' + ((r.saved || {})[it.key] || '') + '（重启 Vixiq 后生效）', 'ok');
  } catch (e) { toast('选择文件夹失败: ' + (e.message || e), 'err'); }
  finally { if (btn) btn.disabled = false; }
}

async function cleanTempDir() {
  const btn = $('#path-temp-clean');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/paths/temp/clean', { method: 'POST' });
    if (!r || r.ok !== true) throw new Error((r && r.error) || '服务端没有返回结果');
    if (r.sizes) pathsState.sizes = r.sizes;
    paintPaths();
    toast('临时目录已清理：删除 ' + (r.removed || 0) + ' 项，释放 ' + (r.freed_mb || 0) + ' MB', 'ok');
  } catch (e) { toast('清理失败: ' + (e.message || e), 'err'); }
  finally { if (btn) btn.disabled = false; }
}

async function clearPathDir(which) {
  const it = PATH_EDITABLE[which];
  const btn = $('#' + it.clear);
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/paths', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [it.key]: '' }),
    });
    if (!r || r.ok !== true) throw new Error((r && r.error) || '服务端没有返回结果');
    pathsState = r; paintPaths();
    toast(it.label + '已回到默认落点（重启 Vixiq 后生效）', 'ok');
  } catch (e) { toast('设置失败: ' + (e.message || e), 'err'); }
  finally { if (btn) btn.disabled = false; }
}

// ===== ✨ 壁纸文案 (2026-09-27): 「测试连接」+「上传当前壁纸」两颗按钮 =====
// 「上传当前壁纸」点一次 = 存下地址/模型/密钥 + 把当前壁纸缩到 512px 连图上传 + 当场拿回
// 「标题 + 氛围句」(后端同时写进缓存, 桌面小控件随即显示)。换壁纸**不自动**再调接口 ——
// 设计约定, 免得每翻一张就烧一次钱。
// 「测试连接」 只验地址/密钥/
// 模型名, 不发图、不要求有当前壁纸 —— 生成失败时先用它把「配置错」和「模型不配合」分开。
// 密钥**不回显**: GET 只给「填过没有 + 后四位」, 输入框留空 = 沿用已存的那把。
let aiState = {};

// 两颗按钮提交的都是同一份表单 (地址/模型/密钥), 后端 save_test 共用一套「表单值优先」口径。
function aiFormBody() {
  return JSON.stringify({
    base_url: ($('#ai-base') || {}).value || '',
    model: ($('#ai-model') || {}).value || '',
    api_key: ($('#ai-key') || {}).value || '',
  });
}

async function loadAiText() {
  if (!$('#ai-base')) return;
  try { aiState = await api('/api/ai'); }
  catch (e) { aiState = {}; }
  const base = $('#ai-base'), model = $('#ai-model'), key = $('#ai-key');
  if (base) base.value = aiState.base_url || '';
  if (model) model.value = aiState.model || '';
  if (key) { key.value = ''; key.placeholder = aiState.has_key
    ? `已保存（尾号 ${aiState.key_tail || '****'}）` : 'API 密钥'; }
  paintAiText();
}

function paintAiText() {
  const st = $('#ai-state');
  if (!st) return;
  if (!aiState.has_key) st.textContent = '还缺 API 密钥';
  else st.textContent = `已配置：${aiState.model || '未填模型'}`;
}

async function runAiText() {
  const btn = $('#ai-run'), st = $('#ai-state');
  if (btn) btn.disabled = true;
  if (st) st.textContent = '正在上传并生成…';
  try {
    const r = await api('/api/ai/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: aiFormBody() });
    if (!r || r.ok !== true) throw new Error((r && r.error) || '生成失败');
    // 生成等了好几秒, 结果**先上屏**再说 —— 表单状态 (密钥尾号等) 后台慢慢刷,
    // 别让一次多余的 GET 挡在标题前面 (2026-09-29)。loadAiText 里的 paintAiText
    // 会拿「已配置:…」盖掉刚显示的标题, 所以刷完再恢复一次。
    const got = `「${r.title || '—'}」 ${r.mood || ''}`;
    if (st) st.textContent = got;
    loadAiText().catch(() => {}).then(() => { if (st) st.textContent = got; });
    toast('壁纸文案已生成并保存', 'ok');
  } catch (e) {
    if (st) st.textContent = '生成失败：' + (e.message || e);
    toast('生成失败: ' + (e.message || e), 'err');
  } finally { if (btn) btn.disabled = false; }
}

async function testAiText() {
  const btn = $('#ai-test'), st = $('#ai-state');
  if (btn) btn.disabled = true;
  if (st) st.textContent = '正在测试连接…';
  try {
    const r = await api('/api/ai/test', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: aiFormBody() });
    if (!r || r.ok !== true) throw new Error((r && r.error) || '测试失败');
    await loadAiText();
    if (st) st.textContent = `连接正常：${r.model || '—'}（${r.ms || 0}ms）`;
    toast('连接正常', 'ok');
  } catch (e) {
    if (st) st.textContent = '连接失败：' + (e.message || e);
    toast('连接失败: ' + (e.message || e), 'err');
  } finally { if (btn) btn.disabled = false; }
}

// ===== 🖼 壁纸库 (2026-09-27): 从图库按规则生成壁纸 =====
// 规则在 core/wallpaper_lib.py (尺寸 + 比例两道门槛, 原样复制不缩放不裁剪)。上千行判定
// 加上几百 MB 的复制, 放在请求里等不起 → 后端后台线程跑, 这里轮询进度。
let wallLibTimer = null;

async function loadWallLib() {
  if (wallLibTimer) { clearTimeout(wallLibTimer); wallLibTimer = null; }
  const st = $('#wall-lib-state');
  if (!st) return;
  let d = null;
  try { d = await api('/api/wallpaper/lib'); }
  catch (e) { st.textContent = '读取失败: ' + (e.message || e); return; }
  wallLibPaint(d);
  if (d.status === 'running') wallLibTimer = setTimeout(loadWallLib, 1000);
}

async function runWallLib() {
  const btn = $('#wall-lib-run');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/wallpaper/lib/scan', { method: 'POST' });
    // 服务端出错时 api() 因为拿得到 JSON 而**不会抛**, 只返回 {ok:false, error} ——
    // 必须显式判 ok, 否则会把错误体当成功 (与 pickWallDir 同一个坑)。
    if (!r || r.ok !== true) throw new Error((r && r.error) || '服务端没有返回结果');
    if (!r.already) toast('开始扫描图库，可先做别的事', 'ok');
    await loadWallLib();
  } catch (e) {
    const st = $('#wall-lib-state');
    if (st) st.textContent = '生成失败：' + (e.message || e);
    toast('生成失败: ' + (e.message || e), 'err');
  } finally { if (btn) btn.disabled = false; }
}

function wallLibPaint(d) {
  const st = $('#wall-lib-state');
  if (!st) return;
  const res = d.result || null;
  if (d.status === 'running') {
    const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
    st.textContent = '生成中 ' + pct + '%（' + d.done + '/' + d.total + '）'
      + (d.current ? ' · ' + d.current : '');
    return;
  }
  if (d.status === 'error') { st.textContent = '生成失败：' + (d.error || ''); return; }
  if (!res) { st.textContent = '还没生成过'; return; }
  let s = '新增 ' + res.added + ' 张 · 壁纸夹里已有 ' + res.skipped_exist + ' 张'
    + '（扫描 ' + res.scanned + ' 张，过门槛 ' + res.passed + ' 张，用时 ' + res.elapsed + 's）';
  const drop = [];
  if (res.skipped_size) drop.push('尺寸不合 ' + res.skipped_size);
  if (res.skipped_ratio) drop.push('比例不合 ' + res.skipped_ratio);
  if (res.skipped_ext) drop.push('格式不支持 ' + res.skipped_ext);
  if (res.missing) drop.push('文件已不在 ' + res.missing);
  if (drop.length) s += ' · 筛掉：' + drop.join('、');
  if ((res.errors || []).length) s += ' · 出错 ' + res.errors.length + ' 项';
  st.textContent = s;
}

async function clearWallDir() {
  try {
    const r = await api('/api/wallpaper/dir', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: '' }),
    });
    if (r && r.ok === false) throw new Error(r.error || '保存失败');
    const inp = $('#wall-dir-input'), st = $('#wall-dir-state'), clr = $('#wall-dir-clear');
    if (inp) inp.value = '';
    if (clr) clr.hidden = true;
    if (st) st.textContent = r.effective || '（当前没有壁纸，读不到目录）';
    toast('已改回跟随当前壁纸所在文件夹', 'ok');
  } catch (e) { toast('清除失败: ' + (e.message || e), 'err'); }
}

function renderWorksGrid(works, gallery = false) {
  const typeMap = {0: '插画', 1: '漫画', 2: '动图'};
  return `<div class="works-grid">` + works.map(w => {
    // 原网页 URL: 优先 DB 记录的 source_url (外来源归档的直达链接), 否则 pixiv 作品的确定链接
    const origUrl = w.source_url || w.pixiv_url || '';
    return `
    <div class="work-card${w.file_missing ? ' is-missing' : ''}" data-key="${esc(w._key)}"${origUrl ? ` data-url="${esc(origUrl)}"` : ''} data-aid="${esc(w.artwork_id)}" data-r18="${w.is_r18 ? '1' : '0'}">
      ${gallery ? `<label class="card-sel" title="Shift+点击可范围多选"><input type="checkbox" data-key="${esc(w._key)}" ${libSelected.has(w._key) ? 'checked' : ''}></label>` : ''}
      ${w.file_missing
        ? `<img class="thumb" src="${imgSrc((w.image_urls || [])[0], w)}" alt=""
               title="本地文件已缺失（显示删前的缓存缩略图）"
               onerror="libThumbMissing(this)">`
        : `<img class="thumb${r18BlurCls(w)}" src="${imgSrc((w.image_urls || [])[0], w)}" alt=""
               onerror="this.style.visibility='hidden'">${r18FlagHtml(w)}`}
      <div class="w-body">
        ${w.file_missing ? '<div class="tag missing">⚠ 文件缺失</div>' : ''}${deletedTagHtml(w)}
        ${w.source && w.source !== 'pixiv' ? `<div class="tag">🌐 ${esc(sourceLabel(w.source))}${w.source_author ? ` · @${esc(w.source_author)}` : ''}</div>` : ''}
        <div class="w-title">${esc(w.title)}</div>
${authorCellHtml(w, gallery)}
        <div class="w-meta">${esc(typeMap[w.illust_type] || w.illust_type || '')}${w.is_r18 ? ' · R18' : ''}</div>
        <div class="w-pages" title="${esc(pageNumsTitle(w))}">${pageDetailHtml(w)}</div>
        <div class="w-file">${(w.width && w.height) ? esc(w.width + '×' + w.height) : ''}${w.file_size ? ((w.width && w.height ? ' · ' : '') + fmtSize(w.file_size)) : ''}</div>
        <div class="w-tags">${(w.tags || []).slice(0, 3).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>
        <div class="w-link">${origUrl ? `<a class="btn ghost" target="_blank" rel="noopener" href="${esc(origUrl)}" title="打开作品原网页">🔗 原网页</a>` : ''}</div>
        <div class="w-path${w.local_path ? '' : ' none'}" title="${esc(w.local_path || '无本地文件')}"
             ${w.local_path ? `onclick="openFolder('${esc(w.artwork_id)}')" data-aid="${esc(w.artwork_id)}"` : ''}>
          ${w.local_path ? `📁 ${esc(w.local_path)}` : '📁 无本地文件'}
        </div>
        <div class="w-actions${gallery ? ' has-icons' : ''}">
          ${cartActionHtml(w)}
          ${gallery ? `<div class="w-icon-grid">${onlyThisWorkBtnHtml(w)}<button class="btn ghost icon-btn" title="编辑" onclick="openLibEdit('${esc(w.artwork_id)}')">✏️</button>
          <button class="btn ghost icon-btn" title="编辑标签" onclick="openLibTags('${esc(w.artwork_id)}')">🏷️</button>
          <button class="btn danger icon-btn" title="删除" onclick="libDeleteOne('${esc(w.artwork_id)}')">🗑️</button></div>` : `<button class="btn ghost" title="跳转到该作者（搜索作者）" onclick="jumpAuthor('${esc(w.author_id)}')">👤</button>`}
        </div>
      </div>
    </div>`;
  }).join('') + `</div>`;
}

// 「这张是不是附加页」的统一判据 —— 一律走**位置**，不看当初的记录 (2026-09-17 
// 「删页 + 随意调序后显示有 bug」)。原网页只有 0..total_pages-1 这几页，超出的才是附加页；
// `pages.origin` 只是"这张图当初从哪来"的痕迹，而页号会被调序/填空位换来换去 —— 拿它当
// 显示判据就会出现「p1*（附加）」这种一看就不对的标记（真实用例：94992683 原 2 页却把 p1
// 标成附加、114518913 原 2 页却把 p2 标成原网页页）。页数未知(外来源作品)时才退回看 origin。
function pageIsExtra(w, p) {
  if (!p) return false;
  const tp = Number((w && (w.total_pages || w.page_count)) || 0);
  if (tp > 0 && p.page_num != null) return Number(p.page_num) >= tp;
  return p.origin === 'merged';
}

// 作品卡片「原 N 页 · 库内哪几张」行 (作品视图): 页数来自元数据, 库内页码来自 pages 清单。
// 附加页 = 超出原网页页数的那几张 (按页号位置判定, 见 pageIsExtra) → 页码后打 * 标出。
function pageDetailHtml(w) {
  const total = (w.page_count != null && w.page_count !== '') ? w.page_count : (w.total_pages != null ? w.total_pages : null);
  const pages = (w.pages || []).slice().sort((a, b) => a.page_num - b.page_num);
  const totalTxt = total != null ? `原 ${total} 页` : '页数未知';
  if (!pages.length) return `${totalTxt} · 库内 0 张`;
  const cap = pages.length > 12;
  const mergedCnt = pages.filter(p => pageIsExtra(w, p)).length;
  const nums = pages.slice(0, 12).map(p => 'p' + p.page_num + (pageIsExtra(w, p) ? '*' : '')).join(', ') +
    (cap ? ' …' : '') + (mergedCnt ? `（${mergedCnt} 附加）` : '');
  return `${totalTxt} · 库内 ${nums}`;
}
function pageNumsTitle(w) {
  const pages = (w.pages || []);
  const nums = pages.map(p => 'p' + p.page_num + (pageIsExtra(w, p) ? '* 合并附加' : '')).join(', ');
  return pages.length ? `库内页: ${nums}` : '库内暂无图片';
}

// 作品卡/行上的「👁️ 仅查看该作品」按钮 (图库页专用)
function onlyThisWorkBtnHtml(w) {
  const on = libOnlyAid === String(w.artwork_id);
  return `<button class="btn ghost icon-btn${on ? ' active' : ''}"
    title="仅查看该作品：筛选结果只显示这个作品，并自动切换为图片视图平铺展示（顶部可一键撤销，回到作品视图并定位到这里）"
    onclick="libOnlyThisWork('${esc(w.artwork_id)}')">👁️</button>`;
}

// 作者显示: 图库卡片上单击 = 在库中筛选该作者 (用户 2026-09-16 要求); 查询页卡片上单击 = 搜 pixiv 作者
// 双击 = 有 pixiv PID 时直接打开作者主页。260ms 延迟用于区分单击/双击。
let _authorClickTimer = null;
function authorLabel(w) { return w.author_name || w.author_id || '未知作者'; }
function authorCellHtml(w, gallery) {
  const txt = esc(authorLabel(w));
  // 作者存续状态徽标 (图库卡片/列表): 活跃中 / 已注销 —— 来自 authors.status (自检/定向刷新时联网核对)。
  // 只在**核对过**时才挂: 未核对(NULL)与「未知」都不显示, 否则每张卡都多一个灰标签变噪声。
  const stTag = (gallery && (w.author_status === 'alive' || w.author_status === 'gone'))
    ? authorStatusTag(w.author_status) : '';
  if (!w.author_id) return `<div class="w-author" title="无作者信息">${txt}</div>`;
  const isPixiv = /^\d+$/.test(String(w.author_id));   // 纯数字 ID = pixiv 作者 PID
  const tip = gallery
    ? (isPixiv ? '单击：在图库中筛选该作者 · 双击：打开 pixiv 作者主页' : '单击：在图库中筛选该作者')
    : (isPixiv ? '单击：搜索该作者 · 双击：打开 pixiv 作者主页' : '单击：搜索该作者');
  return `<div class="w-author" data-name="${txt}" title="${tip}"
    onclick="${gallery ? 'authorClickLib' : 'authorClick'}('${esc(w.author_id)}', event)"
    ${isPixiv ? `ondblclick="authorDblClick('${esc(w.author_id)}', event)"` : ''}>${txt}${stTag}</div>`;
}

// 作者名单击 → 跳「搜索作者」并查询 (延迟以区分双击, 双击不触发搜索)
window.authorClick = function (authorId, e) {
  if (e) e.stopPropagation();
  if (_authorClickTimer) { clearTimeout(_authorClickTimer); _authorClickTimer = null; }
  _authorClickTimer = setTimeout(() => { _authorClickTimer = null; jumpAuthor(authorId); }, 260);
};
// 图库里的作者名单击 → 在图库中筛选该作者 (名字先取出来: 延时后 e.currentTarget 已失效)
window.authorClickLib = function (authorId, e) {
  if (e) e.stopPropagation();
  const nm = (e && e.currentTarget && e.currentTarget.dataset)
    ? (e.currentTarget.dataset.name || '') : '';
  if (_authorClickTimer) { clearTimeout(_authorClickTimer); _authorClickTimer = null; }
  _authorClickTimer = setTimeout(() => {
    _authorClickTimer = null;
    libFilterByAuthor(authorId, nm);
  }, 260);
};
// 作者名双击 → 直接打开 pixiv 作者主页 (仅当作者有 pixiv PID 时绑定此事件)
window.authorDblClick = function (authorId, e) {
  if (e) e.stopPropagation();
  if (_authorClickTimer) { clearTimeout(_authorClickTimer); _authorClickTimer = null; }
  window.open('https://www.pixiv.net/users/' + authorId, '_blank');
};

function jumpAuthor(authorId) {
  authorDetailBack = null;   // 从别处直接跳作者页 → 没有「关键词结果」可返回
  $('#search-input').value = authorId;
  // 跳到「搜索」主标签页 (否则在图库页点击只在隐藏的搜索页执行, 用户看不到反应)
  switchTab('search');
  // 跳到「搜索作者」子页并查该作者 (给出明确跳转动作, 强制重新查询)
  searchScope = 'author';
  $$('.sstab[data-scope]').forEach(b => b.classList.toggle('active', b.dataset.scope === 'author'));
  doSearch(authorId);
}

// 📚 查看该作者的图片 :
//   以前点作者名是跳「查询」页去 pixiv 搜 —— 但我想看的往往是自己库里存了什么。
//   现在改成: 切到图库页 + 按该作者精确筛选, 并清掉会把他作品挡住的筛选条件。
//   第二参传元素(this, 读 data-name) 或直接传名字字符串 —— 名字里的引号走 data 属性, 不会破坏 onclick。
window.libFilterByAuthor = function (authorId, elOrName) {
  const aid = String(authorId || '').trim();
  if (!aid) return;
  const label = (typeof elOrName === 'string'
    ? elOrName.trim()
    : ((elOrName && elOrName.dataset && elOrName.dataset.name) || '').trim()) || aid;
  switchTab('library');
  libFilters.author = aid;
  libFilters.missing = false;
  libFilters.source = ''; libFilters.type = ''; libFilters.tag = ''; libFilters.ai = '';
  ['#lib-f-missing', '#lib-f-missing-file', '#lib-f-deleted']
    .forEach(id => { const el2 = $(id); if (el2) el2.classList.remove('active'); });
  ['#lib-f-source', '#lib-f-type', '#lib-f-ai']
    .forEach(id => { const el2 = $(id); if (el2) el2.value = ''; });
  // 清空搜索框 + 标签框, 并把作者框回填成这位作者 —— 全部走 combo 自己的方法,
  // **不能派发假 input 事件**: input 事件是下拉面板的展开触发器, 会一并把
  // 作者/标签两个下拉框弹开。
  if (comboTag) comboTag.clear();
  const tagEl = $('#lib-f-tag-input');
  if (tagEl) { tagEl.value = ''; if (tagEl._clearSync) tagEl._clearSync(); }
  const q = $('#lib-search-input');
  if (q) { q.value = ''; if (q._clearSync) q._clearSync(); }
  if (comboAuthor) comboAuthor.setByValue(aid, label);
  const ai = $('#lib-f-author-input');
  if (ai) { ai.value = label; if (ai._clearSync) ai._clearSync(); }
  libDataView = 'artwork';
  libSelected.clear(); _libAnchor = null;
  loadLibrary({ resetPage: true }).then(() => {
    // 库里有作品才提示"筛出来了"; 没有则如实说没有
    const items = ($('#lib-result')._lastItems || []);
    if (items.length) toast(`已在图库筛选出「${label}」的作品 ${libTotal} 个`, 'ok');
    else toast(`库里没有「${label}」的作品`, 'info');
  });
};

// 加入清单后自动开始下载 (用户无需再去任务页点按钮)
async function autoStartDownload() {
  try { await api('/api/cart/download', { method: 'POST' }); } catch (e) { /* 已在跑则忽略 */ }
  refreshCart();
}

// ============ pixez 式下载状态回显 (2026-09-15) ============
// 作品: downloaded(已完成,按钮禁用) / in_queue(下载列表中,禁用) /
//       partial(在库但缺页,文案变「补全页数」仍可点) /
//       expired(原站已失效404,改成可点的「搜索来源」) / none(保持加入清单)
// 作者: 仅 in_queue 一种拦截态。
// 文案与图标集中一份, 初始渲染(cartActionHtml)与异步回显(_applyWorkCartState)共用,
// 避免两处各写一份导致状态切换时文字/图标跳变。
const CART_BTN = {
  downloaded: { text: '✅ 已入库', title: '该作品已完整入库，无需重复下载' },
  in_queue: { text: '⏳ 已在下载列表', title: '已在下载任务列表中，请勿重复添加' },
  partial: { text: '🧩 补全页数', title: '本地有该作品但不完整，加入清单只补缺失的页' },
  none: { text: '➕ 加入清单', title: '加入下载清单并开始下载' },
  source: { text: '🔎 搜索来源', title: '有 URL 直接归类站点；无 URL 自动反搜（本地图走 SauceNAO）' },
  expired: { text: '🔎 搜索来源', title: '该作品在原站已失效（404），无法再补下；可搜索其他来源' },
};

function _setCartBtnDisabled(btn, text, title) {
  btn.disabled = true;
  btn.textContent = text;
  if (title) btn.title = title;
  btn.classList.remove('accent');
  btn.classList.add('ghost');
  btn.removeAttribute('onclick');
}

function _applyWorkCartState(btn, state) {
  const cfg = CART_BTN[state];
  if (!cfg) return;
  if (state === 'expired') {
    // 已失效: 就地换成可点的「🔎 搜索来源」, 与 cartActionHtml 的 canRedownload=false 分支一致
    const aid = btn.dataset.cartAid || '';
    btn.removeAttribute('data-cart-aid');   // 摘掉标记, 避免后续再被装饰回下载态
    btn.disabled = false;
    btn.textContent = cfg.text;
    btn.title = cfg.title;
    btn.classList.remove('accent');
    btn.classList.add('ghost');
    btn.setAttribute('onclick', `sourceSearch('${aid}')`);
    return;
  }
  if (state === 'downloaded' || state === 'in_queue') {
    _setCartBtnDisabled(btn, cfg.text, cfg.title);
  } else {
    btn.textContent = cfg.text;
    btn.title = cfg.title;
  }
}

// 作品卡「加入清单」按钮的初始渲染 (搜索页/图库网格共用)。
// 搜索接口会带上 dl_state, 所以首屏就能显示正确状态, 不依赖异步装饰。
// cart-main = 卡片主操作按钮的样式钩子 (加粗加大 + 胶囊圆角, 见 style.css)。
function cartActionHtml(w) {
  if (!canRedownload(w)) {
    const cfg = (w && w.deleted) ? CART_BTN.expired : CART_BTN.source;
    return `<button class="btn ghost cart-main" title="${cfg.title}" onclick="sourceSearch('${esc(w.artwork_id)}')">${cfg.text}</button>`;
  }
  const aid = esc(w.artwork_id);
  const click = `onclick="addArtworkToCart(${JSON.stringify(w).replace(/"/g, '&quot;')})"`;
  if (w.dl_state === 'downloaded') {
    return `<button class="btn ghost cart-main" data-cart-aid="${aid}" data-dec="1" disabled title="${CART_BTN.downloaded.title}">${CART_BTN.downloaded.text}</button>`;
  }
  if (w.dl_state === 'in_queue') {
    return `<button class="btn ghost cart-main" data-cart-aid="${aid}" data-dec="1" disabled title="${CART_BTN.in_queue.title}">${CART_BTN.in_queue.text}</button>`;
  }
  if (w.dl_state === 'partial') {
    return `<button class="btn accent cart-main" data-cart-aid="${aid}" ${click} title="${CART_BTN.partial.title}">${CART_BTN.partial.text}</button>`;
  }
  return `<button class="btn accent cart-main" data-cart-aid="${aid}" ${click} title="${CART_BTN.none.title}">${CART_BTN.none.text}</button>`;
}

// 扫描容器内带 data-cart-aid / data-cart-author 且尚未装饰的按钮, 一次批量请求回显
async function decorateCartButtons(root) {
  root = root || document;
  const wBtns = [...root.querySelectorAll('button[data-cart-aid]:not([data-dec])')];
  const aBtns = [...root.querySelectorAll('button[data-cart-author]:not([data-dec])')];
  if (!wBtns.length && !aBtns.length) return;
  wBtns.forEach(b => { b.dataset.dec = '1'; });
  aBtns.forEach(b => { b.dataset.dec = '1'; });
  const wids = [...new Set(wBtns.map(b => b.dataset.cartAid))];
  const aids = [...new Set(aBtns.map(b => b.dataset.cartAuthor))];
  let st = null;
  try {
    st = await api('/api/library/download-status?ids=' + encodeURIComponent(wids.join(','))
      + '&author_ids=' + encodeURIComponent(aids.join(',')));
  } catch (e) { return; }  // 状态回显失败不阻塞原有按钮功能
  const ws = (st && st.works) || {};
  const as = (st && st.authors) || {};
  wBtns.forEach(b => _applyWorkCartState(b, ws[b.dataset.cartAid]));
  aBtns.forEach(b => {
    if (as[b.dataset.cartAuthor] === 'in_queue') {
      _setCartBtnDisabled(b, '⏳ 已在下载列表', '该作者的「全部作品」任务已在下载列表中');
    }
  });
}

// 加入成功后就地把当前页同作品按钮置为「下载中」, 不等重新渲染
function _markAidCartState(aid, state) {
  document.querySelectorAll('button[data-cart-aid]').forEach(b => {
    if (b.dataset.cartAid === String(aid)) _applyWorkCartState(b, state);
  });
}

// 加入清单 (JSON 转义放进 onclick 由后端 JSON 编码保证安全)
window.addArtworkToCart = async function (a) {
  const body = {
    item_type: 'artwork',
    title: `#${a.artwork_id} ${a.title || ''}`,
    author_id: a.author_id || null,
    author_name: a.author_name || null,
    artwork_id: a.artwork_id,
    page_count: a.page_count || a.total_pages || 1,
  };
  try {
    const r = await api('/api/cart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    // 后端 409 仍返回 JSON (api() 对带 body 的非 2xx 不抛异常): 明确提示, 不开任务
    if (r && r.reason === 'in_queue') { toast('⏳ 该作品已在下载列表中，无需重复添加', 'info'); return; }
    if (r && r.reason === 'already_downloaded') { toast('✓ 该作品已入库！', 'info'); return; }
    toast('已加入清单，自动开始下载 ⬇', 'ok');
    _markAidCartState(a.artwork_id, 'in_queue');
    await autoStartDownload();
  } catch (e) { toast('加入清单失败: ' + e.message, 'err'); }
};

window.addAuthorToCart = async function (a) {
  const body = {
    item_type: 'author_all',
    title: `作者主页全部: ${a.name} (${a.author_id})`,
    author_id: a.author_id,
    author_name: a.name,
  };
  try {
    const r = await api('/api/cart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r && r.reason === 'in_queue') { toast('⏳ 该作者的全部作品任务已在下载列表中，无需重复添加', 'info'); return; }
    toast('已加入清单，自动开始下载 ⬇', 'ok');
    document.querySelectorAll('button[data-cart-author]').forEach(b => {
      if (b.dataset.cartAuthor === String(a.author_id)) {
        _setCartBtnDisabled(b, '⏳ 已在下载列表', '该作者的「全部作品」任务已在下载列表中');
      }
    });
    await autoStartDownload();
  } catch (e) { toast('加入清单失败: ' + e.message, 'err'); }
};

// ============ 下载任务 (独立页) ============
let cartRunning = false;
let cartItems = [];          // 最近一次 /api/cart 全量
let taskFilter = 'all';      // all | running | done | failed
let taskShowThumb = true;
let taskOrderDesc = true;    // true=时间倒序(新在前)

function statusGroup(s) {
  if (s === 'done' || s === 'ok') return 'done';
  if (s === 'failed') return 'failed';
  return 'running';          // pending(待下载)/running(进行中) 都算未完成
}

function fmtTime(iso) {
  if (!iso) return '';
  return String(iso).replace('T', ' ').slice(0, 19).replace(/-/g, '/');
}

async function refreshCart() {
  try {
    const r = await api('/api/cart');
    cartItems = r.items || [];
    $('#cart-badge').textContent = cartItems.length;
    $('#cart-badge').classList.toggle('hidden', cartItems.length === 0);
    renderTaskTable();
  } catch (e) { /* 忽略 */ }
}

function renderTaskTable() {
  let items = cartItems.slice();
  // 排序: 按发起时间
  items.sort((a, b) => {
    const d = (a.added_at || '') < (b.added_at || '') ? -1 : (a.added_at || '') > (b.added_at || '') ? 1 : 0;
    return taskOrderDesc ? -d : d;
  });
  // 四栏过滤
  if (taskFilter !== 'all') items = items.filter(it => statusGroup(it.status) === taskFilter);
  $('#task-total').textContent = `共 ${items.length} 条`;

  const tbody = $('#cart-table tbody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="hint" style="text-align:center">' +
      (cartItems.length ? '当前分栏下没有任务。' : '任务列表是空的，先去「查询」页点「⬇ 下载」吧。') + '</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(it => {
    const thumb = taskShowThumb && (it.thumb_url || (it.artwork_id ? 1 : 0))
      ? `<img class="thumb-sm" src="${it.thumb_url ? imgSrc(it.thumb_url, it) : ''}" alt="" onerror="this.style.visibility='hidden'">`
      : `<div class="thumb-sm placeholder">${esc((it.title || '?')[0]) || ''}</div>`;
    return `
      <tr id="cart-row-${it.id}">
        <td class="cell-thumb">${thumb}</td>
        <td class="task-title">${esc(it.title || '(未命名任务)')}</td>
        <td class="task-author">${esc(it.author_name || '—')}</td>
        <td>${esc(it.item_type === 'artwork' ? '作品' : it.item_type === 'author_all' ? '作者全部' : it.item_type)}</td>
        <td class="cell-progress" id="cart-prog-${it.id}">${progressCell(it.id, it.status)}</td>
        <td class="cell-status">${statusTag(it.status)}</td>
        <td class="mono">${fmtTime(it.added_at)}</td>
        <td class="cell-ops">
          ${it.status === 'failed' ? `<button class="btn ghost" style="padding:4px 8px;font-size:12px" onclick="retryCartItem(${it.id})">↻</button>` : ''}
          <button class="btn danger" style="padding:4px 8px;font-size:12px" onclick="removeCartItem(${it.id})">🗑️</button>
        </td>
      </tr>`;
  }).join('');
}

function progressCell(itemId, status) {
  if (status === 'done') return '✓ 完成';
  if (status === 'failed') return '✖ 失败';
  if (status === 'running') return `<span style="color:var(--accent)">进行中…</span>`;
  return '—';
}

function updateOverallProgress(cur, total, status) {
  const wrap = $('#dl-progress-wrap');
  wrap.classList.remove('hidden');
  const pct = total ? Math.round(cur / total * 100) : 0;
  $('#dl-progress-bar').innerHTML = `<div style="width:${pct}%"></div>`;
  $('#dl-progress-text').textContent = `${cur}/${total} ${status || ''}`;
  if (total && cur >= total) {
    setTimeout(() => { wrap.classList.add('hidden'); refreshCart(); }, 2500);
  }
}

function updateItemProgress(itemId, cur, total, status) {
  const cell = $('#cart-prog-' + itemId);
  if (!cell) return;
  if (status === '完成' || status === 'ok' || status === 'done') cell.textContent = '✓ 完成';
  else if (status === '失败' || status === 'failed') cell.textContent = '✖ 失败';
  else {
    const pct = total ? Math.round(cur / total * 100) : 0;
    cell.textContent = `${pct}% (${cur}/${total}) ${status || ''}`;
  }
  // 同步更新状态列
  const row = $('#cart-row-' + itemId);
  if (row) {
    const st = row.querySelector('.tag-state');
    if (st) {
      if (status === '完成') { st.className = 'tag-state done'; st.textContent = '✓ 完成'; }
      else if (status === '失败') { st.className = 'tag-state failed'; st.textContent = '✖ 失败'; }
      else { st.className = 'tag-state running'; st.textContent = '进行中'; }
    }
  }
}

window.removeCartItem = async function (id) {
  try {
    await api('/api/cart/' + id, { method: 'DELETE' });
    refreshCart();
  } catch (e) { toast('删除失败', 'err'); }
};

// 单条重试 (失败任务)
window.retryCartItem = async function (id) {
  try {
    await api('/api/cart/' + id + '/retry', { method: 'POST' });
    toast('已重新提交任务 #' + id, 'ok');
    refreshCart();
    cartRunning = true;
    $('#dl-run').disabled = true;
    $('#dl-stop').disabled = false;
  } catch (e) { toast('重试失败: ' + e.message, 'err'); }
};

// 重试全部失败任务
async function retryFailedTasks() {
  try {
    const r = await api('/api/cart/retry-failed', { method: 'POST' });
    toast(`已将 ${r.resetted ?? 0} 个失败任务重置并重跑`, 'ok');
    await refreshCart();
    if (r.resetted) {
      cartRunning = true; $('#dl-run').disabled = true; $('#dl-stop').disabled = false;
    }
  } catch (e) { toast('重试失败: ' + e.message, 'err'); }
}

// 清空下载任务: 清哪一栏 = 看当前筛选 (全部/进行中/完成/失败), 与筛选栏共用一套语义。
// 四个 scope 与后端 queue_clear_scope 一一对应; 文案里的「未完成」= pending+running。
// 清掉的行**真的不会被下载** (后端: 每项开跑前认领 + 正在跑的逐页回查) —— 所以文案是
// 「会中止」而不是「不会停下」。
const TASK_CLEAR_SCOPE = {
  all: { label: '全部', warn: true },
  running: { label: '未完成', warn: true },
  done: { label: '已完成', warn: false },
  failed: { label: '失败', warn: false },
};

async function clearTasks() {
  const f = TASK_CLEAR_SCOPE[taskFilter] ? taskFilter : 'all';
  const cfg = TASK_CLEAR_SCOPE[f];
  const n = cartItems.filter(it => f === 'all' || statusGroup(it.status) === f).length;
  if (!n) { toast(`当前分栏下没有可清空的任务`, 'info'); return; }
  let msg = `确认清空「${cfg.label}」的 ${n} 条任务记录？`;
  if (cfg.warn) {
    msg += '\n\n正在下载的任务会被中止（已下好的页保留在图库，以后可以补下）。';
  }
  if (!confirm(msg)) return;
  try {
    const r = await api('/api/cart/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: f }),
    });
    toast(`已清空 ${r.removed ?? 0} 条${cfg.label}任务`, 'ok');
    refreshCart();
  } catch (e) { toast('清空失败: ' + e.message, 'err'); }
}

function setTaskFilter(f) {
  taskFilter = f;
  $$('#task-filters .filter-btn').forEach(b => b.classList.toggle('active', b.dataset.f === f));
  renderTaskTable();
}

function toggleTaskThumb() {
  taskShowThumb = $('#task-show-thumb').classList.contains('active');
  renderTaskTable();
}

function toggleTaskOrder() {
  taskOrderDesc = !taskOrderDesc;
  $('#task-order').textContent = taskOrderDesc ? '↓ 时间倒序' : '↑ 时间正序';
  renderTaskTable();
}

async function runDownload() {
  if (cartRunning) return;
  // 危险策略: 强制重下覆盖本地 → 先弹窗确认
  const stratEl = $('#dl-strategy');
  const strategy = stratEl && stratEl.value === 'overwrite' ? 'overwrite' : 'only_new';
  if (strategy === 'overwrite') {
    const okUrl = confirm('⚠ 强制重下覆盖：将重新下载队列中全部作品的原图，' +
      '覆盖本地同名文件（原图内容以 pixiv 当前版本为准，本地旧版本将丢失）。\n\n确定要继续吗？');
    if (!okUrl) return;
  }
  cartRunning = true;
  $('#dl-run').disabled = true;
  $('#dl-stop').disabled = false;
  try {
    await api('/api/cart/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strategy }),
    });
  } catch (e) { toast('启动下载失败: ' + e.message, 'err'); }
}

async function stopTasks() {
  try {
    await api('/api/tasks/stop', { method: 'POST' });
    toast('已发送停止指令', 'ok');
  } catch (e) { /* 忽略 */ }
}

// 下载结束后恢复按钮状态 (轮询购物车状态)
setInterval(() => {
  if (!cartRunning) return;
  api('/api/cart').then(r => {
    const rows = r.items || [];
    const allDone = rows.every(it => it.status === 'done' || it.status === 'failed');
    if (allDone && rows.length) {
      cartRunning = false;
      $('#dl-run').disabled = false;
      $('#dl-stop').disabled = true;
      refreshCart();
    }
  }).catch(() => {});
}, 2000);

// ============ 图库 ============
async function loadStats() {
  try {
    const s = await api('/api/library/stats');
    $('#lib-stats').innerHTML = `
      <div class="stat-card"><div class="num">${s.total_artworks ?? 0}</div><div class="lbl">作品</div></div>
      <div class="stat-card"><div class="num">${s.total_pages ?? 0}</div><div class="lbl">图片页</div></div>
      <div class="stat-card"><div class="num">${s.total_authors ?? 0}</div><div class="lbl">画师</div></div>
      <div class="stat-card"><div class="num">${fmtSize(s.disk_size_bytes)}</div><div class="lbl">磁盘占用</div></div>`;
    const miss = s.missing_metadata ?? 0;
    const hint = $('#lib-missing-hint');
    if (miss > 0) {
      hint.style.display = '';
      hint.textContent = `⚠ 有 ${miss} 个作品缺标题/标签/缩略图，点【🩺 一键自检】联网补齐（需已登录）。`;
    } else {
      hint.style.display = 'none';
    }
  } catch (e) {
    $('#lib-stats').innerHTML = '<div class="hint">统计读取失败。</div>';
  }
}

let libView = 'grid';            // 图库视图模式: grid 网格 / list 列表
let libDataView = 'artwork';     // 图库数据粒度: artwork 作品视图(一个作品一卡) / image 图片视图(一张图一卡)
let libSelected = new Set();     // 批量选中项的主键: 作品视图=作品ID; 图片视图=「作品ID|页码」(一张图一项)
// 图库默认只显示非 R18 (r18='0'), 需看全部/仅 R18 时可手动切换下拉 (安全模式无关, 始终生效)
// 默认排序 sort='updated' (按修改时间): 刚入库(pages 变动会 bump updated_at)或刚改动的作品都排最前
let libFilters = { q: '', type: '', status: '', source: '', r18: '0', ai: '', author: '', tag: '', missing: false, sort: 'updated', order: 'desc' };
let libPage = 1;                 // 图库当前页码 (从 1 起)
const LIB_PAGE_SIZE = 60;        // 每页固定张数 (网格/列表)
let libTotal = 0, libPages = 0;  // 当前筛选下的总条数/总页数 (供分页栏)
// 「仅查看该作品」特殊筛选 (2026-09-16): 精确 ID 过滤 + 图片视图平铺, 可整体撤销
let libOnlyAid = null;           // 当前正在单独查看的作品 ID (null=未启用)
let libOnlyPrev = null;          // 启用前的快照 (筛选/视图/页码/搜索词/滚动位置), 撤销时还原
let safemode = false;            // 安全模式: R18 缩略图模糊打码 + Lightbox 需解锁 (从 /api/safemode 加载)

// 非 pixiv 来源的显示名 (source 存英文键, 界面显示中文/站点名)。
// 「未知来源」固定排最后: 图库/作者管理筛选下拉重建时也保持它在底部。
const SOURCE_PRESETS = [['pixiv', 'pixiv'], ['danbooru', 'danbooru'], ['twitter', 'X（推特）'],
                        ['bilibili', 'bilibili'], ['ggac', 'GGAC'], ['artstation', 'ArtStation'],
                        ['deviantart', 'DeviantArt'], ['niconico', 'Nico静画'],
                        ['skeb', 'Skeb 约稿'],
                        ['yandere', 'yande.re'], ['konachan', 'Konachan'], ['gelbooru', 'Gelbooru'],
                        ['sankaku', 'Sankaku'], ['other', '未知来源']];
// 反搜命中站点徽标 (与数据库 source 键一致)
const SITE_BADGE = { pixiv: 'Pixiv', twitter: 'Twitter（X）', artstation: 'ArtStation',
                     deviantart: 'DeviantArt', danbooru: 'Danbooru', niconico: 'Nico静画',
                     skeb: 'Skeb',
                     yandere: 'yande.re', konachan: 'Konachan', gelbooru: 'Gelbooru',
                     sankaku: 'Sankaku', safebooru: 'Safebooru',
                     zerochan: 'Zerochan', animepictures: 'Anime-Pictures', drawr: 'drawr',
                     shutterstock: 'Shutterstock', mangadex: 'MangaDex', fakku: 'FAKKU',
                     kemono: 'Kemono', nhentai: 'nHentai', ehentai: 'E-Hentai', bcy: 'bcy.net',
                     medibang: 'MediBang', portalgraphics: 'PortalGraphics', madokami: 'Madokami',
                     doujinshi: 'DoujinshiDB', '2dmarket': '2D-Market' };
const SOURCE_LABELS = Object.fromEntries(SOURCE_PRESETS);
function sourceLabel(s) { return SOURCE_LABELS[s] || s || '未知来源'; }
// 自定义来源 (localStorage 持久化, 编辑/批量弹窗的「＋ 自定义」选择后追加到这里, 可在来源管理删除)
function customSources() {
  try { return JSON.parse(localStorage.getItem('custom_sources') || '[]'); } catch (e) { return []; }
}
// 被「来源管理」隐藏的预设来源键 (只影响下拉显示, 不影响作品数据)
function hiddenSourceSet() {
  try { return new Set(JSON.parse(localStorage.getItem('hidden_sources') || '[]')); } catch (e) { return new Set(); }
}
// 编辑/批量弹窗共用的来源下拉选项: 预设(可隐藏, 去 other) + 自定义 + 「未知来源(other)」固定置底 + 动作项
function sourceOptions(cur) {
  const hidden = hiddenSourceSet();
  // 当前作品正在用的来源即使被隐藏也保留并选中, 防止保存时误改成别的来源
  const presets = SOURCE_PRESETS.filter(([v]) => v !== 'other' && (!hidden.has(v) || v === cur));
  const custom = customSources().filter(s => !hidden.has(s) || s === cur);
  let html = presets.concat(custom.map(s => [s, s])).map(([v, t]) =>
    `<option value="${esq(v)}" ${cur === v ? 'selected' : ''}>${esc(t)}</option>`).join('');
  // 「未知来源」固定排在最底部 (所有真实来源之后), 与筛选下拉口径一致
  html += `<option value="other" ${cur === 'other' ? 'selected' : ''}>未知来源</option>`;
  html += `<option value="__custom__">＋ 自定义来源…</option>`;
  html += `<option value="__manage__">管理来源…</option>`;
  return html;
}
function esq(s) { return String(s == null ? '' : s).replace(/"/g, '&quot;'); }
// 下拉选中「＋ 自定义来源…」→ 弹窗输入新来源名, 记住后回填; 「管理来源…」→ 打开来源管理
async function onSourceCustom(selectEl) {
  const v = selectEl.value;
  if (v === '__manage__') {
    openSourceManager();
    selectEl.value = selectEl.dataset.prev || '';
    return;
  }
  if (v !== '__custom__') return;
  const name = prompt('输入新来源名（英文关键词，如 deviantart / artstation）：', '');
  if (name && name.trim()) {
    const s = name.trim();
    const list = customSources();
    if (!list.includes(s)) { list.push(s); localStorage.setItem('custom_sources', JSON.stringify(list)); }
    setTimeout(() => { selectEl.innerHTML = sourceOptions(s); selectEl.value = s; }, 0);
  } else {
    selectEl.value = '';
  }
}

// ---- 来源管理: 删除自定义来源 / 隐藏-显示预设来源 (下拉即时生效, 不碰作品数据) ----
function openSrcModal(title, bodyHtml) {
  $('#src-modal-title').textContent = title;
  $('#src-modal-body').innerHTML = bodyHtml;
  $('#src-modal').style.display = '';
}
function closeSrcModal() { $('#src-modal').style.display = 'none'; }

async function openSourceManager() {
  if (!facetData.sources) { try { await loadLibFacets(); } catch (e) { /* ignore */ } }
  const custom = customSources();
  const hidden = hiddenSourceSet();
  const customRows = custom.length
    ? custom.map(s => `<tr><td>${esc(sourceLabel(s))}</td><td class="mono">${esc(s)}</td>
        <td><button class="btn mini danger" onclick="deleteCustomSource('${esq(s)}')">🗑️ 删除</button></td></tr>`).join('')
    : '<tr><td colspan="3" class="hint">暂无自定义来源（编辑弹窗选「＋ 自定义来源…」添加，这里可删除）</td></tr>';
  const presetRows = SOURCE_PRESETS.filter(([v]) => v !== 'other').map(([v, t]) => {
    const h = hidden.has(v);
    return `<tr><td>${esc(t)}</td><td class="mono">${esc(v)}</td>
      <td><button class="btn mini ${h ? '' : 'active'}" onclick="toggleHiddenSource('${esq(v)}')">${h ? '👁 显示' : '🙈 隐藏'}</button></td></tr>`;
  }).join('');
  const dbRows = (facetData.sources || []).map(s =>
    `<tr><td>${esc(sourceLabel(s))}</td><td class="mono">${esc(s)}</td>
      <td class="hint">${s === 'other' ? '未知来源 · 固定置底' : '来自作品数据'}</td></tr>`).join('');
  openSrcModal('来源管理', `
    <div class="hint">自定义来源可删除；预设来源可隐藏（只收进下拉，作品数据不动）。</div>
    <div class="tidy-table-wrap"><table class="table">
      <thead><tr><th>自定义来源（可删）</th><th>键</th><th></th></tr></thead><tbody>${customRows}</tbody></table></div>
    <div class="tidy-table-wrap"><table class="table">
      <thead><tr><th>预设来源（可隐藏）</th><th>键</th><th></th></tr></thead><tbody>${presetRows}</tbody></table></div>
    <div class="tidy-table-wrap"><table class="table">
      <thead><tr><th>库内实际使用来源（只读）</th><th>键</th><th></th></tr></thead><tbody>${dbRows || '<tr><td colspan="3" class="hint">暂无</td></tr>'}</tbody></table></div>
    <div class="modal-actions"><button class="btn ghost" onclick="closeSrcModal()">关闭</button></div>`);
}

// 删除自定义来源
function deleteCustomSource(key) {
  const list = customSources().filter(s => s !== key);
  localStorage.setItem('custom_sources', JSON.stringify(list));
  rebuildSourceFilters(facetData.sources || []);
  openSourceManager();   // 刷新管理弹窗
}
// 隐藏/显示预设来源
function toggleHiddenSource(key) {
  const set = hiddenSourceSet();
  if (set.has(key)) set.delete(key); else set.add(key);
  localStorage.setItem('hidden_sources', JSON.stringify([...set]));
  rebuildSourceFilters(facetData.sources || []);
  openSourceManager();
}

// 图片视图卡片用 _key=作品|页码 定位 (同一作品多张页也能精确点开对应大图)
function _libMarkKeys(items) {
  items.forEach(it => {
    it._key = libDataView === 'image'
      ? String(it.artwork_id) + '|' + (it.page_num == null ? 0 : it.page_num)
      : String(it.artwork_id);
  });
  return items;
}

function renderLibrary(items) {
  $('#lib-result')._lastItems = _libMarkKeys(items);
  $('#lib-result').innerHTML = renderLibraryInner(items);
  decorateCartButtons($('#lib-result'));
}

function renderLibraryInner(items) {
  if (!items.length) return '<div class="hint">图库中没有匹配项。</div>';
  if (libDataView === 'image') {
    return libView === 'list' ? renderImageList(items) : renderImageGrid(items);
  }
  return libView === 'list' ? renderWorksList(items, true) : renderWorksGrid(items, true);
}

// ---- 图片视图: 库里所有图片逐张列出 (每页一张卡) ----
function renderImageGrid(items) {
  const typeMap = {0: '插画', 1: '漫画', 2: '动图'};
  return `<div class="works-grid">` + items.map(w => {
    // 原网页 URL (按所属作品): 外来源用 DB 记录的 source_url, pixiv 用确定链接
    const origUrl = w.source_url || w.pixiv_url || '';
    return `
    <div class="work-card${w.file_missing ? ' is-missing' : ''}" data-key="${esc(w._key)}">
      <label class="card-sel" title="选中这张图片（Shift+点击范围多选）"><input type="checkbox" data-key="${esc(w._key)}" ${libSelected.has(w._key) ? 'checked' : ''}></label>
      ${w.file_missing
        ? `<img class="thumb" src="${imgSrc((w.image_urls || [])[0], w)}" alt=""
               title="本地文件已缺失（显示删前的缓存缩略图）"
               onerror="libThumbMissing(this)">`
        : `<img class="thumb${r18BlurCls(w)}" src="${imgSrc((w.image_urls || [])[0], w)}" alt=""
               onerror="this.style.visibility='hidden'">${r18FlagHtml(w)}`}
      <div class="w-body">
        ${w.file_missing ? '<div class="tag missing">⚠ 文件缺失</div>' : ''}${deletedTagHtml(w)}
        ${w.source && w.source !== 'pixiv' ? `<div class="tag">🌐 ${esc(sourceLabel(w.source))}${w.source_author ? ` · @${esc(w.source_author)}` : ''}</div>` : ''}
        <div class="w-title" title="${esc(w.title)}">${esc(w.title)}</div>
${authorCellHtml(w, true)}
        <div class="w-meta">p${esc(w.page_num)} · ${esc(typeMap[w.illust_type] || w.illust_type || '')}${w.is_r18 ? ' · R18' : ''}</div>
        <div class="w-file">${(w.width && w.height) ? esc(w.width + '×' + w.height) : ''}${w.file_size ? ((w.width && w.height ? ' · ' : '') + fmtSize(w.file_size)) : ''}</div>
        <div class="w-link">${origUrl ? `<a class="btn ghost" target="_blank" rel="noopener" href="${esc(origUrl)}" title="打开作品原网页">🔗 原网页</a>` : ''}</div>
        <div class="w-path${w.local_path ? '' : ' none'}" title="${esc(w.local_path || '无本地文件')}"
             ${w.local_path ? `onclick="openFolder('${esc(w.artwork_id)}')" data-aid="${esc(w.artwork_id)}"` : ''}>
          ${w.local_path ? `📁 ${esc(w.local_path)}` : '📁 无本地文件'}
        </div>
        <div class="w-actions">
          ${cartActionHtml(w)}
          <button class="btn danger icon-btn" title="删除这张图片" onclick="libDeletePage('${esc(w.artwork_id)}', ${Number(w.page_num) || 0})">🗑️</button>
          <button class="btn ghost icon-btn" title="跳转到该作者（搜索作者）" onclick="jumpAuthor('${esc(w.author_id)}')">👤</button>
        </div>
      </div>
    </div>`;
  }).join('') + `</div>`;
}

function renderImageList(items) {
  const typeMap = {0: '插画', 1: '漫画', 2: '动图'};
  return `<div class="table-wrap"><table class="table lib-list"><thead><tr>
    <th></th><th>作品ID</th><th>页码</th><th>标题</th><th>作者</th><th>类型</th><th>文件</th><th>R18</th><th>路径</th><th>操作</th>
  </tr></thead><tbody>` + items.map(w => `
    <tr class="${w.file_missing ? 'is-missing' : ''}" data-key="${esc(w._key)}">
      <td class="cell-thumb">
        <label class="card-sel" title="选中这张图片（Shift+点击范围多选）"><input type="checkbox" data-key="${esc(w._key)}" ${libSelected.has(w._key) ? 'checked' : ''}></label>
        ${w.file_missing
          ? `<img class="thumb" src="${imgSrc((w.image_urls || [])[0], w)}" alt="" onerror="libThumbMissing(this)">`
          : `<img class="thumb${r18BlurCls(w)}" src="${imgSrc((w.image_urls || [])[0], w)}" alt="" onerror="this.style.visibility='hidden'">`}
      </td>
      <td class="mono">${esc(w.artwork_id)}</td>
      <td>p${esc(w.page_num)}</td>
      <td class="w-title">${w.source && w.source !== 'pixiv' ? `<span class="tag src-tag">${esc(sourceLabel(w.source))}</span> ` : ''}${w.deleted ? '<span class="tag deleted" title="作品页已确认失效">🗑️ 已失效</span> ' : ''}${esc(w.title)}</td>
      <td class="w-author">${authorCellHtml(w, true)}</td>
      <td>${esc(typeMap[w.illust_type] || w.illust_type || '')}</td>
      <td class="cell-file">${(w.width && w.height) ? esc(w.width + '×' + w.height) : ''}${w.file_size ? ((w.width && w.height ? ' · ' : '') + fmtSize(w.file_size)) : ''}</td>
      <td>${w.is_r18 ? '<span class="tag r18">R-18</span>' : ''}</td>
      <td class="cell-path${w.local_path ? '' : ' none'}" title="${esc(w.local_path || '无本地文件')}"
          ${w.local_path ? `onclick="openFolder('${esc(w.artwork_id)}')"` : ''}>
        ${w.local_path ? '📁 ' + esc(w.local_path) : '—'}
      </td>
      <td class="cell-ops">
        ${(w.source_url || w.pixiv_url) ? `<a class="btn ghost" style="padding:4px 8px;font-size:12px" target="_blank" rel="noopener" href="${esc(w.source_url || w.pixiv_url)}" title="打开作品原网页">🔗</a>` : ''}
        ${canRedownload(w)
          ? `<button class="btn accent" style="padding:4px 8px;font-size:12px" data-cart-aid="${esc(w.artwork_id)}" onclick="addArtworkToCart(${JSON.stringify(w).replace(/"/g, '&quot;')})">➕ 加入清单</button>`
          : `<button class="btn ghost" style="padding:4px 8px;font-size:12px" title="有 URL 直接归类站点；无 URL 自动反搜" onclick="sourceSearch('${esc(w.artwork_id)}')">🔎 搜索来源</button>`}
        <button class="btn danger" style="padding:4px 8px;font-size:12px" title="删除这张图片" onclick="libDeletePage('${esc(w.artwork_id)}', ${Number(w.page_num) || 0})">🗑️</button>
        <button class="btn ghost" style="padding:4px 8px;font-size:12px" onclick="jumpAuthor('${esc(w.author_id)}')">👤</button>
      </td>
    </tr>`).join('') + `</tbody></table></div>`;
}

function setLibView(view) {
  libView = view;
  $$('#lib-view-toggle .view-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  const items = $('#lib-result')._lastItems;
  if (items) {
    // 与 libDataView 联动的正确渲染 (图片视图也走 renderImageGrid/List, 不再误渲染成作品卡)
    $('#lib-result').innerHTML = renderLibraryInner(items);
    decorateCartButtons($('#lib-result'));
  }
}

// 作品/图片 数据粒度切换 (重新请求, 回第 1 页)
function setLibDataView(view) {
  if (view === libDataView) return;
  libDataView = view;
  // 两视图选中粒度不同 (作品 vs 单张图片), 切换时清空, 避免主键语义串味导致误删
  libSelected.clear();
  _libAnchor = null;
  $$('#lib-data-toggle .view-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  loadLibrary();
}

// 图片视图选中项主键 = "作品ID|页码" → 拆成 delete-pages 接口参数
function parsePageKey(k) {
  const s = String(k);
  const i = s.lastIndexOf('|');
  return { artwork_id: s.slice(0, i), page_num: parseInt(s.slice(i + 1), 10) || 0 };
}

function renderWorksList(works, gallery = false) {
  const typeMap = {0: '插画', 1: '漫画', 2: '动图'};
  return `<div class="table-wrap"><table class="table lib-list"><thead><tr>
    <th></th><th>ID</th><th>标题</th><th>作者</th><th>类型</th><th>页数</th><th>文件</th><th>R18</th><th>标签</th><th>路径</th><th>操作</th>
  </tr></thead><tbody>` + works.map(w => {
    const origUrl = w.source_url || w.pixiv_url || '';
    return `
    <tr class="${w.file_missing ? 'is-missing' : ''}" data-key="${esc(w._key)}">
      <td class="cell-thumb">
        ${gallery ? `<label class="card-sel" title="Shift+点击可范围多选"><input type="checkbox" data-key="${esc(w._key)}" ${libSelected.has(w._key) ? 'checked' : ''}></label>` : ''}
        ${w.file_missing
          ? `<img class="thumb" src="${imgSrc((w.image_urls || [])[0], w)}" alt=""
                 title="本地文件已缺失（显示删前的缓存缩略图）"
                 onerror="libThumbMissing(this)">`
          : `<img class="thumb${r18BlurCls(w)}" src="${imgSrc((w.image_urls || [])[0], w)}" alt=""
               onerror="this.style.visibility='hidden'">`}
      </td>
      <td class="mono">${esc(w.artwork_id)}</td>
      <td class="w-title">${w.source && w.source !== 'pixiv' ? `<span class="tag src-tag">${esc(sourceLabel(w.source))}${w.source_author ? ` @${esc(w.source_author)}` : ''}</span> ` : ''}${w.deleted ? '<span class="tag deleted" title="作品页已确认失效">🗑️ 已失效</span> ' : ''}${esc(w.title)}</td>
      <td class="w-author">${authorCellHtml(w, gallery)}</td>
      <td>${esc(typeMap[w.illust_type] || w.illust_type || '')}</td>
      <td title="${esc(pageNumsTitle(w))}">${pageDetailHtml(w)}</td>
      <td class="cell-file">${(w.width && w.height) ? esc(w.width + '×' + w.height) : ''}${w.file_size ? ((w.width && w.height ? ' · ' : '') + fmtSize(w.file_size)) : ''}</td>
      <td>${w.is_r18 ? '<span class="tag r18">R-18</span>' : ''}</td>
      <td class="cell-tags">${(w.tags || []).slice(0, 4).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</td>
      <td class="cell-path${w.local_path ? '' : ' none'}" title="${esc(w.local_path || '无本地文件')}"
          ${w.local_path ? `onclick="openFolder('${esc(w.artwork_id)}')"` : ''}>
        ${w.local_path ? '📁 ' + esc(w.local_path) : '—'}
      </td>
      <td class="cell-ops">
        ${origUrl ? `<a class="btn ghost" style="padding:4px 8px;font-size:12px" target="_blank" rel="noopener" href="${esc(origUrl)}" title="打开作品原网页">🔗</a>` : ''}
        ${canRedownload(w)
          ? `<button class="btn accent" style="padding:4px 8px;font-size:12px" data-cart-aid="${esc(w.artwork_id)}" onclick="addArtworkToCart(${JSON.stringify(w).replace(/"/g, '&quot;')})">➕ 加入清单</button>`
          : `<button class="btn ghost" style="padding:4px 8px;font-size:12px" title="有 URL 直接归类站点；无 URL 自动反搜" onclick="sourceSearch('${esc(w.artwork_id)}')">🔎 搜索来源</button>`}
        ${gallery ? `${onlyThisWorkBtnHtml(w)}<button class="btn ghost" style="padding:4px 10px;font-size:15px" title="编辑" onclick="openLibEdit('${esc(w.artwork_id)}')">✏️</button>
        <button class="btn ghost" style="padding:4px 10px;font-size:15px" title="编辑标签" onclick="openLibTags('${esc(w.artwork_id)}')">🏷️</button>
        <button class="btn danger" style="padding:4px 10px;font-size:15px" title="删除" onclick="libDeleteOne('${esc(w.artwork_id)}')">🗑️</button>` : `<button class="btn ghost" style="padding:4px 8px;font-size:12px" onclick="jumpAuthor('${esc(w.author_id)}')">👤</button>`}
      </td>
    </tr>`;
  }).join('') + `</tbody></table></div>`;
}

// 图库 ↔ 磁盘实时对账: 手动删文件后刷新即反映真实状态
window._libHealth = null;   // 最近一次对账结果 {missing_ids, empty_dirs, corrupt_files, ...}

// 在资源管理器中定位作品首图所在文件夹 (文件管理器式管理)
window.openFolder = async function (artworkId) {
  const items = ($('#lib-result')._lastItems || []);
  const w = items.find(x => String(x.artwork_id) === String(artworkId));
  const p = w && w.local_path;
  if (!p) { toast('该作品没有本地文件', 'err'); return; }
  try {
    await api('/api/library/open-folder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: p })
    });
    toast('已在资源管理器中打开 📂', 'ok');
  } catch (e) { toast('打开文件夹失败: ' + e.message, 'err'); }
};

async function libSync(showToast = false, periodic = false) {
  try {
    // periodic=1: 定时轮询用, 后端 TTL 内不强制全量重扫磁盘 (图库大时避免每 15s 抢磁盘 IO)
    const h = await api('/api/library/sync' + (periodic ? '?periodic=1' : ''), { method: 'POST' });
    window._libHealth = h;
    paintLibHealth();
    if (h && h.new_pages_linked > 0) {
      toast(`📥 发现 ${h.new_pages_linked} 张新图片，已登记入库，正在联网补全标题/标签…`, 'ok');
      loadLibrary(); loadStats(); loadLibFacets();
      // 元数据后台补全后再刷一次, 标题/作者即时出现
      setTimeout(() => { loadLibrary(); loadStats(); loadLibFacets(); }, 15000);
    } else {
      // 手动同步 (用户点「🔄 同步」): 无论有没有新图都刷新当前视图,
      // 让「本地删文件后」的缺失标记/缺失缩略图立即生效, 不用切页
      if (showToast) {
        loadLibrary(); loadStats(); loadLibFacets();
        const miss = h && (h.missing_ids || []).length;
        toast(miss ? `已与磁盘同步 ✓ · ${miss} 个文件缺失（本页已刷新标红）` : '已与磁盘同步 ✓', miss ? 'err' : 'ok');
      }
    }
    return h;
  } catch (e) {
    if (showToast) toast('同步失败: ' + e.message, 'err');
    return null;
  }
}

// 📁 把本地图片搬进图库 —— 先复制入库, 成功后**源文件送回收站**(可还原), 效果是搬动。
// 两个入口 (Windows 通用对话框一次只能"选多张文件"或"选一个文件夹", 没有合一的形态):
//   选文件夹  → 递归其内所有图片
//   选多张图片 → 只导选中的那几张
// 流程: 弹系统选择框(由本地服务端调起, 已带父窗口, 不会再跑到浏览器后面)
//       → dry-run 预览 → 确认 → 执行。
async function importLocalImages(mode) {
  const isFolder = mode === 'folder';
  let pick;
  try {
    pick = await api(isFolder ? '/api/library/pick-folder' : '/api/library/pick-files',
                     { method: 'POST' });
  } catch (e) { toast('无法打开选择框: ' + e.message, 'err'); return; }
  if (!pick || pick.error) { toast((pick && pick.error) || '打开选择框失败', 'err'); return; }
  const body = isFolder ? { path: pick.path } : { paths: pick.paths };
  if (isFolder ? !pick.path : !(pick.paths || []).length) return;   // 用户取消: 静默返回

  let pre;
  try {
    pre = await api('/api/library/import-from-path', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, dry_run: true }),
    });
  } catch (e) { toast('扫描失败: ' + e.message, 'err'); return; }
  if (!pre || pre.error) { toast((pre && pre.error) || '扫描失败', 'err'); return; }
  if (!pre.total) { toast('这里没有可导入的图片', 'info'); return; }

  // 导入前先判定 : 含作品ID 的直接入库, 不用问;
  // 只有"不是标准 P 站作品"的那部分才需要设计约定 —— 转反搜 还是 建自定义作品。
  const nonPixiv = pre.without_id || 0;
  let makeCustom = false;
  if (nonPixiv > 0) {
    const mode = await _askImportMode(pre);
    if (mode === null) return;              // 用户取消
    makeCustom = (mode === 'custom');
  } else {
    const lines = [
      `将从以下位置导入 ${pre.total} 张图片：`,
      pre.path, '',
      `· 全部含作品ID ${pre.with_id} 张 → 直接入库（自动补全标题/标签并归入作者文件夹）`,
    ];
    if (pre.conflicts) lines.push(`· 其中 ${pre.conflicts} 张库内已有同名，将跳过（源文件不动）`);
    lines.push('', '♻ 导入成功后，源文件会被送进回收站（可从回收站还原），确定继续？');
    if (!confirm(lines.join('\n'))) return;
  }

  try {
    const r = await api('/api/library/import-from-path', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, make_custom_artworks: makeCustom }),
    });
    if (!r || r.error) { toast((r && r.error) || '导入失败', 'err'); return; }
    const parts = [];
    if (r.imported && r.imported.length) parts.push(`📥 入库 ${r.imported.length} 张`);
    if (r.custom_created && r.custom_created.length) parts.push(`🖼️ 建自定义作品 ${r.custom_created.length} 个`);
    if (r.redirected && r.redirected.length) parts.push(`🔍 ${r.redirected.length} 张无作品ID已转反搜收件箱`);
    if (r.recycled) parts.push(`♻ 源文件已进回收站 ${r.recycled} 个`);
    if (r.skipped && r.skipped.length) parts.push(`⏭ ${r.skipped.length} 张库内已有，已跳过（源文件未动）`);
    if (r.recycle_failed && r.recycle_failed.length) parts.push(`⚠ ${r.recycle_failed.length} 个源文件没能进回收站（仍在原处）`);
    if (r.failed && r.failed.length) parts.push(`✖ ${r.failed.length} 张复制失败`);
    toast(parts.join(' · ') || '没有需要导入的文件',
          (r.imported && r.imported.length) ? 'ok' : 'info');
    await libSync(); loadLibrary(); loadStats(); loadLibFacets();
  } catch (e) { toast('导入失败: ' + e.message, 'err'); }
}

// 「不是标准 P 站作品」的那部分怎么处理? 弹窗二选一 (需求"导入前就判定")。
// 返回 'reverse' | 'custom' | null(取消)。
function _askImportMode(pre) {
  const n = pre.without_id || 0;
  return new Promise((resolve) => {
    openLibModal('导入图片', `
      <div class="hint" style="margin-bottom:8px">来源：${esc(pre.path || '')}</div>
      <div class="hint">共 <b>${pre.total}</b> 张 · 含作品ID <b>${pre.with_id}</b> 张（直接入库）</div>
      <div class="hint" style="margin:8px 0 4px">
        另有 <b>${n}</b> 张不是标准 P 站作品（文件名里没有作品 ID），这些怎么处理？
      </div>
      <label class="hint" style="display:block;margin-top:8px;cursor:pointer">
        <input type="radio" name="imp-mode" value="reverse" checked>
        转反搜收件箱（默认 · 之后可反查真实来源）
      </label>
      <label class="hint" style="display:block;margin-top:6px;cursor:pointer">
        <input type="radio" name="imp-mode" value="custom">
        直接建自定义作品（标题=文件名 · 来源=其他来源）
      </label>
      ${pre.conflicts ? `<div class="hint" style="margin-top:8px">其中 ${pre.conflicts} 张库内已有同名，会跳过（源文件不动）。</div>` : ''}
      <div class="hint" style="margin-top:10px">♻ 导入成功后源文件会送进回收站（可从回收站还原）。</div>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn accent" id="imp-ok">开始导入</button>
        <button class="btn ghost" id="imp-cancel">取消</button>
      </div>`);
    const okBtn = $('#imp-ok'), cancelBtn = $('#imp-cancel');
    if (okBtn) okBtn.onclick = () => {
      const v = document.querySelector('input[name="imp-mode"]:checked');
      closeLibModal();
      resolve(v ? v.value : 'reverse');
    };
    if (cancelBtn) cancelBtn.onclick = () => { closeLibModal(); resolve(null); };
  });
}
window.importFromFolder = () => importLocalImages('folder');
window.importFromFiles = () => importLocalImages('files');

function paintLibHealth() {
  const bar = $('#lib-health-hint');
  if (!bar) return;
  const h = window._libHealth;
  const miss = h && h.missing_ids ? h.missing_ids.length : 0;
  const empty = h && h.empty_dirs ? h.empty_dirs.length : 0;
  const bad = h && h.corrupt_files ? h.corrupt_files.length : 0;
  // 体检报告: 由本函数统一渲染, 每次同步/轮询重画都不会丢 —— 用户随时能点进去看明细
  const rc = lastCheckReport;
  const susN = rc ? (rc.suspicious || []).length : 0;
  const todoN = rc ? (rc.todos || []).length : 0;
  const liveN = susN + todoN;
  // 一件待处理都没有(问题都解决了), 或用户点过「✕ 收起」→ 不再挂这条汇总。
  // 
  //  没解决但不想看也能收起; 下次真跑体检会重新显示。)
  const showReport = !!rc && liveN > 0 && !rc.bar_dismissed;
  const visible = $('#tab-library').classList.contains('active')
    && (miss || empty || bad || showReport || checkRunning);
  bar.style.display = visible ? '' : 'none';
  bar.innerHTML = `<span class="lib-sync-line">🔄 已与磁盘同步 · 扫描 ${h ? h.scanned_images : 0} 图</span>` +
    (miss ? `<span class="health-badge miss" title="作品有本地下载记录但磁盘文件已被删除">⚠ 文件缺失 ${miss}</span>` : '') +
    (empty ? `<span class="health-badge empty">📁 空目录 ${empty}</span>` : '') +
    (bad ? `<span class="health-badge bad">💥 损坏 ${bad}</span>` : '') +
    (showReport ? `<span class="lib-sync-line" title="体检时间 ${esc(rc.time || '—')}｜系统异常可直接用界面按钮修复；缺信息需人工补齐（作者名、来源等）">🩺 上次体检：待处理 ${liveN}（系统异常 ${susN} · 缺信息 ${todoN}）</span>` : '') +
    (checkRunning ? `<span class="lib-sync-line">⏳ 体检中：${esc(checkStepTitle(checkRunning))}</span>` : '') +
    (showReport ? `<button class="btn accent" onclick="showSelfCheckReport()">📋 查看详情</button>` : '') +
    (showReport ? `<button class="btn ghost icon-btn" title="收起这条提示（报告不删；下次体检会重新显示）" onclick="dismissCheckBar()">✕</button>` : '') +
    (miss ? `<button class="btn danger" onclick="cleanMissing()">🗑️ 清理缺失</button>` : '') +
    `<button class="btn ghost" onclick="runSelfCheck()">🩺 体检</button>`;
}

// 收起图库顶部那条体检汇总 (落盘记住; 下次真跑体检自动恢复)
window.dismissCheckBar = async function () {
  try {
    await api('/api/library/check-report/dismiss', { method: 'POST' });
    if (lastCheckReport) lastCheckReport.bar_dismissed = '1';
    paintLibHealth();
    toast('已收起这条提示（报告还在「🩺 体检」里，下次体检会自动重新显示）', 'ok');
  } catch (e) { toast('收起失败: ' + e.message, 'err'); }
};

async function cleanMissing() {
  if (!confirm('清理所有「文件缺失」的幽灵记录？（把磁盘上已经没有文件、库里的残留记录一并清掉，保持数据库干净）')) return;
  try {
    const r = await api('/api/library/clean-missing', { method: 'POST' });
    libSelected.clear();
    toast(`已清理 ${r.cleaned} 条缺失记录 ✓`, 'ok');
    await libSync();
    loadLibrary(); loadStats(); loadLibFacets();
  } catch (e) { toast('清理失败: ' + e.message, 'err'); }
}

// ===== 🩺 体检 (2026-09-16: 原「一键自检」拆成 6 个可独立运行的检查项) =====
//   旧版把磁盘对账/离线清理/联网补全/文件整理全塞进一次后台跑: 耗时长、进度只有"第几步"、
//   某步出错整体白跑、中途关页面进程就退出导致前功尽弃, 报告里还混着补不回来的假问题。
//   现在: 每项单独跑/单独看结果; 有 x/y 详细进度; 报告分「可疑项」「待办项」且条目可忽略;
//   被中断的任务下次启动会提示是否继续。
let checkTimer = null;
let checkOutline = [];        // 体检项定义 (后端下发, 前端不硬编码)
let checkSteps = {};          // 各体检项最近一次结果
let checkProgress = {};       // 当前进度 {step,done,total,current}
let checkRunning = null;      // 正在跑的体检项名 (null = 空闲)
let checkWatching = false;    // 是否在盯一次运行 (结束时要自动刷新报告)
let lastCheckReport = null;   // 最近一次报告 {time,suspicious[],todos[],ignored}
let pendingTask = null;       // 上次异常中断、等用户决定是否继续的任务

function checkStepTitle(name) {
  const it = (checkOutline || []).find(x => x.name === name);
  return it ? it.title : (name || '');
}

function _setCheckBtns() {
  const busy = !!checkRunning;
  [['#lib-selfcheck-btn', '🩺 体检'], ['#set-selfcheck-btn', '🩺 体检']].forEach(([sel, txt]) => {
    const b = $(sel); if (!b) return;
    b.disabled = busy;
    b.textContent = busy ? '🩺 体检中…' : txt;
  });
}

// 进度条文字: "④ 联网补元数据 12/96 · #1234567"
function checkProgressHtml() {
  if (!checkRunning) return '';
  const p = checkProgress || {};
  const total = p.total || 0;
  const pct = total ? Math.max(2, Math.round((p.done || 0) / total * 100)) : 8;
  return `<div class="check-progress"><span>⏳ 正在跑「${esc(checkStepTitle(checkRunning))}」` +
    (total ? ` ${p.done || 0}/${total}` : '') +
    (p.current ? ` · ${esc(p.current)}` : '') + `</span>` +
    `<div class="check-bar"><i style="width:${pct}%"></i></div></div>`;
}

function startCheckPolling() {
  if (!checkTimer) checkTimer = setInterval(refreshCheckStatus, 1500);
  refreshCheckStatus();
}
function stopCheckPolling() { clearInterval(checkTimer); checkTimer = null; }

async function refreshCheckStatus() {
  let r;
  try { r = await api('/api/library/check-status'); } catch (e) { return; }
  checkOutline = r.outline || checkOutline;
  checkSteps = r.steps || {};
  checkProgress = r.progress || {};
  const run = r.running || null;
  if (run) checkWatching = true;
  checkRunning = run;
  _setCheckBtns();
  paintCheckPanel();
  if (!run && checkWatching) {
    checkWatching = false;
    await loadCheckReport();       // 跑完 → 拉回报告并重画图库顶部提示
    libSync(); loadStats(); loadLibrary(); loadLibFacets();
    setLastAction('library', [], '刚跑完的体检');
  }
  // 空闲且面板没开着 → 没必要继续轮询
  const modal = $('#lib-modal');
  if (!run && !(modal && modal.style.display === 'flex')) stopCheckPolling();
}

// 面板内容 (体检项清单 + 每项单独运行按钮)
function checkItemsHtml() {
  return (checkOutline || []).map(it => {
    const st = checkSteps[it.name];
    let last = '<span class="hint">还没跑过</span>';
    if (st) {
      const sum = Object.entries(st.summary || {})
        .filter(([k, v]) => v).map(([k, v]) => `${CHK_LABEL[k] || k} ${v}`).join(' · ');
      last = st.ok
        ? `<span class="chk-ok">✓ ${esc(st.at || '')}${sum ? ' · ' + esc(sum) : ''}</span>`
        : `<span class="chk-err">✗ ${esc(st.at || '')} · ${esc(st.error || '出错')}</span>`;
    }
    const busy = !!checkRunning;
    return `<div class="chk-item">
      <div class="chk-head"><b>${esc(it.title)}</b>
        <button class="btn ghost chk-run" ${busy ? 'disabled' : ''}
          onclick="chkRun('${it.name}')">▶ 运行</button></div>
      <div class="chk-desc">${esc(it.desc || '')}</div>
      <div class="chk-last">${last}</div>
    </div>`;
  }).join('');
}

const CHK_LABEL = {
  scanned: '扫描', new_pages_linked: '新关联', paths_relinked: '路径自愈', missing: '缺失',
  corrupt: '损坏', empty_dirs: '空夹', authors_adopted: '补作者', foreign_registered: '登记外来',
  sources_refined: '来源细分', orphans_removed: '清孤儿', titles_repaired: '修标题',
  reidentified: '纠正身份', checked: '核对作者', renamed: '改名', filled: '补全',
  failed: '失败', deleted_marked: '标记失效', organized: '整理', legacy_renamed: '规范化名',
  relinked: '路径自愈', suspicious: '系统异常', todos: '缺信息', ignored: '已忽略',
  timestamps_fixed: '时间戳统一',
};

// 体检统计徽标: 没有待处理项时给绿色（没什么要做的），有项时给对应色
function chkStatBadge(text, n, clsWhenHas) {
  return `<span class="op-badge ${n ? clsWhenHas : 'add'}">${text} ${n}</span>`;
}

function paintCheckPanel() {
  const el = $('#chk-panel');
  if (!el) return;
  const rc = (lastCheckReport || {});
  el.innerHTML =
    `<div class="hint">各项可单独运行、单独查看结果。</div>` +
    `<div class="btn-row">
       <button class="btn accent" onclick="chkRun('', true)" ${checkRunning ? 'disabled' : ''}>▶ 全部跑一遍</button>
       <button class="btn ghost" onclick="refreshCheckStatus()">🔄 刷新状态</button>
       <button class="btn ghost" onclick="openCheckReport()">📋 查看报告</button>
       <button class="btn ghost" onclick="openIgnored()">🙈 已忽略项</button>
     </div>` +
    checkProgressHtml() +
    `<div class="chk-list">${checkItemsHtml()}</div>` +
    `<div class="chk-stats">` +
      chkStatBadge('🔧 系统异常', (rc.suspicious || []).length, 'del') +
      chkStatBadge('✋ 缺信息', (rc.todos || []).length, 'upd') +
      (rc.ignored ? `<span class="op-badge">🙈 已忽略 ${rc.ignored}</span>` : '') +
      (rc.time ? `<span class="chk-stats-time">${esc(rc.time)}</span>` : '') +
    `</div>` +
    recheckHintHtml(rc);
}

// ⑥ 复检结论 : 让"已解决的被清掉 / 变了的更新"看得见,
// 而不是只给一个总数 —— 否则用户没法判断复检到底干了什么。
function recheckHintHtml(rc) {
  const rk = (rc && rc.recheck) || null;
  if (!rk || !rk.at) {
    return '<div class="hint">上次复检会与上一版报告逐条对账：已解决项清除，内容变化项更新，其余保留。</div>';
  }
  const parts = [
    `已解决 <b>${rk.resolved || 0}</b>`,
    `有更新 <b>${rk.changed || 0}</b>`,
    `新增 <b>${rk.added || 0}</b>`,
  ];
  if (rk.auto_resolved) parts.push(`自动核对 <b>${rk.auto_resolved}</b>`);
  if (rk.unverified) parts.push(`未复检 <b>${rk.unverified}</b>`);
  return `<div class="hint">上次复检 ${esc(rk.at)} · ${parts.join(' · ')}</div>` +
    (rk.unverified ? '<div class="hint">未复检项本次未运行，条目原样保留，不计为已解决。</div>' : '');
}

// 打开体检面板并开始盯状态 (和「一键整理」一样用大号弹窗: 6 项清单 + 结果 + 进度条要地方)
async function openCheckPanel() {
  openLibModal('🩺 图库体检', '<div id="chk-panel" class="hint">读取状态中…</div>', { lg: true });
  startCheckPolling();
}
window.openCheckPanel = openCheckPanel;

// 跑单项 (step) 或全部 (all)
async function chkRun(step, all) {
  if (checkRunning) { toast('已有体检项在运行，请等它结束', 'err'); return; }
  try {
    const body = all ? { all: true } : { step };
    const r = await api('/api/library/check', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.status !== 'started') throw new Error(r.error || '启动失败');
    checkWatching = true;
    checkRunning = (r.steps && r.steps[0]) || step || 'check';
    _setCheckBtns();
    if (!all) toast(`已开始「${checkStepTitle(step)}」`, 'ok');
    else toast('已开始全部体检项（可随时看单项结果）', 'ok');
    startCheckPolling();
    paintCheckPanel();
  } catch (e) { toast('启动失败: ' + e.message, 'err'); }
}
window.chkRun = chkRun;

async function loadCheckReport() {
  try {
    const r = await api('/api/library/check-report');
    if (r.report) lastCheckReport = r.report;
    window._chkIgnored = r.ignored_items || [];
  } catch (e) { /* 忽略 */ }
  paintLibHealth();
  paintCheckPanel();
  paintSelfCheckCard();
}

window.openCheckReport = function () { renderCheckReport(lastCheckReport, true); };

// 图库页 / 设置页的「体检」入口: 直接开面板
window.runSelfCheck = function () { openCheckPanel(); };
// 旧入口名保留 (旧书签/旧代码可能还引用)
window.showSelfCheckReport = function () { renderCheckReport(lastCheckReport, true); };

// 载入: 报告 + 是否有体检在跑 (页面刷新/服务重启后仍能看到上次结果)
async function loadLastSelfCheck() {
  try {
    const r = await api('/api/library/check-report');
    if (r.report) lastCheckReport = r.report;
    window._chkIgnored = r.ignored_items || [];
  } catch (e) { /* 忽略 */ }
  paintLibHealth();
  paintSelfCheckCard();
  try {
    const s = await api('/api/library/check-status');
    checkOutline = s.outline || [];
    checkSteps = s.steps || {};
    if (s.running) { checkWatching = true; startCheckPolling(); }
  } catch (e) { /* 忽略 */ }
  _setCheckBtns();   // 按钮文案/禁用态归一 (不跑体检时也应显示「🩺 体检」)
}

// ===== 任务运行记录: 上次被拦腰砍断的任务, 提示是否继续 =====
async function loadInterruptedTasks() {
  let r;
  try { r = await api('/api/tasks/interrupted'); } catch (e) { return; }
  const it = (r.items || [])[0];
  if (!it) return;
  pendingTask = it;
  if (!it.resumable) {
    openLibModal('⚠ 上次任务异常中断',
      `<div class="hint">「${esc(it.title || it.key)}」上次被中断后没能继续（${esc(it.updated_at || '')}），
         这项不支持中途接续，请重新运行。</div>
       <div class="btn-row"><button class="btn ghost" onclick="ackPendingTask()">知道了</button></div>`);
    return;
  }
  const remain = (it.remain || []).map(checkStepTitle).join(' → ') || '尚未开始的步骤';
  openLibModal('⚠ 上次体检被中断',
    `<div class="hint">「${esc(it.title || it.key)}」上次没有跑完就被中断了（${esc(it.updated_at || '')}，
       一般是关掉页面后程序自动退出导致）。<br>可以从断点接着跑剩下的：
       <b>${esc(remain)}</b><br>每一步都是幂等的，重跑不会重复改坏数据。</div>
     <div class="btn-row">
       <button class="btn accent" id="chk-resume-btn" onclick="resumePendingTask()">▶ 继续</button>
       <button class="btn ghost" onclick="ackPendingTask()">不用了，忽略</button>
     </div>`);
}
window.loadInterruptedTasks = loadInterruptedTasks;

window.resumePendingTask = async function () {
  if (!pendingTask) return;
  const btn = $('#chk-resume-btn');
  if (btn) { btn.disabled = true; btn.textContent = '▶ 正在接着跑…'; }
  try {
    const r = await api('/api/tasks/resume', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: pendingTask.key }) });
    if (r.status !== 'started') throw new Error(r.error || '无法继续');
    toast(`已接着跑：${(r.steps || []).map(checkStepTitle).join(' → ')}`, 'ok');
    pendingTask = null;
    checkWatching = true;
    openCheckPanel();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '▶ 继续'; }
    toast('继续失败: ' + e.message, 'err');
  }
};

window.ackPendingTask = async function () {
  const key = pendingTask && pendingTask.key;
  pendingTask = null;
  closeLibModal();
  if (!key) return;
  try { await api('/api/tasks/ack', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) }); }
  catch (e) { /* 忽略 */ }
};

// ===== 设置页 · 🩺 体检与审批 =====
// 概览卡片: 上次体检时间 + 可疑项/待办项计数; 「打开体检面板」逐项运行/查看结果
function paintSelfCheckCard() {
  const el = $('#settings-selfcheck');
  if (!el) return;
  const rc = lastCheckReport;
  if (!rc) {
    el.innerHTML = '暂无体检报告。点击下方「🩺 体检」开始检查，各项可单独运行。';
    return;
  }
  const sus = (rc.suspicious || []).length, todo = (rc.todos || []).length;
  el.innerHTML = `上次体检 <strong>${esc(rc.time || '—')}</strong> · ` +
    chkStatBadge('系统异常', sus, 'del') +
    chkStatBadge('缺信息', todo, 'upd') +
    (rc.ignored ? ` · 已忽略 ${rc.ignored} 项` : '') +
    (rc.from_disk ? ' · 重启前的结果' : '');
}

// ===== 设置页 · 📋 操作记录 (增/改/删审计) =====
let _opFilter = 'all';

async function loadOperations() {
  try {
    const r = await api('/api/logs/operations?limit=300');
    window._opData = r;
    paintOps();
  } catch (e) { /* 忽略: 设置页面板静默失败不影响主流程 */ }
}

function paintOps() {
  const r = window._opData;
  if (!r) return;
  const c = r.counts || {};
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('#op-cnt-all', r.total || 0);
  set('#op-cnt-add', c.add || 0);
  set('#op-cnt-update', c.update || 0);
  set('#op-cnt-delete', c.delete || 0);
  $$('.op-filter').forEach(b => b.classList.toggle('active', b.dataset.op === _opFilter));
  const box = $('#op-list');
  if (!box) return;
  let items = r.items || [];
  if (_opFilter !== 'all') items = items.filter(x => x.kind === _opFilter);
  if (!items.length) { box.innerHTML = '<div class="log-empty">暂无操作记录…</div>'; return; }
  box.innerHTML = items.map(x => {
    const cls = x.kind === 'add' ? 'add' : (x.kind === 'delete' ? 'del' : 'upd');
    const ids = (x.ids || []).map(i =>
      `<span class="id-chip" title="点击跳到图库定位这个作品" onclick="libFocusArtwork('${esc(i)}')">${esc(i)}</span>`).join('');
    return `<div class="op-item">` +
      `<span class="op-badge ${cls}">${esc(x.kind_label || x.kind || '')}</span>` +
      `<span class="op-time">${esc(x.ts || '')}</span>` +
      `<span class="op-act">${esc(x.action || '')}</span>` +
      `<span class="op-detail" title="${esc(x.detail || '')}">${esc(x.detail || '')}</span>` +
      (ids ? `<span class="op-ids">${ids}</span>` : '') +
      `</div>`;
  }).join('');
}

function filterOps(k) { _opFilter = k; paintOps(); }

async function clearOperations() {
  if (!confirm('清空全部操作记录？（只删除这份审计文本，不影响图库数据）')) return;
  try {
    const r = await api('/api/logs/operations/clear', { method: 'POST' });
    toast(`已清空 ${r.cleared || 0} 条操作记录 ✓`, 'ok');
    window._opData = { items: [], total: 0, counts: { add: 0, update: 0, delete: 0 } };
    paintOps();
  } catch (e) { toast('清空失败: ' + e.message, 'err'); }
}
window.loadOperations = loadOperations;
window.filterOps = filterOps;
window.clearOperations = clearOperations;

// 体检报告: 分「可疑项」(系统异常，通常能一键修) 与「待办项」(需要你决策)，每条都能单独忽略。
// 条目按钮用下标传参 (window._chkItems)，避免文件名里的引号破坏 onclick 属性。
const CHK_GROUPS = [
  ['missing_file', '⚠ 文件丢失：数据库有记录，但磁盘图片文件已找不到（可清理记录，或重新下载）',
   `<button class="btn danger" onclick="cleanMissing()">🗑️ 一键清理这类记录</button>`],
  ['corrupt', '💥 损坏图片：文件存在但无法正常打开', ''],
  ['duplicate', '🧩 重复图片：同一文件在图库中存在多份副本',
   `<button class="btn accent" onclick="dedupDups()">🧹 清理重复副本（每组保留画师文件夹内一份）</button>`],
  ['empty_dir', '📁 空文件夹：作者文件夹里已没有任何图片',
   `<button class="btn danger" onclick="cleanEmptyDirs()">🗑️ 删除空文件夹</button>`],
  ['legacy_name', '📝 历史存疑文件名：旧版反搜留下的 (识别ID) 前缀，主体作品作者未验证，暂不自动改名', ''],
  ['loose_image', '📂 未整理：散落在图库根目录且无法自动归档的图片（可用反搜识别作者）',
   `<button class="btn accent" onclick="switchTab('reverse')">🔎 去反搜归类</button>`],
  ['no_author', '👤 缺作者名：文件夹名里也读不到作者名（可逐条「指定作者」，或对本地有图的作品「反搜来源」溯源）', ''],
  ['metadata_failed', '🔧 元数据补全失败：作品可能已限流或需重新登录（作品页确认失效的已自动标记，不再重复报）', ''],
];
const CHK_TYPE_NAME = {};
CHK_GROUPS.forEach(g => { CHK_TYPE_NAME[g[0]] = g[1].split('：')[0]; });

function chkItemHtml(s, idx) {
  const t = esc(s.text || '');
  const aid = s.artwork_id ? String(s.artwork_id) : '';
  const idChip = aid
    ? `<span class="id-chip" title="点击跳到图库定位这个作品" onclick="libFocusArtwork('${esc(aid)}')">${esc(aid)}</span>`
    : '';
  const acts = [`<button class="btn ghost" title="不再提示这一条（可在「已忽略」里恢复）" onclick="chkIgnore(${idx}, false)">🙈 忽略</button>`];
  if (s.type === 'no_author' && aid) {
    // has_local 为 undefined = 旧版快照(未带该字段), 按「可试」处理
    if (s.has_local === undefined || s.has_local)
      acts.unshift(`<button class="btn ghost" onclick="susReverseSource('${esc(aid)}')">🔎 反搜来源</button>`);
    acts.unshift(`<button class="btn ghost" onclick="susAssignAuthor('${esc(aid)}')">🖊 指定作者</button>`);
  }
  return `<li>${t}<span class="sus-acts">${idChip}${acts.join('')}</span></li>`;
}

function chkSectionHtml(title, note, items, base, emptyText) {
  let html = `<h4 class="chk-sec">${title}<span class="chk-sec-note">${note}</span>` +
    `<span class="chk-sec-num">${items.length} 项</span></h4>`;
  if (!items.length) return html + `<div class="hint">${emptyText}</div>`;
  for (const [type, gtitle, actTpl] of CHK_GROUPS) {
    const arr = items.filter(s => s.type === type);
    if (!arr.length) continue;
    // 「文件丢失 / 损坏图片」两组再给一个「♻ 重下修复」—— 它们正是重下修复最典型的用途
    let act = actTpl || '';
    if (type === 'missing_file' || type === 'corrupt') {
      const nRep = arr.filter(s => /^\d+$/.test(String(s.artwork_id || ''))).length;
      if (nRep) {
        act = (act ? act + ' ' : '') +
          `<button class="btn accent" onclick="chkRepairGroup('${type}')" title="用 pixiv 官方源【覆盖】重下这些作品（旧文件自动备份为 .bak，可在「💾 备份」里恢复）">♻ 重下修复这 ${nRep} 个</button>`;
      }
    }
    html += `<div class="health-group"><div class="health-group-title">${gtitle} · ${arr.length}</div>` +
      `<ul class="health-list">` +
      arr.slice(0, 60).map(s => chkItemHtml(s, base + items.indexOf(s))).join('') +
      (arr.length > 60 ? `<li class="hint">…其余 ${arr.length - 60} 项从略（可逐条忽略）</li>` : '') +
      `</ul>${act ? `<div class="btn-row">${act}</div>` : ''}</div>`;
  }
  const known = new Set(CHK_GROUPS.map(g => g[0]));
  const rest = items.filter(s => !known.has(s.type));
  if (rest.length) {
    html += `<div class="health-group"><div class="health-group-title">其他 · ${rest.length}</div><ul class="health-list">` +
      rest.slice(0, 60).map(s => chkItemHtml(s, base + items.indexOf(s))).join('') + `</ul></div>`;
  }
  return html;
}

function renderCheckReport(rc, reopen = false) {
  if (!rc) { toast('还没有体检报告，先点「🩺 体检」跑一遍', 'err'); return; }
  if (reopen) openLibModal('🩺 体检报告', '', { lg: true });
  // 去重兜底: 服务端已按 (type, ref) 去重, 前端再兜一层 —— 同一件作品若真有**两个不同问题**
  // (如"文件丢了" + "缺作者名"), 那是两条独立的问题, 各占一条是对的; 但完全相同的条目绝不该出现两次。
  const uniq = (arr) => {
    const seen = new Set();
    return (arr || []).filter(x => {
      const k = String(x.type) + '|' + String(x.ref);
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
  };
  const sus = uniq(rc.suspicious), todo = uniq(rc.todos);
  window._chkItems = sus.concat(todo);      // 忽略按钮按下标取条目 (见 chkItemHtml)
  let html = `<div class="hint">体检时间 ${esc(rc.time || '—')}` +
    (rc.from_disk ? ' · 重启前的结果，重新运行可刷新' : '') +
    (rc.ignored ? ` · 已忽略 ${rc.ignored} 项` : '') + `</div>`;
  // 两类问题的说明折起来: 常看报告的人不需要每次读一遍判据, 第一次来的人点开就能懂
  html += `<details class="chk-help"><summary>两类问题的区别</summary>` +
    `<div class="hint"><b>🔧 系统异常</b>：可用界面上的按钮直接修复（重下修复、清理空文件夹等）。<br>` +
    `<b>✋ 缺信息</b>：需要人工补齐的字段（作者名、来源等）。<br>` +
    `同一作品存在多个不同问题时分别列出。</div></details>`;
  html += recheckBlockHtml(rc);              // ⑥ 复检干了什么: 清掉哪些 / 哪些变了
  html += chkSectionHtml('🔧 系统异常', '可一键修复', sus, 0,
    rc.corrupt_verified === false
      ? '本次未校验图片损坏情况，运行「① 磁盘对账」后可确认'
      : '无异常项');
  html += chkSectionHtml('✋ 缺信息', '需人工补齐', todo, sus.length, '无待办项');
  html += `<div class="btn-row">
      <button class="btn accent" onclick="openCheckPanel()">🩺 打开体检面板</button>
      <button class="btn ghost" onclick="openIgnored()">🙈 已忽略项</button>
      <button class="btn ghost" onclick="closeLibModal()">关闭</button>
    </div>`;
  $('#lib-modal-body').innerHTML = html;
}

// ⑥ 复检明细块: 明确列出"清理了哪些已解决的""哪些条目变了(旧→新)",
// 以及"哪些本次没条件复检、已原样保留"—— 复检的价值就在于这三件事看得见。
function recheckBlockHtml(rc) {
  const rk = (rc && rc.recheck) || null;
  if (!rk || !rk.at) return '';
  const steps = (rk.steps_ran || []).map(s => checkStepTitle(s) || s).join('、') || '—';
  let h = `<div class="chk-recheck-box">
    <div class="chk-recheck-head">上次复检 · ${esc(rk.at)}</div>
    <div class="chk-stats">` +
      `<span class="op-badge add">已解决 ${rk.resolved || 0}</span>` +
      `<span class="op-badge upd">有更新 ${rk.changed || 0}</span>` +
      `<span class="op-badge">新增 ${rk.added || 0}</span>` +
      (rk.unverified ? `<span class="op-badge del">未复检 ${rk.unverified}</span>` : '') +
    `</div>` +
    (rk.unverified
      ? `<div class="hint">未复检项本次未运行，条目原样保留，不计为已解决。</div>` : '') +
    `<div class="hint">复核范围：${esc(steps)}</div>` +
    // 顺手本地复核 (打开报告时做的): 缺作者/缺文件/空夹/散落图/损坏图(文件已删)这几类
    // 本地就能判, 解决了当场清掉 —— 让用户看得见"为什么数量自己降了", 而不是以为报告乱跳。
    (rk.auto_resolved
      ? `<div class="hint">打开报告时自动核对，另清理 <b>${rk.auto_resolved}</b> 条已解决项` +
        `（缺作者 / 缺文件 / 空文件夹 / 散落图 / 损坏图）· ${esc(rk.auto_at || '')}</div>`
      : '');
  const ri = rk.resolved_items || [];
  if (ri.length) {
    h += `<div class="hint">已解决并清理（已从报告中移除）：</div><ul class="health-list">` +
      ri.slice(0, 100).map(x =>
        `<li>${esc(CHK_TYPE_NAME[x.type] || x.type)}：${esc(x.text || x.ref)}</li>`).join('') +
      `</ul>`;
  }
  const ci = rk.changed_items || [];
  if (ci.length) {
    h += `<div class="hint">有变化（已按最新情况更新）：</div><ul class="health-list">` +
      ci.slice(0, 100).map(x =>
        `<li>${esc(CHK_TYPE_NAME[x.type] || x.type)}：<s>${esc(x.was)}</s> → <b>${esc(x.now)}</b></li>`
      ).join('') + `</ul>`;
  }
  return h + `</div>`;
}

// 忽略一条 / 恢复一条 (体检报告里"不再提示"的入口)
window.chkIgnore = async function (idx, undo) {
  const it = (window._chkItems || [])[idx];
  if (!it) return;
  try {
    const r = await api('/api/library/check-ignore', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: it.type, ref: it.ref, undo: !!undo }) });
    window._chkIgnored = r.ignored_items || [];
    toast(undo ? '已恢复提示' : '已忽略，不再提示这条', 'ok');
  } catch (e) { toast('操作失败: ' + e.message, 'err'); return; }
  await loadCheckReport();
  renderCheckReport(lastCheckReport, true);
};

window.openIgnored = async function () {
  let items = window._chkIgnored || [];
  try {
    const r = await api('/api/library/check-ignored');
    items = r.items || []; window._chkIgnored = items;
  } catch (e) { /* 用缓存 */ }
  window._chkIgnoredView = items;
  const list = items.length
    ? `<div class="hint">以下条目已忽略，体检报告不再提示。点击「恢复」可重新纳入。</div>
       <ul class="health-list">` + items.slice(0, 300).map((x, i) =>
        `<li>${esc(CHK_TYPE_NAME[x.kind] || x.kind)}：<code>${esc(x.ref)}</code>` +
        `<span class="sus-acts"><button class="btn ghost" onclick="chkRestore(${i})">↩ 恢复</button></span></li>`
      ).join('') + `</ul>`
    : '<div class="hint">暂无已忽略条目。</div>';
  openLibModal('🙈 已忽略的体检条目', list +
    `<div class="btn-row"><button class="btn accent" onclick="openCheckPanel()">🩺 体检面板</button>
      <button class="btn ghost" onclick="closeLibModal()">关闭</button></div>`, { lg: true });
};

window.chkRestore = function (i) {
  const it = (window._chkIgnoredView || [])[i];
  if (!it) return;
  chkIgnoreRestore(it.kind, it.ref);
};

async function chkIgnoreRestore(kind, ref) {
  try {
    const r = await api('/api/library/check-ignore', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, ref, undo: true }) });
    window._chkIgnored = r.ignored_items || [];
    toast('已恢复提示（下次体检会重新列出）', 'ok');
  } catch (e) { toast('恢复失败: ' + e.message, 'err'); }
  await loadCheckReport();   // 同步「已忽略 N 项」计数, 别让用户以为恢复没生效
  openIgnored();
}

// 审批动作①: 手动指定作者 (弹独立小窗, 自检报告在后面保持不动)
window.susAssignAuthor = async function (aid) {
  if (!facetData.authors.length) { try { await loadLibFacets(); } catch (e) { /* ignore */ } }
  openSrcModal(`🖊 指定作者 · 作品 ${aid}`,
    `<div class="hint">给作品 <b>${esc(aid)}</b> 指定作者：<br>
      · 已有作者 → 在「作者名」里输入关键词（主名/曾用名/ID）从下拉选，会自动带上其 pixiv ID；<br>
      · pixiv 新画师 → 填「作者 ID（纯数字）」+ 名字（名字可省，会自动联网补）；<br>
      · 其他来源（推特 / ArtStation / 未知作者名）→ 只填名字即可，会建一个本地作者。</div>
     <div class="modal-field"><label>作者名</label>
       <div class="combo-wrap">
         <input id="sus-author-name" type="text" autocomplete="off" placeholder="输入关键词有提示（主名/曾用名/ID），也可直接输入新名字">
         <div class="combo-list" id="sus-author-combo"></div>
       </div></div>
     <div class="modal-field"><label>pixiv 作者 ID（可选）</label>
       <input id="sus-author-id" type="text" placeholder="纯数字，如 12345678；填了就按 pixiv 画师关联"></div>
     <div class="modal-actions">
       <button class="btn accent" onclick="susSaveAuthor('${esc(aid)}')">💾 保存并从清单移除</button>
       <button class="btn ghost" onclick="closeSrcModal()">取消</button>
     </div>`);
  // 从下拉选中已有作者时, 顺带把它的 pixiv ID 填进 ID 栏 (避免用户再去查一遍)
  makeEditAuthorCombo($('#sus-author-name'), $('#sus-author-combo'), (name) => {
    const a = (facetData.authors || []).find(x => String(x.name) === String(name));
    const idEl = $('#sus-author-id');
    if (a && idEl && !idEl.value.trim() && /^\d+$/.test(String(a.author_id || '')))
      idEl.value = a.author_id;
  });
};

window.susSaveAuthor = async function (aid) {
  const name = ($('#sus-author-name').value || '').trim();
  const pid = ($('#sus-author-id').value || '').trim();
  if (!name && !pid) { toast('请至少填写作者名或 pixiv 作者 ID', 'err'); return; }
  try {
    const r = await api('/api/library/set-author', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [aid], author_name: name, author_id: pid }) });
    closeSrcModal();
    toast(`作品 ${aid} 已归入作者「${r.name}」 ✓`, 'ok');
    await susResolveLocally(aid);
  } catch (e) { toast('指定作者失败: ' + e.message, 'err'); }
};

// 审批动作②: 反搜来源 (复用图库的逐作品 SauceNAO 溯源, 完成后在反搜页确认写库)
window.susReverseSource = function (aid) {
  closeLibModal();   // 反搜会切到反搜页并用弹窗/页内结果, 先收起报告 (报告还在 lastCheckReport, 随时可再看)
  sourceSearch(aid);
};

// 处理完一条 (如已指定作者) → 从当前报告里移除, 并刷新图库/统计
async function susResolveLocally(aid) {
  try {
    await api('/api/library/self-check-dismiss', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artwork_id: aid }) });
  } catch (e) { /* 报告同步失败不影响主流程 */ }
  await loadCheckReport();
  if (lastCheckReport) renderCheckReport(lastCheckReport, true);
  loadLibrary(); libSync(); loadStats();
}

async function cleanEmptyDirs() {
  if (!confirm('删除全部空文件夹？（仅删不含任何图片的作者目录）')) return;
  try {
    const r = await api('/api/library/clean-empty-dirs', { method: 'POST' });
    let msg = `已删除 ${r.removed} 个空文件夹 ✓`;
    if (r.failed > 0) msg += `（${r.failed} 个被占用跳过）`;
    toast(msg, 'ok');
    await libSync();
    chkRun('report');     // 只重跑「汇总报告」这一项, 不用再把整套体检跑一遍
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

async function dedupDups() {
  if (!confirm('清理重复副本？每组重复只保留画师文件夹内的一份，其余副本会被删除。')) return;
  try {
    const r = await api('/api/library/dedup', { method: 'POST' });
    toast(`已清理 ${(r.removed || []).length} 个重复副本，保留 ${(r.kept || []).length} 份 ✓`, 'ok');
    await libSync();
    chkRun('report');
  } catch (e) { toast('清理失败: ' + e.message, 'err'); }
}

// 缺元数据/缺失文件/已删除 三选一互斥切换: mode = 'metadata' / 'file' / 'deleted' / ''(全部)
// 点已激活的按钮 = 取消回全部; 同一作品极少同时处于多种状态, 不需要交集视图
function setLibMissingMode(mode) {
  libFilters.missing = mode === 'metadata';
  $('#lib-f-missing').classList.toggle('active', mode === 'metadata');
  $('#lib-f-missing-file').classList.toggle('active', mode === 'file');
  $('#lib-f-deleted').classList.toggle('active', mode === 'deleted');
  loadLibrary();
}

// 从操作记录 / 自检可疑项点进来: 跳到图库并按该作品定位。
// 会先清掉可能把目标筛掉的筛选(缺元数据/缺失文件/已删除/来源/类型/作者/标签), 保证一定看得到。
// 注意 R18 筛选**不动**(那是安全默认): 目标若是 R18 又开着「仅非R18」, 如实提示而不是悄悄改筛选。
window.libFocusArtwork = async function (aid) {
  aid = String(aid || '').trim();
  if (!aid) return;
  switchTab('library');
  libFilters.missing = false;
  libFilters.source = ''; libFilters.type = ''; libFilters.author = ''; libFilters.tag = '';
  ['#lib-f-source', '#lib-f-type'].forEach(id => { const el = $(id); if (el) el.value = ''; });
  ['#lib-f-author-input', '#lib-f-tag-input'].forEach(id => { const el = $(id); if (el) el.value = ''; });
  ['#lib-f-missing', '#lib-f-missing-file', '#lib-f-deleted'].forEach(id => {
    const el = $(id); if (el) el.classList.remove('active');
  });
  const q = $('#lib-search-input');
  if (q) q.value = aid;
  libDataView = 'artwork';
  await loadLibrary({ resetPage: true });
  const items = ($('#lib-result')._lastItems || []);
  if (items.some(x => String(x.artwork_id) === aid)) toast('已在图库定位 ' + aid, 'ok');
  else toast('没找到 ' + aid + '：可能被 R18 等筛选挡住，或作品已不在库中', 'err');
};

// 统一加载: 按当前筛选条件请求 /api/library/items
// opts.resetPage=true (默认): 查询条件变化时回到第 1 页; 翻页调用传 {resetPage:false} 保留当前页
let libReqSeq = 0;   // 请求竞态令牌: 快速连续搜索/筛选时旧响应不覆盖新结果
async function loadLibrary(opts = {}) {
  if (opts.resetPage !== false) libPage = 1;
  libFilters.q = $('#lib-search-input').value.trim();
  const mySeq = ++libReqSeq;
  const cont = $('#lib-result');
  // 静默刷新: 已有列表/网格内容时不清空(避免闪烁/跳滚动), 只在首次/空容器时显示「加载中」
  const hasList = !!(cont.querySelector('.lib-list, .cards, .card-grid, .table, .works-grid'));
  const silent = hasList && !opts.forceLoading;
  const prevScroll = window.scrollY;
  if (!silent) { cont.innerHTML = '<div class="hint">加载中…</div>'; cont._libSig = ''; }
  const p = new URLSearchParams();
  if (libOnlyAid) {
    // 特殊筛选「仅查看该作品」: 只按作品 ID 精确过滤, 其余条件(含 R18 默认隐藏)
    // 一律放空 —— 否则 R18 作品会被 r18='0' 挡掉, 达不到「只看这一个」的目的。
    p.set('only', libOnlyAid);
    p.set('r18', '');
    p.set('view', libDataView);
    p.set('limit', LIB_PAGE_SIZE);
    p.set('offset', (libPage - 1) * LIB_PAGE_SIZE);
  } else {
  p.set('q', libFilters.q);
  p.set('type', libFilters.type);
  p.set('status', libFilters.status || '');
  p.set('source', libFilters.source || '');
  p.set('r18', libFilters.r18);
  p.set('ai', libFilters.ai || '');   // AI 作品筛选: '' 全部 / '0' 非 AI / '2' 仅 AI
  p.set('author_id', libFilters.author);
  p.set('tag', libFilters.tag);
  p.set('missing', libFilters.missing ? '1' : '');
  const onlyMissFile = $('#lib-f-missing-file') ? $('#lib-f-missing-file').classList.contains('active') : false;
  p.set('file_missing', onlyMissFile ? '1' : '');
  const onlyDeleted = $('#lib-f-deleted') ? $('#lib-f-deleted').classList.contains('active') : false;
  p.set('deleted', onlyDeleted ? '1' : '');
  p.set('sort', libFilters.sort);
  p.set('order', libFilters.order);
  p.set('view', libDataView);
  p.set('limit', LIB_PAGE_SIZE);
  p.set('offset', (libPage - 1) * LIB_PAGE_SIZE);
  }
  try {
    const r = await api('/api/library/items?' + p.toString());
    if (mySeq !== libReqSeq) return;   // 已有更新的请求发出 → 丢弃本次过期响应
    const items = r.items || [];
    libTotal = r.total ?? 0;
    libPages = Math.max(1, Math.ceil(libTotal / LIB_PAGE_SIZE));
    if (libPage > libPages) { libPage = libPages; }  // 删除到最后几条后页码越界, 自动回落到末页
    $('#lib-total').textContent = libOnlyAid
      ? `👁️ 仅查看作品 #${libOnlyAid} · 共 ${libTotal} 张图片`
      : `共 ${libTotal} 条${libTotal > LIB_PAGE_SIZE ? ` · 第 ${libPage}/${libPages} 页` : ''}`;
    renderLibPagers();
    // 重画判据 = **渲染指纹**(数据载荷 + 打码态 + 视图模式), 只要影响"画出来长什么样"的
    // 输入有一个变了就必须重画。
    // 2026-09-17 的坑: 以前指纹里只有数据载荷, 关掉安全模式后数据一个字没变
    //   → 判定"没变" → 不重画 → 打码的图还是打码, 用户以为刷新按钮坏了, 得靠
    //   "清空筛选再选回来"硬把载荷刷变才生效。所以 safemode 必须进指纹。
    // 同时: 手动点刷新 / 切换打码等**显式动作**一律 force, 用户点了就该看见重画。
    const sig = _libRenderSig(items);
    if (opts.force || sig !== cont._libSig) {
      cont._libSig = sig;
      renderLibrary(items);
    } else {
      cont._lastItems = _libMarkKeys(items);   // 数据没变, 只换引用, 不动 DOM
    }
    syncLibSelUI();
    if (silent) window.scrollTo(0, prevScroll);
  } catch (e) {
    if (!silent) cont.innerHTML = '<div class="hint">加载失败。</div>';
    else toast('刷新失败: ' + e.message, 'err');
  }
}

// 图库「渲染指纹」: 把**所有影响渲染的输入**拼成一个串。
// ⚠ 以后新增任何"会改变卡片长什么样"的全局开关(打码态、视图模式、标签显示…),
//    都要加进这里 —— 漏一个就会出现"数据没变 → 不重画 → 用户看到旧样子"的假 bug
//    (2026-09-17 安全模式切换就是这么中招的)。冒烟 T101 盯着这条不变量。
function _libRenderSig(items) {
  return (safemode ? 'sm1' : 'sm0') + '|' + libDataView + '|' + libView + '|'
         + JSON.stringify(items);
}

// 打码态一变, **所有渲染过缩略图的视图**都得按新状态重画一遍。
// 只刷图库是不够的: 搜索结果 / 回收站 / 反搜列表 / 对账台 也都画缩略图。
// (用户 2026-09-17 报的正是"关了安全模式切回来还是打码的, 刷新也没用"。)
async function refreshAllThumbViews() {
  const lib = $('#lib-result');
  if (lib) lib._libSig = '';                  // 清指纹 → 下次一定重画
  const jobs = [loadLibrary({ force: true })];
  if (isTabActive('trash')) jobs.push(loadTrash());
  // 搜索结果: 用缓存里的**原始响应**重渲染(不发第二次联网请求, 离线也有效)
  const cached = searchCache[searchScope];
  const sbox = $('#search-result');
  if (cached && cached.res && sbox && sbox.innerHTML.trim()) {
    renderSearch(sbox, cached.res);
    cached.html = sbox.innerHTML;
  }
  if (isTabActive('reverse') && typeof loadReverseStatus === 'function') {
    jobs.push(loadReverseStatus());
  }
  await Promise.allSettled(jobs);
}

// ===== 👁️ 仅查看该作品 (特殊筛选, 2026-09-16) =====
// 进入: 记录当前筛选/视图/页码/滚动位置快照 → 精确 ID 过滤 → 切图片视图网格平铺。
// 撤销: 完整还原快照 → 回到作品视图 → 滚动定位到该作品并闪烁高亮。
function _paintLibViewToggles() {
  $$('#lib-view-toggle .view-btn').forEach(b => b.classList.toggle('active', b.dataset.view === libView));
  $$('#lib-data-toggle .view-btn').forEach(b => b.classList.toggle('active', b.dataset.view === libDataView));
}

function renderOnlyBanner() {
  const el = $('#lib-only-banner');
  if (!el) return;
  if (!libOnlyAid) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.innerHTML = `<span class="only-banner-txt">👁️ 特殊筛选：<b>仅查看作品 #${esc(libOnlyAid)}</b>
      　（图片视图平铺 · 已暂时忽略其他筛选条件）</span>
    <button class="btn ghost" onclick="libOnlyClear()" title="撤销特殊筛选，回到作品视图并定位到该作品">↩ 撤销，返回全部作品</button>`;
}

async function libOnlyThisWork(aid) {
  aid = String(aid || '').trim();
  if (!aid) return;
  if (libOnlyAid === aid) { toast('已在仅查看该作品', 'ok'); return; }
  if (!libOnlyAid) {
    // 只在首次进入时记录快照 (重复点别的作品不覆盖, 保证撤销回最初状态)
    libOnlyPrev = {
      filters: Object.assign({}, libFilters),
      dataView: libDataView, view: libView, page: libPage,
      search: $('#lib-search-input').value,
      scrollY: window.scrollY,
    };
  }
  libOnlyAid = aid;
  libSelected.clear(); _libAnchor = null;
  libDataView = 'image';   // 图片视图
  libView = 'grid';        // 网格平铺
  libPage = 1;
  _paintLibViewToggles();
  renderOnlyBanner();
  await loadLibrary({ forceLoading: true });
  // 滚到横幅 (撤销按钮) 位置 —— 页面真实滚动容器是 main, window.scrollTo 无效
  const banner = $('#lib-only-banner');
  if (banner) banner.scrollIntoView({ block: 'start' });
}

async function libOnlyClear() {
  if (!libOnlyAid) return;
  const prev = libOnlyPrev || {};
  const aid = libOnlyAid;
  libOnlyAid = null; libOnlyPrev = null;
  // 还原筛选与视图 (快照缺失时退化为作品视图第 1 页)
  if (prev.filters) libFilters = Object.assign({}, prev.filters);
  if (typeof prev.search === 'string') $('#lib-search-input').value = prev.search;
  libDataView = prev.dataView || 'artwork';
  libView = prev.view || 'grid';
  libPage = prev.page || 1;
  libSelected.clear(); _libAnchor = null;
  _paintLibViewToggles();
  renderOnlyBanner();
  await loadLibrary({ resetPage: false });
  flashLibCard(aid);
}

// 定位到指定作品卡/行并短暂闪烁高亮 (撤销特殊筛选后「回到刚才的位置」)
function flashLibCard(aid) {
  const el = $(`#lib-result [data-key="${String(aid).replace(/"/g, '')}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('lib-flash');
  setTimeout(() => el.classList.remove('lib-flash'), 2400);
}

// ---- 图库分页栏 (顶部/底部同步渲染): 首页/上一页/页码输入/下一页/尾页 ----
function renderLibPagers() {
  const html = libTotal > LIB_PAGE_SIZE ? `
    <div class="pager-inner">
      <button class="btn ghost" onclick="goLibPage(1)" ${libPage <= 1 ? 'disabled' : ''} title="首页">«</button>
      <button class="btn ghost" onclick="goLibPage(${libPage - 1})" ${libPage <= 1 ? 'disabled' : ''} title="上一页">‹</button>
      <span class="pager-info">第
        <input class="lib-page-input" type="number" min="1" max="${libPages}" value="${libPage}"
               onkeydown="if(event.key==='Enter')goLibPageFromInput(this)"> / ${libPages} 页
      </span>
      <button class="btn ghost" onclick="goLibPage(${libPage + 1})" ${libPage >= libPages ? 'disabled' : ''} title="下一页">›</button>
      <button class="btn ghost" onclick="goLibPage(${libPages})" ${libPage >= libPages ? 'disabled' : ''} title="尾页">»</button>
    </div>` : '';
  $('#lib-pager-top').innerHTML = html;
  $('#lib-pager-bottom').innerHTML = html;
}

function goLibPage(n) {
  n = Math.min(Math.max(1, n | 0), libPages || 1);
  if (n === libPage) return;
  libPage = n;
  loadLibrary({ resetPage: false });
  $('#lib-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function goLibPageFromInput(input) {
  const n = parseInt(input.value, 10);
  if (Number.isFinite(n)) goLibPage(n);
  else input.value = libPage;
}

// ---- 可搜索下拉 (作者/标签筛选): 输入关键词过滤, 点击选中 ----
let facetData = { authors: [], tags: [] };

function makeCombo(input, list, { itemsRef, onPick, digitAsId }) {
  let pickedValue = '', pickedLabel = '';

  function render(forceShow) {
    const q = input.value.trim().toLowerCase();
    // 已选中时输入框显示的是标签名, 不把它当过滤词 (聚焦时才开始新过滤)
    let rows = itemsRef();
    if (q && document.activeElement === input) {
      // 过滤同时匹配主标签/曾用名(别名)/ID (作者改名后输入旧昵称能搜到, 输入作者ID也能筛)
      rows = rows.filter(r => {
        if (r.label.toLowerCase().includes(q)) return true;
        if (String(r.value).toLowerCase().includes(q)) return true;
        return Array.isArray(r.aliases) && r.aliases.some(x => String(x).toLowerCase().includes(q));
      });
    }
    let html = '';
    if (!q) html += `<div class="combo-item all" data-value=""><span>${input.placeholder ? '✕ 全部（清除筛选）' : '✕ 全部'}</span></div>`;
    // 作者筛选: 纯数字输入且没有 ID 完全匹配的作者 → 直接按作者ID精确筛选
    if (digitAsId && q && /^\d+$/.test(q) && !rows.some(r => String(r.value) === q)) {
      html += `<div class="combo-item" data-value="${q}" data-label="#${q}"><span class="c-label">#${q}（按作者ID精确筛选）</span></div>`;
    }
    const shown = rows.slice(0, 80);
    html += shown.map(r => {
      const sub = (r.aliases && r.aliases.length)
        ? `<div class="c-alias">曾用名：${r.aliases.map(esc).join('、')}</div>` : '';
      return `<div class="combo-item${r.value === pickedValue ? ' active' : ''}" data-value="${esc(r.value)}" data-label="${esc(r.label)}"><span class="c-label">${esc(r.label)}${sub}</span><span class="c-count">${r.count}</span></div>`;
    }).join('');
    if (rows.length > shown.length) html += `<div class="combo-empty">…继续输入关键词缩小范围 (共 ${rows.length} 条)</div>`;
    if (q && !rows.length) html = `<div class="combo-empty">无匹配「${esc(input.value.trim())}」</div>`;
    list.innerHTML = html;
    if (forceShow) list.classList.add('open');
  }

  function open() { render(true); }
  function close() { list.classList.remove('open'); }

  input.addEventListener('focus', () => { input.select(); open(); });
  input.addEventListener('click', open);
  input.addEventListener('input', () => render(true));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { close(); input.blur(); }
    else if (e.key === 'Enter') {
      const first = list.querySelector('.combo-item:not(.all)') || list.querySelector('.combo-item');
      if (first) pick(first.dataset.value || '', first.dataset.label || '');
    }
  });
  input.addEventListener('blur', () => {
    setTimeout(close, 120);
    // 输入框被清空 → 取消筛选; 失焦时恢复显示已选项标签
    if (!input.value.trim() && pickedValue) { pickedValue = ''; pickedLabel = ''; onPick('', ''); }
    else if (pickedValue) input.value = pickedLabel;
  });
  // mousedown 早于 blur: 点选项先取值 (preventDefault 阻止输入框失焦);
  // click 兜底 (键盘/合成事件场景), 用标志防重复触发
  let mousedownPicked = false;
  list.addEventListener('mousedown', (e) => {
    const it = e.target.closest('.combo-item');
    if (!it) return;
    e.preventDefault();
    mousedownPicked = true;
    pick(it.dataset.value || '', it.dataset.label || '');
    setTimeout(() => { mousedownPicked = false; }, 0);
  });
  list.addEventListener('click', (e) => {
    const it = e.target.closest('.combo-item');
    if (!it || mousedownPicked) return;
    pick(it.dataset.value || '', it.dataset.label || '');
  });
  document.addEventListener('click', (e) => {
    if (!list.contains(e.target) && e.target !== input) close();
  });

  function pick(value, label) {
    pickedValue = value; pickedLabel = value ? label : '';
    input.value = pickedLabel;
    close(); input.blur();
    onPick(value, label);
  }

  return {
    /** 按值回填显示 (筛选状态恢复/外部设置时用)。fallbackLabel: 选项列表里查不到时显示什么 */
    setByValue(value, fallbackLabel) {
      const hit = itemsRef().find(r => String(r.value) === String(value));
      pickedValue = value || '';
      pickedLabel = hit ? hit.label : (value ? (fallbackLabel || '') : '');
      input.value = pickedLabel;
    },
    clear() { pickedValue = ''; pickedLabel = ''; input.value = ''; },
  };
}

// ---- 编辑弹窗关键词提示 (编辑作者/编辑标签): 复用筛选下拉的 combo 交互 ----
// 弹窗每次打开都新建 input+list, 用全局注册表挂一个 document click 关闭器防监听器泄漏
const _editComboRegs = new Set();
if (!window._editComboBound) {
  window._editComboBound = true;
  document.addEventListener('click', (e) => {
    _editComboRegs.forEach(({ list, input }) => {
      if (!list.contains(e.target) && e.target !== input) list.classList.remove('open');
    });
  });
}
function _regEditCombo(list, input) { _editComboRegs.add({ list, input }); }

// 编辑作者: 输入过滤主名/曾用名/ID, 点选/回车回填作者名。onPick(可选)=选中后回调(可顺带填别的字段)
function makeEditAuthorCombo(input, list, onPick) {
  if (!input || !list) return;
  _regEditCombo(list, input);
  function render() {
    const q = input.value.trim().toLowerCase();
    let rows = facetData.authors || [];
    if (q) rows = rows.filter(a =>
      String(a.name || '').toLowerCase().includes(q) ||
      String(a.author_id || '').toLowerCase().includes(q) ||
      (a.aliases || []).some(x => String(x).toLowerCase().includes(q)));
    list.innerHTML = rows.slice(0, 80).map(a =>
      `<div class="combo-item" data-v="${esc(a.name)}"><span class="c-label">${esc(a.name)}</span><span class="c-count">${a.count || ''}</span></div>`).join('')
      || '<div class="combo-empty">无匹配作者，可手动输入新名字</div>';
    list.classList.add('open');
  }
  function pick(name) {
    if (!name) return;
    input.value = name;
    list.classList.remove('open');
    input.focus();
    if (onPick) onPick(name);
  }
  input.addEventListener('focus', () => { input.select(); render(); });
  input.addEventListener('click', render);
  input.addEventListener('input', render);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { list.classList.remove('open'); input.blur(); }
    else if (e.key === 'Enter') {
      const first = list.querySelector('.combo-item');
      if (first) pick(first.dataset.v);
    }
  });
  input.addEventListener('blur', () => setTimeout(() => list.classList.remove('open'), 120));
  let down = false;
  list.addEventListener('mousedown', (e) => {
    const it = e.target.closest('.combo-item');
    if (!it) return;
    e.preventDefault(); down = true;
    pick(it.dataset.v);
    setTimeout(() => { down = false; }, 0);
  });
  list.addEventListener('click', (e) => {
    const it = e.target.closest('.combo-item');
    if (!it || down) return;
    pick(it.dataset.v);
  });
}

// 编辑标签: 过滤已存在标签给出建议; 点选=加建议标签, 回车=添加输入内容 (与旧行为一致)
function makeEditTagSuggest(input, list) {
  if (!input || !list) return;
  _regEditCombo(list, input);
  function haveTags() {
    const ed = $('#lib-tag-editor');
    return new Set(Array.from(ed ? ed.querySelectorAll('.tag.big') : [])
      .map(t => ((t.childNodes[0] || {}).textContent || '').trim().toLowerCase()));
  }
  function render() {
    const q = input.value.trim().toLowerCase();
    const have = haveTags();
    let rows = (facetData.tags || []).filter(t => !have.has(String(t.tag_name).toLowerCase()));
    if (q) rows = rows.filter(t => String(t.tag_name).toLowerCase().includes(q));
    list.innerHTML = rows.slice(0, 30).map(t =>
      `<div class="combo-item" data-v="${esc(t.tag_name)}"><span class="c-label">${esc(t.tag_name)}</span><span class="c-count">${t.count}</span></div>`).join('')
      || (q ? '<div class="combo-empty">无匹配标签，回车直接添加新标签</div>' : '');
    list.classList.add('open');
  }
  function add(v) {
    if (!v) return;
    const inp = $('#lib-tag-input');
    if (!inp) return;
    inp.value = v;
    libAddTag();
    render();
  }
  input.addEventListener('focus', render);
  input.addEventListener('click', render);
  input.addEventListener('input', render);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') list.classList.remove('open');
    else if (e.key === 'Enter') {
      e.preventDefault();
      add(input.value.trim());
    }
  });
  input.addEventListener('blur', () => setTimeout(() => list.classList.remove('open'), 120));
  let down = false;
  list.addEventListener('mousedown', (e) => {
    const it = e.target.closest('.combo-item');
    if (!it) return;
    e.preventDefault(); down = true;
    add(it.dataset.v);
    setTimeout(() => { down = false; }, 0);
  });
  list.addEventListener('click', (e) => {
    const it = e.target.closest('.combo-item');
    if (!it || down) return;
    add(it.dataset.v);
  });
}

let comboAuthor = null, comboTag = null;
function initCombos() {
  if (comboAuthor) return;
  comboAuthor = makeCombo($('#lib-f-author-input'), $('#lib-f-author-list'), {
    itemsRef: () => facetData.authors.map(a => ({ value: a.author_id, label: a.name, count: a.count, aliases: a.aliases || [] })),
    onPick: (v) => { libFilters.author = v; loadLibrary(); },
    digitAsId: true,  // 输入纯数字作者ID可直接精确筛选
  });
  comboTag = makeCombo($('#lib-f-tag-input'), $('#lib-f-tag-list'), {
    itemsRef: () => facetData.tags.map(t => ({ value: t.tag_name, label: t.tag_name, count: t.count })),
    onPick: (v) => { libFilters.tag = v; loadLibrary(); },
  });
  // 输入框右侧清空小叉叉: 有内容时显示, 点击清空并取消对应筛选
  attachInputClear($('#lib-f-author-input'), () => { libFilters.author = ''; loadLibrary(); });
  attachInputClear($('#lib-f-tag-input'), () => { libFilters.tag = ''; loadLibrary(); });
}

async function loadLibFacets() {
  try {
    const f = await api('/api/library/facets');
    facetData = { authors: f.authors || [], tags: f.tags || [], sources: f.sources || [] };
    rebuildSourceFilters(f.sources || []);
    initCombos();
    // 按当前筛选状态回填显示, 切页/刷新不丢已选作者/标签
    comboAuthor.setByValue(libFilters.author || '');
    comboTag.setByValue(libFilters.tag || '');
  } catch (e) { /* 静默 */ }
}

// 用 DB 实际来源(含自定义)重建图库+作者管理两个来源下拉; 保留当前选中值
function rebuildSourceFilters(dbSources) {
  const merged = [];
  const seen = new Set();
  const add = (s) => { if (s && !seen.has(s)) { seen.add(s); merged.push(s); } };
  const hidden = hiddenSourceSet();
  // 预设(去 other, 可被「来源管理」隐藏) + DB 实际来源 + 自定义来源 → 最后固定追加「未知来源(other)」在底部
  [['pixiv','pixiv'],['danbooru','danbooru'],['twitter','X（推特）'],['bilibili','bilibili'],['ggac','GGAC']]
    .forEach(([v, t]) => { if (!hidden.has(v)) add(v); });
  (dbSources || []).forEach(d => { if (d.source !== 'other') add(d.source); });
  customSources().forEach(s => { if (s !== 'other') add(s); });
  add('other');
  const opts = merged.map(v => `<option value="${esq(v)}">${esc(sourceLabel(v))}</option>`).join('');
  ['#lib-f-source', '#authors-f-source'].forEach(selId => {
    const sel = $(selId);
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = `<option value="">来源: 全部</option>` + opts;
    if (cur) sel.value = cur;
  });
}

function syncLibSelUI() {
  const n = libSelected.size;
  $('#lib-sel-count').textContent = n;
  $('#lib-sel-count2').textContent = n;
  $('#lib-sel-count3').textContent = n;
  const cnt4 = $('#lib-sel-count4');
  if (cnt4) cnt4.textContent = n;
  $('#lib-batch-del').disabled = n === 0;
  $('#lib-batch-edit').disabled = n === 0;
  $('#lib-batch-addcart').disabled = n === 0;
  const repairBtn = $('#lib-batch-repair');
  if (repairBtn) repairBtn.disabled = n === 0;
  $('#lib-batch-srcsearch').disabled = n === 0;
  // 作品视图: 选中项里若有「已入库但缺页」的作品, 这个按钮实质是"补缺失页",
  // 文案跟着改, 免得用户以为会重下一遍 
  const addBtn = $('#lib-batch-addcart');
  if (addBtn && libDataView === 'artwork') {
    const selW = ($('#lib-result')._lastItems || []).filter(w => libSelected.has(w._key));
    const missN = selW.filter(w => {
      const total = (w.page_count != null && w.page_count !== '') ? w.page_count : w.total_pages;
      return total != null && (w.pages || []).length < total;
    }).length;
    addBtn.innerHTML = missN
      ? `⬇ 批量补缺失页(<span id="lib-sel-count3">${n}</span>)`
      : `➕ 批量加入下载(<span id="lib-sel-count3">${n}</span>)`;
    addBtn.title = missN
      ? `选中的作品里有 ${missN} 个缺页：只补下载缺失的页，已下好的会自动跳过（永不覆盖本地文件）`
      : '加入下载：已完整的不再重复下、缺页的只补缺失页、没有的才整部下 —— 这个按钮永远不会覆盖本地文件';
  }
  // 🔗 合并图片: 仅图片视图可用 (作品粒度无"归为同一作品"语义), 需 ≥2 张
  const mergeBtn = $('#lib-batch-merge');
  if (mergeBtn) {
    mergeBtn.classList.toggle('hidden', libDataView !== 'image');
    mergeBtn.disabled = n < 2;
  }
  // ✂️ 拆分: 同样仅图片视图 (按「张」选中才有拆的意义), 单张也能拆
  const splitBtn = $('#lib-batch-split');
  if (splitBtn) {
    splitBtn.classList.toggle('hidden', libDataView !== 'image');
    splitBtn.disabled = n === 0;
  }
  const clearBtn = $('#lib-sel-clear');
  if (clearBtn) clearBtn.disabled = n === 0;
  const all = $$('#lib-result input[data-key]');
  const selAllBtn = $('#lib-sel-all');
  if (selAllBtn) selAllBtn.classList.toggle('active', all.length > 0 && all.every(cb => cb.checked));
}

let _libAnchor = null;   // Shift 范围选择的锚点 (上一次点击项的主键: 作品视图=作品ID, 图片视图=作品|页码)
window.libToggleSel = function (cb, ev = {}) {
  const key = String(cb.dataset.key);
  // Shift+点击: 把锚点到当前项之间的所有项一并选中 (网格/列表同顺序; 图片视图按「张」范围选)
  if (ev.shiftKey && _libAnchor && _libAnchor !== key) {
    const order = ($('#lib-result')._lastItems || []).map(w => w._key);
    const a = order.indexOf(_libAnchor), b = order.indexOf(key);
    const keys = (a >= 0 && b >= 0)
      ? order.slice(Math.min(a, b), Math.max(a, b) + 1)
      : [key];
    keys.forEach(k => libSelected.add(k));
    // 让范围内的 checkbox 显示为选中 (只影响可见项)
    $$('#lib-result input[data-key]').forEach(inp => {
      if (keys.indexOf(String(inp.dataset.key)) >= 0) inp.checked = true;
    });
  } else {
    if (cb.checked) libSelected.add(key); else libSelected.delete(key);
    _libAnchor = key;
  }
  syncLibSelUI();
}

// ============ 拖拽框选 (网格视图) ============
// 用法: 在图库网格上按住左键拖出矩形, 框住的卡片全部选中。
// - **普通拖拽 = 追加**: 每次框选都并到现有选择 (再次框选不会清掉之前选中的)
// - **Ctrl/⌘ + 拖拽 = 反向取消**: 把框中的图片从现有选择里移除 (已选中的被取消, 未选中的不变)
// - 想清空重来: 点「✕ 取消选择」
// - 拖动超过阈值(6px)才激活, 纯单击不会产生框选, 也不影响卡片点击行为
// - 拖拽过程中滚轮滚动页面 / 拖到上/下边缘会自动滚动: 框的**左上角固定在按下那一行的文档坐标**,
//   随内容滚动上移, 当前角跟随鼠标 → 从起点行一路框到当前行, 实现「从初始行一直选到末尾行」
const _BOX_T = 6; // 激活框选的拖动阈值 px
const _BOX_EDGE = 48; // 靠近滚动容器上/下边缘多少 px 触发自动滚动
function initBoxSelect(container) {
  if (!container) return;
  let sel = null;           // 当前框选状态
  const clear = () => { sel = null; };

  container.addEventListener('mousedown', (e) => {
    // 仅在左键、图库面板、网格视图下生效; 点交互元素(按钮/链接/输入)不触发
    if (e.button !== 0 || !container.querySelector('.works-grid')) return;
    // 排除对交互控件的误触: checkbox/label/button/a/input
    // 文字元素(标题/作者/元信息/标签/路径等)也排除 → 交给浏览器原生文本选择, 可拖选复制
    const t = e.target.closest('input, button, a, select, textarea, .card-sel, .w-actions, .w-path, .w-title, .w-meta, .w-pages, .w-file, .w-tags, .w-author, .w-link, .tag');
    if (t) return;

    // 实际滚动的容器: 页面布局里是 main (window 不滚), 兜底到 documentElement
    const scroller = container.closest('main') || document.scrollingElement || document.documentElement;

    sel = {
      docX0: e.clientX + scroller.scrollLeft,   // 起始角: 文档坐标 (滚动后仍固定在初始那一行)
      docY0: e.clientY + scroller.scrollTop,
      cx: e.clientX, cy: e.clientY,   // 最近一次鼠标视口坐标 (滚轮滚动时没有 mousemove, 用它重算)
      ctrl: e.ctrlKey || e.metaKey,   // Ctrl/⌘ = 重新框选 (替代现有选择)
      box: document.createElement('div'),
      active: false,
      scroller,
      scrollRaf: 0,
    };
    // 容器在文档坐标系中的位置 (main 滚动时恒定, 用作框的绝对定位基准)
    const cr0 = container.getBoundingClientRect();
    sel.docRectLeft = cr0.left + scroller.scrollLeft;
    sel.docRectTop = cr0.top + scroller.scrollTop;
    sel.box.className = 'lib-boxsel';
    container.appendChild(sel.box);

    // 统一重算: 框矩形按「文档坐标」计算 —— 起始角固定在按下那一刻所在的内容行 (随滚动上移),
    // 当前角跟随鼠标; 滚轮下滑时框从起始行一路延伸到鼠标所在行, 从而「从起点行一直选到末尾行」。
    const update = () => {
      if (!sel) return;
      const sl = sel.scroller.scrollLeft, st = sel.scroller.scrollTop;
      const docCX = sel.cx + sl, docCY = sel.cy + st;   // 鼠标当前文档坐标
      const bx0 = Math.min(sel.docX0, docCX), by0 = Math.min(sel.docY0, docCY);
      const bx1 = Math.max(sel.docX0, docCX), by1 = Math.max(sel.docY0, docCY);
      sel.box.style.left = (bx0 - sel.docRectLeft) + 'px';
      sel.box.style.top = (by0 - sel.docRectTop) + 'px';
      sel.box.style.width = (bx1 - bx0) + 'px';
      sel.box.style.height = (by1 - by0) + 'px';
      // 命中判定: 卡片文档坐标 = 视口坐标 + 当前滚动量, 与框文档矩形比较
      container.querySelectorAll('.work-card').forEach(card => {
        const cr = card.getBoundingClientRect();
        const crl = cr.left + sl, crt = cr.top + st;
        const overlap = !(crl > bx1 || crt > by1 || crl + cr.width < bx0 || crt + cr.height < by0);
        card.classList.toggle('box-hit', overlap);
      });
    };

    const onScroll = () => { if (sel && sel.active) update(); };
    const onMove = (ev) => {
      if (!sel) return;
      sel.cx = ev.clientX; sel.cy = ev.clientY;
      // 阈值判定用文档坐标移动量 (滚动不影响)
      const ddx = (ev.clientX + sel.scroller.scrollLeft) - sel.docX0;
      const ddy = (ev.clientY + sel.scroller.scrollTop) - sel.docY0;
      if (!sel.active && Math.abs(ddx) < _BOX_T && Math.abs(ddy) < _BOX_T) {
        // 还没超过阈值, 不显示选框
        return;
      }
      if (!sel.active) {
        sel.active = true;
        sel.box.style.display = 'block';
        container.classList.add('lib-boxsel-active');
        document.body.style.userSelect = 'none';  // 防拖动时选中文字/图片闪蓝
        sel.scroller.addEventListener('scroll', onScroll);
      }
      update();
      // 边缘自动滚动: 鼠标靠近滚动容器上/下边缘时持续滚动, 实现「一路拖到底」
      const sr = sel.scroller.getBoundingClientRect();
      const inY = ev.clientY >= sr.top - _BOX_EDGE && ev.clientY <= sr.bottom + _BOX_EDGE;
      if (inY && sel.active) {
        sel.dist = ev.clientY < sr.top + _BOX_EDGE
          ? ev.clientY - (sr.top + _BOX_EDGE)
          : ev.clientY > sr.bottom - _BOX_EDGE ? ev.clientY - (sr.bottom - _BOX_EDGE) : 0;
        if (!sel.scrollRaf) {
          const step = () => {
            if (!sel || !sel.active) { sel.scrollRaf = 0; return; }
            if (sel.dist !== 0) sel.scroller.scrollTop += sel.dist * 0.25;
            sel.scrollRaf = requestAnimationFrame(step);
          };
          sel.scrollRaf = requestAnimationFrame(step);
        }
      } else if (sel.scrollRaf) {
        cancelAnimationFrame(sel.scrollRaf); sel.scrollRaf = 0;
      }
    };
    const onUp = (ev) => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = '';      // 恢复文本选择
      document.body.style.cursor = '';
      if (!sel) return;
      sel.scroller.removeEventListener('scroll', onScroll);
      if (sel.scrollRaf) { cancelAnimationFrame(sel.scrollRaf); sel.scrollRaf = 0; }
      container.classList.remove('lib-boxsel-active');
      const box = sel.box;
      try { container.removeChild(box); } catch (e) { /* 节点已被列表刷新替换 */ }
      if (!sel.active) { clear(); return; }   // 没拖成矩形, 当普通点击
      // 标记本次为真实拖拽, 让随后补发的 click 不再触发卡片切换选中
      container.dataset.boxDragged = '1';
      // 最终选区: 与 update 同一文档坐标算法 (鼠标文档坐标 = 视口坐标 + 当前滚动量)
      const sl = sel.scroller.scrollLeft, st = sel.scroller.scrollTop;
      const docCX = ev.clientX + sl, docCY = ev.clientY + st;
      const bx0 = Math.min(sel.docX0, docCX), by0 = Math.min(sel.docY0, docCY);
      const bx1 = Math.max(sel.docX0, docCX), by1 = Math.max(sel.docY0, docCY);
      let keys = [];
      container.querySelectorAll('.work-card').forEach(card => {
        card.classList.remove('box-hit');
        const cr = card.getBoundingClientRect();
        const crl = cr.left + sl, crt = cr.top + st;
        const overlap = !(crl > bx1 || crt > by1 || crl + cr.width < bx0 || crt + cr.height < by0);
        if (overlap) {
          const cb = card.querySelector('input[data-key]');
          if (cb) keys.push(String(cb.dataset.key));
        }
      });
      if (keys.length) {
        if (sel.ctrl) {
          // Ctrl/⌘ + 拖拽 = 反向取消: 把框中的项从现有选择里移除
          keys.forEach(k => libSelected.delete(k));
        } else {
          // 普通拖拽 = 追加到现有选择 (再次框选不会清掉之前选中的)
          keys.forEach(k => libSelected.add(k));
        }
        // 同步 checkbox 显示
        container.querySelectorAll('input[data-key]').forEach(inp => {
          inp.checked = libSelected.has(String(inp.dataset.key));
        });
      }
      clear();
      syncLibSelUI();
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // 阻止图片被浏览器原生拖拽 (否则拖图会触发 HTML5 drag 而不是框选)
  container.addEventListener('dragstart', (e) => { e.preventDefault(); });
}

async function libDeleteOne(id) {
  if (!confirm(`把作品 ${id} 移入回收站？\n\n图片文件会一起移入，随时可在「🗑️ 回收站」里恢复。`)) return;
  try {
    const r = await api('/api/library/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: [id] }) });
    libSelected.delete(String(id));
    toast(`已把 ${r.deleted} 个作品移入回收站（可恢复）` + (r.files_removed ? ` · 移动 ${r.files_removed} 个文件` : '') + ' 🗑️', 'ok');
    loadLibrary(); loadStats(); loadTrashBadge();
  } catch (e) { toast('移入回收站失败: ' + e.message, 'err'); }
}

// 「删到作品全空」时让设计约定: 保留作品(0页) / 作品也一并进回收站 / 取消。
// 为什么不用 confirm: 三选一里 true/false 说不清"取消"和"保留"的区别。
function _askEmptyChoice(labels) {
  return new Promise((resolve) => {
    openLibModal('这个作品要被删空了', `
      <div class="hint">删掉这些图片后，作品 <b>${esc(labels)}</b> 会变成 <b>0 页</b>。</div>
      <div class="hint" style="margin-top:8px">作品本身怎么处理？</div>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn accent" id="emp-keep">保留作品（显示 0 页）</button>
        <button class="btn danger" id="emp-trash">作品也一并移入回收站</button>
        <button class="btn ghost" id="emp-cancel">取消</button>
      </div>`);
    const k = $('#emp-keep'), t = $('#emp-trash'), c = $('#emp-cancel');
    if (k) k.onclick = () => { closeLibModal(); resolve('keep'); };
    if (t) t.onclick = () => { closeLibModal(); resolve('trash'); };
    if (c) c.onclick = () => { closeLibModal(); resolve(null); };
  });
}

// 「删空二选一」弹窗要显示的"是哪些作品"文案。
// ⚠ 2026-09-18 修的真 bug: 服务端 `would_empty_labels` 是**人读字符串**(如「《标题》· 作者 X」),
//   不是数组 —— 老代码对它 `.join('、')` 直接抛 TypeError, 被外层 catch 吞掉变成
//   「移入回收站失败: …join is not a function」toast, 用户看到的就是"点删除没反应、
//   点几次都一样"(浏览器实测复现)。所以这里两种形状都容错。
function _emptyChoiceLabels(r) {
  const s = r && r.would_empty_labels;
  if (typeof s === 'string' && s) return s;
  const a = r && r.would_empty;
  if (Array.isArray(a)) return a.join('、');
  return (a === null || a === undefined) ? '' : String(a);
}

// 图片视图: 把单张图片移入回收站 (作品|页码)。
// 后端若发现"这会把作品删空", 会先返回 need_choice 让设计约定再执行。
async function _softDeletePages(items) {
  const post = (extra) => api('/api/library/delete-pages', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items, ...(extra || {}) }),
  });
  let r = await post(null);
  if (r && r.status === 'need_choice') {
    const choice = await _askEmptyChoice(_emptyChoiceLabels(r));
    if (choice === null) return null;
    r = await post({ cascade_choice: choice });
  }
  return r;
}

window.libDeletePage = async function (aid, pageNum) {
  const pn = Number(pageNum) || 0;
  if (!confirm(`把这张图片移入回收站？（作品 ${aid} · 第 ${pn + 1} 张）\n\n随时可在「🗑️ 回收站」里恢复。`)) return;
  try {
    const r = await _softDeletePages([{ artwork_id: String(aid), page_num: pn }]);
    if (!r) return;
    libSelected.delete(`${aid}|${pn}`);
    toast(`已把 ${r.pages_deleted} 张图片移入回收站（可恢复）`
          + (r.artworks_trashed ? ` · ${r.artworks_trashed} 个作品一并移入` : '') + ' 🗑️', 'ok');
    loadLibrary(); loadStats(); loadLibFacets(); loadTrashBadge();
  } catch (e) { toast('移入回收站失败: ' + e.message, 'err'); }
};

async function libBatchDelete() {
  if (!libSelected.size) return;
  try {
    if (libDataView === 'image') {
      // 图片视图: 选中粒度 = 单张图片 (作品|页码), 只删被勾选的页
      const items = [...libSelected].map(parsePageKey);
      if (!confirm(`把选中的 ${items.length} 张图片移入回收站？\n\n随时可在「🗑️ 回收站」里恢复。`)) return;
      const r = await _softDeletePages(items);
      if (!r) return;
      libSelected.clear();
      _libAnchor = null;
      toast(`已把 ${r.pages_deleted} 张图片移入回收站（可恢复）`
            + (r.artworks_trashed ? ` · ${r.artworks_trashed} 个作品一并移入` : '') + ' 🗑️', 'ok');
    } else {
      // 作品视图: 选中粒度 = 作品, 整个作品移入回收站
      if (!confirm(`把选中的 ${libSelected.size} 个作品移入回收站？\n\n图片文件会一起移入，随时可在「🗑️ 回收站」里恢复。`)) return;
      const r = await api('/api/library/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: [...libSelected] }) });
      libSelected.clear();
      _libAnchor = null;
      toast(`已把 ${r.deleted} 个作品移入回收站（可恢复）` + (r.files_removed ? ` · 移动 ${r.files_removed} 个文件` : '') + ' 🗑️', 'ok');
    }
    loadLibrary(); loadStats(); loadLibFacets(); loadTrashBadge();
  } catch (e) { toast('移入回收站失败: ' + e.message, 'err'); }
}

// ============ 🗑️ 回收站页 (2026-09-17) ============
// 双层保护的前端: 删除=移进来(可恢复), 彻底删除=卸载记录且文件只进 Windows 电脑回收站。
// 恢复遇冲突由后端判、只跳过不覆盖, 前端如实把"哪几项被跳过、为什么"摆给用户看。
let trashKind = 'artwork';

window.trashSwitchKind = function (kind) {
  trashKind = (kind === 'page') ? 'page' : 'artwork';
  $$('#tab-trash .trash-kind').forEach(b => b.classList.toggle('active', b.dataset.kind === trashKind));
  loadTrash();
};

function _trashReasonLabel(x) {
  return { user: '手动删除', cascade: '作品删空后一并移入', cleanup: '系统清理' }[x] || x || '';
}

async function loadTrashBadge() {
  try {
    const r = await api('/api/library/trash/count');
    const n = Number((r && r.artworks) || 0) + Number((r && r.pages) || 0);
    const b = $('#trash-badge');
    if (!b) return;
    b.textContent = String(n);
    b.classList.toggle('hidden', !n);
  } catch (e) { /* 角标失败不影响主流程 */ }
}

async function loadTrash() {
  const cont = $('#trash-result');
  if (!cont) return;
  cont.innerHTML = '<div class="hint">加载中…</div>';
  let r;
  try { r = await api('/api/library/trash/list?kind=' + trashKind); }
  catch (e) { cont.innerHTML = '<div class="hint">加载失败：' + esc(e.message) + '</div>'; return; }
  const items = (r && r.items) || [];
  const tot = $('#trash-total');
  if (tot) tot.textContent = items.length
    ? `共 ${items.length} ${trashKind === 'artwork' ? '个作品' : '张图片'}` : '';
  // 空的时候把批量按钮禁掉: 点了只能得到"已彻底删除 0 项"这种没意义的提示 (浏览器实测后改)
  const raBtn = $('#trash-restore-all'), paBtn = $('#trash-purge-all');
  if (raBtn) raBtn.disabled = !items.length;
  if (paBtn) paBtn.disabled = !items.length;
  if (!items.length) {
    cont.innerHTML = '<div class="hint">回收站是空的 ✓</div>';
    loadTrashBadge();
    return;
  }
  cont.innerHTML = (trashKind === 'artwork') ? _trashArtworkHtml(items) : _trashPageHtml(items);
  loadTrashBadge();
}

function _trashArtworkHtml(items) {
  const rows = items.map(x => {
    const id = esc(x.artwork_id);
    // 缩略图必须走 imgSrc: 安全模式下 R18 会被换成占位图。
    // (第一版这里直接写 x.thumb 的真实地址, 等于让回收站成了打码的漏网口子 —— 2026-09-17 修)
    const thumb = x.thumb
      ? `<img src="${esc(imgSrc(x.thumb, x))}" class="trash-thumb" loading="lazy" alt="">`
      : '<span class="hint">无图</span>';
    return `<tr>
      <td>${thumb}</td>
      <td class="cell-path"><b>${esc(x.title || '(无标题)')}</b>
        <div class="hint mono">${id}</div></td>
      <td>${Number(x.page_count) || 0}</td>
      <td class="hint">${esc(x.trashed_at || '')}</td>
      <td class="hint">${esc(_trashReasonLabel(x.trashed_reason))}</td>
      <td style="white-space:nowrap">
        <button class="btn ghost" onclick="trashRestoreArtwork('${id}')">↩ 恢复</button>
        <button class="btn danger" onclick="trashPurgeArtwork('${id}')">🗑️ 彻底删除</button>
      </td></tr>`;
  }).join('');
  return '<div class="table-wrap"><table class="table"><thead><tr>'
    + '<th>缩略图</th><th>作品</th><th>页数</th><th>移入时间</th><th>原因</th><th>操作</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function _trashPageHtml(items) {
  const rows = items.map(x => {
    const aid = esc(x.artwork_id);
    const pn = Number(x.page_num) || 0;
    const thumb = x.thumb
      ? `<img src="${esc(imgSrc(x.thumb, x))}" class="trash-thumb" loading="lazy" alt="">`
      : '<span class="hint">无图</span>';
    return `<tr>
      <td>${thumb}</td>
      <td class="cell-path"><b>${esc(x.title || '(无标题)')}</b>
        <div class="hint mono">${aid} · 第 ${pn + 1} 张</div></td>
      <td class="hint">${esc(x.trashed_at || '')}</td>
      <td style="white-space:nowrap">
        <button class="btn ghost" onclick="trashRestorePage('${aid}', ${pn})">↩ 恢复</button>
        <button class="btn danger" onclick="trashPurgePage('${aid}', ${pn})">🗑️ 彻底删除</button>
      </td></tr>`;
  }).join('');
  return '<div class="table-wrap"><table class="table"><thead><tr>'
    + '<th>缩略图</th><th>图片</th><th>移入时间</th><th>操作</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

async function _trashRestore(body) {
  try {
    const r = await api('/api/library/trash/restore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const sk = (r && r.skipped) || [];
    if (sk.length) {
      // 冲突项必须让用户看清楚"为什么没恢复" —— 静默跳过等于让用户以为坏了
      const lines = sk.map(s => `· ${s.item.artwork_id}`
        + (s.item.page_num != null ? ` 第 ${Number(s.item.page_num) + 1} 张` : '')
        + `：${s.detail}`).join('\n');
      alert(`已恢复 ${r.restored} 项。\n\n有 ${sk.length} 项因冲突被跳过（仍留在回收站，绝不会覆盖图库现有内容）：\n\n`
            + lines + '\n\n可先在图库里处理冲突（改名 / 移走占位文件 / 删除占位作品）后再试。');
    } else {
      toast(`已恢复 ${r.restored} 项 ↩`, 'ok');
    }
    loadTrash(); loadLibrary(); loadStats(); loadLibFacets();
  } catch (e) { toast('恢复失败: ' + e.message, 'err'); }
}

window.trashRestoreArtwork = (id) => _trashRestore({ ids: [String(id)] });
window.trashRestorePage = (aid, pn) => _trashRestore(
  { items: [{ artwork_id: String(aid), page_num: Number(pn) }] });

async function _trashPurge(body) {
  try {
    const r = await api('/api/library/trash/purge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const bad = (r && r.recycle_failed) || [];
    toast(`已彻底删除 ${r.deleted} 项 · 文件送电脑回收站 ${r.recycled} 个`
          + (bad.length ? `（${bad.length} 个没能送进回收站）` : ''), 'ok');
    loadTrash(); loadLibrary(); loadStats();
  } catch (e) { toast('彻底删除失败: ' + e.message, 'err'); }
}

window.trashPurgeArtwork = async function (id) {
  if (!confirm(`彻底删除作品 ${id}？\n\n数据库记录会被卸载；图片文件会被送进 **Windows 电脑回收站**（仍可从那里还原），并记入操作日志。`)) return;
  await _trashPurge({ ids: [String(id)] });
};
window.trashPurgePage = async function (aid, pn) {
  if (!confirm(`彻底删除这张图片（作品 ${aid} · 第 ${Number(pn) + 1} 张）？\n\n记录会被卸载；文件送进 **Windows 电脑回收站**（仍可还原）。`)) return;
  await _trashPurge({ items: [{ artwork_id: String(aid), page_num: Number(pn) }] });
};

window.trashRestoreAll = async function () {
  let r0;
  try { r0 = await api('/api/library/trash/list?kind=' + trashKind); } catch (e) { r0 = null; }
  const items = (r0 && r0.items) || [];
  if (!items.length) { toast('回收站是空的', 'info'); return; }
  if (!confirm(`恢复当前视图的全部 ${items.length} 项？\n\n与图库现有内容冲突的项会被跳过，绝不会覆盖。`)) return;
  if (trashKind === 'artwork') {
    await _trashRestore({ ids: items.map(x => String(x.artwork_id)) });
  } else {
    await _trashRestore({ items: items.map(x => ({
      artwork_id: String(x.artwork_id), page_num: Number(x.page_num) })) });
  }
};

window.trashPurgeAll = async function () {
  if (!confirm('清空回收站？\n\n所有记录会被卸载；图片文件全部送进 **Windows 电脑回收站**（仍可还原），并记入操作日志。\n\n这是最深的一层删除，确定继续？')) return;
  if (!confirm('再次确认：真的要清空回收站吗？')) return;
  await _trashPurge({ all: true });
};

// 🔗 合并图片: 把选中的多张图片归为同一个作品的多页
async function openMergeImages() {
  if (libDataView !== 'image' || libSelected.size < 2) return;
  const keys = [...libSelected];
  const items = keys.map(parsePageKey);
  const loaded = ($('#lib-result')._lastItems || []);
  const byAid = {};
  const byKey = {};
  loaded.forEach(w => {
    byAid[w.artwork_id] = byAid[w.artwork_id] || w;
    byKey[w._key] = w;
  });
  const artIds = [...new Set(items.map(it => it.artwork_id))];
  // 只有 pixiv 作品的「原网页页」(origin !== 'merged') 不能搬走 —— 会让原作品缺页,
  // 且合并后无法归属回原作品; **合并附加页**(用户自己并进来的)可以搬, 那正是「反悔合并」。
  // 其他来源/未知来源的作品不受限。后端有完全相同的页级闸门, 这里提前告知免得白操作。
  const blocked = keys.filter(k => {
    const w = byKey[k] || {};
    if (!/^\d+$/.test(String(w.artwork_id || ''))) return false;
    return (w.origin || 'web') !== 'merged';
  });
  if (blocked.length) {
    const names = blocked.slice(0, 3)
      .map(k => { const w = byKey[k] || {}; return `${w.artwork_id}_p${w.page_num}`; })
      .join('、');
    openLibModal('🔗 无法合并', `
      <div class="hint err-text" style="margin-bottom:10px">
        选中的图片里有 <b>${blocked.length} 张是 pixiv 作品的原网页页</b>（${esc(names)}${blocked.length > 3 ? '…' : ''}）。
      </div>
      <div class="hint">pixiv 作品的原网页不能合并到别处：
        <br>· 原作品会因此缺页（原本完整的页数被拆散）；
        <br>· 合并后那些图片也无法再自动归属回原 pixiv 作品。
        <br><br>可以搬的是<b>合并进来的附加页</b>（页码带 * 标记），那属于「反悔合并」。
        <br>其他来源 / 未知来源的图片不受限，可自由合并。</div>
      <div class="modal-actions"><button class="btn ghost" onclick="closeLibModal()">知道了</button></div>`);
    return;
  }
  const artOptions = artIds.map(aid => {
    const w = byAid[aid] || {};
    const label = w.title ? ` · ${String(w.title).slice(0, 26)}` : '';
    return `<option value="${esq(aid)}">作品 ${esc(aid)}${esc(label)}</option>`;
  }).join('');
  openLibModal(`🔗 合并 ${items.length} 张图片为同一作品`, `
    <div class="hint" style="margin-bottom:10px">将选中的 ${items.length} 张图片合并为一个作品的多页。并入已有作品时页码追加到末尾；新建作品时页码从 0 重排。<b>图片文件只搬进目标作品目录，文件名保持原样</b>（同名才加「 (2)」），不会改成内部编号。</div>
    <div class="be-row"><label><input type="radio" name="merge-target" value="existing" checked> 并入已有作品</label>
      <select class="be-ctl" id="merge-existing">${artOptions}</select></div>
    <div class="be-row"><label><input type="radio" name="merge-target" value="new"> 新建作品</label>
      <input type="text" class="be-ctl" id="merge-new-title" placeholder="新作品标题（可选）"></div>
    <div class="modal-actions">
      <button class="btn accent" onclick="saveMergeImages()">合并</button>
      <button class="btn ghost" onclick="closeLibModal()">取消</button>
    </div>`);
}

// ➕ 批量加入清单: 选中作品 → 批量入队并自动开始下载 (图片视图按张选中归并到所属作品去重)
window.libBatchAddCart = async function () {
  const keys = [...libSelected];
  if (!keys.length) return;
  const loaded = ($('#lib-result')._lastItems || []);
  const byKey = {};
  loaded.forEach(w => { byKey[w._key] = w; });
  const artMap = new Map();   // aid -> {artwork_id,title,author_id,author_name,page_count}
  keys.forEach(k => {
    const w = byKey[k] || {};
    const aid = w.artwork_id || (k.indexOf('|') >= 0 ? k.split('|')[0] : k);
    if (!aid || artMap.has(aid)) return;
    artMap.set(aid, {
      artwork_id: aid,
      title: w.title || '',
      author_id: w.author_id || null,
      author_name: w.author_name || null,
      page_count: (w.page_count != null ? w.page_count : w.total_pages) || 1,
    });
  });
  const items = [...artMap.values()];
  try {
    const r = await api('/api/cart/batch-add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }) });
    const parts = [];
    if (r.added) parts.push(`新加入 ${r.added} 个`);
    if (r.already_downloaded) parts.push(`已下载 ${r.already_downloaded} 个（跳过）`);
    if (r.in_queue) parts.push(`已在下载列表 ${r.in_queue} 个（跳过）`);
    if (r.invalid) parts.push(`无效 ${r.invalid} 个`);
    if (skipForeign) parts.push(`非 pixiv 来源 ${skipForeign} 个未加入`);
    if (r.added > 0) {
      toast('已加入清单，自动开始下载 ⬇（' + parts.join('，') + '）', 'ok');
      await autoStartDownload();
    } else {
      // 全部已在库/在队列: 用 info 色, 与「成功新增并开始下载」的绿色区分
      toast('✓ 没有新任务：' + parts.join('，'), 'info');
    }
    libSelected.clear(); _libAnchor = null; syncLibSelUI();
    setLastAction('library', [], '刚加入的下载任务');
  } catch (e) { toast('批量加入清单失败: ' + e.message, 'err'); }
};

// ♻ 重下修复: 用官方源【覆盖】重下选定作品 (旧文件自动备份为 .bak)。
//   与「加入下载」的差别只在意图显式声明 —— 这个动作永远覆盖, 所以批量也没有"该覆盖还是跳过"的歧义。
//   批量按钮 / 体检报告的缺失·损坏分组 / 单作品入口都走这一个函数。
async function repairWorks(ids, label) {
  const pixivIds = [...new Set((ids || []).map(x => String(x).trim()).filter(a => /^\d+$/.test(a)))];
  if (!pixivIds.length) {
    toast('没有可重下的 pixiv 作品（其他来源/合并产物没有官方源）', 'err'); return false;
  }
  if (!confirm(`♻ 重下修复 ${pixivIds.length} 个作品？\n\n`
    + `· 会用 pixiv 官方源【覆盖】本地文件（内容以官方当前版本为准）\n`
    + `· 被覆盖的旧文件自动备份为 .bak，可在「💾 备份」里对比后一键恢复\n`
    + `· 只跑这几个作品，不影响下载队列里的其他任务`
    + (label ? `\n\n范围：${label}` : ''))) return false;
  try {
    const r = await api('/api/cart/repair', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: pixivIds }) });
    const err = apiErr(r);
    if (err) throw new Error(err);
    const parts = [];
    if (r.added) {
      parts.push(`已开始覆盖重下 ${r.added} 个` +
        (r.backed_up ? `（旧文件已备份 ${r.backed_up} 张）` : '（本地还没有可覆盖的文件）'));
    }
    if (r.in_queue) parts.push(`已在下载列表 ${r.in_queue} 个（跳过）`);
    if ((r.skipped || []).length) {
      parts.push(`跳过 ${r.skipped.length} 个`);
      console.warn('[重下修复] 跳过明细', r.skipped);
    }
    toast((r.added ? '♻ ' : '✓ ') + (parts.join('，') || '没有可重下的作品')
      + (r.added ? '，进度见「下载任务」页' : ''), r.added ? 'ok' : 'info');
    refreshCart();
    if (r.added) setLastAction('library', [], '刚重下修复的作品');
    return !!r.added;
  } catch (e) { toast('重下修复失败: ' + e.message, 'err'); return false; }
}
window.repairWorks = repairWorks;

// 单作品入口 (编辑弹窗里那行)
window.repairOneWork = function (aid) {
  if (!/^\d+$/.test(String(aid))) { toast('非 pixiv 作品，没有官方源可重下', 'err'); return; }
  repairWorks([aid], `作品 ${aid}`);
};

// 体检报告里「⚠ 文件丢失」「💥 损坏图片」两组的批量入口
window.chkRepairGroup = function (type) {
  const ids = (window._chkItems || [])
    .filter(x => x.type === type && x.artwork_id).map(x => String(x.artwork_id));
  repairWorks(ids, type === 'corrupt' ? '体检报告 · 损坏图片' : '体检报告 · 文件丢失');
};

// 从图库选中项触发 (批量操作行的「♻ 重下修复」)
window.libBatchRepair = async function () {
  const keys = [...libSelected];
  if (!keys.length) return;
  const loaded = ($('#lib-result')._lastItems || []);
  const byKey = {};
  loaded.forEach(w => { byKey[w._key] = w; });
  const aids = [...new Set(keys.map(k => {
    const w = byKey[k] || {};
    return String(w.artwork_id || (k.indexOf('|') >= 0 ? k.split('|')[0] : k) || '');
  }))].filter(Boolean);
  if (!aids.length) return;
  const ok = await repairWorks(aids, `图库选中 ${aids.length} 项`);
  if (ok) { libSelected.clear(); _libAnchor = null; syncLibSelUI(); }
};

// 🔎 批量搜索来源: 已记录 URL 的直接归类(瞬间), 其余逐个 SauceNAO 反搜(慢, 后台串行)。
// 反搜命中只作参考, 不自动改库 —— 由用户在「搜索来源/批量编辑」里确认。
window.batchSourceSearch = async function () {
  const loaded = ($('#lib-result')._lastItems || []);
  const byKey = {};
  loaded.forEach(w => { byKey[w._key] = w; });
  const aids = [...new Set([...libSelected].map(k => {
    const w = byKey[k] || {};
    return w.artwork_id || (k.indexOf('|') >= 0 ? k.split('|')[0] : k);
  }))].filter(Boolean);
  if (!aids.length) return;
  if (!confirm(`将对选中的 ${aids.length} 个作品搜索来源：\n\n`
    + `· 已记录来源 URL 的 → 直接归类（瞬间完成）\n`
    + `· 其余的 → SauceNAO 反搜，每张约 5~15 秒，可能较久\n\n`
    + `反搜结果只作参考，不会自动改动作品。继续？`)) return;

  switchTab('reverse');
  $('#rev-gallery-result').innerHTML = '<div class="hint">🔄 正在搜索来源…</div>';
  try {
    await api('/api/library/batch-source-search', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artwork_ids: aids }) });
  } catch (e) {
    $('#rev-gallery-result').innerHTML =
      `<div class="hint err-text">启动失败: ${esc(e.message)}</div>`;
    return;
  }
  clearInterval(galleryPollTimer);
  galleryPollTimer = setInterval(async () => {
    let st = {};
    try { st = (await api('/api/reverse/status')).src_search || {}; } catch (e) { /* 忽略 */ }
    renderReverseTasks(await loadReverseStatus());
    const b = st.batch;
    if (!b) return;
    if (st.running) {
      $('#rev-gallery-result').innerHTML =
        `<div class="hint">🔄 搜索来源 ${b.done}/${b.total}`
        + (st.current ? ` · 当前 ${esc(st.current)}` : '') + '…</div>';
      return;
    }
    clearInterval(galleryPollTimer);
    renderBatchSourceResult(b);
  }, 1500);
};

let _batchSourceItems = [];   // 最近一次「批量搜索来源」的结果, 供逐条/一键应用

// 把某条批量搜索结果写入作品 (来源/来源URL/来源作者/作者) —— 一键完成, 不用手抄
window.applyBatchSource = async function (i) {
  const it = _batchSourceItems[i];
  if (!it || it.applied) return;
  const body = { artwork_id: it.artwork_id };
  if (it.url) body.source_url = it.url;
  if (it.site) body.source = it.site;
  const sa = it.handle || it.author_name || '';
  if (sa) body.source_author = sa;
  if (it.author_name) body.author_name = it.author_name;
  if (it.author_id) body.author_id = it.author_id;
  if (Object.keys(body).length <= 1) { toast('该条目没有可写入的信息', 'info'); return; }
  try {
    const r = await api('/api/library/source-set', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    // api() 对「带 JSON 的错误响应」不抛异常 → 必须显式判 status,
    // 否则写入失败也会被标成「已写入」
    if (!r || r.status !== 'ok') throw new Error((r && r.error) || '写入失败');
    it.applied = true;
    renderBatchSourceResult({ items: _batchSourceItems });
    toast(`已写入 ${it.artwork_id} 的来源 ✓`, 'ok');
  } catch (e) { toast(`写入 ${it.artwork_id} 失败: ` + e.message, 'err'); }
};

window.applyAllBatchSource = async function () {
  const todo = [];
  _batchSourceItems.forEach((x, i) => {
    if (!x.applied && (x.status === 'hit' || x.site || x.url)) todo.push(i);
  });
  if (!todo.length) { toast('没有可应用的条目', 'info'); return; }
  if (!confirm(`将把 ${todo.length} 条来源信息写入对应作品（不会覆盖已有的 pixiv 作者），继续？`)) return;
  for (const i of todo) await window.applyBatchSource(i);
};

function renderBatchSourceResult(b) {
  const items = b.items || [];
  _batchSourceItems = items;
  const stTxt = { classified: '✅ 已归类', hit: '🎯 命中', miss: '— 未命中',
                  error: '✖ 失败', queued: '… 未处理', pending: '… 未处理' };
  const rows = items.map((it, i) => {
    const link = it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">打开</a>` : '';
    const extra = it.error ? `<span class="err-text">${esc(it.error)}</span>`
      : esc([it.label, it.site, it.author_name, it.handle].filter(Boolean).join(' · '));
    let act = '';
    if (it.applied) act = '<span style="color:var(--success)">✓ 已写入</span>';
    else if (it.status === 'hit' || it.site || it.url)
      act = `<button class="btn accent" style="padding:2px 8px;font-size:12px"`
        + ` onclick="applyBatchSource(${i})">✅ 应用</button>`;
    return `<tr><td style="padding:4px 8px">${esc(it.artwork_id)}</td>`
      + `<td style="padding:4px 8px">${stTxt[it.status] || esc(it.status)}</td>`
      + `<td style="padding:4px 8px">${extra}</td>`
      + `<td style="padding:4px 8px">${link}</td>`
      + `<td style="padding:4px 8px">${act}</td></tr>`;
  }).join('');
  const hitN = items.filter(x => x.status === 'hit').length;
  const doneN = items.filter(x => x.applied).length;
  const restN = items.filter(x => !x.applied && (x.status === 'hit' || x.site || x.url)).length;
  $('#rev-gallery-result').innerHTML =
    `<div class="hint">完成：共 ${items.length} 个 · 自动归类 ${b.auto_classified || 0} 个`
    + ` · 反搜命中 ${hitN} 个`
    + (doneN ? ` · 已写入 ${doneN} 条` : '')
    + `</div>`
    + (restN ? `<div style="margin:8px 0"><button class="btn accent"`
        + ` onclick="applyAllBatchSource()">✅ 一键应用全部（${restN} 条）</button></div>` : '')
    + `<table style="width:100%;font-size:12px;border-collapse:collapse;margin-top:6px">`
    + `<thead><tr><th style="text-align:left;padding:4px 8px">作品</th>`
    + `<th style="text-align:left;padding:4px 8px">结果</th>`
    + `<th style="text-align:left;padding:4px 8px">详情</th>`
    + `<th style="text-align:left;padding:4px 8px">链接</th>`
    + `<th style="text-align:left;padding:4px 8px">操作</th></tr></thead>`
    + `<tbody>${rows}</tbody></table>`
    + `<div class="hint" style="margin-top:8px">「应用」= 写入来源站点/来源URL/来源作者`
    + `（若反搜给出作者，一并建作者并关联，但不会覆盖已有 pixiv 作者）。</div>`;
}

// 🔎 搜索来源: 先解析已记录 URL 归类(快) → 无/解析不出才 SauceNAO 反搜 → 未命中填 URL
async function sourceSearch(aid) {
  let det;
  try {
    det = await api('/api/library/source-detect?artwork_id=' + encodeURIComponent(aid));
  } catch (e) { toast('检测来源失败: ' + e.message, 'err'); return; }
  const d = det.detected;
  if (det.has_url && d) {           // 有 URL 且能归类 → 确认
    // 本地占位作品 (merged_/ext_) 命中 pixiv 作品 ID → 主推「认领转正」(ID/标题/作者/页数一次性合并回来)
    const canClaim = d.site === 'pixiv' && d.artwork_id
      && !/^\d+$/.test(String(aid)) && String(aid) !== String(d.artwork_id);
    openLibModal(`🔎 搜索来源 · ${esc(aid)}`,
      `<div class="hint">已记录来源 URL，识别为 <b>${esc(d.label)}</b>${d.note ? '（' + esc(d.note) + '）' : ''}：</div>
       <div class="modal-field"><input type="text" value="${esc(det.url)}" readonly>
         <a class="btn ghost" href="${esc(det.url)}" target="_blank" rel="noopener"
            style="margin-top:6px;display:inline-block">🔗 打开这个链接（新标签页）</a></div>
       ${d.artwork_id ? `<div class="hint">识别到 Pixiv 作品 ID：<b>${esc(d.artwork_id)}</b></div>` : ''}
       ${canClaim ? `<div class="hint" style="margin-top:6px">这个本地作品还是占位 ID <b>${esc(aid)}</b>。认领转正会：拉取网页标题/作者/页数，按文件名页码合并为 <b>#${esc(d.artwork_id)}</b> 本体，文件规范化重命名，占位记录删除。</div>` : ''}
       <div class="modal-actions">
         ${canClaim
           ? `<button class="btn accent" onclick="claimPixiv('${esc(aid)}','${esc(d.artwork_id)}')">✅ 认领为 pixiv 作品 #${esc(d.artwork_id)}</button>`
           : `<button class="btn accent" onclick="applySource('${esc(aid)}','${esc(d.site)}','${esc(det.url)}')">✅ 确认归类来源</button>`}
         ${canClaim ? `<button class="btn ghost" onclick="applySource('${esc(aid)}','${esc(d.site)}','${esc(det.url)}')">仅记录来源</button>` : ''}
         <button class="btn ghost" onclick="editSourceUrl('${esc(aid)}','${esc(det.url)}')">✏️ 修改 URL</button>
         <button class="btn ghost" onclick="closeLibModal()">关闭</button>
       </div>`);
    return;
  }
  if (det.has_url) {                // 有 URL 但站点未知 → 可改或反搜
    openLibModal(`🔎 搜索来源 · ${esc(aid)}`,
      `<div class="hint">已记录 URL 但未能识别站点：</div>
       <div class="modal-field"><input type="text" value="${esc(det.url)}" readonly>
         <a class="btn ghost" href="${esc(det.url)}" target="_blank" rel="noopener"
            style="margin-top:6px;display:inline-block">🔗 打开这个链接（新标签页）</a></div>
       <div class="modal-actions">
         <button class="btn ghost" onclick="editSourceUrl('${esc(aid)}','${esc(det.url)}')">✏️ 修改 URL</button>
         <button class="btn accent" onclick="runSourceSearch('${esc(aid)}')">🔎 进反搜</button>
         <button class="btn ghost" onclick="closeLibModal()">关闭</button>
       </div>`);
    return;
  }
  runSourceSearch(aid);             // 无 URL → 直接反搜
}

async function runSourceSearch(aid, forceSauce) {
  // 图库「进反搜」: 进度并入反搜页任务表逐行显示, 完成后在页内渲染结果 (不再弹窗)
  // forceSauce=true: 忽略文件名捷径, 强制上传 SauceNAO 以图搜图核对
  switchTab('reverse');
  $('#rev-gallery-result').innerHTML = '';
  galleryAid = String(aid);
  let r;
  try {
    r = await api('/api/library/source-search', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artwork_id: aid, saucenao: forceSauce ? 'force' : '' }) });
  } catch (e) {
    $('#rev-gallery-result').innerHTML = `<div class="hint err-text">反搜请求失败: ${esc(e.message)}</div>` + urlFormHtml(aid, '');
    galleryAid = null;
    return;
  }
  // 轮询进度 (1s), 进度并入任务表; 完成后渲染结果
  clearInterval(galleryPollTimer);
  let ticks = 0;
  galleryPollTimer = setInterval(async () => {
    ticks++;
    let st = {};
    try { st = (await api('/api/reverse/status')).src_search || {}; } catch (e) { /* 忽略 */ }
    renderReverseTasks(await loadReverseStatus());   // 图库任务行随轮询刷新
    const mine = st.artwork_id === String(aid);
    const finished = mine && !st.running && (st.phase === 'done' || st.phase === 'error');
    if (finished || ticks > 240) {
      clearInterval(galleryPollTimer);
      galleryAid = null;
      await loadReverseStatus();
      if (finished && st.phase === 'done') renderGallerySearchResult(aid, st.result || []);
      else if (st.phase === 'error') $('#rev-gallery-result').innerHTML = `<div class="hint err-text">反搜失败: ${esc(st.error || '未知')}</div>` + urlFormHtml(aid, '');
      else $('#rev-gallery-result').innerHTML = `<div class="hint err-text">反搜超时，请重试</div>` + urlFormHtml(aid, '');
    }
  }, 1000);
}

function renderGallerySearchResult(aid, hits) {
  const rows = hits.length ? hits.map(h => {
    if (h.error) return `<tr><td class="hint">${esc(h.file || '')}</td><td class="err">${esc(h.error)}</td></tr>`;
    if (h.site === 'pixiv' && h.artwork_id) {
      // 身份来源标清楚: 文件名自带ID (待人工确认) / SauceNAO 以图搜图
      const idTag = h.author_src === 'filename'
        ? ' <span class="tag idx" title="作品 ID 提取自文件名（优先级高于 SauceNAO 识图），尚未人工核对">🟡 文件名自带ID·待确认</span>'
        : ' <span class="tag ok" title="SauceNAO 以图搜图命中">✓ 以图搜图命中</span>';
      return `<tr><td class="hint">${esc(h.file)}</td><td>✅ 命中 Pixiv <b>#${esc(h.artwork_id)}</b>${h.page !== undefined ? ' · p' + esc(h.page) : ''}${h.author_name ? ' · ' + esc(h.author_name) : ''}${idTag}</td></tr>`;
    }
    if (h.site && h.url) {
      const nm = h.author_name || h.handle || '';
      // 作者名的来源要说清楚: 网页当前值(权威) / URL 账号 / 第三方索引快照(可能过时)
      const srcTag = h.author_src === 'saucenao_index'
        ? ' <span class="tag idx" title="这是 SauceNAO 结果卡上的名字 —— 它自己的索引快照。作者改过名，这里就一直是旧名；请以源网页为准。">⚠ 索引名·可能过时</span>'
        : (h.author_src === 'page'
          ? ' <span class="tag ok" title="作者名读取自源网页当前内容（与 pixiv 同思路）">✓ 网页当前</span>' : '');
      // 链接必须能点  —— 站点名与 URL 都是链接
      const u = esc(h.url);
      return `<tr><td class="hint">${esc(h.file)}</td><td>🔍 命中 <a href="${u}" target="_blank" rel="noopener"><b>${esc(SITE_BADGE[h.site] || h.site)}</b></a>${nm ? ' · ' + esc(nm) : ''}${srcTag}<div class="hint"><a href="${u}" target="_blank" rel="noopener">${u}</a></div></td></tr>`;
    }
    return `<tr><td class="hint">${esc(h.file)}</td><td class="hint">未命中 / 无有效来源</td></tr>`;
  }).join('') : '<tr><td colspan="2" class="hint">无结果</td></tr>';
  // 首选 pixiv 命中, 其次任意站点
  const firstPixiv = hits.find(h => h && h.site === 'pixiv' && h.artwork_id);
  const firstOther = hits.find(h => h && h.site && h.site !== 'pixiv' && h.url);
  // 本地占位作品 (merged_/ext_) → 可以直接「认领转正」为命中的 pixiv 作品本体
  const canClaim = !!(firstPixiv && !/^\d+$/.test(String(aid))
    && String(aid) !== String(firstPixiv.artwork_id));
  const onlyFilename = !!(firstPixiv && hits.every(h => !h || h.error
    || (h.site === 'pixiv' && h.artwork_id === firstPixiv.artwork_id && h.author_src === 'filename')));
  // 第三方索引名绝不预填进库: SauceNAO 卡片名可能是旧名。
  // 只有「读自源网页」或「URL 里的账号」才自动带作者; 索引名仅提示, 由用户核对后自己填。
  const otherIdx = !!(firstOther && firstOther.author_src === 'saucenao_index');
  const otherName = firstOther ? (otherIdx ? '' : (firstOther.author_name || firstOther.handle || '')) : '';
  const hitNote = firstPixiv
    ? (canClaim
        ? `<div class="hint" style="margin:6px 0">这个本地作品还是占位 ID <b>${esc(aid)}</b>。认领转正会：拉取网页标题/作者/总页数，按文件名页码合并为 <b>#${esc(firstPixiv.artwork_id)}</b> 本体，文件规范重命名，占位记录删除；身份对不上会拒绝，不覆盖已有图。</div>
           <div class="modal-actions">
             <button class="btn accent" onclick="claimPixiv('${esc(aid)}','${esc(firstPixiv.artwork_id)}')">✅ 认领为 pixiv 作品 #${esc(firstPixiv.artwork_id)}（转正合并）</button>
             <button class="btn ghost" onclick="applySourceAny('${esc(aid)}','pixiv','https://www.pixiv.net/artworks/${esc(firstPixiv.artwork_id)}','','${esc(firstPixiv.author_name || '')}','${esc(firstPixiv.author_id || '')}')">仅记录来源</button>
           </div>
           ${onlyFilename ? `<div class="modal-actions"><button class="btn ghost" onclick="runSourceSearch('${esc(aid)}', true)">🔎 仍要用 SauceNAO 以图搜图核对</button></div>` : ''}`
        : `<div class="hint">确认后写入：来源 = pixiv，作品页 = <a href="https://www.pixiv.net/artworks/${esc(firstPixiv.artwork_id)}" target="_blank" rel="noopener"><b>https://www.pixiv.net/artworks/${esc(firstPixiv.artwork_id)}</b></a>${firstPixiv.author_name ? '，作者 = <b>' + esc(firstPixiv.author_name) + '</b>' : ''}</div>
           <div class="modal-actions"><button class="btn accent" onclick="applySourceAny('${esc(aid)}','pixiv','https://www.pixiv.net/artworks/${esc(firstPixiv.artwork_id)}','','${esc(firstPixiv.author_name || '')}','${esc(firstPixiv.author_id || '')}')">✅ 确认为 pixiv 作品</button></div>`)
    : firstOther
      ? `<div class="hint">确认后写入：来源 = <b>${esc(SITE_BADGE[firstOther.site] || firstOther.site)}</b>，链接 = <a href="${esc(firstOther.url)}" target="_blank" rel="noopener"><b>${esc(firstOther.url)}</b></a>${firstOther.handle ? '，作者 = <b>@' + esc(firstOther.handle) + '</b>' : ''}</div>
         ${otherIdx ? `<div class="hint warn-text">⚠ 只从第三方索引拿到旧名 <b>${esc(firstOther.author_name || '')}</b>，<b>不会自动写入作者</b>。请先打开上面的源网页核对当前作者名，确认来源后再用「✏️ 编辑作品」填作者。</div>` : ''}
         <div class="modal-actions"><button class="btn accent" onclick="applySourceAny('${esc(aid)}','${esc(firstOther.site)}','${esc(firstOther.url)}','${esc(firstOther.handle || '')}','${esc(otherName)}','${esc(firstOther.author_id || '')}')">✅ 确认为 ${esc(SITE_BADGE[firstOther.site] || firstOther.site)} 来源</button></div>`
      : `<div class="hint">反搜没有有效命中（pixiv/推特/ArtStation/DeviantArt 等均无）。可手动填写来源 URL（保存后自动识别站点）：</div>`;
  $('#rev-gallery-result').innerHTML =
    `<div class="rev-section-title">📋 图库作品 <b>#${esc(aid)}</b> · 反搜结果</div>
     <div class="tidy-table-wrap"><table class="table"><thead><tr><th>文件</th><th>结果</th></tr></thead><tbody>${rows}</tbody></table></div>
     ${hitNote}${urlFormHtml(aid, '')}`;
}

// ✅ 认领转正: 占位作品 (merged_/ext_) → 真正的 pixiv 作品 (拉网页元数据 + 按文件名页码合并)
async function claimPixiv(aid, pid) {
  let r;
  try {
    r = await api('/api/library/claim-pixiv', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artwork_id: aid, pixiv_id: pid }) });
  } catch (e) { toast('认领失败: ' + e.message, 'err'); return; }
  if (!r || r.status !== 'ok') {
    toast('认领失败: ' + ((r && (r.message || r.error)) || '未知错误'), 'err');
    return;
  }
  toast(`✅ 已转正为 #${pid}《${r.title || ''}》· ${r.pages.length} 页` +
    (r.files_moved ? ` · 重命名 ${r.files_moved} 个文件` : ''), 'ok');
  closeLibModal();
  galleryAid = null;
  loadLibrary();
}

// 🪄 认领散落页 (并入「🧹 一键整理」预览): 离线扫描结果 → 弹窗内分区 HTML。
// 勾选后批量转正, 防再次出现「被拆/错并后合不回来」。
let claimScanData = null;
function claimSectionHtml(r) {
  claimScanData = r || { ready: [], mixed: [], no_id: 0 };
  const ready = r.ready || [], mixed = r.mixed || [];
  if (!ready.length && !mixed.length) return '';   // 干干净净就不占版面
  const rows = ready.map((e, i) => {
    const pages = (e.pages || []).map(p => 'p' + p.declared_page).join(', ');
    return `<label class="claim-row" title="${esc(e.old_id)}">
      <input type="checkbox" class="claim-pick" data-idx="${i}" checked>
      <span class="claim-main">${esc(e.title || e.old_id)}
        <span class="hint">（${esc(e.old_id)}，${e.page_count} 页：${esc(pages)}）</span>
        → <b>pixiv #${esc(e.pixiv_id)}</b></span>
    </label>`;
  }).join('');
  const mixedRows = mixed.map(e => {
    const detail = (e.pages || []).filter(p => p.pid)
      .map(p => `p${p.page_num}→#${p.pid}`).join('，');
    return `<div class="claim-row claim-mixed">⚠ ${esc(e.title || e.old_id)}
      <span class="hint">（${esc(e.old_id)}）的图片分属 ${e.pids.length} 个不同作品：${esc(detail)}
      —— 不能自动认领，请先在图库把它按张拆出，再整理认领。</span></div>`;
  }).join('');
  return `<div class="tidy-claim-section">
    <div class="doc-title">🪄 认领散落的 pixiv 页（被拆/错并到占位作品里的原图）</div>
    <div class="hint" style="margin:4px 0 8px">
      扫描依据：图片文件名自带的 pixiv 作品 ID（如 <code>88007979_p0.png</code>）。
      认领时当场拉取网页标题/作者/总页数，文件规范重命名并合并回正式作品；身份对不上或目标位已被别的图占用会跳过，不覆盖。
      此操作需联网，与下方「执行整理」（纯本地文件归位）互相独立、分别执行。
    </div>
    ${ready.length
      ? `<div style="margin-bottom:6px"><b>✅ 可直接认领转正：${ready.length} 个占位作品</b></div>
         <div class="claim-list">${rows}</div>
         <div class="modal-actions"><button class="btn accent" onclick="startClaimBatch()">🪄 认领勾选的 ${ready.length} 个作品</button></div>`
      : ''}
    ${mixed.length ? `<div style="margin:10px 0 6px"><b class="warn-text">⚠ ${mixed.length} 个作品的图片身份混杂，需先人工拆分：</b></div><div class="claim-list">${mixedRows}</div>` : ''}
    ${r.no_id ? `<div class="hint" style="margin-top:8px">另有 ${r.no_id} 个非 pixiv 命名的本地作品（外来源/手工命名），已跳过。</div>` : ''}
    <hr class="tidy-sep">
  </div>`;
}

// 认领完成 → 刷新图库后重开「一键整理」预览 (已认领的项会从分区消失)
async function finishClaimBatch() {
  loadLibrary(); loadStats(); loadLibFacets();
  await openTidyPreview();
}

async function startClaimBatch() {
  const r0 = claimScanData || { ready: [] };
  const picked = [...document.querySelectorAll('.claim-pick')]
    .filter(cb => cb.checked)
    .map(cb => {
      const e = r0.ready[Number(cb.dataset.idx)];
      return { artwork_id: e.old_id, pixiv_id: e.pixiv_id };
    });
  if (!picked.length) { toast('请勾选至少一个作品', 'err'); return; }
  let r;
  try {
    r = await api('/api/library/claim-batch', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: picked }) });
  } catch (e) { toast('启动批量认领失败: ' + e.message, 'err'); return; }
  if (!r || r.status !== 'started') {
    toast((r && r.message) || '启动失败', 'err');
    return;
  }
  pollClaimBatch();
}

let claimPollTimer = null;
async function pollClaimBatch() {
  clearInterval(claimPollTimer);
  const render = (st) => {
    const pct = st.total ? Math.round(st.done / st.total * 100) : 0;
    const lines = (st.results || []).map(x => x.ok
      ? `<div>✅ ${esc(x.title || x.pixiv_id)}（#${esc(x.pixiv_id)}，${x.pages} 页${x.files_moved ? '，重命名 ' + x.files_moved : ''}）</div>`
      : `<div class="err-text">❌ ${esc(x.artwork_id)} → #${esc(x.pixiv_id)}：${esc(x.error)}</div>`
    ).join('');
    openLibModal('🪄 批量认领进行中',
      `<div class="hint">${st.done}/${st.total}（${pct}%）· 当前：${esc(st.current || '—')}</div>
       <div class="progress-bar" style="margin:8px 0"><div style="width:${pct}%"></div></div>
       <div class="claim-list">${lines}</div>
       ${st.error ? `<div class="hint err-text" style="margin-top:8px">${esc(st.error)}</div>` : ''}
       ${(!st.running && st.finished_at)
         ? `<div class="modal-actions">
              <button class="btn accent" onclick="finishClaimBatch()">✅ 完成（回到整理预览）</button>
            </div>
            <div class="hint">成功 ${st.ok} 个${st.failed ? `，失败 ${st.failed} 个（见上）` : ''}</div>`
         : ''}`);
  };
  let ticks = 0;
  claimPollTimer = setInterval(async () => {
    ticks++;
    let st = {};
    try { st = await api('/api/library/claim-batch/status'); } catch (e) { return; }
    render(st);
    if (!st.running && st.finished_at) {
      clearInterval(claimPollTimer);
      loadLibrary(); loadStats(); loadLibFacets();
      if (!st.failed) toast(`✅ 批量认领完成：${st.ok} 个作品已转正`, 'ok');
      else toast(`批量认领完成：成功 ${st.ok}，失败 ${st.failed}（详见弹窗）`, 'err');
    }
    if (ticks > 600) clearInterval(claimPollTimer);
  }, 1000);
  render({ total: 0, done: 0, current: '启动中…', results: [], running: true });
}

function urlFormHtml(aid, cur) {
  return `<div class="modal-field"><label>来源 URL</label>
    <input id="lib-src-url" type="url" value="${esc(cur)}" placeholder="https://…（pixiv / twitter / bilibili / ggac / danbooru 等）"></div>
    <div class="modal-actions"><button class="btn accent" onclick="saveSourceUrl('${esc(aid)}')">💾 保存 URL</button></div>`;
}

async function saveSourceUrl(aid) {
  const url = ($('#lib-src-url').value || '').trim();
  try {
    const r = await api('/api/library/source-set', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ artwork_id: aid, source_url: url }) });
    closeLibModal();
    if (r && r.reidentified && r.reidentified.length) {
      // 身份已纠正为 ext_ (merged_ 主键作废) → 必须刷新后再操作, 不能拿旧 ID 继续请求
      const nid = r.reidentified[0].new;
      toast('URL 已保存' + reidentifyNote(r), 'ok');
      await loadLibrary();
      sourceSearch(nid);
      return;
    }
    toast('URL 已保存，识别来源中…', 'ok');
    sourceSearch(aid);   // 保存后重新 detect 归类
  } catch (e) { toast('保存 URL 失败: ' + e.message, 'err'); }
}

async function applySource(aid, source, url) {
  try {
    const r = await api('/api/library/source-set', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ artwork_id: aid, source_url: url, source }) });
    toast('已归类来源：' + source + reidentifyNote(r), 'ok');
    closeLibModal(); loadLibrary();
  } catch (e) { toast('归类失败: ' + e.message, 'err'); }
}

// 作品身份被纠正 (merged_ → ext_) 时给一句说明, 否则用户会奇怪"作品怎么换了个 ID"
function reidentifyNote(r) {
  const rk = (r && r.reidentified) || [];
  return rk.length ? `　已按真实来源纠正身份：${rk[0].old} → ${rk[0].new}` : '';
}

// 通用来源确认: source=site + 来源 URL + 作者 handle (source_author) + 作者名/作者ID (建库关联)
async function applySourceAny(aid, site, url, handle, authorName, authorId) {
  try {
    const body = { artwork_id: aid, source_url: url, source: site };
    if (handle) body.source_author = handle;
    if (authorName) body.author_name = authorName;
    if (authorId) body.author_id = authorId;
    const r = await api('/api/library/source-set', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const e2 = apiErr(r);
    if (e2) { toast('归类失败: ' + e2, 'err'); return; }
    toast('已归类来源：' + (SITE_BADGE[site] || site) + (handle ? ' @' + handle : '') + (authorName ? ' · 作者 ' + authorName : '') + reidentifyNote(r), 'ok');
    closeLibModal(); loadLibrary();
  } catch (e) { toast('归类失败: ' + e.message, 'err'); }
}

async function editSourceUrl(aid, cur) {
  openLibModal(`✏️ 修改来源 URL · ${esc(aid)}`, `<div class="hint">修改来源 URL（保存后自动重新识别站点）：</div>` + urlFormHtml(aid, cur));
}

window.saveMergeImages = async function () {
  const items = [...libSelected].map(parsePageKey);
  const targetType = ($$('#lib-modal input[name="merge-target"]').find(r => r.checked) || {}).value || 'existing';
  const body = { items, target: targetType };
  if (targetType === 'existing') {
    const tid = $('#merge-existing').value;
    if (!tid) { toast('请选择要合并到的作品', 'err'); return; }
    body.target_id = tid;
  } else {
    body.title = ($('#merge-new-title').value || '').trim();
  }
  try {
    const r = await api('/api/library/merge-pages', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(`已合并 ${r.pages_moved} 张图片到作品 ${r.target_id}` + (r.artworks_removed.length ? `（${r.artworks_removed.length} 个源作品已搬空移除）` : '') + `，移动 ${r.files_moved} 个文件 ✓`, 'ok');
    libSelected.clear();
    _libAnchor = null;
    closeLibModal();
    loadLibrary(); loadStats(); loadLibFacets();
  } catch (e) { toast('合并失败: ' + e.message, 'err'); }
};

// ✂️ 拆分: 把选中的图片从所属作品里拆出来。两种形态 ——
//   「每张独立成作品」(split-pages) / 「组成一个新作品」(merge-pages 的新建模式)
// 文件不移动, 只改数据库归属; 源作品被拆空则自动删除。
async function openSplitImages() {
  if (libDataView !== 'image' || !libSelected.size) return;
  const keys = [...libSelected];
  const items = keys.map(parsePageKey);
  // 与「合并」完全相同的硬闸门: pixiv 作品(数字ID)的原网页页 (origin !== 'merged')
  // 不能拆 —— 后端 split_pages/merge_pages 都会拦, 这里提前硬阻断, 不让用户白填弹窗。
  // 2026-09-16 : 旧版只在弹窗里写了行警告, 点了才报错, 应像合并一样直接拦下。
  const loaded = ($('#lib-result')._lastItems || []);
  const byKey = {};
  loaded.forEach(w => { byKey[w._key] = w; });
  const blocked = keys.filter(k => {
    const w = byKey[k] || {};
    if (!/^\d+$/.test(String(w.artwork_id || ''))) return false;
    return (w.origin || 'web') !== 'merged';
  });
  if (blocked.length) {
    const names = blocked.slice(0, 3)
      .map(k => { const w = byKey[k] || {}; return `${w.artwork_id}_p${w.page_num}`; })
      .join('、');
    openLibModal('✂️ 无法拆分', `
      <div class="hint err-text" style="margin-bottom:10px">
        选中的图片里有 <b>${blocked.length} 张是 pixiv 作品的原网页页</b>（${esc(names)}${blocked.length > 3 ? '…' : ''}）。
      </div>
      <div class="hint">pixiv 作品的原网页页不能拆：
        <br>· 原作品会因此缺页（原本完整的页数被拆散）；
        <br>· 拆走后这些图片难以再自动归属回原 pixiv 作品（可用「🪄 认领散落页」找回，但不该先制造混乱）。
        <br><br>能拆的只有<b>合并进来的附加页</b>（页码带 * 标记），那属于「反悔合并」。
        <br>其他来源 / 未知来源的图片不受限，可自由拆分。</div>
      <div class="modal-actions"><button class="btn ghost" onclick="closeLibModal()">知道了</button></div>`);
    return;
  }
  const nArt = new Set(items.map(it => it.artwork_id)).size;
  openLibModal(`✂️ 拆分 ${items.length} 张图片`, `
    <div class="hint" style="margin-bottom:10px">
      把选中的 ${items.length} 张图片从所属的 ${nArt} 个作品中拆出，<b>文件不会移动</b>，只改数据库归属。
      ${nArt > 1 ? '<br>⚠ 选中的图片来自多个作品，拆出后各作品分别减少对应页数。' : ''}
      <br>⚠ 合并进来的图片其原归属在合并时就已丢弃，<b>无法还原回合并前的作品</b>，拆出只能生成新作品。
      <br>⚠ pixiv 作品的<b>原网页页不能拆</b>（会让原作品缺页）；只有合并进来的附加页可以拆。
    </div>
    <div class="be-row"><label><input type="radio" name="split-mode" value="each" checked> 每张独立成作品</label>
      <span class="hint">新建 ${items.length} 个单页作品</span></div>
    <div class="be-row"><label><input type="radio" name="split-mode" value="group"${items.length < 2 ? ' disabled' : ''}> 组成一个新作品</label>
      <input type="text" class="be-ctl" id="split-new-title" placeholder="新作品标题（可选）"${items.length < 2 ? ' disabled' : ''}></div>
    <div class="modal-actions">
      <button class="btn accent" onclick="saveSplitImages()">拆分</button>
      <button class="btn ghost" onclick="closeLibModal()">取消</button>
    </div>`);
}

window.saveSplitImages = async function () {
  const items = [...libSelected].map(parsePageKey);
  if (!items.length) return;
  const mode = ($$('#lib-modal input[name="split-mode"]').find(r => r.checked) || {}).value || 'each';
  try {
    let r, msg;
    if (mode === 'group') {
      r = await api('/api/library/merge-pages', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items, target: 'new',
                               title: ($('#split-new-title').value || '').trim() }) });
      msg = `已拆出 ${r.pages_moved} 张，组成新作品 ${r.target_id}`;
    } else {
      r = await api('/api/library/split-pages', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items }) });
      msg = `已拆出 ${r.pages_moved} 张，每张独立成作品`;
    }
    if (r.artworks_removed && r.artworks_removed.length)
      msg += `（${r.artworks_removed.length} 个原作品已搬空并删除）`;
    toast(msg + ' ✓', 'ok');
    libSelected.clear();
    _libAnchor = null;
    closeLibModal();
    loadLibrary(); loadStats(); loadLibFacets();
  } catch (e) { toast('拆分失败: ' + e.message, 'err'); }
};

// ✏️ 批量编辑: 弹窗中每个字段带「修改」勾选, 只提交勾选的字段
async function openBatchEdit() {
  if (!libSelected.size) return;
  if (!facetData.authors.length) { try { await loadLibFacets(); } catch (e) { /* ignore */ } }
  // 图片视图选中粒度是「张」, 但作者/类型/R18/来源/标签都是作品级属性 → 归并到所属作品处理
  const isImg = libDataView === 'image';
  const artIds = isImg ? [...new Set([...libSelected].map(k => parsePageKey(k).artwork_id))] : [...libSelected];
  const nSel = libSelected.size, nArt = artIds.length;
  openLibModal(`✏️ 批量编辑 · ${isImg ? `${nSel} 张图片（${nArt} 个作品）` : `${nSel} 个作品`}`, `
    ${isImg ? `<div class="hint" style="margin-bottom:8px">⚠ 作者/类型/R18/来源/标签均为作品级属性，将应用到所选图片所属的 <b>${nArt}</b> 个作品（同一作品的其他页也会一并生效）。</div>` : ''}
    <div class="hint" style="margin-bottom:8px">勾选要修改的字段并填写内容；未勾选的字段保持不变。</div>
    <div class="be-row"><label><input type="checkbox" class="be-chk" data-k="author"> 修改作者</label>
      <div class="combo-wrap"><input type="text" class="be-ctl" id="be-author" disabled
        placeholder="作者名（输入有提示：主名/曾用名/ID）；或「名字_ID」/纯ID"><div class="combo-list" id="be-author-combo"></div></div></div>
    <div class="be-row"><label><input type="checkbox" class="be-chk" data-k="type"> 修改类型</label>
      <select class="be-ctl" id="be-type" disabled><option value="0">插画</option><option value="1">漫画</option><option value="2">动图</option></select></div>
    <div class="be-row"><label><input type="checkbox" class="be-chk" data-k="r18"> 修改 R18</label>
      <select class="be-ctl" id="be-r18" disabled><option value="0">否</option><option value="1">是</option></select></div>
    <div class="be-row"><label><input type="checkbox" class="be-chk" data-k="source"> 修改来源</label>
      <select class="be-ctl" id="be-source" disabled>${sourceOptions('')}</select></div>
    <div class="be-row be-col"><label><input type="checkbox" class="be-chk" data-k="tags"> 修改标签</label>
      <input type="text" class="be-ctl" id="be-tags" disabled placeholder="多个标签用逗号/空格分隔，如：壁纸, waifu2x放大">
      <div class="be-tagmode">
        <label><input type="radio" name="be-tagmode" value="replace" class="be-ctl" disabled checked> 替换为这些标签</label>
        <label><input type="radio" name="be-tagmode" value="append" class="be-ctl" disabled> 追加（保留原标签）</label>
        <label><input type="radio" name="be-tagmode" value="clear" class="be-ctl" disabled> 清空全部标签</label>
      </div></div>
    <div class="modal-actions">
      <button class="btn accent" onclick="saveBatchEdit()">💾 应用到 ${nArt} 个作品</button>
      <button class="btn ghost" onclick="closeLibModal()">取消</button>
    </div>`);
  $$('#lib-modal .be-chk').forEach(chk => chk.addEventListener('change', () => {
    const k = chk.dataset.k;
    if (k === 'tags') {
      $$('#lib-modal input[name="be-tagmode"], #lib-modal #be-tags').forEach(el => { el.disabled = !chk.checked; });
    } else {
      $(`#be-${k}`).disabled = !chk.checked;
    }
  }));
  // 批量编辑的来源下拉: 支持「＋ 自定义来源…」/「管理来源…」（不受上面 disabled 勾选控制的 select 也能触发）
  $('#be-source').dataset.prev = '';
  $('#be-source').addEventListener('change', e => onSourceCustom(e.target));
  makeEditAuthorCombo($('#be-author'), $('#be-author-combo'));
}

window.saveBatchEdit = async function () {
  // 图片视图: 选中的是「张」, 归并为所属作品 ID (去重) 后再提交作品级修改
  const ids = libDataView === 'image'
    ? [...new Set([...libSelected].map(k => parsePageKey(k).artwork_id))]
    : [...libSelected];
  const body = { ids };
  const on = k => $(`#lib-modal .be-chk[data-k="${k}"]`).checked;
  if (on('author')) Object.assign(body, resolveAuthorInput($('#be-author').value));
  if (on('type')) body.illust_type = parseInt($('#be-type').value, 10);
  if (on('r18')) body.is_r18 = parseInt($('#be-r18').value, 10);
  if (on('source')) body.source = $('#be-source').value;
  if (on('tags')) {
    body.tag_mode = ($$('#lib-modal input[name="be-tagmode"]').find(r => r.checked) || {}).value || 'replace';
    body.tags = $('#be-tags').value.split(/[,，、;；\s]+/).map(s => s.trim()).filter(Boolean);
  }
  if (Object.keys(body).length <= 1) { toast('请至少勾选一个要修改的字段', 'err'); return; }
  try {
    const r = await api('/api/library/batch-update', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(`已更新 ${r.updated} 个作品 ✓${r.name ? '（作者：' + r.name + '）' : ''}`, 'ok');
    libSelected.clear();    // 批量操作完成后清空选择 (避免残留勾选误导后续操作)
    closeLibModal(); loadLibrary(); loadStats(); loadLibFacets();
  } catch (e) { toast('批量编辑失败: ' + e.message, 'err'); }
};

// ---- 弹窗: 编辑作品 / 编辑标签 ----
function openLibModal(title, bodyHtml, opts) {
  // 大号弹窗 (opts.lg): 一键整理预览/来源管理等表格类内容用, 更宽更高好读
  const box = $('#lib-modal .modal-box');
  if (box) box.classList.toggle('modal-lg', !!(opts && opts.lg));
  $('#lib-modal-title').textContent = title;
  $('#lib-modal-body').innerHTML = bodyHtml;
  $('#lib-modal').style.display = 'flex';
}
function closeLibModal() { $('#lib-modal').style.display = 'none'; }

// ============ 💾 备份中心 (2026-09-16 合并原「一键备份」+「备份管理」) ============
//   需求: 只留一个入口, 「一键备份」搬进里面 —— 上半是数据库备份(zip), 下半是
//   「♻ 重下修复」覆盖前自动留下的 .bak 旧文件(对比/恢复/删除)。这两件事本来就是
//   一体两面: 一个备份"库", 一个备份"图", 出问题时都在这里找回来。
async function openBackupCenter() {
  let dbk = { items: [], dir: '' }, bk = { items: [] };
  try {
    const [a, b] = await Promise.all([
      api('/api/library/db-backups').catch(() => ({ items: [], dir: '' })),
      api('/api/library/backups').catch(() => ({ items: [] })),
    ]);
    dbk = a || dbk; bk = b || bk;
  } catch (e) { /* 单项失败不影响另一块 */ }
  const dbItems = dbk.items || [];
  const dbRows = dbItems.map(it => `<tr>
      <td class="mono" title="${esc(it.name)}">${esc(it.name)}</td>
      <td style="white-space:nowrap">${fmtSize(it.size)}</td>
      <td style="white-space:nowrap">${it.mtime ? new Date(it.mtime * 1000).toLocaleString('zh-CN') : ''}</td>
      <td style="white-space:nowrap"><button class="btn danger" onclick="deleteDbBackup('${esc(it.name)}')">🗑️ 删除</button></td>
    </tr>`).join('');
  const bakItems = bk.items || [];
  const bakRows = bakItems.map(it => `
      <tr>
        <td style="white-space:nowrap">${esc(it.artwork_id || '?')}${it.page_num != null ? '_p' + it.page_num : ''}</td>
        <td class="w-path" title="${esc(it.orig_rel)}">${esc(it.orig_name)}</td>
        <td style="white-space:nowrap">${fmtSize(it.size)}</td>
        <td style="white-space:nowrap">${it.mtime ? new Date(it.mtime * 1000).toLocaleString('zh-CN') : ''}</td>
        <td style="white-space:nowrap">
          <button class="btn ghost" onclick="compareBackup(${JSON.stringify(it).replace(/"/g, '&quot;')})">🔀 对比</button>
          <button class="btn ghost" onclick="restoreBackup('${esc(it.rel)}')">↩ 恢复</button>
          <button class="btn danger" onclick="deleteBackup('${esc(it.rel)}')">🗑️ 删除</button>
        </td>
      </tr>`).join('');
  openLibModal('💾 备份中心', `
    <h4>🗄 数据库备份（pixiv.db + 反搜特征库）</h4>
    <div class="hint">打包成 zip 存到 <code>${esc(dbk.dir || 'data/backup/')}</code>，可自行拷到别处长期保存（换机/重装后放回来即可）。</div>
    <div class="btn-row" style="margin:8px 0">
      <button class="btn accent" onclick="doBackup()">💾 立即备份数据库</button>
    </div>
    ${dbItems.length
      ? `<table class="table"><thead><tr><th>备份文件</th><th>大小</th><th>备份时间</th><th>操作</th></tr></thead><tbody>${dbRows}</tbody></table>`
      : '<div class="hint">还没有数据库备份。点上面「立即备份数据库」生成第一个。</div>'}
    <h4 style="margin-top:18px">🕒 重下修复留下的旧文件（.bak）</h4>
    <div class="hint">「♻ 重下修复」用官方源覆盖本地前，旧文件会自动存为 <code>.bak</code>；
      这里可以逐条「🔀 对比」新旧，再决定恢复或删除。</div>
    ${bakItems.length
      ? `<table class="table"><thead><tr><th>作品/页</th><th>原文件</th><th>大小</th><th>备份时间</th><th>操作</th></tr></thead><tbody>${bakRows}</tbody></table>`
      : '<div class="hint">暂无 .bak 旧文件。</div>'}
    <div class="btn-row"><button class="btn ghost" onclick="closeLibModal()">关闭</button></div>`, { lg: true });
}

async function deleteDbBackup(name) {
  if (!confirm('删除这个数据库备份？只删这份 zip，不影响当前数据库。')) return;
  try {
    const r = await api('/api/library/db-backup/delete', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    if (r.status !== 'ok') throw new Error(r.error || '失败');
    toast('备份已删除', 'ok');
    openBackupCenter();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}
window.deleteDbBackup = deleteDbBackup;

// 对比视图: 左=旧备份(.bak), 右=当前文件; 看完可恢复或删除
// allow-raw-img: 这一处**刻意**不走 imgSrc —— 它是用户针对"自己刚备份的那一个文件"
// 主动发起的对比(等价于 Lightbox 里点「显示」), 不是列表批量铺图, 所以不打码。
// 冒烟 T101 扫描"裸本地图片地址的图片标签", 这段标记后面若干行是唯一豁免区。
function compareBackup(it) {
  const oldUrl = '/api/library/backup-img?rel=' + encodeURIComponent(it.rel);
  const newUrl = '/api/local-img?p=' + encodeURIComponent(it.orig_rel) + (it.orig_rel.toLowerCase().endsWith('.gif') ? '' : '');
  openLibModal('🔀 对比备份 · ' + esc(it.orig_name),
    `<div class="bk-compare">
       <div class="bk-side">
         <div class="bk-label">🕒 旧备份（强制重下前）</div>
         <img class="bk-img" src="${oldUrl}" alt="旧备份" onerror="this.parentNode.innerHTML+='<div class=hint>旧备份不可读</div>'">
       </div>
       <div class="bk-side">
         <div class="bk-label">📄 当前文件</div>
         <img class="bk-img" src="${newUrl}" alt="当前" onerror="this.parentNode.innerHTML+='<div class=hint>当前文件缺失</div>'">
       </div>
     </div>
     <div class="btn-row">
       <button class="btn ghost" onclick="openBackupCenter()">‹ 返回备份中心</button>
       <button class="btn accent" onclick="restoreBackup('${esc(it.rel)}')">↩ 恢复此备份</button>
       <button class="btn danger" onclick="deleteBackup('${esc(it.rel)}')">🗑️ 删除此备份</button>
       <button class="btn ghost" onclick="closeLibModal()">关闭</button>
     </div>`);
}

async function restoreBackup(rel) {
  if (!confirm('恢复备份：将用备份文件覆盖当前文件（当前新版将被替换为备份内容）。确认恢复？')) return;
  try {
    const r = await api('/api/library/backup/restore', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rel }) });
    if (r.status !== 'ok') throw new Error(r.error || '失败');
    toast('已恢复备份', 'ok');
    openBackupCenter();
  } catch (e) { toast('恢复失败: ' + e.message, 'err'); }
}

async function deleteBackup(rel) {
  if (!confirm('删除备份：备份文件将被删除（保留当前文件）。确认删除？')) return;
  try {
    const r = await api('/api/library/backup/delete', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rel }) });
    if (r.status !== 'ok') throw new Error(r.error || '失败');
    toast('备份已删除', 'ok');
    openBackupCenter();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

// 作者输入解析: 「名字_ID」→ pixiv 作者; 纯数字 → pixiv ID; 已知名 → 该作者; 其他 → 本地作者
function resolveAuthorInput(text) {
  const raw = String(text || '').trim();
  if (!raw) return {};
  const m = raw.match(/^(.+?)_(\d+)$/);
  if (m) return { author_name: m[1].trim(), author_id: m[2] };
  if (/^\d+$/.test(raw)) return { author_id: raw };
  const hit = facetData.authors.find(a => a.name.toLowerCase() === raw.toLowerCase());
  if (hit) return { author_id: hit.author_id };
  return { author_name: raw };
}

async function openLibEdit(aid) {
  let w;
  const items = $('#lib-result')._lastItems || [];
  w = items.find(x => String(x.artwork_id) === String(aid));
  if (!w) {
    try { const r = await api('/api/library/search?q=' + encodeURIComponent(aid)); w = (r.items || []).find(x => String(x.artwork_id) === String(aid)); } catch (e) { /* ignore */ }
  }
  if (!facetData.authors.length) { try { await loadLibFacets(); } catch (e) { /* ignore */ } }
  const title = w ? esc(w.title || '') : '';
  const authorName = w ? esc(w.author_name || '') : '';
  const type = w ? (w.illust_type ?? 0) : 0;
  const r18 = w ? (w.is_r18 ? 1 : 0) : 0;
  const source = (w && w.source) || 'pixiv';
  const tags = w && w.tags ? w.tags.map(t => esc(t)) : [];
  openLibModal(`✏️ 编辑作品 ${esc(aid)}`, `
    <div class="modal-field"><label>标题</label><input id="lib-e-title" type="text" value="${title}" placeholder="作品标题"></div>
    <div class="modal-field"><label>作者</label><div class="combo-wrap">
      <input id="lib-e-author" type="text" value="${authorName}" autocomplete="off"
        placeholder="已知作者填「作者名_作者ID」或纯数字ID；其他来源直接填名字；留空=不修改">
      <div class="combo-list" id="lib-e-author-combo"></div></div>
      <div class="btn-row" style="margin-top:6px">
        <button class="btn ghost" id="lib-e-author-lookup"
          onclick="pixivLookupAuthor('${esc(aid)}')">🔍 联网查 pixiv 作者 ID</button>
      </div>
      <div class="hint">库里已有的作者 → 输入关键词选提示即可；<b>pixiv 作者</b> → 用「作者名_作者ID」或纯数字 ID（不确定 ID 就点上面 🔍 联网查）；
        <b>其他来源</b>（X/推特/未知画师）→ 只填名字，会建一个「自定义作者」（不参与 pixiv 联网核对）。</div>
    </div>
    <div class="modal-field"><label>类型</label>
      <select id="lib-e-type"><option value="0" ${type===0?'selected':''}>插画</option><option value="1" ${type===1?'selected':''}>漫画</option><option value="2" ${type===2?'selected':''}>动图</option></select></div>
    <div class="modal-field"><label>R18</label>
      <select id="lib-e-r18"><option value="0" ${r18===0?'selected':''}>否</option><option value="1" ${r18===1?'selected':''}>是</option></select></div>
    <div class="modal-field"><label>来源</label>
      <select id="lib-e-source">${sourceOptions(source)}</select></div>
    <div class="modal-field"><label>来源 URL</label>
      <input id="lib-e-url" type="url" value="${w ? esc(w.source_url || '') : ''}"
        placeholder="来源页链接（pixiv / twitter / bilibili / ggac / danbooru…），留空清除"></div>
    <div class="modal-field"><label>标签</label>
      <div class="tag-editor" id="lib-tag-editor">
        ${tags.length ? tags.map(t => `<span class="tag big">${t} <button class="tag-x" onclick="libRemoveTag(this)">✕</button></span>`).join('') : '<div class="hint" id="lib-tags-empty">暂无标签，在下方添加。</div>'}
      </div>
      <div class="combo-wrap"><input id="lib-tag-input" type="text" placeholder="输入标签关键词, 回车添加, 点选提示" autocomplete="off">
        <div class="combo-list" id="lib-tag-combo"></div></div></div>
    <div class="hint" style="margin-top:4px">
      ${w && w.deleted
        ? `<span class="tag deleted">🗑️ 原作品已失效</span> 作品页在 pixiv 已确认不存在（联网补全时 404 自动记录）。
           <button class="btn ghost" style="padding:2px 8px;font-size:11px;margin-left:6px"
                   onclick="setDeletedFlag('${esc(aid)}', false)">♻ 撤销失效标记</button>`
        : (aid && /^\d+$/.test(aid)
            ? `<span class="hint">作品页失效（pixiv 404）会在联网补全时自动记录；若确认页已不存在，也可
                 <button class="btn ghost" style="padding:2px 8px;font-size:11px"
                         onclick="setDeletedFlag('${esc(aid)}', true)">🗑️ 手动标记为已失效</button></span>`
            : '')}
    </div>
    ${/^\d+$/.test(String(aid)) ? `<div class="btn-row" style="margin-top:8px">
      <button class="btn ghost" onclick="repairOneWork('${esc(aid)}')"
        title="用 pixiv 官方源【覆盖】重下这个作品：文件损坏/丢失、或想要官方最新版时用。旧文件会自动备份为 .bak，可在「💾 备份」里对比恢复">♻ 重下修复这个作品（覆盖本地）</button>
    </div>` : ''}
    <div class="modal-actions">
      <button class="btn accent" onclick="saveLibEdit('${esc(aid)}')">💾 保存</button>
      <button class="btn ghost" onclick="closeLibModal()">取消</button>
    </div>`);
  $('#lib-e-source').dataset.prev = source;
  $('#lib-e-source').addEventListener('change', e => onSourceCustom(e.target));
  makeEditAuthorCombo($('#lib-e-author'), $('#lib-e-author-combo'));
  makeEditTagSuggest($('#lib-tag-input'), $('#lib-tag-combo'));
}

// 「作品页已失效」标记的人工纠正 (客观事实自动来自 404; 这里给一条纠错路径)
window.setDeletedFlag = async function (aid, deleted) {
  try {
    const r = await api('/api/library/deleted-flag', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artwork_id: aid, deleted: !!deleted }) });
    toast(r.deleted ? '已标记为「原作品已失效」✓' : '已撤销失效标记，恢复参与联网补全 ✓', 'ok');
    loadLibrary(); loadStats(); loadLibFacets();
    closeLibModal();
  } catch (e) { toast('操作失败: ' + e.message, 'err'); }
};

// 作者只填了一个「库里没有的名字」时, 先问清楚这位是不是 pixiv 作者 (用户 2026-09-16 反馈):
// 以前会默默建成「自定义作者」, 明明知道他是 p 站作者却没法声明。
function askIsPixivAuthor(aid, body, name) {
  window._pendingAuthorSave = { aid, body, name };
  openSrcModal('这位是 pixiv 作者吗？', `
    <div class="hint">库里还没有作者「<b>${esc(name)}</b>」。请选一种，别让它默默变成自定义作者：<br>
      · <b>是 pixiv 作者</b> → 联网查出作者 ID（作品会正确挂到 pixiv 作者名下，参与改名/存续同步）；<br>
      · <b>是其他来源</b>（X/推特/未知画师）→ 建一个自定义作者（不参与 pixiv 联网核对）。</div>
    <div class="btn-row">
      <button class="btn accent" onclick="pendingAuthorLookup()">🔍 是 pixiv 作者，联网查 ID</button>
      <button class="btn ghost" onclick="pendingAuthorSaveLocal()">建为自定义作者</button>
      <button class="btn ghost" onclick="closeSrcModal()">取消</button>
    </div>`);
}

window.pendingAuthorLookup = function () {
  const p = window._pendingAuthorSave;
  if (!p) return;
  closeSrcModal();
  pixivLookupAuthor(p.aid, p.body);
};
window.pendingAuthorSaveLocal = function () {
  const p = window._pendingAuthorSave;
  if (!p) return;
  closeSrcModal();
  doSaveLibEdit(p.aid, p.body);
};

// 🔍 联网查 pixiv 作者: 作品是纯数字 pixiv ID → 直接问作品页(HTTP 直连, 最准);
// 否则用输入框/待保存里的名字去搜 pixiv 作者, 命中即填成「作者名_作者ID」。
window.pixivLookupAuthor = async function (aid, saveBody) {
  const btn = $('#lib-e-author-lookup');
  const setBusy = (b) => {
    if (btn) { btn.disabled = b; btn.textContent = b ? '🔍 查询中…' : '🔍 联网查 pixiv 作者 ID'; }
  };
  const typed = ($('#lib-e-author') && $('#lib-e-author').value || '').trim();
  const post = (payload) => api('/api/library/pixiv-author', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload) });
  setBusy(true);
  try {
    let cands = [];
    if (/^\d+$/.test(String(aid || ''))) {
      const r1 = await post({ artwork_id: String(aid) });
      const e1 = apiErr(r1); if (e1) throw new Error(e1);
      cands = r1.candidates || [];
    }
    if (!cands.length) {
      const nm = (typed.replace(/_\d+$/, '') || (window._pendingAuthorSave || {}).name || '').trim();
      if (!nm || /^\d+$/.test(nm)) {
        toast('请先在「作者」里填一个名字，再点联网查 ID', 'err');
        return;
      }
      const r2 = await post({ name: nm });
      const e2 = apiErr(r2); if (e2) throw new Error(e2);
      cands = r2.candidates || [];
    }
    if (!cands.length) {
      toast('pixiv 上没搜到这个作者（名字可能不同；也可手动填「作者名_作者ID」）', 'err');
      return;
    }
    if (cands.length === 1) { applyAuthorCandidate(cands[0], aid, saveBody); return; }
    window._pixivCands = cands;
    window._pixivCandsSave = { aid, saveBody };
    openSrcModal('🔍 选择 pixiv 作者', `
      <div class="hint">找到 ${cands.length} 个候选，点「选这个」填入（会填成「作者名_作者ID」）：</div>
      <ul class="health-list">` + cands.map((c, i) => `<li>${esc(c.name || '(无名)')}
        <code>${esc(c.author_id)}</code><span class="sus-acts">
        <a class="btn ghost" href="https://www.pixiv.net/users/${esc(c.author_id)}" target="_blank" rel="noopener">主页</a>
        <button class="btn accent" onclick="pickAuthorCandidate(${i})">选这个</button></span></li>`).join('') +
      `</ul><div class="btn-row"><button class="btn ghost" onclick="closeSrcModal()">取消</button></div>`);
  } catch (e) {
    toast('联网查询失败: ' + e.message, 'err');
  } finally { setBusy(false); }
};

function applyAuthorCandidate(c, aid, saveBody) {
  if (saveBody) {
    doSaveLibEdit(aid, Object.assign({}, saveBody, {
      author_id: String(c.author_id), author_name: c.name || '' }));
    return;
  }
  const el = $('#lib-e-author');
  if (el) el.value = c.name ? `${c.name}_${c.author_id}` : String(c.author_id);
  toast(`已填入 pixiv 作者：${c.name || ''}（ID ${c.author_id}），记得点「保存」`, 'ok');
}

window.pickAuthorCandidate = function (i) {
  const c = (window._pixivCands || [])[i];
  if (!c) return;
  const ctx = window._pixivCandsSave || {};
  closeSrcModal();
  applyAuthorCandidate(c, ctx.aid, ctx.saveBody);
};

// 真正落库 (单独拆出来: 「保存」和「先联网确认作者再保存」两条路都走这里)
async function doSaveLibEdit(aid, body) {
  try {
    const r = await api('/api/library/update', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const err = apiErr(r);
    if (err) throw new Error(err);
    const outAid = String(r.author_id || body.author_id || '');
    const isPixiv = /^\d+$/.test(outAid);
    if (r.name) {
      toast(`已保存 ✓（作者：${r.name}${isPixiv ? ' · pixiv ' + outAid : ' · 自定义作者'}）`, 'ok');
    } else if (isPixiv) {
      // 只关联了 ID、名字还没拿到: 说清楚去哪补, 别让用户以为"没生效"
      toast(`已保存 ✓ 已关联 pixiv 作者 ID ${outAid}（名字会在体检「④ 联网补元数据」时自动补上）`, 'ok');
    } else {
      toast('已保存 ✓', 'ok');
    }
    closeSrcModal();
    closeLibModal();
    loadLibrary(); loadLibFacets();
    // 记下"刚改过这个作品" → 右下角 🔄 按钮就能只针对它做定向核对
    setLastAction('work', [aid], `刚编辑的作品 #${aid}`);
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function saveLibEdit(aid) {
  const tags = $$('#lib-tag-editor .tag.big').map(t => t.childNodes[0].textContent.trim());
  const urlEl = $('#lib-e-url');
  const auth = resolveAuthorInput($('#lib-e-author').value);
  const body = {
    artwork_id: aid,
    title: $('#lib-e-title').value.trim(),
    illust_type: parseInt($('#lib-e-type').value, 10),
    is_r18: parseInt($('#lib-e-r18').value, 10),
    source: $('#lib-e-source').value,
    source_url: urlEl ? (urlEl.value || '').trim() : '',
    tags,
    ...auth,
  };
  // 只写了一个库里没有的名字 → 先问"这位是 pixiv 作者吗", 别默默建成自定义作者
  if (auth.author_name && !auth.author_id) {
    const nm = String(auth.author_name).toLowerCase();
    const known = (facetData.authors || [])
      .some(a => String(a.name || '').toLowerCase() === nm);
    if (!known) { askIsPixivAuthor(aid, body, auth.author_name); return; }
  }
  await doSaveLibEdit(aid, body);
}

async function openLibTags(aid) {
  let w;
  const items = $('#lib-result')._lastItems || [];
  w = items.find(x => String(x.artwork_id) === String(aid));
  if (!w) {
    try { const r = await api('/api/library/search?q=' + encodeURIComponent(aid)); w = (r.items || []).find(x => String(x.artwork_id) === String(aid)); } catch (e) { /* ignore */ }
  }
  const tags = w && w.tags ? w.tags.map(t => esc(t)) : [];
  if (!facetData.tags.length) { try { await loadLibFacets(); } catch (e) { /* ignore */ } }
  openLibModal(`🏷️ 编辑标签 · ${esc(aid)}`, `
    <div class="tag-editor" id="lib-tag-editor">
      ${tags.length ? tags.map((t, i) => `<span class="tag big">${t} <button class="tag-x" data-i="${i}" onclick="libRemoveTag(this)">✕</button></span>`).join('') : '<div class="hint" id="lib-tags-empty">暂无标签，在下方添加。</div>'}
    </div>
    <div class="modal-field"><div class="combo-wrap">
      <input id="lib-tag-input" type="text" placeholder="输入标签关键词, 回车添加, 点选提示" autocomplete="off">
      <div class="combo-list" id="lib-tag-combo"></div></div></div>
    <div class="modal-actions">
      <button class="btn accent" onclick="saveLibTags('${esc(aid)}')">💾 保存</button>
      <button class="btn ghost" onclick="closeLibModal()">取消</button>
    </div>`);
  makeEditTagSuggest($('#lib-tag-input'), $('#lib-tag-combo'));
  $('#lib-tag-input').focus();
}

window.libAddTag = function () {
  const input = $('#lib-tag-input');
  const v = input.value.trim();
  if (!v) return;
  const editor = $('#lib-tag-editor');
  const empty = $('#lib-tags-empty');
  if (empty) empty.remove();
  const i = editor.querySelectorAll('.tag.big').length;
  editor.insertAdjacentHTML('beforeend', `<span class="tag big">${esc(v)} <button class="tag-x" data-i="${i}" onclick="libRemoveTag(this)">✕</button></span>`);
  input.value = '';
  input.focus();
};

window.libRemoveTag = function (btn) {
  btn.closest('.tag.big').remove();
};

async function saveLibTags(aid) {
  const tags = $$('#lib-tag-editor .tag.big').map(t => t.childNodes[0].textContent.trim());
  try {
    await api('/api/library/tags', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ artwork_id: aid, tags }) });
    toast('标签已保存 ✓', 'ok');
    closeLibModal(); loadLibrary(); loadLibFacets();
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

// ---- 兼容旧调用 (搜索框回车/按钮仍可用) ----
async function searchLibrary() { loadLibrary(); }

// 📋 全部作品 = 重置全部筛选 (关键字/类型/R18/作者/标签/缺元数据/排序) 回完整列表。
// 之前只是重新 loadLibrary, libFilters 里残留的作者/标签筛选清不掉, 导致"选了作者后回不去"。
async function loadLibraryRecent() {
  // 重置同时退出「仅查看该作品」特殊筛选 (否则列表仍被 only 过滤住)
  if (libOnlyAid) { libOnlyAid = null; libOnlyPrev = null; renderOnlyBanner(); _paintLibViewToggles(); }
  libFilters = { q: '', type: '', status: '', source: '', r18: '0', ai: '', author: '', tag: '', missing: false, sort: 'updated', order: 'desc' };
  $('#lib-search-input').value = '';
  ['#lib-f-type', '#lib-f-status', '#lib-f-source'].forEach(sel => { $(sel).value = ''; });
  $('#lib-f-sort').value = 'updated';   // 默认排序: 按修改时间 
  $('#lib-f-r18').value = '0';   // 默认非 R18
  $('#lib-f-ai').value = '';     // 默认 AI: 全部 (不隐藏任何作品)
  if (comboAuthor) comboAuthor.clear();
  if (comboTag) comboTag.clear();
  $('#lib-f-missing').classList.remove('active');
  $('#lib-f-missing-file').classList.remove('active');
  $('#lib-f-deleted').classList.remove('active');
  $('#lib-f-order').textContent = '↓ 降序';
  loadLibrary();
}

async function doBackup() {
  try {
    const r = await api('/api/library/backup', { method: 'POST' });
    if (r.status !== 'ok') throw new Error(r.error || '失败');
    toast('备份完成 ✓ ' + (r.path || ''), 'ok');
    // 备份中心开着 → 刷新清单, 让用户马上看到刚生成的那份
    const modal = $('#lib-modal');
    if (modal && modal.style.display === 'flex') openBackupCenter();
  } catch (e) { toast('备份失败: ' + e.message, 'err'); }
}

// ============ 一键整理 (预览后执行) ============
// 顶部「❔ 说明」: 展开/收起各项操作的说明面板 (默认收起, 只在需要时看)
window.tidyHelpToggle = function (btn) {
  const box = document.getElementById('tidy-help');
  if (!box) return;
  const opened = box.classList.toggle('hidden') === false;
  if (btn) btn.textContent = opened ? '❔ 收起' : '❔ 说明';
};

async function openTidyPreview() {
  try {
    // 「🪄 认领散落 pixiv 页」并入一键整理 :
    // 离线认领扫描与整理预览并行, 弹窗里认领分区在上、文件归位表格在下, 各自独立执行。
    const [r, scan] = await Promise.all([
      api('/api/library/tidy/preview', { method: 'POST' }),
      api('/api/library/claim-scan', { method: 'POST' }).catch(() => null),
    ]);
    const counts = r.counts || {};
    const claimHtml = claimSectionHtml(scan);
    if (!counts.total) {
      openLibModal('🧹 一键整理',
        (claimHtml || '<div class="hint">图库摆放已经全部规范：没有需要规范、认领或待确认的内容 ✓</div>') +
        '<div class="modal-actions"><button class="btn ghost" onclick="closeLibModal()">关闭</button></div>',
        { lg: true });
      return;
    }
    // 类型徽标 (emoji 一律补 U+FE0F 变体选择符 —— 🏷/❓ 缺了会渲染成黑白方框; 鼠标悬停出说明)
    const TYPE_BADGE = { rename: '🏷️ 目录规范', move: '📦 位置规范', filrename: '📝 命名规范', rmdir: '🧹 清理空夹', categorize: '❓️ 待确认' };
    const TYPE_TIP = {
      rename: '把作者文件夹名规范成「作者名_作品ID」。名字取自数据库（现用名 + 作者ID），不猜文件夹名；只改文件夹名，里面的文件不动。',
      move: '把散落在根目录或错误位置的图片，搬进它该在的作者文件夹：顶层是「作者名_作者ID」，里面按来源分「站点@账号」子夹（单来源也一样建这层，否则夹名里看不出作品来自哪个站）。目标路径同样由数据库算出。移动后同步数据库路径。',
      filrename: '把没有信息量的乱码文件名（纯哈希串 / 纯数字 / 太短）改成可读名字：pixiv 作品用作品ID，其他来源用「站点@账号_序号」。名字本身可读的一律不动。',
      rmdir: '整棵子树里除了 Thumbs.db 这类系统杂物外没有任何文件的文件夹 —— 就是归位/平铺搬空后留下的空夹。删掉不丢任何图片；夹里但凡有 .psd/.txt 等真实文件就一律不删。',
      categorize: '磁盘与数据库对不上，或身份无法识别 —— 系统不做猜测，留待人工判定；默认不勾选，执行时不会改动任何数据。'
    };
    window._tidyPlans = r.plans || [];
    const rows = (r.plans || []).map(p => {
      // 「待确认 · 作者归属存疑」: 给逐条出入口 —— 采纳夹名(写库) / 保持数据库(什么都不动)
      const claimBtns = (p.claim_aid && p.artwork_id) ? `
        <div class="tidy-claim">
          <button type="button" class="btn mini accent"
            onclick="tidyAdoptClaim('${esc(p.artwork_id)}','${esc(p.claim_aid)}','${esq(p.claim_name)}')"
            title="把「${esc(p.claim_name)}」(${esc(p.claim_aid)}) 写入数据库作为该作品的作者">✔ 采用夹名</button>
          <button type="button" class="btn mini"
            onclick="toast('已保留数据库记录 —— 直接执行上面的「目录规范」，文件夹名会被改成库里的作者名，这条就不会再出现','ok')"
            title="不做任何写入；执行「目录规范」后文件夹名会改成数据库里的作者名">✋ 保持数据库</button>
        </div>` : '';
      return `
      <tr class="${p.auto ? '' : 'tidy-risk'}">
        <td class="tidy-chk-cell">
          <input type="checkbox" class="tidy-chk" data-id="${p.id}" ${p.auto ? 'checked' : 'disabled'} title="${p.auto ? '勾选后执行此项' : '无法自动执行，需人工确认'}">
        </td>
        <td title="${esc(TYPE_TIP[p.type] || '')}">${TYPE_BADGE[p.type] || p.type}</td>
        <td class="tidy-from" title="${esc(p.from)}">
          <span class="tidy-path-text">${esc(p.from || '（根目录）')}</span>
          <button type="button" class="tidy-open" onclick="openTidyLoc(${p.id})" title="打开所在位置确认">📂</button>
        </td>
        <td>→</td>
        <td class="tidy-to" title="${esc(p.to)}">${esc(p.to)}</td>
        <td class="hint">${esc(p.reason)}${p.files.length ? ` · <b>${p.files.length}</b> 个文件` : ''}${claimBtns}</td>
      </tr>`;
    }).join('');
    const riskNote = counts.need_confirm
      ? `<div class="hint err-text">⚠ 其中 ${counts.need_confirm} 项需人工判定（默认不勾选，执行时不会改动任何数据）：可能是作者归属存疑，也可能是身份无法识别。核对「说明」列后可重新整理。</div>` : '';
    // 顶部只留一行计数摘要, 详细说明收进「❔ 说明」折叠面板 (鼠标悬停表格里的类型徽标也会出对应提示)
    const helpHtml =
      `<div id="tidy-help" class="tidy-help hidden">
        <div class="doc-title">数据怎么流动（唯一标准）</div>
        <div class="doc-note" style="margin-top:4px">
          <b>用户 → 数据库 → 磁盘</b>，只有这一个方向。数据库是唯一真相，文件夹名和文件名都是<b>产物</b>。
          系统<b>不会</b>根据文件夹名去改数据库；两边对不上时只把它列进「待确认」，人工定案后才写库，再由数据库同步到磁盘。
        </div>
        <div class="doc-title" style="margin-top:10px">本次包含的操作</div>
        <ul>
          <li><b>🏷️ 目录规范</b> · ${esc(TYPE_TIP.rename)}</li>
          <li><b>📦 位置规范</b> · ${esc(TYPE_TIP.move)}<ul>
            <li>顶层 = <code>作者名_作者ID</code>，里面 = <code>站点@账号</code>（无账号用 <code>#站点#</code>，未知来源 <code>#其他来源#</code>）。</li>
            <li><b>单来源也建这层来源子夹</b>：以前单来源会平铺进作者夹，结果夹名里看不出作品来自哪个站（如 <code>bilibili@Colorful辉色</code> 被搬成 <code>Colorful辉色_local_xxx</code>）；现在统一保留来源这一层。没有作者的作品才平铺 <code>站点@账号</code>。</li>
          </ul></li>
          <li><b>📝 命名规范</b> · ${esc(TYPE_TIP.filrename)}<ul>
            <li>pixiv 作品 → <code>{作品ID}_p{页码}.ext</code>（与下载一致）；已带规范名字的 pixiv 文件一律不动。</li>
            <li>非 pixiv 且原名是<b>纯哈希串、纯顺序号、太短</b>这类无信息乱码 → <code>{站点@账号}_{序号}.ext</code>，序号按作品 ID 排序生成、可重现。</li>
            <li><b>不写作者名</b>：作者名归数据库管，写进文件名就会被「可读名一律不动」永久固化，以后作者改名再也纠不回来。</li>
            <li><b>放大版（waifu2x / w2x）算特殊格式</b>：放大标记是「这是放大版」的唯一线索，一律保留，只把写法拉齐成 <code>_waifu2x[_noiseN][_scaleNx]</code>（固定顺序、<code>scale2x1</code>→<code>scale2x</code>、缩写 <code>w2x2x</code>→<code>waifu2x_scale2x</code>）。</li>
            <li>名字本身可读的（含中文/日文/英文单词/人名）<b>一律保留不动</b>；同一文件夹内撞名才加「 (2)」。</li>
          </ul></li>
          <li><b>🧹 清理空夹</b> · ${esc(TYPE_TIP.rmdir)}<ul>
            <li>为什么整理要管这个：<b>归位/平铺会把图从一个夹搬到另一个夹，从而留下空夹</b>；善后放同一次「一键」里做完，不用再跑去健康面板点一遍「删除空文件夹」。</li>
            <li>只列最外面那一处（子夹随它一起删），所以同一片残留不会列出一排重复行。</li>
          </ul></li>
          <li><b>❓️ 待确认</b> · ${esc(TYPE_TIP.categorize)}<ul>
            <li><b>作者归属存疑</b>：文件夹名解析出的作者与数据库记录不一致（例如夹名说「kase_22」(43460823)，库里记的是「ユウマロ」(22)）。以哪边为准需人工指定，定案后不再提示。</li>
            <li>该文件夹会被<b>暂时冻结</b>，不参与目录/位置/命名规范，免得先按库里改了磁盘、证据就没了。</li>
          </ul></li>
        </ul>
        <div class="doc-note">底下表格逐条列出「当前 → 目标」与理由，请核对后再执行。点行内 <b>📂</b> 可在资源管理器中打开所在位置核对。</div>
      </div>`;
    openLibModal(`🧹 一键整理 · 预览 ${counts.total} 项`,
      `<div class="hint tidy-sum"><button type="button" class="help-toggle" onclick="tidyHelpToggle(this)" title="展开 / 收起各项操作的说明">❔ 说明</button>` +
        `目录规范 <b>${counts.rename}</b> · 位置规范 <b>${counts.move}</b> · 命名规范 <b>${counts.filrename || 0}</b> · 清理空夹 <b>${counts.rmdir || 0}</b> · 待确认 <b>${counts.categorize}</b>　可自动执行 <b>${counts.auto}</b> 项 / 共涉及 <b>${counts.files}</b> 个文件</div>` +
      claimHtml +
      helpHtml +
      riskNote +
      '<div class="tidy-table-wrap"><table class="table"><thead><tr><th class="tidy-chk-cell"><input type="checkbox" id="tidy-selall" checked title="全选/取消所有可执行项"></th><th>类型</th><th>当前</th><th></th><th>目标</th><th>说明</th></tr></thead>' +
      `<tbody>${rows}</tbody></table></div>` +
      `<div class="modal-actions">
        <label class="tidy-selall-label"><input type="checkbox" id="tidy-selall-bottom" checked> ☑ 全选可执行项</label>
        <button class="btn accent" onclick="applyTidySel()">✅ 执行整理 (<span id="tidy-sel-n">${counts.auto}</span> 项)</button>
        <button class="btn ghost" onclick="closeLibModal()">取消</button>
      </div>`, { lg: true });
    // 全选联动 (顶部表头 + 底部按钮旁两个开关)
    const syncSel = () => {
      const all = Array.from(document.querySelectorAll('.tidy-chk')).filter(cb => !cb.disabled);
      const on = all.filter(cb => cb.checked).length;
      const state = on === all.length && all.length > 0;
      const s1 = $('#tidy-selall'), s2 = $('#tidy-selall-bottom');
      if (s1) s1.checked = state;
      if (s2) s2.checked = state;
      const n = $('#tidy-sel-n');
      if (n) n.textContent = on;
    };
    const bindSelAll = (id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener('change', () => {
        document.querySelectorAll('.tidy-chk').forEach(cb => { if (!cb.disabled) cb.checked = el.checked; });
        syncSel();
      });
    };
    bindSelAll('#tidy-selall'); bindSelAll('#tidy-selall-bottom');
    document.querySelectorAll('.tidy-chk').forEach(cb =>
      cb.addEventListener('change', syncSel));
  } catch (e) {
    openLibModal('🧹 一键整理', `<div class="hint err-text">预览失败: ${esc(e.message)}</div>` +
      '<div class="modal-actions"><button class="btn ghost" onclick="closeLibModal()">关闭</button></div>');
  }
}

// 打开整理项所在位置 (from 文件夹, 根目录散件定位到具体文件)
window.openTidyLoc = async function (id) {
  const p = (window._tidyPlans || []).find(x => String(x.id) === String(id));
  if (!p) return;
  let rel = p.from || (p.files && p.files[0]);
  if (!rel) { toast('该行没有本地路径可打开', 'err'); return; }
  try {
    await api('/api/library/open-folder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: rel })
    });
    toast('已在资源管理器中打开 📂', 'ok');
  } catch (e) { toast('打开失败: ' + e.message, 'err'); }
};

// 「待确认 · 作者归属存疑」→ 采纳文件夹名(写库)。这是单向闭环里唯一的回流写入动作:
// 用户判断 → 写数据库 → 下次整理由数据库去改磁盘。作者已建档时只改作品归属, 不动库里的作者名。
window.tidyAdoptClaim = async function (artworkId, aid, name) {
  if (!confirm(`确认作品 ${artworkId} 的作者是「${name}」(${aid})？\n\n` +
    `这会写入数据库；之后文件夹名与文件名都会按数据库来规范。`)) return;
  try {
    const r = await api('/api/library/tidy/adopt-claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artwork_id: artworkId, author_id: aid, author_name: name })
    });
    toast(`作品 ${artworkId} 已归入作者「${r.name}」✓ 重新整理后这条不会再出现`, 'ok');
    loadLibrary(); loadLibFacets(); loadStats();
    await openTidyPreview();
  } catch (e) { toast('写入失败: ' + e.message, 'err'); }
};

function applyTidySel() {
  const ids = Array.from(document.querySelectorAll('.tidy-chk:checked'))
    .map(cb => Number(cb.dataset.id));
  if (!ids.length) { toast('未勾选任何整理项', 'warn'); return; }
  applyTidy(ids);
}

async function applyTidy(ids) {
  try {
    const r = await api('/api/library/tidy/apply', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plans: ids }) });
    closeLibModal();
    const fail = (r.failed || []).length;
    toast(`整理完成 ✓ 目录规范 ${r.renamed} · 位置规范 ${r.moved} · 命名规范 ${r.filenamed || 0} · 清理空夹 ${r.rmdirs || 0}`
      + (fail ? ` · ${fail} 项跳过（见日志）` : ''), fail ? 'warn' : 'ok');
    loadLibrary(); libSync(); loadStats();
  } catch (e) {
    toast('整理失败: ' + e.message, 'err');
    closeLibModal();
  }
}

// ============ 反搜 ============
let reverseRunning = false;
let reversePollTimer = null;

async function loadReverseStatus() {
  try {
    const r = await api('/api/reverse/status');
    $('#reverse-status').innerHTML =
      `输入目录: <code>${esc(r.scan_dir)}</code> · 待反搜图片: <strong>${r.image_files}</strong> 张`;
    // 待反搜缩略图网格 (图片被移动/读取失败时隐藏, 不显示裂图)
    $('#reverse-pending-count').textContent = r.image_files ? `(${r.image_files})` : '';
    const grid = $('#reverse-pending-grid');
    // 轮询重绘时按文件名保持勾选状态
    const prev = new Set(Array.from(grid.querySelectorAll('.rev-pick'))
      .filter(cb => cb.checked).map(cb => cb.dataset.name));
    const hadPrev = grid.dataset.init === '1';
    grid.dataset.init = '1';
    grid.innerHTML = (r.pending || []).length
      ? r.pending.map(p => {
        const checked = !hadPrev || prev.has(p.name) ? 'checked' : '';
        const rel = p.rel || p.name;
        // 未找到的 (上次反搜没命中、被挪进 not_found/) 单独标出来, 并给「手动入库」出口 ——
        // 以前这些图从待反搜区消失, 用户以为"卡住了没法下一步"。
        const tag = p.not_found ? '<span class="tag rev-nf">未找到</span>' : '';
        return `
        <div class="rev-cell${p.not_found ? ' is-nf' : ''}">
          <label class="rev-pick-label"><input type="checkbox" class="rev-pick"
                 data-name="${esc(p.name)}" data-rel="${esc(rel)}" ${checked}> 选</label>
          ${tag}
          <img src="${safeThumb(p.url, p)}" alt="${esc(p.name)}" loading="lazy"
               onerror="this.closest('.rev-cell').style.display='none'">
          <div class="rev-cap" title="${esc(rel)}">${esc(p.name)}</div>
          <div class="rev-cell-ops">
            <button class="btn mini" title="直接把这张加入图库（建自定义作品，标题=文件名）"
                    onclick="revAdopt(['${esc(rel).replace(/'/g, "\\'")}'])">📥 入库</button>
            <button class="btn mini ghost" title="不要这张了：送进 Windows 电脑回收站（可还原）"
                    onclick="revDiscard(['${esc(rel).replace(/'/g, "\\'")}'])">🗑️</button>
          </div>
        </div>`; }).join('')
      : '<div class="rev-empty">输入目录为空，点【➕ 添加图片】选择要反搜的图片，或把图片放进上面显示的输入目录</div>';
    grid.querySelectorAll('.rev-pick').forEach(cb => cb.addEventListener('change', updateReverseSelCount));
    updateReverseSelCount();
    // 反搜任务表 (实时进度 + 最终结果, 含图库「进反搜」)
    renderReverseTasks(r);
    // 总进度条
    renderReverseProgress(r.progress);
    return r;
  } catch (e) {
    $('#reverse-status').textContent = '读取失败: ' + e.message;
  }
}

// ============ 反搜任务视图 (下载页式: 每张图一行, 实时状态 + 最终结果) ============
let revTaskFilter = 'all';     // 任务表分栏: all / running / done / failed
let revInputRunFiles = null;   // 本次输入反搜运行处理的文件 (startReverse 记录, 任务表据此显示)
let revTasksHidden = false;    // 用户点「清空」后不再从 last_result 重建
let revHiddenKeys = new Set(); // 「只清空失败」摘掉的行 (新一轮反搜开始 / 结果换代时自动失效)
let revHiddenStamp = null;     // 摘行时对应的 last_result.time
let revLastTasks = [];         // 最近一次渲染用的任务列表 (供「只清空失败」判断哪些是失败行)
let revLastStamp = null;       // 最近一次渲染时的 last_result.time (摘行时记下来当"代次")
let galleryAid = null;         // 图库「进反搜」的作品 ID (进度并入同一张任务表)
let galleryPollTimer = null;

const REV_STATUS_LBL = {
  wait: ['💤', '等待'], running: ['⏳', '识别中'], done: ['✓', '已处理'],
  ok: ['✓', '识别成功'], fail: ['✗', '未识别'], conflict: ['⚠', '待确认'],
  inlib: ['✓', '已在图库'], skip: ['⏭', '已跳过'],
};

function revStatusChip(status) {
  const [ic, lbl] = REV_STATUS_LBL[status] || ['?', status];
  const cls = status === 'running' ? 'running' : ['ok', 'inlib', 'done'].includes(status) ? 'done'
    : ['fail', 'conflict'].includes(status) ? 'failed' : 'pending';
  return `<span class="tag-state ${cls}">${ic} ${lbl}</span>`;
}

function revResultCell(t) {
  if (t.status === 'running') return `<span style="color:var(--accent)">识别中…</span>`;
  if (t.status === 'done') return '<span class="muted">已处理</span>';
  if (t.status === 'wait') return '<span class="muted">等待</span>';
  if (t.status === 'ok') return t.result || '✓ 识别成功';
  if (t.status === 'inlib') return '✅ 本地指纹命中：图库已有同一张（未重复归档）';
  if (t.status === 'skip') return '⏭ 目标已有同名文件，已跳过';
  if (t.status === 'conflict') {
    return `<span class="err-text">文件名与识别结果不一致（${esc(t.result || '')}）</span>
      <button class="btn mini" onclick="resolveReverseConflict('${esc(t.file || '').replace(/'/g, "\\'")}')">按文件名归档</button>`;
  }
  // 没识别出来: 明确给出路 —— "没找到结果就留在那了, 我不能继续入库了"
  const fn = String(t.file || '').replace(/'/g, "\\'");
  return `<span class="err-text">${esc(t.result || '未识别')}</span>
    <button class="btn mini" title="识别不出来也没关系：直接把这张加入图库（建自定义作品，标题=文件名）"
            onclick="revAdopt(['${fn}'])">📥 入库</button>
    <button class="btn mini ghost" title="不要这张了：送进 Windows 电脑回收站（可还原）"
            onclick="revDiscard(['${fn}'])">🗑️</button>`;
}

function revTaskThumb(t, pending) {
  const hit = (pending || []).find(p => p.name === t.name);
  if (hit && hit.url) {
    return `<img class="thumb-sm" src="${safeThumb(hit.url, hit)}" alt="" loading="lazy"
      onerror="this.style.visibility='hidden'">`;
  }
  const ch = String(t.name || '?').trim().charAt(0) || '?';
  return `<div class="thumb-sm placeholder">${esc(ch)}</div>`;
}

// 任务表: 输入反搜(本次运行文件) + 图库反搜(src_search) 混合渲染; 结束后用 last_result 逐文件定稿
function renderReverseTasks(r) {
  const p = (r && r.progress) || {};
  const lr = (r && r.last_result) || null;
  revLastStamp = (lr && lr.time) || null;
  const ss = (r && r.src_search) || {};
  const pending = (r && r.pending) || [];
  const tasks = [];

  // ① 输入反搜任务
  if (revInputRunFiles && revInputRunFiles.length) {
    const done = p.done || 0;
    revInputRunFiles.forEach((f, i) => {
      const name = typeof f === 'object' ? f.name : f;
      tasks.push({ key: 'in-' + i, name, file: name, kind: 'input',
        status: i < done ? 'done' : (i === done && p.running ? 'running' : 'wait'), result: '' });
    });
  } else if (!revTasksHidden && lr && lr.time && !p.running) {
    // 页面重载后的兜底: 从最近一次结果重建 (无进行中任务时)
    const mark = (x) => (lr.success || []).includes(x) ? 'ok' : (lr.failed || []).includes(x) ? 'fail'
      : (lr.conflict || []).includes(x) ? 'conflict' : (lr.in_library || []).includes(x) ? 'inlib' : 'skip';
    const all = [...(lr.success || []), ...(lr.failed || []), ...(lr.conflict || []),
                  ...(lr.in_library || []), ...(lr.skipped || [])];
    all.forEach((x, i) => tasks.push({
      key: 'res-' + i, name: (x.original_file || x.file || '').split(/[\\/]/).pop(),
      file: (x.original_file || x.file || '').split(/[\\/]/).pop(), kind: 'input',
      status: mark(x), result: x.reason || x.author || (x.artwork_id ? ('作品 ' + x.artwork_id) : '') }));
  }
  // 运行结束后: 用 last_result 逐文件定稿 (success/failed/conflict/in_library/skipped)
  if (!p.running && lr && lr.time && revInputRunFiles && revInputRunFiles.length) {
    const fin = new Map();
    const addFin = (arr, status) => (arr || []).forEach(x => {
      const k = (x.original_file || x.file || '').split(/[\\/]/).pop();
      if (k) fin.set(k, { status, x });
    });
    addFin(lr.success, 'ok'); addFin(lr.failed, 'fail'); addFin(lr.conflict, 'conflict');
    addFin(lr.in_library, 'inlib'); addFin(lr.skipped, 'skip');
    for (const t of tasks) {
      const f = fin.get(t.name);
      if (f) {
        t.status = f.status;
        t.result = f.x.reason || f.x.author
          || (f.x.site && f.x.artwork_id ? `${SITE_BADGE[f.x.site] || f.x.site} 作品 #${f.x.artwork_id}` : '')
          || (f.x.artwork_id ? '作品 ' + f.x.artwork_id : '') || '';
      }
    }
  }

  // ② 图库「进反搜」任务 (src_search 进度)
  const gal = galleryAid && ss && ss.artwork_id === galleryAid;
  if (gal && (ss.running || ss.phase === 'done')) {
    const n = Math.max(ss.total || 0, ss.done || 0, 1);
    for (let i = 0; i < n; i++) {
      tasks.push({ key: 'g-' + i, name: `图库 #${galleryAid} · p${i}`, kind: 'gallery',
        status: i < (ss.done || 0) ? 'done' : (i === (ss.done || 0) && ss.running ? 'running' : 'wait'),
        result: i < (ss.done || 0) ? '已识别' : '' });
    }
  }

  // 分栏筛选 + 渲染
  // 「只清空失败」摘掉的行在这里过滤掉; 结果换代(last_result.time 变了)就自动作废 ——
  // 不能因为上次摘过, 让同一张图这次新跑出来的失败结果也被藏起来。
  if (revHiddenStamp !== null && lr && lr.time && lr.time !== revHiddenStamp) {
    revHiddenKeys.clear();
    revHiddenStamp = null;
  }
  const visible = revHiddenKeys.size ? tasks.filter(t => !revHiddenKeys.has(t.key)) : tasks;
  revLastTasks = visible;
  const filtered = revTaskFilter === 'all' ? visible
    : revTaskFilter === 'running' ? visible.filter(t => t.status === 'running' || t.status === 'wait')
    : revTaskFilter === 'done' ? visible.filter(t => ['ok', 'inlib', 'done', 'skip'].includes(t.status))
    : visible.filter(t => ['fail', 'conflict'].includes(t.status));
  const tbody = $('#rev-task-table tbody');
  $('#rev-task-count').textContent = visible.length ? `(${visible.length})` : '';
  $('#rev-task-total').textContent = visible.length ? `共 ${visible.length} 条` : '';
  $('#rev-task-empty').style.display = visible.length ? 'none' : '';
  // 「清空失败」按钮: 有失败行才可点 (没失败就没必要给个死按钮)
  const nFail = visible.filter(t => ['fail', 'conflict'].includes(t.status)).length;
  const failBtn = $('#rev-task-clear-failed');
  if (failBtn) {
    failBtn.disabled = !nFail;
    failBtn.textContent = nFail ? `🧹 清空失败（${nFail}）` : '🧹 清空失败';
  }
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="hint" style="text-align:center">当前分栏下没有任务。</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(t => `
    <tr>
      <td class="cell-thumb">${revTaskThumb(t, pending)}</td>
      <td class="task-title" title="${esc(t.name)}">${esc(t.name)}</td>
      <td class="cell-progress">${revResultCell(t)}</td>
      <td class="cell-status">${revStatusChip(t.status)}</td>
    </tr>`).join('');
}

function clearReverseTasks() {
  revInputRunFiles = null;
  revTasksHidden = true;
  revHiddenKeys.clear();
  revHiddenStamp = null;
  galleryAid = null;
  $('#rev-gallery-result').innerHTML = '';
  loadReverseStatus();
}

// 只清空失败：把「未识别 / 待确认」的行从列表里摘掉，成功/进行中的行原样留着。
// 文案必须**如实**: 以前无脑写"(文件仍在待反搜区)", 用户明明已经手动入库、收件箱也空了,
// 却被这句话反复误导。现在按真实的待反搜张数说话。
window.clearReverseFailedTasks = async function () {
  const fails = revLastTasks.filter(t => ['fail', 'conflict'].includes(t.status));
  if (!fails.length) { toast('当前没有失败的任务', ''); return; }
  fails.forEach(t => revHiddenKeys.add(t.key));
  revHiddenStamp = revLastStamp;      // 结果换代(下一轮反搜)时这些 key 自动失效
  const r = await loadReverseStatus();   // 顺手让服务端把"文件已不在收件箱"的过时记录也清掉
  const left = (r && r.image_files) || 0;
  toast(`已从任务列表移除 ${fails.length} 条失败记录`
    + (left ? `（待反搜区还有 ${left} 张没处理，可「📥 入库」或「🗑️ 丢弃」）`
            : '（待反搜区已空，这些记录不会再出现）'), 'ok');
};

// 入库/丢弃成功后: 这几张不再属于"本次运行的任务", 从任务表里去掉 (否则失败行一直挂着)
function revDropFromRun(files) {
  if (!revInputRunFiles || !revInputRunFiles.length) return;
  const base = (v) => String(typeof v === 'object' ? v.name : v).split(/[\\/]/).pop();
  const gone = new Set((files || []).map(base));
  revInputRunFiles = revInputRunFiles.filter(f => !gone.has(base(f)));
}

// 总进度条 (批次点已由任务表逐行状态取代)
function renderReverseProgress(p) {
  const box = $('#reverse-progress');
  if (!box) return;
  if (!p || p.running !== true) { box.style.display = 'none'; return; }
  box.style.display = '';
  const total = p.total || 0, done = p.done || 0;
  const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  const fill = $('#reverse-progress-fill');
  if (fill) fill.style.width = pct + '%';
  const txt = $('#reverse-progress-text');
  if (txt) {
    const phaseTxt = p.phase === 'break' ? `⏸ 批次休息 ${p.rest_sec}s`
      : p.phase === 'error' ? '✗ 任务异常' : '⏳ 反搜中';
    txt.textContent = `${done}/${total} · ${pct}% · ${phaseTxt}`;
  }
  const line = $('#reverse-progress-line');
  if (line) {
    const el = p.started_at ? Math.round(Date.now() / 1000 - p.started_at) : 0;
    const bs = p.batch_size || 5;
    const nB = total ? Math.ceil(total / bs) : 1;
    line.textContent = (p.current ? `当前: ${esc(p.current)} · ` : '') +
      `已用时 ${el}s · 批次 ${p.batch || 1}/${nB}`;
  }
}

function updateReverseSelCount() {
  const n = document.querySelectorAll('#reverse-pending-grid .rev-pick:checked').length;
  const cnt = $('#reverse-sel-count');
  if (cnt) cnt.textContent = n;
  const btnSel = $('#reverse-run-sel');
  if (btnSel) btnSel.disabled = n === 0;
  // 批量「入库 / 丢弃」也跟着勾选状态启停 (反搜识别不出来时的出路)
  const bAdopt = $('#reverse-adopt-sel'), bDrop = $('#reverse-drop-sel');
  if (bAdopt) bAdopt.disabled = n === 0;
  if (bDrop) bDrop.disabled = n === 0;
  const all = document.querySelectorAll('#reverse-pending-grid .rev-pick');
  const ckAll = $('#reverse-check-all');
  if (ckAll && all.length) ckAll.checked = (all.length === n);
}

// 当前勾选的待反搜文件 (用 rel: 顶层就是文件名, not_found 里的是 "not_found/xxx")
function revSelectedRels() {
  return Array.from(document.querySelectorAll('#reverse-pending-grid .rev-pick:checked'))
    .map(cb => cb.dataset.rel || cb.dataset.name).filter(Boolean);
}

// 📥 手动入库: 识别不出来的图也能进图库 (建自定义作品, 标题=文件名)
window.revAdopt = async function (files) {
  const list = files && files.length ? files : revSelectedRels();
  if (!list.length) { toast('先勾选要入库的图片', 'err'); return; }
  if (!confirm(`把这 ${list.length} 张图直接加入图库？\n\n会收纳到 img/${'#其他来源#'} 并建成「自定义作品」（标题=文件名），\n收件箱里的原文件会被移走（图库里有的是副本）。`)) return;
  try {
    const r = await api('/api/reverse/adopt', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: list }) });
    if (r.status !== 'ok') throw new Error(r.error || '入库失败');
    const parts = [`📥 已入库 ${r.imported.length} 张`];
    if ((r.skipped || []).length) parts.push(`⏭ 跳过 ${r.skipped.length} 张（${r.skipped[0].reason}）`);
    if ((r.failed || []).length) parts.push(`✖ 失败 ${r.failed.length} 张（${r.failed[0].reason}）`);
    if (r.over_limit) parts.push(`⚠ 单次最多 500 张，另有 ${r.over_limit} 张未处理（请再点一次）`);
    toast(parts.join(' · '), r.imported.length ? 'ok' : 'info');
    // 收走的这几张不该再挂在「本次任务」里 (否则失败行一直显示, 用户以为没生效)
    revDropFromRun(list);
    await loadReverseStatus();
    loadLibrary(); loadStats(); loadLibFacets(); loadTrashBadge();
  } catch (e) { toast('入库失败: ' + e.message, 'err'); }
};

// 🗑️ 丢弃: 送进 Windows 电脑回收站(可还原), 不是硬删
window.revDiscard = async function (files) {
  const list = files && files.length ? files : revSelectedRels();
  if (!list.length) { toast('先勾选要丢弃的图片', 'err'); return; }
  if (!confirm(`丢弃这 ${list.length} 张待反搜图片？\n\n它们会被送进 **Windows 电脑回收站**（之后仍可从那里还原）。`)) return;
  try {
    const r = await api('/api/reverse/discard', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: list }) });
    toast(`已丢弃 ${r.removed} 张 · 送电脑回收站 ${r.recycled} 个`
          + (r.over_limit ? ` · ⚠ 另有 ${r.over_limit} 张超出单次上限未处理` : ''), 'ok');
    revDropFromRun(list);
    await loadReverseStatus();
  } catch (e) { toast('丢弃失败: ' + e.message, 'err'); }
};

async function uploadReverseImages(input) {
  const files = input.files;
  if (!files || !files.length) return;
  input.disabled = true;
  try {
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    const r = await fetch('/api/reverse/upload', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    let msg = `已添加 ${data.saved.length} 张图片 ✓`;
    if (data.rejected && data.rejected.length) msg += `（${data.rejected.length} 个非图片文件已跳过）`;
    toast(msg, 'ok');
    await loadReverseStatus();
  } catch (e) {
    toast('添加失败: ' + e.message, 'err');
  } finally {
    input.disabled = false;
    input.value = '';  // 允许再次选同一文件
  }
}

async function startReverse(mode, files, toastMsg) {
  if (reverseRunning) return;
  reverseRunning = true;
  const btns = [$('#reverse-run'), $('#reverse-run-sel')];
  btns.forEach(b => { if (b) b.disabled = true; });
  $('#reverse-run').textContent = '⏳ 反搜进行中…';
  try {
    const startTs = Date.now();
    const archiveSel = $('#reverse-archive');
    const existSel = $('#reverse-existing');
    const body = {
      mode,
      archive: archiveSel ? archiveSel.value === '1' : true,
      existing: existSel ? existSel.value : 'copy',
      batch_size: parseInt($('#reverse-batch-size').value, 10) || 5,
      batch_break_sec: parseInt($('#reverse-batch-break').value, 10) || 30,
    };
    if (files) body.files = files;
    await api('/api/reverse/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    // 任务表: 记录本次运行要处理的文件 (选中项或当前全部待反搜), 供逐行显示进度
    revTasksHidden = false;
    // 新一轮开始 → 上次「只清空失败」摘掉的行全部恢复 (同一张图这次再失败也要看得见)
    revHiddenKeys.clear();
    revHiddenStamp = null;
    if (files) {
      revInputRunFiles = files.slice();
    } else {
      revInputRunFiles = Array.from(document.querySelectorAll('#reverse-pending-grid .rev-pick'))
        .map(cb => cb.dataset.name);   // 反搜全部 = 待反搜区所有图片 (与勾选无关)
    }
    toast(toastMsg || (files ? `已启动：反搜选中的 ${files.length} 张` : '反搜已启动，完成后这里自动显示结果'), 'ok');
    // 1s 轮询进度: running=False 且 phase=done/error 即完成 (结果文件在置位前已写完);
    // 兼容旧服务端: 结果时间更新 或 待反搜清空 也判定完成; 上限 10 分钟自动停
    clearInterval(reversePollTimer);
    let ticks = 0;
    reversePollTimer = setInterval(async () => {
      ticks++;
      const r = await loadReverseStatus();
      const p = (r && r.progress) || {};
      const remainSelected = files
        ? (r.pending || []).some(p => files.includes(p.name))
        : r.image_files > 0;
      const wroteResult = r.last_result && r.last_result.time &&
        new Date(String(r.last_result.time).replace(' ', 'T')).getTime() >= startTs - 5000;
      const oldDone = r && (wroteResult || !remainSelected);
      const finished = (p.running === false && (p.phase === 'done' || p.phase === 'error' || p.phase === 'ok') && ticks > 2)
        || oldDone;
      if (finished || ticks > 600) {
        clearInterval(reversePollTimer);
        reverseRunning = false;
        btns.forEach(b => { if (b) b.disabled = false; });
        $('#reverse-run').textContent = '▶ 反搜全部';
        updateReverseSelCount();
        renderReverseProgress(p);
        if (finished) {
          if (p.phase === 'error') toast('反搜任务异常: ' + (p.error || '未知'), 'err');
          else toast('反搜完成 ✓ 请查看结果分区（注意⚠待确认项）', 'ok');
        }
      }
    }, 1000);
  } catch (e) {
    toast('启动失败: ' + e.message, 'err');
    reverseRunning = false;
    btns.forEach(b => { if (b) b.disabled = false; });
    $('#reverse-run').textContent = '▶ 反搜全部';
  }
}

async function runReverse(onlySelected) {
  const mode = $('#reverse-mode').value;
  let files = null;
  if (onlySelected) {
    files = Array.from(document.querySelectorAll('#reverse-pending-grid .rev-pick:checked'))
      .map(cb => cb.dataset.name);
    if (!files.length) { toast('请先勾选要反搜的图片', 'err'); return; }
  }
  await startReverse(mode, files);
}

// 人工裁决: 冲突时以文件名 ID 为准, 走「按ID直查」(Pixiv 官方信息归档)
async function resolveReverseConflict(fileName) {
  await startReverse('id', [fileName], `已按文件名 ID 归档 ${fileName}（Pixiv 官方信息）`);
}

// ============ 登录 / 账户 ============
let loginPollTimer = null;

async function refreshAuth() {
  let loggedIn = false, account = null;
  try {
    const r = await api('/api/auth/status');
    loggedIn = !!r.cookies_exist;
    account = r.account || null;
  } catch (e) { /* 状态读取失败按未登录处理 */ }

  const name = account && account.name;
  const avatarUrl = account && account.avatar ? '/api/auth/avatar' : '';
  const avatarHtml = loggedIn
    ? (avatarUrl ? `<img src="${avatarUrl}" alt="" class="ac-avatar-img">` : '🙂')
    : '👤';

  // 顶栏头像按钮
  const avatar = $('#account-avatar'), label = $('#account-label'), menu = $('#account-menu');
  if (avatar) {
    avatar.innerHTML = avatarHtml;
    avatar.classList.toggle('ok', loggedIn);
    avatar.classList.toggle('off', !loggedIn);
  }
  if (label) label.textContent = loggedIn ? (name || '已登录') : '未登录';
  if (menu) {
    menu.innerHTML = loggedIn
      ? `<div class="menu-item disabled"><span class="account-dot ok"></span>${esc(name || '已登录')}</div>
         <button class="menu-item" onclick="openLoginAndClose()">🔑 切换 / 重新登录</button>
         <button class="menu-item danger" onclick="logoutAccount()">🚪 退出登录</button>
         <button class="menu-item" onclick="gotoSettingsAndClose()">⚙ 账户设置</button>`
      : `<div class="menu-item disabled"><span class="account-dot off"></span>当前未登录（点击头像即可登录）</div>
         <button class="menu-item accent" onclick="openLoginAndClose()">🔑 登录 Pixiv</button>
         <button class="menu-item" onclick="gotoSettingsAndClose()">⚙ 账户设置</button>`;
  }

  // 设置页账户卡片
  const acAvatar = $('#ac-avatar'), acName = $('#ac-name'), acStatus = $('#ac-status');
  if (acAvatar) {
    acAvatar.innerHTML = avatarHtml;
    acAvatar.classList.toggle('ok', loggedIn);
    acAvatar.classList.toggle('off', !loggedIn);
  }
  if (acName) acName.textContent = loggedIn ? (name || '已登录') : '未登录';
  if (acStatus) {
    acStatus.textContent = loggedIn
      ? (account && account.user_id ? `Pixiv 账号 #${account.user_id}，登录态自动注入查询/下载` : '登录态已保存，查询/下载时自动注入')
      : '点击下方按钮登录 Pixiv 账号';
  }
  const btnLogin = $('#ac-login'), btnLogout = $('#ac-logout');
  if (btnLogin) btnLogin.style.display = loggedIn ? 'none' : '';
  if (btnLogout) btnLogout.style.display = loggedIn ? '' : 'none';
}

function toggleAccountMenu(force) {
  const menu = $('#account-menu');
  if (menu) menu.classList.toggle('hidden', force === undefined ? menu.classList.contains('hidden') === false : !force);
}
window.openLoginAndClose = function () {
  toggleAccountMenu(false);
  switchTab('settings');
  startLogin();
};
window.gotoSettingsAndClose = function () {
  toggleAccountMenu(false);
  switchTab('settings');
};
window.logoutAccount = async function () {
  toggleAccountMenu(false);
  switchTab('settings');
  await doLogout();
};

// 登录: 弹出可见浏览器, 后端轮询到登录成功后【自动保存 Cookies】, 前端只需等结果
async function startLogin() {
  const btn = $('#ac-login'), cancelBtn = $('#ac-cancel-login'), hint = $('#ac-login-hint');
  try {
    await api('/api/auth/login', { method: 'POST' });
  } catch (e) { toast('打开登录浏览器失败: ' + e.message, 'err'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 等待登录…（在弹出的浏览器里操作）'; }
  if (cancelBtn) cancelBtn.style.display = '';
  if (hint) hint.style.display = '';
  toast('登录浏览器已弹出，登录成功后会自动保存 ✓', 'ok');
  clearInterval(loginPollTimer);
  loginPollTimer = setInterval(async () => {
    try {
      const st = await api('/api/auth/login-status');
      if (st.state === 'success') {
        finishLoginUI();
        toast('登录成功，欢迎 ' + ((st.account && st.account.name) || '') + ' 🎉', 'ok');
        refreshAuth();
      } else if (st.state === 'idle' && st.msg && !st.account) {
        // 窗口被关 / 超时 / 失败
        finishLoginUI();
        if (st.msg) toast(st.msg, 'err');
      }
    } catch (e) { /* 轮询偶发失败忽略 */ }
  }, 2000);
}
function finishLoginUI() {
  clearInterval(loginPollTimer);
  const btn = $('#ac-login'), cancelBtn = $('#ac-cancel-login'), hint = $('#ac-login-hint');
  if (btn) { btn.disabled = false; btn.textContent = '🔑 登录 Pixiv'; }
  if (cancelBtn) cancelBtn.style.display = 'none';
  if (hint) hint.style.display = 'none';
}
window.cancelLogin = async function () {
  try { await api('/api/auth/cancel-login', { method: 'POST' }); } catch (e) { /* ignore */ }
  finishLoginUI();
  toast('已取消登录', '');
};

async function doLogout() {
  if (!confirm('确定退出登录？将删除本地保存的 Cookies。\n（切换账号：退出后点【登录 Pixiv】，在弹出的浏览器里登录另一个号即可）')) return;
  try {
    await api('/api/auth/logout', { method: 'POST' });
    toast('已退出登录', 'ok');
  } catch (e) { toast('退出失败: ' + e.message, 'err'); }
  refreshAuth();
}
window.doLogout = doLogout;
window.startLogin = startLogin;

window.showBrowser = async function () {
  try {
    await api('/api/auth/show-browser', { method: 'POST' });
    toast('浏览器窗口即将弹出（登录态自动注入）', 'ok');
  } catch (e) { toast('失败: ' + e.message, 'err'); }
};
window.hideBrowser = async function () {
  try {
    await api('/api/auth/hide-browser', { method: 'POST' });
    toast('浏览器已隐藏，恢复后台无头运行', 'ok');
  } catch (e) { toast('失败: ' + e.message, 'err'); }
};

// ============ 🖼️ 缩略图缓存管理 (设置页) ============
let _thumbsPollTimer = null;
async function loadThumbsStats() {
  const box = $('#thumbs-stats');
  if (!box) return;
  try {
    const d = await api('/api/thumbs/stats');
    const warmTxt = d.warming ? '　<span class="tag" style="background:#fff3cd;color:#856404">预热中…</span>'
      : (d.done ? '　<span class="tag" style="background:#d4edda;color:#155724">已完成</span>'
         : '　<span class="tag">未预热</span>');
    box.innerHTML = `缓存文件 <strong>${d.count}</strong> 个，占用 <strong>${d.size_mb} MB</strong>` +
      (d.orphans ? `，其中<strong class="err-text">孤儿 ${d.orphans}</strong> 个（图被移动/改名后的残留，点「清理孤儿」删除）` : '') +
      warmTxt;
  } catch (e) {
    box.innerHTML = `<span class="hint">读取失败: ${esc(e.message)}</span>`;
  }
}
async function warmThumbs() {
  try {
    const r = await api('/api/thumbs/warm', { method: 'POST' });
    if (r.status === 'already_running') {
      toast('预热已在运行中，请稍候', 'warn');
    } else {
      toast('已开始后台预热全部缩略图（浏览日志查看进度）', 'ok');
    }
    loadThumbsStats();
    // 预热期间每 5s 刷新一次状态, 直到完成或超时 10 分钟
    if (_thumbsPollTimer) clearInterval(_thumbsPollTimer);
    let stopAfter = Date.now() + 10 * 60 * 1000;
    _thumbsPollTimer = setInterval(async () => {
      await loadThumbsStats();
      if (Date.now() > stopAfter) { clearInterval(_thumbsPollTimer); _thumbsPollTimer = null; }
    }, 5000);
  } catch (e) { toast('失败: ' + e.message, 'err'); }
}
async function clearThumbs() {
  if (!confirm('确认清空缩略图缓存？下次浏览原图时会自动重建（不影响原图）')) return;
  try {
    const r = await api('/api/thumbs/clear', { method: 'POST' });
    toast(`已清空 ${r.deleted} 个缓存文件`, 'ok');
    loadThumbsStats();
  } catch (e) { toast('失败: ' + e.message, 'err'); }
}
async function clearThumbsOrphans() {
  if (!confirm('删除孤儿缩略图（图片被移动/改名/删除后的残留缓存）？当前图片的缩略图不受影响')) return;
  try {
    const r = await api('/api/thumbs/clear-orphans', { method: 'POST' });
    toast(r.deleted ? `已清理 ${r.deleted} 个孤儿缓存 ✓` : '当前没有孤儿缓存 ✓', 'ok');
    loadThumbsStats();
  } catch (e) { toast('失败: ' + e.message, 'err'); }
}

// ============ 退出程序 (设置页) ============
// 全屏遮罩 + 尝试关闭本标签页。多窗口场景下每个标签页各自收到 app_quit 事件后调用,
// 保证一次退出全部关闭, 而不是只关点击的那个。
function showExitMask() {
  if (document.getElementById('quit-mask')) return;  // 已有遮罩不重复叠加
  const mask = document.createElement('div');
  mask.id = 'quit-mask';
  mask.style.cssText = 'position:fixed;inset:0;background:rgba(20,22,28,.92);z-index:99999;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:#e8eaed;font-size:18px';
  mask.innerHTML = '<div style="width:44px;height:44px;display:flex;align-items:center;justify-content:center">'
    + '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="#e8eaed" stroke-width="2"'
    + ' stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M12 3v9"/>'
    + '<path d="M6.2 5.8a8 8 0 1 0 11.6 0"/></svg></div>'
    + '<div>程序已退出，端口已释放</div><div style="font-size:13px;color:#9aa0a6">正在关闭标签页…</div>';
  document.body.appendChild(mask);
  // 尝试关闭当前标签页 (Chromium 允许关闭导航栈 < 2 条的页面, 本 SPA 不产生历史记录):
  // 若浏览器只剩当前这一个标签页, 关闭它即连浏览器窗口一起退出。
  setTimeout(() => { try { window.close(); } catch (e) { /* 被浏览器拦截则保留遮罩, 提示用户手动关闭 */ } }, 600);
}

window.quitApp = async function () {
  if (!confirm('确认退出程序？\n\n• 后台服务将完全停止，所有占用端口立即释放\n• 浏览器驱动等后台进程一并关闭\n• 退出后本页面无法继续操作，下次使用需重新启动程序')) return;
  try {
    await api('/api/app/quit', { method: 'POST' });
  } catch (e) { /* 进程退出中, fetch 可能被中断, 属正常 */ }
  showExitMask();
};

// ============ 事件绑定 / 初始化 ============
// 输入框右侧小叉叉: 有内容时显示, 点击清空文本并触发 onClear
function attachInputClear(input, onClear) {
  const wrap = input.closest('.search-box, .combo');
  const btn = wrap && wrap.querySelector('.input-clear');
  if (!wrap || !btn) return;
  const sync = () => { btn.hidden = !input.value.trim(); };
  // 把 sync 挂到元素上: 外部代码想"静默改值后刷新小叉叉"时直接调它 ——
  // 以前是派发假 input 事件来触发 sync, 但 input 事件同时是「展开下拉面板」的触发器,
  // 于是点一次作者名会把作者/标签两个下拉框一起弹开。
  input._clearSync = sync;
  input.addEventListener('input', sync);
  input.addEventListener('keyup', sync);
  btn.addEventListener('click', () => {
    input.value = '';
    sync();
    input.focus();
    if (onClear) onClear();
  });
  sync();
}

function bind() {
  // 顶栏搜索: 切到「查询」页 + 激活「搜索作品」子页再搜
  const startSearchFromBar = () => {
    $$('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === 'search'));
    $$('.tabpage').forEach(p => p.classList.toggle('active', p.id === 'tab-search'));
    // 顶栏发起的是**新搜索** → 撤掉「从关键词结果进作者详情」的返回点
    authorDetailBack = null;
    // 尊重用户当前选中的类型子页(artwork/author), 不再强制重置为作品
    doSearch($('#search-input').value);
  };
  $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); startSearchFromBar(); });
  $('#search-btn').addEventListener('click', startSearchFromBar);
  // 显式监听 Enter: 保证焦点在输入框时按回车必触发搜索 (表单隐式提交在某些环境不可靠)
  $('#search-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); startSearchFromBar(); }
  });
  // 顶栏搜索框小叉叉: 清空文本 (不触发搜索, 让用户重新输入)
  attachInputClear($('#search-input'), null);

  // 「查询」页内 作品/作者 双子页切换 (点击切换才开始搜对应类型)
  // 只绑 [data-scope] 的: 推荐区的 .feed-kind 按钮也带 .sstab 样式, 不该走这条
  $$('.sstab[data-scope]').forEach(b => b.addEventListener('click', () => setSearchScope(b.dataset.scope)));

  // 推荐区 (2026-09-20): 来源切换 + 刷新
  $$('.feed-kind').forEach(b => b.addEventListener('click', () => setFeedKind(b.dataset.kind)));
  const feedRefresh = $('#search-feed-refresh');
  if (feedRefresh) feedRefresh.addEventListener('click', () => loadFeed(true));

  // 🚫 隐藏 AI 作品 (2026-09-20): 就地重渲染缓存里的原始响应 —— 不重新联网,
  // 切一下按钮不该再打一次 pixiv。
  (function () {
    const btn = $('#search-hide-ai');
    if (!btn) return;
    const paint = () => btn.classList.toggle('active', searchHideAi);
    paint();
    btn.addEventListener('click', async () => {
      searchHideAi = !searchHideAi;
      try { localStorage.setItem('vixiq.searchHideAi', searchHideAi ? '1' : '0'); } catch (e) {}
      paint();
      toast(searchHideAi ? '🚫 已隐藏 AI 作品' : '👁 已显示 AI 作品', 'ok');
      const cached = searchCache[searchScope];
      const sbox = $('#search-result');
      if (cached && cached.res && sbox && sbox.innerHTML.trim()) {
        renderSearch(sbox, cached.res);
        cached.html = sbox.innerHTML;
      }
      // 推荐区也要跟着变。排行榜那批的 AI 标记是「按需联网补」的 —— 第一次拨开
      // 「隐藏 AI」必须重取一次, 否则拿一份全是"未知"的缓存去过滤 = 一张都藏不掉。
      if (!$('#search-feed').hidden) {
        if (searchHideAi && feedNeedsAi(feedKind)) await loadFeed();
        else if (feedCache[feedKind]) renderFeed(feedCache[feedKind]);
      }
    });
  })();

  // 搜索页作品卡点击 → 自研「远程大图」弹窗 (2026-10-20 需求,
  // 不再直接跳 pixiv 原站)。原站入口保留为卡片上已有的「🔗 原网页」按钮 (它是 <a>, 由守卫放行)。
  // 保留 mousedown/mouseup 位移判定「拖选复制」→ 不打开弹窗; 推荐区 (.work-card) 同样处理。
  ['#search-result', '#search-feed-body'].forEach((sel) => {
    const sbox = $(sel);
    if (!sbox) return;
    let downX = 0, downY = 0, dragged = false;
    sbox.addEventListener('mousedown', (e) => { downX = e.clientX; downY = e.clientY; dragged = false; });
    sbox.addEventListener('mouseup', (e) => {
      if (Math.abs(e.clientX - downX) > 4 || Math.abs(e.clientY - downY) > 4) dragged = true;
    });
    sbox.addEventListener('click', (e) => {
      if (e.target.closest('a, button, input, select, textarea, .w-actions, .w-path')) return;
      const card = e.target.closest('.work-card');
      if (!card || !card.dataset.aid) return;
      if (dragged) { dragged = false; return; }   // 拖选文字/图片, 不打开
      dragged = false;
      window.openRemoteLightbox(card);
    });
  });

  $$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));

  // 顶栏头像按钮: 点击展开账户菜单; 点击菜单外区域自动收起
  $('#account-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleAccountMenu();
  });
  document.addEventListener('click', (e) => {
    const menu = $('#account-menu');
    if (menu && !menu.classList.contains('hidden') &&
        !menu.contains(e.target) && !$('#account-btn').contains(e.target)) {
      toggleAccountMenu(false);
    }
  });

  $('#dl-run').addEventListener('click', runDownload);
  $('#dl-stop').addEventListener('click', stopTasks);
  $('#dl-clear').addEventListener('click', clearTasks);
  $('#dl-retry-failed').addEventListener('click', retryFailedTasks);
  // 任务页: 四栏 / 缩略图 / 排序
  $$('#task-filters .filter-btn').forEach(b => b.addEventListener('click', () => setTaskFilter(b.dataset.f)));
  $('#task-show-thumb').addEventListener('click', () => {
    $('#task-show-thumb').classList.toggle('active');
    toggleTaskThumb();
  });
  $('#task-order').addEventListener('click', toggleTaskOrder);

  $('#lib-backup').addEventListener('click', openBackupCenter);
  $('#lib-tidy-btn').addEventListener('click', openTidyPreview);
  $('#lib-search-btn').addEventListener('click', searchLibrary);
  $('#lib-search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchLibrary(); });
  // 图库搜索框小叉叉: 清空文本并回到第 1 页 (等同全部作品)
  attachInputClear($('#lib-search-input'), () => loadLibrary());
  $('#lib-recent-btn').addEventListener('click', loadLibraryRecent);
  $('#lib-selfcheck-btn').addEventListener('click', runSelfCheck);
  $('#lib-src-manage').addEventListener('click', openSourceManager);
  $$('#lib-view-toggle .view-btn').forEach(b => b.addEventListener('click', () => setLibView(b.dataset.view)));
  $$('#lib-data-toggle .view-btn').forEach(b => b.addEventListener('click', () => setLibDataView(b.dataset.view)));

  // 图库筛选 / 排序 / 批量操作
  ['#lib-f-type', '#lib-f-status', '#lib-f-source', '#lib-f-r18', '#lib-f-ai', '#lib-f-sort'].forEach(sel => {
    $(sel).addEventListener('change', (e) => {
      const map = { '#lib-f-type': 'type', '#lib-f-status': 'status', '#lib-f-source': 'source', '#lib-f-r18': 'r18', '#lib-f-ai': 'ai', '#lib-f-sort': 'sort' };
      libFilters[map[sel]] = e.target.value;
      loadLibrary();
    });
  });
  // 启动时把排序下拉同步为**真实状态** (HTML 里的 selected 只是初始显示;
  // 曾出现 HTML 默认项与 libFilters.sort 不一致 → 下拉显示「按入库」实际按「修改时间」查询)
  $('#lib-f-sort').value = libFilters.sort;
  $('#lib-f-order').textContent = libFilters.order === 'desc' ? '↓ 降序' : '↑ 升序';
  $('#lib-f-order').addEventListener('click', () => {
    libFilters.order = libFilters.order === 'desc' ? 'asc' : 'desc';
    $('#lib-f-order').textContent = libFilters.order === 'desc' ? '↓ 降序' : '↑ 升序';
    loadLibrary();
  });
  // toggle 图标按钮: 「缺元数据」「缺失文件」互斥切换 (再点一次取消)
  $('#lib-f-missing').addEventListener('click', () => {
    setLibMissingMode($('#lib-f-missing').classList.contains('active') ? '' : 'metadata');
  });
  $('#lib-sel-all').addEventListener('click', () => {
    const on = $('#lib-sel-all').classList.toggle('active');
    $$('#lib-result input[data-key]').forEach(cb => {
      cb.checked = on;
      if (on) libSelected.add(String(cb.dataset.key));
    });
    if (!on) libSelected.clear();
    syncLibSelUI();
  });
  $('#lib-sel-clear').addEventListener('click', () => {
    libSelected.clear();
    _libAnchor = null;
    $$('#lib-result input[data-key]').forEach(cb => { cb.checked = false; });
    syncLibSelUI();
  });
  $('#lib-batch-del').addEventListener('click', libBatchDelete);
  $('#lib-batch-edit').addEventListener('click', openBatchEdit);
  $('#lib-batch-merge').addEventListener('click', openMergeImages);
  $('#lib-batch-split').addEventListener('click', openSplitImages);
  $('#lib-batch-srcsearch').addEventListener('click', batchSourceSearch);
  $('#lib-batch-addcart').addEventListener('click', libBatchAddCart);
  $('#lib-batch-repair').addEventListener('click', libBatchRepair);

  // 作者管理页
  $('#authors-search-btn').addEventListener('click', () => loadAuthors());
  $('#authors-search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadAuthors(); });
  // 作者搜索框小叉叉: 清空文本后重新加载 (状态/来源筛选保留)
  attachInputClear($('#authors-search-input'), () => loadAuthors());
  ['#authors-f-status', '#authors-f-source'].forEach(sel => {
    $(sel).addEventListener('change', () => loadAuthors());
  });

  $('#reverse-run').addEventListener('click', () => runReverse(false));
  $('#reverse-run-sel').addEventListener('click', () => runReverse(true));
  // 识别不出来的图的出路: 手动入库 / 丢弃 
  $('#reverse-adopt-sel').addEventListener('click', () => revAdopt(null));
  $('#reverse-drop-sel').addEventListener('click', () => revDiscard(null));
  $('#reverse-upload').addEventListener('click', () => $('#reverse-file-input').click());
  $('#reverse-clear').addEventListener('click', async () => {
    if (!confirm('确认清空待反搜区？\n\n该目录下所有图片（含 not_found 子目录）会被送进 **Windows 电脑回收站**，之后仍可从那里还原。')) return;
    try {
      const r = await api('/api/reverse/clear', { method: 'POST' });
      toast(`已清空待反搜区（${r.removed} 张进电脑回收站）`, 'ok');
      loadReverseStatus();
    } catch (e) { toast('清空失败: ' + e.message, 'err'); }
  });
  $('#reverse-clear-result').addEventListener('click', async () => {
    try {
      await api('/api/reverse/clear-result', { method: 'POST' });
      toast('已清空最近一次反搜结果', 'ok');
      clearReverseTasks();
    } catch (e) { toast('清空失败: ' + e.message, 'err'); }
  });
  // 反搜任务表: 分栏筛选 + 清空 (下载页式任务视图)
  $$('#rev-task-filters .filter-btn').forEach(b => b.addEventListener('click', () => {
    revTaskFilter = b.dataset.f;
    $$('#rev-task-filters .filter-btn').forEach(x => x.classList.toggle('active', x === b));
    loadReverseStatus();
  }));
  $('#rev-task-clear').addEventListener('click', () => { clearReverseTasks(); toast('已清空任务列表（只清这张列表，不动文件与图库记录）', 'ok'); });
  $('#rev-task-clear-failed').addEventListener('click', clearReverseFailedTasks);
  $('#reverse-file-input').addEventListener('change', e => uploadReverseImages(e.target));
  $('#reverse-check-all').addEventListener('change', e => {
    document.querySelectorAll('#reverse-pending-grid .rev-pick')
      .forEach(cb => { cb.checked = e.target.checked; });
    updateReverseSelCount();
  });

  // 设置页账户卡片按钮
  $('#ac-login').addEventListener('click', startLogin);
  $('#ac-logout').addEventListener('click', doLogout);
  $('#ac-cancel-login').addEventListener('click', cancelLogin);
  $('#ac-show-browser').addEventListener('click', showBrowser);
  $('#ac-hide-browser').addEventListener('click', hideBrowser);
  // 缩略图缓存管理 (设置页)
  $('#thumbs-warm-btn').addEventListener('click', warmThumbs);
  $('#thumbs-orphan-btn').addEventListener('click', clearThumbsOrphans);
  $('#thumbs-clear-btn').addEventListener('click', clearThumbs);
  // 安全模式 (设置页): R18 打码开关
  $('#safemode-toggle').addEventListener('click', toggleSafemode);
  // 下载后自动收藏 (设置页): 开关 + 公开/非公开
  $('#autobook-toggle').addEventListener('click', toggleAutoBookmark);
  $('#autobook-vis').addEventListener('change', function () {
    setBookmarkVisibility(this.value);
  });
  // 启动与托盘 (设置页): 事件委托, 几个开关共用一个处理器
  const _lz = $('#launcher-switches');
  if (_lz) _lz.addEventListener('click', e => {
    const b = e.target.closest('button[data-k]');
    if (b) toggleLauncher(b.dataset.k);
  });
  $('#top-quit-btn').addEventListener('click', quitApp);

  // 图库同步按钮 + 「仅看缺失文件」筛选 (走后端, 与缺元数据 AND)
  $('#lib-sync-btn').addEventListener('click', () => libSync(true));
  // 🔍 本地以图搜图 (ORB+颜色): 上传一张图 → 图库相似结果弹窗
  $('#lib-similar-btn').addEventListener('click', () => $('#lib-similar-file').click());
  $('#lib-similar-file').addEventListener('change', async function () {
    const file = this.files && this.files[0];
    if (!file) return;
    openLibModal('🔍 以图搜图', '<div class="hint">正在检索…（首次使用需先构建特征库：扫描全库图片提取特征，可能需要几分钟）</div>');
    const fd = new FormData();
    fd.append('image', file);
    try {
      const r = await fetch('/api/library/similar', { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok || data.status === 'error') throw new Error(data.message || ('HTTP ' + r.status));
      const items = data.items || [];
      if (!items.length) {
        $('#lib-modal-body').innerHTML = '<div class="hint">没有找到相似图片（特征库为空或图库图片过少）。</div>';
        return;
      }
      $('#lib-modal-body').innerHTML =
        `<div class="hint" style="margin-bottom:8px">找到 ${items.length} 张相似图片（按相似度降序），点卡片在图库中定位该作品：</div>
         <div class="works-grid">` + items.map(it => `
        <div class="work-card" style="cursor:pointer"
             onclick="closeLibModal();switchTab('library');$('#lib-search-input').value='${esc(it.artwork_id || '')}';loadLibrary()">
          <img class="thumb" src="${esc(safeThumb(it.thumb, it))}" alt="" onerror="this.style.visibility='hidden'">
          <div class="w-body">
            <div class="w-title">${esc(it.title || '（无标题）')}</div>
            <div class="w-author">${esc(it.author || '')} · ${esc(it.artwork_id || '')}</div>
            <div class="w-meta">相似度 ${(it.fusion_score * 100).toFixed(1)}%</div>
            <div class="w-path" title="${esc(it.rel)}">📁 ${esc(it.rel)}</div>
          </div>
        </div>`).join('') + `</div>`;
    } catch (e) {
      $('#lib-modal-body').innerHTML = `<div class="hint">以图搜图失败：${esc(e.message)}</div>`;
    }
    this.value = '';
  });
  // 📁 从本地文件夹导入 (移动文件)
  $('#lib-import-btn').addEventListener('click', () => importLocalImages('folder'));
  const importFilesBtn = $('#lib-import-files-btn');
  if (importFilesBtn) importFilesBtn.addEventListener('click', () => importLocalImages('files'));
  $('#lib-f-missing-file').addEventListener('click', () => {
    setLibMissingMode($('#lib-f-missing-file').classList.contains('active') ? '' : 'file');
  });
  $('#lib-f-deleted').addEventListener('click', () => {
    setLibMissingMode($('#lib-f-deleted').classList.contains('active') ? '' : 'deleted');
  });

  // 拖拽框选 (网格视图): 按住左键在卡片上拖出一个矩形, 框住即选中
  // 支持与已选中集合并集 (不配合 Ctrl 时从空集开始, 配合 Ctrl 追加到现有选择)
  initBoxSelect($('#lib-result'));

  // ---- 卡片缩略图交互: 单击切换选中, 双击打开大图预览, Shift+点击范围多选 ----
  (function() {
    let _cardClickTimer = null;
    let _clickedCard = null;
    const container = $('#lib-result');
    const cancelCardTimer = () => {
      if (_cardClickTimer) { clearTimeout(_cardClickTimer); _cardClickTimer = null; _clickedCard = null; }
    };

    container.addEventListener('click', (e) => {
      // 拖拽框选刚结束的这次 click 直接忽略 (mouseup 落在卡片上会再补发一次 click)
      if (container.dataset.boxDragged === '1') { delete container.dataset.boxDragged; return; }

      // 复选框点击 (含 Shift 范围多选): change 事件没有 shiftKey, 统一在这里用真实 click 事件处理
      const selWrap = e.target.closest('.card-sel');
      if (selWrap) {
        cancelCardTimer();
        const selCb = selWrap.querySelector('input[data-key]');
        if (selCb) {
          if (e.shiftKey) selCb.checked = true;   // Shift 按下: 本次点击也纳入范围, 不按原生 toggle
          libToggleSel(selCb, e);
        }
        return;
      }

      // 图片点击
      const thumb = e.target.closest('.thumb');
      if (!thumb) return;
      // 点击交互元素时不触发 (checkbox/按钮/链接/路径)
      if (e.target.closest('input, button, a, .w-actions, .w-path, .cell-ops, .cell-path')) return;

      const card = thumb.closest('.work-card, tr');
      if (!card) return;
      const key = card.dataset.key;               // 作品视图=作品ID; 图片视图=作品|页码 (即选中主键)
      const cb = card.querySelector('input[data-key]');

      // 有复选框的视图 (作品/图片视图都是): Shift+点击立即范围选择, 并取消挂起的单击定时器
      if (cb && e.shiftKey) {
        cancelCardTimer();
        cb.checked = true;
        libToggleSel(cb, e);
        return;
      }

      // 双击检测: 300ms 内再次点击同一卡片 → 打开大图预览
      if (_cardClickTimer && _clickedCard === card) {
        cancelCardTimer();
        const items = (container._lastItems || []);
        const w = key ? items.find(x => x._key === key) : null;
        if (w) {
          // 作品视图: 传 pages 清单 → Lightbox 可左右翻页; 图片视图: 单张
          const pageList = (w.pages && w.pages.length) ? w.pages : null;
          // nav = 当前这一页的卡片清单 + 本卡序号 → Lightbox 右下角给一个「下个作品/下一张」入口
          openLightbox(w, pageList, { items, index: items.indexOf(w) });
        }
        return;
      }

      // 点了不同卡片 → 取消之前的悬空定时器 (避免旧卡被误切换)
      if (_cardClickTimer) clearTimeout(_cardClickTimer);

      // 单击 → 立即记录 Shift 锚点 (不等 300ms, 否则快速连续 Shift 点击会丢失锚点),
      // 延迟 300ms 再切换选中 (延迟是为了区分双击); 搜索页等无复选框的卡片单击不做事
      if (cb) _libAnchor = String(cb.dataset.key);
      _cardClickTimer = setTimeout(() => {
        _cardClickTimer = null;
        _clickedCard = null;
        if (cb) {
          cb.checked = !cb.checked;
          libToggleSel(cb, e);
        }
      }, 300);
      _clickedCard = card;
    });
  })();

  // ---- 🔍 点图 1:1 放大 + 拖动平移 (本地图库查看器与远程大图弹窗共用) ----
  // 两处所需行为完全一样, 抽成一支装上, 免得改了这边忘了那边 
  // 「关于点击查看大图的优化也应该适用到本地图库里面」)。调用方要保证:
  //   ① HTML 里放一个 span.lb-zoom-hint 当放大提示位 (缺了也不报错, 只是没提示);
  //   ② CSS 的 .lightbox-img-wrap.is-zoom 规则生效 —— 放大态由 CSS 把**图片元素自身**撑到
  //      自然尺寸, 于是「视口」= wrap 的 overflow:hidden, translate 才是移动视口。只写
  //      object-fit: none 是不够的: 元素盒子仍是容器尺寸, 拖动就只是把一个方块拖着走。
  //   sizeWrapFn: 调用方自己的重排 —— 按 isZoom() 取宽高额度 (放大态放开上限), 写入 wrap 的
  //               inline 尺寸, 并记下自然尺寸
  //   getNat:     取 [自然宽, 自然高] (调用方的 curNw/curNh)
  const setupZoom = (lb, wrap, lay, sizeWrapFn, getNat) => {
    const hint = lb.querySelector('.lb-zoom-hint');
    let zoom = false, zx = 0, zy = 0, panFrom = null, panMoved = false, blocked = false;
    // 平移量是「相对居中」的偏移: 上限 = (自然 - 视口)/2; 图比视口小 → 不可拖。
    // 视口必须取 wrap 的**实际渲染尺寸**, 不能拿自然尺寸去算 (那样算出来恒为 0 → 拖不动)。
    const clampAxis = (v, view, nat) => {
      const lim = Math.max(0, (nat - view) / 2);
      return Math.min(lim, Math.max(-lim, v));
    };
    const paint = () => lay.forEach((el) => {
      el.style.transform = zoom ? `translate(-50%, -50%) translate(${zx}px, ${zy}px)` : '';
    });
    // -50%/-50% 负责把自然尺寸的大图居中 (元素 transform 的百分比按自身盒解析), 平移量叠在它后面
    const applyZoom = () => {
      lb.classList.toggle('is-zoom', zoom);   // 放大态才放开 wrap / img-wrap 的宽高上限
      wrap.classList.toggle('is-zoom', zoom);
      paint();
      if (hint) hint.textContent = zoom ? '再点图片还原 · 拖动可平移' : '点击图片放大 100%';
      sizeWrapFn();      // 额度随放大态变 → 立刻重排
    };
    const zoomTo = (on) => { zoom = !!on; zx = 0; zy = 0; applyZoom(); };
    // 视口尺寸变了 (窗口缩放 / 刚切过放大态): 重排 + 按新视口重新夹一遍平移量 (旧上限已失效)
    const resync = () => {
      sizeWrapFn();
      if (!zoom) return;
      const [nw, nh] = getNat();
      zx = clampAxis(zx, wrap.clientWidth, nw);
      zy = clampAxis(zy, wrap.clientHeight, nh);
      paint();
    };
    const panStart = (ev) => {
      if (!zoom || ev.button !== 0) return;
      panFrom = { x: ev.clientX, y: ev.clientY, ox: zx, oy: zy };
      panMoved = false;
      ev.preventDefault();     // 拖图时不要顺手选中文字
    };
    const panMove = (ev) => {
      if (!panFrom) return;
      const dx = ev.clientX - panFrom.x, dy = ev.clientY - panFrom.y;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) panMoved = true;
      const [nw, nh] = getNat();
      zx = clampAxis(panFrom.ox + dx, wrap.clientWidth, nw);
      zy = clampAxis(panFrom.oy + dy, wrap.clientHeight, nh);
      paint();
    };
    const panEnd = () => { panFrom = null; };
    const onClick = (ev) => {
      // 点「🔒 显示」/ 刚拖完图 (panMoved) 都不算「点图放大」
      if (ev.target.closest('.lightbox-reveal') || panMoved) { panMoved = false; return; }
      if (blocked) return;     // 打码页 / 没有图: 占位图 1:1 只会露出一块小矩形
      zoomTo(!zoom);
    };
    wrap.addEventListener('mousedown', panStart);
    wrap.addEventListener('click', onClick);
    window.addEventListener('mousemove', panMove);
    window.addEventListener('mouseup', panEnd);
    applyZoom();     // 落一次初值 (调用方还没自然尺寸时, 它自己的 sizeWrap 会早退)
    return {
      isZoom: () => zoom,
      zoomTo,
      resync,
      // 打码页(安全模式未解锁) / 没有图可放: 禁放大并收起提示; 已放大则先还原
      setBlocked: (b) => {
        blocked = !!b;
        if (hint) hint.classList.toggle('hidden', blocked);
        if (blocked && zoom) zoomTo(false);
      },
      teardown: () => {
        wrap.removeEventListener('mousedown', panStart);
        wrap.removeEventListener('click', onClick);
        window.removeEventListener('mousemove', panMove);
        window.removeEventListener('mouseup', panEnd);
      },
    };
  };

  // ---- 轻量级大图预览 (Lightbox): 双叠层 + 隐藏层加载完成才淡入, 零空白帧 ----
  // 父容器尺寸由 JS 按图片自然比例显式设定(叠层 absolute 不撑开父容器), 缩略图/原图
  // 同画幅 → 换层只有透明度过渡, 无布局跳变、无闪烁; 可见层绝不中途改 src。
  window.openLightbox = function (w, pageList, nav) {
    const old = document.getElementById('lightbox');
    if (old) old.remove();

    // pageList: 作品视图传该作品的库内页清单 (每页 {page_num,url,...}), 可翻页;
    // 图片视图/搜索页不传 → 只有当前这一张
    // nav: {items, index} = 图库当前这一页的卡片清单 + 本卡序号。给了它就在右下角多一个
    //      「下个作品 / 下一张」按钮: 作品视图切到下一个作品, 图片视图切到下一张图片
    //。
    let cur = w;
    let pages = (Array.isArray(pageList) && pageList.length) ? pageList : [w];
    const navItems = (nav && Array.isArray(nav.items) && nav.items.length) ? nav.items : null;
    let navIdx = (nav && Number.isInteger(nav.index) && nav.index >= 0) ? nav.index : -1;
    let idx = 0, seq = 0, vis = 0, curNw = 0, curNh = 0;

    const lb = document.createElement('div');
    lb.id = 'lightbox';
    lb.className = 'lightbox';
    lb.innerHTML = '<div class="lightbox-bg"></div>'
      + '<div class="lightbox-wrap">'
      + '<button class="lightbox-close" title="关闭 (Esc)">✕</button>'
      // 右下角跨卡片按钮组 (上个/下个); 无 navItems(搜索页) 时不建
      + (navItems ? '<div class="lightbox-nav-items">'
        + '<button class="lightbox-prev-item" type="button"></button>'
        + '<button class="lightbox-next-item" type="button"></button></div>' : '')
      // 左右翻页按钮**总是**建出来 (只有多页作品才显示): 跨卡片切换时当前卡可能是单张,
      // 若按需增删元素就得在建/删之间来回折腾, 交给 showPage 按 pages.length 切显隐更省事
      + '<button class="lightbox-prev" title="上一张 (←)">‹</button>'
      + '<button class="lightbox-next" title="下一张 (→)">›</button>'
      + '<div class="lightbox-img-wrap">'
      + '<div class="lb-spinner"></div>'
      + '<img class="lightbox-img lb-layer" alt="">'
      + '<img class="lightbox-img lb-layer" alt="">'
      + '<button class="lightbox-reveal hidden" title="查看原图">🔒 显示</button>'
      + '<span class="lb-zoom-hint"></span>'
      + '</div>'
      + '<div class="lightbox-info">'
      + '<span class="lb-title"></span>'
      + '<span class="lb-meta"></span>'
      + '<span class="lb-file"></span>'
      + '<span class="lb-nav"></span>'
      + '<button class="lb-reorder-btn" type="button" title="调整这个作品内的页序">↔ 调整页序</button>'
      + '</div>'
      + '<div class="lb-strip hidden">'
      + '<div class="lb-strip-hint">点 ◀ ▶ 调整页序（第 N 张即浏览顺序），改完点「完成」；文件会同步重命名</div>'
      + '<div class="lb-strip-list"></div>'
      + '<button class="lb-strip-done" type="button">完成</button>'
      + '</div></div>';
    document.body.appendChild(lb);

    const wrap = lb.querySelector('.lightbox-img-wrap');
    const lay = lb.querySelectorAll('.lightbox-img.lb-layer');
    const spinner = lb.querySelector('.lb-spinner');
    const revealBtn = lb.querySelector('.lightbox-reveal');
    const lbTitle = lb.querySelector('.lb-title');
    const lbMeta = lb.querySelector('.lb-meta');
    const lbFile = lb.querySelector('.lb-file');
    const lbInfo = lb.querySelector('.lightbox-info');   // 量信息行实测高度, 好把放大态竖屏额度算准
    const lbNav = lb.querySelector('.lb-nav');
    const btnPrev = lb.querySelector('.lightbox-prev');
    const btnNext = lb.querySelector('.lightbox-next');
    const prevItemBtn = lb.querySelector('.lightbox-prev-item');
    const nextItemBtn = lb.querySelector('.lightbox-next-item');

    // 右下角跨卡片按钮的文案/提示: 图片视图(主键带「|」)= 上一张/下一张图,
    // 作品视图 = 上个/下个作品。每次换卡片都要重画 —— 标题与「第 N / 共 M 个」都得跟着走。
    const refreshNavBtn = () => {
      if (!nextItemBtn) return;
      const isImageCard = String(cur._key || '').indexOf('|') >= 0;
      const pos = '（当前第 ' + (navIdx + 1) + ' / ' + navItems.length + ' 个，到头会绕回另一端）';
      nextItemBtn.textContent = isImageCard ? '下一张 ▸' : '下个作品 ▸';
      nextItemBtn.title = '切到下一个卡片' + pos + ' · 快捷键 PageDown';
      if (prevItemBtn) {
        prevItemBtn.textContent = isImageCard ? '◂ 上一张' : '◂ 上个作品';
        prevItemBtn.title = '切到上一个卡片' + pos + ' · 快捷键 PageUp';
      }
    };
    // 「↔ 调整页序」是否可用: 只对本地库内、多页、页码齐全且唯一的卡片开放 (远端搜索结果/单张不开放)。
    // 写成函数而非一次性常量 —— 跨卡片切换后页清单与作品 ID 都变了, 每次显示都要重算。
    const canReorderNow = () => {
      const s = new Set(pages.map(p => p.page_num));
      return pages.length >= 2
        && pages.every(p => p.page_num != null && s.size === pages.length
          && String(p.url || '').startsWith('/api/'))
        && !!cur.artwork_id;
    };

    // 该页的缩略图 URL(默认已带 thumb=1, 浏览器缓存/480px 秒开) 与 原图 URL
    // 安全模式 + R18: **不返回真实地址**, 一律给占位图 —— 浏览器从不请求 R18 像素;
    // 用户在弹窗里点「 显示」才把该张标记为已解锁 (pg._reveal), 此时才去加载原图。
    const urlsOf = (pg) => {
      if (safemode && (pg.is_r18 || (pages[0] && pages[0].is_r18 || cur.is_r18))
          && !pg._reveal) return { t: R18_MASK_SVG, f: R18_MASK_SVG };
      const raw = (pg.url || (pg.image_urls || [])[0] || '').trim();
      if (!raw) return { t: '', f: '' };
      if (raw.startsWith('/api/')) {
        const f = raw.replace(/[?&]thumb=1/g, '');
        const t = (f.indexOf('?') >= 0 ? f + '&' : f + '?') + 'thumb=1';
        return { t, f };
      }
      return { t: raw, f: raw };   // 远端图: 没有本地缩略图, 原图即缩略图
    };

    // 原图就绪 → 更新「宽×高 · 大小」信息行。
    // 两点讲究 :
    //   ① 只算一次字符串、**值没变不碰 DOM** —— 解码回调算出的尺寸常与库内已记录的一致,
    //      旧写法每次都先清空再重填, 于是每翻一页这行字都闪一下;
    //   ② 只取本页已知的字段拼装: 库内**没有**宽高的页(全库 318 页, 多为非首页)以前会先
    //      清成空白、等原图解码后再冒出尺寸 —— 现在传进 pg 就能立刻显示已知的大小,
    //      不再有「先消失再出现」的空窗。
    const updateDim = (w_, h_, pg) => {
      let txt = (w_ && h_) ? w_ + '×' + h_ : '';
      if (pg && pg.file_size) txt += (txt ? ' · ' : '') + fmtSize(pg.file_size);
      if (txt !== lbFile.textContent) lbFile.textContent = txt;
    };

    // 按图片自然比例把父容器设定为「适配视口后的像素尺寸」→ 换层时容器零变化
    let z = null;   // setupZoom 的句柄 (在下面装上): 只有它知道当前是不是放大态
    const sizeWrap = (nw, nh) => {
      if (!nw || !nh) return;
      const zin = !!(z && z.isZoom());
      const capNorm = window.innerHeight * 0.85;
      const capZoom = Math.max(capNorm, window.innerHeight * 0.94
        - (lbInfo ? lbInfo.offsetHeight : 0) - lb.querySelector('.lb-strip').offsetHeight - 10);
      if (zin) {
        // 放大态: wrap 只是**取景窗** (图片元素自身已是自然尺寸, 溢出由 overflow:hidden 裁掉),
        // 所以不再跟图片比例走 —— 直接铺满可用区域 
        // 比例稍微向外撑了一些, 我要的是直接铺满屏幕」)。竖向扣掉信息行与页序条的**实测**
        // 高度, 下方那两行与 ✕ / ‹ › 才不会被顶出视野。
        wrap.style.width = Math.max(1, Math.round(window.innerWidth * 0.98)) + 'px';
        wrap.style.height = Math.max(1, Math.round(capZoom)) + 'px';
      } else {
        // 常态: 按图片比例「适配」进视口 (contain) 且不放大超过原尺寸 —— 换层时容器零变化
        const r = Math.min(window.innerWidth * 0.92 / nw, capNorm / nh, 1);
        wrap.style.width = Math.max(1, Math.round(nw * r)) + 'px';
        wrap.style.height = Math.max(1, Math.round(nh * r)) + 'px';
      }
      curNw = nw; curNh = nh;
    };
    // 窗口尺寸变化: 重排 + (放大态) 按新视口重新夹一遍平移量
    const onWinResize = () => { if (curNw && curNh) z.resync(); };
    window.addEventListener('resize', onWinResize);
    // 🔍 点图 1:1 放大 + 拖动平移 (与远程大图弹窗共用同一支; 2026-10-20 需求下沉到本地图库)
    z = setupZoom(lb, wrap, lay, () => sizeWrap(curNw, curNh), () => [curNw, curNh]);

    // 把 url 放到「当前隐藏层」, 后台 Image 加载+decode 完成后才在隐藏层落 src,
    // 再淡入新层、淡出旧层 —— 可见层从不中途改 src, 全程无空白帧。
    // st: showPage 每次调用独立的 {fullShown} 状态; isFull: 是否为原图(缩略图晚到则丢弃)。
    const swapLayer = (url, token, st, isFull, done) => {
      if (!url) { if (done) done(null); return; }
      const pre = new Image();
      const render = () => {
        if (token !== seq) return;
        const hid = 1 - vis;   // 渲染时再取「当前隐藏层」
        const im = lay[hid];
        if (im.getAttribute('src') !== url) im.src = url;   // 解码缓存命中, 隐藏层瞬间有内容
        const showIt = () => {
          if (token !== seq || im.getAttribute('src') !== url) return;
          if (isFull) st.fullShown = true;
          else if (st.fullShown) return;      // 缩略图后到但原图已显示 → 丢弃, 防模糊覆盖清晰
          im.classList.add('show');
          lay[vis].classList.remove('show');
          vis = hid;
          spinner.style.display = 'none';
          if (done) done(im);
          window.setTimeout(() => {
            if (token === seq && !lay[1 - vis].classList.contains('show')
                && lay[1 - vis].getAttribute('src')) lay[1 - vis].removeAttribute('src');
          }, 300);
        };
        if (im.complete) showIt();
        else { im.onload = showIt; im.onerror = () => { if (token === seq) showIt(); }; }
      };
      pre.onload = () => {
        if (token !== seq) return;
        Promise.resolve(pre.decode ? pre.decode() : undefined).catch(() => {}).then(render);
      };
      pre.onerror = render;   // 加载失败也尽力渲染, 绝不留白
      pre.src = url;
    };

    function showPage(i) {
      const prevIdx = idx;
      idx = (i + pages.length) % pages.length;
      const token = ++seq;
      const pg = Object.assign({}, cur, pages[idx]);   // 页面字段覆盖作品字段
      const st = { fullShown: false };
      // 安全模式提示跟随当前开关状态实时变化 (已解锁的那张不再打码)
      const needBlur = !!(safemode && pg.is_r18 && !pg._reveal);
      lay.forEach(el => el.classList.toggle('blur-r18', needBlur));
      revealBtn.classList.toggle('hidden', !needBlur);
      z.setBlocked(needBlur);   // 打码页不给放大 (占位图 1:1 放大只会露出一块小矩形)
      lbTitle.textContent = pg.title || '（无标题）';
      lbMeta.textContent = (pg.author_name || pg.author_id || '未知作者') + ' · #' + pg.artwork_id
        + (pg.is_r18 ? (safemode ? ' · R18（安全模式已打码）' : ' · R18') : '');
      lbNav.textContent = pages.length > 1 ? (idx + 1) + ' / ' + pages.length : '';
      if (btnPrev) btnPrev.style.display = pages.length > 1 ? '' : 'none';
      if (btnNext) btnNext.style.display = pages.length > 1 ? '' : 'none';
      // 「↔ 调整页序」与右下角「下个作品/下一张」都随当前卡片重算: 换卡片后页清单、
      // 作品 ID 都变了, 一次算好显示态, 免得点了按钮操作到上一张卡片的页
      if (reorderBtn) reorderBtn.classList.toggle('hidden', !canReorderNow());
      refreshNavBtn();
      updateDim(pg.width, pg.height, pg);
      if (pg.width && pg.height) sizeWrap(pg.width, pg.height);

      const { t, f } = urlsOf(pg);
      const fitFrom = (im) => { if (im && im.naturalWidth) sizeWrap(im.naturalWidth, im.naturalHeight); };
      // 缩略图(缓存秒开)先淡入垫底; 原图解码后在隐藏层就绪再淡入 → 全程无空白
      swapLayer(t, token, st, false, fitFrom);
      swapLayer(f, token, st, true, (im) => {
        fitFrom(im);
        updateDim(im ? im.naturalWidth : pg.width, im ? im.naturalHeight : pg.height, pg);
      });
      // 这里**不再**预取相邻页原图。
      // 原写法一次翻页就悄悄下两张 img-original (各 1~9MB), 既费流量, 又跟当前这张抢同一
      // 域名的 6 条并发连接 —— 看起来就是「连低清都要等很久」。
      // 换页才收缩放; 同一页里的「缩略图 ↔ 原图」切换保留放大状态 —— 那正是要 1:1 看细节的场合
      if (z.isZoom() && idx !== prevIdx) z.zoomTo(false);
    }

    // 安全模式: 点「🔒 显示」解锁**当前这一张** (仅本次, 不取消安全模式)
    // 解锁 = 允许去加载这张的真实原图 (在此之前浏览器从未请求过它的像素)
    revealBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (pages[idx]) pages[idx]._reveal = true;
      showPage(idx);
    });

    if (btnPrev) btnPrev.addEventListener('click', (ev) => { ev.stopPropagation(); showPage(idx - 1); });
    if (btnNext) btnNext.addEventListener('click', (ev) => { ev.stopPropagation(); showPage(idx + 1); });

    // ---- ↔ 页序调整  ----
    // 可用性判据见 canReorderNow (跨卡片切换时由 showPage 重算显隐), 这里只管取元素。
    const reorderBtn = lb.querySelector('.lb-reorder-btn');
    const strip = lb.querySelector('.lb-strip');
    const stripList = lb.querySelector('.lb-strip-list');
    const stripDone = lb.querySelector('.lb-strip-done');
    // 页码空洞 = 「原网页该有的页(0..total_pages-1)」里本地缺的那些 (删过页/认领后留洞)。
    // 有洞时服务端会拒绝整体调序 order(那会把洞悄悄压实、完整性跟着变), 所以:
    //   · ◀▶ 改发 assign(只交换这两页的页号, 洞与其它页一个都不动);
    //   · 附加页多一个「↳ 填 pN」按钮, 把空位真正补上。
    const holesOf = () => {
      const have = new Set(pages.map(p => Number(p.page_num)));
      const tp = Number(cur.total_pages) || 0;   // 按当前卡片算, 换卡片后页数可能不同
      let hi = tp > 0 ? tp : 0;
      if (!hi) have.forEach(n => { hi = Math.max(hi, n + 1); });
      const out = [];
      for (let n = 0; n < hi; n++) if (!have.has(n)) out.push(n);
      return out;
    };
    let reorderBusy = false;
    const renderStrip = () => {
      // R18 打码与大图同一规则: 安全模式开启时页序条里也是占位图 (走 urlsOf, 不请求真实像素)
      const stripBlur = safemode && cur.is_r18 ? ' blur-r18' : '';
      const hole = holesOf()[0];
      stripList.innerHTML = pages.map((pg, i) => {
        const isExtra = pageIsExtra(cur, pg);
        const extra = isExtra ? '<span class="lb-strip-tag">附加</span>' : '';
        // 只有「附加页」(页号超出原网页页数) 能填洞 —— 原网页那几页不该被改号,
        // 否则等于伪造 pixiv 的页序。判据与卡片一致: 看**位置**, 不看当初的记录。
        const fill = (hole != null && isExtra)
          ? `<button type="button" data-act="fill" title="把这张附加页改成空出来的第 p${hole} 页（其余页号不动）">↳ 填 p${hole}</button>`
          : '';
        return `<div class="lb-strip-item${i === idx ? ' cur' : ''}" data-i="${i}" data-pn="${esc(pg.page_num)}">`
          + `<img class="lb-strip-thumb${stripBlur}" src="${esq(urlsOf(pg).t)}" alt="" loading="lazy" decoding="async">`
          + `<div class="lb-strip-meta">第 ${i + 1} 张 · p${pg.page_num}${extra}</div>`
          + '<div class="lb-strip-ops">'
          + `<button type="button" data-act="prev" title="前移一位"${i === 0 ? ' disabled' : ''}>◀</button>`
          + `<button type="button" data-act="next" title="后移一位"${i === pages.length - 1 ? ' disabled' : ''}>▶</button>`
          + fill
          + '</div></div>';
      }).join('')
        // 有洞时把「洞」也显式列出来, 用户才知道「填 pN」到底在填哪儿
        + (hole != null
          ? `<div class="lb-strip-gap" title="原网页第 p${hole} 页本地缺失（被删了 / 还没补）">`
            + `🕳 空位 · p${hole}${holesOf().length > 1 ? ` （另有 ${holesOf().length - 1} 个）` : ''}`
            + '</div>'
          : '');
    };
    // 重排成功后**从服务端重取该作品的页清单**再重建页序。
    // 不用前端猜文件名: 文件名不变、内容互换是页序调整的常态(第 N 张 = p{N}),
    // 而服务端 URL 带内容版本令牌 (?,v=...), 只有它才能保证浏览器不吃 30 天旧缓存
    // —— 这就是"调了却没调 / 封面还是那样"的根因。
    const refetchPages = async () => {
      const rr = await api('/api/library/items?q=&only='
        + encodeURIComponent(String(cur.artwork_id))
        + '&r18=&view=artwork&limit=1&offset=0');
      const it = (rr.items || [])[0];
      if (!it || !Array.isArray(it.pages) || !it.pages.length) {
        throw new Error('服务端没有返回该作品的页清单');
      }
      pages.splice(0, pages.length, ...it.pages);
    };
    {
      reorderBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        strip.classList.toggle('hidden');
        // 调整页序时收起右下角「上个作品 / 下个作品」: 它俩 fixed 在视口右下角, 展开页序条后
        // 正好压住「完成」按钮。退出调整页序时恢复 (见下三处)。
        lb.classList.toggle('is-reordering', !strip.classList.contains('hidden'));
        if (!strip.classList.contains('hidden')) renderStrip();
      });
      stripDone.addEventListener('click', (ev) => {
        ev.stopPropagation();
        strip.classList.add('hidden');
        lb.classList.remove('is-reordering');
      });
      strip.addEventListener('click', (ev) => ev.stopPropagation());
      stripList.addEventListener('click', async (ev) => {
        const btn = ev.target.closest('button[data-act]');
        if (!btn || btn.disabled || reorderBusy) return;
        const act = btn.dataset.act;
        const i = Number(btn.closest('.lb-strip-item').dataset.i);
        const holes = holesOf();
        const numOf = pages.map(p => Number(p.page_num));
        // 该走哪条路? **只有"页码正好是 0..n-1"才允许整体排列 order**(服务端就是这么要求的),
        // 否则(有洞 / 带超出原页数的附加页 → 页号像 p0,p1,p4)一律用点对点改号 assign。
        // 以前是用"tp 范围内有没有洞"判断的 → 「带附加页但没有洞」的作品被判成能走 order,
        // 服务端一句"页号有空缺"就 400, 用户点了像没反应。
        const consecutive = numOf.every((n, k) => n === k);
        const newNum = new Map(numOf.map(n => [n, n]));   // 旧页号 → 新页号 (只记被改动的)
        let body, tip = '✅ 页序已调整';
        let flashPns = [];                 // 换完要闪一下的页 (让"换了哪几格"看得见)
        if (act === 'fill') {
          // 把这张附加页改号去填空位 (只动这一页; 其它页号与其它洞都不受影响)
          const target = holes[0];
          if (target == null) return;
          newNum.set(numOf[i], target);
          body = { assign: [{ old: numOf[i], new: target }] };
          tip = `✅ 已把附加页 p${numOf[i]} 填进空位 p${target}`;
          flashPns = [target];
        } else {
          const j = i + (act === 'prev' ? -1 : 1);
          if (j < 0 || j >= pages.length) return;
          newNum.set(numOf[i], numOf[j]);
          newNum.set(numOf[j], numOf[i]);
          if (!consecutive) {
            // 页码不是 0..n-1 (有洞 / 有超出原页数的附加页): 只能点对点改号,
            // 只交换这两页的页号, 洞与其它页号一个都不动
            body = { assign: [{ old: numOf[i], new: numOf[j] },
                              { old: numOf[j], new: numOf[i] }] };
            tip = `✅ 已把 p${numOf[i]} 和 p${numOf[j]} 互换页号`;
          } else {
            body = { order: numOf.slice() };
            body.order[i] = numOf[j];
            body.order[j] = numOf[i];
            // 页序条上的「第 N 张 · pM」是**按位置**生成的 (换序后页码又被压实成 0..n-1),
            // 所以光看文字永远看不出"换过" —— 必须点名"第几张换到第几张", 并闪一下这两格,
            // 否则这个按钮用起来就像"左右翻页"。
            tip = `✅ 已把「第 ${i + 1} 张」和「第 ${j + 1} 张」互换位置`;
          }
          flashPns = [numOf[i], numOf[j]];
        }
        // 让"当前正在看的那张"跟到它的新位置 (画面不跳): 记住它的新页号, 重取后按页号定位
        const followPn = newNum.get(numOf[idx]);
        reorderBusy = true;
        stripList.classList.add('busy');
        try {
          const r = await api('/api/library/reorder-pages', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artwork_id: String(cur.artwork_id), ...body }),
          });
          await refetchPages();
          const k = pages.findIndex(p => Number(p.page_num) === followPn);
          idx = k >= 0 ? k : Math.max(0, Math.min(idx, pages.length - 1));
          renderStrip();
          // 闪一下刚动过的格子 —— 不闪的话"换过了"完全看不出来(标签是按位置生成的)
          flashPns.forEach(pn => {
            const el = stripList.querySelector(`.lb-strip-item[data-pn="${pn}"]`);
            if (!el) return;
            el.classList.add('flash');
            setTimeout(() => el.classList.remove('flash'), 1300);
          });
          showPage(idx);
          toast(tip + (r.files_renamed ? `，已同步重命名 ${r.files_renamed} 个文件` : ''));
        } catch (e) {
          toast('调整页序失败：' + e.message, 'err');
        } finally {
          reorderBusy = false;
          stripList.classList.remove('busy');
        }
      });
    }

    // ---- 跨卡片浏览 : 不关弹窗、不重建 DOM, 直接换掉当前卡片。
    // 作品视图 = 切下一个作品(从它的第 1 页看起); 图片视图 = 切下一张图。
    // 走同一个 Lightbox 实例(而非重新 openLightbox): 双叠层换图机制能直接复用, 不会闪一下黑底。
    const loadCard = (i) => {
      if (!navItems) return;
      navIdx = ((i % navItems.length) + navItems.length) % navItems.length;
      cur = navItems[navIdx];
      pages = (cur.pages && cur.pages.length) ? cur.pages : [cur];
      strip.classList.add('hidden');   // 页序条属于上一张卡片, 换卡时收起
      lb.classList.remove('is-reordering');   // 同时恢复右下角两颗按钮 (见 reorderBtn 处)
      showPage(0);
    };
    if (nextItemBtn) {
      nextItemBtn.addEventListener('click', (ev) => { ev.stopPropagation(); loadCard(navIdx + 1); });
    }
    if (prevItemBtn) {
      prevItemBtn.addEventListener('click', (ev) => { ev.stopPropagation(); loadCard(navIdx - 1); });
    }

    // 点击背景或关闭按钮关闭
    lb.addEventListener('click', (e) => {
      if (e.target === lb || e.target.closest('.lightbox-close') || e.target.closest('.lightbox-bg')) {
        window.removeEventListener('resize', onWinResize);
        z.teardown();       // 摘掉放大/平移那几个 window 监听 (不摘会在关窗后继续响应鼠标)
        lb.remove();
      }
    });
    // ESC 关闭, ←/→ 在**当前作品内**翻页; PageDown/PageUp 跨卡片 (与浏览器翻页习惯一致)
    const onKey = (e) => {
      if (e.key === 'Escape') {
        window.removeEventListener('resize', onWinResize);
        z.teardown();
        lb.remove();
        document.removeEventListener('keydown', onKey);
      } else if (e.key === 'ArrowLeft') showPage(idx - 1);
      else if (e.key === 'ArrowRight') showPage(idx + 1);
      else if (e.key === 'PageDown') { e.preventDefault(); loadCard(navIdx + 1); }
      else if (e.key === 'PageUp') { e.preventDefault(); loadCard(navIdx - 1); }
    };
    document.addEventListener('keydown', onKey);
    showPage(0);
  };

  // ---- 远程大图预览 (查询页卡片: 单击/双击都开它, 2026-10-20 需求) ----
  // 刻意**不复用** openLightbox —— 它的 canReorderNow / holesOf / 页序调序全绑「库内作品」,
  // 塞进远程模式会把那个函数撑爆、动摇既有断言。这里单写一支, 数据只来自
  // /api/artwork/<id>/pages: 大图默认低清 (regular), 点「显示原图」才叠 original;
  // 大图按钮组 = 「显示原图 · 下载该页 · 🔗 原网页」(没有「上个作品/下个作品」);
  // 下方一条缩略图多选条 = 勾几页下几页, 已入库的页标 ✓ 并灰掉。
  // 缩略图多选条折叠态: 跨打开沿用上次选择, 默认收起 —— 打开大图的主目的是看图,
  // 竖向空间先还给大图 
  let lbStripFolded = true;
  window.openRemoteLightbox = async function (card) {
    const aid = String((card && card.dataset && card.dataset.aid) || '').trim();
    if (!aid) return;
    const origUrl = (card && card.dataset && card.dataset.url)
      || ('https://www.pixiv.net/artworks/' + aid);
    const old = document.getElementById('lightbox');
    if (old) old.remove();

    const lb = document.createElement('div');
    lb.id = 'lightbox';
    lb.className = 'lightbox';   // 与本地图库查看器同一套样式 (放大/平移由 setupZoom 共同装上;
    lb.innerHTML = '<div class="lightbox-bg"></div>'
      + '<div class="lightbox-wrap">'
      + '<button class="lightbox-close" title="关闭 (Esc)">✕</button>'
      + '<button class="lightbox-prev hidden" title="上一页 (←)">‹</button>'
      + '<button class="lightbox-next hidden" title="下一页 (→)">›</button>'
      + '<div class="lightbox-img-wrap">'
      + '<div class="lb-spinner"></div>'
      + '<img class="lightbox-img lb-layer" alt="">'
      + '<img class="lightbox-img lb-layer" alt="">'
      + '<button class="lightbox-reveal hidden" title="查看原图">🔒 显示</button>'
      + '<span class="lb-zoom-hint"></span>'
      + '</div>'
      + '<div class="lightbox-info">'
      + '<span class="lb-title"></span>'
      + '<span class="lb-meta"></span>'
      + '<span class="lb-file"></span>'
      + '<span class="lb-nav"></span>'
      + '<span class="lb-remote-ops">'
      + '<button class="lb-remote-hi" type="button" title="切换原图（默认低清）">🖼️ 显示原图</button>'
      + '<button class="lb-remote-dl" type="button" title="只下载当前这一页">⬇ 下载该页</button>'
      + '<a class="lb-remote-src" href="' + esc(origUrl) + '" target="_blank" rel="noopener" title="打开作品原网页">🔗 原网页</a>'
      + '</span>'
      + '</div>'
      + '<div class="lb-remote-strip lb-strip hidden"></div>'
      + '</div>';
    document.body.appendChild(lb);

    const wrap = lb.querySelector('.lightbox-img-wrap');
    const lay = lb.querySelectorAll('.lightbox-img.lb-layer');
    const spinner = lb.querySelector('.lb-spinner');
    const revealBtn = lb.querySelector('.lightbox-reveal');
    const lbTitle = lb.querySelector('.lb-title');
    const lbMeta = lb.querySelector('.lb-meta');
    const lbFile = lb.querySelector('.lb-file');
    const lbInfo = lb.querySelector('.lightbox-info');   // 量它的实测高度, 好把额度算准
    const lbNav = lb.querySelector('.lb-nav');
    const btnPrev = lb.querySelector('.lightbox-prev');
    const btnNext = lb.querySelector('.lightbox-next');
    const hiBtn = lb.querySelector('.lb-remote-hi');
    const dlBtn = lb.querySelector('.lb-remote-dl');
    const strip = lb.querySelector('.lb-remote-strip');
    let stripList = null;

    let pages = [], art = null, isR18 = false;
    let idx = 0, seq = 0, vis = 0, curNw = 0, curNh = 0;
    let hiOn = false;                 // 「显示原图」开关: 默认关 (先给低清, 省流量)
    let folded = lbStripFolded;       // 缩略图多选条是否收起
    let busy = false;
    const picked = new Set();         // 多选条里勾中的页号
    const blocked = (p) => p.status === 1 || p.status === 'in_queue';

    let z = null;   // setupZoom 的句柄 (在下面装上): 只有它知道当前是不是放大态
    const sizeWrap = (nw, nh) => {
      if (!nw || !nh) return;
      const zin = !!(z && z.isZoom());
      // 大图能吃多少: 展开多选条时下方还有信息行 + 工具行 + 缩略图 (~250px), 常态只能给 0.56 ——
      // 按本地那套会把多选条顶出视口 (弹窗整体不滚动, .lightbox-wrap 只 max-height 不 overflow);
      // 折叠后把省下的高度还给大图。
      const infoH = lbInfo ? lbInfo.offsetHeight : 0;
      const stripH = strip.offsetHeight;          // 收起的条 / 未加载时为 0
      const capNorm = window.innerHeight * (folded ? 0.72 : 0.56);
      const capZoom = Math.max(capNorm, window.innerHeight * 0.94 - infoH - stripH - 10);
      if (zin) {
        // 放大态: wrap 只是**取景窗** (图片元素自身已是自然尺寸, 溢出由 overflow:hidden 裁掉),
        // 所以不再跟图片比例走 —— 直接铺满可用区域 
        // 比例稍微向外撑了一些, 我要的是直接铺满屏幕」)。竖向扣掉信息行与工具行的**实测**高度
        // (弹窗不滚动, 多给就会把下方那行顶出视口), 下方那两行与 ✕ / ‹ › 才不会被顶出视野。
        wrap.style.width = Math.max(1, Math.round(window.innerWidth * 0.98)) + 'px';
        wrap.style.height = Math.max(1, Math.round(capZoom)) + 'px';
      } else {
        // 常态: 按图片比例「适配」进视口 (contain) 且不放大超过原尺寸
        const r = Math.min(window.innerWidth * 0.92 / nw, capNorm / nh, 1);
        wrap.style.width = Math.max(1, Math.round(nw * r)) + 'px';
        wrap.style.height = Math.max(1, Math.round(nh * r)) + 'px';
      }
      curNw = nw; curNh = nh;
    };
    // 窗口尺寸变化: 重排 + (放大态) 按新视口重新夹一遍平移量
    const onResize = () => { if (curNw && curNh) z.resync(); };
    window.addEventListener('resize', onResize);

    // 🔍 点图 1:1 放大 + 拖动平移 (与本地图库查看器共用同一支 setupZoom; 它按 isZoom() 取额度)。
    // 为什么需要: 弹窗里大图是按容器缩放的, master1200(低清) 与 img-original(原图) 在这个尺寸
    // 下都远超容器, 肉眼完全一样 —— 只有 1:1 看才比得出细节(以及 master1200 重编码的痕迹)。
    // 放大只重排**已经加载的**那一张, 不会顺手去取原图 (原图仍只在点「🖼️ 显示原图」时才请求)。
    z = setupZoom(lb, wrap, lay, () => sizeWrap(curNw, curNh), () => [curNw, curNh]);

    // 把 url 放隐藏层, 解码完成才切可见性 (与本地 Lightbox 同一套双叠层, 无空白帧)
    const swapLayer = (url, token, st, isFull, done) => {
      if (!url) { if (done) done(null); return; }
      const pre = new Image();
      const render = () => {
        if (token !== seq) return;
        const hid = 1 - vis;
        const im = lay[hid];
        if (im.getAttribute('src') !== url) im.src = url;
        const showIt = () => {
          if (token !== seq || im.getAttribute('src') !== url) return;
          if (isFull) st.fullShown = true;
          else if (st.fullShown) return;
          im.classList.add('show');
          lay[vis].classList.remove('show');
          vis = hid;
          spinner.style.display = 'none';
          if (done) done(im);
          window.setTimeout(() => {
            if (token === seq && !lay[1 - vis].classList.contains('show')
                && lay[1 - vis].getAttribute('src')) lay[1 - vis].removeAttribute('src');
          }, 300);
        };
        if (im.complete) showIt();
        else { im.onload = showIt; im.onerror = () => { if (token === seq) showIt(); }; }
      };
      pre.onload = () => {
        if (token !== seq) return;
        Promise.resolve(pre.decode ? pre.decode() : undefined).catch(() => {}).then(render);
      };
      pre.onerror = render;
      pre.src = url;
    };

    // 远端图一律走 /api/img 防盗链代理 (i.pximg.net 不带 Referer 会 403)
    const proxied = (u) => u ? imgSrc(u, {}) : '';
    // 安全模式 + R18: 未解锁当前页时**不返回真实地址**, 一律占位 (浏览器从不请求 R18 像素)
    const urlsOf = (pg) => (safemode && isR18 && !pg._reveal)
      ? { t: R18_MASK_SVG, f: R18_MASK_SVG }
      : { t: proxied(pg.low || pg.high), f: proxied(pg.high || pg.low) };
    const thumbOf = (pg) => (safemode && isR18 && !pg._reveal)
      ? R18_MASK_SVG : proxied(pg.thumb || pg.small || pg.low);

    function syncStripCur() {
      if (!stripList) return;
      stripList.querySelectorAll('.lb-strip-item').forEach((el) => {
        el.classList.toggle('cur', Number(el.dataset.i) === idx);
      });
    }

    function showPage(i) {
      if (!pages.length) return;
      const prevIdx = idx;
      idx = (i + pages.length) % pages.length;
      const token = ++seq;
      const pg = pages[idx];
      const needBlur = !!(safemode && isR18 && !pg._reveal);
      lay.forEach((el) => el.classList.toggle('blur-r18', needBlur));
      revealBtn.classList.toggle('hidden', !needBlur);
      z.setBlocked(needBlur);   // 打码页不给放大 (占位图 1:1 放大只会露出一块小矩形)
      lbTitle.textContent = (art && art.title) || '（无标题）';
      lbMeta.textContent = ((art && (art.author_name || art.author_id)) || '未知作者')
        + ' · #' + aid + (isR18 ? (safemode ? ' · R18（安全模式已打码）' : ' · R18') : '');
      lbNav.textContent = pages.length > 1 ? (idx + 1) + ' / ' + pages.length : '';
      btnPrev.classList.toggle('hidden', pages.length <= 1);
      btnNext.classList.toggle('hidden', pages.length <= 1);
      lbFile.textContent = (pg.width && pg.height) ? pg.width + '×' + pg.height : '';
      if (pg.width && pg.height) sizeWrap(pg.width, pg.height);
      const { t, f } = urlsOf(pg);
      const st = { fullShown: false };
      const fitFrom = (im) => { if (im && im.naturalWidth) sizeWrap(im.naturalWidth, im.naturalHeight); };
      spinner.style.display = '';
      swapLayer(t, token, st, false, fitFrom);
      if (hiOn) swapLayer(f, token, st, true, (im) => {
        fitFrom(im);
        if (im && im.naturalWidth) lbFile.textContent = im.naturalWidth + '×' + im.naturalHeight;
      });
      // 换页才收缩放; 同一页切「低清 ↔ 原图」保留放大状态 —— 那正是要 1:1 比清晰度的场合
      if (z.isZoom() && idx !== prevIdx) z.zoomTo(false);
      syncStripCur();
    }

    // 折叠/展开多选条: 收起只留一条工具行, 把竖向空间还给大图 (并同步重排大图尺寸)
    const applyFold = () => {
      if (!stripList) return;
      strip.classList.toggle('folded', folded);
      const b = strip.querySelector('.lb-remote-fold');
      if (b) b.textContent = folded ? '▸ 展开缩略图' : '▾ 收起缩略图';
      lbStripFolded = folded;                  // 记住本次选择, 下次打开沿用
      if (curNw && curNh) sizeWrap(curNw, curNh);
    };

    // 缩略图多选条: 勾几页 → 「下载选中页」; 点格子本身 = 切到那一页
    const renderStrip = () => {
      const cand = pages.filter((p) => !blocked(p));
      const allSel = cand.length > 0 && cand.every((p) => picked.has(p.page_num));
      const list = pages.map((pg, i) => {
        const done = pg.status === 1, queued = pg.status === 'in_queue';
        const locked = done || queued;
        return '<div class="lb-strip-item' + (i === idx ? ' cur' : '') + (locked ? ' is-done' : '')
          + '" data-i="' + i + '" data-pn="' + esc(pg.page_num) + '">'
          + '<img class="lb-strip-thumb" src="' + esq(thumbOf(pg)) + '" alt="" loading="lazy" decoding="async">'
          + '<div class="lb-strip-meta">第 ' + (i + 1) + ' 页 · p' + esc(pg.page_num)
          + (pg.is_extra ? '<span class="lb-strip-tag">附加</span>' : '') + '</div>'
          + '<label class="lb-remote-pick"><input type="checkbox" data-pn="' + esc(pg.page_num) + '"'
          + (locked ? ' disabled' : (picked.has(pg.page_num) ? ' checked' : '')) + '>'
          + '<span>' + (done ? '✓ 已入库' : (queued ? '⏳ 已加入' : '下载')) + '</span></label>'
          + '</div>';
      }).join('');
      strip.innerHTML = '<div class="lb-strip-hint">勾选要下载的页 → 「下载选中页」；已入库的页标 ✓ 并灰掉</div>'
        + '<div class="lb-remote-strip-bar">'
        + '<button class="lb-remote-fold" type="button"></button>'
        + '<label class="lb-remote-all"><input type="checkbox"' + (allSel ? ' checked' : '')
        + '><span>全选</span></label>'
        + '<span class="lb-remote-count">已选 ' + picked.size + ' 页 · 共 ' + pages.length
        + ' 页 · 未下载 ' + cand.length + ' 页</span>'
        + '<button class="lb-remote-dlsel" type="button">⬇ 下载选中页</button>'
        + '</div>'
        + '<div class="lb-strip-list">' + list + '</div>';
      strip.classList.remove('hidden');
      stripList = strip.querySelector('.lb-strip-list');
      applyFold();
    };

    // 「下载该页 / 下载选中页」共用: 走既有清单链路 (入队 → 下载引擎 → 落进图库)
    const doDownload = async (pns) => {
      if (busy) return;
      const asked = (pns || []).map((n) => pages.find((p) => p.page_num === Number(n)))
        .filter(Boolean);
      const nums = [];
      asked.forEach((pg) => {
        if (!blocked(pg) && nums.indexOf(pg.page_num) < 0) nums.push(pg.page_num);
      });
      if (!nums.length) {
        // 说清为什么点不动 (同一个作品连点第二次时, 要的是「已在清单」而不是一句空话)
        if (asked.some((pg) => pg.status === 'in_queue')) {
          toast('⏳ 该作品已在下载列表中，无需重复添加', 'info');
        } else if (asked.some((pg) => pg.status === 1)) {
          toast('✓ 该页已入库', 'info');
        } else {
          toast('没有可下载的页', 'info');
        }
        return;
      }
      busy = true;
      try {
        const r = await api('/api/cart', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            item_type: 'artwork',
            artwork_id: aid,
            author_id: (art && art.author_id) || null,
            author_name: (art && art.author_name) || null,
            title: '#' + aid + ' ' + ((art && art.title) || ''),
            page_count: (art && art.page_count) || pages.length,   // 远端总页数, 不是选中页数
            pages: nums,
          }),
        });
        const reason = (r && r.reason) || '';
        if (reason === 'in_queue') { toast('⏳ 该作品已在下载列表中，无需重复添加', 'info'); return; }
        if (reason === 'already_downloaded') { toast('✓ 该作品已入库', 'info'); return; }
        if (!r || (r.status !== 'ok' && reason !== 'added')) {
          toast('加入清单失败：' + apiErr(r), 'err'); return;
        }
        nums.forEach((n) => {
          const pg = pages.find((p) => p.page_num === n);
          if (pg) pg.status = 'in_queue';
        });
        picked.clear();
        renderStrip();
        toast('已加入清单，本次只下 ' + nums.length + ' 页 ⬇', 'ok');
        await autoStartDownload();
      } catch (e) {
        toast('加入清单失败：' + e.message, 'err');
      } finally {
        busy = false;
      }
    };

    const closeLb = () => {
      window.removeEventListener('resize', onResize);
      z.teardown();       // 摘掉放大/平移那几个 window 监听 (不摘会在关窗后继续响应鼠标)
      document.removeEventListener('keydown', onKey);
      lb.remove();
    };
    const onKey = (e) => {
      if (e.key === 'Escape') closeLb();
      else if (e.key === 'ArrowLeft') showPage(idx - 1);
      else if (e.key === 'ArrowRight') showPage(idx + 1);
    };
    document.addEventListener('keydown', onKey);
    lb.addEventListener('click', (e) => {
      if (e.target === lb || e.target.closest('.lightbox-close') || e.target.closest('.lightbox-bg')) closeLb();
    });
    revealBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (pages[idx]) pages[idx]._reveal = true;
      showPage(idx);
      renderStrip();
    });
    btnPrev.addEventListener('click', (ev) => { ev.stopPropagation(); showPage(idx - 1); });
    btnNext.addEventListener('click', (ev) => { ev.stopPropagation(); showPage(idx + 1); });
    hiBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const pg = pages[idx];
      if (safemode && isR18 && pg && !pg._reveal) {
        toast('安全模式下需先点「🔒 显示」解锁本页', 'info'); return;
      }
      if (!hiOn && !(pg && pg.high) ) {   // 没有原图地址就别空转一趟
        toast('这个作品没有提供原图地址', 'info'); return;
      }
      hiOn = !hiOn;
      hiBtn.textContent = hiOn ? '🖼️ 显示低清' : '🖼️ 显示原图';
      showPage(idx);
    });
    dlBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (pages[idx]) doDownload([pages[idx].page_num]);
    });
    strip.addEventListener('click', (ev) => {
      if (ev.target.closest('.lb-remote-fold')) { folded = !folded; applyFold(); return; }
      if (ev.target.closest('.lb-remote-all')) {
        ev.preventDefault();     // 自己算全选态, 不吃原生 label 的切换
        const cand = pages.filter((p) => !blocked(p));
        const allSel = cand.length > 0 && cand.every((p) => picked.has(p.page_num));
        cand.forEach((p) => { if (allSel) picked.delete(p.page_num); else picked.add(p.page_num); });
        renderStrip();
        return;
      }
      if (ev.target.closest('.lb-remote-dlsel')) { doDownload([...picked]); return; }
      const pick = ev.target.closest('.lb-remote-pick');
      if (pick) {
        const box = pick.querySelector('input[type="checkbox"]');
        if (!box || box.disabled) return;
        // 不让 label/checkbox 自己改状态, 一律由 picked 集合驱动 —— 否则勾选态与计数文案
        // 会各走各的 (原生的 checked 改了, 我们的 Set 没改)。
        ev.preventDefault();
        const n = Number(box.dataset.pn);
        if (picked.has(n)) picked.delete(n); else picked.add(n);
        renderStrip();
        return;
      }
      const item = ev.target.closest('.lb-strip-item');
      if (item) showPage(Number(item.dataset.i));
    });

    let data = null;
    try {
      data = await api('/api/artwork/' + encodeURIComponent(aid) + '/pages');
    } catch (e) {
      spinner.style.display = 'none';
      hiBtn.classList.add('hidden');
      dlBtn.classList.add('hidden');
      z.setBlocked(true);   // 没图可放, 别留一句「点击图片放大」
      lbFile.textContent = '加载失败：' + e.message;
      return;
    }
    // 没登录: 明确说清原因 + 只留「🔗 原网页」(不要一个空白弹窗)
    if (!data || data.status === 'need_login') {
      spinner.style.display = 'none';
      hiBtn.classList.add('hidden');
      dlBtn.classList.add('hidden');
      z.setBlocked(true);
      lbFile.textContent = (data && data.text)
        || '需要先在设置页登录 pixiv 才能查看原图 / 下载';
      return;
    }
    if (data.status !== 'ok' || !Array.isArray(data.pages) || !data.pages.length) {
      spinner.style.display = 'none';
      hiBtn.classList.add('hidden');
      dlBtn.classList.add('hidden');
      z.setBlocked(true);
      lbFile.textContent = (data && data.text) || '没有取到该作品的页列表';
      return;
    }
    art = data.artwork || {};
    isR18 = !!art.is_r18;
    pages = data.pages.map((p) => ({ ...p }));
    showPage(0);
    renderStrip();
  };

  // 定时轮询对账: 仅当停留在「图库」页时每 15s 轻量同步 (TTL 内不强制重扫),
  // 手动点「同步」/导入才强制即时对账, 避免图库大时周期全扫和缩略图加载抢磁盘
  setInterval(() => {
    const libTab = $('#tab-library');
    if (libTab && libTab.classList.contains('active')) { libSync(false, true); loadStats(); }
  }, 15000);
}

bind();
connectEvents();
refreshAuth();
refreshCart();
loadStats();
loadReverseStatus();
// 安全模式状态 (R18 打码), 影响图库/搜索渲染。链一个推荐区加载: 得等打码状态到位
// 再画推荐, 否则那批卡的 R18 打码用的是过期状态 (2026-09-20 加推荐区时接上的)。
loadSafemode().then(() => loadFeed());
loadLastSelfCheck();   // 载入上次体检报告: 页面刷新/服务重启后仍能点「📋 查看详情」看明细
loadInterruptedTasks();  // 上次被拦腰砍断的任务 → 提示是否从断点继续
loadVersionInfo();     // ℹ️ 版本与构建时间 (确认当前跑的是不是最新打包版)

// ===== 🔔 事件驱动刷新调度器  =====
//   后端每次**成功的写请求**都会登记"哪些数据域变了"并经 SSE 推来 {"type":"dirty"};
//   前端据此**只刷新对应视图** —— 图库改了刷图库/筛选项/统计, 作者改了刷作者页,
//   下载/体检跑完各自刷相关页; 页面上任何入口都不用自己记得"改完要通知谁"。
//   术语对照: 写侧登记 = event/state change 上报; 版本号 = revision; 这里 = 按域增量重算
//   (scoped incremental refresh); SSE 断线时用 /api/dirty?since= 轮询补偿 = 反熵 (anti-entropy)。
//   每个域一条「怎么把这块数据重新拉一遍」的规则 —— 后端说哪块脏了, 前端就只跑哪条。
//   非图库数据的域(设置/登录/缩略图/备份/操作记录)额外加了"当前视图开着才拉"的守卫:
//   没人看着的页面不该因为后台改动去白跑请求。
const SCOPE_REFRESH = {
  artworks: () => loadLibrary({ resetPage: false }),
  pages: () => loadLibrary({ resetPage: false }),   // 与作品列表同一视图(下面去重)
  authors: () => loadAuthors({ resetPage: false }),
  facets: () => loadLibFacets(),
  stats: () => loadStats(),
  queue: () => refreshCart(),
  check: () => loadCheckReport(),
  reverse: () => loadReverseStatus(),
  sources: () => loadLibFacets(),
  // 查询页: 不重跑 pixiv 查询, 只把每张卡上的「本地状态回显」重算一遍
  // (已入库 / 已在下载列表 / 支持补缺失页 / 加入下载) —— 这几项取决于库数据, 会因下载/删除而变
  search: () => {
    if (!isTabActive('search')) return;
    const box = $('#tab-search');
    if (!box) return;
    box.querySelectorAll('button[data-cart-aid],button[data-cart-author]')
      .forEach(b => { delete b.dataset.dec; });
    return decorateCartButtons(box);
  },
  settings: () => loadSafemode(),                  // R18 开关 (图库重画由 artworks 域负责)
  auth: () => refreshAuth(),                       // 登录/退出 → 头像与登录态
  thumbs: () => { if (isTabActive('settings')) loadThumbsStats(); },
  backups: () => { const m = $('#lib-modal');
                   if (m && m.style.display === 'flex') openBackupCenter(); },
  ops: () => { if (isTabActive('settings')) loadOperations(); },
};
const SCOPE_LABEL = { artworks: '图库', pages: '图片', authors: '作者', facets: '筛选项',
  stats: '统计', queue: '下载任务', check: '体检报告', reverse: '反搜', sources: '来源分类',
  search: '查询结果', settings: '设置', auth: '登录态', thumbs: '缩略图缓存',
  backups: '备份', ops: '操作记录' };
let _dirtyScopes = {};     // {scope: {rev, ids}} 还没刷新的数据域
let _dirtyRev = 0;         // 已消费到的事件版本号 (补偿轮询的 since)
let _dirtyTimer = null;

function onDirty(ev) {
  const sc = (ev && ev.scopes) || {};
  Object.keys(sc).forEach(k => { if (SCOPE_REFRESH[k]) _dirtyScopes[k] = sc[k]; });
  if (ev && ev.rev) _dirtyRev = Math.max(_dirtyRev, Number(ev.rev) || 0);
  paintSmartRefresh();
  scheduleDirtyFlush();
}
window.onDirty = onDirty;

function scheduleDirtyFlush() {
  // 一次操作往往连带几个域 (作品+页+筛选+统计) → 节流合并成一轮, 避免连环刷新卡顿
  if (_dirtyTimer) return;
  _dirtyTimer = setTimeout(() => { _dirtyTimer = null; flushDirty(); }, 350);
}

window.flushDirty = async function () {
  const scopes = Object.keys(_dirtyScopes);
  if (!scopes.length) return;
  _dirtyScopes = {};
  const uniq = [...new Set(scopes.map(s => (s === 'pages' ? 'artworks' : s)))];
  for (const s of uniq) {
    try { await (SCOPE_REFRESH[s] || (() => null))(); } catch (e) { /* 单个失败不挡其他域 */ }
  }
  // 「上一步改过什么」这条线索已被事件兑现 → 撤销, 别一直挂在按钮提示上
  const a = activeAction();
  if (a && (ACTION_SCOPES[a.scope] || []).some(s => uniq.includes(s))) clearLastAction();
  paintSmartRefresh();
};

// 页面打开时只对齐版本号 (不触发刷新: 页面本来就在加载全部数据)
async function initDirtyBaseline() {
  try {
    const r = await api('/api/dirty?since=0');
    _dirtyRev = Number((r && r.rev) || 0);
  } catch (e) { /* 静默 */ }
}

// 补偿轮询: SSE 断线/被浏览器节流时也不会丢事件 (since=已消费版本)
async function pollDirty() {
  try {
    const r = await api('/api/dirty?since=' + encodeURIComponent(_dirtyRev));
    if (r && r.scopes && Object.keys(r.scopes).length) onDirty(r);
  } catch (e) { /* 静默 */ }
  paintSmartRefresh();   // 顺手校正提示 (lastAction 过期后按钮要回到默认文案)
}

// ===== 📜 运行日志查看  =====
// 后端把「接口流水 + 用户点击」都写进了 logs/activity/; 这里给一个只读的查看入口。
async function loadActivityStats() {
  const el = $('#activity-stats');
  if (!el) return;
  try {
    const r = await api('/api/activity/tail?n=1');
    const lines = r.lines || [];
    el.innerHTML = lines.length
      ? `最新一条：<code>${esc(String(lines[lines.length - 1]).slice(0, 140))}</code>`
        + `<br>当天(${esc(r.day)})共 ${(r.files || []).length} 个文件`
      : '今天还没有记录。';
  } catch (e) { el.textContent = '读取失败：' + e.message; }
}

window.openActivityLog = async function (n) {
  const cnt = Number(n) || 400;
  openLibModal(`📜 运行日志（最近 ${cnt} 行）`, '<div class="hint">加载中…</div>', { lg: true });
  try {
    const r = await api('/api/activity/tail?n=' + cnt);
    const lines = r.lines || [];
    $('#lib-modal-body').innerHTML =
      `<div class="hint" style="margin-bottom:8px">${esc(r.day)} · ${lines.length} 行`
      + ` · 文件 ${esc((r.files || []).join(', '))}`
      + `<button class="btn ghost" style="float:right" onclick="openActivityLog(${cnt})">🔄 刷新</button></div>`
      + `<pre class="activity-log">${esc(lines.join('\n'))}</pre>`;
  } catch (e) {
    $('#lib-modal-body').innerHTML = '<div class="hint">读取失败：' + esc(e.message) + '</div>';
  }
};

// ===== 🎯 万能刷新: 行脚医生 (轻量 / 定向, 2026-09-17 用户定位) =====
// 用户的比喻: 自检 = 医院全院普查 (多条、每条都是全量); 刷新 = 行脚医生 ——
// 同样能筛查所有病症, 但**只诊治刚刚出问题的人**。所以:
//   ① 每次关键改动记下 lastAction (作用域 + 目标 ID);
//   ② 右下角 🔄 按钮据此只重算那一小块 (/api/library/refresh-scope), 毫秒级;
//   ③ 兜底也最差只是把当前视图重拉一遍 —— 不跑全库磁盘普查 (那是自检的活)。
let lastAction = null;   // {scope:'work'|'author'|'library', ids:[...], label, at}
// 这条线索是"临时的": 要么被事件驱动刷新兑现后撤销, 要么超过 TTL 自动过期 ——
// 否则按钮提示会永远停在"刷新：刚编辑的作品 #xxx"上, 用户就失去了"不知道刷什么就点它"的兜底语义。
const ACTION_TTL = 5 * 60 * 1000;
// 每类动作会连带影响哪些域: 事件驱动把这些域刷过 → 这条线索已兑现, 可以撤销
const ACTION_SCOPES = {
  work: ['artworks', 'pages', 'authors'],
  author: ['authors', 'artworks'],
  library: ['artworks', 'queue', 'check'],
};
function activeAction() {
  return (lastAction && Date.now() - lastAction.at < ACTION_TTL) ? lastAction : null;
}
function clearLastAction() {
  lastAction = null;
  paintSmartRefresh();
}
window.clearLastAction = clearLastAction;
function setLastAction(scope, ids, label) {
  lastAction = { scope: String(scope), ids: (ids || []).map(String),
                 label: label || '', at: Date.now() };
  paintSmartRefresh();
}
window.setLastAction = setLastAction;

function paintSmartRefresh() {
  const b = $('#smart-refresh');
  if (!b) return;
  const a = activeAction();
  const dirty = Object.keys(_dirtyScopes).map(s => SCOPE_LABEL[s] || s);
  if (dirty.length) {
    // 有事件驱动的待刷新域 → 按钮明说"哪几块有更新"
    b.dataset.tip = `刷新：${dirty.join(' / ')} 有更新`;
  } else if (a) {
    b.dataset.tip = `刷新${a.label ? '：' + a.label : ''}`;
  } else {
    b.dataset.tip = '刷新';
  }
  b.classList.toggle('has-target', !!(dirty.length || a));
}

window.smartRefresh = async function () {
  const b = $('#smart-refresh');
  const act = activeAction();
  const dirty = Object.keys(_dirtyScopes);
  if (b) { b.disabled = true; b.classList.add('spin'); }
  try {
    // 「行脚医生」定位 : 刷新 = 同能力筛查、但**只诊治刚刚出问题的人**;
    // 全库磁盘普查是「自检」的活, 刷新不做, 保持毫秒级。
    // 但"当前视图的真实状态"必须**一步到位**: 封面改了 / 因修改在"按修改时间"里提到了最前 /
    // 新入库了 / 刚删掉了 —— 点一下就该看见 (旧实现按三分支走, 有时只刷了局部就收工)。
    const done = [];
    if (dirty.length) {
      await flushDirty();               // ① 先把手动触发前累积的待刷新域兑现
      done.push(...dirty.map(s => SCOPE_LABEL[s] || s));
    }
    if (act && (act.scope === 'work' || act.scope === 'author') && act.ids.length) {
      // ② 有"刚改过的对象" → 顺手定向核对它 (本地重算毫秒级; 联网失败不影响结果)
      try {
        const r = await api('/api/library/refresh-scope', { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scope: act.scope, ids: act.ids }) });
        const err = apiErr(r);
        if (err) throw new Error(err);
        if ((r.missing_files || []).length) {
          toast(`⚠ 发现 ${r.missing_files.length} 个作品的文件缺失`, 'err');
        }
      } catch (e) { /* 定向核对失败不挡整体刷新 */ }
      clearLastAction();
    }
    // ③ 当前视图重拉一遍 (列表/统计/筛选项/同步状态/回收站), 让"看得见的变化"都反映出来。
    //    手动点刷新 = 显式要求"按现在的真实状态重画一遍" → 用 force 绕过指纹跳过;
    //    
    //     这里再加一层 force, 保证"用户点了就一定看得见重画"。)
    const jobs = [loadStats(), loadTrashBadge()];
    if (isTabActive('library')) {
      jobs.push(loadLibrary({ resetPage: false, force: true }), libSync(), loadLibFacets());
    } else if (isTabActive('trash')) jobs.push(loadTrash());
    else if (isTabActive('authors')) jobs.push(loadAuthors({ resetPage: false }));
    else if (isTabActive('stats')) jobs.push(loadStatsPage());
    if (isTabActive('settings')) jobs.push(loadOperations());
    await Promise.allSettled(jobs);
    toast(done.length ? `已刷新：${done.join(' / ')}` : '已刷新', 'ok');
    paintSmartRefresh();
  } catch (e) {
    toast('刷新失败: ' + e.message, 'err');
  } finally {
    if (b) { b.disabled = false; b.classList.remove('spin'); }
  }
};

(function wireSmartRefresh() {
  const b = $('#smart-refresh');
  if (!b) return;
  // 图标是 index.html 里的内联 SVG (透明底 + currentColor), 不要用 JS 覆盖成 emoji
  b.addEventListener('click', () => window.smartRefresh());
  paintSmartRefresh();
})();

// ============ 📡 实时活动上报  ============
// 把用户的每一次点击 / 控件变化 / 切页送到后端, 与后端 HTTP 流水合成一份**可回放的现场**:
//   用户点了哪个按钮(CLICK) → 触发了哪个接口(HTTP) → 结果与耗时 —— 出事时照着 log 就能定位。
// 三条纪律:
//   ① 捕获阶段监听: 即使某处 JS stopPropagation 拦掉了冒泡, 也能记到;
//   ② 批量攒着发(满 30 条或 0.8s 一次), **绝不逐条发请求** —— 用户明确要求不卡顿;
//   ③ 上报自身失败一律静默: 日志功能绝不影响正常使用。
const _actQ = [];
let _actTimer = null;
let _actSeq = 0;

function _actEl(el) {
  if (!el || !el.tagName) return '?';
  let s = el.tagName.toLowerCase();
  if (el.id) s += '#' + el.id;
  const cls = (el.className && typeof el.className === 'string')
    ? el.className.split(/\s+/).filter(Boolean).slice(0, 3).join('.') : '';
  return cls ? s + '.' + cls : s;
}

function _actText(el, n = 40) {
  try {
    const t = (el.innerText || el.value || el.getAttribute('title') || '')
      .replace(/\s+/g, ' ').trim();
    return t.slice(0, n);
  } catch (e) { return ''; }
}

function _actCtx(el) {
  // 离它最近的 tabpage, 便于回放时知道"用户当时在哪个页面动的手"
  try {
    const page = el && el.closest ? el.closest('.tabpage') : null;
    const tab = (page && page.id) ? page.id.replace(/^tab-/, '') : '';
    const head = el && el.closest ? el.closest('h2, h3') : null;
    return { tab, near: head ? _actText(head, 20) : '' };
  } catch (e) { return {}; }
}

function actPush(kind, msg, fields) {
  try {
    _actQ.push({ k: kind, m: msg, f: Object.assign({ seq: ++_actSeq }, fields || {}) });
    if (_actQ.length >= 30) actFlush();
    else if (!_actTimer) _actTimer = setTimeout(actFlush, 800);
  } catch (e) { /* 静默 */ }
}

function actFlush() {
  if (_actTimer) { clearTimeout(_actTimer); _actTimer = null; }
  if (!_actQ.length) return;
  const batch = _actQ.splice(0, 60);
  try {
    fetch('/api/activity', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: batch }), keepalive: true,
    }).catch(() => {});
  } catch (e) { /* 静默 */ }
}

document.addEventListener('click', (ev) => {
  const t = ev.target;
  const el = (t && t.closest)
    ? (t.closest('button, a, .tag, input, label, .tab, [role="button"]') || t) : t;
  actPush('CLICK', _actEl(el), Object.assign({ text: _actText(el) }, _actCtx(el)));
}, true);

document.addEventListener('change', (ev) => {
  const el = ev.target;
  if (!el || !el.tagName) return;
  let v = el.value;
  if (el.type === 'checkbox' || el.type === 'radio') v = el.checked ? 'checked' : 'unchecked';
  else if (el.tagName === 'SELECT') {
    v = (el.selectedOptions && el.selectedOptions[0]) ? el.selectedOptions[0].text : el.value;
  }
  actPush('CHANGE', _actEl(el),
    Object.assign({ value: String(v == null ? '' : v).slice(0, 60) }, _actCtx(el)));
}, true);

// 文本输入: 停手 1.2s 才记一条"最终输入了什么" (逐键记会把日志刷成洪水)
let _actInputTimer = null;
const _actInputPending = {};
document.addEventListener('input', (ev) => {
  const el = ev.target;
  if (!el || !el.tagName) return;
  if (el.type === 'range' || el.type === 'number') {
    actPush('INPUT', _actEl(el), Object.assign({ value: String(el.value).slice(0, 30) }, _actCtx(el)));
    return;
  }
  if (!/^(text|search|textarea|)$/.test(el.type || '')) return;
  _actInputPending[el.id || el.name || 'input'] = {
    el, v: String(el.value || '').slice(0, 60), ctx: _actCtx(el),
  };
  if (_actInputTimer) clearTimeout(_actInputTimer);
  _actInputTimer = setTimeout(() => {
    Object.keys(_actInputPending).forEach((k) => {
      const it = _actInputPending[k];
      delete _actInputPending[k];
      actPush('INPUT', _actEl(it.el), Object.assign({ value: it.v }, it.ctx));
    });
  }, 1200);
}, true);

document.addEventListener('submit', (ev) => {
  actPush('SUBMIT', _actEl(ev.target), _actCtx(ev.target));
}, true);

// 快捷键 (只记带修饰键的组合, 普通按键不记 —— 否则打字段落会淹掉日志)
document.addEventListener('keydown', (ev) => {
  if (ev.ctrlKey || ev.altKey || ev.metaKey) {
    actPush('KEY', `Ctrl/Alt+${ev.key}`, { target: _actEl(ev.target) });
  }
}, true);

window.addEventListener('beforeunload', () => { actFlush(); });

// 🔔 事件驱动: 打开页面时对齐事件版本基线, 之后每 20s 兜底补偿一次
//    (正常路径是 SSE 实时推送; 轮询只防"标签页被浏览器节流/SSE 断线"导致漏事件)
initDirtyBaseline();
setInterval(pollDirty, 20000);
loadTrashBadge();   // 🗑️ 回收站角标: 启动时先显示已有数量 (删/恢复后各自会再更新)

// 托盘/桌面小控件打开界面时会带 hash 落地 → 直接落到对应页
// (不认这个 hash 的话, 用户从托盘进来还得自己再点一下)
// #library?only=<作品ID> 是桌面小控件「在库中查看」的落地: 进来就是「仅查看这个作品」。
// 除了首次加载还要认 hashchange —— 浏览器里已经开着这个页面时, 只改 fragment 的跳转不会
// 重新加载页面, 只触发 hashchange; 只听加载事件的话, 点了小控件的按钮看着像没反应。
function applyDeepLink() {
  const h = location.hash || '';
  const m = h.match(/^#library\?only=(\d+)$/);
  if (m) { switchTab('library'); libOnlyThisWork(m[1]); return; }
  if (h === '#settings') switchTab('settings');
  else if (h === '#library') switchTab('library');
}
applyDeepLink();
window.addEventListener('hashchange', applyDeepLink);

// 回到顶部 (初音 🎀 按钮): main 是滚动容器, 滚动超过 400px 淡入 =====
(function () {
  const btn = $('#to-top');
  const scroller = document.querySelector('main') || document.scrollingElement;
  if (!btn || !scroller) return;
  const onScroll = () => btn.classList.toggle('show', scroller.scrollTop > 400);
  scroller.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
  btn.addEventListener('click', () => scroller.scrollTo({ top: 0, behavior: 'smooth' }));
  // 立绘可能不存在(公开发布包不带他人 pixiv 作品的派生图, 见 .gitignore): 探测失败就退化成
  // 纯色圆形 + 白色箭头 —— 否则按钮会变成一个「看不见但仍能点」的透明热区, 压住右下角。
  // ⚠ 路径必须是 'static/to-top.png' 而不是 'to-top.png': JS 的 src 相对**文档**解析
  // (index.html 在根 '/'), 而 CSS 里的 url() 相对**样式表**解析 (/static/style.css) ——
  // 写成裸文件名会永远 404, 立绘明明在也会一直被当成缺失。
  const probe = new Image();
  probe.onerror = () => btn.classList.add('no-img');
  probe.src = 'static/to-top.png';
})();

// 心跳: 页面开着就上报存活; 后端只在「页面确实全关」时才自动退出服务。
// 注意: 浏览器会节流隐藏标签的定时器(约 1 次/分钟), 省电模式/内存压力下还会
// 冻结标签(定时器完全停) —— 所以除定时 ping 外, 在「回到本页/窗口聚焦/页面恢复」
// 时立刻补一次, 用户切回来即刷新存活信号, 避免服务被误判成「页面已关闭」而退出
// (那会导致切回来处处 failed to fetch)。
(function heartbeat() {
  const ping = () => fetch('/api/heartbeat').catch(() => {});
  ping();
  setInterval(ping, 10000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) ping(); });
  window.addEventListener('focus', ping);
  window.addEventListener('pageshow', ping);
  // 页面关闭/刷新瞬间发告别信号 (sendBeacon 专为卸载时投递设计, 不阻塞关闭);
  // 后端不只看它: 还要 SSE 连接也断开且过宽限才退出, 且刷新后新页面的心跳会取消退出
  window.addEventListener('pagehide', () => {
    try { navigator.sendBeacon('/api/bye'); } catch (e) { /* 忽略 */ }
  });
})();
