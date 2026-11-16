// management-toolbar.js
// 三管理页（library / authors / wallpapers）共享的吸顶工具栏 + 抽屉式收起控制器。
// 核心思路：滚动本身只是触发条件，真正的开合由“用户意图”驱动。
// 如果只看 scrollTop 阈值，反向小幅回滚、分页 scrollIntoView、resize 补正都会误触发收起，
// 所以这里把 direction intent / manualDistance / layout 保护与 DOM 写入解耦。
(function () {
  'use strict';

  var STATE = window.ManagementDrawerState;
  if (!STATE) return;

  var TABS = ['library', 'authors', 'wallpapers'];
  var INTENT_TTL = 1800;      // 用户意图有效期（ms）
  var MANUAL = 60;            // 与 State 手动展开保持距离一致
  var COMPACT_AT = 80;        // 向上/顶部附近可直接展开的边界

  var main = document.querySelector('main');
  var root = main && main.closest ? main : document.querySelector('main');
  main = main || root;
  if (!main) return;

  var state = new STATE({ high: 160, low: 80, manualDistance: MANUAL });

  var toolbars = {};   // name -> {bar, heading, primary, extra, selection, pager, toggle}
  var sections = {};   // name -> section
  var active = null;

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $all(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }

  function collect() {
    $all('.management-toolbar[data-management]').forEach(function (bar) {
      var name = bar.getAttribute('data-management');
      if (TABS.indexOf(name) === -1) return;
      toolbars[name] = {
        bar: bar,
        heading: $('.management-heading', bar),
        primary: $('.management-primary', bar),
        extra: $('.management-extra', bar),
        selection: $('.management-selection', bar),
        pager: $('.management-pager', bar),
        toggle: $('.management-toggle', bar)
      };
      var sec = bar.closest('#tab-' + name) || bar.parentElement;
      sections[name] = sec;
    });
    // 首次按 .tabpage.active 决定当前管理页
    var tab = $('.tabpage.active');
    if (tab && tab.id && tab.id.indexOf('tab-') === 0) {
      var nm = tab.id.slice(4);
      if (TABS.indexOf(nm) !== -1) active = nm;
    }
  }

  function isActive(name) { return name === active; }
  function activeToolbar() { return active ? toolbars[active] : null; }

  function focusedExtra() {
    var t = activeToolbar();
    if (!t || !t.extra) return false;
    var ae = document.activeElement;
    return !!(ae && t.extra.contains(ae));
  }

  // ---- 统一的 UI 写入（只有真正改变才碰 DOM） ----
  var lastPaintCompact = null;
  var lastToggleText = null;

  function applyCompactClass(compact) {
    if (lastPaintCompact === compact) return;
    lastPaintCompact = compact;
    if (compact) main.classList.add('management-compact');
    else main.classList.remove('management-compact');
  }

  function paintToggles() {
    var compact = !!state.compact;
    var text = compact ? '展开菜单' : '收起菜单';
    var exp = compact ? 'false' : 'true';
    if (lastToggleText === text) return;
    lastToggleText = text;
    TABS.forEach(function (n) {
      var t = toolbars[n] && toolbars[n].toggle;
      if (!t) return;
      if (t.textContent !== text) t.textContent = text;
      if (t.getAttribute('aria-expanded') !== exp) t.setAttribute('aria-expanded', exp);
    });
  }

  // ---- 标签栏叠三角: 收起态悬停滑出简化菜单 ----
  var peekBtn = document.getElementById('mgmt-peek');
  var peekTimer = 0;

  function peekCancelClose() { clearTimeout(peekTimer); }

  function peekOpen() {
    if (!active || !state.compact) return;
    clearTimeout(peekTimer);
    if (!main.classList.contains('management-peek')) main.classList.add('management-peek');
    if (peekBtn) peekBtn.setAttribute('aria-expanded', 'true');
  }

  function peekClose() {
    clearTimeout(peekTimer);
    main.classList.remove('management-peek');
    if (peekBtn) peekBtn.setAttribute('aria-expanded', 'false');
  }

  function peekFocused() {
    var t = activeToolbar();
    if (!t || !t.bar || !document.activeElement) return false;
    if (peekBtn && peekBtn.contains(document.activeElement)) return false;
    return t.bar.contains(document.activeElement);
  }

  function peekCloseSoon() {
    clearTimeout(peekTimer);
    peekTimer = setTimeout(function () {
      if (peekFocused()) return;   // 焦点还在菜单里(例如正在输入搜索词)就不收起
      peekClose();
    }, 220);
  }

  function paintPeek() {
    if (!peekBtn) return;
    var show = !!active && !!state.compact;
    if (show) {
      peekBtn.removeAttribute('hidden');
    } else {
      peekBtn.setAttribute('hidden', '');
      peekClose();
    }
  }

  // ---- 布局保护 + settle 世代 ----
  var settleGen = 0;

  function paint(force) {
    if (!active) { paintPeek(); return; }
    var compact = !!state.compact;
    var changed = force || compact !== lastPaintCompact;
    if (!changed && lastToggleText === (compact ? '展开菜单' : '收起菜单')) {
      paintPeek();
      return;
    }

    if (changed) {
      // beginLayout 防止 class 切换触发的回流被 state 当作“用户滚动”再次收起
      try { state.beginLayout(); } catch (e) {}
      applyCompactClass(compact);
      if (!compact) peekClose();   // 菜单已完全展开, 不再需要浮层
      try { window.closeLibRedownload && window.closeLibRedownload(); } catch (e) {}
      var gen = ++settleGen;
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          if (gen !== settleGen) return; // 取消陈旧 settle
          try { state.settle(main.scrollTop); } catch (e) {}
          measure();
        });
      });
    }
    paintToggles();
    paintPeek();
  }

  // ---- 测量：只给结果定位留吸顶高度 ----
  // 2026-11-17: 展开区不再独立滚动 (见 management-toolbar.css), 所以不再算 extra 限高 ——
  // 限高会裁掉作者/标签候选浮层, 也是"鼠标停在菜单上滚不动"的根源之一。
  var measureRaf = 0;
  var lastMargins = {};
  var lastMainMargin = null;

  function measure() {
    if (!active) return;
    var t = activeToolbar();
    if (!t || !t.bar) return;
    if (!isActive(active)) return;
    var bar = t.bar;
    var rect = bar.getBoundingClientRect();
    if (rect.height === 0 && rect.width === 0) return; // 不可见
    var barH = rect.height;

    // scroll-margin 给结果定位，避开吸顶高度
    var sec = sections[active];
    if (sec) {
      var mg = String(Math.round(barH + 12)) + 'px';
      if (lastMargins[active] !== mg) {
        lastMargins[active] = mg;
        sec.style.setProperty('--management-scroll-margin', mg);
      }
      if (lastMainMargin !== mg) {
        lastMainMargin = mg;
        main.style.setProperty('--management-scroll-margin', mg);
      }
    }
  }

  function scheduleMeasure() {
    if (measureRaf) return;
    measureRaf = requestAnimationFrame(function () {
      measureRaf = 0;
      measure();
    });
  }

  // ---- 意图方向记录（带 TTL） ----
  var intentAt = 0;

  function setIntent(dir) {
    if (!active) return false;
    if (dir === 0) { state.intent = 0; intentAt = 0; return false; }
    intentAt = Date.now();
    // 列表变短可能已经把 scrollTop 压到零；此时向上输入不会再产生 scroll。
    // 必须消费 input 的直接状态变化，不能只记录方向、永远等待滚动事件。
    var changed = state.input(dir, main.scrollTop);
    if (changed) paint(false);
    return changed;
  }

  function decayIntent() {
    if (intentAt && Date.now() - intentAt > INTENT_TTL) {
      state.intent = 0;
      intentAt = 0;
    }
  }

  // ---- 滚动 rAF 节流 ----
  var scrollRaf = 0;
  var dragging = false;
  var dragStartTop = 0;

  function onScroll() {
    if (!active) return;
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(function () {
      scrollRaf = 0;
      if (!active) return;
      decayIntent();

      var top = main.scrollTop;
      // 滚动列表时收起滑出的简化菜单
      if (main.classList.contains('management-peek') && !peekFocused()) peekClose();

      // 正在拖动滚动条：按 scrollTop 差推断意图
      if (dragging) {
        var diff = top - dragStartTop;
        if (Math.abs(diff) >= 2) {
          setIntent(diff > 0 ? 1 : -1);
          dragStartTop = top;
        }
      }

      var fe = focusedExtra();
      var changed = false;
      try { changed = state.scroll(top, { focusedExtra: fe }); } catch (e) {}
      if (changed) paint(false);
    });
  }

  // ---- wheel ----
  // 只有真正可滚动的浮层 (作者/标签候选、下拉菜单) 才拦滚轮;
  // 普通菜单区域一律滚主列表 —— 否则鼠标停在筛选栏上滚不动。
  function inScrollableOverlay(el) {
    return !!(el && el.closest && el.closest('.combo-list, .lib-menu, .lib-redownload-menu, .dropdown-menu'));
  }

  function onWheel(e) {
    if (!active) return;
    if (inScrollableOverlay(e.target)) return;
    var dy = e.deltaY;
    if (!dy) return;
    decayIntent();
    setIntent(dy > 0 ? 1 : -1);
    var top = main.scrollTop;
    var fe = focusedExtra();
    var changed = false;
    try { changed = state.scroll(top, { focusedExtra: fe }); } catch (err) {}
    if (changed) paint(false);
  }

  // ---- keydown ----
  function isEditable(el) {
    if (!el) return false;
    var tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function onKeydown(e) {
    if (!active) return;
    if (e.key === 'Escape' && main.classList.contains('management-peek')) { peekClose(); return; }
    if (isEditable(document.activeElement)) return;
    var k = e.key;
    var dir = 0;
    if (k === 'ArrowDown' || k === 'PageDown' || k === ' ' || k === 'Spacebar' || k === 'End') dir = 1;
    else if (k === 'ArrowUp' || k === 'PageUp' || k === 'Home') dir = -1;
    if (!dir) return;
    // Home/End 不 preventDefault，其它维持原生行为即可
    decayIntent();
    setIntent(dir);
    var top = main.scrollTop;
    var fe = focusedExtra();
    var changed = false;
    try { changed = state.scroll(top, { focusedExtra: fe }); } catch (err) {}
    if (changed) paint(false);
  }

  // ---- touch ----
  var touchStartY = 0;
  var touchActive = false;

  function onTouchStart(e) {
    if (!active) return;
    if (!e.touches || e.touches.length !== 1) { touchActive = false; return; }
    touchActive = true;
    touchStartY = e.touches[0].clientY;
  }

  function onTouchMove(e) {
    if (!active || !touchActive) return;
    if (!e.touches || e.touches.length !== 1) return;
    if (inScrollableOverlay(e.target)) return;
    var dy = e.touches[0].clientY - touchStartY;
    if (Math.abs(dy) <= 4) return;
    decayIntent();
    // 手指向下 = 内容上移 = 负方向
    setIntent(dy > 0 ? -1 : 1);
    touchStartY = e.touches[0].clientY;
    var top = main.scrollTop;
    var fe = focusedExtra();
    var changed = false;
    try { changed = state.scroll(top, { focusedExtra: fe }); } catch (err) {}
    if (changed) paint(false);
  }

  function onTouchEnd() { touchActive = false; }

  // ---- pointer：清意图 + 滚动条拖动 ----
  var dragStartRegistered = false;

  function onPointerDown(e) {
    if (!active) return;
    var rect = main.getBoundingClientRect();
    var x = e.clientX;
    var isScrollbar = (x >= rect.right - 22);
    if (isScrollbar) {
      dragging = true;
      dragStartTop = main.scrollTop;
      if (!dragStartRegistered) {
        dragStartRegistered = true;
        document.addEventListener('pointerup', onPointerUp, true);
      }
    } else {
      // 普通控件点击清意图，避免旧方向残留
      state.intent = 0;
      intentAt = 0;
    }
  }

  function onPointerUp() {
    dragging = false;
  }

  // ---- 按钮点击 ----
  function onToolbarClick(e) {
    if (!active) return;
    var t = activeToolbar();
    if (!t) return;
    var toggle = e.target.closest && e.target.closest('.management-toggle');
    if (toggle && t.toggle && t.bar.contains(toggle)) {
      var top = main.scrollTop;
      var changed = false;
      try { changed = state.toggle(top); } catch (err) {}
      if (changed) paint(false);
      return;
    }
  }

  function onPageClick(e) {
    if (!active) return;
    var pager = e.target.closest && e.target.closest(
      '#lib-pager-top, #lib-pager-bottom, #authors-pager-top, #authors-pager-bottom, #wf-pager'
    );
    if (!pager) return;
    // 记录正意图，配合原 scrollIntoView 触发收起
    decayIntent();
    setIntent(1);
    var top = main.scrollTop;
    var fe = focusedExtra();
    var changed = false;
    try { changed = state.scroll(top, { focusedExtra: fe }); } catch (err) {}
    if (changed) paint(false);
  }

  function onToTopClick(e) {
    if (!active) return;
    var btn = e.target.closest && e.target.closest('#to-top');
    if (!btn) return;
    decayIntent();
    setIntent(-1);
    var top = main.scrollTop;
    var fe = focusedExtra();
    var ch = false;
    try { ch = state.input(-1, top); } catch (err) {}
    if (ch) paint(false);
  }

  // ---- tab change ----
  var lastTabName = null;

  function onTabChange(e) {
    var name = e && e.detail && e.detail.name;
    if (!name) return;
    if (name === lastTabName) return;
    lastTabName = name;
    var isMgmt = TABS.indexOf(name) !== -1;
    if (isMgmt) {
      ++settleGen;
      active = name;
      main.scrollTop = 0;
      try { state.reset(0); } catch (err) {}
      applyCompactClass(false);
      lastPaintCompact = false;
      peekClose();
      paintToggles();
      paintPeek();
      scheduleMeasure();
    } else {
      active = null;
      main.classList.remove('management-compact');
      lastPaintCompact = null;
      peekClose();
      paintPeek();
      main.style.setProperty('--management-scroll-margin', '0px');
      lastMainMargin = '0px';
    }
  }

  // ---- 初始化 ----
  function init() {
    if (!window.ManagementDrawerState) return;
    collect();
    if (!Object.keys(toolbars).length) return;

    // 当前 tab 名称
    lastTabName = active;

    main.tabIndex = 0; // 便于键盘滚动

    // 统一展开初始态（无活动管理页时不动）
    if (active) {
      try { state.reset(main.scrollTop); } catch (e) {}
      applyCompactClass(false);
      lastPaintCompact = false;
      paintToggles();
    }
    paintPeek();

    // 只装一次监听
    main.addEventListener('scroll', onScroll, { passive: true });
    main.addEventListener('wheel', onWheel, { passive: true });
    document.addEventListener('keydown', onKeydown);
    main.addEventListener('touchstart', onTouchStart, { passive: true });
    main.addEventListener('touchmove', onTouchMove, { passive: true });
    main.addEventListener('touchend', onTouchEnd, { passive: true });
    main.addEventListener('pointerdown', onPointerDown, { passive: true });
    main.addEventListener('click', onToolbarClick);
    main.addEventListener('click', onPageClick);
    document.addEventListener('click', onToTopClick, true);
    document.addEventListener('vixiq:tab-change', onTabChange);

    // 叠三角: 悬停/聚焦滑出, 移开或失焦延时收起; 点一下也能切换
    if (peekBtn) {
      peekBtn.addEventListener('mouseenter', function () { peekCancelClose(); peekOpen(); });
      peekBtn.addEventListener('mouseleave', peekCloseSoon);
      peekBtn.addEventListener('focus', peekOpen);
      peekBtn.addEventListener('blur', peekCloseSoon);
      peekBtn.addEventListener('click', function (e) {
        e.preventDefault();
        if (main.classList.contains('management-peek')) peekClose();
        else peekOpen();
      });
    }
    TABS.forEach(function (n) {
      var t = toolbars[n];
      if (!t || !t.bar) return;
      t.bar.addEventListener('mouseenter', function () {
        if (main.classList.contains('management-peek')) peekCancelClose();
      });
      t.bar.addEventListener('mouseleave', function () {
        if (main.classList.contains('management-peek')) peekCloseSoon();
      });
    });

    // ResizeObserver：main + 3 toolbar
    if (typeof ResizeObserver === 'function') {
      var ro = new ResizeObserver(function () { scheduleMeasure(); });
      ro.observe(main);
      TABS.forEach(function (n) {
        if (toolbars[n] && toolbars[n].bar) ro.observe(toolbars[n].bar);
      });
    }
    window.addEventListener('resize', scheduleMeasure, { passive: true });

    scheduleMeasure();
  }

  init();
})();
