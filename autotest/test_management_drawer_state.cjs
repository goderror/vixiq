'use strict';

const assert = require('assert');
const path = require('path');
const { ManagementDrawerState } = require(
  path.resolve(__dirname, '../web/static/management-drawer-state.js')
);

function make() {
  return new ManagementDrawerState({ high: 160, low: 80, manualDistance: 60 });
}

const cases = [];
function test(name, fn) { cases.push([name, fn]); }

// 1. 下滑 170 收起，120/90 保持，70 主动上滑展开
test('下滑收起与阈值保持', () => {
  const s = make();
  assert.strictEqual(s.compact, false);
  s.input(1, 0);
  assert.strictEqual(s.scroll(120), false);
  assert.strictEqual(s.compact, false, '120 未超高阈值不应收起');
  assert.strictEqual(s.scroll(90), false);
  assert.strictEqual(s.compact, false, '90 未超高阈值不应收起');
  assert.strictEqual(s.scroll(170), true);
  assert.strictEqual(s.compact, true, '170 应自动收起');
  // 收起后再继续下滑保持收起
  s.scroll(220);
  assert.strictEqual(s.compact, true);
});

test('低位置主动上滑展开', () => {
  const s = make();
  s.toggle(500); // 手动收起
  assert.strictEqual(s.compact, true);
  s.settle(70);
  s.input(-1, 70);
  assert.strictEqual(s.compact, false, '70 上滑应展开');
  assert.strictEqual(s.manualTop, null, '自动展开清空 manualTop');
});

// 2. 布局导致 170 -> 0 不展开、不循环
test('布局位移不触发自动展开', () => {
  const s = make();
  s.input(1, 0);
  s.scroll(170);
  assert.strictEqual(s.compact, true);
  s.beginLayout();
  s.input(-1, 170);
  assert.strictEqual(s.compact, true, '布局锁下 input 不展开');
  s.scroll(0); // 物理归零
  assert.strictEqual(s.compact, true, '布局归零不得展开');
  s.settle(0);
  assert.strictEqual(s.lastTop, 0);
  s.scroll(0);
  assert.strictEqual(s.compact, true, '布局后无意图滚动仍收起');
});

// 3. 无意图多轮尺寸回跳不变
test('无意图尺寸回跳不改变状态', () => {
  const s = make();
  s.input(1, 0);
  s.scroll(170);
  assert.strictEqual(s.compact, true);
  s.input(0, 170);
  for (const t of [0, 40, 0, 200, 0, 300, 0]) {
    assert.strictEqual(s.scroll(t), false);
    assert.strictEqual(s.compact, true, '无意图钳制不应改变状态');
  }
  s.reset(0);
  s.input(0, 0);
  for (const t of [0, 200, 0, 400]) {
    assert.strictEqual(s.scroll(t), false);
    assert.strictEqual(s.compact, false, '展开态无意图不应收起');
  }
});

// 4. 短列表不自动收
test('短列表不自动收起', () => {
  const s = make();
  s.input(1, 0);
  for (const t of [20, 60, 100, 159]) {
    assert.strictEqual(s.scroll(t), false);
    assert.strictEqual(s.compact, false);
  }
});

// 5. 手动展开后需继续下滑 60 才收
test('手动展开需额外下滑 manualDistance', () => {
  const s = make();
  assert.strictEqual(s.toggle(300), true);
  assert.strictEqual(s.compact, true);
  assert.strictEqual(s.toggle(300), true); // 手动展开
  assert.strictEqual(s.compact, false);
  assert.strictEqual(s.manualTop, 300);
  s.input(1, 300);
  s.scroll(350);
  assert.strictEqual(s.compact, false, '仅超出 high 但未到 manualTop+60 不收起');
  s.scroll(359);
  assert.strictEqual(s.compact, false);
  s.scroll(360);
  assert.strictEqual(s.compact, true, '到 manualTop+60 应收起');
});

// 6. 布局锁覆盖所有自动输入
test('布局锁阻止所有自动开合', () => {
  const s = make();
  s.beginLayout();
  assert.strictEqual(s.layout, true);
  assert.strictEqual(s.intent, 0);
  s.input(1, 0);
  s.scroll(500);
  assert.strictEqual(s.compact, false, '布局中下滑不收起');
  s.input(-1, 0);
  assert.strictEqual(s.compact, false, '布局中 input 不展开');
  s.toggle(0); // 手动仍可用
  assert.strictEqual(s.compact, true);
  s.input(-1, 0);
  assert.strictEqual(s.compact, true, '布局中输入上滑不展开');
  s.scroll(0);
  assert.strictEqual(s.compact, true);
  s.settle(0);
  assert.strictEqual(s.layout, false);
  assert.strictEqual(s.intent, 0);
});

// 7. focusedExtra 阻止收起
test('focusedExtra 阻止自动收起', () => {
  const s = make();
  s.input(1, 0);
  assert.strictEqual(s.scroll(400, { focusedExtra: true }), false);
  assert.strictEqual(s.compact, false);
  assert.strictEqual(s.scroll(500), true);
  assert.strictEqual(s.compact, true);
});

// 8. 手动收起后零位置上滑可展开
test('零位置上滑恢复展开', () => {
  const s = make();
  s.settle(0);
  s.toggle(0);
  assert.strictEqual(s.compact, true);
  s.input(-1, 0);
  assert.strictEqual(s.compact, false, '物理 top=0 上滑应展开');
  assert.strictEqual(s.manualTop, null);
});

// 9. reset 对应切 tab 统一展开
test('reset 统一展开', () => {
  const s = make();
  s.input(1, 0);
  s.scroll(300);
  assert.strictEqual(s.compact, true);
  s.reset(0);
  assert.strictEqual(s.compact, false);
  assert.strictEqual(s.lastTop, 0);
  assert.strictEqual(s.intent, 0);
  assert.strictEqual(s.layout, false);
  assert.strictEqual(s.manualTop, null);
});

// 控制器也必须消费 input 的直接变化；只测纯状态无法发现漏接 paint。
const vm = require('node:vm');
const fs = require('node:fs');
const controller = fs.readFileSync(require('node:path').join(__dirname, '../web/static/management-toolbar.js'), 'utf8');
function controllerFixture() {
  const state = make();
  state.toggle(0);
  const screen = { compact: true, paints: 0 };
  const context = vm.createContext({
    state, active: 'wallpapers', main: { scrollTop: 0 }, intentAt: 0,
    INTENT_TTL: 1800, document: { activeElement: { tagName: 'MAIN' } },
    Date, focusedExtra: () => false, activeToolbar: () => ({ extra: { contains: () => false } }),
    paint: () => { screen.compact = state.compact; screen.paints++; },
  });
  for (const [from, to] of [
    ['  function setIntent(', '  function decayIntent('],
    ['  function decayIntent(', '  // ---- 滚动'],
    ['  function onWheel(', '  // ---- keydown'],
    ['  function isEditable(', '  function onKeydown('],
    ['  function onKeydown(', '  // ---- touch'],
  ]) {
    const start = controller.indexOf(from), end = controller.indexOf(to, start);
    assert.ok(start >= 0 && end > start, from);
    vm.runInContext(controller.slice(start, end), context);
  }
  return { state, screen, context };
}
test('控制器：高度钳制到零后 Home 无需额外滚动即展开', () => {
  const f = controllerFixture();
  vm.runInContext("onKeydown({key:'Home'})", f.context);
  assert.equal(f.screen.compact, false);
  assert.equal(f.screen.paints, 1);
  vm.runInContext("onKeydown({key:'Home'})", f.context);
  assert.equal(f.screen.paints, 1, '重复顶部输入不能反复重画');
});
test('控制器：零位置向上滚轮立即展开', () => {
  const f = controllerFixture();
  vm.runInContext('onWheel({deltaY:-30,target:{}})', f.context);
  assert.equal(f.screen.compact, false);
  assert.equal(f.screen.paints, 1);
});
test('控制器：零位置向下滚轮不展开、不提前收起', () => {
  const f = controllerFixture();
  vm.runInContext('onWheel({deltaY:30,target:{}})', f.context);
  assert.equal(f.screen.compact, true);
  assert.equal(f.screen.paints, 0);
});
test('控制器：搜索框内 Home 保留编辑语义', () => {
  const f = controllerFixture();
  vm.runInContext("document.activeElement={tagName:'INPUT'};onKeydown({key:'Home'})", f.context);
  assert.equal(f.screen.compact, true);
  assert.equal(f.screen.paints, 0);
});

let failed = 0;
for (const [name, fn] of cases) {
  try {
    fn();
    console.log(`PASS ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${e && e.message}`);
  }
}
console.log(`\n${cases.length - failed}/${cases.length} passed`);
process.exitCode = failed === 0 ? 0 : 1;
