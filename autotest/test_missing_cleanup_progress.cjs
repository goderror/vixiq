'use strict';

// 清理缺失作品共用进度: 直接运行 app.js 内的真实函数。
// 覆盖: 预览即时核对/空清单/图库不可用/请求失败、取消不执行、
// 复核→计数→回收→结果、跳过/失败/回收失败提示、快速完成、
// 关闭重开、短暂失败重试、串行查询、迟到回包与重新预览。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// 清理缺失: 预览只读; 确认后由后台任务复核';
const END = '// 同步磁盘索引结果';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少清理缺失进度代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  function makeEl(id) {
    const set = new Set();
    const handlers = {};
    const el = {
      id, innerHTML: '', textContent: '', dataset: {}, style: {},
      classList: {
        add: c => set.add(c),
        remove: c => set.delete(c),
        contains: c => set.has(c),
        toggle: (c, on) => {
          if (on === undefined) { set.has(c) ? set.delete(c) : set.add(c); }
          else if (on) set.add(c); else set.delete(c);
          return set.has(c);
        },
      },
      addEventListener(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
      querySelectorAll() { return []; },
    };
    return el;
  }
  const els = {};
  ['#lib-modal', '#lib-modal-body', '#miss-task', '#miss-progress',
   '#miss-note', '#miss-result']
    .forEach(s => { els[s] = makeEl(s); });
  els['#lib-modal'].style.display = 'none';
  function $(sel) {
    if (sel === '.miss-clean-cb') return checkboxes;
    return els[sel] || null;
  }
  let checkboxes = [];
  function $$(sel) {
    if (sel === '.miss-clean-cb') return checkboxes;
    return [];
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function openLibModal(title, html, opts) {
    els['#lib-modal'].style.display = 'flex';
    els['#lib-modal-body'].innerHTML = html;
    els['#lib-modal'].dataset.opOwner = '';
    opRevealProgressIn(els['#lib-modal-body']);
  }
  function closeLibModal() { els['#lib-modal'].style.display = 'none'; }
  function opClaimModal(owner) { els['#lib-modal'].dataset.opOwner = owner || ''; }

  let progressCalls = [];
  function opProgressHtml(wid, o) {
    return `<div class="op-progress" id="${wid}">${esc((o && o.text) || '')}</div>`;
  }
  function opProgressPaint(wid, o) {
    progressCalls.push({ wid, o: Object.assign({}, o) });
  }
  let revealCalls = [];
  function opRevealProgressIn(scope) { revealCalls.push(scope); }

  const store = {};
  const sessionStorage = {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  };

  let apiCalls = [];
  let previewHandler = async () => ({
    status: 'ok', items: [], total: 0, root_available: true,
    scan_incomplete: false, unreadable_dirs: [],
  });
  let postHandler = async () => ({
    status: 'ok', task: missingTask('checking', { id: 't1', total: 2 }),
  });
  async function api(url, opts) {
    apiCalls.push({ url, opts });
    if (String(url).includes('/api/library/missing-items')) {
      return previewHandler(url, opts);
    }
    if (String(url).includes('/api/library/clean-missing/task')) {
      return postHandler(url, opts);
    }
    return {};
  }
  function apiErr(r) {
    if (r && r.status === 'error') return r.error || r.message || '请求失败';
    return '';
  }

  let getCalls = [];
  let statusHandler = async () => ({
    status: 'ok', task: missingTask('checking', { id: 't1' }),
  });
  async function opFetchJson(url) {
    getCalls.push(url);
    return statusHandler(url);
  }

  let confirmAnswer = true;
  let confirmTexts = [];
  function confirm(text) { confirmTexts.push(text); return confirmAnswer; }
  let toasts = [];
  function toast(text, kind) { toasts.push([text, kind]); }
  let syncCalls = 0;
  function libSync() { syncCalls += 1; return Promise.resolve(); }
  const libSelected = { clear() {} };
  function loadLibrary() {}
  function loadStats() {}
  function loadLibFacets() {}

  const realSetTimeout = globalThis.setTimeout;
  let timers = [];
  let timerSeq = 0;
  function setTimeout(fn, ms) {
    timerSeq += 1;
    timers.push({ id: timerSeq, fn, ms });
    return timerSeq;
  }
  function clearTimeout(id) {
    timers = timers.filter(t => t.id !== id);
  }
  function timerCount() { return timers.length; }
  async function runNextTimer() {
    const t = timers.shift();
    if (!t) return false;
    await t.fn();
    return true;
  }

  const window = {};

  /*__MISS_SLICE__*/

  // 浏览器里挂在 window 上; Node 测试补成同名局部函数便于直接调用
  const openMissingCleanup = window.openMissingCleanup;
  const applyMissingCleanup = window.applyMissingCleanup;
  const missResetToPreview = window.missResetToPreview;

  const flush = () => new Promise(r => realSetTimeout(r, 0));
  function lastPaint(wid) {
    for (let i = progressCalls.length - 1; i >= 0; i -= 1) {
      if (progressCalls[i].wid === wid) return progressCalls[i].o;
    }
    return null;
  }
  function missingTask(stage, opt) {
    return Object.assign({
      id: 't1', status: 'running', stage, done: 0, total: 0,
      cleaned: 0, skipped: 0, failed: 0, recycled: 0, cleaned_ids: [],
      skipped_items: [], failed_items: [], recycle_failed: [], errors: [],
      current: '', error: '', recycle_files: false, ids: [],
      scan_incomplete: false, result: null,
    }, opt || {});
  }
  function doneTask(opt) {
    return missingTask('', Object.assign({
      status: 'done', stage: '', done: 2, total: 2,
      result: {
        total: 2, cleaned: 2, skipped: 0, failed: 0, cleaned_ids: ['1', '2'],
        skipped_items: [], failed_items: [], recycled: 0,
        recycle_failed: [], recycle_files: false, scan_incomplete: false,
      },
    }, opt || {}));
  }
  function reset() {
    progressCalls = [];
    revealCalls = [];
    apiCalls = [];
    getCalls = [];
    timers = [];
    toasts = [];
    confirmTexts = [];
    confirmAnswer = true;
    syncCalls = 0;
    checkboxes = [];
    _missTaskId = '';
    _missTimer = null;
    _missGen = 0;
    _missInFlight = false;
    _missStarting = false;
    _missStartSeq = 0;
    _missLast = null;
    _missRefreshedFor = '';
    Object.keys(store).forEach(k => delete store[k]);
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.style = {};
    });
    els['#lib-modal'].style.display = 'none';
    previewHandler = async () => ({
      status: 'ok', items: [], total: 0, root_available: true,
      scan_incomplete: false, unreadable_dirs: [],
    });
    postHandler = async () => ({
      status: 'ok', task: missingTask('checking', { id: 't1', total: 2 }),
    });
    statusHandler = async () => ({
      status: 'ok', task: missingTask('checking', { id: 't1' }),
    });
  }
  function bodyHtml() { return els['#lib-modal-body'].innerHTML; }
  function noteText() {
    const html = els['#miss-note'].innerHTML;
    if (html) return html.replace(/<[^>]*>/g, '');
    return els['#miss-note'].textContent;
  }
  function apiCount(kind) {
    return apiCalls.filter(c => String(c.url).includes(kind)).length;
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 预览即时核对 + 结果列出缺失作品与残留文件数
  test('预览立即显示核对流动，完成后列出缺失与残留文件', async () => {
    reset();
    let resolvePreview;
    previewHandler = () => new Promise(r => { resolvePreview = r; });
    const p = openMissingCleanup();
    await flush();
    assert.strictEqual(els['#lib-modal'].style.display, 'flex');
    assert.ok(bodyHtml().includes('正在核对缺失作品'), bodyHtml());
    assert.strictEqual(_missTaskId, '');
    resolvePreview({
      status: 'ok', root_available: true, scan_incomplete: false,
      unreadable_dirs: [],
      items: [
        { artwork_id: '1', title: '作品一', author_name: 'A', existing_files: 0,
          paths: [{ rel: 'a.jpg', exists: false }] },
        { artwork_id: '2', title: '作品二', author_name: 'B', existing_files: 1,
          paths: [{ rel: 'b.jpg', exists: true }] },
      ],
    });
    await p; await flush();
    assert.ok(bodyHtml().includes('作品一') && bodyHtml().includes('作品二'));
    assert.ok(bodyHtml().includes('残留文件 1 个'), bodyHtml());
    assert.ok(bodyHtml().includes('只清理索引记录'));
    assert.ok(bodyHtml().includes('清理索引并回收残留文件'));
    assert.strictEqual(window._missingPreview.length, 2);
  });

  // 2. 空清单 / 图库不可用 / 请求失败
  test('空清单、图库不可用与请求失败分别提示', async () => {
    reset();
    await openMissingPreview();
    assert.ok(bodyHtml().includes('没有被判定为「文件缺失」'), bodyHtml());

    reset();
    previewHandler = async () => ({
      status: 'error', error: '图库不可用', root_available: false,
    });
    await openMissingPreview();
    assert.ok(bodyHtml().includes('图库不可用'), bodyHtml());

    reset();
    previewHandler = async () => { throw new Error('网络断了'); };
    await openMissingPreview();
    assert.ok(bodyHtml().includes('读取失败：网络断了'), bodyHtml());
  });

  // 3. 取消确认不创建任务
  test('取消确认不发起执行', async () => {
    reset();
    checkboxes = [{ value: '1', checked: true }];
    confirmAnswer = false;
    await applyMissingCleanup(false);
    assert.strictEqual(apiCount('/api/library/clean-missing/task'), 0);
    assert.strictEqual(els['#lib-modal'].style.display, 'none');
    assert.ok(confirmTexts[0].includes('磁盘文件不动'));
  });

  // 4. 确认后立即受理 → 复核 → 计数 → 回收 → 结果
  test('执行按复核/清理/回收阶段展示并显示结果', async () => {
    reset();
    checkboxes = [{ value: '1', checked: true }, { value: '2', checked: true }];
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    const p = applyMissingCleanup(true);
    await flush();
    assert.strictEqual(apiCount('/api/library/clean-missing/task'), 1);
    assert.ok(confirmTexts[0].includes('Windows 回收站'));
    assert.ok(lastPaint('miss-flow').text.includes('正在提交清理任务'));
    resolvePost({
      status: 'ok',
      task: missingTask('checking', { id: 't1', total: 2, recycle_files: true }),
    });
    await p; await flush();
    assert.strictEqual(store['vixiq.miss.task'], 't1');
    assert.strictEqual(timerCount(), 1);
    assert.ok(lastPaint('miss-flow').text.includes('正在复核所选作品'));

    statusHandler = async () => ({
      status: 'ok',
      task: missingTask('cleaning', {
        id: 't1', done: 1, total: 2, cleaned: 1, recycle_files: true,
      }),
    });
    await runNextTimer(); await flush();
    assert.ok(lastPaint('miss-count').text.includes('已处理 1／2 个作品'));
    assert.strictEqual(lastPaint('miss-count').hidden, false);
    assert.ok(lastPaint('miss-flow').text.includes('正在清理索引记录'));

    statusHandler = async () => ({
      status: 'ok',
      task: missingTask('recycling', {
        id: 't1', done: 2, total: 2, cleaned: 2, recycled: 1,
        recycle_files: true,
      }),
    });
    await runNextTimer(); await flush();
    assert.ok(lastPaint('miss-flow').text.includes('正在回收残留文件'));
    assert.ok(lastPaint('miss-count').text.includes('已处理 2／2 个作品'));

    statusHandler = async () => ({
      status: 'ok',
      task: doneTask({
        id: 't1', recycle_files: true,
        result: {
          total: 2, cleaned: 2, skipped: 0, failed: 0,
          cleaned_ids: ['1', '2'], skipped_items: [], failed_items: [],
          recycled: 1, recycle_failed: [], recycle_files: true,
          scan_incomplete: false,
        },
      }),
    });
    await runNextTimer(); await flush();
    assert.ok(noteText().includes('已清理 2 条缺失记录'), noteText());
    assert.ok(noteText().includes('已回收残留文件 1 个'), noteText());
    assert.ok(noteText().includes('重新预览'));
    assert.strictEqual(timerCount(), 0);
    assert.strictEqual(syncCalls, 1);
  });

  // 5. 跳过/失败/回收失败分别提示
  test('跳过、失败与回收失败分别给出原因', async () => {
    reset();
    statusHandler = async () => ({
      status: 'ok',
      task: doneTask({
        id: 't1', recycle_files: true,
        result: {
          total: 3, cleaned: 1, skipped: 1, failed: 1,
          cleaned_ids: ['1'],
          skipped_items: [{ artwork_id: '2', reason: '文件已恢复或已不缺失' }],
          failed_items: [{ artwork_id: '3', reason: '清理记录失败：disk' }],
          recycled: 0, recycle_failed: ['C:/x.jpg'],
          recycle_files: true, scan_incomplete: true,
        },
      }),
    });
    _missTaskId = 't1';
    missTaskShell('正在读取清理任务…');
    await loadMissTask(true); await flush();
    const text = noteText();
    assert.ok(text.includes('记录已清理，但 1 个残留文件'), text);
    assert.ok(text.includes('文件已恢复或已不缺失'), text);
    assert.ok(text.includes('清理记录失败：disk'), text);
    assert.ok(text.includes('本次扫描不完整'), text);
    assert.ok(text.includes('C:/x.jpg'), text);
  });

  // 6. 很快完成直接显示结果
  test('快速完成直接显示结果', async () => {
    reset();
    checkboxes = [{ value: '1', checked: true }];
    postHandler = async () => ({
      status: 'ok',
      task: doneTask({ id: 'f1', done: 1, total: 1,
        result: { total: 1, cleaned: 1, skipped: 0, failed: 0,
                  cleaned_ids: ['1'], skipped_items: [], failed_items: [],
                  recycled: 0, recycle_failed: [], recycle_files: false,
                  scan_incomplete: false } }),
    });
    await applyMissingCleanup(false);
    await flush();
    assert.ok(noteText().includes('已清理 1 条缺失记录'), noteText());
    assert.strictEqual(timerCount(), 0);
    assert.strictEqual(getCalls.length, 0);
  });

  // 7. 关闭后重开接回任务
  test('关闭后重开凭任务号接回结果', async () => {
    reset();
    _missTaskId = 't1';
    store['vixiq.miss.task'] = 't1';
    statusHandler = async () => ({ status: 'ok', task: doneTask({ id: 't1' }) });
    closeLibModal();
    await openMissingCleanup();
    await flush();
    assert.strictEqual(els['#lib-modal'].style.display, 'flex');
    assert.ok(getCalls.some(u => String(u).includes('id=t1')), getCalls);
    assert.ok(noteText().includes('已清理 2 条缺失记录'), noteText());
  });

  // 8. 短暂查询失败保留进度并重试
  test('短暂查询失败保留进度并自动重试', async () => {
    reset();
    checkboxes = [{ value: '1', checked: true }];
    _missTaskId = 't1';
    missTaskShell('正在读取清理任务…');
    _missLast = missingTask('cleaning', { id: 't1', done: 1, total: 2, cleaned: 1 });
    let rejectOnce = true;
    statusHandler = async () => {
      if (rejectOnce) { rejectOnce = false; throw new Error('任务查询超时'); }
      return { status: 'ok', task: doneTask({ id: 't1' }) };
    };
    await loadMissTask(true); await flush();
    assert.ok(lastPaint('miss-count').text.includes('已处理 1／2 个作品'));
    assert.strictEqual(timerCount(), 1);
    await runNextTimer(); await flush();
    assert.ok(noteText().includes('已清理 2 条缺失记录'), noteText());
  });

  // 9. 上一查询未结束时不发下一次
  test('查询串行，不并发', async () => {
    reset();
    _missTaskId = 't1';
    missTaskShell('正在读取清理任务…');
    let resolveGet;
    statusHandler = () => new Promise(r => { resolveGet = r; });
    const p = loadMissTask(true);
    await flush();
    assert.strictEqual(getCalls.length, 1);
    const before = getCalls.length;
    await loadMissTask(false);
    await startMissPolling();
    assert.strictEqual(getCalls.length, before);
    resolveGet({ status: 'ok', task: doneTask({ id: 't1' }) });
    await p; await flush();
  });

  // 10. 迟到回包不得覆盖新预览
  test('旧任务迟到回包不覆盖重新预览', async () => {
    reset();
    _missTaskId = 't1';
    missTaskShell('正在读取清理任务…');
    let resolveOld;
    statusHandler = () => new Promise(r => { resolveOld = r; });
    const p = loadMissTask(true);
    await flush();
    let resolvePreview;
    previewHandler = () => new Promise(r => { resolvePreview = r; });
    missResetToPreview();
    await flush();
    assert.ok(bodyHtml().includes('正在核对缺失作品'), bodyHtml());
    resolveOld({ status: 'ok', task: doneTask({ id: 't1' }) });
    await p; await flush();
    assert.ok(bodyHtml().includes('正在核对缺失作品'), bodyHtml());
    assert.ok(!bodyHtml().includes('已清理 2 条缺失记录'), bodyHtml());
    resolvePreview({ status: 'ok', items: [], root_available: true,
                     scan_incomplete: false, unreadable_dirs: [] });
    await flush();
  });

  // 11. 重新预览按钮清除任务号
  test('重新预览清除会话任务号并回到预览', async () => {
    reset();
    _missTaskId = 't1';
    store['vixiq.miss.task'] = 't1';
    statusHandler = async () => ({ status: 'ok', task: doneTask({ id: 't1' }) });
    missTaskShell('正在读取清理任务…');
    await loadMissTask(true); await flush();
    missResetToPreview();
    await flush();
    assert.strictEqual(_missTaskId, '');
    assert.strictEqual(store['vixiq.miss.task'], undefined);
    assert.ok(bodyHtml().includes('正在核对缺失作品')
              || bodyHtml().includes('没有被判定为「文件缺失」'), bodyHtml());
  });

  // 12. 同一次执行只 POST 一次
  test('执行中的重复点击只提交一次', async () => {
    reset();
    checkboxes = [{ value: '1', checked: true }];
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    const p = applyMissingCleanup(false);
    await flush();
    const second = applyMissingCleanup(false);
    await second; await flush();
    assert.strictEqual(apiCount('/api/library/clean-missing/task'), 1);
    resolvePost({ status: 'ok', task: doneTask({ id: 'd1', done: 1, total: 1 }) });
    await p; await flush();
  });

  (async () => {
    let failed = 0;
    for (const [name, fn] of cases) {
      try {
        await fn();
        console.log('ok - ' + name);
      } catch (e) {
        failed += 1;
        console.error('not ok - ' + name);
        console.error(e && e.stack || e);
      }
    }
    if (failed) {
      console.error(failed + ' failed');
      process.exit(1);
    }
    console.log('NODE OK');
  })();
}

const body = '(' + harnessBody.toString()
  .replace('/*__MISS_SLICE__*/', blockSrc) + ')();\n';
new Function('assert', body)(assert);
