'use strict';

// 下载队列动图合成阶段: updateItemProgress 复用共用流动样式。
// 覆盖: 「正在合成动图」出现流动提示; 完成/失败后清除流动并显示最终态;
// 普通计数下载分支保持不变。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = 'function itemNoteHtml(';
const END = 'window.removeCartItem';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少 updateItemProgress 代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  const els = {};
  function el(id) {
    let html = '', text = '';
    const obj = {
      id, className: '', dataset: {},
      classList: { add() {}, remove() {} },
      querySelector: sel => (sel === '.tag-state' ? (obj._tag || null) : null),
    };
    Object.defineProperty(obj, 'innerHTML', {
      get: () => html,
      set: v => { html = String(v); text = html.replace(/<[^>]*>/g, ''); },
    });
    Object.defineProperty(obj, 'textContent', {
      get: () => text,
      set: v => { text = String(v); html = ''; },
    });
    return obj;
  }
  function $(sel) { return els[sel] || null; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function reset() {
    Object.keys(els).forEach(k => delete els[k]);
    els['#cart-prog-1'] = el('cart-prog-1');
    els['#cart-row-1'] = el('cart-row-1');
    els['#cart-row-1']._tag = el('tag');
  }

  /*__CART_SLICE__*/

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  test('合成阶段显示共用流动条与阶段文字', () => {
    reset();
    updateItemProgress(1, 0, 1, '正在合成动图');
    const cell = els['#cart-prog-1'];
    assert.ok(cell.innerHTML.includes('cart-flow'), cell.innerHTML);
    assert.ok(cell.innerHTML.includes('op-track flow'), cell.innerHTML);
    assert.ok(cell.textContent.includes('正在合成动图'), cell.textContent);
    assert.strictEqual(els['#cart-row-1']._tag.className, 'tag-state running');
  });

  test('完成后清除流动并显示完成', () => {
    reset();
    updateItemProgress(1, 0, 1, '正在合成动图');
    updateItemProgress(1, 1, 1, '完成');
    const cell = els['#cart-prog-1'];
    assert.strictEqual(cell.textContent, '✓ 完成');
    assert.ok(!cell.innerHTML.includes('cart-flow'), cell.innerHTML);
    assert.strictEqual(els['#cart-row-1']._tag.textContent, '✓ 完成');
  });

  test('失败后清除流动并显示失败', () => {
    reset();
    updateItemProgress(1, 0, 1, '正在合成动图');
    updateItemProgress(1, 1, 1, '失败');
    const cell = els['#cart-prog-1'];
    assert.strictEqual(cell.textContent, '✖ 失败');
    assert.ok(!cell.innerHTML.includes('cart-flow'), cell.innerHTML);
    assert.strictEqual(els['#cart-row-1']._tag.className, 'tag-state failed');
  });

  test('普通计数下载分支不变', () => {
    reset();
    updateItemProgress(1, 2, 5, '下载中');
    const cell = els['#cart-prog-1'];
    assert.strictEqual(cell.textContent, '40% (2/5) 下载中');
    assert.ok(!cell.innerHTML.includes('cart-flow'));
  });

  test('最终失败详情可展开并保留备份路径', () => {
    reset();
    updateItemProgress(1, 1, 1,
      '失败：旧图尚未恢复；可恢复副本：D:\\iso\\x.png.bak');
    const cell = els['#cart-prog-1'];
    assert.ok(cell.innerHTML.includes('cart-note'), cell.innerHTML);
    assert.ok(cell.textContent.includes('旧图尚未恢复'), cell.textContent);
    assert.ok(cell.textContent.includes('.bak'), cell.textContent);
    assert.strictEqual(els['#cart-row-1']._tag.className, 'tag-state failed');
  });

  test('部分页失败仍显示完成并带详情', () => {
    reset();
    updateItemProgress(1, 1, 1, '完成：部分页失败；p1: 旧图尚未恢复');
    const cell = els['#cart-prog-1'];
    assert.ok(cell.innerHTML.includes('cart-note'), cell.innerHTML);
    assert.ok(cell.textContent.includes('部分页失败'), cell.textContent);
    assert.strictEqual(els['#cart-row-1']._tag.className, 'tag-state done');
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
  .replace('/*__CART_SLICE__*/', () => blockSrc) + ')();\n';
new Function('assert', generated)(assert);
