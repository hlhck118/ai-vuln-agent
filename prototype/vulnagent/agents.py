# -*- coding: utf-8 -*-
"""智能体层：七个角色 + 共享黑板。

角色与方案书一致：侦察、规划、审计、反证、验证、利用链、报告。
协作机制的四条约束在这里落地：
  * 显式预算：每步都检查预算，超限即停并转人工；
  * 证据绑定：假设必须挂载静态/图谱证据，反证必须给出理由；
  * 交叉验证：审计与反证成对出现，验证以沙箱执行结果为准；
  * 失败升级：无法自动判定的一律标记为待人工确认，不伪装成结论。
"""
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .analysis import parse_target
from .domain import (AttackChain, CodeSlice, Evidence, Finding, GraphSummary,
                     Hypothesis, ScanResult, Target, Verification, max_severity)
from .llm import HypothesisContext

REMEDIATION = {
    "CWE-89": "改用参数化查询（占位符绑定），禁止用拼接或格式化构造 SQL；"
              "对查询接口做统一封装，避免绕过。",
    "CWE-78": "关闭 shell（shell=False）并改用参数数组传参；"
              "对外部输入做白名单校验，禁止直接拼接命令行。",
    "CWE-22": "对外部传入的文件名只取 basename，并用 realpath 校验最终路径必须位于"
              "允许目录之内；优先使用文件 ID 映射代替路径拼接。",
    "CWE-502": "禁止对不可信数据使用 pickle/marshal；改用 JSON 等纯数据格式，"
               "并对反序列化结果做结构校验。",
    "CWE-327": "口令场景改用 PBKDF2、bcrypt 或 Argon2 加盐派生；"
               "确需摘要时使用 SHA-256 及以上算法。",
    "CWE-798": "凭据改由密钥管理服务或环境变量注入，源码中不得出现明文；"
               "同时轮换已泄露凭据并检查历史提交。",
    "CWE-95": "禁止对动态内容使用 eval/exec；改用显式映射表或解析器。",
    "CWE-79": "所有输出到 HTML 的外部数据统一经上下文相关的转义处理（如 html.escape）；"
              "优先使用模板引擎的自动转义能力，避免手工拼接。",
    "CWE-918": "限制请求协议为 http/https，并对目标主机做白名单校验；"
               "禁止跟随重定向到非白名单地址，必要时通过网络出口策略兜底。",
}

CHAIN_RULES = [
    (("CWE-78", "CWE-502", "CWE-95"), "CWE-798",
     "凭据泄露叠加远程代码执行：攻击者可先取得硬编码凭据，再借助命令执行或反序列化"
     "在服务器上落地，形成完整的失陷链。", "critical"),
    (("CWE-89",), "CWE-798",
     "凭据泄露叠加数据注入：硬编码凭据为攻击者提供横向移动条件，"
     "SQL 注入可直接窃取或篡改业务数据。", "high"),
    (("CWE-22",), "CWE-798",
     "凭据泄露叠加路径穿越：可通过读取配置文件进一步拿到更多凭据，扩大影响面。", "high"),
]


# ---------------------------------------------------------------- 黑板

@dataclass
class Blackboard:
    """共享黑板：所有智能体只在这里交换结构化结论。"""
    run_id: str
    target_root: str
    target: Optional[Target] = None
    graph: Optional[GraphSummary] = None
    units: Dict[str, Any] = field(default_factory=dict)
    slices: List[CodeSlice] = field(default_factory=list)
    hypotheses: List[Hypothesis] = field(default_factory=list)
    verifications: List[Verification] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    chains: List[AttackChain] = field(default_factory=list)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    _hyp_seq: int = 0
    _find_seq: int = 0

    def next_hyp_id(self) -> str:
        self._hyp_seq += 1
        return "H-%04d" % self._hyp_seq

    def next_finding_id(self) -> str:
        self._find_seq += 1
        return "F-%03d" % self._find_seq

    def elapsed_ms(self) -> int:
        """距本次扫描开始的毫秒数；轨迹带上时间轴才叫执行轨迹。"""
        return int((time.time() - self.started_at) * 1000)

    def log(self, agent: str, message: str, **extra) -> None:
        entry = {"agent": agent, "message": message, "ts": self.elapsed_ms()}
        entry.update(extra)
        self.timeline.append(entry)

    def slices_of(self, path: str, symbol: str) -> List[CodeSlice]:
        return [s for s in self.slices if s.path == path and s.symbol == symbol]


# ---------------------------------------------------------------- 智能体基类

class BaseAgent:
    name = "base"

    def __init__(self, ctx: "AgentContext"):
        self.ctx = ctx

    def run(self, bb: Blackboard) -> None:      # pragma: no cover - 抽象方法
        raise NotImplementedError


@dataclass
class AgentContext:
    """智能体共享依赖（由编排器注入）。"""
    config: Any
    reasoner: Any
    verifier: Any
    tools: Any
    memory: Any
    budget: Any
    critic: Any = None      # 反证角色专用推理器；缺省回退到 reasoner


# ---------------------------------------------------------------- 侦察智能体

class ReconAgent(BaseAgent):
    name = "recon"

    def run(self, bb: Blackboard) -> None:
        cfg = self.ctx.config
        payload = self.ctx.tools.invoke(
            "repo_analyze", root=bb.target_root, exts=cfg.run.languages,
            exclude_dirs=cfg.run.exclude_dirs, max_bytes=cfg.run.max_file_bytes)
        if not isinstance(payload, dict) or "error" in payload:
            raise RuntimeError("侦察阶段失败：%r" % (payload,))
        repo, graph = payload["repo"], payload["graph"]
        units, slices = payload["units"], payload["slices"]
        bb.target = Target(name=os.path.basename(os.path.abspath(bb.target_root)),
                           root=repo.root, languages=repo.languages,
                           file_count=len(repo.files), loc=repo.loc,
                           hash=repo.hash)
        bb.graph = graph
        bb.units = units
        bb.slices = slices
        bb.log(self.name, "仓库测绘完成",
               files=len(repo.files), loc=repo.loc,
               functions=graph.functions, sinks=len(graph.sinks),
               entries=len(graph.entry_points), slices=len(slices))


# ---------------------------------------------------------------- 规划智能体

class PlannerAgent(BaseAgent):
    name = "planner"

    def run(self, bb: Blackboard) -> None:
        ordered = sorted(bb.slices, key=lambda s: s.risk_score, reverse=True)
        bb.slices = ordered
        budget = self.ctx.budget
        plan = []
        for idx, sl in enumerate(ordered):
            if idx >= budget.max_slices:
                break
            plan.append({"slice_id": sl.slice_id, "symbol": sl.symbol,
                         "risk": sl.risk_score})
        budget.planned = len(plan)
        bb.notes.append("审计计划：按风险分从高到低处理 %d 个切片" % len(plan))
        bb.log(self.name, "生成审计计划", planned=len(plan),
               skipped=max(0, len(ordered) - len(plan)))


# ---------------------------------------------------------------- 审计智能体

class AuditorAgent(BaseAgent):
    name = "auditor"

    def run(self, bb: Blackboard) -> None:
        reasoner = self.ctx.reasoner
        memory = self.ctx.memory
        for sl in bb.slices:
            if not self.ctx.budget.can_continue():
                bb.notes.append("预算耗尽，审计阶段提前结束")
                break
            self.ctx.budget.spend_round()
            key = "%s::%s" % (sl.path, sl.symbol)
            unit = bb.units.get(key)
            sinks = list(unit.sinks) if unit else []
            ctx = HypothesisContext(
                slice=sl, sinks=sinks,
                reachable=key in (bb.graph.reachable_from_entry or []),
                sanitizers=list(getattr(unit, "sanitizers", []) or []),
                unresolved_calls=int(getattr(unit, "unresolved_calls", 0) or 0),
                memory_hits=memory.search_similar(str(sinks[0].cwe) if sinks else "",
                                                  sl.code) if sinks else [])
            items = reasoner.propose(ctx)
            made = 0
            for raw in items:
                if made >= self.ctx.budget.max_hypotheses_per_slice:
                    break
                if self._duplicate(bb, raw, sl):
                    continue
                hyp = self._to_hypothesis(bb, raw, sl, ctx)
                bb.hypotheses.append(hyp)
                memory.record_hypothesis(bb.run_id, hyp)
                made += 1
            self.ctx.budget.spend_tool()
            bb.log(self.name, "切片审计完成", slice=sl.slice_id,
                   symbol=sl.symbol, hypotheses=made)

    def _duplicate(self, bb: Blackboard, raw: Dict[str, Any], sl: CodeSlice) -> bool:
        for hyp in bb.hypotheses:
            if (hyp.cwe == raw.get("cwe") and hyp.file == sl.path
                    and int(hyp.line) == int(raw.get("line", 0))):
                return True
        return False

    def _to_hypothesis(self, bb: Blackboard, raw: Dict[str, Any],
                       sl: CodeSlice, ctx: HypothesisContext) -> Hypothesis:
        line = int(raw.get("line", sl.start_line))
        hyp = Hypothesis(
            hyp_id=bb.next_hyp_id(), slice_id=sl.slice_id,
            cwe=str(raw.get("cwe", "")), title=str(raw.get("title", "")),
            file=sl.path, line=line, sink_symbol=sl.symbol,
            source=str(raw.get("source", "")),
            preconditions=list(raw.get("preconditions") or []),
            confidence=float(raw.get("confidence", 0.5)),
            severity_guess=str(raw.get("severity", "medium")),
            rationale=str(raw.get("rationale", "")), producer=self.name)
        hyp.evidence.append(Evidence(kind="static",
                                     ref="%s:%d" % (os.path.basename(sl.path), line),
                                     detail=hyp.rationale[:200]))
        hyp.evidence.append(Evidence(kind="graph", ref=sl.slice_id,
                                     detail="；".join(sl.graph_context)))
        for hit in ctx.memory_hits[:2]:
            hyp.evidence.append(Evidence(kind="memory", ref=hit.get("hyp_id", ""),
                                         detail="历史案例：%s（判定：%s）"
                                                % (hit.get("title", ""),
                                                   hit.get("verdict", ""))))
        return hyp


# ---------------------------------------------------------------- 反证智能体

class CriticAgent(BaseAgent):
    name = "critic"

    def run(self, bb: Blackboard) -> None:
        # 反证角色取自己的推理器（缺省与审计同源，但路由各自留痕，
        # 报告里才能回答"这条假设是被谁用什么实现驳回的"）
        reasoner = self.ctx.critic or self.ctx.reasoner
        for hyp in list(bb.hypotheses):
            if hyp.status != "proposed":
                continue
            if not self.ctx.budget.can_continue():
                bb.notes.append("预算耗尽，反证阶段提前结束")
                break
            key = "%s::%s" % (hyp.file, hyp.sink_symbol)
            unit = bb.units.get(key)
            sl = bb.slices_of(hyp.file, hyp.sink_symbol)
            if not sl:
                continue
            ctx = HypothesisContext(slice=sl[0],
                                    sinks=list(unit.sinks) if unit else [],
                                    reachable=key in (bb.graph.reachable_from_entry or []),
                                    sanitizers=list(getattr(unit, "sanitizers", []) or []),
                                    unresolved_calls=int(getattr(unit, "unresolved_calls", 0) or 0))
            verdict = reasoner.challenge(hyp.to_dict(), ctx)
            hyp.status = "challenged"
            if verdict.get("keep") is False:
                hyp.status = "dropped"
                hyp.rationale += "｜反证结论：%s" % verdict.get("reason", "")
            else:
                try:
                    hyp.confidence = float(verdict.get("confidence", hyp.confidence))
                except (TypeError, ValueError):
                    pass
                if verdict.get("reason"):
                    hyp.evidence.append(Evidence(kind="critic", ref=self.name,
                                                 detail=str(verdict["reason"])))
            bb.log(self.name, "假设质询完成", hyp=hyp.hyp_id, keep=bool(verdict.get("keep")),
                   confidence=hyp.confidence)


# ---------------------------------------------------------------- 验证智能体

class VerifierAgent(BaseAgent):
    name = "verifier"

    def run(self, bb: Blackboard) -> None:
        keep = [h for h in bb.hypotheses if h.status == "challenged"]
        keep.sort(key=lambda h: h.confidence, reverse=True)
        for hyp in keep:
            if not self.ctx.budget.can_continue():
                hyp.status = "inconclusive"
                bb.notes.append("预算耗尽，%s 未完成动态验证，转人工确认" % hyp.hyp_id)
                continue
            self.ctx.budget.spend_tool()
            outcome = self.ctx.verifier.verify(hyp, bb.target_root)
            verification = Verification(
                hyp_id=hyp.hyp_id, verdict=outcome["verdict"], mode=outcome["mode"],
                poc_path="poc_%s.py" % hyp.hyp_id,
                poc_source=outcome.get("poc", "") or "",
                target_runs=outcome.get("runs", []),
                observed=outcome.get("observed", {}),
                duration_ms=outcome.get("duration_ms", 0),
                note=outcome.get("note", ""))
            bb.verifications.append(verification)
            hyp.status = "verified" if outcome["verdict"] in ("verified",) else (
                "inconclusive" if outcome["verdict"] == "inconclusive" else "dropped")
            self.ctx.memory.record_verdict(hyp.hyp_id, outcome["verdict"],
                                           outcome["mode"], outcome.get("note", ""),
                                           outcome.get("observed", {}))
            self.ctx.memory.remember_pattern(hyp.cwe, hyp.sink_symbol,
                                             REMEDIATION.get(hyp.cwe, ""))
            if outcome["verdict"] in ("verified", "confirmed_static"):
                finding = Finding(
                    finding_id=bb.next_finding_id(), cwe=hyp.cwe, title=hyp.title,
                    severity=hyp.severity_guess, file=hyp.file, line=hyp.line,
                    confidence=hyp.confidence, symbol=hyp.sink_symbol,
                    impact=self._impact(hyp.cwe),
                    remediation=REMEDIATION.get(hyp.cwe, "按最小权限与输入校验原则整改。"),
                    evidence_chain=list(hyp.evidence), verification=verification)
                bb.findings.append(finding)
                hyp.status = "verified" if outcome["verdict"] == "verified" else "challenged"
            bb.log(self.name, "假设验证完成", hyp=hyp.hyp_id,
                   verdict=outcome["verdict"], mode=outcome["mode"],
                   duration_ms=outcome.get("duration_ms", 0))

    @staticmethod
    def _impact(cwe: str) -> str:
        return {
            "CWE-89": "攻击者可绕过查询条件读取、篡改或删除数据库中的任意数据，"
                      "在数据库权限较高时可能进一步执行系统命令。",
            "CWE-78": "攻击者可在服务器上执行任意系统命令，直接导致服务器失陷。",
            "CWE-22": "攻击者可读取服务器上任意可读文件，包括配置与凭据文件。",
            "CWE-502": "攻击者可通过构造序列化数据在服务器上执行任意代码。",
            "CWE-327": "口令摘要可被快速暴力破解或彩虹表还原，导致账户被接管。",
            "CWE-798": "源码或制品泄露即等同于凭据泄露，可被用于横向移动。",
            "CWE-95": "攻击者可注入并执行任意代码，等同于服务器失陷。",
            "CWE-79": "攻击者可在受害者浏览器中执行任意脚本，导致会话劫持、"
                      "钓鱼与页面篡改。",
            "CWE-918": "攻击者可借服务端身份访问内网资源与云元数据接口，"
                       "读取本地文件或探测内网拓扑。",
        }.get(cwe, "可能导致信息泄露或权限提升。")


# ---------------------------------------------------------------- 利用链智能体

class ChainAgent(BaseAgent):
    name = "chain"

    def run(self, bb: Blackboard) -> None:
        if not bb.findings:
            # 没有交付项也要留痕：否则这一环在轨迹里凭空消失，
            # 看起来像"利用链阶段没跑"，实际是跑了、只是无链可组
            bb.log(self.name, "攻击链分析完成", chains=0)
            return
        present = {f.cwe for f in bb.findings}
        for exec_cwes, secret_cwe, narrative, severity in CHAIN_RULES:
            if not present.intersection(exec_cwes):
                continue
            if secret_cwe not in present:
                continue
            steps = [f.finding_id for f in bb.findings
                     if f.cwe in set(exec_cwes).union({secret_cwe})]
            chain_id = "C-%s" % hashlib.sha1(
                ("".join(sorted(steps))).encode("utf-8")).hexdigest()[:6]
            bb.chains.append(AttackChain(chain_id=chain_id, steps=steps,
                                         narrative=narrative, severity=severity))
            break
        bb.log(self.name, "攻击链分析完成", chains=len(bb.chains))


# ---------------------------------------------------------------- 报告智能体

class ReporterAgent(BaseAgent):
    name = "reporter"

    def run(self, bb: Blackboard) -> ScanResult:
        severities = [f.severity for f in bb.findings]
        result = ScanResult(
            run_id=bb.run_id, target=bb.target, graph=bb.graph, slices=bb.slices,
            hypotheses=bb.hypotheses, verifications=bb.verifications,
            findings=sorted(bb.findings, key=lambda f: f.severity, reverse=True),
            chains=bb.chains, budget_snapshot=self.ctx.budget.snapshot(),
            timeline=bb.timeline)
        bb.log(self.name, "报告生成完成", findings=len(bb.findings),
               top_severity=max_severity(severities) if severities else "info")
        return result
