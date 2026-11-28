'use strict';

// 图库单图反搜来源: 共用 task 跟踪的业务适配。
// 覆盖: 启动快照只表示已受理 (不伪造 total)、单页保持流动/多页才计数、
// 只认本 artwork_id 的状态、409 不启动、完成/失败结果落位。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = 'async function runSourceSearch(';
const END = 'function renderGallerySearchResult(';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少 runSourceSearch 代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  const els = {};
  function el(id) {
    const obj = { id, innerHTML: '', dataset: {} };
    return obj;
  }
  function $(sel) { return els[sel] || null; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  let galleryAid = null;
  let captured = null;
  function runWithProgress(o) { captured = o; return { result: null, error: '' }; }
  function switchTab() {}
  let apiCalls = [];
  let apiHandler = async () => ({ status: 'started' });
  async function api(url, opts) {
    apiCalls.push({ url, opts: opts || {} });
    return apiHandler(url, opts || {});
  }
  let reverseLoads = 0;
  async function loadReverseStatus() { reverseLoads += 1; return {}; }
  let rendered = [];
  function renderGallerySearchResult(aid, hits) { rendered.push({ aid, hits }); }
  function renderReverseTasks() {}
  function urlFormHtml(aid) { return `<div>url-form:${aid}</div>`; }
  function reset() {
    els['#rev-gallery-result'] = el('rev-gallery-result');
    galleryAid = null;
    captured = null;
    apiCalls = [];
    rendered = [];
    reverseLoads = 0;
    apiHandler = async () => ({ status: 'started' });
  }

  /*__SRC_SLICE__*/

  const ss = (aid, total, done, extra) => ({
    src_search: Object.assign({ running: true, artwork_id: aid,
                                total, done, phase: 'running' }, extra || {}),
  });

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  test('启动快照只表示已受理, 不伪造 total/done', async () => {
    reset();
    await runSourceSearch('123');
    assert.ok(captured, '未调用共用入口');
    assert.strictEqual(captured.mode, 'task');
    const snap = captured.task.snapshotOf();
    assert.strictEqual(snap.src_search.running, true);
    assert.ok(!('total' in snap.src_search), snap);
    assert.ok(!('done' in snap.src_search), snap);
    assert.strictEqual(captured.task.fastFollowUpMs, 180,
                       '首查 running 后应在约 180ms 补查一次');
  });

  test('单页保持流动, 多页才显示真实计数', async () => {
    reset();
    await runSourceSearch('123');
    const map = captured.task.map;
    assert.strictEqual(map.total(ss('123', 1, 0)), 0);
    assert.strictEqual(map.done(ss('123', 1, 0)), 0);
    assert.strictEqual(map.total(ss('123', 3, 1)), 3);
    assert.strictEqual(map.done(ss('123', 3, 1)), 1);
  });

  test('只认本 artwork_id, 不串批量/别的作品结果', async () => {
    reset();
    await runSourceSearch('123');
    const map = captured.task.map;
    assert.strictEqual(map.running(ss('123', 2, 0)), true);
    assert.strictEqual(map.running(ss('999', 2, 0)), false);
    assert.strictEqual(map.running(ss(null, 2, 0)), false);
    assert.deepStrictEqual(map.result(ss('123', 2, 2, { result: [{ site: 'pixiv' }] })),
                           [{ site: 'pixiv' }]);
    assert.strictEqual(map.result(ss('999', 2, 2, { result: [{ site: 'x' }] })), null);
    assert.strictEqual(map.error(ss('123', 2, 1, { phase: 'error', error: '坏了' })), '坏了');
  });

  test('启动请求带 force 参数; 409 直接失败', async () => {
    reset();
    await runSourceSearch('123', true);
    await captured.task.start();
    const body = JSON.parse(apiCalls[0].opts.body);
    assert.strictEqual(body.artwork_id, '123');
    assert.strictEqual(body.saucenao, 'force');
    apiHandler = async () => ({ status: 'error', error: '已有反搜来源任务进行中' });
    let err = '';
    try { await captured.task.start(); } catch (e) { err = e.message; }
    assert.ok(err.includes('已有反搜来源任务进行中'), err);
  });

  test('完成渲染结果; 失败显示原因和 URL 表单', async () => {
    reset();
    await runSourceSearch('123');
    await captured.onDone([{ site: 'pixiv', artwork_id: '456' }]);
    assert.strictEqual(galleryAid, null);
    assert.strictEqual(reverseLoads, 1);
    assert.deepStrictEqual(rendered[0], {
      aid: '123', hits: [{ site: 'pixiv', artwork_id: '456' }],
    });
    captured.onError(new Error('网络中断'));
    assert.ok(els['#rev-gallery-result'].innerHTML.includes('反搜失败'), els['#rev-gallery-result'].innerHTML);
    assert.ok(els['#rev-gallery-result'].innerHTML.includes('url-form:123'));
  });

  (async () => {
    let failed = 0;
    for (const [name, fn] of cases) {
      try { await fn(); console.log('ok - ' + name); }
      catch (e) {
        failed += 1;
        console.error('not ok - ' + name);
        console.error(e && e.stack || e);
      }
    }
    if (failed) { console.error(failed + ' failed'); process.exit(1); }
    console.log('NODE OK');
  })();
}

const generated = '(' + harnessBody.toString()
  .replace('/*__SRC_SLICE__*/', () => blockSrc) + ')();\n';
new Function('assert', generated)(assert);
