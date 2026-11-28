'use strict';

// 反搜收件箱「添加图片」: 共用入口上传接法的业务适配。
// 共用引擎本身由 test_operation_progress_entry.cjs 覆盖; 这里验
// 真实上传字节单位、写入阶段文案、结果/失败分行、按钮与输入复位、
// 完成后刷新收件箱、重复选择忽略、切页 repaint 接回。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// 反搜收件箱「添加图片」上传进度';
const END = 'async function startReverse(';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少反搜添加图片进度代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  const els = {};
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
    let html = '';
    Object.defineProperty(obj, 'innerHTML', {
      get: () => html,
      set: v => { html = String(v); },
    });
    return obj;
  }
  ['#reverse-upload', '#reverse-file-input', '#reverse-upload-task',
   '#reverse-upload-progress', '#reverse-upload-note']
    .forEach(s => { els[s] = el(s); });
  function $(sel) { return els[sel] || null; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function fmtSize(bytes) {
    if (bytes == null || isNaN(bytes) || bytes < 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(2) + ' MB';
    return (bytes / 1073741824).toFixed(2) + ' GB';
  }
  let toasts = [];
  function toast(text, kind) { toasts.push([String(text), kind]); }
  let refreshCount = 0;
  async function loadReverseStatus() { refreshCount += 1; return {}; }
  let runCalls = [];
  function runWithProgress(o) { runCalls.push(o); return { key: o.key }; }
  let repaints = [];
  function repaintOp(key) { repaints.push(key); return true; }
  class FakeFormData {
    constructor() { this.items = []; }
    append(k, v) { this.items.push([k, v]); }
  }
  const FormData = FakeFormData;
  const window = {};

  /*__UPLOAD_SLICE__*/

  function note() { return els['#reverse-upload-note'].innerHTML; }
  function noteText() { return note().replace(/<[^>]*>/g, ''); }
  function reset() {
    toasts = [];
    refreshCount = 0;
    runCalls = [];
    repaints = [];
    els['#reverse-upload'].disabled = false;
    els['#reverse-upload'].textContent = '➕ 添加图片';
    els['#reverse-file-input'].disabled = false;
    els['#reverse-file-input'].value = '';
    els['#reverse-upload-note'].innerHTML = '';
  }
  function input(names) {
    return {
      files: (names || []).map(n => ({ name: n })),
      value: '',
      disabled: false,
    };
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  test('没有文件时不提交也不发请求', async () => {
    reset();
    await uploadReverseImages({ files: [], value: '' });
    assert.strictEqual(runCalls.length, 0);
  });

  test('上传中重复选择被忽略并清空输入', async () => {
    reset();
    els['#reverse-upload'].disabled = true;
    const inp = input(['b.png']);
    inp.value = 'x';
    await uploadReverseImages(inp);
    assert.strictEqual(runCalls.length, 0);
    assert.strictEqual(inp.value, '');
  });

  test('正常选择交给共用入口 upload 模式', async () => {
    reset();
    const inp = input(['a.png', 'b.png']);
    await uploadReverseImages(inp);
    assert.strictEqual(runCalls.length, 1);
    const o = runCalls[0];
    assert.strictEqual(o.key, 'reverse-upload');
    assert.strictEqual(o.mode, 'upload');
    assert.strictEqual(o.upload.url, '/api/reverse/upload');
    assert.strictEqual(o.upload.formData.items.length, 2);
    assert.strictEqual(o.flowId, 'rev-up-flow');
    assert.strictEqual(o.countId, 'rev-up-count');
    assert.strictEqual(o.area, '#reverse-upload-task');
    assert.strictEqual(o.button, '#reverse-upload');
    assert.deepStrictEqual(o.inputs, ['#reverse-file-input']);
    assert.ok(o.countText(400, 1000).includes('400 B'), o.countText(400, 1000));
    assert.ok(o.countText(400, 1000).includes('1000 B'));
  });

  test('成功结果分行报告已添加/未添加原因并刷新', async () => {
    reset();
    const inp = input(['a.png']);
    inp.value = 'x';
    await uploadReverseImages(inp);
    const o = runCalls[0];
    const data = {
      status: 'ok', saved: ['a.png', 'b.png'],
      rejected: ['bad.txt', 'fail.png'],
      rejected_details: [
        { name: 'bad.txt', reason: '格式不支持' },
        { name: 'fail.png', reason: '写入失败：磁盘满' },
      ],
    };
    const html = o.resultHtml(data);
    assert.ok(html.includes('已添加 2 张'), html);
    assert.ok(html.includes('未添加 2 项'), html);
    assert.ok(html.includes('格式不支持'), html);
    assert.ok(html.includes('写入失败：磁盘满'), html);
    o.onDone(data);
    await Promise.resolve();
    assert.strictEqual(inp.value, '');
    assert.strictEqual(refreshCount, 1);
    assert.ok(toasts.some(t => t[0].includes('未添加 2 项') && t[1] === 'warn'), toasts);
  });

  test('失败结果保留待反搜区提示并刷新', async () => {
    reset();
    const inp = input(['a.png']);
    inp.value = 'x';
    await uploadReverseImages(inp);
    const o = runCalls[0];
    o.onError(new Error('网络中断'));
    await Promise.resolve();
    const html = o.errorHtml('网络中断');
    assert.ok(html.includes('添加失败'), html);
    assert.ok(html.includes('待反搜区'), html);
    assert.strictEqual(inp.value, '');
    assert.strictEqual(refreshCount, 1);
  });

  test('结果明细最多列 5 条并提示剩余数量', () => {
    const details = [];
    for (let i = 0; i < 7; i += 1) {
      details.push({ name: `f${i}.txt`, reason: `原因${i}` });
    }
    const html = revUploadResultHtml({
      saved: [], rejected: details.map(x => x.name), rejected_details: details,
    });
    assert.ok(html.includes('未添加 7 项'), html);
    assert.ok(html.includes('等 7 项'), html);
    assert.ok(html.includes('f4.txt'), html);
    assert.ok(!html.includes('f5.txt'), '超过 5 条不应逐条展示');
  });

  test('切页 repaint 接回同一上传状态', () => {
    reset();
    revUploadRepaint();
    assert.deepStrictEqual(repaints, ['reverse-upload']);
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
  .replace('/*__UPLOAD_SLICE__*/', () => blockSrc) + ')();\n';
new Function('assert', generated)(assert);
