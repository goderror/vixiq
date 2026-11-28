// 壁纸文件管理：作品归属与自定义分组独立，所有图片遵循安全模式。
const WFA = {
  groups: [], page: 1, per: 80, results: [], visible: [], loading: 0,
  works: [], workSeq: 0, busy: false, sort: 'name', order: 'asc', compact: false,
};

const WF_FILTER_LABELS = {
  all: '全部', owned: '已归属', orphan: '未归属', dupe: '重复图片', favorite: '收藏',
};
const WF_SORT_LABELS = { name: '文件名', new: '最近修改', size: '文件大小' };
const WF_ORDER_LABELS = { asc: '升序', desc: '降序' };
const WF_MENUS = ['wf-sort-menu', 'wf-filters', 'wf-scan-menu',
                  'wf-organize-menu', 'wf-more-menu', 'wf-batch-menu'];
let _wfMenuTrigger = null;

// 过滤: 范围 + 状态 + 作品 + 分组 + 关键词; 壁纸库范围追加图库原图。
// 图库原图没有自定义分组, 分组筛选只作用于壁纸夹文件。
function wallFilterEntries(d, opt) {
  opt = opt || {};
  const scope = opt.scope === 'library' ? 'library' : 'wall';
  const filter = WF_FILTER_LABELS[opt.filter] ? opt.filter : 'all';
  const work = String(opt.work || '');
  const group = String(opt.group || '');
  const q = String(opt.q || '').trim().toLocaleLowerCase();
  const duplicateNames = opt.duplicateNames || new Set();
  const matchQ = f => !q || [f.name, f.title, f.artwork_id, f.author_name,
    f.group_name, f.rel, f.match_label, ...(f.labels || [])]
    .join(' ').toLocaleLowerCase().includes(q);
  let list = (d.items || []).filter(f => {
    if (filter === 'owned' && !f.artwork_id) return false;
    if (filter === 'orphan' && f.artwork_id) return false;
    if (filter === 'dupe' && !duplicateNames.has(f.name)) return false;
    if (filter === 'favorite' && !f.favorite) return false;
    if (work && f.artwork_id !== work) return false;
    if (group === 'none' && f.group_id != null) return false;
    if (group && group !== 'none' && String(f.group_id) !== group) return false;
    return matchQ(f);
  });
  if (scope === 'library') {
    const lib = (d.library_items || []).filter(f => {
      if (work && f.artwork_id !== work) return false;
      if (group) return false;
      return matchQ(f);
    });
    list = list.concat(lib);
  }
  return list;
}

// 排序: 文件名自然排序; 最近修改/大小按方向, 同值用文件名升序做稳定次级。
function wallSortEntries(list, sort, order) {
  const dir = order === 'desc' ? -1 : 1;
  const nameOf = f => String(f.name || f.rel || '');
  const nameCmp = (a, b) => nameOf(a).localeCompare(nameOf(b), 'zh-CN', { numeric: true });
  return list.slice().sort((a, b) => {
    if (sort === 'name') return nameCmp(a, b) * dir;
    let v = 0;
    if (sort === 'new') v = (a.mtime || 0) - (b.mtime || 0);
    else if (sort === 'size') v = (a.size || 0) - (b.size || 0);
    if (v === 0) return nameCmp(a, b);
    return v * dir;
  });
}

// 首次改选字段: 文件名升序, 最近修改/文件大小降序; 同字段保持当前方向。
function wallNextSort(field, currentSort, currentOrder) {
  const f = WF_SORT_LABELS[field] ? field : 'name';
  if (f === currentSort) return { sort: f, order: currentOrder };
  return { sort: f, order: f === 'name' ? 'asc' : 'desc' };
}

function wallFilterCount(opt) {
  opt = opt || {};
  return (WF_FILTER_LABELS[opt.filter] && opt.filter !== 'all' ? 1 : 0)
    + (opt.work ? 1 : 0) + (opt.group ? 1 : 0);
}

async function wallPost(url, data) {
  const r = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  if (!r || (r.ok !== true && r.status !== 'ok')) throw new Error(r?.msg || r?.error || '操作失败');
  return r;
}

// ---- 下拉菜单: 点外部/Esc 关闭, 焦点回入口 ----
function wfCloseMenus(exceptId) {
  let closedTrigger = null;
  WF_MENUS.forEach(id => {
    if (id === exceptId) return;
    const menu = document.getElementById(id);
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    const btn = document.querySelector(`[data-wf-menu="${id}"]`);
    if (btn) {
      btn.setAttribute('aria-expanded', 'false');
      if (!closedTrigger) closedTrigger = btn;
    }
  });
  if (!exceptId) _wfMenuTrigger = null;
  return closedTrigger;
}

function wfToggleMenu(btn, menuId) {
  const menu = document.getElementById(menuId);
  if (!menu) return;
  const open = menu.hidden;
  wfCloseMenus(menuId);
  menu.hidden = !open;
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  _wfMenuTrigger = open ? btn : null;
  if (open && menuId === 'wf-more-menu') wallPaintMoreToggle();
  if (open) {
    const first = menu.querySelector('button:not([disabled])');
    if (first) first.focus();
  }
}

function wallPaintMoreToggle() {
  const btn = document.getElementById('wf-more-toggle');
  if (!btn) return;
  const main = document.querySelector('main');
  btn.textContent = main && main.classList.contains('management-compact') ? '展开菜单' : '收起菜单';
}

window.wallMoreToggle = function () {
  wfCloseMenus();
  const btn = document.querySelector('#tab-wallpapers .management-primary .management-toggle');
  if (btn) btn.click();
};

window.wallOpenBatchMenu = function () {
  if (!WF_SEL.size) { toast('请先选择壁纸', 'info'); return; }
  wfCloseMenus();
  const btn = document.getElementById('wf-batch-btn');
  const menu = document.getElementById('wf-batch-menu');
  if (!btn || !menu) return;
  menu.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  _wfMenuTrigger = btn;
  const first = menu.querySelector('button:not([disabled])');
  if (first) first.focus();
};

// ---- 工具栏布局: 桌面两行; 窄屏把范围/排序/筛选收成第二行, 其余进「更多」 ----
function initWallpaperToolbarLayout() {
  const bar = document.querySelector('#tab-wallpapers .management-toolbar');
  if (!bar || bar.dataset.wfLayout === '1') return;
  bar.dataset.wfLayout = '1';
  const primary = bar.querySelector('.management-primary');
  const filtersRow = document.getElementById('wf-extra-tools');
  const scopeGroup = document.getElementById('wf-scope');
  const scopeSelect = document.getElementById('wf-scope-select');
  const sortWrap = bar.querySelector('.wf-sort-wrap');
  const selectAll = document.getElementById('wf-select-all');
  const collapseBtn = bar.querySelector('.wf-collapse-btn');
  const moreBtn = document.getElementById('wf-more-btn');
  const toolMenus = bar.querySelector('.wf-tool-menus-desktop');
  const filterWrap = bar.querySelector('.wf-filter-wrap');
  const filterMenu = document.getElementById('wf-filters');
  const work = document.getElementById('wf-work-filter');
  const group = document.getElementById('wf-group-filter');
  const clearBtn = document.getElementById('wf-clear-filters');
  if (!primary || !filtersRow || !scopeGroup || !scopeSelect || !sortWrap
      || !selectAll || !collapseBtn || !moreBtn || !toolMenus || !filterWrap
      || !filterMenu || !work || !group || !clearBtn) return;

  const mq = window.matchMedia('(max-width: 980px)');
  function apply(compact) {
    WFA.compact = compact;
    wfCloseMenus();
    if (compact) {
      filtersRow.insertBefore(scopeSelect, filtersRow.firstChild);
      filtersRow.insertBefore(sortWrap, scopeSelect.nextSibling);
      filterMenu.append(work, group, clearBtn);
      scopeGroup.hidden = true;
      scopeSelect.hidden = false;
      selectAll.hidden = true;
      collapseBtn.hidden = true;
      moreBtn.hidden = false;
      toolMenus.hidden = true;
    } else {
      primary.insertBefore(scopeSelect, scopeGroup.nextSibling);
      primary.insertBefore(sortWrap, selectAll);
      filterWrap.after(work, group, clearBtn);
      scopeGroup.hidden = false;
      scopeSelect.hidden = true;
      selectAll.hidden = false;
      collapseBtn.hidden = false;
      moreBtn.hidden = true;
      toolMenus.hidden = false;
    }
    wallSyncFiltersUI();
    wallSyncSortUI();
  }
  mq.addEventListener('change', e => apply(e.matches));
  apply(mq.matches);

  // 下拉触发按钮与菜单的对应关系 (HTML 只写按钮 ID, 这里统一接线)
  [['wf-sort-btn', 'wf-sort-menu'], ['wf-filter-btn', 'wf-filters'],
   ['wf-scan-menu-btn', 'wf-scan-menu'], ['wf-organize-menu-btn', 'wf-organize-menu'],
   ['wf-more-btn', 'wf-more-menu'], ['wf-batch-btn', 'wf-batch-menu']]
    .forEach(([btnId, menuId]) => {
      const b = document.getElementById(btnId);
      if (b) b.dataset.wfMenu = menuId;
    });
  document.addEventListener('click', e => {
    if (e.target.closest('[data-wf-menu]') || e.target.closest('.lib-menu')) return;
    const open = WF_MENUS.some(id => {
      const m = document.getElementById(id);
      return m && !m.hidden;
    });
    if (!open) return;
    const trigger = _wfMenuTrigger;
    wfCloseMenus();
    // 点空白处关闭时把焦点还给入口; 点到其他交互控件则保留用户的新焦点
    const focusable = e.target.closest(
      'input, select, textarea, button, a, [contenteditable="true"]');
    if (!focusable && trigger && trigger.getClientRects().length) trigger.focus();
  });
}

function wallSyncScopeUI() {
  document.querySelectorAll('#wf-scope [data-wf-scope]').forEach(b => {
    const on = b.dataset.wfScope === WF_SCOPE;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  const sel = document.getElementById('wf-scope-select');
  if (sel) sel.value = WF_SCOPE;
}

window.wallSetScope = function (scope) {
  const next = scope === 'library' ? 'library' : 'wall';
  if (next === WF_SCOPE) return;
  WF_SCOPE = next;
  // 筛选只作用于壁纸夹: 换范围从全范围搜索开始, 保留关键词/排序/作品
  WF_FILTER = 'all';
  const group = document.getElementById('wf-group-filter');
  if (group) group.value = '';
  WFA.page = 1;
  wfCloseMenus();
  wallSyncScopeUI();
  wallSyncFiltersUI();
  wallFilesPaint();
};

function wallSetFilter(filter) {
  if (!WF_FILTER_LABELS[filter]) return;
  const trigger = _wfMenuTrigger;
  WF_FILTER = filter;
  WFA.page = 1;
  wfCloseMenus();
  wallSyncFiltersUI();
  wallFilesPaint();
  if (trigger && trigger.getClientRects().length) trigger.focus();
}

window.wallClearFilters = function () {
  WF_FILTER = 'all';
  ['wf-work-filter', 'wf-group-filter'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  WFA.page = 1;
  wfCloseMenus();
  wallSyncFiltersUI();
  wallFilesPaint();
};

function wallSyncFiltersUI() {
  const work = document.getElementById('wf-work-filter')?.value || '';
  const group = document.getElementById('wf-group-filter')?.value || '';
  const n = wallFilterCount({ filter: WF_FILTER, work, group });
  const label = document.getElementById('wf-filter-label');
  if (label) label.textContent = WF_FILTER_LABELS[WF_FILTER] || '全部';
  const count = document.getElementById('wf-filter-count');
  if (count) {
    count.textContent = n ? ` ${n}` : '';
    count.hidden = n === 0;
  }
  const btn = document.getElementById('wf-filter-btn');
  if (btn) {
    btn.dataset.count = String(n);
    btn.setAttribute('aria-label', n ? `筛选，已启用 ${n} 项` : '筛选');
  }
  document.querySelectorAll('#wf-filters [data-f]').forEach(b => {
    const on = b.dataset.f === WF_FILTER;
    b.classList.toggle('active', on);
    b.classList.toggle('is-checked', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
    const chk = b.querySelector('.wf-check');
    if (chk) chk.textContent = on ? '✓' : '';
  });
  const clear = document.getElementById('wf-clear-filters');
  if (clear) clear.hidden = n === 0;
}

function wallSyncSortUI() {
  const field = WF_SORT_LABELS[WFA.sort] ? WFA.sort : 'name';
  const order = WFA.order === 'desc' ? 'desc' : 'asc';
  const btn = document.getElementById('wf-sort-btn');
  if (btn) {
    const fEl = btn.querySelector('.wf-sort-field');
    const wEl = btn.querySelector('.wf-sort-word');
    const aEl = btn.querySelector('.wf-sort-arrow');
    if (fEl) fEl.textContent = WF_SORT_LABELS[field];
    if (wEl) wEl.textContent = ' ' + WF_ORDER_LABELS[order];
    if (aEl) aEl.textContent = order === 'asc' ? '↑' : '↓';
    btn.setAttribute('aria-label',
      `排序，${WF_SORT_LABELS[field]}，${WF_ORDER_LABELS[order]}`);
  }
  const native = document.getElementById('wf-sort');
  if (native) native.value = field;
  document.querySelectorAll('#wf-sort-menu [data-wf-sort]').forEach(b => {
    const on = b.dataset.wfSort === field;
    b.classList.toggle('is-checked', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
    const chk = b.querySelector('.wf-check');
    if (chk) chk.textContent = on ? '✓' : '';
  });
  document.querySelectorAll('#wf-sort-menu [data-wf-order]').forEach(b => {
    const on = b.dataset.wfOrder === order;
    b.classList.toggle('is-checked', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
    const chk = b.querySelector('.wf-check');
    if (chk) chk.textContent = on ? '✓' : '';
  });
}

function wallSetSortField(field) {
  const next = wallNextSort(field, WFA.sort, WFA.order);
  const changed = next.sort !== WFA.sort || next.order !== WFA.order;
  const trigger = _wfMenuTrigger;
  WFA.sort = next.sort;
  WFA.order = next.order;
  wfCloseMenus();
  wallSyncSortUI();
  if (!changed) {
    if (trigger && trigger.getClientRects().length) trigger.focus();
    return;
  }
  WFA.page = 1;
  wallFilesPaint();
  if (trigger && trigger.getClientRects().length) trigger.focus();
}

function wallSetSortOrder(order) {
  const next = order === 'desc' ? 'desc' : 'asc';
  const trigger = _wfMenuTrigger;
  wfCloseMenus();
  if (next === WFA.order) {
    if (trigger && trigger.getClientRects().length) trigger.focus();
    return;
  }
  WFA.order = next;
  WFA.page = 1;
  wallSyncSortUI();
  wallFilesPaint();
  if (trigger && trigger.getClientRects().length) trigger.focus();
}

function wallPaintSearchClear() {
  const input = document.getElementById('wf-search');
  const btn = document.getElementById('wf-search-clear');
  if (input && btn) btn.hidden = !input.value;
}

function wallFilesEnter() {
  const root = $('#tab-wallpapers');
  if (!root._adminBound) {
    root._adminBound = true;
    initWallpaperToolbarLayout();
    root.addEventListener('click', e => {
      const trigger = e.target.closest('[data-wf-menu]');
      if (trigger) {
        e.preventDefault();
        wfToggleMenu(trigger, trigger.dataset.wfMenu);
        return;
      }
      const scope = e.target.closest('[data-wf-scope]');
      if (scope) { wallSetScope(scope.dataset.wfScope); return; }
      const sortField = e.target.closest('[data-wf-sort]');
      if (sortField) { wallSetSortField(sortField.dataset.wfSort); return; }
      const sortOrder = e.target.closest('[data-wf-order]');
      if (sortOrder) { wallSetSortOrder(sortOrder.dataset.wfOrder); return; }
      const filter = e.target.closest('#wf-filters [data-f]');
      if (filter) { wallSetFilter(filter.dataset.f); return; }
      const more = e.target.closest('[data-wall-more]');
      if (more) wallDetails(more.dataset.wallMore);
      const preview = e.target.closest('[data-preview]');
      if (preview) wallFilesPreview(preview.dataset.preview);
      const libPreview = e.target.closest('[data-preview-lib]');
      if (libPreview) wallLibraryPreview(libPreview.dataset.previewLib);
      const page = e.target.closest('[data-wall-page]');
      if (page) wallGoPage(Number(page.dataset.wallPage));
      if (e.target.closest('#wf-search-clear')) {
        const input = document.getElementById('wf-search');
        if (input) {
          input.value = '';
          WFA.page = 1;
          wallPaintSearchClear();
          wallFilesPaint();
          input.focus();
        }
      }
      // 菜单项执行完动作后收起; 「操作（已选 N）」要接着打开批量菜单
      if (e.target.closest('.lib-menu button') && !e.target.closest('#wf-more-batch')) {
        wfCloseMenus();
      }
    });
    root.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        const open = WF_MENUS.some(id => {
          const m = document.getElementById(id);
          return m && !m.hidden;
        });
        if (open) {
          const trigger = _wfMenuTrigger;
          wfCloseMenus();
          if (trigger && trigger.getClientRects().length) trigger.focus();
          // 打开的壁纸下拉先消费这次 Esc, 不再冒泡去关闭外层简化工具栏
          e.stopPropagation();
          return;
        }
      }
      if (!e.target.matches('[data-wall-page-input]') || e.key !== 'Enter') return;
      const n = parseInt(e.target.value, 10);
      if (Number.isFinite(n)) wallGoPage(n);
      else e.target.value = WFA.page;
    });
    root.addEventListener('change', e => {
      if (e.target.matches('input[data-name]')) wallFilesToggle(e.target);
      if (['wf-work-filter', 'wf-group-filter'].includes(e.target.id)) {
        WFA.page = 1;
        wallSyncFiltersUI();
        wallFilesPaint();
      }
      if (e.target.id === 'wf-scope-select') wallSetScope(e.target.value);
    });
    const search = document.getElementById('wf-search');
    if (search) search.addEventListener('input', () => {
      WFA.page = 1;
      wallPaintSearchClear();
      wallFilesPaint();
    });
  }
  wallFilesLoad();
  api('/api/wallpaper/files/scan/status').then(s => { if (s?.status === 'scanning') wallFilesPoll(); }).catch(() => {});
}

function wallOptions(el, options) {
  const previous = el.value;
  el.innerHTML = options;
  el.value = previous;
  if (el.selectedIndex < 0) el.value = '';
}

async function wallFilesLoad() {
  const seq = ++WFA.loading;
  try {
    const [d, groups] = await Promise.all([
      api('/api/wallpaper/files'),
      api('/api/wallpaper/groups'),
    ]);
    if (seq !== WFA.loading) return;
    if (!d?.ok || !groups?.ok) throw new Error(d?.error || groups?.msg || '读取失败');
    WFD = d; WFA.groups = groups.items || [];
    const alive = new Set(d.items.map(x => x.name));
    [...WF_SEL].forEach(n => { if (!alive.has(n)) WF_SEL.delete(n); });
    const works = new Map();
    const addWork = x => {
      if (!x.artwork_id) return;
      const w = works.get(x.artwork_id) || { title: x.title, n: 0 };
      w.n++;
      works.set(x.artwork_id, w);
    };
    d.items.forEach(addWork);
    (d.library_items || []).forEach(addWork);
    wallOptions($('#wf-work-filter'), '<option value="">作品：全部</option>' + [...works].map(([id, w]) =>
      `<option value="${esc(id)}">${esc(w.title || id)} · ${w.n} 张</option>`).join(''));
    wallOptions($('#wf-group-filter'), '<option value="">分组：全部（仅壁纸夹）</option><option value="none">未分组（仅壁纸夹）</option>' + WFA.groups.map(g =>
      `<option value="${g.id}">${esc(g.name)} · ${d.items.filter(x => x.group_id === g.id).length} 张 · 仅壁纸夹</option>`).join(''));
    wallFilesPaint();
  } catch (e) { if (seq === WFA.loading) $('#wf-result').innerHTML = `<div class="hint err-text">${esc(e.message)}</div>`; }
}

function wallFilesCard(f) {
  const owner = f.artwork_id ? `${f.title || f.artwork_id}${f.page_num == null ? '' : ' · 第 ' + (f.page_num + 1) + ' 张'}` : '未归属';
  return `<article class="wf-card${f.current ? ' is-current' : ''}${WF_SEL.has(f.name) ? ' is-selected' : ''}">
    <label class="wf-pick"><input type="checkbox" data-name="${esc(f.name)}" aria-label="选择 ${esc(f.name)}"${WF_SEL.has(f.name) ? ' checked' : ''}></label>
    <button class="wf-more" data-wall-more="${esc(f.name)}" aria-label="更多操作 ${esc(f.name)}" title="更多操作">⋯</button>
    <button class="wf-thumb" data-preview="${esc(f.name)}" aria-label="预览 ${esc(f.name)}">
      <img src="${esc(safeThumb(f.thumb, f))}" alt="" loading="lazy" onerror="this.style.opacity=.15">
      ${f.current ? '<span class="wf-cur">当前桌面</span>' : ''}</button>
    <div class="wf-meta"><div class="wf-name" title="${esc(f.name)}">${f.favorite ? '★ ' : ''}${esc(f.name)}</div>
      <div class="wf-line" title="${esc(owner)}">${esc(owner)}</div>
      <div class="wf-line">${esc(f.author_name || '')} ${f.width}×${f.height} · ${fmtSize(f.size)} · ${esc(f.format)}</div>
      <div class="wf-badges"><span class="wf-tag ${f.artwork_id ? '' : 'orphan'}">${esc(f.group_name || '未分组')}</span>${(f.labels || []).map(t => `<span class="wf-tag">${esc(t)}</span>`).join('')}</div>
    </div></article>`;
}

function wallLibraryCard(f) {
  const owner = f.artwork_id ? `${f.title || f.artwork_id}${f.page_num == null ? '' : ' · 第 ' + (f.page_num + 1) + ' 张'}` : '未归属';
  return `<article class="wf-card wf-lib-card${f.current ? ' is-current' : ''}">
    <span class="wf-lib-badge">图库原图</span>
    <button class="wf-thumb" data-preview-lib="${esc(f.rel)}" aria-label="预览 ${esc(f.name)}">
      <img src="${esc(safeThumb(f.thumb, f))}" alt="" loading="lazy" onerror="this.style.opacity=.15">
      ${f.current ? '<span class="wf-cur">当前桌面</span>' : ''}</button>
    <div class="wf-meta"><div class="wf-name" title="${esc(f.rel)}">${esc(f.name)}</div>
      <div class="wf-line" title="${esc(owner)}">${esc(owner)}</div>
      <div class="wf-line">${esc(f.author_name || '')} ${fmtSize(f.size)} · ${esc(f.format || '')}</div>
      <div class="wf-badges"><span class="wf-tag">图库原图</span>${f.used_at
        ? `<span class="wf-tag">${esc(String(f.used_at).slice(0, 10))}</span>` : ''}</div>
    </div></article>`;
}

function wallLibraryPreview(rel) {
  const f = (WFD?.library_items || []).find(x => x.rel === rel);
  if (!f) return;
  openLightbox({ page_num: f.page_num || 0, url: f.url, width: f.width, height: f.height,
    file_size: f.size, title: f.name, author_name: f.author_name,
    is_r18: f.is_r18 == null ? 1 : f.is_r18, artwork_id: f.artwork_id }, null, null);
}

function wallFilesPaint() {
  if (!WFD) return;
  const d = WFD, stats = d.stats || {};
  const q = $('#wf-search').value.trim();
  $('#wf-dir').textContent = d.dir ? '壁纸夹 ' + d.dir : '尚未指定壁纸夹';
  $('#wf-scan-tip').textContent = d.scanned ? `共 ${stats.total} 张 · ${fmtSize(stats.bytes)} · 上次扫描 ${stats.scanned_at || '未知'}` : '扫描归属后可管理壁纸文件。';
  $('#wf-stats').innerHTML = [
    ['壁纸库', stats.library_total ?? stats.total, '壁纸夹文件 + 已登记但未复制到壁纸夹的图库原图'],
    ['壁纸夹', stats.total, '壁纸夹里的实际文件数'],
    ['已归属', stats.owned, '能对应到图库作品/页的壁纸'],
    ['重复图片', `${stats.dupes || 0} 组`, '同作品同页或内容一致的重复组'],
  ].map(([label, n, tip]) =>
    `<div class="stat-card" title="${esc(tip)}"><div class="num">${n}</div><div class="lbl">${label}</div></div>`).join('');
  const duplicateNames = new Set((d.groups || []).flatMap(g => g.files));
  const list = wallSortEntries(wallFilterEntries(d, {
    scope: WF_SCOPE, filter: WF_FILTER,
    work: $('#wf-work-filter').value,
    group: $('#wf-group-filter').value,
    q, duplicateNames,
  }), WFA.sort, WFA.order);
  WFA.results = list;
  const pages = Math.max(1, Math.ceil(list.length / WFA.per));
  WFA.page = Math.max(1, Math.min(WFA.page, pages));
  WFA.visible = list.slice((WFA.page - 1) * WFA.per, WFA.page * WFA.per);
  let content = '';
  if (WF_FILTER === 'dupe' && WF_SCOPE === 'wall') {
    const visible = new Set(WFA.visible.filter(x => x.kind !== 'library').map(x => x.name));
    content = (d.groups || []).map(g => {
      const files = list.filter(f => visible.has(f.name) && g.files.includes(f.name));
      if (!files.length) return '';
      const label = g.kind === 'page' ? `${g.title || g.artwork_id} · 第 ${Number(g.page_num) + 1} 张图片` : '同一张图片';
      return `<div class="wf-group"><div class="wf-group-head">${esc(label)} · ${g.files.length} 个版本</div><div class="wf-grid">${files.map(wallFilesCard).join('')}</div></div>`;
    }).join('');
  } else {
    content = `<div class="wf-grid">${WFA.visible.map(f =>
      f.kind === 'library' ? wallLibraryCard(f) : wallFilesCard(f)).join('')}</div>`;
  }
  if (!list.length) {
    // 壁纸夹没搜到: 提供沿用关键词去壁纸库继续搜
    content = WF_SCOPE === 'wall' && q
      ? `<div class="hint wf-empty">没有符合条件的壁纸。<button type="button" class="btn ghost" onclick="wallSetScope('library')">到壁纸库搜索</button></div>`
      : '<div class="hint wf-empty">没有符合条件的壁纸。</div>';
  }
  $('#wf-result').innerHTML = content;
  // 与图库/作者同一套分页: 左侧总数, 右侧首页/上一页/页码输入/下一页/尾页
  const pagerNav = pages > 1 ? `
      <button class="btn ghost" data-wall-page="1"${WFA.page === 1 ? ' disabled' : ''} title="首页">«</button>
      <button class="btn ghost" data-wall-page="${WFA.page - 1}"${WFA.page === 1 ? ' disabled' : ''} title="上一页">‹</button>
      <span class="pager-info">第
        <input class="lib-page-input" type="number" min="1" max="${pages}" value="${WFA.page}"
               data-wall-page-input aria-label="跳转页码"> / ${pages} 页
      </span>
      <button class="btn ghost" data-wall-page="${WFA.page + 1}"${WFA.page === pages ? ' disabled' : ''} title="下一页">›</button>
      <button class="btn ghost" data-wall-page="${pages}"${WFA.page === pages ? ' disabled' : ''} title="尾页">»</button>` : '';
  const libCount = list.filter(f => f.kind === 'library').length;
  const totalText = WF_SCOPE === 'library'
    ? `共 ${list.length} 项 · 壁纸夹 ${list.length - libCount} · 图库原图 ${libCount}`
    : `共 ${list.length} 张`;
  $('#wf-pager').innerHTML = `<div class="pager-inner"><span class="pager-total">${totalText}</span>${pagerNav}</div>`;
  wallFilesSyncSel();
  wallSyncFiltersUI();
  wallSyncSortUI();
  wallSyncScopeUI();
  wallPaintSearchClear();
}

// 翻页: 页码越界钳制到有效范围; 页码输入框由重新渲染复位
function wallGoPage(n) {
  const pages = Math.max(1, Math.ceil(WFA.results.length / WFA.per));
  const next = Math.min(Math.max(1, n | 0), pages);
  if (next === WFA.page) { wallFilesPaint(); return; }
  WFA.page = next;
  wallFilesPaint();
  $('#wf-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function wallFilesToggle(cb) {
  if (cb.checked) WF_SEL.add(cb.dataset.name); else WF_SEL.delete(cb.dataset.name);
  cb.closest('.wf-card')?.classList.toggle('is-selected', cb.checked);
  wallFilesSyncSel();
}
function wallFilesSyncSel() {
  const n = WF_SEL.size;
  const selRow = document.getElementById('wf-selection-row');
  const filterRow = document.getElementById('wf-extra-tools');
  if (selRow) selRow.hidden = n === 0;
  if (filterRow) filterRow.hidden = n > 0;
  const count = document.getElementById('wf-sel-count');
  if (count) count.textContent = n ? `已选 ${n} 张` : '未选择';
  const recycle = document.getElementById('wf-recycle');
  if (recycle) recycle.disabled = !n;
  const rename = document.getElementById('wf-rename');
  if (rename) rename.disabled = n !== 1;
  const moreBatch = document.getElementById('wf-more-batch');
  if (moreBatch) {
    moreBatch.hidden = n === 0;
    moreBatch.textContent = n ? `操作（已选 ${n} 张）` : '操作（未选择）';
  }
  // 选中后第二行切成操作行, 筛选/扫描菜单不再留在屏上
  if (n > 0) wfCloseMenus();
}
function wallSelectVisible() { WFA.visible.filter(f => f.kind !== 'library').forEach(f => WF_SEL.add(f.name)); wallFilesPaint(); }
function wallClearSelection() { WF_SEL.clear(); wallFilesPaint(); }

function wallSelectionInfo() {
  const files = (WFD?.items || []).filter(f => WF_SEL.has(f.name));
  if (files.length === 1) return wallDetails(files[0].name);
  wallModal(`所选 ${files.length} 张壁纸的信息`, files.map(f => `<div class="wf-info-row"><strong>${esc(f.name)}</strong><p>${f.width}×${f.height} · ${fmtSize(f.size)} · ${esc(f.format)} · ${esc(f.title || '未归属')}</p><button class="btn" data-wall-action="preview" data-name="${esc(f.name)}">查看原图</button><button class="btn" data-wall-action="work" data-name="${esc(f.name)}">所属作品</button>${/^https?:\/\//i.test(f.source_url || '') ? `<a class="btn" href="${esc(f.source_url)}" target="_blank" rel="noopener noreferrer">来源链接 ↗</a>` : ''}</div>`).join(''));
}

function wallFilesPreview(name) {
  const f = WFD?.items.find(x => x.name === name);
  if (!f) return;
  openLightbox({ page_num: 0, url: f.url, width: f.width, height: f.height, file_size: f.size,
    title: f.name, author_name: f.author_name, is_r18: f.is_r18 == null ? 1 : f.is_r18, artwork_id: '' }, null, null);
}

function wallModal(title, html) {
  ++WFA.workSeq;
  openLibModal(title, `<div id="wf-modal-content">${html}</div><p id="wf-modal-error" class="err-text" role="alert"></p>`);
  $('#wf-modal-content').addEventListener('click', e => {
    const action = e.target.closest('[data-wall-action]');
    if (action) wallAction(action.dataset.wallAction, [action.dataset.name]);
  });
}
function wallError(e) { const el = $('#wf-modal-error'); if (el) el.textContent = e.message || String(e); else toast(String(e), 'err'); }
function wallCancel() { if (!WFA.busy) { ++WFA.workSeq; closeLibModal(); } }
function wallFooter(text = '保存') { return `<div class="modal-actions"><button class="btn ghost" onclick="wallCancel()">取消</button><button id="wf-save" class="btn accent">${text}</button></div>`; }
function wallBindSave(fn) {
  $('#wf-save').addEventListener('click', async () => {
    if (WFA.busy) return;
    WFA.busy = true; $('#lib-modal').dataset.wallBusy = '1'; $('#wf-save').disabled = true; $('#wf-modal-error').textContent = '';
    try { const result = await fn(); if (result === false) return; ++WFA.workSeq; delete $('#lib-modal').dataset.wallBusy; closeLibModal(); await wallFilesLoad(); loadTrashBadge(); }
    catch (e) { wallError(e); }
    finally { delete $('#lib-modal').dataset.wallBusy; WFA.busy = false; if ($('#wf-save')) $('#wf-save').disabled = false; }
  });
}
function wallDetails(name) {
  if (WFA.busy) return;
  const f = WFD?.items.find(x => x.name === name);
  if (!f) return;
  const source = /^https?:\/\//i.test(f.source_url || '') ? f.source_url : '';
  wallModal('壁纸详情与操作', `<div class="wf-detail"><img class="wf-detail-img" src="${esc(safeThumb(f.thumb, f))}" alt="壁纸预览"><div>
    <strong>${esc(f.name)}</strong><p>${f.width} × ${f.height} · ${fmtSize(f.size)} · ${esc(f.format)}</p>
    <p>所属作品：${esc(f.artwork_id ? (f.title || f.artwork_id) : '未归属')}${f.page_num == null ? '' : ' · 第 ' + (f.page_num + 1) + ' 张'}</p>
    <p>自定义分组：${esc(f.group_name || '未分组')}</p><p>标记：${esc((f.labels || []).join('、') || '无')}</p>
    ${source ? `<a class="btn" href="${esc(source)}" target="_blank" rel="noopener noreferrer">查看来源链接 ↗</a>` : '<span class="muted">尚无来源链接</span>'}</div></div>
    <div class="wf-detail-actions">${[
      ['设置', [['desktop', '设为桌面壁纸']]],
      ['归属', [['work', '查看所属作品'], ['owner', '改归属'], ['group', '移动分组']]],
      ['信息', [['preview', '查看原图']]],
      ['整理', [['rename', '重命名'], [f.favorite ? 'unfavorite' : 'favorite', f.favorite ? '取消收藏' : '收藏'], ['labels', '标记'], ['trash', '移出壁纸库']]],
      ['删除', [['delete', '删除…']]]
    ].map(([label, actions]) => `<div><h4>${label}</h4>${actions.map(([id, title]) => `<button class="btn${id === 'delete' ? ' danger' : ''}" data-wall-action="${id}" data-name="${esc(name)}">${title}</button>`).join('')}</div>`).join('')}</div>`);
}

async function wallAction(action, explicit) {
  if (WFA.busy) return;
  const names = explicit || [...WF_SEL], files = (WFD?.items || []).filter(f => names.includes(f.name));
  if (!files.length) return;
  const postEdit = payload => wallPost('/api/wallpaper/files/edit', { names, ...payload });
  const first = files[0];
  if (action === 'preview') { closeLibModal(); wallFilesPreview(first.name); return; }
  if (action === 'work') {
    if (!first.artwork_id) { toast('该壁纸尚未归属作品', 'info'); return; }
    closeLibModal(); switchTab('library'); $('#lib-search-input').value = first.artwork_id; loadLibrary(); return;
  }
  if (action === 'favorite' || action === 'unfavorite') {
    try { await postEdit({ favorite: action === 'favorite' }); closeLibModal(); await wallFilesLoad(); toast(action === 'favorite' ? '已收藏' : '已取消收藏', 'ok'); }
    catch (e) { toast(e.message, 'err'); } return;
  }
  if (action === 'desktop') {
    wallModal('设为桌面壁纸', `<p>选择要使用的一个版本，确认后设置。</p>
      <div class="wf-setting-grid">${files.map(f => `<label><input type="radio" name="wf-target" value="${esc(f.name)}"><img src="${esc(safeThumb(f.thumb, f))}" alt=""><span>${esc(f.name)}${f.current ? ' · 当前桌面' : ''}</span></label>`).join('')}</div>
      <button id="wf-setting-preview" class="btn">预览所选原图</button>${wallFooter('确认设置')}`);
    $('#wf-setting-preview').onclick = () => { const n = $('input[name="wf-target"]:checked')?.value; if (n) wallFilesPreview(n); };
    $('#wf-save').disabled = true;
    $$('input[name="wf-target"]').forEach(r => r.onchange = () => { $('#wf-save').disabled = false; });
    if (files.length === 1) { $('input[name="wf-target"]').checked = true; $('#wf-save').disabled = false; }
    wallBindSave(async () => {
      const name = $('input[name="wf-target"]:checked')?.value;
      if (!name) throw new Error('请选择一张壁纸');
      await wallPost('/api/wallpaper/use', { name });
      toast('已设为桌面壁纸', 'ok');
    }); return;
  }
  if (action === 'owner') {
    wallModal(`修改 ${files.length} 张壁纸的作品归属`, `<p>只指定作品时，图片保持未指定；选择具体图片会影响同图判重。</p>
      <input id="wf-owner-query" type="search" placeholder="搜索作品名、ID 或作者" aria-label="搜索所属作品">
      <select id="wf-owner" size="6" aria-label="目标作品"></select>
      <label class="wf-form-row">作品中的图片<select id="wf-owner-page"><option value="">不指定具体图片</option></select></label>${wallFooter()}`);
    const input = $('#wf-owner-query');
    const search = async () => {
      if ($('#wf-owner-query') !== input || WFA.busy) return;
      const seq = ++WFA.workSeq;
      try {
        const r = await api('/api/wallpaper/works?q=' + encodeURIComponent(input.value));
        if (seq !== WFA.workSeq || !$('#wf-owner')) return;
        if (!r.ok) throw new Error(r.msg || '作品搜索失败');
        WFA.works = r.items;
        $('#wf-owner').innerHTML = '<option value="">解除作品归属</option>' + r.items.map(w => `<option value="${esc(w.artwork_id)}">${esc(w.title || w.artwork_id)} · #${esc(w.artwork_id)} · ${esc(w.author_name)} · ${w.wallpaper_count} 张壁纸</option>`).join('');
        $('#wf-owner').selectedIndex = -1;
        $('#wf-owner-page').innerHTML = '<option value="">不指定具体图片</option>';
        $('#wf-save').disabled = true;
      } catch (e) { if (seq === WFA.workSeq) wallError(e); }
    };
    let timer;
    $('#wf-owner-query').oninput = () => { ++WFA.workSeq; clearTimeout(timer); timer = setTimeout(search, 200); };
    $('#wf-owner').onchange = () => {
      $('#wf-save').disabled = false;
      const w = WFA.works.find(x => x.artwork_id === $('#wf-owner').value);
      $('#wf-owner-page').innerHTML = '<option value="">不指定具体图片</option>' + (w?.pages || []).map(p => `<option value="${p}">第 ${p + 1} 张图片</option>`).join('');
    };
    wallBindSave(() => { if ($('#wf-owner').selectedIndex < 0) throw new Error('请选择目标作品或解除归属'); return postEdit({ artwork_id: $('#wf-owner').value, page_num: $('#wf-owner-page').value === '' ? null : Number($('#wf-owner-page').value) }); });
    search(); return;
  }
  if (action === 'group') {
    wallModal(`移动 ${files.length} 张壁纸到分组`, `<label class="wf-form-row">自定义分组<select id="wf-edit-group"><option value="">未分组</option>${WFA.groups.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join('')}</select></label><p class="hint">分组调整不改变所属作品。</p>${wallFooter()}`);
    if (files.length === 1) $('#wf-edit-group').value = first.group_id || '';
    wallBindSave(() => postEdit({ group_id: $('#wf-edit-group').value ? Number($('#wf-edit-group').value) : null })); return;
  }
  if (action === 'labels') {
    wallModal(`标记 ${files.length} 张壁纸`, `<p>用逗号分隔标记，留空则清除；保存后替换所选壁纸的标记。</p><input id="wf-labels" type="text" value="${esc(files.length === 1 ? (first.labels || []).join('，') : '')}" placeholder="例如：待整理，已检查">${wallFooter()}`);
    wallBindSave(() => postEdit({ labels: $('#wf-labels').value.split(/[,，]/).map(x => x.trim()).filter(Boolean) })); return;
  }
  if (action === 'rename') {
    wallModal('重命名壁纸文件', `<p>保留原扩展名，同名文件不会覆盖。</p><input id="wf-new-name" type="text" value="${esc(first.name)}">${wallFooter()}`);
    wallBindSave(async () => {
      const result = await wallPost('/api/wallpaper/files/rename', { names: [first.name], new_name: $('#wf-new-name').value });
      if (WF_SEL.delete(first.name)) WF_SEL.add(result.name);
      return result;
    }); return;
  }
  if (action === 'trash' || action === 'delete') {
    wallModal(action === 'trash' ? '移出壁纸库' : '删除壁纸', `<p>已选择 ${files.length} 张壁纸。</p>
      <label class="wf-delete-option"><input type="radio" name="wf-delete-mode" value="trash" checked> 从库中删除：移入应用回收站，可恢复文件和归属</label>
      ${action === 'delete' ? '<label class="wf-delete-option"><input type="radio" name="wf-delete-mode" value="disk"> 从磁盘删除：直接送入 Windows 回收站</label>' : ''}
      <p class="hint">图库原图不参与操作。当前桌面壁纸须先切换后再删除。</p>${wallFooter('确认移除')}`);
    wallBindSave(async () => {
      const disk = $('input[name="wf-delete-mode"]:checked').value === 'disk';
      if (disk && !confirm(`将所选 ${files.length} 张壁纸文件送入 Windows 回收站？`)) return false;
      const ctl = await window.runWithProgress({
        key: 'wall-files-delete', host: '#op-progress-host',
        label: disk ? '删除壁纸' : '移出壁纸库', unit: '张',
        scanText: disk ? '正在送入 Windows 回收站…' : '正在移入应用回收站…',
        request: {
          run: () => wallPost(disk ? '/api/wallpaper/files/recycle'
                                   : '/api/wallpaper/files/trash', { names }),
          timeout: 180000,
        },
        onDone: (r) => {
          toast(disk ? `已送入 Windows 回收站 ${r.recycled} 张`
                     : `已移入应用回收站 ${r.moved} 张`, 'ok');
        },
        onError: () => {},
      });
      const r = ctl && ctl.result;
      if (!r) throw new Error((ctl && ctl.error) || '操作失败');
      if (r.failed?.length || r.refused?.length) {
        await wallFilesLoad();
        throw new Error([...(r.failed || []), ...(r.refused || [])].join('；'));
      }
      return r;
    });
  }
}

async function wallGroupsManage() {
  if (WFA.busy) return;
  wallModal('管理自定义分组', `<p>分组独立于所属作品；删除分组只解除分组关系。</p><div class="wf-group-editor">${WFA.groups.map(g =>
    `<div><input data-group-name="${g.id}" value="${esc(g.name)}" aria-label="分组名"><button class="btn" data-group-save="${g.id}">改名</button><button class="btn danger" data-group-delete="${g.id}">删除</button></div>`).join('')}</div>
    <div class="wf-group-create"><input id="wf-new-group" placeholder="新分组名称"><button class="btn accent" id="wf-create-group">新建分组</button></div>`);
  const save = async (url, data) => {
    if (WFA.busy) return;
    WFA.busy = true;
    try { await wallPost(url, data); await wallFilesLoad(); WFA.busy = false; wallGroupsManage(); } catch (e) { wallError(e); } finally { WFA.busy = false; }
  };
  $('#wf-create-group').onclick = () => save('/api/wallpaper/groups/save', { name: $('#wf-new-group').value });
  $('#wf-modal-content').addEventListener('click', e => {
    const s = e.target.closest('[data-group-save]'), d = e.target.closest('[data-group-delete]');
    if (s) save('/api/wallpaper/groups/save', { id: Number(s.dataset.groupSave), name: $(`input[data-group-name="${s.dataset.groupSave}"]`).value });
    if (d && confirm('删除该分组？组内壁纸改为未分组。')) save('/api/wallpaper/groups/delete', { id: Number(d.dataset.groupDelete) });
  });
}

let wallTrashItems = [];
async function wallTrashLoad() {
  const cont = $('#trash-result');
  try {
    const r = await api('/api/wallpaper/trash');
    if (!r?.ok) throw new Error(r?.msg || '读取失败');
    wallTrashItems = r.items || [];
    $('#trash-total').textContent = `共 ${wallTrashItems.length} 张壁纸`;
    $('#trash-restore-all').disabled = $('#trash-purge-all').disabled = !wallTrashItems.length;
    cont.innerHTML = wallTrashItems.length ? '<div class="wf-trash-list">' + wallTrashItems.map(f => `<article><img src="${esc(safeThumb(f.thumb, f))}" alt=""><div><strong>${esc(f.name)}</strong><p>${fmtSize(f.size)} · ${esc(f.trashed_at)}</p></div><button class="btn" data-wall-restore="${f.id}">恢复</button><button class="btn danger" data-wall-purge="${f.id}">从磁盘删除</button></article>`).join('') + '</div>' : '<div class="hint">壁纸回收站是空的</div>';
    if (!cont._wallBound) {
      cont._wallBound = true;
      cont.addEventListener('click', e => {
        const b = e.target.closest('[data-wall-restore],[data-wall-purge]');
        if (b) wallTrashAct(b.dataset.wallRestore ? 'restore' : 'purge', [b.dataset.wallRestore || b.dataset.wallPurge]);
      });
    }
    loadTrashBadge();
  } catch (e) { cont.innerHTML = `<div class="hint err-text">${esc(e.message)}</div>`; }
}
async function wallTrashAct(action, ids) {
  if (WFA.busy) return;
  if (!confirm(action === 'restore' ? `恢复所选 ${ids.length} 张壁纸？同名冲突会跳过。` : `将所选 ${ids.length} 张壁纸送入 Windows 回收站？应用内记录会移除。`)) return;
  WFA.busy = true;
  try {
    await window.runWithProgress({
      key: 'wall-trash-act', host: '#op-progress-host',
      label: action === 'restore' ? '恢复壁纸' : '回收壁纸', unit: '张',
      scanText: action === 'restore' ? '正在恢复壁纸…' : '正在送入 Windows 回收站…',
      request: {
        run: () => wallPost('/api/wallpaper/trash/' + action, { ids }),
        timeout: 180000,
      },
      onDone: async (r) => {
        await wallTrashLoad(); await wallFilesLoad();
        if (r.failed?.length) {
          openLibModal('部分壁纸未处理',
            `<p>完成 ${r.done} 张，以下项目保留：</p><p>${r.failed.map(esc).join('<br>')}</p>`);
        } else {
          toast(`已${action === 'restore' ? '恢复' : '回收'} ${r.done} 张`, 'ok');
        }
      },
      onError: (e) => toast(e.message, 'err'),
    });
  } finally { WFA.busy = false; }
}
function wallTrashAll(action) { if (wallTrashItems.length) return wallTrashAct(action, wallTrashItems.map(x => x.id)); }
