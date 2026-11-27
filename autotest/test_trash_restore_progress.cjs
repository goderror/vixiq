'use strict';

// 图库回收站「全部恢复」共用进度: 直接运行 app.js 内的真实函数。
// 覆盖作品/图片两种视图的流动与计数、恢复成功/冲突跳过/文件搬回失败结果、
// 失败解锁、关闭回收站页再回来接回任务。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// ============ ♻️ 回收站全部恢复进度 ============';
const END = '// ============ 🗑️ 回收站清空/彻底删除 ============';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少回收站全部恢复进度代码块');
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
  ['#trash-restore-all', '#trash-restore-task',
   '#trash-restore-progress', '#trash-restore-note'].forEach(s => {
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
  let confirmResult = true;
  let confirmLog = [];
  function confirm(msg) { confirmLog.push(msg); return confirmResult; }

  let intervalFn = null;
  let intervalCleared = false;
  function setInterval(fn) { intervalFn = fn; intervalCleared = false; return 1; }
  function clearInterval() { intervalFn = null; intervalCleared = true; }

  let reloads = 0;
  function loadTrash() { reloads += 1; }
  function loadLibrary() {}
  function loadStats() {}
  function loadLibFacets() {}
  function wallTrashAll() {}

  const window = {};

  let trashKind = 'artwork';
  let _trashListCount = 0;
  let _trashRestoreRunning = false;
  let _trashRestoreLocking = false;
  let _trashRestoreTimer = null;
  let _trashRestoreStatsTick = 0;

  /*__TRASH_SLICE__*/

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
    confirmLog = [];
    confirmResult = true;
    intervalFn = null;
    intervalCleared = false;
    _trashRestoreTimer = null;
    _trashRestoreRunning = false;
    _trashRestoreLocking = false;
    _trashListCount = 0;
    trashKind = 'artwork';
    reloads = 0;
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.disabled = false;
    });
    apiHandler = async () => ({ status: 'ok', task: { state: 'idle' } });
  }
  function runningTask(kind, stage, done, total) {
    return { id: 'r1', kind, state: 'running', stage, done, total,
             message: '', result: null, error: null };
  }
  function doneTask(kind, result) {
    return { id: 'r1', kind, state: 'done', stage: '完成', done: 0, total: 0,
             message: '', result, error: null };
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 作品视图流动阶段 + 按钮禁用
  test('作品视图锁定候选时显示流动条并禁用按钮', () => {
    reset();
    _trashListCount = 3;
    paintTrashRestoreTask(runningTask('artwork', '锁定候选', 0, 0));
    assert.ok(!els['#trash-restore-task'].classList.contains('hidden'));
    const flow = lastPaint('trash-restore-flow');
    assert.ok(flow && flow.hidden === false && flow.mode === 'flow');
    assert.strictEqual(lastPaint('trash-restore-count').hidden, true);
    assert.strictEqual(els['#trash-restore-all'].disabled, true);
    assert.ok(els['#trash-restore-note'].textContent.includes('锁定候选'));
  });

  // 2. 执行进度按本次文件清单计数 (统一「张」)
  test('执行进度按文件数计数', () => {
    reset();
    _trashListCount = 5;
    paintTrashRestoreTask(runningTask('artwork', '恢复中', 2, 5));
    const c = lastPaint('trash-restore-count');
    assert.ok(c && c.hidden === false);
    assert.ok(c.text.includes('已处理 2／5 张'), c.text);
    assert.strictEqual(lastPaint('trash-restore-flow').hidden, true);
  });
  test('图片视图执行同样按文件数计数', () => {
    reset();
    _trashListCount = 3;
    paintTrashRestoreTask(runningTask('page', '恢复中', 1, 3));
    const c = lastPaint('trash-restore-count');
    assert.ok(c.text.includes('已处理 1／3 张'), c.text);
  });

  // 3. 恢复成功/冲突跳过/文件搬回失败结果
  test('全部成功显示恢复数量并解锁', () => {
    reset();
    _trashListCount = 5;
    paintTrashRestoreTask(doneTask('artwork', {
      kind: 'artwork', total: 5, restored: 5, skipped: [], move_failed: [],
      files_moved: 5,
    }));
    assert.strictEqual(lastPaint('trash-restore-flow').hidden, true);
    assert.strictEqual(lastPaint('trash-restore-count').hidden, true);
    assert.ok(els['#trash-restore-note'].innerHTML.includes('已恢复 5 个作品'));
    assert.strictEqual(els['#trash-restore-all'].disabled, false);
  });
  test('冲突跳过与搬回失败分别列出原因', () => {
    reset();
    _trashListCount = 4;
    paintTrashRestoreTask(doneTask('artwork', {
      kind: 'artwork', total: 4, restored: 2,
      skipped: [{ item: { artwork_id: '111', page_num: 0 },
                  detail: '原路径已被另一份不同内容的文件占用：a.png' }],
      move_failed: [{ item: { artwork_id: '222' },
                      errors: ['222/p0__b.png: 文件被占用'] }],
      files_moved: 2,
    }));
    const html = els['#trash-restore-note'].innerHTML;
    assert.ok(html.includes('已恢复 2 个作品'), html);
    assert.ok(html.includes('跳过 1'), html);
    assert.ok(html.includes('文件搬回失败 1'), html);
    assert.ok(html.includes('作品 111 第 1 张'), html);
    assert.ok(html.includes('原路径已被另一份不同内容的文件占用'), html);
    assert.ok(html.includes('作品 222'), html);
    assert.ok(html.includes('文件被占用'), html);
    assert.strictEqual(els['#trash-restore-all'].disabled, false);
  });
  test('图片视图结果按张显示', () => {
    reset();
    _trashListCount = 3;
    paintTrashRestoreTask(doneTask('page', {
      kind: 'page', total: 3, restored: 1,
      skipped: [{ item: { artwork_id: '333', page_num: 1 }, detail: '冲突' }],
      move_failed: [], files_moved: 1,
    }));
    const html = els['#trash-restore-note'].innerHTML;
    assert.ok(html.includes('已恢复 1 张图片'), html);
    assert.ok(html.includes('作品 333 第 2 张'), html);
  });

  // 4. 失败终态解锁
  test('恢复失败显示原因并解锁按钮', () => {
    reset();
    _trashListCount = 3;
    paintTrashRestoreTask({ id: 'r1', kind: 'page', state: 'error', stage: '',
                            done: 0, total: 0, result: null,
                            error: '无法启动后台任务：模拟失败' });
    assert.ok(els['#trash-restore-note'].innerHTML.includes('无法启动后台任务'));
    assert.strictEqual(els['#trash-restore-all'].disabled, false);
  });

  // 5. 关闭回收站页再回来: 接回运行中任务
  test('重开回收站接回运行中任务', async () => {
    reset();
    _trashListCount = 3;
    apiHandler = async () => ({
      status: 'ok', task: runningTask('page', '恢复中', 1, 3),
    });
    await loadTrashRestoreTask();
    await flush();
    assert.ok(intervalFn, '运行中任务应启动轮询');
    assert.ok(lastPaint('trash-restore-count').text.includes('已处理 1／3 张'));
    assert.strictEqual(els['#trash-restore-all'].disabled, true);

    apiHandler = async () => ({
      status: 'ok', task: doneTask('page', {
        kind: 'page', total: 3, restored: 3, skipped: [], move_failed: [],
        files_moved: 3,
      }),
    });
    await intervalFn();
    await flush();
    assert.ok(intervalCleared, '完成后应停止轮询');
    assert.ok(els['#trash-restore-note'].innerHTML.includes('已恢复 3 张图片'));
    assert.ok(reloads >= 1, '完成后应刷新回收站列表');
  });

  // 6. 启动全部恢复: POST 固定批次, 立即禁用, 轮询接回
  test('启动恢复任务提交固定批次并轮询', async () => {
    reset();
    _trashListCount = 2;
    let sent = null;
    apiHandler = async (url, opts) => {
      if (opts && opts.method === 'POST') {
        sent = JSON.parse(opts.body);
        return { status: 'ok', task: runningTask('artwork', '恢复中', 0, 2) };
      }
      return { status: 'ok', task: runningTask('artwork', '恢复中', 1, 2) };
    };
    await startTrashRestoreAll('artwork', { ids: ['11', '22'] });
    await flush();
    assert.deepStrictEqual(sent, { kind: 'artwork', ids: ['11', '22'] });
    assert.ok(intervalFn);
    assert.strictEqual(els['#trash-restore-all'].disabled, true);
    assert.ok(lastPaint('trash-restore-count').text.includes('已处理 1／2 张'));
  });

  // 7. 「全部恢复」先锁清单再确认: 请求体带确认批次
  test('确认后提交的是点击时固定的批次', async () => {
    reset();
    _trashListCount = 2;
    let sent = null;
    apiHandler = async (url, opts) => {
      if (String(url).includes('/trash/list')) {
        return { status: 'ok', items: [{ artwork_id: '11' }, { artwork_id: '22' }] };
      }
      if (opts && opts.method === 'POST') {
        sent = JSON.parse(opts.body);
        return { status: 'ok', task: runningTask('artwork', '恢复中', 0, 2) };
      }
      return { status: 'ok', task: runningTask('artwork', '恢复中', 1, 2) };
    };
    await window.trashRestoreAll();
    await flush();
    assert.ok(confirmLog[0].includes('2 个作品'), confirmLog[0]);
    assert.deepStrictEqual(sent, { kind: 'artwork', ids: ['11', '22'] });
  });

  // 8. 清单请求未完成时切换视图: 确认与提交仍用点击时视图
  test('清单未返回时切换视图不改变确认对象', async () => {
    reset();
    _trashListCount = 2;
    trashKind = 'artwork';
    let resolveList;
    let sent = null;
    apiHandler = async (url, opts) => {
      if (String(url).includes('/trash/list')) {
        return new Promise(r => { resolveList = r; });
      }
      if (opts && opts.method === 'POST') {
        sent = JSON.parse(opts.body);
        return { status: 'ok', task: runningTask('artwork', '恢复中', 0, 2) };
      }
      return { status: 'ok', task: runningTask('artwork', '恢复中', 1, 2) };
    };
    const p = window.trashRestoreAll();
    trashKind = 'page';               // 等待期间切到图片视图
    resolveList({ status: 'ok', items: [{ artwork_id: '11' }, { artwork_id: '22' }] });
    await p; await flush();
    assert.ok(confirmLog[0].includes('2 个作品'), confirmLog[0]);
    assert.deepStrictEqual(sent, { kind: 'artwork', ids: ['11', '22'] });
  });

  // 9. 取消确认不创建任务
  test('取消确认不创建恢复任务', async () => {
    reset();
    _trashListCount = 1;
    confirmResult = false;
    apiHandler = async (url) => {
      if (String(url).includes('/trash/list')) {
        return { status: 'ok', items: [{ artwork_id: '11' }] };
      }
      return { status: 'error', error: '不应提交' };
    };
    await window.trashRestoreAll();
    await flush();
    assert.ok(confirmLog[0].includes('1 个作品'), confirmLog[0]);
    assert.ok(!apiCalls.some(c => c.opts && c.opts.method === 'POST'), '取消后不应 POST');
    assert.ok(els['#trash-restore-task'].classList.contains('hidden'));
  });

  // 10. 运行中同一批重复点击不重复提交
  test('运行中重复点击不重复提交', async () => {
    reset();
    _trashListCount = 2;
    _trashRestoreRunning = true;
    let posts = 0;
    apiHandler = async (url, opts) => {
      if (opts && opts.method === 'POST') posts += 1;
      return { status: 'ok', task: runningTask('artwork', '恢复中', 0, 2) };
    };
    await startTrashRestoreAll('artwork', { ids: ['11', '22'] });
    assert.strictEqual(posts, 0);
  });

  // 11. 登记失败与回退失败在结果中交代原因
  test('登记失败与回退失败显示原因', () => {
    reset();
    _trashListCount = 2;
    paintTrashRestoreTask(doneTask('artwork', {
      kind: 'artwork', total: 2, restored: 1, skipped: [], move_failed: [],
      register_failed: [{ item: { artwork_id: '444' },
                          error: '数据库写入失败',
                          rollback_errors: ['回退位置已有文件, 未回退: x（实际位置: y）'] }],
      files_moved: 1,
    }));
    const html = els['#trash-restore-note'].innerHTML;
    assert.ok(html.includes('登记失败 1'), html);
    assert.ok(html.includes('数据库写入失败'), html);
    assert.ok(html.includes('回退失败'), html);
    assert.ok(html.includes('实际位置'), html);
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
  .replace('/*__TRASH_SLICE__*/', () => blockSrc);
new Function('assert', inner)(assert);
