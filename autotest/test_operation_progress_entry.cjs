'use strict';

// 共用进度入口 runWithProgress: 请求 / 上传 / 后台任务三种接法。
// 覆盖: 300ms 阈值、流动/计数切换、按钮复位、串行查询与短暂失败重试、
// 重复点击只接回、关窗 detach 与重开 resume、上传字节/写入阶段/迟到隔离、
// 任务会话 id、业务结果回调只结算一次。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// ============ 共用进度入口';
const END = '// ============ 一键整理';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少共用进度入口代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  // ---- 极简 DOM: 支持 id 注册、classList、innerHTML 子控件、300ms 显示 ----
  const registry = new Map();
  function parseIds(html) {
    const ids = [];
    const re = /\sid="([^"]+)"/g;
    let m;
    while ((m = re.exec(String(html)))) ids.push(m[1]);
    return ids;
  }
  function parseProgressIds(html) {
    const ids = [];
    const re = /<div class="op-progress[^"]*"\s+id="([^"]+)"/g;
    let m;
    while ((m = re.exec(String(html)))) ids.push(m[1]);
    return ids;
  }
  function el(id) {
    const set = new Set();
    const kids = [];
    const obj = {
      id, dataset: {}, style: {}, disabled: false, value: '',
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
      querySelectorAll: sel => (sel === '.op-progress' ? kids.slice() : []),
      insertAdjacentHTML: (pos, html) => {
        obj.innerHTML = String(obj.innerHTML || '') + html;
      },
      get isConnected() { return true; },
    };
    let html = '', text = '';
    Object.defineProperty(obj, 'innerHTML', {
      get: () => html,
      set: v => {
        html = String(v);
        text = html.replace(/<[^>]*>/g, '');
        kids.length = 0;
        parseIds(html).forEach(pid => {
          if (!registry.has(pid)) registry.set(pid, el(pid));
        });
        parseProgressIds(html).forEach(pid => kids.push(registry.get(pid)));
      },
    });
    Object.defineProperty(obj, 'textContent', {
      get: () => text,
      set: v => { text = String(v); html = String(v); },
    });
    registry.set(id, obj);
    return obj;
  }
  const ids = ['#op-progress-host', '#h1', '#h2', '#note1', '#btn1', '#inp1'];
  ids.forEach(x => el(x.replace('#', '')));
  function $(sel) { return registry.get(String(sel).replace('#', '')) || null; }
  const document = {
    getElementById: id => registry.get(id) || null,
    querySelector: sel => $(sel),
    querySelectorAll: () => [],
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // ---- 真实 opProgressHtml / opProgressPaint / opRevealProgressIn 的等价实现 ----
  function opProgressHtml(wid, o) {
    o = o || {};
    const mode = o.mode || 'count';
    const hidden = o.hidden ? ' hidden' : '';
    const textId = o.textId || (wid + '-text');
    const fillId = o.fillId || (wid + '-fill');
    const liveId = o.liveId || (wid + '-live');
    const live = `<span class="sr-only" id="${liveId}" role="status"></span>`;
    if (mode === 'flow') {
      return `
      <div class="op-progress${hidden}" id="${wid}">
        <div class="op-track flow"><div class="op-fill"></div></div>
        <div class="op-labels"><span id="${textId}">${esc(o.text || '')}</span></div>
        ${live}
      </div>`;
    }
    return `
    <div class="op-progress${hidden}" id="${wid}">
      <div class="op-track"><div class="op-fill" id="${fillId}"></div></div>
      <div class="op-labels">${o.label ? `<span>${esc(o.label)}</span>` : ''}<span id="${textId}">${esc(o.text || '')}</span></div>
      ${live}
    </div>`;
  }
  function opProgressPaint(wid, o) {
    o = o || {};
    const box = document.getElementById(wid);
    if (!box) return;
    if (o.hidden !== undefined) box.classList.toggle('hidden', !!o.hidden);
    if (o.stage !== undefined) {
      const live = document.getElementById(o.liveId || (wid + '-live'));
      if (live && live.textContent !== String(o.stage || '')) {
        live.textContent = String(o.stage || '');
      }
    }
    const txt = document.getElementById(o.textId || (wid + '-text'));
    if (o.mode === 'flow') {
      if (txt && o.text !== undefined) txt.textContent = o.text;
      return;
    }
    const total = Number(o.total || 0);
    const done = Math.max(0, Math.min(Number(o.done || 0), total || Number(o.done || 0)));
    const fill = document.getElementById(o.fillId || (wid + '-fill'));
    if (fill) fill.style.width = (total ? done / total * 100 : 0).toFixed(1) + '%';
    if (txt) txt.textContent = o.text || (total ? `已处理 ${done}／${total} 项` : '');
  }
  let revealCount = 0;
  function opRevealProgressIn(scope) {
    revealCount += 1;
    if (!scope || !scope.querySelectorAll) return;
    scope.querySelectorAll('.op-progress').forEach(e => {
      if (e.dataset.opReveal === '1') return;
      e.dataset.opReveal = '1';
      e.classList.add('op-fade-in');
      setTimeout(() => e.classList.add('op-shown'), 300);
    });
  }
  function opFetchJson() { throw new Error('未配置 opFetchJson'); }

  const store = new Map();
  const sessionStorage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
  };
  const window = {};
  let toasts = [];
  function toast(text, kind) { toasts.push([String(text), kind]); }
  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    return (n / 1024).toFixed(1) + ' KB';
  }
  let xhrs = [];
  class FakeXHR {
    constructor() { this.upload = {}; this.status = 0; this.responseText = ''; this.timeout = 0; xhrs.push(this); }
    open(m, u) { this.method = m; this.url = u; }
    send(fd) { this.fd = fd; this.sent = true; }
  }
  const XMLHttpRequest = FakeXHR;
  class FakeFormData {
    constructor() { this.items = []; }
    append(k, v) { this.items.push([k, v]); }
  }
  const FormData = FakeFormData;

  /*__ENTRY_SLICE__*/

  const flush = () => new Promise(r => globalThis.setTimeout(r, 0));
  const sleep = ms => new Promise(r => globalThis.setTimeout(r, ms));
  function hidden(id) {
    const e = registry.get(id);
    return !e || e.classList.contains('hidden');
  }
  function reset() {
    toasts = [];
    xhrs = [];
    revealCount = 0;
    store.clear();
    registry.forEach(e => {
      e.classList.remove('hidden');
      e.dataset = {};
      e.textContent = '';
    });
    $('#h1').innerHTML = '';
    $('#h2').innerHTML = '';
    $('#note1').innerHTML = '';
    $('#btn1').disabled = false;
    $('#btn1').textContent = '执行';
    $('#inp1').disabled = false;
    $('#inp1').dataset = {};
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  test('短请求直接出结果, 进度条隐藏, 按钮复位', async () => {
    reset();
    let done = 0;
    await runWithProgress({
      key: 'req-fast', host: '#h1', note: '#note1', button: '#btn1',
      busyText: '执行中…', scanText: '正在处理…', unit: '项',
      request: { run: async () => ({ ok: true }) },
      resultHtml: r => `<div>完成 ${r.ok}</div>`,
      onDone: () => { done += 1; },
    });
    assert.strictEqual(done, 1);
    assert.strictEqual(hidden('req-fast-count'), true);
    assert.strictEqual(hidden('req-fast-flow'), true);
    assert.strictEqual($('#btn1').disabled, false);
    assert.strictEqual($('#btn1').textContent, '执行');
    assert.ok($('#note1').innerHTML.includes('完成 true'));
  });

  test('慢请求即时受理, 300ms 后流动条才显示', async () => {
    reset();
    let resolve;
    const slow = new Promise(r => { resolve = r; });
    const p = runWithProgress({
      key: 'req-slow', host: '#h2', button: '#btn1', busyText: '处理中…',
      scanText: '正在核对…',
      request: { run: () => slow, timeout: 5000 },
    });
    assert.strictEqual($('#btn1').disabled, true);
    assert.strictEqual($('#btn1').textContent, '处理中…');
    assert.strictEqual($('#h2').classList.contains('hidden'), false);
    const flowEl = document.getElementById('req-slow-flow');
    assert.ok(flowEl.classList.contains('op-fade-in'));
    assert.ok(!flowEl.classList.contains('op-shown'));
    await sleep(340);
    assert.ok(flowEl.classList.contains('op-shown'), '300ms 后应显示流动条');
    resolve({ ok: true });
    await p;
    assert.strictEqual($('#btn1').disabled, false);
  });

  test('请求失败显示错误并复位', async () => {
    reset();
    let err = '';
    await runWithProgress({
      key: 'req-bad', host: '#h1', note: '#note1', button: '#btn1',
      request: { run: async () => ({ ok: false, error: '密钥无效' }),
                 validate: r => (r.ok ? '' : r.error) },
      onError: e => { err = e.message; },
    });
    assert.strictEqual(err, '密钥无效');
    assert.ok($('#note1').innerHTML.includes('密钥无效'));
    assert.strictEqual($('#btn1').disabled, false);
  });

  test('上传: 未知总量流动, 有字节后计数, 写入阶段', async () => {
    reset();
    const fd = new FormData();
    fd.append('files', { name: 'a.png' });
    const p = runWithProgress({
      key: 'up', host: '#h2', note: '#note1', button: '#btn1', mode: 'upload',
      label: '添加图片', scanText: '正在上传图片…',
      upload: { url: '/upload', formData: fd, stage: '正在上传图片…',
                writingStage: '正在写入收件箱…' },
      countText: (d, t) => `已上传 ${fmtSize(d)}／${fmtSize(t)}`,
      resultHtml: r => `<div>已添加 ${r.saved.length} 张</div>`,
      onDone: () => {},
    });
    assert.strictEqual(xhrs.length, 1);
    assert.strictEqual($('#btn1').disabled, true);
    xhrs[0].upload.onprogress({ lengthComputable: true, loaded: 512, total: 2048 });
    assert.ok(document.getElementById('up-count-text').textContent.includes('512 B'));
    assert.strictEqual(hidden('up-flow'), true);
    xhrs[0].upload.onload();
    assert.ok(document.getElementById('up-flow-text').textContent.includes('正在写入收件箱'));
    assert.strictEqual(hidden('up-count'), true);
    xhrs[0].status = 200;
    xhrs[0].responseText = JSON.stringify({ status: 'ok', saved: ['a.png'] });
    xhrs[0].onload();
    await p;
    await flush();
    assert.ok($('#note1').innerHTML.includes('已添加 1 张'));
    assert.strictEqual($('#btn1').disabled, false);
  });

  test('迟到上传事件不能改新批次', async () => {
    reset();
    runWithProgress({
      key: 'up-late', host: '#h2', note: '#note1', button: '#btn1', mode: 'upload',
      label: '添加图片', upload: { url: '/upload', formData: new FormData() },
      countText: (d, t) => `${d}/${t}`,
    });
    const oldXhr = xhrs[0];
    oldXhr.status = 200;
    oldXhr.responseText = JSON.stringify({ status: 'ok', saved: [] });
    oldXhr.onload();
    await flush();
    runWithProgress({
      key: 'up-late', host: '#h2', note: '#note1', button: '#btn1', mode: 'upload',
      label: '添加图片', upload: { url: '/upload', formData: new FormData() },
      countText: (d, t) => `${d}/${t}`,
    });
    const newXhr = xhrs[1];
    newXhr.upload.onprogress({ lengthComputable: true, loaded: 100, total: 1000 });
    const before = document.getElementById('up-late-count-text').textContent;
    oldXhr.upload.onprogress({ lengthComputable: true, loaded: 999, total: 1000 });
    oldXhr.onerror();
    assert.strictEqual(document.getElementById('up-late-count-text').textContent, before);
    newXhr.status = 200;
    newXhr.responseText = JSON.stringify({ status: 'ok', saved: [] });
    newXhr.onload();
  });

  test('后台任务: 计数推进、完成只结算一次、会话 id 保存', async () => {
    reset();
    let starts = 0, done = 0, reads = 0;
    const snaps = [
      { status: 'running', done: 1, total: 2, stage: '处理中' },
      { status: 'done', done: 2, total: 2, result: { n: 2 } },
    ];
    const p = runWithProgress({
      key: 'task1', host: '#h2', note: '#note1', button: '#btn1', mode: 'task',
      unit: '张', label: '入库选中',
      task: {
        start: async () => { starts += 1; return { status: 'ok', task: { id: 't1', status: 'running', done: 0, total: 2 } }; },
        read: async () => { reads += 1; return snaps.shift() || snaps[0]; },
        idOf: r => r.task.id,
        snapshotOf: r => r.task,
        intervalMs: 10,
        map: { running: s => s.status === 'running', error: s => '', result: s => s.result },
      },
      onDone: () => { done += 1; },
    });
    await p;
    await sleep(120);
    assert.strictEqual(starts, 1);
    assert.ok(reads >= 2);
    assert.strictEqual(done, 1);
    assert.strictEqual(store.get('vixiq.op.task1.task'), 't1');
    assert.strictEqual(hidden('task1-count'), true);
    assert.strictEqual($('#btn1').disabled, false);
  });

  test('重复点击同一任务只接回, 不重复启动', async () => {
    reset();
    let starts = 0;
    const p = runWithProgress({
      key: 'task2', host: '#h2', unit: '张', mode: 'task',
      task: {
        start: async () => { starts += 1; return { status: 'ok', task: { id: 't2', status: 'running', done: 0, total: 9 } }; },
        read: async () => ({ status: 'running', done: 1, total: 9 }),
        idOf: r => r.task.id, snapshotOf: r => r.task, intervalMs: 20,
      },
    });
    await runWithProgress({ key: 'task2', host: '#h2', unit: '张', mode: 'task',
      task: { start: async () => { starts += 1; return { status: 'ok', task: { id: 't2', status: 'running' } }; },
              read: async () => ({ status: 'running', done: 1, total: 9 }),
              idOf: r => r.task.id, snapshotOf: r => r.task, intervalMs: 20 } });
    assert.strictEqual(starts, 1);
    detachOp('task2');
  });

  test('查询短暂失败保留最后进度并重试, 中断可接回', async () => {
    reset();
    let calls = 0, done = 0;
    runWithProgress({
      key: 'task3', host: '#h2', unit: '张', mode: 'task',
      task: {
        start: async () => ({ status: 'ok', task: { id: 't3', status: 'running', done: 0, total: 2 } }),
        read: async () => {
          calls += 1;
          if (calls === 1) throw new Error('临时网络错误');
          if (calls === 2) return { status: 'running', done: 1, total: 2 };
          return { status: 'done', done: 2, total: 2, result: { n: 2 } };
        },
        idOf: r => r.task.id, snapshotOf: r => r.task,
        intervalMs: 10, retries: 3,
      },
      onDone: () => { done += 1; },
    });
    await sleep(150);
    assert.strictEqual(done, 1);
  });

  test('关窗 detach 停止查询, 重开 resume 接回同一任务', async () => {
    reset();
    let reads = 0, done = 0;
    const opts = {
      key: 'task4', host: '#h2', unit: '张', mode: 'task',
      task: {
        start: async () => ({ status: 'ok', task: { id: 't4', status: 'running', done: 0, total: 2 } }),
        read: async () => {
          reads += 1;
          return reads < 3
            ? { status: 'running', done: 1, total: 2 }
            : { status: 'done', done: 2, total: 2, result: { n: 2 } };
        },
        idOf: r => r.task.id, snapshotOf: r => r.task, intervalMs: 20,
      },
      onDone: () => { done += 1; },
    };
    runWithProgress(opts);
    await sleep(50);
    detachOp('task4');
    const stopped = reads;
    await sleep(80);
    assert.strictEqual(reads, stopped, 'detach 后不应继续查询');
    resumeOp('task4', opts);
    await sleep(120);
    assert.ok(reads > stopped, 'resume 后应继续查询');
    assert.strictEqual(done, 1);
  });

  test('连续两次 resume 只启动一条查询链', async () => {
    reset();
    let reads = 0, done = 0;
    let resolveRead;
    const opts = {
      key: 'task5', host: '#h2', unit: '张', mode: 'task',
      task: {
        start: async () => ({ status: 'ok', task: { id: 't5', status: 'running', done: 0, total: 2 } }),
        read: async () => {
          reads += 1;
          return new Promise(r => { resolveRead = r; });
        },
        idOf: r => r.task.id, snapshotOf: r => r.task, intervalMs: 10,
      },
      onDone: () => { done += 1; },
    };
    await runWithProgress(opts);
    await sleep(30);
    assert.strictEqual(reads, 1, '第一查询应在途');
    const ctl1 = resumeOp('task5', opts);
    const ctl2 = resumeOp('task5', opts);
    assert.ok(ctl1 && ctl2);
    await sleep(30);
    assert.strictEqual(reads, 1, '重复接回不得再发第二个 GET');
    resolveRead({ status: 'done', done: 2, total: 2, result: { n: 2 } });
    await sleep(40);
    assert.strictEqual(done, 1);
    assert.strictEqual(reads, 1);
  });

  test('GET 在途时关窗再开, 放行旧 GET 后继续推进', async () => {
    reset();
    let reads = 0, done = 0;
    let resolveOld;
    const opts = {
      key: 'task6', host: '#h2', unit: '张', mode: 'task',
      task: {
        start: async () => ({ status: 'ok', task: { id: 't6', status: 'running', done: 0, total: 2 } }),
        read: async () => {
          reads += 1;
          if (reads === 1) return new Promise(r => { resolveOld = r; });
          if (reads === 2) return { status: 'running', done: 1, total: 2 };
          return { status: 'done', done: 2, total: 2, result: { n: 2 } };
        },
        idOf: r => r.task.id, snapshotOf: r => r.task, intervalMs: 10,
      },
      onDone: () => { done += 1; },
    };
    runWithProgress(opts);
    await sleep(30);
    assert.strictEqual(reads, 1, '旧查询应在途');
    detachOp('task6');
    resumeOp('task6', opts);
    assert.strictEqual(reads, 1, '旧 GET 在途时重开不应再发第二个');
    resolveOld({ status: 'running', done: 1, total: 2 });
    await sleep(80);
    assert.ok(reads >= 2, '放行旧 GET 后应继续下一次查询');
    await sleep(80);
    assert.strictEqual(done, 1, '完成回调只结算一次');
    assert.strictEqual(reads, 3);
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

const generated = '(' + harnessBody.toString()
  .replace('/*__ENTRY_SLICE__*/', () => blockSrc) + ')();\n';
new Function('assert', generated)(assert);
