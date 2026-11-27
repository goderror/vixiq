'use strict';

// 单件作品编辑: 只提交实际改动。直接运行 app.js 内的真实
// resolveAuthorInput / libEdit* / collectLibEditChanges / saveLibEdit / doSaveLibEdit。
// 覆盖: 只改标题、无改动、改了又改回、标签顺序、标签增删/清空、
// 清空链接、类型/R18 归零、作者未变/换人/同名改名/留空不修改、
// 后端 unchanged 响应不标记"刚编辑"。

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '../web/static/app.js');
const src = fs.readFileSync(APP_JS, 'utf8');
const START = '// 作者输入解析: 「名字_ID」→ pixiv 作者';
const END = '// 当前作品挂在自定义作者下';
const a = src.indexOf(START);
const b = src.indexOf(END, a);
if (a < 0 || b < 0) throw new Error('缺少编辑作品差异提交代码块');
const blockSrc = src.slice(a, b);

function harnessBody() {
  function el(id, value) {
    return {
      id, value: value || '', dataset: {},
      childNodes: [{ textContent: value || '' }],
    };
  }
  const els = {
    'lib-e-title': el('lib-e-title', '原标题'),
    'lib-e-type': el('lib-e-type', '1'),
    'lib-e-r18': el('lib-e-r18', '1'),
    'lib-e-source': el('lib-e-source', 'pixiv'),
    'lib-e-url': el('lib-e-url', 'https://x/1'),
    'lib-e-author': el('lib-e-author', '作者甲'),
  };
  let tagEls = [];
  function $(sel) {
    if (sel && sel[0] === '#') return els[sel.slice(1)] || null;
    return null;
  }
  function $$(sel) {
    if (sel === '#lib-tag-editor .tag.big') return tagEls;
    return [];
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function setTags(list) {
    tagEls = (list || []).map(t => ({ childNodes: [{ textContent: t }] }));
  }

  let facetData = { authors: [], tags: [], sources: [] };
  let window = {};
  let toasts = [];
  function toast(text, kind) { toasts.push([text, kind]); }
  let apiCalls = [];
  let apiResponder = async () => ({ status: 'ok' });
  async function api(url, opts) {
    apiCalls.push({ url, opts: opts || {} });
    return apiResponder(url, opts);
  }
  function apiErr(r) {
    if (r && r.status === 'error') return r.error || r.message || '请求失败';
    return '';
  }
  let lastActions = [];
  function setLastAction(kind, ids, text) {
    lastActions.push({ kind, ids, text });
  }
  let closed = 0, srcClosed = 0, loads = 0, facetLoads = 0;
  function closeLibModal() { closed += 1; }
  function closeSrcModal() { srcClosed += 1; }
  function loadLibrary() { loads += 1; }
  function loadLibFacets() { facetLoads += 1; }
  let mergeAsks = [];
  function askAuthorMerge(aid, body, mergeFrom) {
    mergeAsks.push({ aid, body, mergeFrom });
  }
  let pixivAsks = [];
  function askIsPixivAuthor(aid, body, name) {
    pixivAsks.push({ aid, body, name });
  }
  function sameNameLocalAuthor() { return ''; }
  function openLibModal() {}
  function openSrcModal() {}
  function sourceOptions() { return '<option value="pixiv">pixiv</option>'; }
  function makeEditAuthorCombo() {}
  function makeEditTagSuggest() {}

  /*__EDIT_SLICE__*/

  const flush = () => new Promise(r => globalThis.setTimeout(r, 0));

  function resetForm(opt) {
    const o = opt || {};
    els['lib-e-title'].value = o.title == null ? '原标题' : o.title;
    els['lib-e-type'].value = String(o.illust_type == null ? 1 : o.illust_type);
    els['lib-e-r18'].value = String(o.is_r18 == null ? 1 : o.is_r18);
    els['lib-e-source'].value = o.source == null ? 'pixiv' : o.source;
    els['lib-e-url'].value = o.source_url == null ? 'https://x/1' : o.source_url;
    els['lib-e-author'].value = o.author_text == null ? '作者甲' : o.author_text;
    els['lib-e-author'].dataset = {};
    if (o.picked_id) els['lib-e-author'].dataset.authorId = o.picked_id;
    setTags(o.tags || ['标签A', '标签B']);
    window._libEditInit = {
      title: o.init_title == null ? '原标题' : o.init_title,
      illust_type: o.init_type == null ? 1 : o.init_type,
      is_r18: o.init_r18 == null ? 1 : o.init_r18,
      source: o.init_source == null ? 'pixiv' : o.init_source,
      source_url: o.init_url == null ? 'https://x/1' : o.init_url,
      tags: (o.init_tags || ['标签A', '标签B']).slice(),
      author_id: o.init_author_id == null ? '123' : o.init_author_id,
      author_name: o.init_author_name == null ? '作者甲' : o.init_author_name,
    };
    window._libEditW = {
      artwork_id: '990001',
      author_id: o.init_author_id == null ? '123' : o.init_author_id,
      author_name: o.init_author_name == null ? '作者甲' : o.init_author_name,
    };
    facetData = { authors: o.authors || [], tags: [], sources: [] };
  }

  function reset() {
    toasts = [];
    apiCalls = [];
    lastActions = [];
    closed = 0; srcClosed = 0; loads = 0; facetLoads = 0;
    mergeAsks = [];
    pixivAsks = [];
    apiResponder = async () => ({ status: 'ok' });
    window._pendingAuthorSave = null;
    resetForm();
  }
  function body() {
    const c = apiCalls.find(x => String(x.url).includes('/api/library/update'));
    return c ? JSON.parse(c.opts.body) : null;
  }
  function keys(b) { return b ? Object.keys(b).sort() : []; }

  const cases = [];
  function test(name, fn) { cases.push([name, fn]); }

  // 1. 只改标题: 请求仅带 artwork_id + title
  test('只改标题时只提交 title', async () => {
    reset();
    els['lib-e-title'].value = '新标题';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', title: '新标题' });
    assert.strictEqual(lastActions.length, 1);
    assert.strictEqual(lastActions[0].ids[0], '990001');
    assert.strictEqual(closed, 1);
  });

  // 2. 无改动: 不发请求
  test('原样保存不发请求并提示没有改动', async () => {
    reset();
    await saveLibEdit('990001'); await flush();
    assert.strictEqual(apiCalls.length, 0);
    assert.ok(toasts.some(t => String(t[0]).includes('没有改动')), toasts);
    assert.strictEqual(closed, 0);
  });

  // 3. 改了又改回
  test('改了又改回视为没有改动', async () => {
    reset();
    els['lib-e-title'].value = '临时标题';
    els['lib-e-title'].value = '原标题';
    await saveLibEdit('990001'); await flush();
    assert.strictEqual(apiCalls.length, 0);
  });

  // 4. 标签顺序变化: 业务语义相同
  test('仅标签顺序变化不发请求', async () => {
    reset();
    setTags(['标签B', '标签A']);
    await saveLibEdit('990001'); await flush();
    assert.strictEqual(apiCalls.length, 0);
  });

  // 5. 标签增删: 只提交 tags
  test('标签增加只提交 tags', async () => {
    reset();
    setTags(['标签A', '标签B', '标签C']);
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), {
      artwork_id: '990001', tags: ['标签A', '标签B', '标签C'],
    });
  });

  // 6. 删除全部标签: 明确空数组
  test('删除全部标签提交空数组', async () => {
    reset();
    setTags([]);
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', tags: [] });
  });

  // 7. 清空来源链接: 明确空字符串
  test('清空来源链接提交空字符串', async () => {
    reset();
    els['lib-e-url'].value = '';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', source_url: '' });
  });

  // 8. 类型/R18 从非零切到零: 零必须提交
  test('类型与 R18 归零仍提交数值 0', async () => {
    reset();
    els['lib-e-type'].value = '0';
    els['lib-e-r18'].value = '0';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), {
      artwork_id: '990001', illust_type: 0, is_r18: 0,
    });
  });

  // 9. 作者未变: 不携带 author 字段
  test('作者未变时不提交作者字段', async () => {
    reset();
    els['lib-e-title'].value = '新标题';
    els['lib-e-author'].value = '作者甲';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', title: '新标题' });
  });

  // 10. 从下拉选中当前作者: 仍视为未变
  test('选中当前作者不提交作者字段', async () => {
    reset();
    els['lib-e-title'].value = '新标题';
    els['lib-e-author'].value = '作者甲';
    els['lib-e-author'].dataset.authorId = '123';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', title: '新标题' });
  });

  // 11. 作者输入为「名字_ID」且 ID 未变: 不重复写作者
  test('名字_ID 的 ID 未变不提交作者字段', async () => {
    reset();
    els['lib-e-title'].value = '新标题';
    els['lib-e-author'].value = '作者甲_123';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', title: '新标题' });
  });

  // 12. 留空作者 = 不修改
  test('作者留空按不修改处理', async () => {
    reset();
    els['lib-e-title'].value = '新标题';
    els['lib-e-author'].value = '';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', title: '新标题' });
  });

  // 13. 换 pixiv 作者: 只带 author_id/author_name
  test('换作者只提交作者字段', async () => {
    reset();
    els['lib-e-author'].value = '作者乙_456';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), {
      artwork_id: '990001', author_id: '456', author_name: '作者乙',
    });
  });

  // 14. 同一 pixiv 作者改名: 提交 ID + 新名
  test('同一 pixiv 作者改名提交新姓名', async () => {
    reset();
    els['lib-e-author'].value = '新名_123';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), {
      artwork_id: '990001', author_id: '123', author_name: '新名',
    });
  });

  // 15. 本地作者只改输入框文字: 不当作改名
  test('本地作者输入框文字变化不算换作者', async () => {
    reset();
    resetForm({ init_author_id: 'local_9', init_author_name: '本地作者',
                author_text: '本地作者' });
    els['lib-e-title'].value = '新标题';
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), { artwork_id: '990001', title: '新标题' });
  });

  // 16. 作者只给新名字: 先走「是不是 pixiv 作者」询问，body 只含新名字
  test('新作者名先询问且不夹带未改字段', async () => {
    reset();
    els['lib-e-author'].value = '新画师';
    await saveLibEdit('990001'); await flush();
    assert.strictEqual(apiCalls.length, 0);
    const pending = window._pendingAuthorSave || {};
    assert.strictEqual(pending.aid, '990001');
    assert.strictEqual(pending.name, '新画师');
    assert.deepStrictEqual(pending.body, {
      artwork_id: '990001', author_name: '新画师',
    });
  });

  // 17. 后端 unchanged: 不刷列表、不标记刚编辑
  test('后端 unchanged 不触发刷新和刚编辑标记', async () => {
    reset();
    apiResponder = async () => ({
      status: 'ok', unchanged: true, changed_fields: [],
    });
    await doSaveLibEdit('990001', { artwork_id: '990001', title: '新标题' });
    await flush();
    assert.ok(toasts.some(t => String(t[0]).includes('没有改动')), toasts);
    assert.strictEqual(lastActions.length, 0);
    assert.strictEqual(loads, 0);
    assert.strictEqual(facetLoads, 0);
  });

  // 18. 多字段同时改: 只带实际变化项
  test('多字段同时改只带变化项', async () => {
    reset();
    els['lib-e-title'].value = '新标题';
    els['lib-e-r18'].value = '0';
    setTags(['标签A']);
    await saveLibEdit('990001'); await flush();
    assert.deepStrictEqual(body(), {
      artwork_id: '990001', title: '新标题', is_r18: 0, tags: ['标签A'],
    });
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
  .replace('/*__EDIT_SLICE__*/', () => blockSrc) + ')();\n';
new Function('assert', generated)(assert);
