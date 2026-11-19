# -*- coding: utf-8 -*-
"""把交付内容打成 zip（用于交接 / 导入）。

**它同时是一份安全边界**：打包时按白名单思路排除运行期数据，而不是"把目录一压"。

排除规则分三类，每一类都有它自己的理由：

| 规则 | 覆盖什么 | 理由 |
|---|---|---|
| **点开头的目录** | `.scratch/`、`.dsh-vision-toolkit/`、`.pytest_cache/`、`.venv/` | 工具与缓存目录。**这条是结构性的**——它不枚举名字，所以以后新增的临时目录自动被挡住 |
| **运行期数据** | `data/`（`data/skills/` 除外）、`memory/`、`prototype/memory/`、`prototype/out/` | 数据库（**管理员口令哈希**、engagement、节点令牌摘要）、`secret.key`（模型密钥的加密密钥）、WAL、生成的报告、以及 66 MB 下载来的外部工具 |
| **机密与身份** | `*.key` `*.crt` `*.csr` `identity.json` `*.db*` `*.pyc` `*.log` | 证书与节点身份（应由接收方自行签发）、构建缓存、运行日志 |

**一个例外要写清楚**：`data/skills/` **进包**。它在 `data/` 下面，
但它是**人写的作业手册**（本平台自己的工具怎么用、什么时候该用哪份），不是运行期
产物，也不是机密。代码里没有任何一处生成它（`skills.py` 只读它，全局搜不到写入方），
所以排除掉等于交付包少一块功能，而症状是"技能列表是空的"——看不出为什么。约 51 KB。

同理，**排除清单不能靠枚举目录名**。枚举漏一个的代价是有上限的：曾经因为漏了
`.scratch/` 与 `.dsh-vision-toolkit/`，包里 45%（149/332）是开发期的临时脚本与
截图转储，其中还包括一份 `pentestd/` 的**备份副本**——收包人会看到两份 `server.py`。

用法：
    python tools/make_handoff_zip.py                      # 默认打到真实桌面
    python tools/make_handoff_zip.py --out D:\\x.zip
    python tools/make_handoff_zip.py --dry-run            # 只列清单不写文件
    python tools/make_handoff_zip.py --out x.zip --no-test-count   # 跳过测试条数统计
    python tools/make_handoff_zip.py --allow-removals     # 上一版有、这版确实要没有

**跨版本比对**：打包前会和同一目录里最新的同族旧包比一遍（新增/变更/删除/未变）。
"上一版有、这一版没有"默认**拒绝出包**（退出码 3），因为下面这条真实事故里，
删失是唯一一种现有自检看不见的失败：

> 2026-10-21 19:22 与 19:28 两次打包之间，`使用手册.docx` 被人删掉，
> 包从 267 个文件变成 266。`verify_package()` 只判定「包内 == 收录清单」，
> `missing_top()` 只看 `INCLUDE_TOP` 那几项在不在——两条都通过，
> 唯一露出痕迹的地方是 索引 少了一行，而没人会逐行比两版 索引。
"""
import argparse
import binascii
import datetime as dt
import os
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
# 2026-10-21 布局上移：`pentest-platform-dsh/` 里的内容提到了 ai-vuln-agent/ 根，
# 于是"仓库根"与"工作台根"变成同一个目录。两个名字都留着，因为它们表达的是两件
# 不同的事（仓库里能装什么 / 工作台从哪里起），只是现在恰好重合。
REPO_ROOT = os.path.dirname(HERE)                   # ai-vuln-agent/
PLATFORM_ROOT = REPO_ROOT                           # 工作台根 = 仓库根

# 目录名精确匹配即整棵排除。**点开头的目录不在这里枚举**，由结构规则处理；
# 两个地方各写一份名单，就是"两处判断同一件事"的经典失败。
EXCLUDED_DIRS = {"__pycache__", "node_modules", "venv"}
# 点开头的**文件**默认也排除，这几个留着：它们说明的正是"什么不该进版本库"。
KEEP_DOTFILES = {".gitignore"}
# 相对 ai-vuln-agent/ 的路径前缀，整棵排除
EXCLUDED_PREFIXES = (
    "prototype/memory",
    "prototype/out",
    "data",
    "memory",          # 运行期 cases.db
)
# 排除前缀之下的**交付内容**白名单，先于前缀判定。见模块头"一个例外"。
DATA_KEEP = ("data/skills",)
# 后缀排除
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".db", ".db-shm", ".db-wal", ".db-journal",
                     ".key", ".crt", ".csr", ".ext", ".srl", ".log")
EXCLUDED_NAMES = {"identity.json", "secret.key", "pentest.db", ".DS_Store"}

# 顶层要收录的条目（白名单：没列进来的不会被误装）
#
# 布局上移之后没有"总目录"可包了，这份清单就是**项目根的全部交付物**。少了哪一条，
# 包里就少一整块；`missing_top()` 会在动手之前把不存在的条目喊出来。
# 不在清单里的：`memory/`（运行期库，由 EXCLUDED_PREFIXES 整棵剪掉）、点开头的
# 目录与文件（`.scratch/`、`.dsh-*` 锁）、`__pycache__`。
INCLUDE_TOP = (
    # 交付说明（`索引.md` **不在这里**：包内那份由本脚本现生成，见 GENERATED_TOP）
    "项目说明.md",
    # 文档
    "使用手册.md", "使用手册.docx", "架构设计.md", "交互规范.md",
    "视觉设计系统.md", "视觉评审.md", "DSH集成说明.md",
    # 启动入口与版本库约定
    "start.sh", ".gitignore",
    # 代码
    "pentestd", "web", "deploy", "tests", "tools", "dsh-plugins",
    # 产品数据（`data/skills/` 是 `data/` 整棵排除之下的例外，见 DATA_KEEP）
    "data",
    # 漏洞发掘引擎
    "prototype",
    # 第三方静态资源（其中 `node_modules/` 仍按目录名整棵排除）
    "vendor",
    # 过程文档
    "docs",
)
# 顶层里**由本脚本自己生成**的东西：盘上可能有一份人看的副本，但它不该作为
# "收录内容"进包——包内那份是在写完 索引 的同时写进去的，两者同名。
# 早先它被写进 INCLUDE_TOP，于是同一个 zip 里出现两条 `ai-vuln-agent/索引.md`，
# 而 `verify_package()` 的重复条目检查会把整个包判失败——一次"打不出来"，
# 原因却是清单里多了一个名字。
GENERATED_TOP = {"索引.md"}


def _norm(rel_path: str) -> str:
    return rel_path.replace("\\", "/").rstrip("/")


def _under(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


def _kept(rel: str) -> bool:
    """是否落在 DATA_KEEP 这类"在排除前缀下、但属于交付内容"的白名单里。"""
    return any(_under(rel, p) for p in DATA_KEEP)


def whole_dir_excluded(rel_dir: str) -> str:
    """整个目录是否该排除；返回理由，不排除则返回空串。

    这是目录侧的唯一判定点，`excluded()` 会调它，所以两者不会各说各话。
    """
    rel = _norm(rel_dir)
    if _kept(rel):
        return ""
    # 这个目录里如果还压着 DATA_KEEP 里的东西，就**不能整棵剪**——得走进去。
    # 少了这一条，`data/` 会在 `data/skills/` 之前被剪掉，例外永远不生效，
    # 而且症状是"技能文件没进包"，看起来像例外没写对。
    if any(keep.startswith(rel + "/") for keep in DATA_KEEP):
        return ""
    parts = rel.split("/")
    if any(part.startswith(".") for part in parts):
        return "工具/缓存目录（点开头）"
    if any(part in EXCLUDED_DIRS for part in parts):
        return "缓存/依赖目录"
    if any(_under(rel, p) for p in EXCLUDED_PREFIXES):
        return "运行期数据"
    return ""


def excluded(rel_path: str) -> str:
    """返回排除理由；不排除则返回空串。文件侧的唯一判定点。

    顺序是刻意的：`DATA_KEEP` 的例外**只放开前缀这一条**，不放开机密与后缀。
    否则往 `data/skills/` 里丢一个 `.key` 就会随包发出去——一个只有写错文件的人才
    注意得到的漏洞。
    """
    rel = _norm(rel_path)
    parts = rel.split("/")
    if len(parts) > 1:
        reason = whole_dir_excluded("/".join(parts[:-1]))
        if reason:
            return reason
    name = parts[-1]
    if name in EXCLUDED_NAMES:
        return "运行期机密或身份文件"
    if name.startswith(".") and name not in KEEP_DOTFILES:
        return "工具/缓存文件"
    if name.lower().endswith(EXCLUDED_SUFFIXES):
        return "运行期机密/构建产物"
    if _kept(rel):
        return ""
    if any(_under(rel, p) for p in EXCLUDED_PREFIXES):
        return "运行期数据"
    return ""


def missing_top():
    """`INCLUDE_TOP` 里在这棵树上**根本不存在**的条目。

    没有这个检查，`collect()` 遇到缺失的顶层条目会静默 `continue`，而
    `verify_package()` 判定的是「包内 == 收录清单」——两条合起来，**整整一棵顶层
    目录丢了也照样打印「条目集合相等、排除规则复核、压缩流完整性 均通过」**。
    那是最坏的一种通过：它证明的是清单被遵守了，而不是清单本身是对的。

    所以检查放在这里，由 `main()` 在动手之前判一次；`collect()` 保持只收集、不抛错，
    因为它还要能在测试自建的残缺树上跑。
    """
    return [n for n in INCLUDE_TOP
            if not os.path.exists(os.path.join(REPO_ROOT, n))]


def collect():
    """返回 (收录列表, 排除列表)，元素均为相对 ai-vuln-agent/ 的路径。

    排除列表里被整棵剪掉的目录**只记一行**：遍历 `data/tools/nmap` 这类目录产出
    上万条明细没有意义，读的人只想知道"哪几棵树没进包"。剪枝也因此让打包变快。
    """
    included, skipped = [], []
    for name in INCLUDE_TOP:
        target = os.path.join(REPO_ROOT, name)
        if not os.path.exists(target):
            continue
        if os.path.isfile(target):
            included.append(_norm(name))
            continue
        for base, dirs, files in os.walk(target):
            for d in sorted(dirs):
                rel = os.path.relpath(os.path.join(base, d), REPO_ROOT)
                reason = whole_dir_excluded(rel)
                if reason:
                    skipped.append((_norm(rel) + "/", reason))
                    dirs.remove(d)                       # 整棵剪掉，不再往下走
            for filename in sorted(files):
                full = os.path.join(base, filename)
                rel = os.path.relpath(full, REPO_ROOT)
                reason = excluded(rel)
                # 统一用 `/`：这张表是给人读的，同一列里一半 `data/` 一半
                # `data\pentest.db` 会让人以为是两种东西。所有下游（索引、
                # zip 条目名、跨版本签名）本来就 `_norm`，这里补上源头。
                (skipped if reason else included).append(
                    (_norm(rel), reason) if reason else _norm(rel))

    # 顶层的其余东西：在盘上、但白名单没要。**必须逐个报出来。**
    # 布局上移之后根目录就是交付物全集，此时"不在 INCLUDE_TOP 里"是唯一一种
    # 能整块消失且不留痕迹的失法：既不进 `included`（于是自检不查它），
    # 也不进 `skipped`（于是排除明细里没有它）。`missing_top()` 管的是反方向
    # （要了却没有），这一条管"有却没要"，两个方向都堵上才谈得上清单可信。
    listed = set(INCLUDE_TOP)
    for name in sorted(os.listdir(REPO_ROOT)):
        if name in listed:
            continue
        # 盘上那份 索引.md 是本脚本上一次生成的镜像，包里那份是现写的。
        # 它不是"漏装的交付物"，所以不进"顶层未收录"——那会让每次打包都报一条
        # 自己生成的东西，久了人就不看这张表了。
        if name in GENERATED_TOP:
            continue
        full = os.path.join(REPO_ROOT, name)
        is_dir = os.path.isdir(full)
        # 空目录没有内容可丢，不必报：它多半是某个进程的旧工作目录留下的壳
        # （删不掉，因为别的会话还站在那里）。报它只会让包内索引多一行噪声。
        if is_dir and not os.listdir(full):
            continue
        # 规则已经解释过的也**照样**记一行，只是把理由换成"是哪条规则拦的"。
        # 早先这里对它们直接 `continue`，结果是"被剪掉、却不在排除明细里"——
        # 而 `.scratch/` 那棵占过包体积 45% 的垃圾树，正是这样从明细里无声消失的：
        # 明细表的用途就是回答"为什么这个不在包里"，漏掉一行等于少一个答案。
        reason = excluded(name)
        if is_dir:
            reason = reason or whole_dir_excluded(name)
        skipped.append((_norm(name) + "/" if is_dir else _norm(name),
                        reason or "顶层未收录"))
    return sorted(included), sorted(skipped)


# ---- 跨版本比对：上一版有、这一版没有，就是"无声少文件" --------------------
#
# 自检能证明的只有两件事：收录清单被遵守了（`verify_package` 的集合相等），
# 以及清单要的那几棵顶层目录还在（`missing_top`）。**两件事都不是"文件还在"**。
# 目录内的某个文件被删、被打包流程中途失败覆盖，两条检查都通过。
#
# 真实事故（2026-10-21）：`使用手册.docx` 在两次打包之间被删，包从 267 变 266，
# 打印的仍是"均通过"。所以这里改成先和同目录里最新的同族旧包比一遍。
# 删除默认拒绝出包——误删时"停下来"远比"打出一个少一个文件的包"便宜。

def _file_signature(path: str):
    """(字节数, CRC32)。CRC 与 zip 条目同源，所以磁盘签名能直接和旧包比。

    用 CRC 而不是 mtime：DSH 的文件工具写盘会**保留原修改时间**，实测里
    5 个文件内容变了而 mtime 全早于打包时间。mtime 在这棵树上不是证据。
    """
    crc = 0
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            crc = binascii.crc32(chunk, crc)
    return total, crc & 0xFFFFFFFF


def entry_signatures(included):
    """收录清单在**磁盘上**的签名，形状与 `read_package_entries()` 一致。"""
    return {_norm(rel): _file_signature(os.path.join(REPO_ROOT, rel))
            for rel in included}


def read_package_entries(path: str, label: str):
    """读一个旧包 → `{相对路径: (字节数, CRC)}`；不是同族包则返回 None。

    同族 = 包内根目录恰好是 `label`。桌面上的其它 zip（别的东西的备份）因此
    不会被当成"上一版"，否则第一次打包就会报出一屏"删失"。
    索引 跳过：它含时间戳，每次都会变，比它只会淹掉真正的差异。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            entries = {}
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                if not name.startswith(label + "/"):
                    return None
                rel = name[len(label) + 1:]
                if rel == "索引.md":
                    continue
                entries[rel] = (info.file_size, info.CRC)
            return entries or None
    except (zipfile.BadZipFile, OSError):
        return None


def find_previous_package(out_path: str, label: str):
    """同目录里最新的同族旧包 → (路径, 签名表)；没有则 (None, None)。"""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    target = os.path.abspath(out_path)
    try:
        names = os.listdir(out_dir)
    except OSError:
        return None, None
    candidates = []
    for name in names:
        if not name.lower().endswith(".zip"):
            continue
        path = os.path.join(out_dir, name)
        if os.path.abspath(path) == target:
            continue
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True):
        entries = read_package_entries(path, label)
        if entries is not None:
            return path, entries
    return None, None


def compare_versions(previous, current):
    """两版签名表 → 差异。`changed` 元素是 (路径, 旧签名, 新签名)。"""
    prev_keys, cur_keys = set(previous), set(current)
    changed = [(rel, previous[rel], current[rel])
               for rel in sorted(prev_keys & cur_keys)
               if previous[rel] != current[rel]]
    return {
        "added": [(rel, current[rel]) for rel in sorted(cur_keys - prev_keys)],
        "changed": changed,
        "removed": [(rel, previous[rel]) for rel in sorted(prev_keys - cur_keys)],
        "identical": len(prev_keys & cur_keys) - len(changed),
    }


def _kb(size: int) -> str:
    return "%.1f KB" % (size / 1024.0)


def _looks_like_a_move(diff) -> bool:
    """删除项是否到了"整棵树改名/搬迁"的量级。

    一次布局上移会让**每一条**路径都变成"上一版有、这一版没有"，与"真的删了
    255 个文件"在签名层面长得一模一样。脚本不能替人判断，但可以说出这个形状，
    否则读的人第一反应是"包坏了"。50% 是个粗线：正常的版本迭代删不了半棵树。
    """
    previous_total = len(diff["removed"]) + len(diff["changed"]) + diff["identical"]
    return previous_total > 0 and len(diff["removed"]) * 2 > previous_total


def render_comparison(diff, prev_path) -> list:
    """控制台上的比对结果。删失单独成段，理由写在旁边。"""
    lines = ["跨版本比对（上一版：%s）" % os.path.basename(prev_path),
             "  新增 %d · 内容变更 %d · 删除 %d · 未变 %d"
             % (len(diff["added"]), len(diff["changed"]),
                len(diff["removed"]), diff["identical"])]
    for title, key, mark in (("新增", "added", "+"), ("内容变更", "changed", "~")):
        rows = diff[key]
        if not rows:
            continue
        lines.append("  %s %d 项：" % (title, len(rows)))
        for row in rows[:20]:
            rel = row[0]
            if key == "added":
                lines.append("    %s %s (%s)" % (mark, rel, _kb(row[1][0])))
            else:
                lines.append("    %s %s  %s → %s"
                             % (mark, rel, _kb(row[1][0]), _kb(row[2][0])))
        if len(rows) > 20:
            lines.append("    ……另有 %d 项，见包内 索引.md" % (len(rows) - 20))
    if diff["removed"]:
        lines.append("")
        lines.append("  !!! 可疑删失 %d 项（上一版有、这一版没有）："
                     % len(diff["removed"]))
        for rel, sig in diff["removed"][:20]:
            lines.append("    - %s (%s)" % (rel, _kb(sig[0])))
        if len(diff["removed"]) > 20:
            lines.append("    ……另有 %d 项" % (len(diff["removed"]) - 20))
        if _looks_like_a_move(diff):
            previous_total = (len(diff["removed"]) + len(diff["changed"])
                              + diff["identical"])
            lines += ["",
                      "  （删除项占上一版的 %d%%：这种规模更像整棵树改名/搬迁，"
                      "而不是真的删了文件；"
                      % round(100.0 * len(diff["removed"]) / previous_total),
                      "   逐条确认是搬迁之后，加 --allow-removals 出包。）"]
        lines += [
            "",
            "  这不是「少了一个文件」那么简单：collect() 遇到缺失的项会静默 continue，",
            "  而自检只判定「包内 == 收录清单」——两条合起来，整份交付物消失也照样",
            "  打印「均通过」。2026-10-21 的 使用手册.docx 就是这样从包里掉的。",
        ]
    return lines


def index_change_section(diff, prev_name) -> list:
    """包内 索引 的「与上一版的变化」段。决定留在交付物里，而不是留在记忆里。"""
    if diff is None:
        return []
    lines = ["", "## 与上一版的变化", "",
             "对照包：`%s`" % prev_name, "",
             "| 类别 | 数量 |", "|---|---|",
             "| 新增 | %d |" % len(diff["added"]),
             "| 内容变更 | %d |" % len(diff["changed"]),
             "| 删除 | %d |" % len(diff["removed"]),
             "| 未变 | %d |" % diff["identical"]]
    if diff["removed"]:
        lines += ["",
                  "> **本包比上一版少了 %d 个文件。** 打包脚本默认会拒绝出包，"
                  "这一包是加 `--allow-removals` 打出来的，即删除是打包人**明确认可**的。"
                  % len(diff["removed"])]
        if _looks_like_a_move(diff):
            lines.append("> 这个规模通常不是删文件，而是整棵树改名/搬迁"
                         "（2026-10-21 的布局上移就是一次）：下面「新增」里那些同名条目"
                         "就是它们的新位置。")
    # 索引的用途是「收包人一眼看出差在哪」。一次整树搬迁会让这里多出五百行，
    # 而五百行与四十行的信息量几乎一样（清单后半段还逐条列了包内全部文件），
    # 所以超过四十项就截断——完整的留在打包时的控制台输出里，两个包也都在。
    for title, key, mark in (("新增", "added", "+"), ("内容变更", "changed", "~"),
                             ("删除", "removed", "-")):
        rows = diff[key]
        if not rows:
            continue
        lines += ["", "%s %d 项：" % (title, len(rows)), ""]
        for row in rows[:40]:
            rel = row[0]
            if key == "added":
                lines.append("- `%s` %s %s" % (mark, rel, _kb(row[1][0])))
            elif key == "changed":
                lines.append("- `%s` %s %s → %s"
                             % (mark, rel, _kb(row[1][0]), _kb(row[2][0])))
            else:
                lines.append("- `%s` %s %s" % (mark, rel, _kb(row[1][0])))
        if len(rows) > 40:
            lines.append("- ……另有 %d 项未列出（完整清单在打包时的控制台输出里）"
                         % (len(rows) - 40))
    return lines


def desktop_dir() -> str:
    """真实桌面目录。

    不能只写 `os.path.join(os.path.expanduser("~"), "Desktop")`：桌面**可以被重定向**
    （注册表里的 Desktop 指到另一个目录），而 `expanduser` 只认 USERPROFILE，于是包会被
    写进用户目录下的 Desktop——一个存在、但不是用户看到的那个桌面的目录。
    症状是"发送到桌面"之后桌面上什么都没有，而命令还报成功。
    """
    if os.name == "nt":
        try:
            import winreg
            key = (r"Software\Microsoft\Windows\CurrentVersion"
                   r"\Explorer\User Shell Folders")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Desktop")
            path = os.path.expandvars(value)
            if os.path.isdir(path):
                return path
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


# ---- 测试条数：打在索引里，且是**实测**的 ----------------------------------
#
# 旧版把"工作台 137 + 引擎 54"写死在源码里，随代码演进变成 9.6 倍偏差
# （实测 1781 + 58）——一份说错自己规模的索引比没有索引更坏，因为它是可信的形状。
#
# 只**收集**不执行：`countTestCases()` 不跑用例。跑在子进程 + 临时工作目录里，
# 并带 `-B`，所以"打包"这个动作不会改动被交付的那棵树。
_COUNT_SNIPPET = (
    "import os, sys, unittest\n"
    "base = sys.argv[1]\n"
    "sys.path.insert(0, base)\n"
    "suite = unittest.TestLoader().discover(os.path.join(base, 'tests'))\n"
    "print(suite.countTestCases())\n"
)


def count_tests(base: str):
    """数一侧的测试条数（不执行）；失败返回 None。

    跑在子进程 + **临时工作目录**里，并带 `-B`/`PYTHONDONTWRITEBYTECODE`：
    "打包这个动作本身不改动被交付的树"是一条硬要求，而 import 测试模块是有副作用的。
    """
    if not os.path.isdir(os.path.join(base, "tests")):
        return None
    with tempfile.TemporaryDirectory(prefix="handoff-count-") as tmp:
        try:
            proc = subprocess.run(
                [sys.executable, "-B", "-c", _COUNT_SNIPPET, base],
                cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        except Exception:
            return None
    if proc.returncode != 0:
        return None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def build_index(included, skipped, label, counts, diff=None, prev_name=None) -> str:
    """生成包内索引：内容清单 + 排除说明 + 怎么跑起来。

    `diff` 非空时在开头加一段「与上一版的变化」：收包人手里的旧包和这个包差在哪，
    不该靠手动 `diff` 两份索引才看得出来。
    """
    total = 0
    sized = []
    for rel in included:
        try:
            size = os.path.getsize(os.path.join(REPO_ROOT, rel))
        except OSError:
            size = 0
        total += size
        sized.append((rel, size))

    platform_n, engine_n = counts
    if platform_n and engine_n:
        test_line = ("共 **%d** 项测试（工作台 %d + 发掘引擎 %d）。"
                     "这是打包时在本机**实测收集**的条数，不是写死的常量。"
                     % (platform_n + engine_n, platform_n, engine_n))
    else:
        test_line = ("条数以本机实测为准（打包时未能收集，请按下面命令自行统计）。")

    lines = [
        "# 交付包索引",
        "",
        "生成时间：%s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "包名：%s" % label,
        "内容：%d 个文件，%.2f MB（未压缩）" % (len(included), total / 1024 / 1024),
    ]
    lines += index_change_section(diff, prev_name)
    lines += [
        "",
        "## 这是什么",
        "",
        "一套**独立部署**的 AI 智能体渗透测试工作台，以及配套的发掘引擎、设计文档与",
        "过程文档。工作台本体零第三方依赖：Python 标准库 + 原生 JS/SVG，无构建步骤，",
        "内网离线可跑。（`docs/` 下的设计说明书重建脚本另需 python-docx，见目录说明。）",
        "",
        "## 怎么跑起来",
        "",
        "```bash",
        "python deploy/start.py      # 起控制台并打开浏览器",
        "```",
        "",
        "包内根目录就是 `%s/`，进去直接跑；工作台、发掘引擎、文档同在根下，"
        "没有第二层嵌套。" % label,
        "",
        "首次打开会要求创建管理员（**没有出厂口令**）。完整操作见 "
        "`使用手册.md`。",
        "",
        "## 目录说明",
        "",
        "| 路径 | 内容 |",
        "|---|---|",
        "| `pentestd/` `web/` `deploy/` | 工作台本体：控制面、Web 界面、部署脚本 |",
        "| `tests/` | 测试套件（见文末「自检」） |",
        "| `使用手册.md` | **使用说明**（从跑到扫出结果） |",
        "| `项目说明.md` | 功能总览、接口契约、测试说明 |",
        "| `架构设计.md` | 架构说明与数据流 |",
        "| `交互规范.md` | 交互规格（面向开发） |",
        "| `视觉设计系统.md` | 视觉令牌与组件基准 |",
        "| `视觉评审.md` | 视觉评审记录与优先级清单 |",
        "| `data/skills/` | 内置作业手册（技能库），产品内容，非运行期数据 |",
        "| `prototype/` | 漏洞发掘引擎（扫描、差分验证、报告） |",
        "| `docs/` | 过程文档：设计说明书成品（`design.docx` / `design.pdf`）及其源码"
        "（`d1.py`–`d5.py` 章节内容、`render.py` 渲染器、`base.docx` 模板）、"
        "架构决策记录（ADR）、集成说明。重建 docx 需 `pip install python-docx`，"
        "**不是工作台的运行依赖** |",
        "| `tools/cloudflared.exe` | 隧道用的 Cloudflare 客户端（54 MB，随包分发以免接收方另行下载） |",
        "",
        "## 本包**故意不含**的内容",
        "",
        "打包时排除了运行期数据，收到后请自行初始化：",
        "",
        "| 未包含 | 理由 |",
        "|---|---|",
        "| `data/`（`data/skills/` **已含**） | 其余部分含管理员口令哈希、"
        "模型密钥的加密密钥、节点令牌摘要、以及下载来的外部工具（66 MB）。这些是"
        "**运行期机密**，交付包不该携带 |",
        "| `memory/`、`prototype/memory/`、"
        "`prototype/out/` | "
        "运行期数据库与生成的报告 |",
        "| `*.key` `*.crt` `*.csr` `identity.json` | 证书与节点身份。证书应由接收方"
        "**用自己的 CA 重新签发**（`python -m pentestd pki init`），而不是沿用别人的 |",
        "| `__pycache__/`、`*.pyc`、`*.log` | 构建缓存与运行日志，与解释器版本/本机绑定 |",
        "| 开发期的点开头目录（`.scratch/` 等） | 探针脚本与截图转储，**不是**产品内容 |",
        "",
        "> 因此首次运行是干净状态：没有账号、没有模型配置、没有节点。",
        "> 目标机器上的执行节点也需要自行重新接入（旧令牌不随包分发）。",
        "",
        "## 自检",
        "",
        "```bash",
        "python -m unittest discover -s tests                    # 工作台",
        "cd prototype && python -m unittest discover -s tests",
        "```",
        "",
        test_line,
        "",
        "## 安全边界（务必阅读）",
        "",
        "- 默认只监听 `127.0.0.1`。对外暴露前**必须**置于带认证的反向代理之后并启用 TLS，"
        "详见 使用手册 第 10 章。",
        "- 授权说明字段只是**审计事实**，不构成门禁；请自行确保扫描行为已获授权。",
        "- 若部署方开启了强制放行（`authz.FORCE_OPEN`），侵入类动作的事实闸门会被跳过。"
        "启动横幅会逐字打印本次生效的授权姿态，**以横幅为准**，不要以本文档为准。",
        "",
        "## 文件清单",
        "",
        "共 %d 个文件，%.2f MB。" % (len(included), total / 1024 / 1024),
        "",
    ]
    for rel, size in sized:
        lines.append("- `%s`  (%.1f KB)" % (_norm(rel), size / 1024.0))

    if skipped:
        lines += ["", "## 排除明细", "",
                  "| 路径 | 理由 |", "|---|---|"]
        for rel, reason in skipped:
            lines.append("| `%s` | %s |" % (_norm(rel), reason))
    return "\n".join(lines) + "\n"


def verify_package(path: str, included, root: str) -> list:
    """打包后的强制自检。返回问题列表，空列表表示通过。

    判定方式是**集合相等**：包内条目必须恰好是 `{索引.md} ∪ 收录清单`。
    这比逐条列举"哪些后缀算敏感"强——列举是第二份名单，它会与收集规则漂移，
    而漂移的表现是"该排除的混进来了、校验还说通过"。
    """
    problems = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        expected = {"%s/索引.md" % root}
        for rel in included:
            expected.add("%s/%s" % (root, _norm(rel)))
        actual = set(names)
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        if extra:
            problems.append("包内出现未收录条目：%s" % (extra[:5],))
        if missing:
            problems.append("缺少条目：%s" % (missing[:5],))
        if len(names) != len(actual):
            problems.append("包内有重复条目")
        # 独立再走一遍排除规则（与收集同一个判定），兜住"收录清单本身就错了"
        bad = [n for n in names
               if n != "%s/索引.md" % root and excluded(n.split("/", 1)[-1])]
        if bad:
            problems.append("包内出现应排除的条目：%s" % (bad[:5],))
        broken = zf.testzip()
        if broken:
            problems.append("压缩流损坏：%s" % broken)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="打包交付内容为 zip")
    default_name = "ai-vuln-agent-%s.zip" % dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    parser.add_argument("--out", default=os.path.join(desktop_dir(), default_name))
    parser.add_argument("--label", default="ai-vuln-agent",
                        help="包内根目录名（与 zip 文件名无关）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-test-count", action="store_true",
                        help="跳过测试条数统计（会慢几秒，且会 import 测试模块）")
    parser.add_argument("--allow-removals", action="store_true",
                        help="上一版有、这一版确实没有（有意删除）；不加则拒绝出包")
    args = parser.parse_args()

    missing = missing_top()
    if missing:
        print("收录清单里有 %d 项在这棵树上不存在：%s"
              % (len(missing), "、".join(missing)))
        print("  → 要么目录被挪了（改 INCLUDE_TOP），要么站错了地方（REPO_ROOT=%s）"
              % REPO_ROOT)
        return 2

    included, skipped = collect()
    print("收录 %d 个文件，排除 %d 项" % (len(included), len(skipped)))
    print("-" * 62)
    print("排除明细（整棵剪掉的目录记一行）：")
    for rel, reason in skipped:
        print("  %-52s %s" % (rel[:52], reason))

    # 跨版本比对：与同目录里最新的同族旧包比。放在 `--dry-run` 之前，
    # 这样"先看一眼会变成什么样"这条路径也带着删失检查。
    prev_path, prev_entries = find_previous_package(args.out, args.label)
    diff = None
    print("-" * 62)
    if prev_path:
        diff = compare_versions(prev_entries, entry_signatures(included))
        for line in render_comparison(diff, prev_path):
            print(line)
    else:
        print("跨版本比对：同目录下没有同族旧包，跳过（首次打包就是这样）")

    if diff and diff["removed"] and not args.allow_removals:
        print("-" * 62)
        print("拒绝出包：%d 项在上一版里有、这一版没有。" % len(diff["removed"]))
        print("  确属有意删除 → 加 --allow-removals 再跑一次（删除清单会写进包内 索引）")
        print("  属于误删 → 先把文件找回来。这条检查就是为了这一刻不选错。")
        return 3

    if args.dry_run:
        print("-" * 62)
        for rel in included:
            print("  " + rel)
        return 0

    counts = (None, None)
    if not args.no_test_count:
        counts = (count_tests(PLATFORM_ROOT), count_tests(
            os.path.join(PLATFORM_ROOT, "prototype")))
        print("-" * 62)
        print("测试条数（实测收集）：工作台 %s · 发掘引擎 %s"
              % (counts[0] or "?", counts[1] or "?"))

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if not os.path.isdir(out_dir):
        print("输出目录不存在：%s" % out_dir)
        return 1
    index = build_index(included, skipped, os.path.basename(args.out), counts,
                        diff=diff,
                        prev_name=os.path.basename(prev_path) if prev_path else None)
    # 先写临时文件，校验通过再原子替换到目标路径。
    # 直接往桌面/同步目录写，一旦中途被打断就会留下一个"能打开但内容不全"的包——
    # 这种半成品比没有更难发现（解压不报错，只是少了文件）。
    staging = args.out + ".part"
    root = args.label
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("%s/索引.md" % root, index)
        for rel in included:
            zf.write(os.path.join(REPO_ROOT, rel),
                     "%s/%s" % (root, _norm(rel)))

    problems = verify_package(staging, included, root)
    if problems:
        os.remove(staging)
        print("校验未通过，已丢弃临时包：")
        for item in problems:
            print("  - %s" % item)
        return 1
    os.replace(staging, args.out)

    size = os.path.getsize(args.out)
    print("-" * 62)
    print("已生成：%s" % args.out)
    print("大小：%.2f MB（%d 个文件 + 索引.md）"
          % (size / 1024 / 1024, len(included)))
    print("自检：条目集合相等、排除规则复核、压缩流完整性 均通过")
    if diff is None:
        print("跨版本比对：无旧包可对照（首次打包）")
    else:
        print("跨版本比对：新增 %d · 变更 %d · 删除 %d · 未变 %d"
              % (len(diff["added"]), len(diff["changed"]),
                 len(diff["removed"]), diff["identical"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
