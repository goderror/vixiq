'use strict';

// 壁纸库「从图库生成壁纸」共用进度: 直接运行 app.js 内的真实函数。
// 覆盖: 点击即时受理/防重复、流动切计数、100% 后登记阶段、结果分类、空候选、
// 启动失败、轮询短暂失败保留进度、切页/刷新接回同一任务、快速完成只留结果。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// ===== 🖼 壁纸库  从图库按规则生成壁纸 =====';
const END = 'async function clearWallDir() {';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少壁纸库生成进度代码块');
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
  ['#wall-lib-run', '#wall-lib-state', '#wall-lib-task', '#wall-lib-progress']
    .forEach(s => { els[s] = makeEl(s); });
  const barEls = { 'wall-lib-flow': makeEl('wall-lib-flow'),
                   'wall-lib-count': makeEl('wall-lib-count') };
  els['#wall-lib-progress'].querySelectorAll = sel =>
    (sel === '.op-progress' ? Object.values(barEls) : []);
  function $(sel) { return els[sel] || null; }

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

  let toastLog = [];
  function toast(msg, type) { toastLog.push({ msg, type }); }

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

  /*__WALL_SLICE__*/

  const flush = () => new Promise(r => realSetTimeout(r, 0));
  function lastPaint(wid) {
    for (let i = progressCalls.length - 1; i >= 0; i -= 1) {
      if (progressCalls[i].wid === wid) return progressCalls[i].o;
    }
    return null;
  }
  function runningTask(stage, done, total) {
    return { status: 'running', stage, done, total, current: '',
             error: '', result: null };
  }
  function doneTask(result, done, total) {
    return { status: 'done', stage: '', done: done || 0, total: total || 0,
             current: '', error: '', result };
  }
  function reset() {
    progressCalls = [];
    revealCalls = [];
    apiCalls = [];
    toastLog = [];
    timers = [];
    wallLibTimer = null;
    _wallLibGen = 0;
    _wallLibInFlight = false;
    _wallLibStarting = false;
    _wallLibLast = null;
    Object.values(els).forEach(el => {
      el.innerHTML = '';
      el.textContent = '';
      el.dataset = {};
      el.disabled = false;
    });
    Object.values(barEls).forEach(el => {
      el.className = '';
      el.dataset = {};
    });
    if (els['#wall-lib-progress']) els['#wall-lib-progress'].dataset = {};
    apiHandler = async () => ({ status: 'idle', result: null });
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 点击立即受理 + 防重复
  test('点击立即禁用并显示正在生成, 重复点击只提交一次', async () => {
    reset();
    let posts = 0, resolvePost;
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        posts += 1;
        return new Promise(r => { resolvePost = r; });
      }
      return { status: 'running', stage: '检查图片', done: 1, total: 3,
               current: '', error: '', result: null };
    };
    const p = runWallLib();
    assert.strictEqual(posts, 1);
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
    assert.ok(els['#wall-lib-state'].textContent.includes('正在生成'));
    assert.ok(!els['#wall-lib-task'].classList.contains('hidden'));
    const flow = lastPaint('wall-lib-flow');
    assert.ok(flow && flow.hidden === false && flow.text.includes('正在读取图库候选'));
    assert.ok(revealCalls.length >= 1, '应走 300ms 出现阈值');
    await runWallLib();                 // 启动请求未返回时重复点击
    assert.strictEqual(posts, 1);
    resolvePost({ ok: true, dir: 'x', task: runningTask('读取图库候选', 0, 0) });
    await p; await flush();
    assert.ok(timerCount() >= 1, '运行中任务应安排下一次查询');
  });

  // 2. 流动 → 真实计数
  test('拿到总数后切换为已检查 N／M 张', () => {
    reset();
    paintWallLibTask(runningTask('检查图片', 2, 6));
    const flow = lastPaint('wall-lib-flow');
    const count = lastPaint('wall-lib-count');
    assert.strictEqual(flow.hidden, true);
    assert.ok(count && count.hidden === false);
    assert.ok(count.text.includes('已检查 2／6 张'), count.text);
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
  });

  // 3. 100% 后仍在登记
  test('检查完最后一张仍显示正在登记壁纸', () => {
    reset();
    paintWallLibTask(runningTask('登记壁纸', 6, 6));
    assert.ok(els['#wall-lib-state'].textContent.includes('正在登记壁纸'));
    const count = lastPaint('wall-lib-count');
    assert.ok(count && count.hidden === false && count.text.includes('已检查 6／6 张'));
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
  });

  // 4. 完整结果与部分失败原因
  test('结果显示完整失败数, 明细仍只取第一条', () => {
    reset();
    const t = doneTask({
      scanned: 6, passed: 3, added: 2, skipped_exist: 1, skipped_size: 1,
      skipped_ratio: 1, skipped_ext: 1, missing: 1, failed: 23,
      errors: Array(20).fill('700002_p0.png: PermissionError: 拒绝访问'),
      elapsed: 1.2,
    }, 6, 6);
    paintWallLibTask(t);
    wallLibPaint(t);
    const s = els['#wall-lib-state'].textContent;
    for (const kw of ['已检查 6 张', '已生成 2 张', '已存在 1 张', '尺寸不合 1',
                      '比例不合 1', '格式不支持 1', '文件已不在 1 张',
                      '失败 23 项', '拒绝访问', '用时 1.2s']) {
      assert.ok(s.includes(kw), s);
    }
    assert.ok(!s.includes('失败 20 项'), '失败数不能取明细条数: ' + s);
    assert.ok(els['#wall-lib-task'].classList.contains('hidden'));
    assert.strictEqual(els['#wall-lib-run'].disabled, false);
  });

  // 5. 空候选 / 全部筛掉
  test('空候选与没有符合条件的图片给出清楚反馈', () => {
    reset();
    assert.ok(wallLibResultText({ scanned: 0, added: 0, skipped_exist: 0 })
      .includes('没有可参与生成'));
    const s = wallLibResultText({ scanned: 3, passed: 0, added: 0,
      skipped_exist: 0, skipped_size: 2, skipped_ratio: 1, errors: [] });
    assert.ok(s.includes('已检查 3 张') && s.includes('没有符合条件的图片')
              && s.includes('尺寸不合 2') && s.includes('比例不合 1'), s);
  });

  // 6. 启动失败 / 任务失败都恢复按钮
  test('启动失败与任务失败显示原因并恢复按钮', async () => {
    reset();
    apiHandler = async (url, opts) => {
      if (opts && opts.method === 'POST') {
        return { ok: false, error: '还没指定壁纸夹' };
      }
      return { status: 'idle', result: null };
    };
    await runWallLib();
    await flush();
    assert.ok(els['#wall-lib-state'].textContent.includes('生成失败'));
    assert.ok(els['#wall-lib-state'].textContent.includes('还没指定壁纸夹'));
    assert.ok(els['#wall-lib-task'].classList.contains('hidden'));
    assert.strictEqual(els['#wall-lib-run'].disabled, false);
    assert.ok(toastLog.some(t => t.type === 'err'));

    reset();
    paintWallLibTask({ status: 'error', stage: '', done: 0, total: 0,
                       current: '', error: 'PermissionError: 壁纸夹只读',
                       result: null });
    wallLibPaint({ status: 'error', stage: '', error: 'PermissionError: 壁纸夹只读',
                   result: null });
    assert.ok(els['#wall-lib-state'].textContent.includes('壁纸夹只读'));
    assert.ok(els['#wall-lib-task'].classList.contains('hidden'));
    assert.strictEqual(els['#wall-lib-run'].disabled, false);
  });

  // 7. 轮询短暂失败: 保留最后进度并继续
  test('轮询短暂失败保留最后进度并继续尝试', async () => {
    reset();
    paintWallLibTask(runningTask('检查图片', 2, 6));
    let calls = 0;
    apiHandler = async () => {
      calls += 1;
      if (calls === 1) throw new Error('网络抖动');
      if (calls === 2) return runningTask('检查图片', 3, 6);
      return doneTask({ scanned: 6, passed: 3, added: 2, skipped_exist: 1,
                        skipped_size: 1, skipped_ratio: 1, skipped_ext: 1,
                        missing: 0, errors: [], elapsed: 0.5 }, 6, 6);
    };
    startWallLibPolling();
    await flush();
    const count = lastPaint('wall-lib-count');
    assert.ok(count && count.text.includes('已检查 2／6 张'), count && count.text);
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
    assert.strictEqual(timerCount(), 1, '失败后要安排下一次查询');
    await runNextTimer(); await flush();
    const count2 = lastPaint('wall-lib-count');
    assert.ok(count2 && count2.text.includes('已检查 3／6 张'), count2 && count2.text);
    assert.strictEqual(timerCount(), 1);
    await runNextTimer(); await flush();
    assert.ok(els['#wall-lib-state'].textContent.includes('已生成 2 张'));
    assert.ok(els['#wall-lib-task'].classList.contains('hidden'));
    assert.strictEqual(timerCount(), 0, '终态后不再轮询');
    assert.strictEqual(els['#wall-lib-run'].disabled, false);
  });

  // 8. 切页返回/刷新接回同一任务, 不重复查询
  test('重进设置页接回运行中任务且只有一条查询链', async () => {
    reset();
    apiHandler = async () => runningTask('检查图片', 1, 3);
    await loadWallLib();
    await flush();
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
    const getCalls = () => apiCalls.filter(c =>
      String(c.url).includes('/api/wallpaper/lib')
      && !(c.opts && c.opts.method)).length;
    const n1 = getCalls();
    assert.ok(n1 >= 1, '接回任务应发一次主查询');
    assert.strictEqual(timerCount(), 1, '接回后应只有一条轮询链');
    await loadWallLib();
    await flush();
    assert.strictEqual(getCalls(), n1, '重复进入不应另发查询');
    assert.strictEqual(timerCount(), 1);
  });

  // 9. 同一条任务链不并发查询
  test('上一请求未结束时不发下一次状态查询', async () => {
    reset();
    let resolveFirst;
    let gets = 0;
    apiHandler = () => {
      gets += 1;
      if (gets === 1) return new Promise(r => { resolveFirst = r; });
      return runningTask('检查图片', 2, 6);
    };
    paintWallLibTask(runningTask('检查图片', 1, 6));
    startWallLibPolling();
    await flush();
    assert.strictEqual(gets, 1);
    assert.strictEqual(timerCount(), 0, '请求在途时不应安排下一次');
    resolveFirst(runningTask('检查图片', 2, 6));
    await flush();
    assert.strictEqual(gets, 1, '上一请求结束前不应发第二次');
    assert.strictEqual(timerCount(), 1);
    await runNextTimer(); await flush();
    assert.strictEqual(gets, 2, '上一次结束后才发下一次');
  });

  // 10. 终态后迟到的旧 running 不覆盖结果
  test('终态后迟到的旧 running 不覆盖结果和按钮', async () => {
    reset();
    let resolveOld;
    apiHandler = () => new Promise(r => { resolveOld = r; });
    const p = loadWallLib();
    await flush();
    // 该链已被停止/终态接管; 旧请求此时才返回 running
    stopWallLibPolling();
    const done = doneTask({ scanned: 2, passed: 1, added: 1, skipped_exist: 1,
                            skipped_size: 0, skipped_ratio: 0, skipped_ext: 0,
                            missing: 0, errors: [], elapsed: 0.1 }, 2, 2);
    paintWallLibTask(done);
    wallLibPaint(done);
    const before = els['#wall-lib-state'].textContent;
    resolveOld(runningTask('检查图片', 1, 2));
    await p; await flush();
    assert.strictEqual(els['#wall-lib-state'].textContent, before);
    assert.ok(before.includes('已生成 1 张'));
    assert.strictEqual(els['#wall-lib-run'].disabled, false);
    assert.strictEqual(timerCount(), 0, '迟到回包不应重新启动轮询');
    assert.ok(els['#wall-lib-task'].classList.contains('hidden'));
  });

  // 11. 新任务启动后旧任务的终态回包不覆盖新进度
  test('新任务启动后旧任务终态返回不覆盖', async () => {
    reset();
    let oldResolve, postResolve;
    let gets = 0;
    apiHandler = (url, opts) => {
      if (opts && opts.method === 'POST') {
        return new Promise(r => { postResolve = r; });
      }
      gets += 1;
      if (gets === 1) return new Promise(r => { oldResolve = r; });
      return new Promise(() => {});   // 新任务的查询先挂起
    };
    const oldP = loadWallLib();
    await flush();
    const newP = runWallLib();
    await flush();
    postResolve({ ok: true, dir: 'x', task: runningTask('检查图片', 0, 3) });
    await newP; await flush();
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
    assert.ok(els['#wall-lib-state'].textContent.includes('正在生成'));
    // 旧任务此时才返回 done
    oldResolve(doneTask({ scanned: 2, passed: 1, added: 1, skipped_exist: 1,
                          skipped_size: 0, skipped_ratio: 0, skipped_ext: 0,
                          missing: 0, errors: [], elapsed: 0.1 }, 2, 2));
    await oldP; await flush();
    assert.ok(els['#wall-lib-state'].textContent.includes('正在生成'));
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
    assert.ok(!els['#wall-lib-task'].classList.contains('hidden'));
    assert.strictEqual(_wallLibInFlight, true, '旧回包不能清掉新任务的在途标记');
    assert.strictEqual(timerCount(), 0);
  });

  // 9. 快速完成只留结果
  test('快速完成不显示进度条只保留结果', async () => {
    reset();
    apiHandler = async (url, opts) => {
      if (opts && opts.method === 'POST') {
        return { ok: true, dir: 'x', task: doneTask({
          scanned: 2, passed: 1, added: 1, skipped_exist: 1, skipped_size: 0,
          skipped_ratio: 0, skipped_ext: 0, missing: 0, errors: [], elapsed: 0.1,
        }, 2, 2) };
      }
      return doneTask({
        scanned: 2, passed: 1, added: 1, skipped_exist: 1, skipped_size: 0,
        skipped_ratio: 0, skipped_ext: 0, missing: 0, errors: [], elapsed: 0.1,
      }, 2, 2);
    };
    await runWallLib();
    await flush();
    assert.ok(els['#wall-lib-task'].classList.contains('hidden'));
    assert.ok(els['#wall-lib-state'].textContent.includes('已生成 1 张'));
    assert.strictEqual(els['#wall-lib-run'].disabled, false);
  });

  // 10. 已有任务的重开响应直接接回
  test('启动响应带回已有任务时接回原任务', async () => {
    reset();
    let posts = 0;
    apiHandler = async (url, opts) => {
      if (opts && opts.method === 'POST') {
        posts += 1;
        return { ok: true, already: true, dir: 'x',
                 task: runningTask('检查图片', 1, 2) };
      }
      return runningTask('检查图片', 1, 2);
    };
    await runWallLib();
    await flush();
    assert.strictEqual(posts, 1);
    assert.strictEqual(els['#wall-lib-run'].disabled, true);
    const count = lastPaint('wall-lib-count');
    assert.ok(count && count.text.includes('已检查 1／2 张'));
    assert.ok(!toastLog.some(t => t.msg.includes('开始扫描')),
              '已有任务不应再提示开始扫描');
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
  .replace('/*__WALL_SLICE__*/', () => blockSrc);
new Function('assert', inner)(assert);
