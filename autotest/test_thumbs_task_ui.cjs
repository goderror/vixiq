'use strict';

// 设置页缩略图缓存任务进度: 直接运行 app.js 内的真实函数。
// 覆盖: 流动/计数切换、三操作共用任务区、结果文案、关闭设置再回来接回任务。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// ============ 🖼️ 缩略图缓存管理 (设置页) ============';
const END = '// ============ 退出程序 (设置页) ============';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少缩略图缓存任务代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  function makeEl() {
    const set = new Set();
    return {
      innerHTML: '', textContent: '', dataset: {}, disabled: false, style: {},
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
  ['#thumbs-stats', '#thumbs-task', '#thumbs-task-progress', '#thumbs-task-note',
   '#thumbs-warm-btn', '#thumbs-orphan-btn', '#thumbs-clear-btn'].forEach(s => {
    els[s] = makeEl();
  });
  function $(sel) { return els[sel] || null; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  let progressCalls = [];
  function opProgressHtml(wid) { return `<div id="${wid}"></div>`; }
  function opProgressPaint(wid, o) {
    progressCalls.push({ wid, o: Object.assign({}, o) });
  }
  function opRevealProgressIn() {}

  let apiCalls = [];
  let apiHandler = async () => ({ status: 'ok', task: { state: 'idle' } });
  async function api(url, opts) {
    apiCalls.push({ url, opts: opts || {} });
    return apiHandler(url, opts || {});
  }

  let toastLog = [];
  function toast(msg, type) { toastLog.push({ msg, type }); }
  function confirm() { return true; }

  let intervalFn = null;
  let intervalCleared = false;
  function setInterval(fn) { intervalFn = fn; intervalCleared = false; return 1; }
  function clearInterval() { intervalFn = null; intervalCleared = true; }

  /*__THUMB_SLICE__*/

  const sleep = ms => new Promise(r => setTimeout(r, ms));
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
    intervalFn = null;
    intervalCleared = false;
    _thumbsTaskTimer = null;
    _thumbsLastStats = null;
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.disabled = false;
    });
    apiHandler = async () => ({ status: 'ok', task: { state: 'idle' } });
  }
  function runningTask(kind, stage, done, total) {
    return { id: 't1', kind, state: 'running', stage, done, total,
             result: null, error: null };
  }
  function doneTask(kind, result) {
    return { id: 't1', kind, state: 'done', stage: '完成', done: 0, total: 0,
             result, error: null, message: '' };
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 流动阶段: 只显示流动条, 按钮禁用
  test('核对阶段显示流动条并禁用按钮', () => {
    reset();
    paintThumbsTask(runningTask('clear-orphans', '核对缩略图', 0, 0));
    assert.ok(!els['#thumbs-task'].classList.contains('hidden'));
    const flow = lastPaint('thumbs-task-flow');
    assert.ok(flow && flow.hidden === false && flow.mode === 'flow');
    assert.strictEqual(flow.stage, '核对缩略图');
    assert.strictEqual(lastPaint('thumbs-task-count').hidden, true);
    assert.ok(els['#thumbs-task-note'].textContent.includes('核对缩略图'));
    assert.strictEqual(els['#thumbs-warm-btn'].disabled, true);
    assert.strictEqual(els['#thumbs-orphan-btn'].disabled, true);
    assert.strictEqual(els['#thumbs-clear-btn'].disabled, true);
  });

  // 2. 锁定范围后切换计数条
  test('锁定范围后显示计数条', () => {
    reset();
    paintThumbsTask(runningTask('clear-orphans', '清理失效缩略图', 3, 10));
    assert.strictEqual(lastPaint('thumbs-task-flow').hidden, true);
    const count = lastPaint('thumbs-task-count');
    assert.ok(count && count.hidden === false);
    assert.strictEqual(count.done, 3);
    assert.strictEqual(count.total, 10);
    assert.ok(count.text.includes('已清理 3／10 张'), count.text);
  });

  // 3. 清空缓存停止预热阶段: flow, 结果文案含已停止预热
  test('清空缓存结果提示已停止预热', () => {
    reset();
    paintThumbsTask(doneTask('clear', {
      deleted: 4, total: 4, failed: [], was_warming: true, stopped_warm: true,
    }));
    assert.strictEqual(lastPaint('thumbs-task-flow').hidden, true);
    assert.strictEqual(lastPaint('thumbs-task-count').hidden, true);
    assert.ok(els['#thumbs-task-note'].textContent.includes('已清空 4 个缓存文件'));
    assert.ok(els['#thumbs-task-note'].textContent.includes('已停止预热'));
    assert.strictEqual(els['#thumbs-warm-btn'].disabled, false);
  });

  // 4. 失效缩略图结果/空结果/跳过根
  test('清理失效缩略图结果文案', () => {
    reset();
    paintThumbsTask(doneTask('clear-orphans', {
      deleted: 7, total: 7, failed: [], scan_complete: true,
    }));
    assert.ok(els['#thumbs-task-note'].textContent.includes('已清理 7 张失效缩略图'));
    paintThumbsTask(doneTask('clear-orphans', {
      deleted: 0, total: 0, failed: [], scan_complete: true,
    }));
    assert.strictEqual(els['#thumbs-task-note'].textContent, '没有失效缩略图');
    paintThumbsTask(doneTask('clear-orphans', {
      deleted: 0, total: 0, failed: [], scan_complete: false,
      skipped_roots: ['wall'],
    }));
    assert.ok(els['#thumbs-task-note'].textContent.includes('部分缓存根暂时不可用'));
    assert.ok(els['#thumbs-task-note'].textContent.includes('wall'));
  });

  // 5. 预热结果: 已检查/已生成/已存在/失败
  test('预热结果文案区分生成与已存在', () => {
    reset();
    paintThumbsTask(doneTask('warm', {
      total: 5, generated: 2, existing: 3, failed: 0,
    }));
    const note = els['#thumbs-task-note'].textContent;
    assert.ok(note.includes('已检查 5 张'), note);
    assert.ok(note.includes('已生成 2 张'), note);
    assert.ok(note.includes('已存在 3 张'), note);
    assert.ok(note.includes('失败 0 张'), note);
  });

  // 6. 关闭设置再回来: 接回运行中任务并继续轮询
  test('重开设置接回运行中任务', async () => {
    reset();
    apiHandler = async () => ({
      status: 'ok', task: runningTask('warm', '预热缩略图', 2, 5),
    });
    await loadThumbsTask();
    await flush();
    assert.ok(intervalFn, '运行中任务应启动轮询');
    const count = lastPaint('thumbs-task-count');
    assert.ok(count && count.done === 2 && count.total === 5);
    assert.strictEqual(els['#thumbs-task'].classList.contains('hidden'), false);

    apiHandler = async () => ({
      status: 'ok', task: doneTask('warm', {
        total: 5, generated: 2, existing: 3, failed: 0,
      }),
    });
    await intervalFn();
    await flush();
    assert.ok(intervalCleared, '完成后应停止轮询');
    assert.ok(els['#thumbs-task-note'].textContent.includes('已检查 5 张'));
  });

  // 7. 启动按钮: POST action, 立即禁用, 轮询接回
  test('启动清理任务后禁用按钮并轮询', async () => {
    reset();
    let sent = null;
    apiHandler = async (url, opts) => {
      if (opts && opts.method === 'POST') {
        sent = JSON.parse(opts.body);
        return { status: 'ok', task: runningTask('clear-orphans', '核对缩略图', 0, 0) };
      }
      return { status: 'ok', task: runningTask('clear-orphans', '清理失效缩略图', 1, 2) };
    };
    await startThumbsTask('clear-orphans');
    await flush();
    assert.deepStrictEqual(sent, { action: 'clear-orphans' });
    assert.ok(intervalFn);
    assert.strictEqual(els['#thumbs-clear-btn'].disabled, true);
    const count = lastPaint('thumbs-task-count');
    assert.ok(count && count.done === 1 && count.total === 2);
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
  .replace('/*__THUMB_SLICE__*/', () => blockSrc);
new Function('assert', inner)(assert);
