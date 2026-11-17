# -*- coding: utf-8 -*-
"""报告与合规输出。

产出四份文件：
  report.md        人读报告（含证据链、复现步骤、修复建议）
  findings.json    机读结果（供缺陷平台/工单系统对接）
  compliance.json  按《网络产品安全漏洞管理规定》口径整理的报送要素
  run_trace.json   全过程审计轨迹（智能体时间线 + 工具调用审计 + 预算）
"""
import json
import os
import time
from typing import Any, Dict, List

from .domain import CWE_NAMES, ScanResult

SEVERITY_LABEL = {"critical": "严重", "high": "高危", "medium": "中危",
                  "low": "低危", "info": "提示"}

VERDICT_LABEL = {"verified": "已差分验证", "confirmed_static": "静态确认",
                 "refuted": "误报（已剔除）", "inconclusive": "待人工确认"}


def _table(headers: List[str], rows: List[List[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("\n", " ") for c in row) + " |")
    return "\n".join(out)


def render_markdown(result: ScanResult, memory_stats: Dict[str, Any] | None = None) -> str:
    target = result.target
    lines: List[str] = []
    lines.append("# 自动化漏洞发掘报告")
    lines.append("")
    lines.append("- 任务编号：`%s`" % result.run_id)
    if target:
        lines.append("- 扫描目标：`%s`（%d 个文件 / %d 行代码 / 指纹 `%s`）"
                     % (target.name, target.file_count, target.loc, target.hash))
    lines.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    budget = result.budget_snapshot or {}
    lines.append("- 资源消耗：%s 轮推理 / %s 次工具调用 / 耗时 %s 毫秒"
                 % (budget.get("rounds"), budget.get("tool_calls"),
                    budget.get("elapsed_ms")))
    sandbox = budget.get("sandbox") or {}
    if sandbox:
        lines.append("- 沙箱后端：`%s`（隔离等级：%s）"
                     % (sandbox.get("effective_backend"),
                        "容器级" if sandbox.get("isolated") else "进程级"))
    if result.graph:
        g = result.graph
        lines.append("- 符号解析：%d 个调用点，内部调用 %d，外部调用 %d，"
                     "无法静态确定 %d，解析率 %.1f%%"
                     % (g.call_sites, g.resolved_calls, g.external_calls,
                        g.unresolved_calls, g.resolution_rate * 100))
    inc = result.incremental or {}
    if inc.get("enabled"):
        diff = inc.get("diff") or {}
        lines.append("- 增量扫描：变更 %d 个文件（新增 %d / 修改 %d / 删除 %d，来源 %s），"
                     "影响 %d 个分析单元；本次审计 %d 个切片，跳过 %d 个"
                     % (diff.get("changed_count", 0), len(diff.get("added", [])),
                        len(diff.get("modified", [])), len(diff.get("deleted", [])),
                        diff.get("source", "-"), inc.get("impacted_units", 0),
                        inc.get("selected_slices", 0), inc.get("skipped_slices", 0)))
    lines.append("")

    # ---------------------------------------------------------- 摘要
    counts: Dict[str, int] = {}
    for f in result.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    verified = sum(1 for v in result.verifications if v.verdict == "verified")
    static = sum(1 for v in result.verifications if v.verdict == "confirmed_static")
    refuted = sum(1 for v in result.verifications if v.verdict == "refuted")
    pending = sum(1 for v in result.verifications if v.verdict == "inconclusive")
    lines.append("## 一、结论摘要")
    lines.append("")
    lines.append("本次任务共提出 %d 条漏洞假设，经反证与差分验证后确认 %d 条，"
                 "其中动态验证 %d 条、静态确认 %d 条；剔除误报 %d 条，"
                 "转人工确认 %d 条。"
                 % (len(result.hypotheses), len(result.findings),
                    verified, static, refuted, pending))
    lines.append("")
    if counts:
        lines.append(_table(["严重程度", "数量"],
                            [[SEVERITY_LABEL.get(k, k), v]
                             for k, v in sorted(counts.items(),
                                                key=lambda kv: -_rank(kv[0]))]))
    else:
        lines.append("未发现需要交付的漏洞。")
    lines.append("")

    if result.degraded:
        lines.append("> **降级说明**：")
        for item in result.degraded:
            lines.append("> - %s" % item)
        lines.append("")

    # ---------------------------------------------------------- 攻击链
    if result.chains:
        lines.append("## 二、攻击链分析")
        lines.append("")
        for chain in result.chains:
            lines.append("**%s（%s）**：%s"
                         % (chain.chain_id, SEVERITY_LABEL.get(chain.severity, chain.severity),
                            chain.narrative))
            lines.append("")
            lines.append("涉及条目：%s" % "、".join(chain.steps))
            lines.append("")

    # ---------------------------------------------------------- 漏洞清单
    lines.append("## 三、漏洞清单")
    lines.append("")
    if result.findings:
        rows = []
        root = result.target.root if result.target else None
        for f in result.findings:
            rows.append([f.finding_id, SEVERITY_LABEL.get(f.severity, f.severity),
                         "%s %s" % (f.cwe, CWE_NAMES.get(f.cwe, "")),
                         "%s:%d" % (_rel(f.file, root), f.line),
                         VERDICT_LABEL.get(f.verification.verdict if f.verification else "",
                                           "-"),
                         "%.2f" % f.confidence])
        lines.append(_table(["编号", "等级", "类型", "位置", "验证方式", "置信度"], rows))
    else:
        lines.append("无。")
    lines.append("")

    # ---------------------------------------------------------- 详情
    lines.append("## 四、漏洞详情")
    lines.append("")
    for f in result.findings:
        lines.append("### %s %s" % (f.finding_id, f.title))
        lines.append("")
        lines.append("- 缺陷类型：%s %s" % (f.cwe, CWE_NAMES.get(f.cwe, "")))
        lines.append("- 严重程度：%s" % SEVERITY_LABEL.get(f.severity, f.severity))
        lines.append("- 代码位置：`%s:%d`（符号 `%s`）"
                     % (_rel(f.file, result.target.root if result.target else None),
                        f.line, f.symbol))
        if f.verification:
            lines.append("- 验证结论：%s（%s）"
                         % (VERDICT_LABEL.get(f.verification.verdict, f.verification.verdict),
                            f.verification.note))
            if f.verification.observed:
                lines.append("- 观测结果：`%s`"
                             % json.dumps(f.verification.observed, ensure_ascii=False))
        lines.append("")
        lines.append("**影响面**：%s" % f.impact)
        lines.append("")
        lines.append("**修复建议**：%s" % f.remediation)
        lines.append("")
        if f.evidence_chain:
            lines.append("**证据链**：")
            lines.append("")
            for ev in f.evidence_chain:
                lines.append("- `[%s]` %s —— %s" % (ev.kind, ev.ref, ev.detail))
            lines.append("")
        if f.verification and f.verification.target_runs:
            lines.append("**复现记录**：")
            lines.append("")
            for run in f.verification.target_runs:
                lines.append("- 目标 `%s`：退出码 %s，耗时 %s 毫秒，观测 `%s`"
                             % (run.get("target"), run.get("exit_code"),
                                run.get("duration_ms"),
                                json.dumps(run.get("parsed", {}), ensure_ascii=False)))
            lines.append("")
        if f.verification and f.verification.poc_source:
            lines.append("**验证用例源码**（`%s`）：" % f.verification.poc_path)
            lines.append("")
            lines.append("```python")
            lines.append(f.verification.poc_source.rstrip())
            lines.append("```")
            lines.append("")

    # ---------------------------------------------------------- 未决项
    dropped = [h for h in result.hypotheses if h.status == "dropped"]
    if dropped:
        lines.append("## 五、已剔除的假设（保留以备复核）")
        lines.append("")
        lines.append(_table(["编号", "类型", "位置", "剔除依据"],
                            [[h.hyp_id, "%s %s" % (h.cwe, CWE_NAMES.get(h.cwe, "")),
                              "%s:%d" % (os.path.basename(h.file), h.line),
                              h.rationale[:120]] for h in dropped]))
        lines.append("")

    # ---------------------------------------------------------- 附录
    lines.append("## 附：执行轨迹")
    lines.append("")
    lines.append(_table(["阶段", "动作", "要点", "时刻(ms)"],
                        [[e.get("agent"), e.get("message"),
                          ", ".join("%s=%s" % (k, v) for k, v in e.items()
                                    if k not in ("agent", "message", "ts")),
                          e.get("ts", "-")]
                         for e in result.timeline]))
    lines.append("")
    if memory_stats:
        lines.append("记忆库统计：`%s`" % json.dumps(memory_stats, ensure_ascii=False))
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 说明：本报告由自动化流程生成。标注为“已差分验证”的条目具备"
                 "可复现证据；标注为“静态确认”的条目仅有源码证据，"
                 "“待人工确认”的条目不得直接作为结论使用。")
    return "\n".join(lines)


def _rank(sev: str) -> int:
    order = ["info", "low", "medium", "high", "critical"]
    return order.index(sev) if sev in order else 0


def _rel(path: str, root: str | None) -> str:
    """把绝对路径转成相对目标的路径，便于阅读与对外报送。"""
    if not root:
        return path
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path


def build_compliance(result: ScanResult) -> Dict[str, Any]:
    """按《网络产品安全漏洞管理规定》第七条要素整理报送材料。"""
    return {
        "run_id": result.run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "product": {
            "name": result.target.name if result.target else "",
            "version": "以实际制品版本为准",
            "fingerprint": result.target.hash if result.target else "",
        },
        "reporting_channel": "工业和信息化部网络安全威胁和漏洞信息共享平台",
        "reporting_deadline_hint": "自发现或获知之日起 2 日内完成报送",
        "vulnerabilities": [
            {
                "finding_id": f.finding_id,
                "type": "%s %s" % (f.cwe, CWE_NAMES.get(f.cwe, "")),
                "severity": f.severity,
                "location": "%s:%d" % (f.file, f.line),
                "technical_feature": f.title,
                "impact_scope": f.impact,
                "evidence": [e.ref for e in f.evidence_chain],
                "verification": f.verification.verdict if f.verification else "",
                "remediation": f.remediation,
            }
            for f in result.findings
        ],
        "disclaimer": "本材料为自动化生成的报送要素草稿，正式报送前须经责任部门复核，"
                      "并确认未包含不应向境外提供的未公开漏洞信息。",
    }


def write_report(result: ScanResult, out_dir: str,
                 memory_stats: Dict[str, Any] | None = None) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "report": os.path.join(out_dir, "report.md"),
        "findings": os.path.join(out_dir, "findings.json"),
        "compliance": os.path.join(out_dir, "compliance.json"),
        "trace": os.path.join(out_dir, "run_trace.json"),
    }
    with open(paths["report"], "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result, memory_stats))
    with open(paths["findings"], "w", encoding="utf-8") as handle:
        json.dump({"run_id": result.run_id,
                   "target": result.target.to_dict() if result.target else None,
                   "findings": [f.to_dict() for f in result.findings],
                   "chains": [c.to_dict() for c in result.chains]},
                  handle, ensure_ascii=False, indent=2)
    with open(paths["compliance"], "w", encoding="utf-8") as handle:
        json.dump(build_compliance(result), handle, ensure_ascii=False, indent=2)
    with open(paths["trace"], "w", encoding="utf-8") as handle:
        json.dump({"run_id": result.run_id,
                   "target": result.target.to_dict() if result.target else None,
                   "budget": result.budget_snapshot,
                   "incremental": result.incremental,
                   "degraded": result.degraded,
                   "timeline": result.timeline,
                   "slices": [{"slice_id": s.slice_id, "path": s.path,
                               "symbol": s.symbol, "start_line": s.start_line,
                               "end_line": s.end_line, "risk_score": s.risk_score,
                               "graph_context": s.graph_context}
                              for s in result.slices],
                   "hypotheses": [h.to_dict() for h in result.hypotheses]},
                  handle, ensure_ascii=False, indent=2)
    return paths
