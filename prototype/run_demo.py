# -*- coding: utf-8 -*-
"""端到端演示：扫描两个演示目标，各自以对应的修复版作为差分基线。

目标一 demo/vulnapp   单文件、六类典型缺陷，验证检出与差分验证能力；
目标二 demo/webapp    三层跨文件调用链，验证跨文件可达性与同名方法消歧能力。

运行： python run_demo.py
产物： out/<目标>/report.md、findings.json、compliance.json、run_trace.json
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vulnagent.config import load_config          # noqa: E402
from vulnagent.orchestrator import Orchestrator   # noqa: E402
from vulnagent.report import write_report         # noqa: E402

SEVERITY_LABEL = {"critical": "严重", "high": "高危", "medium": "中危",
                  "low": "低危", "info": "提示"}

TARGETS = [
    ("vulnapp", "单文件目标：六类典型缺陷"),
    ("webapp", "多文件目标：三层跨文件调用链"),
    ("websvc", "Web 服务片段：四类可动态验证的缺陷"),
]


def scan_one(name: str, desc: str, config) -> None:
    target = os.path.join(HERE, "demo", name)
    patched = os.path.join(HERE, "demo", "%s_patched" % name)
    out_dir = os.path.join(HERE, "out", name)

    orchestrator = Orchestrator(config, patched_root=patched,
                                memory_path=config.memory.db_path)
    try:
        result = orchestrator.scan(target)
        paths = write_report(result, out_dir, orchestrator.memory.stats())
    finally:
        orchestrator.close()

    graph = result.graph
    sandbox = (result.budget_snapshot or {}).get("sandbox", {})
    print("=" * 74)
    print("目标：%s（%s）" % (name, desc))
    print("  规模：%d 个文件 / %d 行代码；函数与单元 %d 个；外部入口 %d 个"
          % (result.target.file_count, result.target.loc,
             graph.functions, len(graph.entry_points)))
    print("  解析：调用点 %d，内部 %d，外部 %d，无法静态确定 %d，解析率 %.0f%%"
          % (graph.call_sites, graph.resolved_calls, graph.external_calls,
             graph.unresolved_calls, graph.resolution_rate * 100))
    print("  沙箱：%s（隔离等级：%s）"
          % (sandbox.get("effective_backend"),
             "容器级" if sandbox.get("isolated") else "进程级"))
    print("  结果：切片 %d 个；假设 %d 条；确认 %d 条"
          % (len(result.slices), len(result.hypotheses), len(result.findings)))
    print("-" * 74)
    for f in result.findings:
        verdict = f.verification.verdict if f.verification else ""
        location = os.path.relpath(f.file, target).replace("\\", "/")
        print("  [%s] %-9s %-16s %s:%d"
              % (SEVERITY_LABEL.get(f.severity, f.severity), f.cwe, verdict,
                 location, f.line))
    for chain in result.chains:
        print("  攻击链 %s：%s" % (chain.chain_id, chain.narrative[:56] + "…"))
    for item in result.degraded:
        print("  降级：%s" % item)
    print("  产物：%s" % os.path.relpath(paths["report"], HERE))


def demo_incremental(config) -> None:
    """增量扫描演示：以修复版为起点建立基线，再重新引入缺陷，观察只审变更影响面。"""
    import shutil
    import tempfile

    workdir = tempfile.mkdtemp(prefix="vulnagent-inc-")
    project = os.path.join(workdir, "app")
    shutil.copytree(os.path.join(HERE, "demo", "webapp_patched"), project)
    out_dir = os.path.join(workdir, "out")
    baseline = os.path.join(out_dir, "baseline.json")

    def run(label):
        cfg = load_config(os.path.join(HERE, "config.example.toml"))
        cfg.run.output_dir = out_dir
        cfg.memory.db_path = os.path.join(workdir, "cases.db")
        cfg.tools.enabled = ["builtin_rules", "repo_map", "ast_graph",
                             "sandbox_exec", "poc_generator"]
        orchestrator = Orchestrator(cfg, patched_root=os.path.join(HERE, "demo", "webapp_patched"),
                                    memory_path=cfg.memory.db_path)
        try:
            result = orchestrator.scan(project, incremental=True,
                                       baseline_path=baseline)
        finally:
            orchestrator.close()
        inc = result.incremental
        diff = inc.get("diff", {})
        print("  %-22s 变更 %s 文件 → 审计 %s 个切片（跳过 %s），确认 %d 条"
              % (label, diff.get("changed_count", 0), inc.get("selected_slices", 0),
                 inc.get("skipped_slices", 0), len(result.findings)))
        return result

    print("=" * 74)
    print("增量扫描演示（CI 场景：先安全、后回归缺陷）")
    run("① 首次扫描（建基线）")
    shutil.copyfile(os.path.join(HERE, "demo", "webapp", "dao.py"),
                    os.path.join(project, "dao.py"))
    result = run("② 重新引入缺陷后")
    for finding in result.findings:
        print("     [%s] %s:%d" % (finding.cwe,
                                   os.path.basename(finding.file), finding.line))
    run("③ 无变更再跑一次")
    shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    config = load_config(os.path.join(HERE, "config.example.toml"))
    config.memory.db_path = os.path.join(HERE, "memory", "cases.db")
    config.tools.enabled = ["builtin_rules", "repo_map", "ast_graph",
                            "sandbox_exec", "poc_generator"]
    for name, desc in TARGETS:
        scan_one(name, desc, config)
    demo_incremental(config)
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
