// 执行真实前端函数，使用小型 DOM/下载替身，验证跨页选择与两个下载入口。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');
const elements = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, { textContent: '', innerHTML: '', disabled: false, hidden: true,
    _lastItems: [], setAttribute() {}, focus() {}, querySelectorAll() { return []; }, classList: { toggle() {} } });
  return elements.get(key);
}
const calls = [], notices = [];
const context = vm.createContext({ console, $, $$: () => [], window: {},
  document: { querySelector: () => null },
  esc: s => String(s || '').replaceAll('<', '&lt;'), fmtSize: n => String(n || 0),
  sourceLabel: s => s, toast: msg => notices.push(msg),
  autoStartDownload: async () => calls.push({ start: true }), setLastAction() {},
  api: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); return { added: 1 }; },
  repairWorks: async ids => { calls.push({ repair: ids }); return false; },
});
function $(key) { return element(key); }
vm.runInContext(`let libSelected = new Set(), libDataView = 'artwork', _libAnchor = null;
  const libSelectionData = new Map();
  let libCounts={artworks:70,images:210}, libTotal=70,libPages=2,libPage=1;
  const LIB_PAGE_SIZE=60;`, context);
function load(start, end) { vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))), context); }
load('function canRedownload(w)', 'function fmtSize(');
load('function libSelectionContext()', 'let _libAnchor =');
load('function renderLibPagers()', 'function goLibPage(');
load('window.libBatchAddCart =', '// ♻ 重下修复:');
load('window.libBatchRepair =', '// 🔎 批量搜索来源:');
const run = script => vm.runInContext(script, context);
const work = (id, source, n) => ({ _key: id, artwork_id: id, source, title: id, page_count: 99,
  pages: Array.from({ length: n }, (_, i) => ({ page_num: i, local_path: `${id}_p${i}.png` })) });
(async () => {
  const a = work('700001', 'pixiv', 3), b = work('ext_1', 'other', 2);
  element('#lib-result')._lastItems = [a];
  run("libSelected.add('700001'); syncLibSelUI();");
  assert.equal(element('#lib-total').textContent, '已选 1 个作品 · 3 张图片');
  element('#lib-result')._lastItems = [b];
  run("libSelected.add('ext_1'); syncLibSelUI();");
  assert.equal(element('#lib-total').textContent, '已选 2 个作品 · 5 张图片');
  assert.equal(element('#lib-redownload-btn').disabled, false);
  assert.equal(element('#lib-batch-merge').disabled, true);
  await run('window.libBatchAddCart()');
  assert.equal(calls[0].url, '/api/cart/batch-add');
  assert.deepEqual(calls[0].body.items.map(x => x.artwork_id), ['700001']);
  assert.equal(calls.filter(x => x.start).length, 1);
  assert.ok(notices.some(s => s.includes('非 pixiv 来源')));
  assert.equal(element('#lib-total').textContent, '已选 0 个作品 · 0 张图片');

  element('#lib-result')._lastItems = [b];
  run("libSelected.add('ext_1'); syncLibSelUI();");
  assert.equal(element('#lib-redownload-btn').disabled, true);
  const before = calls.length;
  await run('window.libBatchAddCart()');
  assert.equal(calls.length, before);

  element('#lib-result')._lastItems = [a, { ...work('700002', 'danbooru', 1) }];
  run("libSelected.clear(); libSelected.add('700001'); libSelected.add('700002'); syncLibSelUI();");
  await run('window.libBatchRepair()');
  assert.deepEqual(Array.from(calls.at(-1).repair), ['700001']);

  element('#lib-result')._lastItems = [0, 1].map(page_num => ({ ...a, _key: `700001|${page_num}`, page_num }));
  run("libDataView='image'; libSelected.clear(); libSelected.add('700001|0');libSelected.add('700001|1');syncLibSelUI();");
  assert.equal(element('#lib-total').textContent, '已选 1 个作品 · 2 张图片');
  assert.equal(element('#lib-batch-merge').disabled, false);
  run('renderLibPagers();');
  assert.ok(element('#lib-pager-top').innerHTML.includes('共 70 个作品 · 210 张图片'));
  assert.ok(element('#lib-pager-top').innerHTML.includes('value="1"'));
  assert.equal(element('#lib-pager-top').innerHTML, element('#lib-pager-bottom').innerHTML);
  run('libCounts={artworks:0,images:0};libTotal=0;libPages=1;renderLibPagers();');
  assert.ok(element('#lib-pager-top').innerHTML.includes('共 0 个作品 · 0 张图片'));
  console.log('PASS: 跨页选择、作品/图片计数、Pixiv 过滤、补完/修复入口、清空与分页统计。');
})().catch(err => { console.error(err); process.exitCode = 1; });
