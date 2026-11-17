# -*- coding: utf-8 -*-
"""领域模型：全流程流转的数据结构。

所有模型均为可 JSON 序列化的 dataclass，便于落库、写审计日志与对外接口复用。
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 常量定义

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]

HYPOTHESIS_STATUS = [
    "proposed",      # 审计智能体提出
    "challenged",    # 反证智能体已质询
    "dropped",       # 被证伪或条件不可达
    "verified",      # 沙箱差分验证通过
    "inconclusive",  # 无法自动验证，转人工
]

VERDICT = ["verified", "refuted", "inconclusive", "confirmed_static"]

CWE_NAMES = {
    "CWE-22": "路径穿越",
    "CWE-78": "操作系统命令注入",
    "CWE-79": "跨站脚本",
    "CWE-89": "SQL 注入",
    "CWE-95": "代码注入（eval）",
    "CWE-327": "使用已被攻破或危险的加密算法",
    "CWE-502": "不可信数据反序列化",
    "CWE-798": "硬编码凭据",
    "CWE-918": "服务端请求伪造",
}


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


# ---------------------------------------------------------------- 目标与切片

@dataclass
class Target:
    """扫描目标。"""
    name: str
    root: str
    languages: Dict[str, int] = field(default_factory=dict)
    file_count: int = 0
    loc: int = 0
    hash: str = ""

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class EntryPoint:
    """外部输入入口。"""
    symbol: str
    path: str
    line: int
    kind: str = "function"      # function / route / main / argv
    params: List[str] = field(default_factory=list)

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class Sink:
    """危险汇聚点（可能触发漏洞的调用）。"""
    symbol: str
    path: str
    line: int
    cwe: str
    snippet: str = ""
    tainted_params: List[str] = field(default_factory=list)

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class CodeSlice:
    """送审的代码切片：一次推理的最小上下文单元。"""
    slice_id: str
    path: str
    symbol: str
    start_line: int
    end_line: int
    code: str
    graph_context: List[str] = field(default_factory=list)   # 调用链摘要
    risk_score: float = 0.0

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class GraphSummary:
    """代码语义图谱摘要。"""
    functions: int = 0
    calls: int = 0
    entry_points: List[EntryPoint] = field(default_factory=list)
    sinks: List[Sink] = field(default_factory=list)
    call_edges: List[List[str]] = field(default_factory=list)
    reachable_from_entry: List[str] = field(default_factory=list)
    # 符号解析质量指标：解析率越高，可达性判断越可信
    call_sites: int = 0
    resolved_calls: int = 0
    external_calls: int = 0
    unresolved_calls: int = 0
    resolution_rate: float = 0.0
    parse_errors: List[str] = field(default_factory=list)

    def to_dict(self):
        return _clean(asdict(self))


# ---------------------------------------------------------------- 假设与证据

@dataclass
class Evidence:
    """证据：任何结论都必须绑定证据。"""
    kind: str            # graph / static / dynamic
    ref: str             # 文件:行 或 工具名
    detail: str = ""

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class Hypothesis:
    """漏洞假设：必须可被证伪。"""
    hyp_id: str
    slice_id: str
    cwe: str
    title: str
    file: str
    line: int
    sink_symbol: str = ""
    source: str = ""                 # 污点来源（参数名等）
    preconditions: List[str] = field(default_factory=list)
    confidence: float = 0.0
    severity_guess: str = "medium"
    rationale: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    status: str = "proposed"
    producer: str = ""               # 由哪个智能体产出

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class Verification:
    """验证结论。"""
    hyp_id: str
    verdict: str = "inconclusive"
    mode: str = "dynamic"            # dynamic / static
    poc_path: str = ""
    poc_source: str = ""             # 验证用例源码（可复现证据，对外集成必需）
    target_runs: List[Dict[str, Any]] = field(default_factory=list)
    observed: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    note: str = ""

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class Finding:
    """交付态漏洞条目。"""
    finding_id: str
    cwe: str
    title: str
    severity: str
    file: str
    line: int
    confidence: float
    symbol: str = ""
    impact: str = ""
    remediation: str = ""
    evidence_chain: List[Evidence] = field(default_factory=list)
    verification: Optional[Verification] = None
    status: str = "open"

    def to_dict(self):
        data = _clean(asdict(self))
        data["cwe_name"] = CWE_NAMES.get(self.cwe, "")
        return data


@dataclass
class AttackChain:
    """攻击链：多个缺陷串联后的影响面。"""
    chain_id: str
    steps: List[str] = field(default_factory=list)      # finding_id 序列
    narrative: str = ""
    severity: str = "high"

    def to_dict(self):
        return _clean(asdict(self))


@dataclass
class ScanResult:
    """一次扫描的完整结果。"""
    run_id: str
    target: Optional[Target] = None
    graph: Optional[GraphSummary] = None
    slices: List[CodeSlice] = field(default_factory=list)
    hypotheses: List[Hypothesis] = field(default_factory=list)
    verifications: List[Verification] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    chains: List[AttackChain] = field(default_factory=list)
    budget_snapshot: Dict[str, Any] = field(default_factory=dict)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    degraded: List[str] = field(default_factory=list)     # 降级记录
    incremental: Dict[str, Any] = field(default_factory=dict)  # 增量扫描统计

    def to_dict(self):
        return _clean(asdict(self))


def severity_rank(sev: str) -> int:
    try:
        return SEVERITY_ORDER.index(sev)
    except ValueError:
        return 0


def max_severity(values: List[str]) -> str:
    if not values:
        return "info"
    return max(values, key=severity_rank)
