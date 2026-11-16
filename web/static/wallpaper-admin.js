// 壁纸文件管理：作品归属与自定义分组独立，所有图片遵循安全模式。
const WFA = { groups: [], page: 1, per: 80, results: [], visible: [], loading: 0, works: [], workSeq: 0, busy: false };

async function wallPost(url, data) {
  const r = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  if (!r || (r.ok !== true && r.status !== 'ok')) throw new Error(r?.msg || r?.error || '操作失败');
  return r;
}

function wallFilesEnter() {
  const root = $('#tab-wallpapers');
  if (!root._adminBound) {
    root._adminBound = true;
    root.addEventListener('click', e => {
      const b = e.target.closest('[data-f]');
      if (b) {
        WF_FILTER = b.dataset.f; WFA.page = 1;
        $$('#wf-filters [data-f]').forEach(x => x.classList.toggle('active', x === b));
        wallFilesPaint();
      }
      const more = e.target.closest('[data-wall-more]');
      if (more) wallDetails(more.dataset.wallMore);
      const preview = e.target.closest('[data-preview]');
      if (preview) wallFilesPreview(preview.dataset.preview);
      const page = e.target.closest('[data-wall-page]');
      if (page) { WFA.page = Number(page.dataset.wallPage); wallFilesPaint(); $('#wf-result').scrollIntoView({ block: 'start' }); }
    });
    root.addEventListener('change', e => {
      if (e.target.matches('input[data-name]')) wallFilesToggle(e.target);
      if (['wf-work-filter', 'wf-group-filter', 'wf-sort'].includes(e.target.id)) { WFA.page = 1; wallFilesPaint(); }
    });
    $('#wf-search').addEventListener('input', () => { WFA.page = 1; wallFilesPaint(); });
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
    const [d, groups] = await Promise.all([api('/api/wallpaper/files'), api('/api/wallpaper/groups')]);
    if (seq !== WFA.loading) return;
    if (!d?.ok || !groups?.ok) throw new Error(d?.error || groups?.msg || '读取失败');
    WFD = d; WFA.groups = groups.items || [];
    const alive = new Set(d.items.map(x => x.name));
    [...WF_SEL].forEach(n => { if (!alive.has(n)) WF_SEL.delete(n); });
    const works = new Map();
    d.items.forEach(x => { if (x.artwork_id) {
      const w = works.get(x.artwork_id) || { title: x.title, n: 0 }; w.n++; works.set(x.artwork_id, w);
    } });
    wallOptions($('#wf-work-filter'), '<option value="">所有作品</option>' + [...works].map(([id, w]) =>
      `<option value="${esc(id)}">${esc(w.title || id)} · ${w.n} 张</option>`).join(''));
    wallOptions($('#wf-group-filter'), '<option value="">所有分组</option><option value="none">未分组</option>' + WFA.groups.map(g =>
      `<option value="${g.id}">${esc(g.name)} · ${d.items.filter(x => x.group_id === g.id).length} 张</option>`).join(''));
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

function wallFilesPaint() {
  if (!WFD) return;
  const d = WFD, stats = d.stats || {}, q = $('#wf-search').value.trim().toLocaleLowerCase();
  $('#wf-dir').textContent = d.dir ? '壁纸夹 ' + d.dir : '尚未指定壁纸夹';
  $('#wf-scan-tip').textContent = d.scanned ? `共 ${stats.total} 张 · ${fmtSize(stats.bytes)} · 上次扫描 ${stats.scanned_at || '未知'}` : '扫描归属后可管理壁纸文件。';
  $('#wf-stats').innerHTML = [['壁纸', stats.total || 0], ['已归属', stats.owned || 0], ['未归属', stats.orphan || 0], ['重复图片', `${stats.dupes || 0} 组`]].map(([label, n]) =>
    `<div class="stat-card"><div class="num">${n}</div><div class="lbl">${label}</div></div>`).join('');
  const duplicateNames = new Set((d.groups || []).flatMap(g => g.files));
  const work = $('#wf-work-filter').value, group = $('#wf-group-filter').value;
  let list = (d.items || []).filter(f => {
    if (WF_FILTER === 'owned' && !f.artwork_id || WF_FILTER === 'orphan' && f.artwork_id || WF_FILTER === 'dupe' && !duplicateNames.has(f.name) || WF_FILTER === 'favorite' && !f.favorite) return false;
    if (work && f.artwork_id !== work || group === 'none' && f.group_id != null || group && group !== 'none' && String(f.group_id) !== group) return false;
    return !q || [f.name, f.title, f.artwork_id, f.author_name, f.group_name, ...(f.labels || [])].join(' ').toLocaleLowerCase().includes(q);
  });
  const sort = $('#wf-sort').value;
  list.sort((a, b) => sort === 'new' ? b.mtime - a.mtime : sort === 'size' ? b.size - a.size : a.name.localeCompare(b.name, 'zh-CN', { numeric: true }));
  WFA.results = list;
  const pages = Math.max(1, Math.ceil(list.length / WFA.per));
  WFA.page = Math.max(1, Math.min(WFA.page, pages));
  WFA.visible = list.slice((WFA.page - 1) * WFA.per, WFA.page * WFA.per);
  let content = '';
  if (WF_FILTER === 'dupe') {
    const visible = new Set(WFA.visible.map(x => x.name));
    content = (d.groups || []).map(g => {
      const files = list.filter(f => visible.has(f.name) && g.files.includes(f.name));
      if (!files.length) return '';
      const label = g.kind === 'page' ? `${g.title || g.artwork_id} · 第 ${Number(g.page_num) + 1} 张图片` : '同一张图片';
      return `<div class="wf-group"><div class="wf-group-head">${esc(label)} · ${g.files.length} 个版本</div><div class="wf-grid">${files.map(wallFilesCard).join('')}</div></div>`;
    }).join('');
  } else content = `<div class="wf-grid">${WFA.visible.map(wallFilesCard).join('')}</div>`;
  $('#wf-result').innerHTML = list.length ? content : '<div class="hint">没有符合条件的壁纸。</div>';
  $('#wf-pager').innerHTML = `<span>共 ${list.length} 张 · ${WFA.page} / ${pages} 页</span><button class="btn" data-wall-page="${WFA.page - 1}"${WFA.page === 1 ? ' disabled' : ''}>上一页</button><button class="btn" data-wall-page="${WFA.page + 1}"${WFA.page === pages ? ' disabled' : ''}>下一页</button>`;
  wallFilesSyncSel();
}

function wallFilesToggle(cb) {
  if (cb.checked) WF_SEL.add(cb.dataset.name); else WF_SEL.delete(cb.dataset.name);
  cb.closest('.wf-card')?.classList.toggle('is-selected', cb.checked);
  wallFilesSyncSel();
}
function wallFilesSyncSel() {
  const n = WF_SEL.size;
  const works = new Set((WFD?.items || []).filter(f => WF_SEL.has(f.name) && f.artwork_id).map(f => f.artwork_id)).size;
  $('#wf-sel-count').textContent = n ? `已选 ${n} 张 · ${works} 个作品` : '未选择';
  $('#wf-batch').classList.toggle('hidden', !n);
  $('#wf-recycle').disabled = !n;
  $('#wf-rename').disabled = n !== 1;
  // 未选中时整行操作区收起, 顶部只留搜索/全选/展开
  const selBox = document.querySelector('#tab-wallpapers .management-selection');
  if (selBox) selBox.hidden = n === 0;
}
function wallSelectVisible() { WFA.visible.forEach(f => WF_SEL.add(f.name)); wallFilesPaint(); }
function wallSelectResults() { WFA.results.forEach(f => WF_SEL.add(f.name)); wallFilesPaint(); }
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
      const r = await wallPost(disk ? '/api/wallpaper/files/recycle' : '/api/wallpaper/files/trash', { names });
      if (r.failed?.length || r.refused?.length) { await wallFilesLoad(); throw new Error([...(r.failed || []), ...(r.refused || [])].join('；')); }
      toast(disk ? `已送入 Windows 回收站 ${r.recycled} 张` : `已移入应用回收站 ${r.moved} 张`, 'ok');
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
    const r = await wallPost('/api/wallpaper/trash/' + action, { ids });
    await wallTrashLoad(); await wallFilesLoad();
    if (r.failed?.length) { openLibModal('部分壁纸未处理', `<p>完成 ${r.done} 张，以下项目保留：</p><p>${r.failed.map(esc).join('<br>')}</p>`); }
    else toast(`已${action === 'restore' ? '恢复' : '回收'} ${r.done} 张`, 'ok');
  } catch (e) { toast(e.message, 'err'); } finally { WFA.busy = false; }
}
function wallTrashAll(action) { if (wallTrashItems.length) return wallTrashAct(action, wallTrashItems.map(x => x.id)); }
