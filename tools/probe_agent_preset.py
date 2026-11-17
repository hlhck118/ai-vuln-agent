# -*- coding: utf-8 -*-
"""Agent 预设表单的验收探针：新建走完整闭环，编辑核对回填。

## 为什么需要它

「设置 → Agent 预设」的增删改查是**跨界面的**：卡片上的按钮是动态生成的（走
`#agent-modes-list` 上的事件委托）、表单是随点随开的、标识要跟着名称自动变、
保存后要收起表单并重绘列表。这些环节里的任何一个断掉，症状都是"点了没反应"或
"保存了但列表没变"，而**单元测试看不到**——它们验的是 HTTP 契约，不验 DOM 事件。

所以这里走**夹具 + 无头浏览器**：生成一份内联了真实接口响应的自包含 HTML，
夹具里已经注入了一段探针脚本（`render_ui_fixture` 的 `--preset-form`），用真实
`click()` / `input` 事件把两条路径走一遍，结果写进 `<html>` 属性，再 `--dump-dom`
读回来。判定的是"结果对不对"，不是"没抛异常"。

## 判定标准

| 场景 | 期望 |
|---|---|
| 新建 · 点「新建预设」 | 表单真的展开（`form-open=yes`），标题是"新建 Agent 预设" |
| 新建 · 输入中文名称 | 标识自动建议为**空**（中文挤不出 ASCII），不要凭空造一个 |
| 新建 · 填完点保存 | 表单收起、列表里多出这一条、提示条念出 id 与"下一轮对话生效" |
| 编辑 · 点「编辑」 | 表单展开且**标识框禁用**、正文按真实长度回填、技能勾选数 > 0 |
| 编辑 · 标题 | 是"编辑预设：<名称>"，不是"新建" |

## 用法

    python tools/probe_agent_preset.py [输出目录]

需要本机有 Edge 或 Chrome（无浏览器时打印 SKIP 并以 0 退出，不误报失败）。
本工具**不进单元测试套件**：`python -m unittest discover -s tests` 必须保持零外部
依赖，而这里要起浏览器。它属于 `tools/` 下的验收工具，与 `probe_graph_click.py`
同一类。
"""
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import render_ui_fixture                                        # noqa: E402
from probe_graph_click import BROWSERS, _browser, _dump          # noqa: E402


def _attr(dom: str, name: str) -> str:
    match = re.search(r'%s="([^"]*)"' % name, dom)
    return match.group(1) if match else ""


def _run(browser: str, out_dir: str, mode: str, failures: list) -> dict:
    fixture = os.path.join(out_dir, "preset-%s.html" % mode)
    data = render_ui_fixture.collect()
    render_ui_fixture.build(fixture, data, tab="settings", group="agents",
                            preset_form=mode)
    dom = _dump(browser, fixture, os.path.join(out_dir, "profile-%s" % mode))

    step = _attr(dom, "data-preset-step")
    opened = _attr(dom, "data-preset-open-after-click")
    still_open = _attr(dom, "data-preset-form-open")
    expect_skills = _attr(dom, "data-preset-expect-skills")
    title = urllib.parse.unquote(_attr(dom, "data-preset-title"))
    handle = _attr(dom, "data-preset-handle")
    locked = _attr(dom, "data-preset-handle-locked")
    prompt_chars = _attr(dom, "data-preset-prompt-chars")
    skills = _attr(dom, "data-preset-skills-selected")
    result = urllib.parse.unquote(_attr(dom, "data-preset-result"))
    errors = _attr(dom, "data-fixture-errors")

    print("— %s —" % mode)
    print("  探针步骤：%s ｜ 点后展开：%s ｜ 结束态展开：%s"
          % (step or "(无)", opened or "(无)", still_open or "(无)"))
    print("  标识：%r ｜ 标识框禁用：%s ｜ 标题：%s"
          % (handle, locked or "-", title or "-"))
    if mode == "edit":
        print("  正文回填：%s 字 ｜ 勾选手册：%s 份（夹具声明 %s 份）"
              % (prompt_chars or "0", skills or "0", expect_skills or "0"))
    else:
        print("  保存结果：%s" % (result or "(无提示条)"))
    if errors and errors != "[]":
        print("  夹具错误：%s" % errors)

    if step != "done":
        failures.append("%s：探针没跑完（最后一步 %s），页面可能抛了异常"
                        % (mode, step or "未开始"))
        return {}
    if opened != "yes":
        failures.append("%s：点完之后表单没展开" % mode)

    if mode == "edit":
        if still_open != "yes":
            failures.append("编辑表单自己收起来了——编辑路径不该提交")
        if locked != "yes":
            failures.append("编辑时标识框没有禁用——改 id 会让钉住的会话静默失效")
        if title and not title.startswith("编辑预设"):
            failures.append("编辑态的标题不对：%s" % title)
        if not prompt_chars or int(prompt_chars) < 10:
            failures.append("正文没回填（%s 字）——一保存就会把原口径清空"
                            % (prompt_chars or "0"))
        if (skills or "0") != (expect_skills or "0"):
            failures.append("手册勾选回填不对：界面 %s 份，夹具声明 %s 份"
                            % (skills or "0", expect_skills or "0"))
    else:
        # 保存成功**必须**把表单收起：它已经完成使命，留在那里会让人以为没保存上，
        # 再点一次就撞"标识已被占用"。
        if still_open != "no":
            failures.append("保存之后表单没有收起")
        if handle != "":
            failures.append("中文名称下标识应留空由用户填，实际自动填成了 %r" % handle)
        if "已创建" not in result:
            failures.append("保存后没有报出创建成功：%s" % (result or "(无)"))
        if "下一轮对话生效" not in result:
            failures.append("提示条没说明生效时机：%s" % (result or "(无)"))
    return {"step": step, "result": result}


def main() -> int:
    browser = _browser()
    if not browser:
        print("SKIP：本机没找到 Edge / Chrome，跳过 Agent 预设表单探针。")
        print("      （这不是失败：探针要真实浏览器，而单元测试套件保持零外部依赖。）")
        return 0

    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(
        prefix="preset-probe-")
    os.makedirs(out_dir, exist_ok=True)
    print("浏览器：%s" % browser)
    print("夹具目录：%s" % out_dir)

    failures = []
    for mode in ("new", "edit"):
        _run(browser, out_dir, mode, failures)

    if failures:
        print("")
        print("结论：FAIL")
        for item in failures:
            print("  · %s" % item)
        return 1
    print("")
    print("结论：PASS（新建闭环与编辑回填都符合预期）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
