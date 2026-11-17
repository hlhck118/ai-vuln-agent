# -*- coding: utf-8 -*-
"""编排引擎：预算控制、智能体调度、状态机与降级记录。

一次扫描的生命周期：
    侦察 → 规划 → 审计 → 反证 → 验证 → 利用链 → 报告
任一阶段超出预算即安全退出，并把"未完成"如实标注为待人工确认，
绝不把未验证的假设包装成结论。
"""
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .agents import (AgentContext, Blackboard, AuditorAgent, ChainAgent,
                     CriticAgent, PlannerAgent, ReconAgent, ReporterAgent,
                     VerifierAgent)
from .analysis import parse_target
from .config import AppConfig
from .domain import ScanResult
from .incremental import (DiffResult, IncrementalPlan, build_manifest,
                          diff_manifests, git_changed_files, impacted_units,
                          load_manifest, save_manifest)
from .llm import Router
from .memory import MemoryStore
from .tools import (DifferentialVerifier, SandboxRunner, ToolRegistry, ToolSpec,
                    run_external_sast)

DEFAULT_BASELINE_FILE = "vulnagent-baseline.json"


# ---------------------------------------------------------------- 预算

class Budget:
    """显式预算：轮数、工具调用、token 三重上限。"""

    def __init__(self, cfg):
        self.max_rounds = cfg.max_rounds
        self.max_tool_calls = cfg.max_tool_calls
        self.max_tokens = cfg.max_tokens
        self.max_slices = getattr(cfg, "max_slices", 100)
        self.max_hypotheses_per_slice = cfg.max_hypotheses_per_slice
        self.rounds = 0
        self.tool_calls = 0
        self.tokens = 0
        self.planned = 0
        self.stopped_reason = ""

    def can_continue(self) -> bool:
        if self.rounds >= self.max_rounds:
            self.stopped_reason = "达到最大轮数 %d" % self.max_rounds
            return False
        if self.tool_calls >= self.max_tool_calls:
            self.stopped_reason = "达到最大工具调用次数 %d" % self.max_tool_calls
            return False
        if self.tokens >= self.max_tokens:
            self.stopped_reason = "达到 token 预算上限 %d" % self.max_tokens
            return False
        return True

    def spend_round(self, n: int = 1) -> None:
        self.rounds += n

    def spend_tool(self, n: int = 1) -> None:
        self.tool_calls += n

    def spend_tokens(self, n: int) -> None:
        self.tokens += max(0, int(n))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds, "max_rounds": self.max_rounds,
            "tool_calls": self.tool_calls, "max_tool_calls": self.max_tool_calls,
            "tokens": self.tokens, "max_tokens": self.max_tokens,
            "planned_slices": self.planned, "stopped_reason": self.stopped_reason,
        }


# ---------------------------------------------------------------- 工具函数

def _repo_analyze(root: str, exts, exclude_dirs, max_bytes) -> Dict[str, Any]:
    repo, graph, units, sinks, slices = parse_target(root, exts, exclude_dirs, max_bytes)
    return {"repo": repo, "graph": graph, "units": units, "sinks": sinks,
            "slices": slices}


# ---------------------------------------------------------------- 编排器

class Orchestrator:
    def __init__(self, config: AppConfig, patched_root: Optional[str] = None,
                 memory_path: Optional[str] = None):
        self.config = config
        self.patched_root = patched_root
        self.registry = ToolRegistry()
        self.runner = SandboxRunner(config.sandbox,
                                    allow_fallback=config.sandbox.allow_fallback)
        self.registry.register(ToolSpec(
            "repo_analyze", "仓库测绘、AST 语义图谱、规则切片", _repo_analyze))
        self.registry.register(ToolSpec(
            "sandbox_exec", "在隔离沙箱中执行验证用例", self.runner.run_poc,
            dangerous=True))
        self.memory = MemoryStore(memory_path or config.memory.db_path,
                                  enabled=config.memory.enabled,
                                  top_k=config.memory.top_k)
        self.registry.register(ToolSpec(
            "memory_search", "历史案例相似检索", self.memory.search_similar))
        self.registry.register(ToolSpec(
            "external_sast", "调用 CodeQL/Semgrep 等外部静态分析工具",
            lambda paths: run_external_sast(paths, config.tools)))
        self.verifier = DifferentialVerifier(self.runner, self.registry,
                                             patched_root)
        self.router = Router(config.model)
        self.reasoner = self.router.hypothesizer()
        self.critic = self.router.critic()
        self.budget = Budget(config.budget)
        self.degraded: List[str] = []

    # ------------------------------------------------------------ 主流程
    def scan(self, target_root: str, incremental: bool = False,
             baseline_path: Optional[str] = None,
             git_ref: Optional[str] = None) -> ScanResult:
        target_root = os.path.abspath(target_root)
        if not os.path.isdir(target_root):
            raise NotADirectoryError("目标目录不存在: %s" % target_root)
        run_id = "R-%s" % uuid.uuid4().hex[:10]
        bb = Blackboard(run_id=run_id, target_root=target_root)
        self.memory.start_run(run_id, os.path.basename(target_root), "")
        started = time.time()

        ctx = AgentContext(config=self.config, reasoner=self.reasoner,
                           critic=self.critic,
                           verifier=self.verifier, tools=self.registry,
                           memory=self.memory, budget=self.budget)

        ReconAgent(ctx).run(bb)
        if self.memory.conn and bb.target:
            self.memory.conn.execute("UPDATE runs SET target_hash=? WHERE run_id=?",
                                     (bb.target.hash, run_id))
            self.memory.conn.commit()
        self._external_sast_probe(bb)

        plan = IncrementalPlan()
        if incremental:
            plan = self._apply_incremental(bb, baseline_path, git_ref)
        PlannerAgent(ctx).run(bb)
        AuditorAgent(ctx).run(bb)
        CriticAgent(ctx).run(bb)
        VerifierAgent(ctx).run(bb)
        ChainAgent(ctx).run(bb)
        result = ReporterAgent(ctx).run(bb)

        result.degraded = self._collect_degraded()
        result.incremental = plan.to_dict()
        result.budget_snapshot["elapsed_ms"] = int((time.time() - started) * 1000)
        result.budget_snapshot["tool_audit"] = self.registry.audit
        result.budget_snapshot["sandbox"] = self.runner.backend_info()
        stats = {
            "findings": len(result.findings),
            "verified": sum(1 for v in result.verifications if v.verdict == "verified"),
            "refuted": sum(1 for v in result.verifications if v.verdict == "refuted"),
            "static": sum(1 for v in result.verifications
                          if v.verdict == "confirmed_static"),
            "slices": len(result.slices),
            "incremental": bool(plan.enabled),
            "skipped_slices": plan.skipped_slices,
            "elapsed_ms": result.budget_snapshot["elapsed_ms"],
        }
        self.memory.finish_run(run_id, stats)
        return result

    # ------------------------------------------------------------ 增量扫描
    def _apply_incremental(self, bb: Blackboard, baseline_path: Optional[str],
                           git_ref: Optional[str]) -> IncrementalPlan:
        """按变更影响面裁剪待审切片。

        省掉的是推理与验证开销，图谱仍需全量构建——否则无法判断未变更的
        函数是否被变更的入口影响。
        """
        cfg = self.config
        # 基线默认放在输出目录而非被扫描仓库：避免污染目标仓库，
        # 也避免基线文件随仓库一起被复制/提交。
        manifest_path = baseline_path or os.path.join(
            cfg.run.output_dir, "vulnagent-baseline.json")
        manifest = build_manifest(bb.target_root, cfg.run.languages,
                                  cfg.run.exclude_dirs, cfg.run.max_file_bytes)
        plan = IncrementalPlan(enabled=True, manifest_path=manifest_path)

        changed_via_git = git_changed_files(bb.target_root, git_ref) if git_ref else None
        if changed_via_git is not None:
            plan.diff = DiffResult(added=[], modified=changed_via_git, deleted=[],
                                   source="git:%s" % git_ref)
        else:
            plan.diff = diff_manifests(load_manifest(manifest_path), manifest)

        edges = [(e[0], e[1]) for e in (bb.graph.call_edges or [])]
        plan.impacted = impacted_units(plan.diff.changed, bb.target_root,
                                       edges, set(bb.units))
        plan.total_slices = len(bb.slices)

        if plan.diff.is_empty:
            bb.slices = []
            bb.notes.append("增量模式：与基线相比无变更，跳过本次审计")
        elif plan.impacted:
            bb.slices = [s for s in bb.slices
                         if "%s::%s" % (s.path, s.symbol) in plan.impacted]
            bb.notes.append("增量模式：%d 个变更文件，影响 %d 个分析单元"
                            % (len(plan.diff.changed), len(plan.impacted)))
        else:
            # 有变更但影响面为空属异常情况（例如变更文件无可分析单元），
            # 保守回退全量，避免静默漏审
            self.degraded.append("增量模式：检测到变更但影响面为空，已保守回退全量审计")

        plan.selected_slices = len(bb.slices)
        plan.skipped_slices = plan.total_slices - plan.selected_slices
        save_manifest(manifest_path, manifest)
        bb.log("planner", "增量计划", changed=len(plan.diff.changed),
               impacted=len(plan.impacted), selected=plan.selected_slices,
               skipped=plan.skipped_slices)
        return plan

    # ------------------------------------------------------------ 辅助
    def _external_sast_probe(self, bb: Blackboard) -> None:
        """尝试调用外部静态分析工具；不可用时记录降级，不阻塞主流程。"""
        if "external_sast" not in self.config.tools.enabled:
            return
        payload = self.registry.invoke("external_sast",
                                       paths=[u.path for u in bb.units.values()][:200])
        if isinstance(payload, dict) and not payload.get("available"):
            self.degraded.append("外部静态分析工具不可用（%s），"
                                 "已使用内置规则引擎，结论置信度相应下调"
                                 % payload.get("reason", "未安装或未配置"))
        bb.log("recon", "外部工具探测", payload=str(payload)[:200])

    def _collect_degraded(self) -> List[str]:
        items = list(self.degraded)
        items.extend(getattr(self.runner, "degraded", []) or [])
        items.extend(getattr(self.reasoner, "degraded", []) or [])
        for route in self.router.routes:
            if route["impl"].startswith("llm-reasoner[null]"):
                items.append("角色 %s 无可用模型，已降级为确定性推理" % route["role"])
        return items

    def close(self) -> None:
        self.memory.close()
