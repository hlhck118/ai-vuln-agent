# -*- coding: utf-8 -*-
"""主题（外观）的契约核对（静态检查，不需要浏览器）。

主题功能的失败模式几乎全是"只在某一套主题下才出现"，靠人眼逐个点开界面去试是查不全的：

  * 某套主题漏了几个令牌 → 它沿用上一套主题的取值，于是局部文字在某套主题下看不清；
  * 令牌名写错一个字母 → 那条声明静默作废，而 CSS 本身不会报错；
  * 一段样式散写颜色字面量 → 那一段在五套主题里有四套是对的，很难发现漏掉的那一套；
  * 内联脚本与 theme.js 的存储键 / 默认值对不上 → "上次选的主题没记住"，两处代码各自看都正常；
  * 预览卡片上的色值与 CSS 漂移 → 预览和点下去的样子不一样，这是最没人信的一类 bug。

**对比度是算出来的，不是看出来的**——这是加主题唯一没法靠截图把关的地方，
所以本模块自带一个最小的 WCAG 计算器，逐主题核对正文与次级文字。
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB_DIR = os.path.join(ROOT, "web")

# 主题清单。**必须与 web/theme.js 的 THEMES 一致**——几条断言会反过来核对这一点，
# 所以它在这里是"期望值"，不是"抄一份"。
THEME_IDS = ["glass", "midnight", "paper", "terminal", "contrast"]
DEFAULT_THEME = "glass"
# 午夜主题的全部取值就是 :root 那一套（改版前的深色外观），它不覆盖任何令牌。
ROOT_IS = "midnight"

# 重点色。「跟随主题」是**默认档，而且刻意没有对应的 CSS 块**——没有块就是没有覆盖；
# 给它写一个"把主题自己的值再抄一遍"的块，会在主题自身换色时把旧值悄悄钉住。
ACCENT_IDS = ["theme", "blue", "indigo", "cyan", "purple", "magenta"]
DEFAULT_ACCENT = "theme"

# 磨砂档位。同样，默认档没有块——而且它与重点色的默认档同名（`theme`）：
# 两处是一个意思，「跟随主题」。**不叫 system**：它跟的不是操作系统，是当前那套主题。
FROST_IDS = ["theme", "off", "soft", "mid", "strong"]
DEFAULT_FROST = "theme"
# 档位允许动的令牌。写死成白名单而不是"看着差不多"：磨砂是**材质**，它一旦伸手
# 去改配色（比如顺手把 --panel 调深一点），五套主题里就有四套会静默变形。
FROST_TOKENS = {"--glass-blur", "--glass-card-blur", "--glass-sat", "--glass-edge",
                "--overlay-blur"}

# 重点色允许动与**禁止**动的令牌。禁的那一批是"读数"：严重度四档与 --ok 描述的是
# 漏洞有多重，节点类型与图谱边描述的是"这是什么"。它们一旦跟着外观走，
# 红色徽章到底是"选中"还是"严重"就分不出来了——这正是本表开头那条原则。
ACCENT_TOKENS = {"--accent", "--accent-dim", "--accent-soft", "--focus", "--focus-ring",
                 "--text-accent", "--brand-fill", "--on-accent-deep"}
ACCENT_FORBIDDEN = {"--sev-critical", "--sev-high", "--sev-medium", "--sev-low", "--ok",
                    "--sev-info", "--danger", "--text-danger", "--text-ok", "--text-warn",
                    "--kind-goal", "--kind-intent", "--kind-fact", "--kind-finding",
                    "--kind-asset", "--edge-spawns", "--edge-yields", "--edge-derived",
                    "--edge-proves", "--edge-parent", "--text", "--text-dim", "--muted",
                    "--bg", "--panel", "--panel-2", "--panel-3", "--border"}


def _read(name: str) -> str:
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


class ThemeContractTestCase(unittest.TestCase):
    """五套主题共用一套令牌，任何一处散写颜色都是某套主题的破口。"""

    @classmethod
    def setUpClass(cls):
        cls._css_raw = _read("style.css")
        cls._css = _strip_comments(cls._css_raw)
        cls._html = _read("index.html")
        cls._js = _read("theme.js")
        cls._settings = _read("settings.js")
        cls._app = _read("app.js")
        cls._blocks = cls._top_level_blocks(cls._css_raw)

    # --------------------------------------------------------------- 工具

    @staticmethod
    def _top_level_blocks(css: str):
        r"""花括号深度为 0 的规则 (选择器, 规则体)。

        不能直接用 `re.findall(r"([^{}]*)\{([^{}]*)\}")`：它把 @media 里嵌套的规则
        也当成顶层规则抓出来，于是 `@media (prefers-reduced-transparency)` 里那份
        玻璃回退会被误读成"玻璃主题的定义"——那份是**偏好开关下的**取值，
        不是主题本身的取值，混进来会让对比度核对跑在两套不同的数上。
        """
        text = _strip_comments(css)
        out, i = [], 0
        while i < len(text):
            j = text.find("{", i)
            if j < 0:
                break
            selector = text[i:j].strip()
            depth, k = 0, j
            while k < len(text):
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if not selector.startswith("@"):
                out.append((selector, text[j + 1:k]))
            i = k + 1
        return out

    @staticmethod
    def _tokens(body: str) -> dict:
        return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+)", body))

    def _root_tokens(self) -> dict:
        for sel, body in self._blocks:
            if sel == ":root":
                return self._tokens(body)
        raise AssertionError("找不到 :root 令牌块")

    def _theme_block(self, name: str) -> str:
        """:root[data-theme="x"] 的规则体。**只取顶层**——见 _top_level_blocks。"""
        selector = ':root[data-theme="%s"]' % name
        for sel, body in self._blocks:
            if sel == selector:
                return body
        return ""

    def _theme_tokens(self, name: str) -> dict:
        """某套主题的**有效**令牌 = :root 的取值被该主题块覆盖之后的结果。

        必须合并而不是只看主题块：午夜就是不覆盖任何令牌的那一套（它就是 :root），
        只读主题块的话它会得到一张空表，后面每一条断言都会在它身上假失败。
        """
        tokens = self._root_tokens()
        tokens.update(self._tokens(self._theme_block(name)))
        return tokens

    def _block_by_selector(self, want: str) -> str:
        """按**归一化之后的**选择器找规则体。

        归一化是必须的：亮色档的选择器是两行（玻璃 + 纸感），而 _top_level_blocks
        取到的是含换行的原文。直接逐字比对会在换行缩进上失败，而那种失败看起来
        像"块不存在"，会被误读成"忘了写"。
        """
        want = re.sub(r"\s+", " ", want).strip()
        for sel, body in self._blocks:
            if re.sub(r"\s+", " ", sel).strip() == want:
                return body
        return ""

    def _accent_dark_block(self, name: str) -> str:
        return self._block_by_selector(':root[data-accent="%s"]' % name)

    def _accent_light_selector(self, name: str) -> str:
        return ", ".join(':root[data-theme="%s"][data-accent="%s"]' % (t, name)
                         for t in self._light_themes())

    def _accent_light_block(self, name: str) -> str:
        return self._block_by_selector(self._accent_light_selector(name))

    def _light_themes(self) -> list:
        """哪几套主题是亮色。**由各主题自己的 color-scheme 决定**，不另抄一份名单
        —— 抄一份就会在新增主题时漏掉，而漏掉的症状是"那一套主题下重点色还是旧的"。

        用正则而不是 `assertIn("color-scheme: light")`：令牌块里的取值是**对齐排过**的
        （`color-scheme:        light;`），冒号后面有多个空格。逐字比对会漏掉它，
        而漏掉的表现是"这套主题不算亮色"，接着所有亮色族断言都会静默地少核一套。
        """
        return [name for name in THEME_IDS
                if re.search(r"color-scheme\s*:\s*light", self._theme_block(name))]

    def _accent_tokens(self, theme: str, accent: str) -> dict:
        """某套主题 + 某一档重点色的**有效**令牌。"""
        tokens = self._theme_tokens(theme)
        if accent == DEFAULT_ACCENT:
            return tokens
        tokens.update(self._tokens(self._accent_dark_block(accent)))
        if theme in self._light_themes():
            tokens.update(self._tokens(self._accent_light_block(accent)))
        return tokens

    # ----------------------------------------------------- WCAG 最小计算器

    @staticmethod
    def _color(value: str, tokens: dict, depth: int = 0) -> tuple:
        """令牌取值 → (r, g, b, a)。支持 hex / rgb() / rgba() / var() 链。"""
        text = (value or "").strip()
        if text == "transparent":              # 非玻璃主题的环境光就是这个值
            return (0.0, 0.0, 0.0, 0.0)
        match = re.fullmatch(r"var\(\s*(--[a-z0-9-]+)\s*\)", text)
        if match:
            name = match.group(1)
            if depth > 8 or name not in tokens:
                raise AssertionError("令牌解析不下去：%s" % value)
            return ThemeContractTestCase._color(tokens[name], tokens, depth + 1)
        match = re.fullmatch(r"#([0-9a-fA-F]{6})", text)
        if match:
            num = int(match.group(1), 16)
            return ((num >> 16) & 255, (num >> 8) & 255, num & 255, 1.0)
        match = re.fullmatch(
            r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", text)
        if match:
            return (float(match.group(1)), float(match.group(2)), float(match.group(3)),
                    float(match.group(4)) if match.group(4) is not None else 1.0)
        raise AssertionError("无法解析的颜色值：%s" % value)

    @staticmethod
    def _over(fg: tuple, bg: tuple) -> tuple:
        alpha = fg[3]
        return tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3)) + (1.0,)

    @staticmethod
    def _luminance(color: tuple) -> float:
        def channel(value):
            value = value / 255.0
            return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        return (0.2126 * channel(color[0]) + 0.7152 * channel(color[1])
                + 0.0722 * channel(color[2]))

    @staticmethod
    def _ratio(fg: tuple, bg: tuple) -> float:
        hi = max(ThemeContractTestCase._luminance(fg), ThemeContractTestCase._luminance(bg))
        lo = min(ThemeContractTestCase._luminance(fg), ThemeContractTestCase._luminance(bg))
        return (hi + 0.05) / (lo + 0.05)

    def _worst_backgrounds(self, tokens: dict) -> list:
        """正文可能落在的每一种底，含**环境光晕的最亮/最暗处**。

        玻璃主题的表面是半透明的，对比度因此随背后那层色晕浮动。只用 --bg 去算会
        漏掉真正的最差情况——而"某一套主题下看着有点糊"恰恰就是这么来的。
        """
        bg = self._color(tokens["--bg"], tokens)
        out = [bg]
        for key in ("--ambient-1", "--ambient-2", "--ambient-3"):
            if key in tokens:
                ambient = self._color(tokens[key], tokens)
                if ambient[3] > 0:
                    out.append(self._over(ambient, bg))
        return out

    # ------------------------------------------------------------ 令牌

    def test_theme_blocks_only_override_tokens_that_exist(self):
        """主题块里写错的令牌名必须当场发现。

        写错一个字母不会报错：那条声明静默作废，该主题于是**沿用 :root 的值**。
        症状是"某一套主题下某个地方不太对"，而两处代码看上去都正常——
        典型的能跑但错，只有静态比对令牌名才抓得住。
        """
        known = set(self._root_tokens())
        for name in THEME_IDS:
            for token in self._tokens(self._theme_block(name)):
                self.assertIn(token, known,
                              "%s 主题覆盖了 :root 里不存在的令牌 %s（该声明会静默作废）"
                              % (name, token))

    def test_every_theme_restates_the_legibility_core(self):
        """除午夜外，每套主题都必须重新给出底、面、正文、次级文字。

        这几项决定"看不看得清"。只改一个重点色、把底和文字留给 :root 的主题，
        在亮色系里会直接变成白底白字——而它看起来是一套"写了一半"的主题，
        不像错误。
        """
        core = ("--bg", "--panel", "--panel-2", "--text", "--text-dim", "--muted", "--border")
        for name in THEME_IDS:
            if name == ROOT_IS:
                continue                       # 它的定义就是 :root 本身
            declared = self._tokens(self._theme_block(name))
            for token in core:
                self.assertIn(token, declared,
                              "%s 主题没有重新给出 %s（会沿用 :root 的深色值）" % (name, token))

    def test_every_theme_asserts_a_color_scheme(self):
        """原生控件（滚动条、多选框、下拉）的外观跟着 color-scheme 走。

        它写在 body 上时是全局的，加主题之后必须由每个主题各自断言一次——否则切到
        亮色主题后这些部件仍按深色绘制，卡片里露出黑色方块（与改版前的毛病正好反向）。
        纸张主题必须是 light，这是它唯一不能写错的一处。
        """
        for name in THEME_IDS:
            block = self._theme_block(name)
            self.assertIn("color-scheme", block, "%s 主题没有断言 color-scheme" % name)
        self.assertIn("color-scheme: light", self._theme_block("paper"))
        self.assertNotIn("color-scheme: dark", self._theme_block("paper"))
        # body 上不得再写死一份：它优先级高于 :root，会把所有主题一起按住。
        self.assertNotIn("color-scheme", self._css[self._css.index("body {"):
                                                    self._css.index("}", self._css.index("body {"))],
                         "body 上不得再写死 color-scheme")

    # ----------------------------------------------------------- 对比度

    def test_body_text_stays_readable_in_every_theme(self):
        """正文对每套主题的每一种底都必须过 AA，正文本身按 AAA 要求。

        这是"加主题"唯一不能靠肉眼把关的地方：换底色不会报错，只会让文字在某套主题下
        变得要眯眼，而使用者会以为是自己屏幕的问题。
        """
        minimum = {"--text": 7.0, "--text-dim": 4.5, "--muted": 4.5}
        for name in THEME_IDS:
            tokens = self._theme_tokens(name)
            for token, need in minimum.items():
                fg = self._color(tokens[token], tokens)
                worst, worst_at = 101.0, ""
                for base in self._worst_backgrounds(tokens):
                    surfaces = [("--bg", base),
                                ("--panel", self._over(self._color(tokens["--panel"], tokens), base)),
                                ("--panel-3", self._over(self._color(tokens["--panel-3"], tokens), base))]
                    for label, bg in surfaces:
                        value = self._ratio(fg, bg)
                        if value < worst:
                            worst, worst_at = value, label
                self.assertGreaterEqual(
                    worst, need,
                    "%s 主题：%s 在 %s 上只有 %.2f:1（要求 %.1f:1）"
                    % (name, token, worst_at, worst, need))

    def test_the_two_accent_foregrounds_are_not_the_same_token(self):
        """亮填充配深字、深填充配亮字，是两件事。

        合并成一个令牌会让其中一半读不出来：深色主题里 --accent 是亮蓝（要深字）、
        --accent-dim 是深蓝（要亮字），到了亮色主题两者的明暗关系整个翻过来。
        改版前这段正是靠两个不同字面量各管一边，只是从没写明白。
        """
        for name in THEME_IDS:
            tokens = self._theme_tokens(name)
            self.assertIn("--on-accent", tokens, "%s 缺 --on-accent" % name)
            self.assertIn("--on-accent-deep", tokens, "%s 缺 --on-accent-deep" % name)
            for fill, ink in (("--accent", "--on-accent"), ("--accent-dim", "--on-accent-deep")):
                ratio = self._ratio(self._color(tokens[ink], tokens),
                                    self._color(tokens[fill], tokens))
                self.assertGreaterEqual(
                    ratio, 4.5,
                    "%s 主题：%s 在 %s 上只有 %.2f:1" % (name, ink, fill, ratio))

    def test_badge_foreground_is_a_separate_token_from_the_muted_text(self):
        """--muted 同时是"说明文字色"和"徽章底色"，亮色主题下这两个要求相反：
        作为文字要够深才读得清，作为底色要够浅才压得住前景字。所以前景必须另给令牌。"""
        tokens = self._root_tokens()
        self.assertEqual(tokens.get("--muted-fill"), "var(--muted)",
                         ":root 里 --muted-fill 的默认值应当就是 var(--muted)")
        for name in THEME_IDS:
            theme = self._theme_tokens(name)
            self.assertIn("--on-muted", theme, "%s 缺 --on-muted" % name)
            ratio = self._ratio(self._color(theme["--on-muted"], theme),
                                self._color(theme["--muted-fill"], theme))
            self.assertGreaterEqual(ratio, 4.5,
                                    "%s 主题：徽章前景只有 %.2f:1" % (name, ratio))

    # --------------------------------------------------- 没有漏网的颜色

    def test_no_color_literal_survives_outside_token_blocks(self):
        """令牌区之外不得再出现颜色字面量。

        这是"主题能不能一直对"的总闸。散写一个 #fff，它在五套主题里有四套是对的，
        漏掉的那一套才会露馅——而那时已经没人记得这个文件加过主题。
        令牌区 = :root + 各 :root[data-theme]（含 @media 里的回退），它们就是颜色的
        **唯一**去处。
        """
        offenders = []
        for sel, body in self._top_level_blocks(self._css_raw):
            if sel.startswith(":root"):
                continue
            for match in re.finditer(r"#[0-9a-fA-F]{3,8}\b|rgba?\(\s*[\d.]+\s*,", body):
                offenders.append("%s → %s" % (sel.split(",")[0].strip(), match.group(0)))
        # @media 里那份玻璃回退也是令牌区，单独放行
        offenders = [item for item in offenders
                     if not item.startswith("@media")]
        self.assertEqual(offenders, [],
                         "规则区散写了颜色字面量（该段在部分主题下会沿用旧配色）：%s" % offenders)

    # ----------------------------------------------- 内联脚本与 theme.js

    def _module_const(self, name: str) -> str:
        """theme.js 里的一个字符串常量。\\b 是必要的：`ATTR` 与 `ACCENT_ATTR`、
        `DEFAULT_ID` 与 `DEFAULT_ACCENT` 互为前缀，不加词边界会取到隔壁那一个，
        而取错了之后**每一条断言都还是绿的**——只是核的是别的东西。"""
        match = re.search(r"\b%s\s*=\s*'([^']+)'" % name, self._js)
        self.assertTrue(match, "theme.js 缺少常量 %s" % name)
        return match.group(1)

    def _module_ids(self, name: str) -> list:
        """theme.js 里某个清单的 id 列表（THEMES / ACCENTS / FROSTS）。"""
        match = re.search(r"const %s = \[(.*?)\n  \];" % name, self._js, flags=re.S)
        self.assertTrue(match, "theme.js 里找不到清单 %s" % name)
        return re.findall(r"id:\s*'([^']+)'", match.group(1))

    def test_inline_script_agrees_with_the_module_on_all_three_axes(self):
        """首屏那段内联脚本与 theme.js 的常量必须完全一致。

        内联脚本**不能**引用 theme.js（那要多一次网络往返，多出来的那一帧就是整页
        白闪），所以这里必然有两份定义。对不上的症状是"上次选的那项没记住"，
        而两处代码各自看都没问题——只能靠逐字比对。

        三条轴（主题 / 重点色 / 磨砂）各核四件事：属性名、id 清单、存储键、默认值。
        磨砂也在首屏脚本里：它虽然是材质不是配色，但晚一帧生效同样看得见——
        22px 的模糊在首帧是 0，界面的层次会跳一下。
        """
        axes = (
            (THEME_IDS, DEFAULT_THEME, "STORAGE_KEY", "ATTR", "DEFAULT_ID", "THEMES"),
            (ACCENT_IDS, DEFAULT_ACCENT, "ACCENT_KEY", "ACCENT_ATTR", "DEFAULT_ACCENT", "ACCENTS"),
            (FROST_IDS, DEFAULT_FROST, "FROST_KEY", "FROST_ATTR", "DEFAULT_FROST", "FROSTS"),
        )
        calls = dict((m.group(1), m) for m in
                     re.finditer(r"apply\('([a-z-]+)', \[([^\]]*)\], '([^']+)', '([^']+)'\)",
                                 self._html))
        # 第一列是 theme.js 里的**常量名**，取它的值才是 DOM 上那个属性名——两者同名
        # 的话这条断言会永远成立，也就永远核不到"内联脚本写的属性名和模块不一致"。
        attrs = [self._module_const(axis[3]) for axis in axes]
        self.assertEqual(sorted(calls), sorted(attrs),
                         "首屏脚本没有一次落齐三个属性（或属性名与 theme.js 不一致）")
        for expected, default, key_name, attr_name, default_name, list_name in axes:
            attr = self._module_const(attr_name)
            node = calls[attr]
            listed = sorted(re.findall(r"'([^']+)'", node.group(2)))
            self.assertEqual(listed, sorted(expected),
                             "内联脚本里 %s 的清单与测试期望不一致" % attr)
            self.assertEqual(listed, sorted(self._module_ids(list_name)),
                             "内联脚本里 %s 的清单与 theme.js 的 %s 不一致" % (attr, list_name))
            self.assertEqual(node.group(3), self._module_const(key_name),
                             "内联脚本里 %s 的存储键与 theme.js 不一致" % attr)
            self.assertEqual(node.group(4), self._module_const(default_name),
                             "内联脚本里 %s 的默认值与 theme.js 不一致" % attr)
            self.assertEqual(node.group(4), default,
                             "内联脚本里 %s 的默认值与测试期望不一致" % attr)
        # 内联脚本必须在样式表之前：放到 DOMContentLoaded 等于等首屏画完再改色。
        self.assertLess(self._html.index("apply('data-accent'"),
                        self._html.index('href="style.css"'),
                        "首屏外观脚本必须在 <link rel=stylesheet> 之前")

    def test_theme_module_survives_a_hostile_localstorage(self):
        """localStorage 读取即抛时不能把整页带崩（隐私模式、企业策略）。

        这条路径上漏一个 try，页面会因为一份"上次选了哪套主题"的偏好而起不来，
        而症状与主题毫无关系——排查要花很久。split.js 那边已经踩过一次。
        """
        for name in ("readStored", "writeStored"):
            start = self._js.index("function %s(" % name)
            body = self._js[start:self._js.index("\n  }", start)]
            self.assertIn("try", body, "%s() 缺少 try" % name)
            self.assertIn("catch", body, "%s() 缺少 catch" % name)

    @staticmethod
    def _at_rule_body(css: str, marker: str) -> str:
        """某个 @media 的规则体（按花括号配对切，因为里面还有一层规则的花括号）。"""
        text = _strip_comments(css)
        start = text.index(marker)
        open_at = text.index("{", start)
        depth, i = 0, open_at
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        return text[open_at + 1:i]

    def test_reduced_transparency_switches_the_glass_material_off(self):
        """玻璃是默认主题，而半透明正是"对比度随背后内容浮动"的来源。

        系统里那个"降低透明度"开关必须真的把这层材质关掉，而不是等使用者自己去
        主题列表里换一个——开了这个开关的人已经表达过意愿了。
        """
        body = self._at_rule_body(self._css_raw, "prefers-reduced-transparency: reduce")
        self.assertTrue(body, "缺少 prefers-reduced-transparency 回退")
        self.assertIn("--glass-blur: 0px", body, "该偏好下必须把模糊关掉")
        self.assertIn("--ambient-1: transparent", body, "该偏好下必须撤掉环境色晕")
        # 回落之后表面必须是不透明色：留着 alpha，这项偏好等于没生效。
        panel = self._tokens(body)["--panel"]
        self.assertNotIn("rgba", panel, "该偏好下 --panel 必须变成不透明色")
        # 反过来钉住**玻璃主题本身**的面是半透明的——不然上面那条永远成立，等于没测。
        self.assertIn("rgba", self._tokens(self._theme_block("glass"))["--panel"],
                      "玻璃主题的面应当是半透明的，否则谈不上材质")

    def test_reduced_transparency_fallback_keeps_the_text_readable(self):
        """回退里的面必须与**当前**的正文色相配，而不是与"上一个版本的正文色"。

        这条抓的是一类特定破口：主题换了明暗，而 `@media` 里那份回退没跟着换。
        它只在"开了系统降低透明度开关"的机器上复现，那条路径上没有任何人会去看；
        而上面那条断言只核对回退的面不含 alpha（"材质真的关掉了"），**从来没有量过
        它上面的字**。

        实测过一次真的：玻璃从深色转亮之后，这份回退里的 --panel 还是 #1c2130，
        正文对它是 1.19:1 —— 侧栏、页头、登录卡、所有卡片全吃 --panel，
        等于整屏读不出来。
        """
        media = self._at_rule_body(self._css_raw, "prefers-reduced-transparency: reduce")
        match = re.search(r':root\[data-theme="(\w+)"\]\s*\{([^}]*)\}', media)
        self.assertTrue(match, "回退块的选择器形状变了，这条守卫跟不上")
        name = match.group(1)
        tokens = self._theme_tokens(name)
        tokens.update(self._tokens(match.group(2)))
        # 回退把环境光撤了，所以底下就是 --bg 一层
        base = self._color(tokens["--bg"], tokens)
        for token, need in (("--text", 7.0), ("--text-dim", 4.5), ("--muted", 4.5)):
            fg = self._color(tokens[token], tokens)
            for surface in ("--panel", "--panel-2", "--panel-3"):
                bg = self._over(self._color(tokens[surface], tokens), base)
                ratio = self._ratio(fg, bg)
                self.assertGreaterEqual(
                    ratio, need,
                    "%s 的降低透明度回退：%s 在 %s 上只有 %.2f:1（要求 %.1f:1）——"
                    "主题换明暗时这份回退没跟着换" % (name, token, surface, ratio, need))

    # ------------------------------------------------------- 选择器与界面

    def test_the_picker_is_a_group_of_buttons_with_a_pressed_state(self):
        """选择器用 aria-pressed 的按钮组，而不是 role=radio。

        声明成单选组就得同时实现漫游 tabindex 与方向键；只写 role 不实现行为，
        读屏用户拿到的是一个"按方向键毫无反应的单选组"，比不声明语义更糟。
        """
        self.assertIn('id="theme-grid"', self._html)
        self.assertNotIn('role="radiogroup"', self._html, "声明了单选组语义就要实现方向键")
        self.assertIn("aria-pressed", self._settings)
        self.assertIn("data-theme-id", self._settings)
        for hook in ("theme-grid", "theme-current", "theme-note"):
            self.assertIn("byId('%s')" % hook, self._settings,
                          "设置页没有取到 #%s" % hook)

    def test_appearance_group_is_first_and_is_the_default(self):
        """外观排在第一组，而且设置页默认就落在这里。

        它是五组里唯一与作业和权限都无关的一项（只改本机显示），排在四组管理项之后
        会让人以为改外观也要管理员——而它是使用者会反复回来找的那一项。
        """
        nav = self._html[self._html.index('class="settings-nav"'):]
        nav = nav[:nav.index("</nav>")]
        order = re.findall(r'data-settings-group="([a-z]+)"', nav)
        self.assertEqual(order[0], "appearance", "外观应当是第一个分组")
        self.assertIn('data-settings-group="appearance"', nav)
        self.assertIn('class="settings-nav-btn active"', nav.split('data-settings-group="agents"')[0],
                      "默认选中的那一组应当是外观")
        self.assertIn("group: 'appearance'", self._settings,
                      "设置页的默认分组与导航上的 active 不一致")

    def test_graph_edge_colors_come_from_tokens_not_from_js(self):
        """图谱边色必须由 CSS 下发。

        原先这六个值写成字面量躺在 app.js 的 EDGE_COLOR 里，而 SVG 的表现属性不认
        var()——结果是换主题时节点换了色、边没换。JS 只该标记类型。
        """
        self.assertNotIn("EDGE_COLOR", self._app, "边色不得再写在 JS 里")
        self.assertNotIn("EDGE_COLOR", self._css)
        for token in ("--edge-spawns", "--edge-yields", "--edge-derived",
                      "--edge-proves", "--edge-parent"):
            self.assertIn("%s:" % token, self._css, "%s 未定义" % token)
        self.assertIn("stroke: var(--edge-spawns)", self._css,
                      "基础规则要给出未知类型的兜底色（缺失即不绘制）")
        self.assertIn("fill: var(--edge-spawns)", self._css, "箭头也要跟着令牌走")
        # 兜底必须与 JS 里的常量同名同类，两处不一致时某条边会直接不画。
        self.assertIn("EDGE_FALLBACK_KIND = 'spawns'", self._app)

    def test_preview_swatches_match_the_css(self):
        """预览卡片上的色值必须真的来自对应主题。

        这三个值是刻意冗余的（预览要能**不切换主题**就画出来），代价是可能漂移。
        漂移的症状是"预览和点下去不一样"——一旦有人遇到，他就不再相信这个列表了。
        """
        pairs = re.findall(r"id:\s*'(\w+)',(.*?)swatch:\s*\{([^}]*)\}", self._js, flags=re.S)
        self.assertEqual(sorted(name for name, _, _ in pairs), sorted(THEME_IDS),
                         "theme.js 的 THEMES 与主题清单不一致")
        for name, _, swatch in pairs:
            # 午夜的定义就是 :root 那一套
            source = self._theme_block(name) if name != ROOT_IS else self._root_body()
            self.assertTrue(source, "找不到 %s 的取值来源" % name)
            for field in ("bg", "accent", "text"):
                match = re.search(r"%s:\s*'(#[0-9a-fA-F]{6})'" % field, swatch)
                self.assertTrue(match, "%s 的 swatch 缺 %s" % (name, field))
                self.assertIn(match.group(1), source,
                              "%s 的预览色 %s（%s）在 CSS 里找不到——预览已经与主题漂移"
                              % (name, field, match.group(1)))

    def _root_body(self) -> str:
        for sel, body in self._blocks:
            if sel == ":root":
                return body
        return ""

    # ------------------------------------------------------- 重点色（可调）

    def test_every_accent_has_both_families(self):
        """每一档重点色都必须同时给出深色族与亮色族。

        只写一族，另一族会**安静地沿用主题自己的值**——也就是"选了没反应"，
        而且只在四套主题里的某两套上没反应，看起来像那一档"效果不明显"。
        """
        for name in ACCENT_IDS:
            if name == DEFAULT_ACCENT:
                self.assertEqual(self._accent_dark_block(name), "",
                                 "「跟随主题」不该有块——没有块就是没有覆盖")
                continue
            self.assertTrue(self._accent_dark_block(name), "%s 缺深色族" % name)
            self.assertTrue(self._accent_light_block(name), "%s 缺亮色族" % name)

    def test_accent_light_overrides_cover_every_light_theme(self):
        """亮色族的选择器必须点名**每一套**亮色主题。

        哪几套是亮色由各主题自己的 color-scheme 决定（唯一真相），而选择器是手写的
        一串。新增一套亮色主题时漏掉它，症状是"那一套主题下重点色不跟着变"——
        与"这一档本来就不明显"无法区分。
        """
        light = sorted(self._light_themes())
        self.assertTrue(light, "一套亮色主题都没有？")
        for name in ACCENT_IDS:
            if name == DEFAULT_ACCENT:
                continue
            found = None
            for sel, _ in self._blocks:
                if sel.startswith(":root[data-theme=") and 'data-accent="%s"' % name in sel:
                    found = sorted(re.findall(r'\[data-theme="(\w+)"\]', sel))
            self.assertEqual(found, light,
                             "%s 的亮色族选择器列的亮色主题与 color-scheme 不符（应为 %s）"
                             % (name, light))

    def test_accent_overrides_stay_in_their_lane(self):
        """重点色只准动重点色家族，**一个读数都不许碰**。

        这是"可调配色"最容易破的一条：想让整体协调，最省事的做法是顺手把
        --sev-high 也调成同一个色相——而那正是让"红色徽章到底是选中还是严重"
        分不出来的那一步。深色族另外**不许覆盖 --on-accent-deep**：它在四套深色
        主题里都是白，而白对任何深填充都是对的；覆盖它等于把一个已经成立的取值
        换成一个必须逐个主题重新验的取值。
        """
        for name in ACCENT_IDS:
            if name == DEFAULT_ACCENT:
                continue
            for label, body in (("深色族", self._accent_dark_block(name)),
                                ("亮色族", self._accent_light_block(name))):
                tokens = set(self._tokens(body))
                self.assertTrue(tokens, "%s 的%s是空的" % (name, label))
                self.assertEqual(tokens - ACCENT_TOKENS, set(),
                                 "%s 的%s动了不该动的令牌：%s"
                                 % (name, label, sorted(tokens - ACCENT_TOKENS)))
                self.assertEqual(tokens & ACCENT_FORBIDDEN, set(),
                                 "%s 的%s碰了读数类令牌：%s"
                                 % (name, label, sorted(tokens & ACCENT_FORBIDDEN)))
            self.assertNotIn("--on-accent-deep", self._tokens(self._accent_dark_block(name)),
                             "%s 的深色族不该覆盖 --on-accent-deep" % name)

    def test_every_accent_keeps_the_two_accent_foregrounds_readable(self):
        """逐 (主题 × 重点色) 核对两组前景对比度。

        重点色是**跨主题**的：同一档要同时落在深色与亮色主题上，而这两边的前景
        方向相反。只按"看着好看"挑色值，一定会在某一套主题上掉到 4.5:1 以下，
        而那个组合恰好没人会去点开看。
        """
        for theme in THEME_IDS:
            for accent in ACCENT_IDS:
                tokens = self._accent_tokens(theme, accent)
                for fill, ink in (("--accent", "--on-accent"),
                                  ("--accent-dim", "--on-accent-deep")):
                    ratio = self._ratio(self._color(tokens[ink], tokens),
                                        self._color(tokens[fill], tokens))
                    self.assertGreaterEqual(
                        ratio, 4.5,
                        "%s + 重点色「%s」：%s 在 %s 上只有 %.2f:1"
                        % (theme, accent, ink, fill, ratio))

    def test_accent_swatches_match_the_css(self):
        """重点色预览的色点必须真的来自 CSS。

        与主题预览同一个理由：这两个值是刻意冗余的（预览要能**不切换主题**就画出来），
        代价是可能漂移。漂移的症状是"点上去的颜色和色点不一样"——一旦有人遇到，
        他就不再相信这一排。
        """
        match = re.search(r"const ACCENTS = \[(.*?)\n  \];", self._js, flags=re.S)
        self.assertTrue(match, "theme.js 里找不到 ACCENTS")
        entries = re.findall(r"id:\s*'(\w+)'[^}]*?dark:\s*'(#[0-9a-fA-F]{6}|)'"
                             r"[^}]*?light:\s*'(#[0-9a-fA-F]{6}|)'", match.group(1))
        self.assertEqual(sorted(name for name, _, _ in entries), sorted(ACCENT_IDS),
                         "theme.js 的 ACCENTS 与重点色清单不一致")
        for name, dark, light in entries:
            if name == DEFAULT_ACCENT:
                self.assertEqual((dark, light), ("", ""),
                                 "「跟随主题」不该有色值可画——画任何颜色都会说谎")
                continue
            self.assertIn(dark, self._accent_dark_block(name),
                          "%s 的深色预览色（%s）在 CSS 里找不到——预览已经与主题漂移"
                          % (name, dark))
            self.assertIn(light, self._accent_light_block(name),
                          "%s 的亮色预览色（%s）在 CSS 里找不到——预览已经与主题漂移"
                          % (name, light))

    # --------------------------------------------------------- 磨砂（可调）

    def test_frost_levels_only_move_material_tokens(self):
        """磨砂档位只准动材质。磨砂是**材质**，不是主题。

        一旦它顺手改了配色（哪怕只是把 --panel 调深一点），五套主题里就有四套
        会静默变形，而"我什么都没改，怎么颜色不对了"是最难联想到磨砂的一条。
        """
        for name in FROST_IDS:
            if name == DEFAULT_FROST:
                self.assertEqual(self._block_by_selector(':root[data-frost="%s"]' % name), "",
                                 "「跟随系统」不该有块——没有块就是取主题自己的值")
                continue
            tokens = set(self._tokens(self._block_by_selector(':root[data-frost="%s"]' % name)))
            self.assertTrue(tokens, "%s 档是空的" % name)
            self.assertEqual(tokens - FROST_TOKENS, set(),
                             "%s 档动了非材质令牌：%s" % (name, sorted(tokens - FROST_TOKENS)))

    def test_frost_levels_come_before_the_reduced_transparency_fallback(self):
        """档位必须**早于** prefers-reduced-transparency 那一段。

        顺序反了的话，一个开了系统"降低透明度"的人只要动一次磨砂滑块，那项无障碍
        偏好就被静默撤销了——而两边各自看都是对的。这里只能靠源码位置核。
        """
        for name in FROST_IDS:
            if name == DEFAULT_FROST:
                continue
            self.assertLess(self._css.index(':root[data-frost="%s"]' % name),
                            self._css.index("prefers-reduced-transparency"),
                            "%s 档写在了降低透明度回退之后（系统偏好会失效）" % name)

    def test_every_frost_and_accent_is_offered_in_the_settings_page(self):
        """三组选择器都必须真的被渲染出来，而且取到了容器。

        只加数据不加渲染，症状是"设置页里少了一组"——而它看起来像"这一版没做那个功能"，
        不像 bug。
        """
        for hook in ("accent-row", "accent-note", "frost-row", "frost-note"):
            self.assertIn('id="%s"' % hook, self._html, "index.html 缺少 #%s" % hook)
            self.assertIn("byId('%s')" % hook, self._settings, "设置页没有取到 #%s" % hook)
        for fn in ("renderAccents", "renderFrosts"):
            self.assertIn("function %s(" % fn, self._settings, "设置页缺少 %s" % fn)
            self.assertIn("%s," % fn, self._settings, "%s 没有被导出" % fn)

    def test_graph_node_highlight_is_a_token_not_a_fixed_brightness(self):
        """节点高亮的滤镜必须是令牌。

        深色主题里"强调" = 提亮；亮色主题里提亮等于把文字与描边一起洗白，强调效果
        正好反过来。写死一个方向的话，另一档主题里的高亮看起来像"没生效"。
        """
        # `(?<![\w-])` 是必须的：令牌名 `--hl-filter` 的结尾正好是 `filter`，
        # 不排除它的话，**令牌本身**会被当成"规则里写死了滤镜"，而那条断言
        # 反过来又会在真正写死时被令牌遮蔽——一个既会假红又会假绿的写法。
        hardcoded = re.findall(r"(?<![\w-])filter:\s*brightness", self._css)
        self.assertEqual(hardcoded, [], "节点高亮不得写死亮度，要走 --hl-filter")
        self.assertIn("var(--hl-filter)", self._css)
        self.assertIn("var(--hl-filter-strong)", self._css)
        for name in THEME_IDS:
            self.assertIn("--hl-filter", self._theme_tokens(name),
                          "%s 主题没有给出 --hl-filter" % name)
        # 反过来钉住两档的方向确实相反——不然上面那几条在"两边都写 1.35"时也成立。
        dark = self._tokens(self._theme_block("midnight"))
        if not dark:
            dark = self._root_tokens()
        self.assertIn("brightness(1.", self._root_tokens()["--hl-filter"],
                      ":root（= 午夜）的强调应当是提亮")
        self.assertIn("brightness(.",
                      self._tokens(self._theme_block("glass"))["--hl-filter"],
                      "亮色主题的强调应当是压深")


if __name__ == "__main__":
    unittest.main()
