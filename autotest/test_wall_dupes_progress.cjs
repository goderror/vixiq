'use strict';

// 壁纸副本扫描: 共用入口后台任务接法的业务适配与结果渲染。
// 共用引擎(轮询/超时/迟到隔离/接回)由 test_operation_progress_entry.cjs 覆盖;
// 这里验打开读取、扫描 task 配置、流动切计数、结果/空目录/失败、关闭 detach、重开接回。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// ============ 🧹 壁纸副本清理 (「省空间」) —— 2026-09-24 ============';
const END = 'window.wallRefresh = wallRefresh;';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少壁纸副本清理代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  function makeEl(id) {
    const set = new Set();
    const obj = {
      id: id || '', innerHTML: '', textContent: '', dataset: {}, disabled: false,
      style: {},
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
    };
    return obj;
  }
  const els = {};
  ['#lib-modal', '#lib-modal-body', '#wall-dupes-task', '#wall-dupes-progress',
   '#wall-dupes-head', '#wall-dupes-list'].forEach(s => { els[s] = makeEl(s); });
  els['#lib-modal'].style.display = 'none';
  let modalAlive = true;
  function $(sel) {
    if (sel === '#wall-dupes-progress' && !modalAlive) return null;
    return els[sel] || null;
  }
  function $$() { return []; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function fmtSize(n) {
    const v = Number(n || 0);
    if (v >= 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + ' MB';
    if (v >= 1024) return (v / 1024).toFixed(1) + ' KB';
    return v + ' B';
  }
  function openLibModal(title, html) {
    modalAlive = true;
    els['#lib-modal'].style.display = '';
    els['#lib-modal-body'].innerHTML = html;
  }
  function closeLibModal() {
    modalAlive = false;
    els['#lib-modal'].style.display = 'none';
  }
  function opClaimModal() {}
  let progressCalls = [];
  function opProgressHtml(wid) { return `<div id="${wid}"></div>`; }
  function opProgressPaint(wid, o) {
    progressCalls.push({ wid, o: Object.assign({}, o) });
  }
  function opRevealProgressIn() {}
  let apiCalls = [];
  let apiHandler = async () => ({ status: 'idle', result: null });
  async function api(url, opts) {
    apiCalls.push({ url, opts: opts || {} });
    return apiHandler(url, opts || {});
  }
  async function opFetchJson(url) {
    apiCalls.push({ url, opts: {} });
    return apiHandler(url, {});
  }
  let toastLog = [];
  function toast(msg, type) { toastLog.push({ msg, type }); }
  let runCalls = [];
  function runWithProgress(o) { runCalls.push(o); return { key: o.key }; }
  const _opRuns = new Map();   // 共用入口的运行实例表 (本用例不预置运行中实例)
  let resumes = [], repaints = [], detaches = [];
  function resumeOp(key, o) { resumes.push({ key, o }); return { key }; }
  function repaintOp(key) { repaints.push(key); return true; }
  function detachOp(key) { detaches.push(key); }
  const window = {};

  /*__DUPES_SLICE__*/

  function lastPaint(wid) {
    for (let i = progressCalls.length - 1; i >= 0; i -= 1) {
      if (progressCalls[i].wid === wid) return progressCalls[i].o;
    }
    return null;
  }
  function runningTask(stage, done, total) {
    return { status: 'scanning', stage, done, total, current: '',
             error: '', result: null };
  }
  function doneTask(result) {
    return { status: 'done', stage: '', done: 0, total: 0, current: '',
             error: '', result };
  }
  function reset() {
    progressCalls = [];
    apiCalls = [];
    toastLog = [];
    runCalls = [];
    resumes = [];
    repaints = [];
    detaches = [];
    wallDupesSel = new Set();
    wallDupesLast = null;
    modalAlive = true;
    els['#lib-modal'].style.display = 'none';
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.disabled = false;
    });
    apiHandler = async () => ({ status: 'idle', result: null });
  }
  function headHtml() { return els['#wall-dupes-head'].innerHTML; }
  function headText() { return headHtml().replace(/<[^>]*>/g, ''); }
  function listHtml() { return els['#wall-dupes-list'].innerHTML; }
  function taskHidden() { return els['#wall-dupes-task'].classList.contains('hidden'); }
  async function openIdle() {
    apiHandler = async () => ({ status: 'idle', result: null });
    await wallDupesOpen();
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  test('打开弹窗读取已有状态; 空闲时显示扫描入口', async () => {
    reset();
    await wallDupesOpen();
    assert.strictEqual(apiCalls.length, 1);
    assert.ok(apiCalls[0].url.includes('/api/wallpaper/dupes'));
    assert.ok(els['#lib-modal'].style.display === '');
    assert.ok(taskHidden());
    assert.ok(headHtml().includes('还没扫过') && headHtml().includes('扫描副本'));
    assert.strictEqual(runCalls.length, 0);
  });

  test('打开时服务器正在扫描 → 接回同一后台任务', async () => {
    reset();
    apiHandler = async () => runningTask('扫描壁纸', 1, 3);
    await wallDupesOpen();
    assert.strictEqual(resumes.length, 1);
    assert.strictEqual(resumes[0].key, 'wall-dupes');
    const o = resumes[0].o;
    assert.strictEqual(o.key, 'wall-dupes');
    assert.strictEqual(o.task.fixedId, 'wall-dupes');
    assert.ok(o.countText(1, 3).includes('已扫描 1／3 张'));
    assert.ok(lastPaint('wall-dupes-count').text.includes('已扫描 1／3 张'));
  });

  test('点击扫描交给共用入口 task 模式并即时显示流动', async () => {
    reset();
    await openIdle();
    apiHandler = async (url, opts) => (opts && opts.method === 'POST')
      ? { status: 'ok', task: runningTask('核对壁纸夹与图库索引', 0, 0) }
      : runningTask('扫描壁纸', 1, 3);
    const btn = makeEl('scan');
    await wallDupesScan(btn);
    assert.strictEqual(runCalls.length, 1);
    const o = runCalls[0];
    assert.strictEqual(o.key, 'wall-dupes');
    assert.strictEqual(o.mode, 'task');
    assert.strictEqual(o.button, btn);
    assert.strictEqual(o.task.fixedId, 'wall-dupes');
    const start = await o.task.start();
    assert.ok(apiCalls.some(c => c.url.includes('/api/wallpaper/dupes/scan')
      && c.opts.method === 'POST'));
    assert.strictEqual(start.status, 'ok');
    assert.strictEqual(o.task.idOf(), 'wall-dupes');
    assert.strictEqual(o.task.map.running(runningTask('x', 1, 3)), true);
    assert.strictEqual(o.task.map.error({ status: 'error', error: '坏了' }), '坏了');
    assert.ok(lastPaint('wall-dupes-flow').text.includes('正在核对壁纸夹与图库索引'));
    assert.strictEqual(headHtml(), '');
  });

  test('候选数确定后切换为已扫描 N／M 张', async () => {
    reset();
    await openIdle();
    runCalls.length = 0;
    await wallDupesScan(makeEl('scan'));
    const o = runCalls[0];
    assert.strictEqual(o.task.map.running(runningTask('扫描壁纸', 1, 3)), true);
    assert.strictEqual(o.task.map.done(runningTask('扫描壁纸', 1, 3)), 1);
    assert.strictEqual(o.task.map.total(runningTask('扫描壁纸', 1, 3)), 3);
    assert.strictEqual(o.task.map.stage(runningTask('扫描壁纸', 1, 3)), '扫描壁纸');
    assert.strictEqual(o.countText(1, 3), '已扫描 1／3 张');
  });

  test('完成结果: 命中/可释放/未完成/行勾选', async () => {
    reset();
    await openIdle();
    wallDupesPaint(doneTask({
      scanned: 3, count: 1, bytes: 1234, failed: 1,
      errors: ['坏文件.png: 无法读取文件指纹'],
      items: [{ name: 'dup.png', size: 1234, artwork_id: '11',
                author_name: 'A', title: 'T', current: 1 }],
    }));
    assert.ok(taskHidden());
    const h = headText();
    for (const kw of ['已扫描 3 张', '发现 1 张副本', '可释放 1.2 KB',
                      '1 张未完成', '坏文件.png', '重扫']) {
      assert.ok(h.includes(kw), h);
    }
    assert.ok(listHtml().includes('dup.png'), listHtml());
    wallDupesPick(true, 'dup.png');
    assert.ok(listHtml().includes('checked'));
    assert.strictEqual(wallDupesSel.size, 1);
  });

  test('空目录、无命中与扫描失败分别给出反馈', async () => {
    reset();
    await openIdle();
    wallDupesPaint(doneTask({ scanned: 0, count: 0, bytes: 0, failed: 0,
                              errors: [], items: [] }));
    assert.ok(headHtml().includes('壁纸夹没有可扫描的图片'));

    reset();
    await openIdle();
    wallDupesPaint(doneTask({ scanned: 2, count: 0, bytes: 0, failed: 0,
                              errors: [], items: [] }));
    assert.ok(headHtml().includes('没有发现与图库字节一致的副本'));

    reset();
    await openIdle();
    wallDupesPaint({ status: 'error', stage: '', error: 'PermissionError: 拒绝访问',
                     result: null });
    assert.ok(headHtml().includes('扫描失败') && headHtml().includes('拒绝访问'));
    assert.ok(headHtml().includes('重扫'));
    assert.ok(taskHidden());
  });

  test('扫描失败由共用入口回调显示原因并提示', async () => {
    reset();
    await openIdle();
    runCalls.length = 0;
    await wallDupesScan(makeEl('scan'));
    const o = runCalls[0];
    o.onError(new Error('读不到当前壁纸路径'));
    assert.ok(headText().includes('扫描失败') && headText().includes('当前壁纸'));
    assert.ok(headHtml().includes('重扫</button>'));
    assert.ok(toastLog.some(t => t.type === 'err'));
  });

  test('关闭弹窗只解除查询订阅, 不取消后台扫描', () => {
    reset();
    wallDupesClose();
    assert.deepStrictEqual(detaches, ['wall-dupes']);
    assert.strictEqual(els['#lib-modal'].style.display, 'none');
  });

  test('快速完成直接显示结果, 进度区隐藏', async () => {
    reset();
    await openIdle();
    runCalls.length = 0;
    await wallDupesScan(makeEl('scan'));
    const o = runCalls[0];
    const res = { scanned: 2, count: 1, bytes: 9, failed: 0,
                  errors: [], items: [{ name: 'a.png', size: 9 }] };
    o.onDone(res, { snapshot: doneTask(res) });
    assert.ok(taskHidden());
    assert.ok(headText().includes('发现 1 张副本'));
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
  .replace('/*__DUPES_SLICE__*/', () => blockSrc) + ')();\n';
new Function('assert', generated)(assert);
