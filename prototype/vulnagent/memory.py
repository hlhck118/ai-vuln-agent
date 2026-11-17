# -*- coding: utf-8 -*-
"""经验记忆库（SQLite）。

落地三件事：
  1. 记录每一次任务的假设与最终判定，形成可追溯的案例；
  2. 提供相似案例检索，把历史成功路径当先验、历史失败路径当排除条件；
  3. 沉淀"缺陷模式 -> 修复模式"，使同类问题第二次出现时无需从零推理。

只依赖标准库 sqlite3，便于离线私有化部署与随平台一起备份迁移。
"""
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    target_name TEXT,
    target_hash TEXT,
    started_at  REAL,
    finished_at REAL,
    stats_json  TEXT
);
CREATE TABLE IF NOT EXISTS hypotheses (
    hyp_id     TEXT PRIMARY KEY,
    run_id     TEXT,
    cwe        TEXT,
    title      TEXT,
    file       TEXT,
    symbol     TEXT,
    line       INTEGER,
    confidence REAL,
    status     TEXT,
    rationale  TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
    hyp_id    TEXT,
    verdict   TEXT,
    mode      TEXT,
    note      TEXT,
    evidence  TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS patterns (
    cwe        TEXT,
    signature  TEXT,
    fix_hint   TEXT,
    hits       INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen  REAL,
    PRIMARY KEY (cwe, signature)
);
CREATE INDEX IF NOT EXISTS idx_hyp_cwe ON hypotheses(cwe);
CREATE INDEX IF NOT EXISTS idx_verdict_hyp ON verdicts(hyp_id);
"""

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def tokenize(code: str) -> set:
    return {t.lower() for t in TOKEN_RE.findall(code or "")}


class MemoryStore:
    def __init__(self, path: str, enabled: bool = True, top_k: int = 3):
        self.enabled = enabled
        self.top_k = top_k
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None
        if not enabled:
            return
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(DDL)
        self.conn.commit()

    # ------------------------------------------------------------ 写入
    def start_run(self, run_id: str, target_name: str, target_hash: str) -> None:
        if not self.conn:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(run_id,target_name,target_hash,started_at) "
            "VALUES(?,?,?,?)", (run_id, target_name, target_hash, time.time()))
        self.conn.commit()

    def finish_run(self, run_id: str, stats: Dict[str, Any]) -> None:
        if not self.conn:
            return
        self.conn.execute("UPDATE runs SET finished_at=?, stats_json=? WHERE run_id=?",
                          (time.time(), json.dumps(stats, ensure_ascii=False), run_id))
        self.conn.commit()

    def record_hypothesis(self, run_id: str, hyp) -> None:
        if not self.conn:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO hypotheses"
            "(hyp_id,run_id,cwe,title,file,symbol,line,confidence,status,rationale) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (hyp.hyp_id, run_id, hyp.cwe, hyp.title, hyp.file, hyp.sink_symbol,
             hyp.line, hyp.confidence, hyp.status, hyp.rationale))
        self.conn.commit()

    def record_verdict(self, hyp_id: str, verdict: str, mode: str,
                       note: str, evidence: Any) -> None:
        if not self.conn:
            return
        self.conn.execute(
            "INSERT INTO verdicts(hyp_id,verdict,mode,note,evidence,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (hyp_id, verdict, mode, note,
             json.dumps(evidence, ensure_ascii=False)[:4000], time.time()))
        self.conn.execute("UPDATE hypotheses SET status=? WHERE hyp_id=?",
                          (verdict, hyp_id))
        self.conn.commit()

    def remember_pattern(self, cwe: str, signature: str, fix_hint: str) -> None:
        if not self.conn:
            return
        now = time.time()
        self.conn.execute(
            "INSERT INTO patterns(cwe,signature,fix_hint,hits,first_seen,last_seen) "
            "VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(cwe,signature) DO UPDATE SET hits=hits+1,last_seen=excluded.last_seen",
            (cwe, signature, fix_hint, now, now))
        self.conn.commit()

    # ------------------------------------------------------------ 检索
    def search_similar(self, cwe: str, code: str,
                       top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """相似案例检索：同类缺陷优先，再按代码标识符 Jaccard 相似度排序。"""
        if not self.conn:
            return []
        limit = top_k or self.top_k
        rows = self.conn.execute(
            "SELECT h.hyp_id,h.cwe,h.title,h.rationale,h.status,"
            "       (SELECT verdict FROM verdicts v WHERE v.hyp_id=h.hyp_id "
            "        ORDER BY created_at DESC LIMIT 1) AS verdict "
            "FROM hypotheses h WHERE h.cwe=? ORDER BY h.rowid DESC LIMIT 200",
            (cwe,)).fetchall()
        tokens = tokenize(code)
        scored: List[Dict[str, Any]] = []
        for hyp_id, row_cwe, title, rationale, status, verdict in rows:
            hay = tokenize("%s %s" % (title, rationale))
            inter = len(tokens & hay)
            union = len(tokens | hay) or 1
            score = inter / union
            if verdict == "verified":
                score += 0.15          # 历史确认过的路径优先
            scored.append({"hyp_id": hyp_id, "cwe": row_cwe, "title": title,
                           "verdict": verdict or status, "score": round(score, 3)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def fix_hints(self, cwe: str, limit: int = 3) -> List[Dict[str, Any]]:
        if not self.conn:
            return []
        rows = self.conn.execute(
            "SELECT signature,fix_hint,hits FROM patterns WHERE cwe=? "
            "ORDER BY hits DESC LIMIT ?", (cwe, limit)).fetchall()
        return [{"signature": s, "fix_hint": f, "hits": h} for s, f, h in rows]

    def stats(self) -> Dict[str, Any]:
        if not self.conn:
            return {"enabled": False}
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])
        return {
            "enabled": True,
            "runs": scalar("SELECT COUNT(*) FROM runs"),
            "hypotheses": scalar("SELECT COUNT(*) FROM hypotheses"),
            "verified": scalar("SELECT COUNT(*) FROM verdicts WHERE verdict='verified'"),
            "refuted": scalar("SELECT COUNT(*) FROM verdicts WHERE verdict='refuted'"),
            "patterns": scalar("SELECT COUNT(*) FROM patterns"),
        }

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None
