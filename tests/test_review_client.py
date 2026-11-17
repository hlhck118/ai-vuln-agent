# -*- coding: utf-8 -*-
"""审查单卡片：它必须**不依赖那一条 SSE 流**。

这一组是静态契约（读源码），因为它要钉住的是一个**结构**事实：

  卡片的内容只有一个来源 —— `view.reviews`；
  而 `view.reviews` 有两个来源 —— SSE 那两帧，以及每次载入会话时的
  `GET /reviews?status=pending`。

只要第二个来源在，刷新就不会丢卡片。它一掉，症状就是这一版要修的那个死角：
刷新之后卡片不见了，而单子还在库里等人批，想发下一轮又被入口闸以 409 挡住。

真正"丢一次真连接"的端到端验证在 `tests/test_review_survives_reload.py`；
这里守的是界面这一侧的结构，两条互为补充。
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def strip_comments(text: str) -> str:
    """去掉块注释与整行 `//` 注释。

    这一条是**必需品**，不是洁癖：这个仓库里已经三次出现"`assertNotIn` 命中了
    自己那句解释性注释"（`.brand-mark`、`text-stroke`、`disabled = locked`）。
    症状是注释写得越清楚、测试越红，而修法会退化成把注释删掉 —— 那等于把
    "为什么这么做"从代码里赶出去。
    只去**整行**的 `//`：字符串里可能有 `http://`。
    """
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in without_block.splitlines()
                     if not line.strip().startswith("//"))


def slice_function(js: str, name: str) -> str:
    """取一个函数体（从 `function <name>(` 到下一个两空格缩进的 `}`）。"""
    return js.split("function %s(" % name)[1].split("\n  }")[0]


class ReviewCardTestCase(unittest.TestCase):

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as handle:
            return handle.read()

    # ---- 卡片在哪

    def test_the_card_is_drawn_into_the_transcript(self):
        """t80：卡片从"输入框上方那一格"搬进**对话记录**，位置就是那一轮该在的地方。

        DSH 桌面端把审批卡画在对话里；这里跟同一个形态。撤掉那一格之后，
        `#chat-review-slot` 不该再出现在任何地方——留着它就是第二份真相。
        """
        html = self._read("web", "index.html")
        self.assertNotIn('id="chat-review-slot"', html,
                         "卡片搬进记录里了，输入框上方不再有一格")
        js = strip_comments(self._read("web", "chat.js"))
        render = slice_function(js, "renderReviews")
        self.assertIn("dom.log", render, "卡片画在 #chat-log 末尾")
        self.assertIn("chat-review", render)

    def test_the_card_is_not_a_fixed_band_with_its_own_scroll(self):
        """记录区自己滚，卡片只是其中一条 —— 不再有自己的封顶高度与内部滚动。

        封顶 + 内部滚是"输入框上方那一格"的几何；搬进记录之后还留着它，
        一张长卡片会在记录里再开一个小滚动条。
        """
        css = self._read("web", "style.css")
        self.assertNotIn(".chat-review-slot {", css,
                         "那一格的样式应当整段撤掉")
        self.assertIn(".chat-log .chat-review", css,
                      "记录里与消息同一列，左右留白沿用卡片自己的 margin")
        block = css.split(".chat-log .chat-review {")[1].split("}")[0]
        self.assertNotIn("max-height", block,
                         "记录区自己滚，卡片不封顶")
        self.assertNotIn("overflow", block)

    # ---- 卡片从哪来

    def test_the_pending_intro_says_the_turn_is_waiting(self):
        """卡片上必须有一句"这一轮停在这里等"：批的是什么、不批会怎样。"""
        js = strip_comments(self._read("web", "chat.js"))
        render = slice_function(js, "renderReviews")
        self.assertIn("chat-review-intro", render)
        self.assertIn("停在这里", render)

    def test_the_session_load_asks_the_server_for_pending_reviews(self):
        js = self._read("web", "chat.js")
        self.assertIn("/reviews?status=pending", js)
        session = slice_function(js, "openSession")
        self.assertIn("await loadReviews()", session,
                      "刷新之后卡片唯一的来源就是这一次取数")

    def test_the_card_does_not_live_in_the_live_bubble(self):
        """气泡是纯客户端状态。卡片挂在里面，刷新一次就没了。"""
        js = strip_comments(self._read("web", "chat.js"))
        self.assertNotIn("live.reviews", js)
        self.assertNotIn("reviewBlock", js)
        self.assertIn("view.reviews[review.id]", js)

    def test_the_load_replaces_rather_than_merges(self):
        """服务端说的是"现在还有这些"，本地那份可能含已经裁决过的单子。"""
        js = strip_comments(self._read("web", "chat.js"))
        load = slice_function(js, "loadReviews")
        self.assertIn("view.reviews = {}", load)
        self.assertIn("view.reviewOrder = []", load)

    def test_a_settled_row_is_not_put_back_on_the_card_slot(self):
        """查询写的是 `status=pending`，但那不等于"还在等人"。

        推着单子走的是等它的那个进程。进程一死（控制面重启、崩溃），库里那一行就
        停在 `pending`，而服务端会把它按 `expired` 报回来（`approval.public` 改的
        是呈现、不是库）。这一格放的是"现在要你回答的"，已经作废的那些不该混进来
        —— 混进来只会多一张永远不动的卡片，而它上面刚印着「到时间会自动收掉」。
        """
        js = strip_comments(self._read("web", "chat.js"))
        load = slice_function(js, "loadReviews")
        self.assertIn("row.pending === false", load,
                      "过期未收尾的单子由服务端按 pending:false 报回来，"
                      "这一格必须把它们挡在外面")

    # ---- 按钮什么时候给

    def test_buttons_only_when_somebody_is_waiting(self):
        """没有等待者的单子**不给按钮**。

        批准它不会有命令执行（发起它的那一轮已经不在了）。画一个按下去什么都不
        发生的按钮比不画更坏 —— 使用者会以为它跑了。服务端在 `decide_review` 里
        也会把这种裁决拒成 409，两处判据同一个来源（`approval.awaited`）。
        """
        js = strip_comments(self._read("web", "chat.js"))
        card = slice_function(js, "reviewCard")
        self.assertIn("review.pending && review.awaiting", card)
        self.assertIn("chat-review-orphan", js,
                      "没有等待者的那些要有一句如实的话，而不是静默消失")

    def test_the_card_no_longer_claims_a_refresh_voids_it(self):
        """那句话曾经是反的，而且会让人白白去等超时。"""
        js = strip_comments(self._read("web", "chat.js"))
        card = slice_function(js, "reviewCard")
        self.assertIn("还会在", card, "要说清刷新不会弄丢它")
        self.assertNotIn("离开这一页", card)

    # ---- 停止要结掉单子

    def test_stop_settles_the_pending_review_instead_of_leaving_it(self):
        """只 `abort()` 不会让服务端的等待结束 —— 等待期间它一个字节都不写，
        所以看不见连接断开，那一轮会停在审查单上直到超时，而入口闸在那段时间里
        会挡住下一轮。"""
        js = strip_comments(self._read("web", "chat.js"))
        stop = slice_function(js, "stopTurn")
        self.assertIn("denyPendingReviews()", stop)
        deny = slice_function(js, "denyPendingReviews")
        self.assertIn("approve: false", deny)
        self.assertIn("row.awaiting", deny, "只结掉**还等着**的那些")


if __name__ == "__main__":
    unittest.main(verbosity=2)
