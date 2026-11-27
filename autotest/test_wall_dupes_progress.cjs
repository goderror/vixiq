'use strict';

// 壁纸副本扫描共用进度: 直接运行 app.js 内的真实函数。
// 覆盖: 打开读取、点击即时受理/防重复、流动切计数、结果/空目录/失败、串行查询、
// 终态后旧 running、关闭弹窗与切换共享弹窗的归属保护、短暂失败、重开接回。

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
    return {
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
  }
  const els = {};
  ['#lib-modal', '#lib-modal-body', '#wall-dupes-task', '#wall-dupes-progress',
   '#wall-dupes-head', '#wall-dupes-list'].forEach(s => {
    els[s] = makeEl(s);
  });
  els['#lib-modal'].style.display = 'none';
  const barEls = { 'wall-dupes-flow': makeEl('wall-dupes-flow'),
                   'wall-dupes-count': makeEl('wall-dupes-count') };
  els['#wall-dupes-progress'].querySelectorAll = sel =>
    (sel === '.op-progress' ? Object.values(barEls) : []);
  let modalOwner = true;
  function $(sel) {
    if (sel === '#wall-dupes-progress' && !modalOwner) return null;
    return els[sel] || null;
  }
  function $$(sel) { return []; }
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
  let modalSeq = 0;
  function openLibModal(title, html) {
    modalSeq += 1;
    els['#lib-modal'].style.display = '';
    els['#lib-modal'].dataset.opSeq = String(modalSeq);
    els['#lib-modal-body'].innerHTML = html;
  }
  function closeLibModal() { els['#lib-modal'].style.display = 'none'; }
  function opClaimModal(owner) {
    els['#lib-modal'].dataset.opOwner = owner || '';
  }
  function opModalSeq() {
    return Number(els['#lib-modal'].dataset.opSeq || 0);
  }
  function opModalSame(seq) {
    return els['#lib-modal'].style.display !== 'none'
      && String(els['#lib-modal'].dataset.opSeq || '') === String(seq);
  }
  function apiErr(r) {
    if (r && r.status === 'ok') return '';
    return (r && (r.msg || r.error)) || '请求失败';
  }

  let progressCalls = [];
  function opProgressHtml(wid) { return `<div id="${wid}"></div>`; }
  function opProgressPaint(wid, o) {
    progressCalls.push({ wid, o: Object.assign({}, o) });
  }
  let revealCalls = [];
  function opRevealProgressIn(scope) { revealCalls.push(scope); }

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
  let confirmResult = true;
  function confirm() { return confirmResult; }

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

  /*__DUPES_SLICE__*/

  const flush = () => new Promise(r => realSetTimeout(r, 0));
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
    revealCalls = [];
    apiCalls = [];
    toastLog = [];
    timers = [];
    wallDupesTimer = null;
    wallDupesSel = new Set();
    wallDupesLast = null;
    _wallDupesGen = 0;
    _wallDupesInFlight = false;
    _wallDupesStarting = false;
    _wallDupesStartSeq = 0;
    modalSeq = 0;
    modalOwner = true;
    els['#lib-modal'].style.display = 'none';
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.disabled = false;
    });
    els['#lib-modal'].style.display = 'none';
    Object.values(barEls).forEach(el => {
      el.className = '';
      el.dataset = {};
    });
    apiHandler = async () => ({ status: 'idle', result: null });
  }
  async function openIdle() {
    apiHandler = async () => ({ status: 'idle', result: null });
    await wallDupesOpen();
    await flush();
  }
  function headHtml() { return els['#wall-dupes-head'].innerHTML; }
  function headText() { return headHtml().replace(/<[^>]*>/g, ''); }
  function listHtml() { return els['#wall-dupes-list'].innerHTML; }
  function postCount() {
    return apiCalls.filter(c => c.opts && c.opts.method === 'POST').length;
  }
  function getCount() {
    return apiCalls.filter(c => !(c.opts && c.opts.method)).length;
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 打开：读取已有状态；无结果时直接显示入口
  test('打开弹窗读取已有状态并显示扫描入口', async () => {
    reset();
    await wallDupesOpen();
    await flush();
    assert.strictEqual(els['#lib-modal'].style.display, '');
    assert.ok(els['#wall-dupes-task'].classList.contains('hidden'));
    assert.ok(headHtml().includes('还没扫过') && headHtml().includes('扫描副本'));
    assert.strictEqual(timerCount(), 0);
  });

  // 2. 点击扫描立即受理 + 防重复
  test('点击扫描立即反馈并只提交一次', async () => {
    reset();
    await openIdle();
    let posts = 0, resolvePost;
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        posts += 1;
        return new Promise(r => { resolvePost = r; });
      }
      return runningTask('核对壁纸夹与图库索引', 0, 0);
    };
    const btn = makeEl('scan');
    const p = wallDupesScan(btn);
    assert.strictEqual(posts, 1);
    assert.strictEqual(btn.disabled, true);
    assert.ok(!els['#wall-dupes-task'].classList.contains('hidden'));
    const flow = lastPaint('wall-dupes-flow');
    assert.ok(flow && flow.text.includes('正在核对壁纸夹与图库索引'));
    assert.strictEqual(headHtml(), '');
    await wallDupesScan(makeEl('scan2'));
    assert.strictEqual(posts, 1);
    resolvePost({ status: 'ok', task: runningTask('核对壁纸夹与图库索引', 0, 0) });
    await p; await flush();
    assert.ok(timerCount() >= 1, '扫描中应安排下一次查询');
  });

  // 3. 流动切真实计数
  test('候选数确定后切换为已扫描 N／M 张', async () => {
    reset();
    await openIdle();
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        return { status: 'ok', task: runningTask('核对壁纸夹与图库索引', 0, 0) };
      }
      return runningTask('扫描壁纸', 1, 3);
    };
    await wallDupesScan(makeEl('scan'));
    await flush();
    const count = lastPaint('wall-dupes-count');
    assert.ok(count && count.hidden === false && count.text.includes('已扫描 1／3 张'),
              count && count.text);
    assert.strictEqual(lastPaint('wall-dupes-flow').hidden, true);
    assert.strictEqual(timerCount(), 1);
  });

  // 4. 完成结果：命中、可释放、未完成
  test('结果显示命中数、可释放量和未完成原因', async () => {
    reset();
    await openIdle();
    const t = doneTask({
      scanned: 3, count: 1, bytes: 1234, failed: 1,
      errors: ['坏文件.png: 无法读取文件指纹'],
      items: [{ name: 'dup.png', size: 1234, artwork_id: '11',
                author_name: 'A', title: 'T', current: 1 }],
    });
    wallDupesPaint(t);
    assert.ok(els['#wall-dupes-task'].classList.contains('hidden'));
    const h = headText();
    for (const kw of ['已扫描 3 张', '发现 1 张副本', '可释放 1.2 KB',
                      '1 张未完成', '坏文件.png', '重扫']) {
      assert.ok(h.includes(kw), h);
    }
    assert.ok(listHtml().includes('dup.png'), listHtml());
    assert.strictEqual(timerCount(), 0);
  });

  // 5. 空目录 / 无命中 / 错误
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
    assert.ok(els['#wall-dupes-task'].classList.contains('hidden'));
  });

  // 6. 启动失败
  test('启动失败显示原因并恢复按钮', async () => {
    reset();
    await openIdle();
    apiHandler = async (url, opts) => {
      if (opts && opts.method === 'POST') {
        return { status: 'error', msg: '读不到当前壁纸路径' };
      }
      return { status: 'idle', result: null };
    };
    const btn = makeEl('scan');
    await wallDupesScan(btn);
    await flush();
    assert.ok(headText().includes('扫描失败') && headText().includes('当前壁纸'));
    // 旧按钮已被结果区替换, 新渲染的「重扫」可点
    assert.ok(headHtml().includes('重扫</button>'));
    assert.strictEqual(postCount(), 1);
    assert.ok(toastLog.some(t => t.type === 'err'));
    assert.strictEqual(timerCount(), 0);
  });

  // 7. 同一条状态链不并发查询
  test('上一请求未结束时不发下一次状态查询', async () => {
    reset();
    await openIdle();
    let resolveFirst;
    let gets = 0;
    apiHandler = () => {
      gets += 1;
      if (gets === 1) return new Promise(r => { resolveFirst = r; });
      return runningTask('扫描壁纸', 2, 6);
    };
    wallDupesPaint(runningTask('扫描壁纸', 1, 6));
    startWallDupesPolling();
    await flush();
    assert.strictEqual(gets, 1);
    assert.strictEqual(timerCount(), 0, '请求在途时不应安排下一次');
    resolveFirst(runningTask('扫描壁纸', 2, 6));
    await flush();
    assert.strictEqual(gets, 1, '上一请求结束前不应发第二次');
    assert.strictEqual(timerCount(), 1);
    await runNextTimer(); await flush();
    assert.strictEqual(gets, 2);
  });

  // 8. 终态后迟到的旧 running 不覆盖结果
  test('终态后迟到的旧 running 不覆盖结果', async () => {
    reset();
    await openIdle();
    let resolveOld;
    apiHandler = () => new Promise(r => { resolveOld = r; });
    const p = loadWallDupes(true);
    await flush();
    stopWallDupesPolling();
    const done = doneTask({ scanned: 2, count: 1, bytes: 10, failed: 0,
                            errors: [], items: [{ name: 'a.png', size: 10 }] });
    wallDupesPaint(done);
    const before = headHtml();
    resolveOld(runningTask('扫描壁纸', 1, 2));
    await p; await flush();
    assert.strictEqual(headHtml(), before);
    assert.ok(headText().includes('发现 1 张副本'));
    assert.strictEqual(timerCount(), 0);
    assert.ok(els['#wall-dupes-task'].classList.contains('hidden'));
  });

  // 9. 关闭弹窗：停止查询，旧回包不绘制
  test('关闭弹窗后旧回包不绘制', async () => {
    reset();
    await openIdle();
    let resolvePending;
    apiHandler = () => new Promise(r => { resolvePending = r; });
    // 进入运行态并启动轮询; 第一次 tick 的请求挂起
    wallDupesPaint(runningTask('扫描壁纸', 1, 3));
    startWallDupesPolling();
    await flush();
    const before = headHtml();
    wallDupesClose();
    resolvePending(runningTask('扫描壁纸', 2, 3));
    await flush();
    assert.strictEqual(headHtml(), before);
    assert.strictEqual(els['#lib-modal'].style.display, 'none');
    assert.strictEqual(timerCount(), 0);
  });

  // 10. 切换共享弹窗：旧回包不绘制
  test('切换共享弹窗后旧回包不绘制', async () => {
    reset();
    await openIdle();
    let resolvePending;
    apiHandler = () => new Promise(r => { resolvePending = r; });
    wallDupesPaint(runningTask('扫描壁纸', 1, 3));
    startWallDupesPolling();
    await flush();
    const beforeHead = headHtml();
    const beforeList = listHtml();
    modalOwner = false;          // 另一个弹窗占用了 #lib-modal-body
    resolvePending(doneTask({ scanned: 3, count: 1, bytes: 1, failed: 0,
                              errors: [], items: [] }));
    await flush();
    assert.strictEqual(headHtml(), beforeHead);
    assert.strictEqual(listHtml(), beforeList);
    assert.strictEqual(timerCount(), 0);
  });

  // 11. 短暂查询失败保留进度并继续
  test('短暂查询失败保留最后进度并继续', async () => {
    reset();
    await openIdle();
    let calls = 0;
    apiHandler = () => {
      calls += 1;
      if (calls === 1) throw new Error('网络抖动');
      if (calls === 2) return runningTask('扫描壁纸', 2, 3);
      return doneTask({ scanned: 3, count: 1, bytes: 5, failed: 0,
                        errors: [], items: [{ name: 'a.png', size: 5 }] });
    };
    wallDupesPaint(runningTask('扫描壁纸', 1, 3));
    startWallDupesPolling();
    await flush();
    const count = lastPaint('wall-dupes-count');
    assert.ok(count && count.text.includes('已扫描 1／3 张'), count && count.text);
    assert.strictEqual(timerCount(), 1);
    await runNextTimer(); await flush();
    assert.ok(lastPaint('wall-dupes-count').text.includes('已扫描 2／3 张'));
    await runNextTimer(); await flush();
    assert.ok(headText().includes('发现 1 张副本'));
    assert.strictEqual(timerCount(), 0);
  });

  // 12. 关闭后重开接回运行中任务
  test('关闭后重开接回运行中任务', async () => {
    reset();
    apiHandler = async () => runningTask('扫描壁纸', 1, 3);
    await wallDupesOpen();
    await flush();
    assert.strictEqual(timerCount(), 1);
    wallDupesClose();
    assert.strictEqual(timerCount(), 0);
    await wallDupesOpen();
    await flush();
    const count = lastPaint('wall-dupes-count');
    assert.ok(count && count.text.includes('已扫描 1／3 张'), count && count.text);
    assert.strictEqual(timerCount(), 1);
  });

  // 13. 快速完成只留结果
  test('快速完成不闪进度条只保留结果', async () => {
    reset();
    await openIdle();
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        return { status: 'ok', task: runningTask('扫描壁纸', 0, 2) };
      }
      return doneTask({ scanned: 2, count: 1, bytes: 9, failed: 0,
                        errors: [], items: [{ name: 'a.png', size: 9 }] });
    };
    await wallDupesScan(makeEl('scan'));
    await flush();
    assert.ok(els['#wall-dupes-task'].classList.contains('hidden'));
    assert.ok(headText().includes('发现 1 张副本'));
    assert.strictEqual(timerCount(), 0);
  });

  // 14. 旧启动成功回包不接管重开的新弹窗
  test('旧启动成功回包不接管重开的新弹窗', async () => {
    reset();
    await openIdle();
    let resolveOld;
    let posts = 0;
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        posts += 1;
        return new Promise(r => { resolveOld = r; });
      }
      return runningTask('扫描壁纸', 1, 3);
    };
    const oldP = wallDupesScan(makeEl('old'));
    await flush();
    wallDupesClose();
    await wallDupesOpen();
    await flush();
    assert.strictEqual(timerCount(), 1, '重开后应接回新任务轮询');
    const countBefore = lastPaint('wall-dupes-count');
    assert.ok(countBefore && countBefore.text.includes('已扫描 1／3 张'));
    const headBefore = headHtml();
    resolveOld({ status: 'ok', task: runningTask('核对壁纸夹与图库索引', 0, 0) });
    await oldP; await flush();
    assert.strictEqual(headHtml(), headBefore);
    const countAfter = lastPaint('wall-dupes-count');
    assert.ok(countAfter && countAfter.text.includes('已扫描 1／3 张'));
    assert.strictEqual(timerCount(), 1, '旧成功回包不能停新轮询');
    assert.strictEqual(posts, 1);
    assert.strictEqual(toastLog.length, 0);
  });

  // 15. 旧启动失败回包不改新弹窗也不弹旧错误
  test('旧启动失败回包不改新弹窗也不弹旧错误', async () => {
    reset();
    await openIdle();
    let resolveOld;
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        return new Promise(r => { resolveOld = r; });
      }
      return runningTask('扫描壁纸', 1, 3);
    };
    const oldP = wallDupesScan(makeEl('old'));
    await flush();
    wallDupesClose();
    await wallDupesOpen();
    await flush();
    assert.strictEqual(timerCount(), 1);
    const headBefore = headHtml();
    resolveOld({ status: 'error', msg: '旧网络错误' });
    await oldP; await flush();
    assert.strictEqual(headHtml(), headBefore);
    assert.ok(!headText().includes('扫描失败'));
    assert.strictEqual(timerCount(), 1, '旧失败不能停新轮询');
    assert.strictEqual(toastLog.length, 0, '旧失败不能弹旧通知');
    assert.strictEqual(_wallDupesStarting, false);
  });

  // 16. 新一轮启动仍挂起时旧 POST 结束: 不清新受理标记
  test('新一轮启动挂起时旧 POST 结束不清新受理标记', async () => {
    reset();
    await openIdle();
    let resolveOld, resolveNew;
    let posts = 0;
    let statusMode = 'idle';
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        posts += 1;
        if (posts === 1) return new Promise(r => { resolveOld = r; });
        return new Promise(r => { resolveNew = r; });
      }
      return statusMode === 'idle'
        ? { status: 'idle', result: null }
        : runningTask('扫描壁纸', 1, 3);
    };
    const oldP = wallDupesScan(makeEl('old'));
    await flush();
    wallDupesClose();
    await wallDupesOpen();
    await flush();
    const newP = wallDupesScan(makeEl('new'));
    await flush();
    assert.strictEqual(_wallDupesStarting, true);
    assert.strictEqual(posts, 2);
    resolveOld({ status: 'error', msg: '旧网络错误' });
    await oldP; await flush();
    assert.strictEqual(_wallDupesStarting, true, '旧 finally 不能清新一轮受理标记');
    assert.strictEqual(headHtml(), '');
    assert.strictEqual(toastLog.length, 0);
    statusMode = 'running';
    resolveNew({ status: 'ok', task: runningTask('核对壁纸夹与图库索引', 0, 0) });
    await newP; await flush();
    assert.strictEqual(_wallDupesStarting, false);
    assert.ok(timerCount() >= 1);
  });

  (async () => {
    for (const [name, fn] of cases) {
      await fn();
      console.log('ok - ' + name);
    }
    console.log('NODE OK');
  })().catch(e => {
    console.error(e && e.stack || e);
    process.exit(1);
  });
}

const body = harnessBody.toString();
const inner = body.slice(body.indexOf('{') + 1, body.lastIndexOf('}'))
  .replace('/*__DUPES_SLICE__*/', () => blockSrc);
new Function('assert', inner)(assert);
