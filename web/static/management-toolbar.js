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
      // 面板与 main 的顶部留白归零: 分页行要贴紧标签栏 (2026-11-17 反馈展开后有缝)
      if (bar.parentElement && bar.parentElement.classList) {
        bar.parentElement.classList.add('management-panel');
      }
      // 下拉提示: 跟手位移时浮在工具栏下方 (不占布局高度)
      if (!bar.querySelector('.mgmt-pull-hint')) {
        var hint = document.createElement('div');
        hint.className = 'mgmt-pull-hint';
        hint.textContent = '继续下拉展开菜单';
        bar.appendChild(hint);
      }
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

  // 叠三角开合时临时关掉 .management-main 的高度过渡:
  // 收起浮层时它要从 absolute 回到文档流, auto→0 的过渡会在中途撑出高度,
  // 把下面的列表顶一下 —— 表现就是「鼠标划过三角形, 图片列表跳一下」(2026-11-18)。
  function peekNoAnim() {
    main.classList.add('management-peek-noanim');
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        main.classList.remove('management-peek-noanim');
      });
    });
  }

  function peekOpen() {
    if (!active || !state.compact) return;
    clearTimeout(peekTimer);
    if (!main.classList.contains('management-peek')) {
      peekNoAnim();
      main.classList.add('management-peek');
    }
    if (peekBtn) peekBtn.setAttribute('aria-expanded', 'true');
  }

  function peekClose() {
    clearTimeout(peekTimer);
    if (main.classList.contains('management-peek')) peekNoAnim();
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

  // ---- 下拉展开: 跟手位移 + 阻尼 + 松手回弹 ----
  // 只累计距离还不够「像下拉刷新」—— 必须让列表跟着手指/滚轮往下走一段,
  // 到阈值才展开, 没到就弹回去 (2026-11-17 用户反馈"没有阻力")。
  var RESULT_IDS = { library: 'lib-result', authors: 'authors-result', wallpapers: 'wf-result' };
  var PULL_DAMP = 0.4;        // 阻尼: 累计 100px 只位移 40px
  var PULL_MAX_OFF = 120;     // 视觉位移上限 (4 格时刚好走满)
  var PULL_WHEEL_NEED = 400;  // 滚轮: 约 4 格才展开 (一格 100px)
  var PULL_TOUCH_NEED = 160;  // 触屏: 手指拉这么远即可
  var pullIdleTimer = 0;

  function pullResultEl() {
    var id = active ? RESULT_IDS[active] : '';
    return id ? document.getElementById(id) : null;
  }

  function pullHintEl() {
    var t = activeToolbar();
    return t && t.bar ? t.bar.querySelector('.mgmt-pull-hint') : null;
  }

  function pullOffset() {
    var acc = state.pullAccum || 0;
    return Math.min(PULL_MAX_OFF, acc * PULL_DAMP);
  }

  function paintPull() {
    var off = pullOffset();
    var el = pullResultEl();
    if (el) {
      el.style.transition = 'none';
      el.style.transform = off > 0.5 ? 'translateY(' + off.toFixed(1) + 'px)' : '';
    }
    var hint = pullHintEl();
    if (hint) {
      hint.textContent = '继续下拉展开菜单';
      hint.style.opacity = off > 4 ? '1' : '0';
      hint.style.transform = 'translate(-50%, ' + (off * 0.6 - 4).toFixed(1) + 'px)';
    }
  }

  function settlePull() {
    clearTimeout(pullIdleTimer);
    pullIdleTimer = 0;
    try { state.resetPull(); } catch (e) {}
    var el = pullResultEl();
    if (el) {
      el.style.transition = 'transform .2s ease';
      el.style.transform = '';
    }
    var hint = pullHintEl();
    if (hint) {
      hint.style.opacity = '0';
      hint.style.transform = 'translate(-50%, -4px)';
    }
  }

  function schedulePullIdle() {
    clearTimeout(pullIdleTimer);
    // 滚轮没有"松手"事件: 停手一小会儿就回弹。给 520ms, 覆盖正常连续滚动的间隔
    // (240ms 会在两格之间误回弹, 用户看到的还是"一格就展开")。
    pullIdleTimer = setTimeout(settlePull, 520);
  }

  function consumePull(distance, threshold) {
    var expanded = false;
    try { expanded = state.pull(distance, threshold); } catch (e) {}
    if (expanded) {
      settlePull();     // 先还原列表位移, 再让菜单高度过渡展开, 不叠两段动画
      paint(false);
      return true;
    }
    paintPull();
    return false;
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
      if (top > 2 && state.pullAccum) settlePull();   // 离开顶部 → 立即取消下拉位移
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
    // deltaMode: 0=像素 / 1=行 / 2=页。统一换算成像素, 否则不同鼠标/触控板一格差几十倍
    var px = dy * (e.deltaMode === 1 ? 40 : (e.deltaMode === 2 ? 800 : 1));
    decayIntent();
    // 收起态且已经在顶部: 继续向上滚 = 「下拉」，列表跟手位移, 累计超过阈值才展开。
    // 滚轮一格通常 100px, 所以这里要求约 3 格 (260px) —— 一格就开等于没有拖拽感。
    if (state.compact && px < 0 && main.scrollTop <= 2) {
      if (!consumePull(-px, PULL_WHEEL_NEED)) schedulePullIdle();
      return;
    }
    if (px > 0) settlePull();
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
    if (e.key === 'Escape' && main.classList.contains('management-peek')) {
      // 先把焦点还给叠三角（focus 事件可能再次打开浮层）, 再关闭简化菜单
      if (peekBtn) peekBtn.focus();
      peekClose();
      return;
    }
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
    if (state.compact && dy > 0 && main.scrollTop <= 2) {
      if (e.cancelable) e.preventDefault();    // 别让浏览器触发自己的下拉刷新
      consumePull(dy, PULL_TOUCH_NEED);
      touchStartY = e.touches[0].clientY;
      return;
    }
    // 手指向下 = 内容上移 = 负方向
    setIntent(dy > 0 ? -1 : 1);
    touchStartY = e.touches[0].clientY;
    var top = main.scrollTop;
    var fe = focusedExtra();
    var changed = false;
    try { changed = state.scroll(top, { focusedExtra: fe }); } catch (err) {}
    if (changed) paint(false);
  }

  function onTouchEnd() {
    touchActive = false;
    if (state.pullAccum) settlePull();   // 没拉到阈值 → 松手回弹
  }

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
    // 点「回到顶部」是明确动作: 直接展开菜单 (滚到顶部本身不再自动展开)。
    var ch = false;
    try { ch = state.expand(); } catch (err) {}
    if (ch) paint(false);
  }

  // ---- tab change ----
  var lastTabName = null;

  function onTabChange(e) {
    var name = e && e.detail && e.detail.name;
    if (!name) return;
    if (name === lastTabName) return;
    lastTabName = name;
    settlePull();      // 切页先清掉可能残留的下拉位移
    var isMgmt = TABS.indexOf(name) !== -1;
    main.classList.toggle('management-page', isMgmt);
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
    main.classList.toggle('management-page', !!active);
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
    main.addEventListener('touchmove', onTouchMove, { passive: false });
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
