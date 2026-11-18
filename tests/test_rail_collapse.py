# -*- coding: utf-8 -*-
"""左侧导航收起：一条 208px 的竖栏收成 64px 的图标条。

这条需求最容易做成「截图看着对、用起来是坏的」，所以分四组钉：

  * **HTML 侧**：按钮必须在、而且必须一直可点——收起之后它要是跟着一起藏掉，就
    再没有任何回到展开态的入口（只能开开发者工具改回去）。它也不能混进 `.tab`
    那一套：`RailTabIconTestCase`（tests/test_ui_contract.py:6601）里那条「每颗
    tab 恰一枚 16px 图标」是按 `class="tab..."` 抓的，按钮蹭进去会让那条不变式
    从此核不到页签，而用例仍然全绿。
  * **脚本侧**：状态只经 `is-collapsed` 一个类名下发；偏好读/写各带 try/catch
    （隐私模式、企业策略下 `localStorage` **读取本身就会抛**，漏一个 try 就是
    整页起不来）；而且**不许复用 `is-folded`** —— 那个类名被
    `test_folding_never_touches_the_draggable_widths` 抓着「折叠不许写宽度」，
    收起态恰恰要写宽度，混用会把那条不变式直接破坏掉。
  * **样式侧**：宽度来自 `--rail` / `--rail-min` 两档；收起态里的三样（字标 /
    段标签 / 账号区）必须真的藏掉，否则「收起了但字还在」。
  * **档位侧**：整套只在 `min-width: 901px` 生效——900px 以下这一栏是顶部横条，
    收窄没有意义，按钮在那里必须藏掉（否则是颗点了没反应的按钮）。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")


def _read(name):
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as handle:
        return handle.read()


class RailCollapseContractTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = _read("index.html")
        cls.js = _read("rail.js")
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

    @staticmethod
    def _rules(css):
        return [(" ".join(sel.split()), " ".join(body.split()))
                for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)]

    def _rule(self, selector):
        for sel, body in self._rules(self._css):
            if sel == selector:
                return body
        return ""

    @staticmethod
    def _decl(rule, prop):
        match = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), rule)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _media_blocks(css):
        """每个 @media 的 (条件, 块内文本)。按花括号配对切，块里还有一层规则。"""
        blocks = []
        for match in re.finditer(r"@media[^{]*", css):
            start = match.end()
            depth, index = 0, start
            while index < len(css):
                if css[index] == "{":
                    depth += 1
                elif css[index] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            blocks.append((match.group(0).strip(), css[start + 1:index]))
        return blocks

    # ------------------------------------------------------------- HTML 侧

    def test_the_aside_carries_an_id_and_the_button_sits_in_the_foot(self):
        """按钮写死在 HTML 里，而且排在账号区**之前**。

        id 必须静态：`rail.js` 用 `getElementById` 找它，而启动链那条
        （`InitTimeSelectorTestCase`）只放行 index.html 里写死的 id。
        按钮排在账号区之前，是因为收起态要把账号区整块藏掉——排在它后面的话，
        那一块先消失、按钮才露出来，视觉上像"按钮跟着一起没了"。
        """
        self.assertIn('<aside class="rail" id="rail">', self.html,
                      "左导航没有 id：rail.js 找不到它，切类名会静默落空")
        self.assertIn('id="rail-collapse"', self.html,
                      "收起按钮不在 HTML 里：收起之后没有任何回到展开态的入口")
        foot = self.html[self.html.index('<div class="rail-foot">'):]
        self.assertLess(foot.index('id="rail-collapse"'),
                        foot.index('<div class="rail-user">'),
                        "按钮跑到了账号区下面：收起之后那一片会先被藏掉它才出现")
        self.assertIn('<script src="rail.js"></script>', self.html,
                      "rail.js 没有挂进页面")

    def test_the_button_does_not_pretend_to_be_a_tab(self):
        """`class="tab ..."` 是页签的入口，收起按钮不是页签。

        蹭进去的代价是「每颗 tab 恰一枚 tab-ico」那条断言从此核的是别的东西。
        """
        tag = re.search(r'<button[^>]*id="rail-collapse"[^>]*>', self.html).group(0)
        self.assertNotIn('class="tab', tag, "收起按钮挂了 .tab 那一套样式")
        tail = self.html.split('id="rail-collapse"')[1][:600]
        self.assertNotIn("tab-ico", tail,
                         "收起按钮的图标用了 tab-ico：页签图标数会被数成 16 枚")
        self.assertIn('aria-controls="rail"', tag, "按钮没有指明它控制哪一块")
        self.assertIn('aria-expanded="true"', tag,
                      "初始 aria-expanded 不在 HTML 上：脚本晚一步时读屏读不到状态")

    # ------------------------------------------------------------- 脚本侧

    @staticmethod
    def _strip_js_comments(source):
        """注释换成等量空格（与 test_ui_contract._strip_js_comments 同一套）。

        文件头那段说明里提到 is-folded / is-collapsed 是**刻意**的：它是这条
        不变式的来历。查"用了哪个类名"必须只看代码，否则一句文档就能把断言顶红。
        """
        source = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                        source, flags=re.S)
        return re.sub(r"(?m)//.*$", lambda m: " " * len(m.group(0)), source)

    def test_the_state_travels_as_one_class_on_the_rail(self):
        self.assertIn("'pentest.rail.v1'", self.js, "偏好键没有版本号")
        code = self._strip_js_comments(self.js)
        self.assertIn("classList.toggle('is-collapsed'", code,
                      "收起态不是经 is-collapsed 一个类名下发的")
        self.assertNotIn("is-folded", code,
                         "用了 is-folded：收起态要写宽度，而那个类名被"
                         "「折叠不许写宽度」那条不变式抓着")
        self.assertRegex(self.js, r"try\s*\{\s*return window\.localStorage",
                         "读偏好没有 try：隐私模式下读取本身就抛，整页会起不来")
        self.assertRegex(self.js, r"try\s*\{\s*window\.localStorage\.setItem",
                         "写偏好没有 try")
        self.assertIn("window.PentestRail", self.js, "没有对外暴露 PentestRail")
        self.assertIn("DOMContentLoaded", self.js,
                      "这一条不经过 boot.js 的启动门，必须自启动")

    def test_the_button_keeps_its_name_in_step_with_the_state(self):
        """收起之后按钮只剩图标，名字必须跟着走：读屏听到的动作要与按下去的一致。"""
        self.assertIn("button.setAttribute('aria-expanded'", self.js)
        self.assertIn("展开左侧导航", self.js, "收起态没有给出「展开」这个动作名")
        self.assertIn("function paintTitles()", self.js,
                      "页签名收起后没有去处：图标条上没有 title 就读不出是哪个页")

    # ------------------------------------------------------------- 样式侧

    def test_the_two_widths_come_from_tokens(self):
        self.assertIn("--rail: 208px;", self._css, "常规宽度没有落成令牌")
        self.assertIn("--rail-min: 64px;", self._css, "收起宽度没有落成令牌")
        self.assertEqual(self._decl(self._rule(".rail"), "width"), "var(--rail)",
                         ".rail 的宽度没有走令牌：收起态就只能另写一条硬宽度")

    def test_the_collapsed_state_hides_the_three_text_blocks(self):
        """字标 / 段标签 / 账号区必须真的藏掉——64px 里它们没有第二个形态。

        页签的文字用 `font-size: 0` 收：它们不是 span，是按钮里的裸文本节点，
        没有类名可挂；而图标自己有固定的 16px 宽高，不跟着塌。
        """
        for part in (".rail-brand", ".rail-group-label", ".rail-user",
                     ".rail-collapse-text"):
            rule = self._rule(".rail.is-collapsed " + part)
            self.assertEqual(self._decl(rule, "display"), "none",
                             "收起态没有藏掉 %s" % part)
        tab = self._rule(".rail.is-collapsed .tab")
        self.assertEqual(self._decl(tab, "font-size"), "0",
                         "页签名没有被收掉：64px 里会折成一列乱码")
        self.assertNotEqual(self._decl(tab, "display"), "none",
                            "把 .tab 整颗藏了：图标也不见了，导航就没了")

    def test_the_brand_rule_still_only_changes_the_size(self):
        """收起态藏字标用的是 `display: none`，**不是**往 `.rail-brand` 里塞字号。

        `test_the_rail_changes_only_the_size`（tests/test_ui_contract.py:6108）把
        `.rail-brand*` 的全部规则体拼起来查，出现 font-size / letter-spacing /
        font-weight 就红——那等于换了等宽那个等式的解。
        """
        block = self._rule(".rail.is-collapsed .rail-brand")
        self.assertIn("display: none", block)
        for forbidden in ("font-size", "letter-spacing", "font-weight"):
            self.assertNotIn(forbidden, block,
                             "收起态在 .rail-brand 上覆盖了 %s" % forbidden)

    # ------------------------------------------------------------- 档位侧

    def test_the_collapse_lives_only_in_the_wide_band(self):
        """整套收起态只在 ≥901px 生效；横条档里按钮直接藏掉。

        900px 及以下导航折成顶部横条（`flex-direction: row` + 横向滚动），
        「收窄竖栏」在这个形态里没有意义；按钮留着就是一颗点了没反应的按钮。
        """
        wide = [block for cond, block in self._media_blocks(self._css)
                if "min-width: 901px" in cond]
        self.assertTrue(wide, "找不到 min-width: 901px 那一档")
        joined = " ".join(wide)
        self.assertIn(".rail.is-collapsed", joined,
                      "收起态没有落在宽屏那一档里")
        self.assertIn("width: var(--rail-min)", joined, "收起宽度没有走令牌")
        self.assertIn("overflow-x: hidden", joined, "64px 下没有挡住横向溢出")
        narrow = [block for cond, block in self._media_blocks(self._css)
                  if "max-width: 900px" in cond]
        self.assertTrue(narrow, "找不到 900px 那一档")
        self.assertIn(".rail-collapse { display: none; }", " ".join(narrow),
                      "横条档里按钮还露着：点了什么都不会发生")

    def test_the_button_is_as_tall_as_the_two_beside_it(self):
        """栏脚三颗按钮等高。收起态只剩 14px 的图标，不钉高度会矮 4px。"""
        rule = self._rule(".rail-collapse")
        self.assertEqual(self._decl(rule, "min-height"), "30px")
        font = self._decl(rule, "font")
        self.assertRegex(font, r"/\s*1\.5\s",
                         "font 简写里没带行高：这颗按钮会比旁边两颗高一两个像素")


if __name__ == "__main__":
    unittest.main()
