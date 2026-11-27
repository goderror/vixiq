'use strict';

// 单张 PNG 转 JPG 进度: 直接运行 app.js 内的真实预览/确认函数。
// 覆盖: 质量预览流动条 → 结果收起; 确认转换分段充能 (编码 → 校验并更新登记);
//       成功收尾关闭, 失败保留弹窗并标红当前段。

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

const jpgSrc = slice('function fmtConversion(rec) {', 'window.openLightbox = function');

function harnessBody() {
  let progressCalls = [];
  function opProgressHtml(wid) { return `<div id="${wid}"></div>`; }
  function opProgressPaint(wid, o) { progressCalls.push({ wid, o: Object.assign({}, o) }); }

  const $ = () => null;
  const document = { getElementById: () => null };
  const window = {};
  function esc(s) { return String(s == null ? '' : s); }
  function fmtSize(n) { return String(Number(n) || 0); }

  let lastModal = null;
  let closeCount = 0;
  function openLibModal(title, html) { lastModal = { title, html }; }
  function closeLibModal() {
    closeCount += 1;
    if (window.cancelJpgConvert) window.cancelJpgConvert();
  }

  let toastLog = [];
  function toast(msg, type) { toastLog.push({ msg, type }); }

  let apiCalls = [];
  let apiHandler = async () => ({ status: 'error', error: '未设置' });
  async function api(url, opts) {
    apiCalls.push({ url, opts: opts || {} });
    return apiHandler(url, opts || {});
  }

  const libSelected = new Set();
  let libraryLoads = 0;
  function loadLibrary() { libraryLoads += 1; }
  function loadStats() {}
  function loadLibFacets() {}

  /*__JPG_SLICE__*/

  const flush = () => new Promise(r => setTimeout(r, 0));
  function lastPaint(wid) {
    for (let i = progressCalls.length - 1; i >= 0; i -= 1) {
      if (progressCalls[i].wid === wid) return progressCalls[i].o;
    }
    return null;
  }
  function reset() {
    progressCalls = [];
    apiCalls = [];
    toastLog = [];
    lastModal = null;
    closeCount = 0;
    libraryLoads = 0;
    apiHandler = async () => ({ status: 'error', error: '未设置' });
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 质量预览: 在途流动条, 结果回来收起
  test('质量预览显示流动条并在结果回来后收起', async () => {
    reset();
    let resolvePreview;
    apiHandler = () => new Promise(r => { resolvePreview = r; });
    window.openJpgConvert('1', 0, '测试');
    let flow = lastPaint('jpg-preview-progress');
    assert.ok(flow && flow.hidden === false, '预览开始应显示流动条');
    assert.strictEqual(flow.stage, '正在生成质量预览');
    resolvePreview({ status: 'ok', quality: 85, preview_id: 'p1',
                     current_size: 100, jpg_size: 80, saved_bytes: 20,
                     saved_pct: 20, width: 8, height: 8,
                     has_alpha: false, disk_note: '只保留一份 JPG' });
    await flush(); await flush();
    flow = lastPaint('jpg-preview-progress');
    assert.ok(flow && flow.hidden === true, '结果回来后应收起流动条');
    assert.strictEqual(window._jpgConvert.previewId, 'p1');
  });

  // 2. 确认转换: 分段充能从「编码」开始, 成功后两段完成并关闭
  test('确认转换显示分段充能并在成功后完成', async () => {
    reset();
    let resolvePreview;
    let resolveApply;
    apiHandler = (url) => {
      if (url.includes('/preview')) return new Promise(r => { resolvePreview = r; });
      return new Promise(r => { resolveApply = r; });
    };
    window.openJpgConvert('2', 0, '测试');
    resolvePreview({ status: 'ok', quality: 85, preview_id: 'p2',
                     current_size: 100, jpg_size: 80, saved_bytes: 20,
                     saved_pct: 20, width: 8, height: 8, has_alpha: false });
    await flush(); await flush();
    const p = window.applyJpgConvert();
    let bar = lastPaint('jpg-apply-progress');
    assert.ok(bar && bar.hidden === false, '确认后应显示分段充能条');
    assert.strictEqual(bar.stageIndex, 0, '从「编码」段开始');
    assert.strictEqual(bar.running, true);
    resolveApply({ status: 'ok', current_size: 100, new_size: 80,
                   saved_bytes: 20, retry_recycle: false });
    await p; await flush();
    bar = lastPaint('jpg-apply-progress');
    assert.strictEqual(bar.stageIndex, 2, '成功后两段都完成');
    assert.strictEqual(bar.running, false);
    assert.ok(closeCount >= 1, '成功后应关闭弹窗');
    assert.ok(toastLog.some(t => t.type === 'ok' && t.msg.includes('已转为 JPG')));
  });

  // 3. 确认失败: 第一段标红, 弹窗保留并提示原因
  test('确认失败标红当前段并保留弹窗', async () => {
    reset();
    let resolvePreview;
    let resolveApply;
    apiHandler = (url) => {
      if (url.includes('/preview')) {
        return new Promise(r => { resolvePreview = r; });
      }
      return new Promise(r => { resolveApply = r; });
    };
    window.openJpgConvert('3', 0, '测试');
    resolvePreview({ status: 'ok', quality: 85, preview_id: 'p3',
                     current_size: 100, jpg_size: 80, saved_bytes: 20,
                     saved_pct: 20, width: 8, height: 8, has_alpha: false });
    await flush(); await flush();
    const p = window.applyJpgConvert();
    resolveApply({ status: 'error', error: '模拟编码失败' });
    await p; await flush();
    const bar = lastPaint('jpg-apply-progress');
    assert.ok(bar && bar.failedIndex === 0, '失败时当前段标红');
    assert.strictEqual(bar.running, false);
    assert.ok(!toastLog.some(t => t.type === 'ok'), '失败不得提示成功');
    assert.ok(toastLog.some(t => t.type === 'err' && t.msg.includes('模拟编码失败')));
    assert.strictEqual(closeCount, 0, '失败时弹窗应保留');
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
  .replace('/*__JPG_SLICE__*/', () => jpgSrc);
new Function('assert', inner)(assert);
