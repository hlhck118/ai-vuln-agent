# -*- coding: utf-8 -*-
"""配置加载：TOML（Python 3.11+ 标准库 tomllib）或 JSON。

设计要点：
  * 默认值即"可离线跑通"的保守配置；
  * 云端模型默认关闭（allow_cloud = false），只有显式打开才会外发任何数据；
  * 所有预算类参数都可在配置中调整，避免把成本写死在代码里。
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class ModelConfig:
    # 默认离线确定性推理：不配置任何模型也能完整跑通，且结果可复现
    provider: str = "heuristic"          # openai_compat / anthropic / ollama / heuristic
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "deepseek-r1-distill"
    api_key_env: str = "VULNAGENT_API_KEY"
    # 直接传入的密钥（控制面下发的模型配置走这里）；优先于 api_key_env。
    # 单独放一个字段而不是塞进环境变量，是为了避免把密钥写进进程全局状态。
    api_key: str = ""
    temperature_hypothesis: float = 0.7
    temperature_verdict: float = 0.2
    max_tokens: int = 2048
    timeout_s: int = 90
    allow_cloud: bool = False            # 私有化优先，默认禁止外发
    cloud_base_url: str = "https://api.example.com/v1"
    cloud_model: str = ""
    fallback_to_heuristic: bool = True   # 模型不可用时降级为离线启发式


@dataclass
class BudgetConfig:
    max_rounds: int = 8
    max_tool_calls: int = 60
    max_tokens: int = 400000
    max_hypotheses_per_slice: int = 3
    max_slices: int = 100
    concurrency: int = 4
    escalate_after_failures: int = 2


@dataclass
class SandboxConfig:
    backend: str = "subprocess"          # subprocess / docker
    docker_image: str = "python:3.12-slim"
    timeout_s: int = 20
    memory_mb: int = 512
    allow_network: bool = False
    max_output_bytes: int = 65536
    # 请求 docker 但容器运行时不可用时是否允许降级为进程级。
    # 生产环境建议置为 false：宁可任务失败，也不要把不可信用例放进宿主进程。
    allow_fallback: bool = True


@dataclass
class ToolsConfig:
    enabled: List[str] = field(default_factory=lambda: [
        "builtin_rules", "repo_map", "ast_graph", "sandbox_exec", "poc_generator"])
    codeql_path: str = ""
    semgrep_path: str = ""
    fuzzer_path: str = ""


@dataclass
class MemoryConfig:
    enabled: bool = True
    db_path: str = "memory/cases.db"
    top_k: int = 3


@dataclass
class RunConfig:
    name: str = "default"
    output_dir: str = "out"
    languages: List[str] = field(default_factory=lambda: [".py", ".java", ".c", ".cpp", ".go", ".js", ".ts"])
    exclude_dirs: List[str] = field(default_factory=lambda: [
        ".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv", "target"])
    max_file_bytes: int = 512 * 1024
    headless: bool = True


@dataclass
class AppConfig:
    run: RunConfig = field(default_factory=RunConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    def to_dict(self):
        return json.loads(json.dumps(asdict(self), ensure_ascii=False))


def _merge(dc, data: Dict):
    for key, value in (data or {}).items():
        if not hasattr(dc, key):
            continue
        current = getattr(dc, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(dc, key, value)
    return dc


def load_config(path: Optional[str] = None) -> AppConfig:
    """读取配置文件；不存在则返回默认配置（离线可跑）。"""
    cfg = AppConfig()
    if not path:
        return cfg
    if not os.path.exists(path):
        raise FileNotFoundError("配置文件不存在: %s" % path)
    with open(path, "rb") as handle:
        raw = handle.read()
    if path.endswith(".json"):
        data = json.loads(raw.decode("utf-8"))
    else:
        import tomllib
        data = tomllib.loads(raw.decode("utf-8"))
    _merge(cfg.run, data.get("run", {}))
    _merge(cfg.model, data.get("model", {}))
    _merge(cfg.budget, data.get("budget", {}))
    _merge(cfg.sandbox, data.get("sandbox", {}))
    _merge(cfg.tools, data.get("tools", {}))
    _merge(cfg.memory, data.get("memory", {}))
    if cfg.model.allow_cloud and not cfg.model.cloud_model:
        raise ValueError("allow_cloud=true 时必须显式配置 cloud_model")
    return cfg
