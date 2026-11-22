# -*- coding: utf-8 -*-
"""在夹具页面上读**计算样式**，把布局争议变成数字。

为什么需要它：`render_ui_fixture.py` 出的是图，`--dump-dom` 出的是 DOM 与属性 —— 两者都
回答不了"这个工具条到底多高""报告标题到底多大""这个胶囊的点击区够不够 24px"。那类问题
只能问浏览器自己，而 `getComputedStyle` 只在渲染进程里存在。

用法：
    python tools/probe_ui_style.py 夹具.html "<返回可 JSON 值的 JS 表达式>"

表达式里可以直接用 `switchTab('setup')` 这类全局函数（夹具内联的脚本是经典脚本，顶层
函数即全局），因此**一份夹具就能量完八个页签**。示例（量八个页签的工具条高度）：

    python tools/probe_ui_style.py f.html "var r={};['chat','graph','findings','assets','report','setup','settings','guide'].forEach(function(t){switchTab(t);var b=document.querySelector('#panel-'+t+' .list-toolbar, #panel-'+t+' .graph-toolbar, #panel-'+t+' .report-toolbar');r[t]=b?b.getBoundingClientRect().height:null;});return r;"

两个实现细节都不是可选的：
  * 注入的脚本必须**延后 1200ms** —— 夹具的 `switchTab` / `fetch` 是异步的，同步执行时
    工作台外壳还没取消 `hidden`，量到的全是 0×0；
  * 结果写进 `<html data-probe>` 再用 `--dump-dom` 取回 —— 无头浏览器没有 stdout 可打印。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import render_ui_fixture as fixture          # noqa: E402


def probe(html_path: str, script: str, wait_ms: int = 12000) -> dict:
    browser = fixture._browser()
    if not browser:
        raise SystemExit("找不到 Edge / Chrome")
    with open(html_path, "r", encoding="utf-8") as handle:
        html = handle.read()
    # 必须延后到夹具自己的初始化之后：footer 里的 switchTab / fetch 是异步的，
    # 同步执行时工作台外壳还没取消 hidden，量到的全是 0×0。
    inject = ("<script>setTimeout(function(){try{var v=(function(){%s})();"
              "document.documentElement.setAttribute('data-probe',"
              "JSON.stringify(v));}catch(e){document.documentElement."
              "setAttribute('data-probe', JSON.stringify({error:String(e)}));}},1200);"
              "</script>") % script
    html = html.replace("</body>", inject + "</body>", 1)
    with tempfile.TemporaryDirectory(prefix="ui-probe-") as tmp:
        path = os.path.join(tmp, "probe.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)
        profile = tempfile.mkdtemp(prefix="ui-probe-profile-")
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
               "--user-data-dir=" + profile,
               "--window-size=1440,1000",
               "--virtual-time-budget=%d" % wait_ms,
               "--dump-dom", "file:///" + path.replace("\\", "/")]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300,
                                check=False)
    marker = 'data-probe="'
    index = result.stdout.find(marker)
    if index < 0:
        raise SystemExit("页面里没有 data-probe —— 注入的脚本可能没跑：\n%s"
                         % result.stdout[:2000])
    start = index + len(marker)
    end = result.stdout.find('"', start)
    raw = (result.stdout[start:end].replace("&quot;", '"')
           .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&#39;", "'"))
    return json.loads(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("html")
    parser.add_argument("js", help="返回可 JSON 序列化值的 JS 表达式")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    value = probe(args.html, args.js)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("written %s" % args.out)
    else:
        print(text)
