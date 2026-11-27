'use strict';

// 设置页「清理临时目录」共用进度: 直接运行 app.js 内的真实函数。
// 覆盖: 点击即时受理/按钮锁定、扫描流动、锁定顶层项后计数、
// 结果分类与空根/不可读、重复点击接回、切页凭任务号接回、
// 查询超时重试、串行查询、迟到回包、404 清会话、快速完成无闪、统计失败保留结果。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// 设置页临时目录清理: 后台任务 + 共用进度';
const END = 'async function clearPathDir(which)';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少临时目录清理进度代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  function makeEl(id) {
    const set = new Set();
    const el = {
      id, innerHTML: '', textContent: '', dataset: {}, style: {}, disabled: false,
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
      querySelectorAll() { return []; },
    };
    return el;
  }
  const els = {};
  ['#path-temp-clean', '#path-temp-task', '#path-temp-progress',
   '#path-temp-note', '#path-positions']
    .forEach(s => { els[s] = makeEl(s); });
  function $(sel) { return els[sel] || null; }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  let progressCalls = [];
  function opProgressHtml(wid, o) {
    return `<div class="op-progress" id="${wid}">${esc((o && o.text) || '')}</div>`;
  }
  function opProgressPaint(wid, o) {
    progressCalls.push({ wid, o: Object.assign({}, o) });
  }
  let revealCalls = [];
  function opRevealProgressIn(scope) { revealCalls.push(scope); }

  let pathsState = {};
  let paintPathsCalls = 0;
  function paintPaths() { paintPathsCalls += 1; }

  const store = {};
  const sessionStorage = {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  };

  let apiCalls = [];
  let postHandler = async () => ({
    status: 'ok', task: tempTask('scanning', { id: 't1' }),
  });
  let pathsHandler = async () => ({
    saved: {}, effective: { temp_root: 'C:/temp' }, sizes: { temp: 1 },
  });
  async function api(url, opts) {
    apiCalls.push({ url, opts });
    if (String(url).includes('/api/paths/temp/clean/task')) {
      return postHandler(url, opts);
    }
    if (String(url).includes('/api/paths')) return pathsHandler(url, opts);
    return {};
  }

  let getCalls = [];
  let statusHandler = async () => ({
    status: 'ok', task: tempTask('scanning', { id: 't1' }),
  });
  async function opFetchJson(url) {
    getCalls.push(url);
    return statusHandler(url);
  }

  let toasts = [];
  function toast(text, kind) { toasts.push([text, kind]); }

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

  /*__TEMP_SLICE__*/

  const flush = () => new Promise(r => globalThis.setTimeout(r, 0));
  function lastPaint(wid) {
    for (let i = progressCalls.length - 1; i >= 0; i -= 1) {
      if (progressCalls[i].wid === wid) return progressCalls[i].o;
    }
    return null;
  }
  function tempTask(stage, opt) {
    return Object.assign({
      id: 't1', status: 'running', stage, done: 0, total: 0, current: '',
      cleaned: 0, skipped: 0, failed: 0, freed_bytes: 0, freed_mb: 0.0,
      skipped_items: [], failed_items: [], errors: [], empty: false,
      error: '', root: 'C:/temp', result: null,
    }, opt || {});
  }
  function doneTask(opt) {
    return tempTask('', Object.assign({
      status: 'done', stage: '', done: 3, total: 3, cleaned: 2, skipped: 1,
      failed: 0, freed_bytes: 2048, freed_mb: 0.0,
      result: {
        total: 3, cleaned: 2, skipped: 1, failed: 0,
        freed_bytes: 2048, freed_mb: 0.0, empty: false,
        skipped_items: [{ name: 'used', reason: '下载任务正在使用' }],
        failed_items: [], root: 'C:/temp',
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
    paintPathsCalls = 0;
    pathsState = {};
    _tempCleanId = '';
    _tempCleanTimer = null;
    _tempCleanGen = 0;
    _tempCleanInFlight = false;
    _tempCleanStarting = false;
    _tempCleanLast = null;
    Object.keys(store).forEach(k => delete store[k]);
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.style = {};
      el.disabled = false;
    });
    els['#path-temp-clean'].textContent = '🧹 清理';
    els['#path-temp-task'].classList.add('hidden');
    postHandler = async () => ({
      status: 'ok', task: tempTask('scanning', { id: 't1' }),
    });
    statusHandler = async () => ({
      status: 'ok', task: tempTask('scanning', { id: 't1' }),
    });
    pathsHandler = async () => ({
      saved: {}, effective: { temp_root: 'C:/temp' }, sizes: { temp: 1 },
    });
  }
  function noteText() {
    const html = els['#path-temp-note'].innerHTML;
    if (html) return html.replace(/<[^>]*>/g, '');
    return els['#path-temp-note'].textContent;
  }
  function postCount() {
    return apiCalls.filter(c => String(c.url).includes('/api/paths/temp/clean/task')).length;
  }
  function pathsCount() {
    return apiCalls.filter(c => String(c.url) === '/api/paths').length;
  }
  function button() { return els['#path-temp-clean']; }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 点击即时受理 → 扫描流动 → 锁定总数后计数 → 结果
  test('点击即时受理，扫描后按顶层项计数并显示结果', async () => {
    reset();
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    const p = cleanTempDir();
    await flush();
    assert.strictEqual(button().disabled, true);
    assert.strictEqual(button().textContent, '清理中…');
    assert.ok(lastPaint('temp-clean-flow').text.includes('正在核对临时目录'));
    assert.ok(revealCalls.length >= 1);
    resolvePost({ status: 'ok', task: tempTask('scanning', { id: 't1' }) });
    await p; await flush();
    assert.strictEqual(store['vixiq.tempclean.task'], 't1');
    assert.strictEqual(timerCount(), 1);

    statusHandler = async () => ({
      status: 'ok',
      task: tempTask('cleaning', { id: 't1', total: 3, current: 'folder' }),
    });
    await runNextTimer(); await flush();
    assert.strictEqual(lastPaint('temp-clean-count').hidden, false);
    assert.ok(lastPaint('temp-clean-count').text.includes('已处理 0／3 项'));
    assert.ok(lastPaint('temp-clean-flow').text.includes('当前：folder'));

    statusHandler = async () => ({
      status: 'ok',
      task: tempTask('cleaning', {
        id: 't1', total: 3, done: 1, cleaned: 1, current: 'b',
      }),
    });
    await runNextTimer(); await flush();
    assert.ok(lastPaint('temp-clean-count').text.includes('已处理 1／3 项'));

    statusHandler = async () => ({ status: 'ok', task: doneTask({ id: 't1' }) });
    await runNextTimer(); await flush();
    assert.strictEqual(timerCount(), 0);
    assert.strictEqual(button().disabled, false);
    assert.strictEqual(button().textContent, '🧹 清理');
    assert.ok(noteText().includes('清理 2 项 · 跳过 1 项 · 失败 0 项'), noteText());
    assert.ok(noteText().includes('释放 2.0 KB'), noteText());
    assert.ok(noteText().includes('下载任务正在使用'), noteText());
    assert.strictEqual(pathsCount(), 1);
    assert.strictEqual(paintPathsCalls, 1);
  });

  // 2. 空根 / 不可读
  test('空根与不可读分别给出结果', async () => {
    reset();
    statusHandler = async () => ({
      status: 'ok',
      task: doneTask({
        id: 't1', done: 0, total: 0, cleaned: 0, skipped: 0,
        result: { total: 0, cleaned: 0, skipped: 0, failed: 0,
                  freed_bytes: 0, freed_mb: 0.0, empty: true,
                  skipped_items: [], failed_items: [], root: 'C:/temp' },
      }),
    });
    _tempCleanId = 't1';
    await loadTempCleanTask(); await flush();
    assert.ok(noteText().includes('临时目录为空'), noteText());

    reset();
    statusHandler = async () => ({
      status: 'ok',
      task: tempTask('', {
        id: 't1', status: 'error', total: 0,
        error: '无法读取临时目录：拒绝访问',
      }),
    });
    _tempCleanId = 't1';
    await loadTempCleanTask(); await flush();
    assert.ok(noteText().includes('清理失败：无法读取临时目录'), noteText());
    assert.strictEqual(button().disabled, false);
  });

  // 3. 失败与跳过逐项原因
  test('结果列出跳过与失败原因', async () => {
    reset();
    statusHandler = async () => ({
      status: 'ok',
      task: doneTask({
        id: 't1', done: 3, total: 3, cleaned: 1, skipped: 1, failed: 1,
        result: {
          total: 3, cleaned: 1, skipped: 1, failed: 1,
          freed_bytes: 1024, freed_mb: 0.0, empty: false,
          skipped_items: [{ name: 'downloads', reason: '下载任务正在使用' }],
          failed_items: [{ name: 'locked', reason: '部分文件未能删除：拒绝访问' }],
          root: 'C:/temp',
        },
      }),
    });
    _tempCleanId = 't1';
    await loadTempCleanTask(); await flush();
    const text = noteText();
    assert.ok(text.includes('清理 1 项 · 跳过 1 项 · 失败 1 项'), text);
    assert.ok(text.includes('下载任务正在使用'), text);
    assert.ok(text.includes('部分文件未能删除'), text);
    assert.ok(text.includes('占用或权限不足'), text);
  });

  // 4. 重复点击只提交一次
  test('执行中的重复点击只提交一次并接回', async () => {
    reset();
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    const p = cleanTempDir();
    await flush();
    await cleanTempDir();
    await flush();
    assert.strictEqual(postCount(), 1);
    resolvePost({ status: 'ok', task: tempTask('scanning', { id: 't1' }) });
    await p; await flush();
    assert.strictEqual(timerCount(), 1);
  });

  // 5. 切页返回凭任务号接回
  test('切页返回凭任务号接回结果', async () => {
    reset();
    _tempCleanId = 't1';
    store['vixiq.tempclean.task'] = 't1';
    statusHandler = async () => ({ status: 'ok', task: doneTask({ id: 't1' }) });
    await loadTempCleanTask(); await flush();
    assert.ok(getCalls.some(u => String(u).includes('id=t1')), getCalls);
    assert.ok(noteText().includes('清理 2 项'), noteText());
    assert.strictEqual(button().disabled, false);
  });

  // 6. 查询超时保留进度并重试
  test('查询超时保留最后进度并自动重试', async () => {
    reset();
    _tempCleanId = 't1';
    _tempCleanLast = tempTask('cleaning', {
      id: 't1', total: 3, done: 1, cleaned: 1,
    });
    let rejectOnce = true;
    statusHandler = async () => {
      if (rejectOnce) { rejectOnce = false; throw new Error('任务查询超时'); }
      return { status: 'ok', task: doneTask({ id: 't1' }) };
    };
    await loadTempCleanTask(); await flush();
    assert.ok(lastPaint('temp-clean-count').text.includes('已处理 1／3 项'));
    assert.strictEqual(timerCount(), 1);
    await runNextTimer(); await flush();
    assert.ok(noteText().includes('清理 2 项'), noteText());
  });

  // 7. 查询串行
  test('查询串行，不并发', async () => {
    reset();
    _tempCleanId = 't1';
    let resolveGet;
    statusHandler = () => new Promise(r => { resolveGet = r; });
    const p = loadTempCleanTask();
    await flush();
    assert.strictEqual(getCalls.length, 1);
    await loadTempCleanTask();
    startTempCleanPolling();
    assert.strictEqual(getCalls.length, 1);
    resolveGet({ status: 'ok', task: doneTask({ id: 't1' }) });
    await p; await flush();
  });

  // 8. 迟到回包不覆盖新任务
  test('旧任务迟到回包不覆盖新任务', async () => {
    reset();
    _tempCleanId = 'old';
    let resolveOld;
    statusHandler = () => new Promise(r => { resolveOld = r; });
    const p = loadTempCleanTask();
    await flush();
    stopTempCleanPolling();
    _tempCleanId = 'new';
    _tempCleanLast = doneTask({ id: 'new' });
    paintTempCleanTask(_tempCleanLast);
    const before = noteText();
    resolveOld({ status: 'ok', task: doneTask({ id: 'old' }) });
    await p; await flush();
    assert.strictEqual(noteText(), before);
  });

  // 9. 404 清会话且不锁按钮
  test('任务号失效时清会话并恢复按钮', async () => {
    reset();
    _tempCleanId = 't1';
    store['vixiq.tempclean.task'] = 't1';
    statusHandler = async () => ({ status: 'idle', task: null });
    await loadTempCleanTask(); await flush();
    assert.strictEqual(_tempCleanId, '');
    assert.strictEqual(store['vixiq.tempclean.task'], undefined);
    assert.strictEqual(button().disabled, false);
    assert.ok(els['#path-temp-task'].classList.contains('hidden'));
  });

  // 10. 快速完成不启动轮询
  test('快速完成直接出结果且不启动轮询', async () => {
    reset();
    postHandler = async () => ({ status: 'ok', task: doneTask({ id: 'f1' }) });
    await cleanTempDir(); await flush();
    assert.strictEqual(timerCount(), 0);
    assert.strictEqual(getCalls.length, 0);
    assert.ok(noteText().includes('清理 2 项'), noteText());
    assert.strictEqual(button().disabled, false);
  });

  // 11. 统计刷新失败保留清理结果
  test('路径统计刷新失败时保留清理结果', async () => {
    reset();
    statusHandler = async () => ({ status: 'ok', task: doneTask({ id: 't1' }) });
    pathsHandler = async () => { throw new Error('统计超时'); };
    _tempCleanId = 't1';
    await loadTempCleanTask(); await flush();
    assert.ok(noteText().includes('清理 2 项'), noteText());
    assert.strictEqual(paintPathsCalls, 0);
  });

  // 12. 迟到回包不能重新锁住按钮
  test('迟到回包不能让按钮重新锁住', async () => {
    reset();
    _tempCleanId = 'old';
    let resolveOld;
    statusHandler = () => new Promise(r => { resolveOld = r; });
    const p = loadTempCleanTask();
    await flush();
    stopTempCleanPolling();
    _tempCleanLast = doneTask({ id: 'new' });
    paintTempCleanTask(_tempCleanLast);
    assert.strictEqual(button().disabled, false);
    resolveOld({ status: 'ok', task: tempTask('cleaning', { id: 'old', total: 3 }) });
    await p; await flush();
    assert.strictEqual(button().disabled, false);
  });

  // 13. 提交失败恢复按钮
  test('提交失败恢复按钮并显示错误', async () => {
    reset();
    postHandler = async () => { throw new Error('网络断了'); };
    await cleanTempDir(); await flush();
    assert.strictEqual(button().disabled, false);
    assert.strictEqual(button().textContent, '🧹 清理');
    assert.ok(noteText().includes('清理失败：网络断了'), noteText());
  });

  // 14. 完成后再次点击启动新任务
  test('完成后再次点击启动新任务', async () => {
    reset();
    _tempCleanId = 'old';
    _tempCleanLast = doneTask({ id: 'old' });
    paintTempCleanTask(_tempCleanLast);
    postHandler = async () => ({ status: 'ok', task: tempTask('scanning', { id: 'new' }) });
    await cleanTempDir(); await flush();
    assert.strictEqual(postCount(), 1);
    assert.strictEqual(_tempCleanId, 'new');
    assert.strictEqual(timerCount(), 1);
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
  .replace('/*__TEMP_SLICE__*/', blockSrc) + ')();\n';
new Function('assert', body)(assert);
