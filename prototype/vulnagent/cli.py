# -*- coding: utf-8 -*-
"""命令行入口：scan / memory / doctor。

用法示例：
    python -m vulnagent scan --target demo/vulnapp --patched demo/vulnapp_patched
    python -m vulnagent memory --db memory/cases.db
    python -m vulnagent doctor
"""
import argparse
import json
import os
import sys

from .config import load_config
from .orchestrator import Orchestrator
from .report import write_report

SEVERITY_LABEL = {"critical": "严重", "high": "高危", "medium": "中危",
                  "low": "低危", "info": "提示"}


def cmd_scan(args) -> int:
    config = load_config(args.config)
    if args.out:
        config.run.output_dir = args.out
    if args.provider:
        config.model.provider = args.provider
    if args.base_url:
        config.model.base_url = args.base_url
    if args.model:
        config.model.model = args.model
    if args.allow_cloud:
        config.model.allow_cloud = True
    db_path = args.db or config.memory.db_path
    orchestrator = Orchestrator(config, patched_root=args.patched,
                                memory_path=db_path)
    try:
        result = orchestrator.scan(args.target, incremental=args.incremental,
                                   baseline_path=args.baseline,
                                   git_ref=args.git_ref)
        paths = write_report(result, config.run.output_dir,
                             orchestrator.memory.stats())
    finally:
        orchestrator.close()

    print("=" * 68)
    print("任务编号 : %s" % result.run_id)
    if result.target:
        print("扫描目标 : %s（%d 文件 / %d 行）"
              % (result.target.name, result.target.file_count, result.target.loc))
    budget = result.budget_snapshot
    print("资源消耗 : %s 轮 / %s 次工具调用 / %s ms"
          % (budget.get("rounds"), budget.get("tool_calls"), budget.get("elapsed_ms")))
    inc = result.incremental or {}
    if inc.get("enabled"):
        diff = inc.get("diff") or {}
        print("增量扫描 : 变更 %s 个文件（新增 %s / 修改 %s / 删除 %s），影响 %s 个单元"
              % (diff.get("changed_count", 0), len(diff.get("added", [])),
                 len(diff.get("modified", [])), len(diff.get("deleted", [])),
                 inc.get("impacted_units", 0)))
        print("           审计 %s 个切片，跳过 %s 个"
              % (inc.get("selected_slices"), inc.get("skipped_slices")))
    print("假设/确认: %d / %d" % (len(result.hypotheses), len(result.findings)))
    print("-" * 68)
    if result.findings:
        for f in result.findings:
            print("  [%s] %-10s %s:%d  %s"
                  % (SEVERITY_LABEL.get(f.severity, f.severity), f.cwe,
                     os.path.basename(f.file), f.line, f.title))
    else:
        print("  未发现需要交付的漏洞")
    if result.chains:
        print("-" * 68)
        for chain in result.chains:
            print("  攻击链 %s：%s" % (chain.chain_id, chain.narrative[:60] + "..."))
    if result.degraded:
        print("-" * 68)
        for item in result.degraded:
            print("  降级：%s" % item)
    print("=" * 68)
    for name, path in paths.items():
        print("%-10s -> %s" % (name, path))
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_memory(args) -> int:
    config = load_config(args.config)
    from .memory import MemoryStore
    store = MemoryStore(args.db or config.memory.db_path)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_export_pentest(args) -> int:
    """把一次扫描的产物导出为 dsh-pentest 的回放序列。"""
    from .integrations.dsh_pentest import export_from_run_dir, validate_replay
    plan = export_from_run_dir(args.run_dir, authorization=args.authorization or "",
                               objective=args.objective or "")
    problems = validate_replay(plan)
    out = args.out or os.path.join(args.run_dir, "pentest_replay.json")
    directory = os.path.dirname(os.path.abspath(out))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)

    counts = plan["counts"]
    print("=" * 68)
    print("目标插件 : %s" % plan["plugin"])
    print("来源任务 : %s" % plan["run_id"])
    print("调用序列 : %d 步（意图 %d / 事实 %d / 漏洞 %d / 资产 %d，预计节点 %d）"
          % (counts["calls"], counts["intents"], counts["facts"],
             counts["findings"], counts["assets"], counts["nodes_estimate"]))
    print("契约校验 : %s" % ("通过" if not problems else "发现 %d 个问题" % len(problems)))
    for item in problems:
        print("   ! %s" % item)
    for item in plan.get("warnings", []):
        print("   提示：%s" % item)
    print("=" * 68)
    print("已写出：%s" % out)
    print("回放方式：在 DSH「渗透模式」会话中按 calls 顺序调用 pentest_* 工具，"
          "把每步返回的 id 填入后续的 $ref 占位符。")
    return 1 if problems else 0


def cmd_doctor(args) -> int:
    """环境自检：确认能否离线跑通、外部工具是否可用。"""
    config = load_config(args.config)
    from .tools import run_external_sast
    report = {
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "sandbox_backend": config.sandbox.backend,
        "model_base_url": config.model.base_url,
        "model_name": config.model.model,
        "allow_cloud": config.model.allow_cloud,
        "memory_db": config.memory.db_path,
        "external_sast": run_external_sast([], config.tools),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vulnagent",
        description="基于 AI 智能体的自动化漏洞发掘系统（原型）")
    parser.add_argument("--config", help="配置文件路径（TOML 或 JSON）")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="扫描目标代码库")
    scan.add_argument("--target", required=True, help="目标代码目录")
    scan.add_argument("--patched", help="修复版目录，用于差分验证")
    scan.add_argument("--out", help="报告输出目录")
    scan.add_argument("--db", help="记忆库路径")
    scan.add_argument("--json", action="store_true", help="额外输出完整 JSON 结果")
    scan.add_argument("--provider", choices=["heuristic", "openai_compat"],
                      help="推理后端：heuristic 离线确定性 / openai_compat 接入模型")
    scan.add_argument("--base-url", help="模型服务地址（OpenAI 兼容）")
    scan.add_argument("--model", help="模型名称")
    scan.add_argument("--allow-cloud", action="store_true",
                      help="允许使用云端模型（须同时在配置中填写 cloud_model）")
    scan.add_argument("--incremental", action="store_true",
                      help="增量扫描：只审变更文件及其影响面")
    scan.add_argument("--baseline", help="增量基线清单路径（默认 <target>/.vulnagent/baseline.json）")
    scan.add_argument("--git-ref", help="用 git diff 指定基准提交计算变更，如 HEAD~1")
    scan.set_defaults(func=cmd_scan)

    memory = sub.add_parser("memory", help="查看记忆库统计")
    memory.add_argument("--db", help="记忆库路径")
    memory.set_defaults(func=cmd_memory)

    export = sub.add_parser("export-pentest",
                            help="把扫描产物导出为 dsh-pentest 探索链路回放序列")
    export.add_argument("--run-dir", required=True,
                        help="一次扫描的产物目录（含 findings.json 与 run_trace.json）")
    export.add_argument("--out", help="输出路径，默认 <run-dir>/pentest_replay.json")
    export.add_argument("--authorization",
                        help="授权说明（授权对象或书面许可引用），写入渗透图与报告留痕")
    export.add_argument("--objective", help="engagement 目标说明，缺省自动生成")
    export.set_defaults(func=cmd_export_pentest)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
