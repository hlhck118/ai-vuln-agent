"""临时探针 2：单个 engagement 的布局尺寸，以及合并视图在不同窗口下的尺寸。

用来定"合并视图的默认窗口取多少"——不靠感觉，靠实测的宽高比。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pentestd import graph, models, store  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "pentest.db")
st = store.Store(DB)


def ratio(w, h):
    return h / max(w, 1)


print("=== 单个 engagement（现状：用户一直这么看） ===")
rows = []
for meta in st.list_engagements(limit=10000):
    eid = meta["id"]
    view = st.view(eid, 200)
    nodes = ([view["goal"]] if view.get("goal") else [])
    for key in ("intents", "facts", "findings", "assets"):
        nodes.extend(view["nodes"].get(key, []))
    layout = graph.build_layout([n for n in nodes if n], view["edges"])
    rows.append((len(nodes), layout["width"], layout["height"], eid))
rows.sort(reverse=True)
for n, w, h, eid in rows[:5]:
    print("  %-14s nodes=%4d  %.0f x %.0f  (1 : %.1f)" % (eid, n, w, h, ratio(w, h)))
print("  ... 共 %d 个，中位 nodes=%d" % (len(rows), rows[len(rows) // 2][0]))

print()
print("=== 合并视图 + 全局窗口（取最近 N 个节点） ===")
merged = []
for meta in st.list_engagements(limit=10000):
    eid = meta["id"]
    for node in st.list_nodes(eid):
        raw = models.public_node(node)
        raw["id"] = "%s:%s" % (eid, node["id"])
        raw["_eid"] = eid
        merged.append(raw)

edges_by_eid = {}
for meta in st.list_engagements(limit=10000):
    eid = meta["id"]
    edges_by_eid[eid] = [{
        "id": "%s:%s" % (eid, e["id"]), "kind": e["kind"],
        "sourceId": "%s:%s" % (eid, e["sourceId"]),
        "targetId": "%s:%s" % (eid, e["targetId"]),
    } for e in st.list_edges(eid)]

merged.sort(key=lambda n: n.get("createdAt") or 0, reverse=True)
for window in (150, 300, 600, 1200):
    kept = merged[:window]
    keep_ids = {n["id"] for n in kept}
    eids = {n["_eid"] for n in kept}
    edges = [e for eid in eids for e in edges_by_eid[eid]
             if e["sourceId"] in keep_ids and e["targetId"] in keep_ids]
    public = [{k: v for k, v in n.items() if k != "_eid"} for n in kept]
    layout = graph.build_layout(public, edges)
    cols = {}
    for n in layout["nodes"]:
        cols[round(n["x"])] = cols.get(round(n["x"]), 0) + 1
    print("  N=%4d  节点 %4d  边 %4d  %.0f x %.0f  (1 : %.1f)  列数=%d 最高列=%d"
          % (window, len(layout["nodes"]), len(layout["edges"]),
             layout["width"], layout["height"], ratio(layout["width"], layout["height"]),
             len(cols), max(cols.values()) if cols else 0))
