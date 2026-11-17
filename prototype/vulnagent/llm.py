# -*- coding: utf-8 -*-
"""模型接入层：私有化优先、云端可选、不可用时确定性降级。

设计要点（对应方案书中的"双模型分工 + 降级策略"）：
  * ChatModel   只负责"把话说出去、把话说回来"，不关心业务；
  * Reasoner    负责"提出假设 / 反证质询"，有两种实现：
      - LLMReasoner  走真实模型（私有化 OpenAI 兼容接口，或显式开启的云端）；
      - RuleReasoner 纯确定性实现，无模型时保证流程仍然跑得通、可复现；
  * Router       按角色路由（plan / hypothesis / critic / report），并记录降级事件。

任何一次模型失败都不会中断流程，只会产生一条 degraded 记录——这是"宁可降级、
不可阻塞"的工程约束。
"""
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from .analysis import RULE_META
from .config import ModelConfig
from .domain import CWE_NAMES, CodeSlice, Sink

# ---------------------------------------------------------------- 提示词

SYSTEM_AUDIT = (
    "你是资深代码安全审计专家，服务于授权范围内的漏洞发掘任务。"
    "你的结论必须可被证伪：给出漏洞位置、类型、成立所需前置条件与置信度。"
    "严禁编造代码中不存在的位置；没有证据支撑的判断必须放弃。"
    "只输出 JSON，不要输出任何解释性文字。"
)

SYSTEM_CRITIC = (
    "你是安全审计的反证专家。你的任务是尽力推翻给定的漏洞假设："
    "检查前置条件是否可达、是否已有防护与校验、是否属于误报。"
    "只输出 JSON，不要输出任何解释性文字。"
)

HYPOTHESIS_SCHEMA_HINT = (
    '输出 JSON 数组，元素字段：'
    '{"cwe":"CWE-xx","title":"简短标题","line":整数行号,'
    '"source":"污点来源","preconditions":["条件1"],'
    '"confidence":0到1的小数,"severity":"low|medium|high|critical",'
    '"rationale":"判断依据，须引用具体代码"}。'
    "没有把握的条目不要输出。"
)

CRITIC_SCHEMA_HINT = (
    '输出 JSON 对象：{"keep":true/false,"confidence":0到1,'
    '"reason":"质询结论，须引用代码或防护证据","sanitizers":["已发现的防护"]}。'
)


# ---------------------------------------------------------------- ChatModel

class LLMUnavailable(RuntimeError):
    """模型不可用（未配置 / 网络失败 / 返回异常）。"""


class ChatModel:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def chat(self, system: str, user: str, temperature: float,
             max_tokens: int, timeout_s: int) -> str:
        raise NotImplementedError


class NullChat(ChatModel):
    """占位模型：始终不可用，用于纯离线场景。"""
    name = "null"

    def available(self) -> bool:
        return False

    def chat(self, *args, **kwargs) -> str:
        raise LLMUnavailable("未配置可用模型")


class OpenAICompatChat(ChatModel):
    """OpenAI 兼容的 Chat Completions 客户端。

    只依赖标准库 urllib，因此私有化部署（vLLM / LM Studio / llama.cpp /
    Xinference / Ollama 的 /v1 端点）与云端模型（OpenAI / DeepSeek / 通义千问 /
    智谱 / Moonshot / 硅基流动 / OpenRouter / Groq）可以共用同一份代码，
    仅需替换 base_url 与模型名——这是"兼容主流与本地模型"的主通道。

    密钥优先取 `api_key` 直接传入的值（控制面下发的配置），其次才读环境变量。
    """

    def __init__(self, base_url: str, model: str, api_key_env: str,
                 name: str = "openai_compat", api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.api_key = api_key
        self.name = "%s:%s" % (name, model)

    def available(self) -> bool:
        return bool(self.base_url and self.model)

    def resolve_key(self) -> str:
        return self.api_key or os.environ.get(self.api_key_env, "")

    def build_request(self, system: str, user: str, temperature: float,
                      max_tokens: int) -> urllib.request.Request:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        key = self.resolve_key()
        if key:
            headers["Authorization"] = "Bearer %s" % key
        return urllib.request.Request(self.base_url + "/chat/completions",
                                      data=body, headers=headers, method="POST")

    def parse(self, data: dict) -> str:
        return data["choices"][0]["message"]["content"]

    def chat(self, system: str, user: str, temperature: float,
             max_tokens: int, timeout_s: int) -> str:
        request = self.build_request(system, user, temperature, max_tokens)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            raise LLMUnavailable("模型调用失败: %r" % (exc,))
        try:
            return self.parse(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("模型返回结构异常: %r" % (exc,))


class AnthropicChat(OpenAICompatChat):
    """Anthropic 原生 Messages API。

    与 OpenAI 格式的三处差异：路径是 `/messages` 而不是 `/chat/completions`；
    鉴权用 `x-api-key` 而不是 `Authorization: Bearer`；必须带 `anthropic-version`。
    系统提示词是顶层字段而不是 messages 里的一条——这是最容易写错的地方。
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, base_url: str, model: str, api_key_env: str,
                 name: str = "anthropic", api_key: str = ""):
        super().__init__(base_url, model, api_key_env, name, api_key)

    def build_request(self, system: str, user: str, temperature: float,
                      max_tokens: int) -> urllib.request.Request:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"Content-Type": "application/json",
                   "anthropic-version": self.ANTHROPIC_VERSION}
        key = self.resolve_key()
        if key:
            headers["x-api-key"] = key
        return urllib.request.Request(self.base_url + "/messages",
                                      data=json.dumps(payload).encode("utf-8"),
                                      headers=headers, method="POST")

    def parse(self, data: dict) -> str:
        blocks = data.get("content") or []
        return "".join(block.get("text", "") for block in blocks
                       if isinstance(block, dict))


class OllamaChat(OpenAICompatChat):
    """Ollama 原生 `/api/chat`（本地部署常用，且默认无需密钥）。"""

    def __init__(self, base_url: str, model: str, api_key_env: str,
                 name: str = "ollama", api_key: str = ""):
        super().__init__(base_url, model, api_key_env, name, api_key)

    def build_request(self, system: str, user: str, temperature: float,
                      max_tokens: int) -> urllib.request.Request:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        headers = {"Content-Type": "application/json"}
        key = self.resolve_key()          # 反向代理加了鉴权时才会用到
        if key:
            headers["Authorization"] = "Bearer %s" % key
        return urllib.request.Request(self.base_url + "/api/chat",
                                      data=json.dumps(payload).encode("utf-8"),
                                      headers=headers, method="POST")

    def parse(self, data: dict) -> str:
        return (data.get("message") or {}).get("content", "") or ""


CHAT_BY_PROVIDER = {"openai_compat": OpenAICompatChat,
                    "anthropic": AnthropicChat,
                    "ollama": OllamaChat}


def build_chat(cfg: "ModelConfig", name: str = "primary") -> ChatModel:
    """按配置里的 provider 造出对应的客户端。

    未知协议直接报错而不是悄悄退化成 OpenAI 兼容——退化的结果是请求打到错误路径，
    错误信息会变成"HTTP 404"，排查起来比一句"不支持的协议"贵得多。
    """
    provider = (getattr(cfg, "provider", "") or "heuristic").lower()
    if provider == "heuristic":
        return NullChat()
    factory = CHAT_BY_PROVIDER.get(provider)
    if factory is None:
        raise ValueError("不支持的模型协议：%s（可选：%s）"
                         % (provider, "、".join(sorted(CHAT_BY_PROVIDER))))
    return factory(cfg.base_url, cfg.model, cfg.api_key_env, name=name,
                   api_key=getattr(cfg, "api_key", "") or "")


# ---------------------------------------------------------------- 假设上下文

@dataclass
class HypothesisContext:
    """一次审计推理所需的全部上下文（结构化，避免把整个工程塞进提示词）。"""
    slice: CodeSlice
    sinks: List[Sink] = field(default_factory=list)
    reachable: bool = False
    sanitizers: List[str] = field(default_factory=list)
    memory_hits: List[Dict[str, Any]] = field(default_factory=list)
    unresolved_calls: int = 0        # 单元内无法静态确定的调用数，用于下调置信度

    def to_prompt(self) -> str:
        lines = [
            "【代码切片】文件：%s  符号：%s  行：%d-%d"
            % (os.path.basename(self.slice.path), self.slice.symbol,
               self.slice.start_line, self.slice.end_line),
            "【图谱上下文】" + "；".join(self.slice.graph_context),
            "【静态分析证据】",
        ]
        for sink in self.sinks:
            lines.append("  - 第%d行 %s(%s)：%s"
                         % (sink.line, sink.cwe, CWE_NAMES.get(sink.cwe, ""),
                            sink.snippet))
        if not self.sinks:
            lines.append("  - 无")
        if self.memory_hits:
            lines.append("【历史经验】")
            for hit in self.memory_hits:
                lines.append("  - %s（历史判定：%s）" % (hit.get("title", ""),
                                                        hit.get("verdict", "")))
        lines.append("【代码】")
        lines.append("```python")
        lines.append(self.slice.code)
        lines.append("```")
        lines.append(HYPOTHESIS_SCHEMA_HINT)
        return "\n".join(lines)


# ---------------------------------------------------------------- 确定性推理器

SEVERITY_FROM_CONF = [(0.85, "critical"), (0.7, "high"), (0.5, "medium"), (0.0, "low")]


def _severity_of(confidence: float, base: str) -> str:
    for threshold, sev in SEVERITY_FROM_CONF:
        if confidence >= threshold:
            return sev
    return base


class RuleReasoner:
    """无模型时的确定性推理器：把工具证据转成可证伪假设。"""

    name = "rule-reasoner"

    def propose(self, ctx: HypothesisContext) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for sink in ctx.sinks:
            cwe, base_conf, base_sev, desc = self._meta(sink.cwe)
            confidence = base_conf
            preconds = []
            if sink.tainted_params:
                preconds.append("外部可控输入经参数 %s 进入危险汇聚点"
                                % "、".join(sink.tainted_params))
            else:
                preconds.append("危险汇聚点的入参可被外部影响")
            if ctx.reachable:
                preconds.append("该函数已确认从外部入口可达")
            else:
                confidence -= 0.2
                preconds.append("可达性尚未确认（需人工确认调用链）")
            if ctx.sanitizers:
                confidence -= 0.25
            confidence = max(0.1, min(0.95, confidence))
            items.append({
                "cwe": cwe,
                "title": "%s（%s）" % (CWE_NAMES.get(cwe, cwe), sink.symbol),
                "line": sink.line,
                "source": "、".join(sink.tainted_params) or "未识别的外部输入",
                "preconditions": preconds,
                "confidence": round(confidence, 2),
                "severity": _severity_of(confidence, base_sev),
                "rationale": "%s。命中证据：%s" % (desc, sink.snippet),
            })
        return items

    def challenge(self, hypothesis: Dict[str, Any],
                  ctx: HypothesisContext) -> Dict[str, Any]:
        conf = float(hypothesis.get("confidence", 0.5))
        keep, reasons, sanitizers = True, [], list(ctx.sanitizers)
        if ctx.sanitizers:
            # 路径/命令类缺陷存在规范化或转义处理时，先降级再交给动态验证裁决
            if hypothesis.get("cwe") in ("CWE-22", "CWE-78"):
                keep = False
                reasons.append("函数内已出现防护处理：%s" % "、".join(ctx.sanitizers))
        if not ctx.reachable:
            conf = max(0.1, conf - 0.2)
            reasons.append("未确认从外部入口可达，置信度下调")
        if ctx.unresolved_calls > 0:
            conf = max(0.1, conf - 0.05)
            reasons.append("存在 %d 处无法静态确定的调用，可达性结论存在不确定性"
                           % ctx.unresolved_calls)
        if not ctx.sinks:
            keep = False
            reasons.append("缺少静态证据绑定，按'无证据不采纳'规则剔除")
        return {"keep": keep, "confidence": round(conf, 2),
                "reason": "；".join(reasons) or "未发现推翻该假设的证据",
                "sanitizers": sanitizers}

    @staticmethod
    def _meta(cwe: str):
        for _rule, (rule_cwe, conf, sev, desc) in RULE_META.items():
            if rule_cwe == cwe:
                return cwe, conf, sev, desc
        return cwe, 0.5, "medium", "静态规则命中"


# ---------------------------------------------------------------- 模型推理器

def extract_json(text: str) -> Any:
    """从模型输出中稳健地抽取 JSON（容忍 ```json 代码块与前后废话）。"""
    if not text:
        raise ValueError("空输出")
    cleaned = re.sub(r"```(?:json)?", "", text).strip("` \n")
    for opener, closer in (("[", "]"), ("{", "}")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if 0 <= start < end:
            candidate = cleaned[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise ValueError("未找到合法 JSON")


class LLMReasoner:
    """走真实模型的推理器；失败时按配置降级到 RuleReasoner。"""

    def __init__(self, chat: ChatModel, cfg: ModelConfig, fallback: RuleReasoner):
        self.chat = chat
        self.cfg = cfg
        self.fallback = fallback
        self.name = "llm-reasoner[%s]" % chat.name
        self.degraded: List[str] = []

    def _ask(self, system: str, user: str, temperature: float) -> Any:
        text = self.chat.chat(system, user, temperature,
                              self.cfg.max_tokens, self.cfg.timeout_s)
        return extract_json(text)

    def propose(self, ctx: HypothesisContext) -> List[Dict[str, Any]]:
        if not self.chat.available():
            return self._degrade(ctx, "模型不可用（未配置或已关闭）")
        try:
            data = self._ask(SYSTEM_AUDIT, ctx.to_prompt(),
                             self.cfg.temperature_hypothesis)
        except (LLMUnavailable, ValueError) as exc:
            return self._degrade(ctx, "假设生成失败：%s" % exc)
        if isinstance(data, dict):
            data = [data]
        items: List[Dict[str, Any]] = []
        for raw in data if isinstance(data, list) else []:
            item = self._sanitize(raw, ctx)
            if item:
                items.append(item)
        if not items:
            return self._degrade(ctx, "模型未给出可采纳假设")
        return items

    def challenge(self, hypothesis: Dict[str, Any],
                  ctx: HypothesisContext) -> Dict[str, Any]:
        if not self.chat.available():
            return self.fallback.challenge(hypothesis, ctx)
        user = ("【待质询假设】%s\n【静态证据】%s\n【代码】\n```python\n%s\n```\n%s"
                % (json.dumps(hypothesis, ensure_ascii=False),
                   json.dumps([s.to_dict() for s in ctx.sinks], ensure_ascii=False),
                   ctx.slice.code, CRITIC_SCHEMA_HINT))
        try:
            data = self._ask(SYSTEM_CRITIC, user, self.cfg.temperature_verdict)
            if isinstance(data, dict) and "keep" in data:
                data["confidence"] = float(data.get("confidence",
                                                    hypothesis.get("confidence", 0.5)))
                return data
        except (LLMUnavailable, ValueError) as exc:
            self.degraded.append("反证阶段降级：%s" % exc)
        return self.fallback.challenge(hypothesis, ctx)

    def _degrade(self, ctx: HypothesisContext, reason: str) -> List[Dict[str, Any]]:
        self.degraded.append(reason)
        return self.fallback.propose(ctx)

    def _sanitize(self, raw: Any, ctx: HypothesisContext) -> Optional[Dict[str, Any]]:
        """证据绑定闸门：位置必须落在切片内，否则丢弃。"""
        if not isinstance(raw, dict) or not raw.get("cwe"):
            return None
        try:
            line = int(raw.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        if not (ctx.slice.start_line <= line <= ctx.slice.end_line):
            sink_lines = [s.line for s in ctx.sinks]
            if not sink_lines:
                return None
            line = sink_lines[0]
            raw["rationale"] = (raw.get("rationale", "") +
                                "（模型给出的行号越界，已回落到静态证据行）")
        confidence = raw.get("confidence", 0.5)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.5
        return {
            "cwe": str(raw.get("cwe")),
            "title": str(raw.get("title") or raw.get("cwe")),
            "line": line,
            "source": str(raw.get("source", "")),
            "preconditions": list(raw.get("preconditions") or []),
            "confidence": round(confidence, 2),
            "severity": str(raw.get("severity", "medium")),
            "rationale": str(raw.get("rationale", "")),
        }


# ---------------------------------------------------------------- 路由器

class Router:
    """按角色选择推理器，并记录全部降级事件与路由决策。"""

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.rule = RuleReasoner()
        # 用 build_chat 按 provider 选协议：openai_compat / anthropic / ollama 三种
        # 都能直接用于扫描，而不是只有 OpenAI 兼容那一种
        self.primary_chat = build_chat(cfg, name="private")
        cloud_cfg = replace(cfg, base_url=cfg.cloud_base_url,
                            model=cfg.cloud_model) if cfg.cloud_model else None
        self.cloud_chat = (build_chat(cloud_cfg, name="cloud")
                           if cfg.allow_cloud and cloud_cfg is not None
                           else NullChat())
        self.degraded: List[str] = []
        self.routes: List[Dict[str, str]] = []

    def hypothesizer(self) -> Any:
        """假设生成（角色 hypothesis）。"""
        return self._for_role("hypothesis")

    def critic(self) -> Any:
        """反证质询（角色 critic）：与假设生成同源，但作为独立角色单独记录路由。

        共用同一份 chat 客户端是有意的——反证要用同样的知识去质疑同一个判断，
        换一个模型只会让"谁在质疑"变得不可追溯。但角色必须各留一条路由记录，
        否则报告里看不到反证阶段的推理实现，也就回答不了
        "这条假设是被谁、用什么实现驳回的"。
        """
        return self._for_role("critic")

    def _for_role(self, role: str) -> Any:
        """按角色选推理器：heuristic=纯离线确定性；其余走模型，云端为备选。"""
        if self.cfg.provider == "heuristic":
            self._record(role, self.rule.name)
            return self.rule
        chat = self.primary_chat if self.primary_chat.available() else self.cloud_chat
        reasoner = LLMReasoner(chat, self.cfg, self.rule)
        self._record(role, reasoner.name)
        return reasoner

    def _record(self, role: str, impl: str) -> None:
        self.routes.append({"role": role, "impl": impl})

    def collect_degraded(self) -> List[str]:
        return list(self.degraded)
