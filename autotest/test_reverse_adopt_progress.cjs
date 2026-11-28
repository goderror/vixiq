'use strict';

// 反搜收件箱「选中入库」: 共用后台任务的业务适配。
// 共用引擎由 test_operation_progress_entry.cjs 覆盖; 这里验确认范围/取消不提交、
// 500 上限文案、任务配置、busy/already、结果分行、只按实际移走隐藏、切页接回、
// 行内单张仍走旧同步接口。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// 📥 手动入库:';
const END = '// 🗑️ 丢弃:';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少选中入库代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  function el(id) {
    const set = new Set(['hidden']);
    const obj = {
      id, dataset: {}, value: '', disabled: false, textContent: '',
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
  ['#reverse-adopt-sel', '#reverse-adopt-task', '#reverse-adopt-progress',
   '#reverse-adopt-note'].forEach(s => { els[s] = el(s); });
  function $(sel) { return els[sel] || null; }
  let picks = [];
  function revSelectedRels() {
    return picks.map(p => (p.dataset && (p.dataset.rel || p.dataset.name)) || '')
      .filter(Boolean);
  }
  const document = {
    querySelectorAll: sel => (sel === '#reverse-pending-grid .rev-pick:checked' ? picks : []),
  };
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  let toasts = [];
  function toast(text, kind) { toasts.push([String(text), kind]); }
  let confirmText = '', confirmResult = true;
  function confirm(text) { confirmText = String(text); return confirmResult; }
  let apiCalls = [];
  let apiHandler = async () => ({ status: 'ok' });
  async function api(url, opts) {
    apiCalls.push({ url, opts: opts || {} });
    return apiHandler(url, opts || {});
  }
  let runCalls = [];
  let runResult = { result: {} };
  function runWithProgress(o) { runCalls.push(o); return runResult; }
  let repaints = [], resumes = [];
  function repaintOp(key) { repaints.push(key); return true; }
  function resumeOp(key, o) { resumes.push({ key, o }); return { key }; }
  const _opRuns = new Map();
  function opSessionGet(key) { return sessionIds[key] || ''; }
  const sessionIds = {};
  let drops = [];
  function revDropFromRun(files) { drops.push(files.slice()); }
  let reverseLoads = 0;
  async function loadReverseStatus() { reverseLoads += 1; return {}; }
  function loadLibrary() {} function loadStats() {}
  function loadLibFacets() {} function loadTrashBadge() {}
  const window = {};

  /*__ADOPT_SLICE__*/

  function reset() {
    toasts = [];
    confirmText = '';
    confirmResult = true;
    apiCalls = [];
    runCalls = [];
    runResult = { result: {} };
    repaints = [];
    resumes = [];
    drops = [];
    reverseLoads = 0;
    picks = [];
    _opRuns.clear();
    Object.keys(sessionIds).forEach(k => delete sessionIds[k]);
    apiHandler = async () => ({ status: 'ok' });
    els['#reverse-adopt-note'].innerHTML = '';
    els['#reverse-adopt-task'].classList.add('hidden');
  }
  function resultTask(res, status) {
    return {
      status: status || 'done', stage: '', done: 1, total: 1,
      result: res || {}, removed_rels: [], kept_rels: [],
    };
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  test('取消确认时不提交', async () => {
    reset();
    confirmResult = false;
    picks = [{ dataset: { rel: 'a.png' }, checked: true }];
    await window.revAdopt(null);
    assert.strictEqual(runCalls.length, 0);
    assert.strictEqual(apiCalls.length, 0);
  });

  test('批量确认写明 500 上限与本次范围/超出数量', async () => {
    reset();
    const files = [];
    for (let i = 0; i < 600; i += 1) files.push(`f${i}.png`);
    picks = files.map(rel => ({ dataset: { rel }, checked: true }));
    await window.revAdopt(null);
    assert.ok(confirmText.includes('600 张'), confirmText);
    assert.ok(confirmText.includes('前 500 张'), confirmText);
    assert.ok(confirmText.includes('另有 100 张'), confirmText);
    assert.strictEqual(runCalls.length, 1);
    const o = runCalls[0];
    assert.strictEqual(o.key, 'reverse-adopt');
    assert.strictEqual(o.mode, 'task');
    assert.strictEqual(o.button, '#reverse-adopt-sel');
    assert.strictEqual(o.area, '#reverse-adopt-task');
    assert.strictEqual(o.task.fixedId, undefined);
    assert.ok(o.countText(3, 10).includes('已处理 3／10 张'));
  });

  test('任务启动: 同批 already 接回, 不同批 busy 抛出', async () => {
    reset();
    const o = revAdoptTaskOptions(['a.png']);
    apiHandler = async () => ({ status: 'already', task: resultTask({}, 'running') });
    const same = await o.task.start();
    assert.strictEqual(same.status, 'already');
    apiHandler = async () => ({ status: 'busy', error: '已有入库任务', task: resultTask({}, 'running') });
    let err = '';
    try { await o.task.start(); } catch (e) { err = e.message; }
    assert.ok(err.includes('已有入库任务'), err);
    const body = JSON.parse(apiCalls[apiCalls.length - 1].opts.body);
    assert.deepStrictEqual(body.files, ['a.png']);
  });

  test('结果分行: 已入库/跳过/失败/原件仍在/转换失败/上限', () => {
    const html = revAdoptResultHtml({
      imported: ['#其他来源#/a.png'], skipped: [{ file: 'b.png', reason: '图库里已有同名文件' }],
      failed: [{ file: 'c.png', reason: '登记失败' }], convert_failed: [{ file: 'd.png', reason: 'PNG 转换失败' }],
      source_kept: 1, kept_items: [{ file: 'a.png', reason: '已入库，收件箱原件仍在' }],
      over_limit: 7,
    });
    for (const kw of ['已入库 1 张', '跳过 1 张', '失败 1 张', '转换失败',
                      '已按原格式入库', '收件箱原件仍在', '另有 7 张']) {
      assert.ok(html.includes(kw), html);
    }
  });

  test('完成只按 removed_rels 隐藏任务行; 原件仍在继续可见', async () => {
    reset();
    const o = revAdoptTaskOptions(['a.png', 'b.png']);
    o.onDone({ imported: ['#其他来源#/a.png'], skipped: [], failed: [],
               removed_rels: ['a.png'], kept_rels: ['b.png'], source_kept: 1,
               kept_items: [{ file: 'b.png', reason: '已入库，收件箱原件仍在' }],
               over_limit: 0 },
             { snapshot: { result: { imported: ['#其他来源#/a.png'], removed_rels: ['a.png'],
                                     kept_rels: ['b.png'], source_kept: 1,
                                     kept_items: [{ file: 'b.png' }] } } });
    await Promise.resolve();
    assert.deepStrictEqual(drops[0], ['a.png']);
    assert.ok(!drops[0].includes('b.png'));
    assert.ok(toasts.some(t => t[0].includes('原件仍在 1 张')), toasts);
  });

  test('切页 repaint 接回会话中的任务', () => {
    reset();
    sessionIds['reverse-adopt'] = 'tid-1';
    revAdoptRepaint();
    assert.deepStrictEqual(repaints, ['reverse-adopt']);
    assert.strictEqual(resumes.length, 1);
    assert.strictEqual(resumes[0].key, 'reverse-adopt');
  });

  test('行内单张仍走旧同步接口, 且只隐藏实际移走的行', async () => {
    reset();
    apiHandler = async () => ({ status: 'ok', imported: ['#其他来源#/x.png'],
                                skipped: [], failed: [], convert_failed: [],
                                over_limit: 0, removed_rels: ['x.png'],
                                kept_rels: [], source_kept: 0 });
    await window.revAdopt(['x.png']);
    assert.strictEqual(runCalls.length, 0);
    assert.ok(apiCalls.some(c => c.url === '/api/reverse/adopt'), apiCalls);
    assert.deepStrictEqual(drops[0], ['x.png']);
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
  .replace('/*__ADOPT_SLICE__*/', () => blockSrc) + ')();\n';
new Function('assert', generated)(assert);
