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
  class ManagementDrawerState {
    constructor({ high = 160, low = 80, manualDistance = 60 } = {}) {
      this.high = high;
      this.low = low;
      this.manualDistance = manualDistance;
      this.compact = false;
      this.lastTop = 0;
      this.intent = 0;
      this.layout = false;
      this.manualTop = null;
    }

    // 滚轮方向输入：direction<0 上滚，>0 下滚，0 无意图。
    // 返回是否发生状态改变。
    input(direction, top) {
      this.intent = direction < 0 ? -1 : direction > 0 ? 1 : 0;
      // 折叠态下的主动上滑：允许展开（含布局把物理 top 归零的场景）。
      if (this.compact && !this.layout && this.intent < 0 && top <= this.low) {
        this._expand();
        return true;
      }
      return false;
    }

    // scroll 回调：记录位移，按意图与阈值决定自动开合。
    scroll(top, { focusedExtra = false } = {}) {
      const delta = top - this.lastTop;
      this.lastTop = top;

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
      } else if (this.intent < 0 && delta < 0) {
        if (top < this.low && this.compact) {
          this._expand();
          return true;
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
