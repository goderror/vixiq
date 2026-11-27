'use strict';

// 批量转 PNG 任务复用与弹窗归属: 直接运行 app.js 内的真实函数。
// 覆盖: 延迟启动回包、同入口重开不重复 POST、延迟刷新完成后不关闭/替换别的弹窗、
//       全部成功与部分失败收尾、异批 busy 回包保留本批。
//
// 做法: 把桩、app.js 真实片段与用例拼进同一个函数作用域执行, 避免 eval/跨脚本
// 拿不到 let 声明的问题。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');

function slice(start, end) {
  const a = src.indexOf(start);
  if (a < 0) throw new Error('缺少起始标记: ' + start);
  const b = src.indexOf(end, a);
  if (b < 0) throw new Error('缺少结束标记: ' + end);
  return src.slice(a, b);
}

const modalSrc = slice('let _opModalSeq = 0;', '// ============ 💾 备份与恢复');
const normSrc = slice('let _normPreview = null;', '// 手动入口: 复用智能刷新的后台进度条与失败重试');
const opSrc = slice('const _opTracks = new Map();', '// ============ 一键整理 (预览后执行) ============');

function harnessBody() {
  // ---- 最小 DOM / 接口桩 ----
  function classList() {
    const set = new Set();
    return {
      add: c => set.add(c),
      remove: c => set.delete(c),
      contains: c => set.has(c),
      toggle: (c, on) => {
        if (on === undefined) { set.has(c) ? set.delete(c) : set.add(c); }
        else if (on) set.add(c); else set.delete(c);
        return set.has(c);
      },
    };
  }

  const elements = new Map();
  function makeEl(id) {
    return {
      id, style: {}, dataset: {}, innerHTML: '', textContent: '',
      classList: classList(), isConnected: true,
      querySelector: () => null,
      querySelectorAll: () => [],
    };
  }
  function getEl(id) {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  }
  function lookupEl(id) { return elements.get(id) || null; }

  const modal = getEl('lib-modal');
  const modalBox = getEl('lib-modal-box');
  const modalTitle = getEl('lib-modal-title');
  const modalBody = getEl('lib-modal-body');
  const modalActions = getEl('lib-modal-actions');

  function $(sel) {
    if (sel === '#lib-modal .modal-box') return modalBox;
    if (sel === '#lib-modal-title') return modalTitle;
    if (sel === '#lib-modal-body') return modalBody;
    if (sel === '#lib-modal') return modal;
    return null;
  }

  const document = {
    getElementById: lookupEl,
    querySelector: sel => (sel === '#lib-modal .modal-actions' ? modalActions : null),
  };

  const window = {};
  window._syncResult = {};
  window.cancelJpgConvert = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function fmtSize(n) { return String(Number(n) || 0); }

  let apiCalls = [];
  let apiHandler = async () => ({ status: 'ok' });
  let api = async (url, opts) => {
    apiCalls.push({ url, opts: opts || {} });
    return apiHandler(url, opts || {});
  };

  let toastLog = [];
  function toast(msg, type) { toastLog.push({ msg, type }); }

  let syncCount = 0;
  let libSyncGate = null;
  function libSync() {
    syncCount += 1;
    return libSyncGate || Promise.resolve();
  }
  let libraryLoads = 0;
  function loadLibrary() { libraryLoads += 1; }
  function loadStats() {}
  function loadLibFacets() {}

  // 轮询桩: 记录回调, 由用例决定何时完成/失败
  let pollCalls = [];

  /*__SLICES__*/

  // 覆盖 app.js 里的真实轮询实现: 用例手动决定任务状态与完成时机
  opPollJobStatus = (url, jobId, onState, opts) => {
    const rec = { url, jobId, onState, opts };
    pollCalls.push(rec);
    return new Promise((resolve, reject) => {
      rec.finish = job => { onState(job); resolve(job); };
      rec.fail = err => reject(err);
    });
  };

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const flush = () => new Promise(r => setTimeout(r, 0));

  function reset() {
    elements.forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.style = {};
      el.dataset = {};
    });
    modal.style.display = 'none';
    _normPreview = null;
    _normApply = null;
    _normPreviewStart = null;
    _normApplyStart = null;
    _normApplyPending = null;
    _normApplyBusyInfo = null;
    _opTracks.clear();
    pollCalls = [];
    apiCalls = [];
    toastLog = [];
    syncCount = 0;
    libraryLoads = 0;
    libSyncGate = null;
    apiHandler = async () => ({ status: 'ok' });
    window._normalizePreview = null;
  }

  function otherModal(title, html) {
    openLibModal(title || '设置', html || '<div id="other-modal">其他内容</div>');
  }

  function plan() {
    window._normalizePreview = {
      ready: [
        { preview_id: 'tok-a', path: 'a.jpg', name: 'a.jpg' },
        { preview_id: 'tok-b', path: 'b.jpg', name: 'b.jpg' },
      ],
    };
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 预览启动回包迟到: 人已换到别的弹窗, 不认领、不覆盖
  test('预览启动迟到回包不接管其他弹窗', async () => {
    reset();
    let resolveStart;
    apiHandler = () => new Promise(r => { resolveStart = r; });
    const p = normalizePreviewStart();
    otherModal('设置', '<div id="settings">设置内容</div>');
    const html = modalBody.innerHTML;
    const seq = modal.dataset.opSeq;
    resolveStart({ status: 'ok', job_id: 'pv-late' });
    await p;
    await flush();
    assert.strictEqual(modalBody.innerHTML, html, '迟到的启动回包不得替换其他弹窗');
    assert.strictEqual(modalTitle.textContent, '设置');
    assert.strictEqual(modal.dataset.opSeq, seq, '其他弹窗的实例号不得被改写');
    assert.strictEqual(_normPreview.id, 'pv-late', '任务状态必须保存');
    assert.ok(pollCalls.some(x => x.jobId === 'pv-late'), '迟到回包仍要跟踪任务');
  });

  // 2. 启动等待中重开入口: 接回同一次等待, 不重复 POST
  test('预览启动等待中重开入口不重复 POST', async () => {
    reset();
    let resolveStart;
    apiHandler = () => new Promise(r => { resolveStart = r; });
    const p = normalizePreviewStart();
    const posts = apiCalls.length;
    closeLibModal();
    window.openNormalizePreview();
    assert.strictEqual(apiCalls.length, posts, '重开入口不得再次 POST');
    assert.strictEqual(modal.dataset.opOwner, 'normalize-preview-start');
    resolveStart({ status: 'ok', job_id: 'pv-reopen' });
    await p;
    await flush();
    assert.strictEqual(modal.dataset.opOwner, 'normalize-preview:pv-reopen');
    assert.strictEqual(_normPreview.id, 'pv-reopen');
  });

  // 3. 预览完成时已换到别的弹窗: 只保存状态
  test('预览完成不替换其他弹窗且保存结果', async () => {
    reset();
    apiHandler = async () => ({ status: 'ok', job_id: 'pv-done' });
    await normalizePreviewStart();
    otherModal('设置', '<div id="settings2">另一处内容</div>');
    const html = modalBody.innerHTML;
    pollCalls[pollCalls.length - 1].finish({
      status: 'ok', id: 'pv-done', state: 'done',
      result: { items: [], ready_count: 0 },
    });
    await flush(); await flush();
    assert.strictEqual(modalBody.innerHTML, html);
    assert.strictEqual(modalTitle.textContent, '设置');
    assert.strictEqual(_normPreview.state, 'done');
  });

  // 4. 预览完成时仍是本任务窗口: 正常显示计划
  test('预览完成仍归属本任务时显示计划', async () => {
    reset();
    apiHandler = async () => ({ status: 'ok', job_id: 'pv-own' });
    await normalizePreviewStart();
    pollCalls[pollCalls.length - 1].finish({
      status: 'ok', id: 'pv-own', state: 'done',
      result: {
        items: [{ preview_id: 't1', path: 'a.jpg', name: 'a.jpg', size: 10,
                  png_size: 8, saved_bytes: 2, saved_pct: 20, format: 'jpeg' }],
        ready_count: 1, conflict_count: 0, error_count: 0,
        total_size: 10, total_png_size: 8, saved_bytes: 2,
      },
    });
    await flush(); await flush();
    assert.strictEqual(modalTitle.textContent, '🖼️ 转为 PNG');
    assert.ok(modalBody.innerHTML.includes('转换计划'), '应显示预览计划');
  });

  // 5. 执行启动回包迟到: 不接管其他弹窗, 状态仍可接回
  test('执行启动迟到回包不接管其他弹窗', async () => {
    reset();
    plan();
    let resolveStart;
    apiHandler = () => new Promise(r => { resolveStart = r; });
    const p = window.applyNormalizePending();
    otherModal('设置', '<div id="settings3">别处内容</div>');
    const html = modalBody.innerHTML;
    resolveStart({ status: 'ok', job_id: 'ap-late', total: 2 });
    await p;
    await flush();
    assert.strictEqual(modalBody.innerHTML, html);
    assert.strictEqual(modalTitle.textContent, '设置');
    assert.strictEqual(_normApply.id, 'ap-late');
    assert.ok(pollCalls.some(x => x.jobId === 'ap-late'));
  });

  // 6. 执行启动等待中重开入口: 不重复 POST, 回包后接回任务
  test('执行启动等待中重开入口不重复 POST', async () => {
    reset();
    plan();
    let resolveStart;
    apiHandler = () => new Promise(r => { resolveStart = r; });
    const p = window.applyNormalizePending();
    const posts = apiCalls.length;
    closeLibModal();
    window.openNormalizePreview();
    assert.strictEqual(apiCalls.length, posts);
    assert.strictEqual(modal.dataset.opOwner, 'normalize-apply-start');
    resolveStart({ status: 'ok', job_id: 'ap-reopen', total: 2 });
    await p;
    await flush();
    assert.strictEqual(modal.dataset.opOwner, 'normalize-apply:ap-reopen');
    assert.strictEqual(_normApply.id, 'ap-reopen');
  });

  // 7. 异批 busy 回包: 明确提示另一批在跑, 保留本批参数可供重试
  test('异批 busy 明确提示并保留本批', async () => {
    reset();
    plan();
    apiHandler = async () => ({
      status: 'error', busy: true, job_id: 'other-job', total: 2,
      overlap: false, message: '已有另一批转换正在执行',
    });
    await window.applyNormalizePending();
    assert.ok(modalTitle.textContent.includes('另一批转换正在执行'), modalTitle.textContent);
    assert.ok(modalBody.innerHTML.includes('重试本批'), '未共用预览时应提供重试本批');
    assert.strictEqual(_normApply, null, '被挡下的批次不得记成自己的任务');
    assert.deepStrictEqual(_normApplyPending.items.map(x => x.preview_id), ['tok-a', 'tok-b']);
    assert.strictEqual(_normApplyPending.recycle, true);
    // 重试本批按原清单与选项重新提交
    apiHandler = async () => ({ status: 'ok', job_id: 'ap-retry', total: 2 });
    window.normalizeApplyRetryPending();
    await flush();
    const last = apiCalls[apiCalls.length - 1];
    const body = JSON.parse(last.opts.body);
    assert.deepStrictEqual(body.items.map(x => x.preview_id), ['tok-a', 'tok-b']);
    assert.strictEqual(body.recycle_old, true);
  });

  // 8. 共用预览的异批: 提示重新预览, 不提供会失效的重试
  test('异批共用预览时提示重新预览', async () => {
    reset();
    plan();
    apiHandler = async () => ({
      status: 'error', busy: true, job_id: 'other-job', total: 2,
      overlap: true, message: '已有另一批转换正在执行',
    });
    await window.applyNormalizePending();
    assert.ok(modalTitle.textContent.includes('另一批转换正在执行'));
    assert.ok(!modalBody.innerHTML.includes('重试本批'));
    assert.ok(modalBody.innerHTML.includes('重新预览'));
    assert.strictEqual(_normApplyPending.overlap, true);
  });

  // 9. 执行完成 + 延迟刷新: 期间切到其他弹窗, 刷新后不得关闭/替换它
  test('执行完成延迟刷新不替换其他弹窗', async () => {
    reset();
    plan();
    apiHandler = async () => ({ status: 'ok', job_id: 'ap-late-refresh', total: 2 });
    await window.applyNormalizePending();
    const rec = pollCalls[pollCalls.length - 1];
    let release;
    libSyncGate = new Promise(r => { release = r; });
    rec.finish({
      status: 'ok', id: 'ap-late-refresh', state: 'done',
      result: { converted: 2, failed: 0, items: [
        { status: 'ok', path: 'a.jpg' }, { status: 'ok', path: 'b.jpg' } ] },
    });
    await sleep(320);          // 已画完并进入图库刷新等待
    otherModal('设置', '<div id="settings4">刷新期间打开</div>');
    const html = modalBody.innerHTML;
    const title = modalTitle.textContent;
    const seq = modal.dataset.opSeq;
    release();
    await sleep(30); await flush();
    assert.strictEqual(modal.style.display, 'flex', '其他弹窗必须保持可见');
    assert.strictEqual(modalBody.innerHTML, html, '其他弹窗内容不得被替换');
    assert.strictEqual(modalTitle.textContent, title);
    assert.strictEqual(modal.dataset.opSeq, seq);
    assert.strictEqual(_normApply.state, 'done');
    assert.ok(!toastLog.some(t => t.type === 'ok'), '不得对别的弹窗弹成功提示');
  });

  // 10. 执行完成仍归属本任务 + 全部成功: 关闭等待窗并提示
  test('全部成功仍归属本任务时关闭等待窗', async () => {
    reset();
    plan();
    apiHandler = async () => ({ status: 'ok', job_id: 'ap-ok', total: 2 });
    await window.applyNormalizePending();
    pollCalls[pollCalls.length - 1].finish({
      status: 'ok', id: 'ap-ok', state: 'done',
      result: { converted: 2, failed: 0, items: [
        { status: 'ok', path: 'a.jpg' }, { status: 'ok', path: 'b.jpg' } ] },
    });
    await sleep(320); await flush();
    assert.strictEqual(modal.style.display, 'none');
    assert.ok(toastLog.some(t => t.type === 'ok' && t.msg.includes('已转为 PNG')));
  });

  // 11. 执行完成仍归属本任务 + 部分失败: 显示转换结果
  test('部分失败仍归属本任务时显示结果', async () => {
    reset();
    plan();
    apiHandler = async () => ({ status: 'ok', job_id: 'ap-part', total: 2 });
    await window.applyNormalizePending();
    pollCalls[pollCalls.length - 1].finish({
      status: 'ok', id: 'ap-part', state: 'done',
      result: { converted: 1, failed: 1, items: [
        { status: 'ok', path: 'a.jpg' },
        { status: 'error', path: 'b.jpg', error: '源文件在预览后发生变化，请重新预览' } ] },
    });
    await sleep(320); await flush();
    assert.strictEqual(modalTitle.textContent, '🖼️ 转换结果');
    assert.ok(modalBody.innerHTML.includes('失败 1 张'));
    assert.ok(modalBody.innerHTML.includes('b.jpg'));
  });

  // 12. 查询中断: 仍归属本任务才提供继续查询, 其他弹窗不受影响
  test('查询中断只在本任务窗口提供继续查询', async () => {
    reset();
    plan();
    apiHandler = async () => ({ status: 'ok', job_id: 'ap-net', total: 2 });
    await window.applyNormalizePending();
    const rec = pollCalls[pollCalls.length - 1];
    otherModal('设置', '<div id="settings5">中断时打开</div>');
    const html = modalBody.innerHTML;
    rec.fail(new Error('网络断开'));
    await flush(); await flush();
    assert.strictEqual(modalBody.innerHTML, html, '中断也只更新自己的弹窗');
    assert.strictEqual(_normApply.pollError, '网络断开');
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
  .replace('/*__SLICES__*/', () => modalSrc + '\n' + normSrc + '\n' + opSrc);
new Function('assert', inner)(assert);
