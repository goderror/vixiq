'use strict';

// 反搜收件箱「添加图片」共用进度: 运行 app.js 内真实 uploadReverseImages。
// 覆盖: 未知总量流动、真实字节计数、上传完切写入、快回结果、
// 格式/写入失败原因、网络中断/HTTP 错误/响应异常、重复操作、
// 迟到事件不改新批次、切页 repaint。

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
  function el(id) {
    const set = new Set(['hidden']);
    const obj = {
      id, dataset: {}, value: '', disabled: false,
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
    let html = '', text = '';
    Object.defineProperty(obj, 'innerHTML', {
      get: () => html,
      set: v => { html = String(v); text = html.replace(/<[^>]*>/g, ''); },
    });
    Object.defineProperty(obj, 'textContent', {
      get: () => text,
      set: v => { text = String(v); html = String(v); },
    });
    return obj;
  }
  const els = {};
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

  let paints = [];
  function opProgressHtml(wid, o) {
    return `<div class="op-progress" id="${wid}">${esc((o && o.text) || '')}</div>`;
  }
  function opProgressPaint(wid, o) {
    paints.push({ wid, o: Object.assign({}, o) });
  }
  let revealCount = 0;
  function opRevealProgressIn() { revealCount += 1; }

  let toasts = [];
  function toast(text, kind) { toasts.push([String(text), kind]); }
  let refreshCount = 0;
  async function loadReverseStatus() { refreshCount += 1; return {}; }

  let xhrs = [];
  class FakeXHR {
    constructor() {
      this.upload = {};
      this.status = 0;
      this.responseText = '';
      this.timeout = 0;
      this.aborted = false;
      xhrs.push(this);
    }
    open(method, url) { this.method = method; this.url = url; }
    send(fd) { this.fd = fd; this.sent = true; }
    abort() { this.aborted = true; }
  }
  class FakeFormData {
    constructor() { this.items = []; }
    append(k, v) { this.items.push([k, v]); }
  }
  const XMLHttpRequest = FakeXHR;
  const FormData = FakeFormData;
  const window = {};

  /*__UPLOAD_SLICE__*/

  const flush = () => new Promise(r => globalThis.setTimeout(r, 0));
  function lastPaint(wid) {
    for (let i = paints.length - 1; i >= 0; i -= 1) {
      if (paints[i].wid === wid) return paints[i].o;
    }
    return null;
  }
  function note() { return els['#reverse-upload-note'].innerHTML; }
  function noteText() { return note().replace(/<[^>]*>/g, ''); }
  function button() { return els['#reverse-upload']; }
  function input(names, sizes) {
    return {
      files: (names || []).map((n, i) => ({
        name: n, size: sizes ? sizes[i] : 1000,
      })),
      value: 'x', disabled: false,
    };
  }
  function reset() {
    paints = [];
    toasts = [];
    revealCount = 0;
    refreshCount = 0;
    xhrs = [];
    _revUploadSeq = 0;
    _revUploadXHR = null;
    _revUploadState = null;
    els['#reverse-upload'].textContent = '➕ 添加图片';
    els['#reverse-upload'].disabled = false;
    els['#reverse-file-input'].disabled = false;
    els['#reverse-file-input'].value = '';
    els['#reverse-upload-task'].classList.add('hidden');
    els['#reverse-upload-progress'].innerHTML = '';
    els['#reverse-upload-progress'].dataset = {};
    els['#reverse-upload-note'].innerHTML = '';
  }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  test('没有文件时不出进度也不发请求', async () => {
    reset();
    await uploadReverseImages({ files: [], value: '' });
    assert.strictEqual(xhrs.length, 0);
    assert.strictEqual(button().textContent, '➕ 添加图片');
    assert.ok(els['#reverse-upload-task'].classList.contains('hidden'));
  });

  test('选图即时受理并显示未知总量流动', async () => {
    reset();
    const inp = input(['a.png', 'b.png']);
    await uploadReverseImages(inp);
    assert.strictEqual(xhrs.length, 1);
    assert.strictEqual(button().textContent, '正在添加…');
    assert.strictEqual(button().disabled, true);
    assert.strictEqual(els['#reverse-upload-task'].classList.contains('hidden'), false);
    assert.ok(lastPaint('rev-up-flow').text.includes('正在上传图片'));
    assert.strictEqual(lastPaint('rev-up-count').hidden, true);
    assert.strictEqual(xhrs[0].fd.items.length, 2);
  });

  test('有真实总字节时显示已上传字节计数', async () => {
    reset();
    await uploadReverseImages(input(['big.png'], [1000]));
    const xhr = xhrs[0];
    xhr.upload.onprogress({ lengthComputable: true, loaded: 400, total: 1000 });
    const count = lastPaint('rev-up-count');
    assert.strictEqual(count.hidden, false);
    assert.strictEqual(count.done, 400);
    assert.strictEqual(count.total, 1000);
    assert.ok(count.text.includes('400 B'), count.text);
    assert.ok(count.text.includes('1000 B'), count.text);
    assert.strictEqual(lastPaint('rev-up-flow').hidden, true);
  });

  test('总量未知时保持流动', async () => {
    reset();
    await uploadReverseImages(input(['big.png'], [1000]));
    xhrs[0].upload.onprogress({ lengthComputable: false, loaded: 400, total: 1000 });
    assert.ok(lastPaint('rev-up-flow').text.includes('正在上传图片'));
    assert.strictEqual(lastPaint('rev-up-count').hidden, true);
  });

  test('字节发完切到正在写入收件箱', async () => {
    reset();
    await uploadReverseImages(input(['a.png']));
    xhrs[0].upload.onload();
    assert.ok(lastPaint('rev-up-flow').text.includes('正在写入收件箱'));
    assert.strictEqual(lastPaint('rev-up-count').hidden, true);
    assert.strictEqual(button().disabled, true);
  });

  test('成功结果报告已添加与未添加原因', async () => {
    reset();
    await uploadReverseImages(input(['a.png', 'b.png']));
    const xhr = xhrs[0];
    xhr.upload.onload();
    xhr.status = 200;
    xhr.responseText = JSON.stringify({
      status: 'ok', saved: ['a.png', 'b.png'],
      rejected: ['bad.txt', 'fail.png'],
      rejected_details: [
        { name: 'bad.txt', reason: '格式不支持' },
        { name: 'fail.png', reason: '写入失败：磁盘满' },
      ],
    });
    xhr.onload();
    await flush();
    const text = noteText();
    assert.ok(text.includes('已添加 2 张'), text);
    assert.ok(text.includes('未添加 2 项'), text);
    assert.ok(text.includes('格式不支持'), text);
    assert.ok(text.includes('写入失败：磁盘满'), text);
    assert.strictEqual(button().textContent, '➕ 添加图片');
    assert.strictEqual(button().disabled, false);
    assert.strictEqual(xhrs[0].status, 200);
    assert.strictEqual(lastPaint('rev-up-flow').hidden, true);
    assert.strictEqual(lastPaint('rev-up-count').hidden, true);
    assert.strictEqual(refreshCount, 1);
    assert.ok(toasts.some(t => t[0].includes('未添加 2 项') && t[1] === 'warn'), toasts);
  });

  test('快回直接出结果，进度条隐藏', async () => {
    reset();
    await uploadReverseImages(input(['a.png']));
    const xhr = xhrs[0];
    xhr.status = 200;
    xhr.responseText = JSON.stringify({ status: 'ok', saved: ['a.png'],
                                        rejected: [], rejected_details: [] });
    xhr.onload();
    await flush();
    assert.ok(noteText().includes('已添加 1 张'), noteText());
    assert.strictEqual(lastPaint('rev-up-flow').hidden, true);
    assert.strictEqual(lastPaint('rev-up-count').hidden, true);
    assert.strictEqual(button().disabled, false);
    assert.strictEqual(refreshCount, 1);
  });

  test('网络中断恢复按钮并提示先看收件箱', async () => {
    reset();
    await uploadReverseImages(input(['a.png']));
    xhrs[0].onerror();
    await flush();
    const text = noteText();
    assert.ok(text.includes('网络中断'), text);
    assert.ok(text.includes('待反搜区'), text);
    assert.strictEqual(button().disabled, false);
    assert.strictEqual(refreshCount, 1);
  });

  test('HTTP 错误显示接口信息', async () => {
    reset();
    await uploadReverseImages(input(['a.png']));
    const xhr = xhrs[0];
    xhr.status = 500;
    xhr.responseText = JSON.stringify({ error: '磁盘不可写' });
    xhr.onload();
    await flush();
    assert.ok(noteText().includes('磁盘不可写'), noteText());
    assert.strictEqual(button().disabled, false);
  });

  test('响应异常不改写已有文件', async () => {
    reset();
    await uploadReverseImages(input(['a.png']));
    const xhr = xhrs[0];
    xhr.status = 200;
    xhr.responseText = 'not-json';
    xhr.onload();
    await flush();
    assert.ok(noteText().includes('接口响应异常'), noteText());
    assert.ok(noteText().includes('待反搜区'), noteText());
    assert.strictEqual(button().disabled, false);
  });

  test('上传中重复操作只保留第一次请求', async () => {
    reset();
    const first = input(['a.png']);
    await uploadReverseImages(first);
    const second = input(['b.png']);
    await uploadReverseImages(second);
    assert.strictEqual(xhrs.length, 1);
    assert.strictEqual(second.value, '');
    assert.strictEqual(button().disabled, true);
  });

  test('迟到的上传事件不能改新批次', async () => {
    reset();
    await uploadReverseImages(input(['old.png']));
    const oldXhr = xhrs[0];
    oldXhr.status = 200;
    oldXhr.responseText = JSON.stringify({ status: 'ok', saved: ['old.png'],
                                           rejected: [], rejected_details: [] });
    oldXhr.onload();
    await flush();
    await uploadReverseImages(input(['new.png']));
    const newXhr = xhrs[1];
    newXhr.upload.onprogress({ lengthComputable: true, loaded: 200, total: 2000 });
    const before = lastPaint('rev-up-count').text;
    oldXhr.upload.onprogress({ lengthComputable: true, loaded: 999, total: 1000 });
    oldXhr.onerror();
    assert.strictEqual(lastPaint('rev-up-count').text, before);
    assert.strictEqual(button().disabled, true);
    assert.ok(noteText() === '');
    newXhr.status = 200;
    newXhr.responseText = JSON.stringify({ status: 'ok', saved: ['new.png'],
                                           rejected: [], rejected_details: [] });
    newXhr.onload();
    await flush();
    assert.ok(noteText().includes('已添加 1 张'), noteText());
  });

  test('切页回来 repaint 同一上传状态', async () => {
    reset();
    await uploadReverseImages(input(['a.png']));
    xhrs[0].upload.onprogress({ lengthComputable: true, loaded: 300, total: 900 });
    _revUploadState = { phase: 'uploading', loaded: 300, total: 900 };
    revUploadRepaint();
    assert.strictEqual(els['#reverse-upload-task'].classList.contains('hidden'), false);
    assert.ok(lastPaint('rev-up-count').text.includes('300 B'), lastPaint('rev-up-count').text);
    assert.strictEqual(button().disabled, true);
    _revUploadState = { phase: 'done', html: '<div>已添加 1 张</div>' };
    _revUploadXHR = null;
    revUploadRepaint();
    assert.ok(noteText().includes('已添加 1 张'), noteText());
    assert.strictEqual(button().disabled, false);
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
