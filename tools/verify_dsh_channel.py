# -*- coding: utf-8 -*-
"""DSH 融合通道的自检：一条命令回答"它现在能不能用、到底在替谁干活"。

三种用法，默认那一种**只读**（不启动任何进程、不发任何请求）：

    python tools/verify_dsh_channel.py                 # 状态
    python tools/verify_dsh_channel.py --patch         # 刷新运行时工具补丁（只动平台自己的 home）
    python tools/verify_dsh_channel.py --turn "在吗"    # 真跑一轮（用模型额度）

为什么要有它：这条通道的失败方式几乎都是**静默**的 —— 补丁不在、档位没生效、
会话跨进程不可复用、工具行没有名字。它们都不会抛异常，只会让"看起来在跑"和
"真的在跑"变成同一件事，而下一步该做什么完全取决于这个区别。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import (chat, dsh_bridge, dsh_runtime, scope as scope_module,   # noqa: E402
                      store as store_module, toolclient)


def _store():
    db = os.environ.get("PENTEST_DB") or os.path.join(ROOT, "data", "pentest.db")
    return store_module.Store(db)


def report_state(store, home: str) -> int:
    state = dsh_runtime.state(store, home, ROOT)
    print("== 运行时通道 ==")
    print("  模式        : %s（%s）" % (state["mode"] or "(空)", "已打开" if state["enabled"] else "关闭"))
    print("  profile     : %s" % state["profile"])
    print("  项目内 DSH  : %s" % state["home"])
    print("  启动器      : %s" % (dsh_runtime.locate_dsh()
                                  or "(项目里没有 DSH；先跑 vendor\\dsh\\install.cmd)"))
    print("  SDK 面补丁  : %s" % state["patch"])
    print("  可自建补丁  : %s" % state["ownSdkPatch"])
    print("  可用        : %s%s" % (state["available"],
                                    "" if state["available"] else " —— " + state["reason"]))
    print("  运行中的通道: %d 条" % state["running"])
    if state["serverInfo"]:
        print("  已握手      : %s" % json.dumps(state["serverInfo"].get("serverInfo") or {},
                                                ensure_ascii=False))
    bridge = dsh_bridge.status(store, home, ROOT)
    print("\n== 平台工具面（随运行时通道挂载）==")
    print("  档位        : %s（可选 %s）" % (bridge["tier"], "、".join(bridge["tierOptions"])))
    print("  暴露工具    : %d 个" % bridge["exposedCount"])
    print("  运行时补丁  : %s%s" % (bridge["patchPath"],
                                    "" if bridge["patchPresent"] else "（不存在，下一条通道会生成）"))
    print("  归本平台所有: %s" % bridge["ownsPatch"])
    print("  桥脚本      : %s%s" % (bridge["scriptPath"],
                                    "" if bridge["scriptPresent"] else "（缺失！）"))
    ok = bool(bridge["scriptPresent"]) and (not state["enabled"] or state["available"])
    print("\n结论：%s" % ("这条通道现在可用。" if ok else "这条通道还缺件，见上面标出的行。"))
    return 0 if ok else 1


def write_patch(store, home: str) -> int:
    outcome = dsh_bridge.write(store, home, ROOT)
    print("刷新运行时补丁：%s" % json.dumps(outcome, ensure_ascii=False))
    return 0 if outcome.get("ok") else 1


def run_turn(store, home: str, question: str) -> int:
    profile = store.active_profile() or {}
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip() or str(
        profile.get("apiKey") or "").strip()
    if not key:
        print("需要一个可用的模型档（带密钥）或 DEEPSEEK_API_KEY —— "
              "否则这一轮发不出去。")
        return 2
    model = {"name": profile.get("name") or "", "provider": profile.get("provider") or "",
             "model": profile.get("model") or "", "apiKey": key,
             "contextWindow": profile.get("contextWindow") or 0}
    plane = toolclient.build_plane(store, scope_module.DEFAULT_SCOPE, (), ())
    print("== 跑一轮（会话 verify-%d）==" % os.getpid())
    events = []
    for event in chat.run_turn_stream(
            plane, model, key, [{"role": "user", "text": question}],
            session={"sessionId": "verify-%d" % os.getpid(), "userId": "verify"},
            policy_id="full", role="admin"):
        kind = event.get("type")
        events.append(kind)
        if kind == "turn":
            print("  turn     : runtime=%s tools=%s" % (event.get("runtime"),
                                                        event.get("tools")))
        elif kind == "notice":
            print("  notice[%s]: %s" % (event.get("kind"), event.get("message")))
        elif kind == "tool":
            print("  tool     : %s ok=%s" % (event.get("name"), event.get("ok")))
        elif kind == "done":
            print("  done     : finish=%s usage=%s text=%s"
                  % (event.get("finishStatus"), event.get("usage"),
                     str(event.get("text"))[:120].replace("\n", " ")))
        elif kind == "error":
            print("  error    : %s" % event.get("error"))
    print("  事件序列 : %s" % " → ".join(events))
    return 0 if events and events[-1] == "done" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="DSH 融合通道自检")
    parser.add_argument("--turn", metavar="问题", default="",
                        help="真跑一轮对话（会用模型额度）")
    parser.add_argument("--patch", action="store_true",
                        help="刷新运行时工具补丁（只动平台自己的 DSH home）")
    args = parser.parse_args()
    store = _store()
    home = os.path.join(ROOT, "data", "dsh-home")
    try:
        if args.patch:
            code = write_patch(store, home)
            if code:
                return code
        if args.turn:
            return run_turn(store, home, args.turn)
        return report_state(store, home)
    finally:
        dsh_runtime.close_channels()
        store.close()


if __name__ == "__main__":
    sys.exit(main())
