"""Static sanity checks for the management toolbar markup and assets."""
from __future__ import annotations

import shutil
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from utils.proc import run

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = PROJECT_ROOT / "web" / "static" / "index.html"

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _Element:
    __slots__ = ("tag", "attrs", "ancestors", "children", "parent")

    def __init__(self, tag, attrs, ancestors):
        self.tag = tag
        self.attrs = attrs
        self.ancestors = ancestors
        self.children = []
        self.parent = ancestors[-1] if ancestors else None

    def get(self, name, default=None):
        return self.attrs.get(name, default)

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def has_class(self, cls):
        return cls in self.classes()

    def ancestors_with_class(self, cls):
        return [a for a in self.ancestors if cls in a.classes()]

    def ancestors_with_tag(self, tag):
        return [a for a in self.ancestors if a.tag == tag]

    def has_ancestor_class(self, cls):
        return bool(self.ancestors_with_class(cls))

    def find_descendants(self, predicate):
        out = []
        for child in self.children:
            if predicate(child):
                out.append(child)
            out.extend(child.find_descendants(predicate))
        return out

    def find_by_id(self, ident):
        if self.attrs.get("id") == ident:
            return self
        for child in self.children:
            found = child.find_by_id(ident)
            if found is not None:
                return found
        return None

    def __repr__(self):
        return "<{} id={!r} class={!r}>".format(
            self.tag, self.attrs.get("id"), self.attrs.get("class")
        )


class _CollectingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Element("__root__", {}, [])
        self.stack = [self.root]
        self.elements = []
        self.id_counts = Counter()

    def handle_starttag(self, tag, attrs):
        attr_map = {}
        for key, value in attrs:
            if value is None:
                value = ""
            attr_map[key] = value
        element = _Element(tag, attr_map, list(self.stack))
        self.stack[-1].children.append(element)
        self.elements.append(element)
        ident = attr_map.get("id")
        if ident:
            self.id_counts[ident] += 1
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                return


class _Document:
    def __init__(self, html_text):
        parser = _CollectingParser()
        parser.feed(html_text)
        parser.close()
        self.root = parser.root
        self.elements = parser.elements
        self.id_counts = parser.id_counts

    def find_by_id(self, ident):
        return self.root.find_by_id(ident)

    def elements_with_class(self, cls):
        return [e for e in self.elements if e.has_class(cls)]

    def elements_with_tag(self, tag):
        return [e for e in self.elements if e.tag == tag]


_MANAGEMENT_DRAWER_STATE = "management-drawer-state.js"
_MANAGEMENT_TOOLBAR = "management-toolbar.js"
_APP_JS = "app.js"
_CONTROLLER_JS = "management-toolbar.js"


class ManagementToolbarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        cls.doc = _Document(cls.html)

    # ------------------------------------------------------------------ #
    def _toolbar_for(self, name):
        matches = [
            e for e in self.doc.elements
            if e.get("data-management") == name
            and e.has_class("management-toolbar")
        ]
        self.assertEqual(len(matches), 1, "expected one toolbar %r" % name)
        return matches[0]

    def _assert_unique_ids(self):
        dups = {k: v for k, v in self.doc.id_counts.items() if v > 1}
        self.assertEqual(dups, {}, "duplicate ids: %r" % dups)

    def _assert_inside(self, element, ancestor_class, message):
        self.assertTrue(
            element.has_ancestor_class(ancestor_class),
            "%r must be inside .%s (%s)" % (element, ancestor_class, message),
        )

    # ------------------------------------------------------------------ #
    def test_toolbars_and_unique_ids(self):
        self._assert_unique_ids()

        toolbars = [
            e for e in self.doc.elements
            if e.has_class("management-toolbar")
            and e.get("data-management") is not None
        ]
        self.assertEqual(
            sorted(t.get("data-management") for t in toolbars),
            ["authors", "library", "wallpapers"],
        )

        for name in ("library", "authors", "wallpapers"):
            toolbar = self._toolbar_for(name)
            tab = self.doc.find_by_id("tab-" + name)
            self.assertIsNotNone(tab, "missing #tab-%s" % name)
            self.assertTrue(
                any(a is tab for a in toolbar.ancestors),
                "toolbar %r must live inside #tab-%s" % (name, name),
            )
            toggles = toolbar.find_descendants(
                lambda e: e.has_class("management-toggle")
            )
            self.assertEqual(
                len(toggles), 1, "toolbar %r needs one .management-toggle" % name
            )
            toggle = toggles[0]
            self.assertEqual(toggle.get("aria-expanded"), "true")
            self.assertEqual(toggle.get("type"), "button")

    # ------------------------------------------------------------------ #
    def test_search_inputs_and_primary(self):
        self._assert_unique_ids()
        mappings = {
            "lib-search-input": "library",
            "wf-search": "wallpapers",
            "authors-search-input": "authors",
        }
        for ident, name in mappings.items():
            element = self.doc.find_by_id(ident)
            self.assertIsNotNone(element, "missing #%s" % ident)
            toolbar = self._toolbar_for(name)
            self.assertTrue(
                any(a is toolbar for a in element.ancestors),
                "#%s must be inside %s toolbar" % (ident, name),
            )
            self._assert_inside(
                element, "management-primary",
                "#%s needs .management-primary" % ident,
            )
            self.assertEqual(
                self.doc.id_counts[ident], 1, "#%s must be unique" % ident
            )

    # ------------------------------------------------------------------ #
    def test_library_and_wallpaper_selections(self):
        self._assert_unique_ids()

        library = self._toolbar_for("library")
        library_selection = [
            e for e in library.find_descendants(
                lambda el: el.has_class("management-selection")
            )
        ]
        self.assertEqual(len(library_selection), 1)
        library_selection = library_selection[0]

        lib_ids = [
            "lib-batch-edit", "lib-batch-addcart", "lib-batch-repair",
            "lib-batch-merge", "lib-batch-split", "lib-batch-srcsearch",
            "lib-batch-del", "lib-selection-info", "lib-total",
        ]
        for ident in lib_ids:
            element = self.doc.find_by_id(ident)
            self.assertIsNotNone(element, "missing #%s" % ident)
            self.assertTrue(
                any(a is library_selection for a in element.ancestors),
                "#%s must be inside library .management-selection" % ident,
            )
        menu = self.doc.find_by_id("lib-redownload-menu")
        self.assertIsNotNone(menu, "missing #lib-redownload-menu")
        self.assertTrue(
            menu.has_class("lib-redownload-menu"),
            "#lib-redownload-menu must be .lib-redownload-menu",
        )
        for ident in ("lib-batch-addcart", "lib-batch-repair"):
            element = self.doc.find_by_id(ident)
            self._assert_inside(
                element, "lib-redownload-menu",
                "#%s must be inside .lib-redownload-menu" % ident,
            )
            self._assert_inside(
                element, "management-selection",
                "#%s must be inside .management-selection" % ident,
            )

        wallpapers = self._toolbar_for("wallpapers")
        wf_selection = wallpapers.find_descendants(
            lambda el: el.has_class("management-selection")
        )
        self.assertEqual(len(wf_selection), 1)
        wf_selection = wf_selection[0]
        for ident in ("wf-batch", "wf-recycle", "wf-sel-count"):
            element = self.doc.find_by_id(ident)
            self.assertIsNotNone(element, "missing #%s" % ident)
            self.assertTrue(
                any(a is wf_selection for a in element.ancestors),
                "#%s must be inside wallpapers .management-selection" % ident,
            )

    # ------------------------------------------------------------------ #
    def test_extra_cards_and_results_not_absorbed(self):
        self._assert_unique_ids()

        # 统计卡 / 当前壁纸卡片已改为随列表滚走 (2026-11-17): 不再吸在顶部,
        # 否则展开态会永久占掉一大块可视高度。
        outside = {
            "library": ["wall-card", "lib-stats"],
            "wallpapers": ["wf-stats"],
        }
        for name, idents in outside.items():
            for ident in idents:
                element = self.doc.find_by_id(ident)
                self.assertIsNotNone(element, "missing #%s" % ident)
                self.assertFalse(
                    element.has_ancestor_class("management-toolbar"),
                    "#%s 应随列表滚走, 不能吸在 %s 工具栏里" % (ident, name),
                )

        # 作者页的筛选仍放在展开区
        toolbar = self._toolbar_for("authors")
        extra = toolbar.find_descendants(
            lambda el: el.has_class("management-extra")
        )
        self.assertTrue(extra, "authors toolbar needs .management-extra")
        for ident in ("authors-f-status", "authors-f-source"):
            element = self.doc.find_by_id(ident)
            self.assertIsNotNone(element, "missing #%s" % ident)
            self.assertTrue(
                any(a in extra for a in element.ancestors),
                "#%s must be inside authors .management-extra" % ident,
            )

        for ident in ("lib-result", "authors-result", "wf-result",
                      "lib-pager-bottom", "authors-pager-bottom"):
            element = self.doc.find_by_id(ident)
            self.assertIsNotNone(element, "missing #%s" % ident)
            self.assertFalse(
                element.has_ancestor_class("management-toolbar"),
                "#%s must not be inside .management-toolbar" % ident,
            )

        # 顶部总数+页码必须留在吸顶工具栏里: 收起菜单后仍能看到并翻页 (2026-11-17)
        for ident in ("lib-pager-top", "authors-pager-top", "wf-pager"):
            element = self.doc.find_by_id(ident)
            self.assertIsNotNone(element, "missing #%s" % ident)
            self.assertTrue(
                element.has_ancestor_class("management-toolbar"),
                "#%s must stay inside .management-toolbar" % ident,
            )

    # ------------------------------------------------------------------ #
    def test_peek_trigger_and_collapsed_menu(self):
        """收起态只留总数与页码; 标签栏叠三角悬停滑出简化菜单。"""
        peek = self.doc.find_by_id("mgmt-peek")
        self.assertIsNotNone(peek, "missing #mgmt-peek")
        self.assertTrue(peek.has_ancestor_class("tabs"), "#mgmt-peek must live in .tabs")
        self.assertIsNotNone(peek.get("hidden"), "#mgmt-peek 默认隐藏")

        css = (PROJECT_ROOT / "web" / "static" / "management-toolbar.css").read_text(encoding="utf-8")
        self.assertIn("main.management-compact .management-main", css)
        self.assertIn("main.management-compact.management-peek .management-main", css)

        js = (PROJECT_ROOT / "web" / "static" / "management-toolbar.js").read_text(encoding="utf-8")
        for token in ("function peekOpen", "function peekClose", "function paintPeek"):
            self.assertIn(token, js, token)

        for name in ("library", "authors", "wallpapers"):
            toolbar = self._toolbar_for(name)
            mains = toolbar.find_descendants(
                lambda e: e.has_class("management-main")
            )
            self.assertEqual(
                len(mains), 1, "toolbar %r needs one .management-main" % name
            )

    # ------------------------------------------------------------------ #
    def test_asset_versions_and_script_order(self):
        assets = self.doc.elements_with_tag("script") + self.doc.elements_with_tag("link")
        versioned = []
        for asset in assets:
            src = asset.get("src") or asset.get("href") or ""
            if asset.tag == "script" and not src:
                continue
            if asset.tag == "link" and (asset.get("rel") or "") != "stylesheet":
                continue
            if "?" not in src:
                continue
            versioned.append((asset, src))

        self.assertTrue(versioned, "expected versioned static assets")
        versions = Counter()
        for _asset, src in versioned:
            query = src.split("?", 1)[1]
            for chunk in query.split("&"):
                if chunk.startswith("v="):
                    versions[chunk[2:]] += 1
                    break
        self.assertEqual(
            len(versions), 1, "all static assets must share one ?v= value, got %r" % versions
        )

        script_srcs = [
            e.get("src") for e in self.doc.elements_with_tag("script")
            if (e.get("src") or "").endswith(".js")
            or ".js?v=" in (e.get("src") or "")
        ]
        script_srcs = [
            s for s in script_srcs
            if s and (s.endswith(".js") or ".js?" in s)
        ]

        def index_of(name):
            for i, src in enumerate(script_srcs):
                base = src.split("?", 1)[0].rsplit("/", 1)[-1]
                if base == name:
                    return i
            self.fail("script %r not found" % name)

        self.assertLess(
            index_of(_MANAGEMENT_DRAWER_STATE),
            index_of(_MANAGEMENT_TOOLBAR),
            "management-drawer-state.js must load before management-toolbar.js",
        )
        self.assertLess(
            index_of(_APP_JS),
            index_of(_CONTROLLER_JS),
            "app.js must load before controller.js",
        )

    # ------------------------------------------------------------------ #
    @unittest.skipUnless(shutil.which("node"), "node is not available")
    def test_node_management_drawer_state(self):
        script = PROJECT_ROOT / "autotest" / "test_management_drawer_state.cjs"
        completed = run(
            ["node", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        details = "rc=%s\nstdout:\n%s\nstderr:\n%s" % (
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )
        self.assertEqual(completed.returncode, 0, details)


if __name__ == "__main__":
    unittest.main()
