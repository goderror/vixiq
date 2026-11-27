'use strict';

// 图库以图搜图共用进度: 直接运行 app.js 内的真实函数。
// 覆盖: 入口选图/重开、上传即时反馈、检查→提取计数→加载→检索→结果、无匹配/失败、
// 新选择替换旧任务、关闭重开、短暂查询失败/超时、串行查询、安全模式缩略图。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// ============ 🔍 图库以图搜图 (共用进度) ============';
const END = '// ============ 🧹 壁纸副本清理';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少图库以图搜图进度代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  function makeEl(id) {
    const set = new Set();
    const handlers = {};
    const el = {
      id, innerHTML: '', textContent: '', dataset: {}, disabled: false,
      value: '', files: null, style: {}, clicked: 0,
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
      dispatch(ev) { (handlers[ev] || []).forEach(fn => fn.call(el, { target: el })); },
      click() { el.clicked += 1; el.dispatch('click'); },
    };
    return el;
  }
  const els = {};
  ['#lib-modal', '#lib-modal-body', '#sim-task', '#sim-progress',
   '#sim-note', '#sim-list', '#lib-similar-btn', '#lib-similar-file']
    .forEach(s => { els[s] = makeEl(s); });
  els['#lib-modal'].style.display = 'none';
  const bars = { 'sim-flow': makeEl('sim-flow'), 'sim-count': makeEl('sim-count') };
  els['#sim-progress'].querySelectorAll = sel =>
    (sel === '.op-progress' ? Object.values(bars) : []);
  let simDom = true;
  function $(sel) {
    if (!simDom && (sel === '#sim-progress' || sel === '#sim-note'
                    || sel === '#sim-list' || sel === '#sim-task')) {
      return null;
    }
    return els[sel] || null;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  let safeThumbCalls = [];
  function safeThumb(url, it) {
    safeThumbCalls.push([url, it]);
    return 'SAFE:' + url;
  }
  function openLibModal(title, html) {
    els['#lib-modal'].style.display = '';
    els['#lib-modal-body'].innerHTML = html;
  }
  function closeLibModal() { els['#lib-modal'].style.display = 'none'; }
  function opClaimModal(owner) { els['#lib-modal'].dataset.opOwner = owner || ''; }
  function loadLibrary() {}
  function switchTab() {}

  let progressCalls = [];
  function opProgressHtml(wid) { return `<div id="${wid}"></div>`; }
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

  class FormData {
    constructor() { this.entries = []; }
    append(k, v) { this.entries.push([k, v]); }
  }

  let fetchCalls = [];
  let postHandler = async () => ({
    ok: true, status: 200,
    json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }),
  });
  async function fetch(url, opts) {
    fetchCalls.push({ url, opts });
    return postHandler(url, opts);
  }

  let getCalls = [];
  let statusHandler = async () => ({
    status: 'ok', task: simTask('checking', { id: 't1' }),
  });
  async function opFetchJson(url) {
    getCalls.push(url);
    return statusHandler(url);
  }

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

  /*__SIM_SLICE__*/

  const flush = () => new Promise(r => realSetTimeout(r, 0));
  function lastPaint(wid) {
    for (let i = progressCalls.length - 1; i >= 0; i -= 1) {
      if (progressCalls[i].wid === wid) return progressCalls[i].o;
    }
    return null;
  }
  function simTask(stage, opt) {
    return Object.assign({
      id: 't1', status: 'running', stage, done: 0, total: 0,
      extracted: 0, skipped: 0, failed: 0, errors: [], current: '',
      error: '', query_name: 'q.png', result: null,
    }, opt || {});
  }
  function doneSim(items, build) {
    return {
      id: 't1', status: 'done', stage: '', done: 0, total: 0,
      extracted: 0, skipped: 0, failed: 0, errors: [], current: '',
      error: '', query_name: 'q.png',
      result: { items: items || [], build: build || {
        total: 0, extracted: 0, skipped: 0, failed: 0, errors: [], index_size: 3,
      } },
    };
  }
  function reset() {
    progressCalls = [];
    revealCalls = [];
    fetchCalls = [];
    getCalls = [];
    safeThumbCalls = [];
    timers = [];
    _simTaskId = '';
    _simTimer = null;
    _simGen = 0;
    _simInFlight = false;
    _simStarting = false;
    _simStartSeq = 0;
    _simLast = null;
    simDom = true;
    Object.keys(store).forEach(k => delete store[k]);
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.disabled = false;
      el.value = '';
      el.files = null;
      el.clicked = 0;
    });
    els['#lib-modal'].style.display = 'none';
    Object.values(bars).forEach(el => {
      el.className = '';
      el.dataset = {};
    });
    postHandler = async () => ({
      ok: true, status: 200,
      json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }),
    });
    statusHandler = async () => ({
      status: 'ok', task: simTask('checking', { id: 't1' }),
    });
  }
  function postCount() { return fetchCalls.length; }
  function getCount() { return getCalls.length; }
  function el(id) { return els[id]; }
  function noteText() {
    const html = els['#sim-note'].innerHTML;
    if (html) return html.replace(/<[^>]*>/g, '');
    return els['#sim-note'].textContent;
  }
  function noteHtml() { return els['#sim-note'].innerHTML; }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 没任务时点入口只打开文件选择器
  test('无任务时点入口打开文件选择器', () => {
    reset();
    els['#lib-similar-btn'].dispatch('click');
    assert.strictEqual(els['#lib-similar-file'].clicked, 1);
    assert.strictEqual(els['#lib-modal'].style.display, 'none');
  });

  // 2. 上传即时反馈 + 防重复
  test('上传后立即显示检查进度且同次选择只提交一次', async () => {
    reset();
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    const p = simFileChosen({ name: 'q.png' });
    await flush();
    assert.strictEqual(els['#lib-modal'].style.display, '');
    assert.ok(!els['#sim-task'].classList.contains('hidden'));
    const flow = lastPaint('sim-flow');
    assert.ok(flow && flow.text.includes('正在检查图库特征'), flow && flow.text);
    assert.ok(noteText().includes('正在上传查询图'));
    assert.strictEqual(_simStarting, true);
    void simFileChosen({ name: 'q2.png' });
    await flush();
    assert.strictEqual(postCount(), 1);
    resolvePost({ ok: true, status: 200,
                  json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }) });
    await p; await flush();
    assert.strictEqual(store['vixiq.sim.task'], 't1');
    assert.strictEqual(timerCount(), 1);
  });

  // 3. 检查 → 提取计数 → 加载 → 检索 → 结果
  test('进度按真实阶段切换并显示结果', async () => {
    reset();
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    statusHandler = async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) });
    const p = simFileChosen({ name: 'q.png' });
    await flush();
    resolvePost({ ok: true, status: 200,
                  json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }) });
    await p; await flush();
    assert.strictEqual(timerCount(), 1);

    statusHandler = async () => ({ status: 'ok', task: simTask('extracting', {
      id: 't1', done: 2, total: 5, extracted: 1, skipped: 1,
    }) });
    await runNextTimer(); await flush();
    const count = lastPaint('sim-count');
    assert.ok(count && count.hidden === false && count.text.includes('已处理 2／5 张'),
              count && count.text);
    assert.strictEqual(lastPaint('sim-flow').hidden, true);
    assert.ok(noteText().includes('已提取 1 张') && noteText().includes('跳过 1 张'));

    statusHandler = async () => ({ status: 'ok', task: simTask('loading', { id: 't1' }) });
    await runNextTimer(); await flush();
    assert.ok(lastPaint('sim-flow').text.includes('正在加载图像特征'));

    statusHandler = async () => ({ status: 'ok', task: simTask('searching', { id: 't1' }) });
    await runNextTimer(); await flush();
    assert.ok(lastPaint('sim-flow').text.includes('正在查找相似图'));

    const item = { rel: 'X图/shuno_1/900000_p0.png', fusion_score: 0.9,
                   artwork_id: '900000', title: '作品900000', author: 'A',
                   thumb: '/api/local-img?p=x' };
    statusHandler = async () => ({ status: 'ok', task: doneSim([item], {
      total: 5, extracted: 4, skipped: 1, failed: 0, errors: [], index_size: 4,
    }) });
    await runNextTimer(); await flush();
    assert.ok(noteText().includes('找到 1 张相似图片'), noteText());
    assert.ok(noteText().includes('特征库：已提取 4 张'), noteText());
    assert.ok(els['#sim-list'].innerHTML.includes('SAFE:/api/local-img?p=x'));
    assert.ok(els['#sim-list'].innerHTML.includes('90.0%'));
    assert.ok(els['#sim-list'].innerHTML.includes('作品900000'));
    assert.strictEqual(safeThumbCalls.length, 1);
    assert.ok(els['#sim-task'].classList.contains('hidden'));
    assert.strictEqual(timerCount(), 0);
    assert.strictEqual(_simStarting, false);
  });

  // 4. 很快完成只留结果
  test('很快完成直接显示结果', async () => {
    reset();
    const item = { rel: 'a.png', fusion_score: 0.5, artwork_id: '1',
                   title: 'T', author: 'A', thumb: '/x' };
    postHandler = async () => ({ ok: true, status: 200,
      json: async () => ({ status: 'ok', task: doneSim([item]) }) });
    statusHandler = async () => ({ status: 'ok', task: doneSim([item]) });
    await simFileChosen({ name: 'q.png' });
    await flush();
    assert.ok(els['#sim-task'].classList.contains('hidden'));
    assert.ok(noteText().includes('找到 1 张相似图片'));
    assert.strictEqual(timerCount(), 0);
  });

  // 5. 无匹配 / 输入图不可读 / 建库失败 / 检索失败
  test('无匹配与各类失败分别给出明确反馈', async () => {
    reset();
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    statusHandler = async () => ({ status: 'ok', task: doneSim([], {
      total: 0, extracted: 0, skipped: 0, failed: 0, errors: [], index_size: 0,
    }) });
    const p = simFileChosen({ name: 'q.png' });
    await flush();
    resolvePost({ ok: true, status: 200,
                  json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }) });
    await p; await flush();
    await runNextTimer(); await flush();
    assert.ok(noteText().includes('没有找到相似图片'), noteText());
    assert.ok(noteText().includes('特征库为空'), noteText());

    for (const msg of ['无法读取目标图片（可能不是有效图片）',
                       '构建特征库失败：RuntimeError: 模拟',
                       '检索失败：RuntimeError: 模拟']) {
      reset();
      let resolvePost2;
      postHandler = () => new Promise(r => { resolvePost2 = r; });
      statusHandler = async () => ({ status: 'ok', task: {
        id: 't1', status: 'error', stage: '', error: msg, result: null,
      } });
      const pp = simFileChosen({ name: 'q.png' });
      await flush();
      resolvePost2({ ok: true, status: 200,
                     json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }) });
      await pp; await flush();
      await runNextTimer(); await flush();
      assert.ok(noteText().includes('以图搜图失败：' + msg), noteText());
      assert.ok(noteHtml().includes('重新选图'));
      assert.ok(els['#sim-task'].classList.contains('hidden'));
      assert.strictEqual(timerCount(), 0);
    }
  });

  // 6. 启动失败
  test('启动失败显示原因并允许重新选图', async () => {
    reset();
    postHandler = async () => ({ ok: false, status: 500,
      json: async () => ({ status: 'error', message: '缺少图片' }) });
    await simFileChosen({ name: 'q.png' });
    await flush();
    assert.ok(noteText().includes('以图搜图失败：缺少图片'), noteText());
    assert.ok(noteHtml().includes('重新选图'));
    assert.strictEqual(store['vixiq.sim.task'], undefined);
    assert.strictEqual(timerCount(), 0);
  });

  // 7. 新选择替换旧任务, 旧结果不覆盖
  test('新选择替换旧任务且旧结果不覆盖', async () => {
    reset();
    let resolvePost1, resolveGet1;
    postHandler = () => new Promise(r => { resolvePost1 = r; });
    statusHandler = () => new Promise(r => { resolveGet1 = r; });
    const p1 = simFileChosen({ name: 'old.png' });
    await flush();
    resolvePost1({ ok: true, status: 200,
                   json: async () => ({ status: 'ok', task: simTask('checking', { id: 'old' }) }) });
    await p1; await flush();
    assert.strictEqual(_simTaskId, 'old');
    // 旧轮询请求挂起时选新图
    const itemOld = { rel: 'old.png', fusion_score: 0.1, artwork_id: 'OLD',
                      title: '旧结果', author: '', thumb: '/old' };
    postHandler = async () => ({ ok: true, status: 200,
      json: async () => ({ status: 'ok', task: simTask('checking', { id: 'new' }) }) });
    statusHandler = async () => ({ status: 'ok', task: doneSim([{
      rel: 'new.png', fusion_score: 0.9, artwork_id: 'NEW',
      title: '新结果', author: '', thumb: '/new' }], {
      total: 0, extracted: 0, skipped: 0, failed: 0, errors: [], index_size: 1,
    }) });
    await simFileChosen({ name: 'new.png' });
    await flush();
    resolveGet1({ status: 'ok', task: doneSim([itemOld]) });
    await flush();
    assert.ok(!els['#sim-list'].innerHTML.includes('旧结果'), els['#sim-list'].innerHTML);
    assert.ok(els['#sim-list'].innerHTML.includes('新结果'));
  });

  // 8. 关闭后重开原入口接回
  test('关闭后重开入口接回当前任务', async () => {
    reset();
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    statusHandler = async () => ({ status: 'ok', task: simTask('extracting', {
      id: 't1', done: 1, total: 3 }) });
    const p = simFileChosen({ name: 'q.png' });
    await flush();
    resolvePost({ ok: true, status: 200,
                  json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }) });
    await p; await flush();
    assert.strictEqual(timerCount(), 1);
    closeLibModal();
    await runNextTimer(); await flush();
    assert.strictEqual(timerCount(), 0, '关闭后应停止查询');
    els['#lib-similar-btn'].dispatch('click');
    await flush();
    assert.strictEqual(els['#lib-modal'].style.display, '');
    assert.ok(lastPaint('sim-count').text.includes('已处理 1／3 张'),
              lastPaint('sim-count').text);
    assert.strictEqual(timerCount(), 1);
  });

  // 9. 短暂查询失败/超时保留最后进度
  test('短暂查询失败保留进度并继续', async () => {
    reset();
    let resolvePost;
    postHandler = () => new Promise(r => { resolvePost = r; });
    statusHandler = async () => ({ status: 'ok', task: simTask('extracting', {
      id: 't1', done: 2, total: 4 }) });
    const p = simFileChosen({ name: 'q.png' });
    await flush();
    resolvePost({ ok: true, status: 200,
                  json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }) });
    await p; await flush();
    assert.ok(lastPaint('sim-count').text.includes('已处理 2／4 张'));
    statusHandler = async () => { throw new Error('任务查询超时'); };
    await runNextTimer(); await flush();
    assert.ok(lastPaint('sim-count').text.includes('已处理 2／4 张'));
    assert.strictEqual(timerCount(), 1);
    statusHandler = async () => ({ status: 'ok', task: doneSim([], {
      total: 0, extracted: 0, skipped: 0, failed: 0, errors: [], index_size: 0,
    }) });
    await runNextTimer(); await flush();
    assert.ok(noteText().includes('没有找到相似图片'));
    assert.strictEqual(timerCount(), 0);
  });

  // 10. 同一条任务链不并发查询
  test('上一查询未结束时不发下一次', async () => {
    reset();
    let resolvePost, resolveGet;
    postHandler = () => new Promise(r => { resolvePost = r; });
    statusHandler = () => new Promise(r => { resolveGet = r; });
    const p = simFileChosen({ name: 'q.png' });
    await flush();
    resolvePost({ ok: true, status: 200,
                  json: async () => ({ status: 'ok', task: simTask('checking', { id: 't1' }) }) });
    await p; await flush();
    assert.strictEqual(getCount(), 1);
    assert.strictEqual(timerCount(), 0);
    resolveGet({ status: 'ok', task: simTask('extracting', { id: 't1', done: 1, total: 3 }) });
    await flush();
    assert.strictEqual(getCount(), 1);
    assert.strictEqual(timerCount(), 1);
    statusHandler = async () => ({ status: 'ok', task: simTask('extracting', {
      id: 't1', done: 2, total: 3 }) });
    await runNextTimer(); await flush();
    assert.strictEqual(getCount(), 2);
  });

  // 11. 结果里的「重新选图」可再次打开文件选择器
  test('重新选图按钮打开文件选择器', async () => {
    reset();
    postHandler = async () => ({ ok: true, status: 200,
      json: async () => ({ status: 'ok', task: doneSim([], {
        total: 0, extracted: 0, skipped: 0, failed: 0, errors: [], index_size: 0,
      }) }) });
    statusHandler = async () => ({ status: 'ok', task: doneSim([], {
      total: 0, extracted: 0, skipped: 0, failed: 0, errors: [], index_size: 0,
    }) });
    await simFileChosen({ name: 'q.png' });
    await flush();
    assert.ok(noteHtml().includes('simPickFile()'));
    window.simPickFile();
    assert.strictEqual(els['#lib-similar-file'].clicked, 1);
    assert.strictEqual(els['#lib-similar-file'].value, '');
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
  .replace('/*__SIM_SLICE__*/', () => blockSrc);
new Function('assert', inner)(assert);
