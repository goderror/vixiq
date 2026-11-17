(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.ManagementDrawerState = factory().ManagementDrawerState;
  }
})(typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  // 无 DOM 依赖的抽屉开合状态机。
  // compact=true 表示折叠（抽屉收起），false 表示展开。
  // 2026-11-17: 收起态不再「滚到顶部就展开」—— 到顶后还要继续往下拉 (pull) 超过阈值才展开,
  // 与「下拉刷新」同一个手感, 避免用户只是滚回顶部看一眼就被弹开菜单。
  class ManagementDrawerState {
    constructor({ high = 160, low = 80, manualDistance = 60, pullThreshold = 90 } = {}) {
      this.high = high;
      this.low = low;
      this.manualDistance = manualDistance;
      this.pullThreshold = pullThreshold;
      this.compact = false;
      this.lastTop = 0;
      this.intent = 0;
      this.layout = false;
      this.manualTop = null;
      this.pullAccum = 0;
    }

    // 滚轮方向输入：direction<0 上滚，>0 下滚，0 无意图。
    // 只记方向；展开必须由 pull() 触发 (见文件头说明)。
    input(direction, top) {
      this.intent = direction < 0 ? -1 : direction > 0 ? 1 : 0;
      if (this.intent > 0) this.pullAccum = 0;
      return false;
    }

    // 到顶后继续「下拉」的距离累积。distance 用正数表示下拉量。
    // 返回是否发生状态改变。
    pull(distance) {
      if (!this.compact || this.layout) return false;
      const d = Number(distance) || 0;
      if (d <= 0) return false;
      this.pullAccum += d;
      if (this.pullAccum >= this.pullThreshold) {
        this.pullAccum = 0;
        this._expand();
        return true;
      }
      return false;
    }

    resetPull() {
      this.pullAccum = 0;
    }

    // 显式展开 (「回到顶部」这类明确动作仍可直接展开)。
    expand() {
      if (!this.compact) return false;
      this._expand();
      this.pullAccum = 0;
      return true;
    }

    // scroll 回调：记录位移，按意图与阈值决定自动开合。
    scroll(top, { focusedExtra = false } = {}) {
      const delta = top - this.lastTop;
      this.lastTop = top;
      if (delta > 0) this.pullAccum = 0;

      if (this.layout) return false; // 布局中仅记录，不转移
      if (this.intent > 0 && delta > 0) {
        if (top > this.high && !focusedExtra) {
          if (!this.compact) {
            if (this.manualTop === null ||
                top >= this.manualTop + this.manualDistance) {
              this._collapse();
              return true;
            }
          }
        }
      }
      return false;
    }

    toggle(top) {
      if (this.compact) {
        this._expand(top);
      } else {
        this._collapse();
      }
      return true;
    }

    beginLayout() {
      this.layout = true;
      this.intent = 0;
    }

    settle(top) {
      this.lastTop = top;
      this.layout = false;
      this.intent = 0;
    }

    reset(top = 0) {
      this.compact = false;
      this.lastTop = top;
      this.intent = 0;
      this.layout = false;
      this.manualTop = null;
      this.pullAccum = 0;
    }

    _expand(top) {
      // 自动展开（未传 top）清空手动覆盖；手动展开（传 top）记录位置。
      this.compact = false;
      this.manualTop = top === undefined ? null : top;
    }

    _collapse() {
      this.compact = true;
      this.manualTop = null;
    }
  }

  return { ManagementDrawerState };
});
