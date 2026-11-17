# -*- coding: utf-8 -*-
"""工具与执行层。

包含三部分：
  1. ToolRegistry —— 工具白名单与调用审计（大模型只能通过它触达工具）；
  2. SandboxRunner —— 隔离执行验证用例，产出客观执行证据；
  3. 差分验证 —— 同一用例分别打在缺陷版与修复版，只有"缺陷版复现、修复版不复现"
     才判定漏洞成立，从机制上消除误报。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import SandboxConfig

RESULT_PREFIX = "VULNAGENT_RESULT "

# ---------------------------------------------------------------- 工具注册表

@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Any]
    dangerous: bool = False      # 是否会产生对外动作（探测/执行）


class ToolRegistry:
    """工具白名单。未注册的工具一律拒绝调用，并记录审计事件。"""

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}
        self.audit: List[Dict[str, Any]] = []
        self.calls = 0

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> List[str]:
        return sorted(self._tools)

    def invoke(self, name: str, **kwargs) -> Any:
        if name not in self._tools:
            self.audit.append({"tool": name, "status": "denied",
                               "reason": "工具不在白名单"})
            raise PermissionError("工具未注册，拒绝调用: %s" % name)
        spec = self._tools[name]
        started = time.time()
        try:
            result = spec.handler(**kwargs)
            status = "ok"
        except Exception as exc:                     # 工具失败不抛出，转为可观测事件
            result, status = {"error": repr(exc)}, "error"
        self.calls += 1
        self.audit.append({"tool": name, "status": status,
                           "dangerous": spec.dangerous,
                           "duration_ms": int((time.time() - started) * 1000)})
        return result


# ---------------------------------------------------------------- 沙箱执行

@dataclass
class ExecResult:
    ok: bool = False
    timed_out: bool = False
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    parsed: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    backend: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "timed_out": self.timed_out, "exit_code": self.exit_code,
            "duration_ms": self.duration_ms, "parsed": self.parsed,
            "backend": self.backend,
            "stdout_tail": self.stdout[-800:], "stderr_tail": self.stderr[-800:],
            "error": self.error,
        }


def _minimal_env(sandbox_dir: str) -> Dict[str, str]:
    """最小化环境变量：不继承代理、密钥等敏感配置。"""
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "HOME": sandbox_dir,
           "TMPDIR": sandbox_dir, "TEMP": sandbox_dir, "TMP": sandbox_dir}
    for key in ("PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "LANG"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


class SandboxRunner:
    """隔离执行器。

    两种后端：
      * subprocess —— 独立临时目录 + 子进程 + 超时 + 输出截断 + 环境变量最小化；
      * docker     —— 容器隔离：无网络、只读目标、CPU/内存限额、进程数限制。

    重要约束：请求 docker 后端但容器运行时不可用时，**不会静默退回** subprocess，
    而是记录一条降级事件并由调用方决定是否接受——因为二者的隔离强度不同，
    静默降级会把"不可信用例"放进宿主进程。可通过 allow_fallback 显式放行。
    """

    def __init__(self, cfg: SandboxConfig, allow_fallback: bool = True):
        self.cfg = cfg
        self.allow_fallback = allow_fallback
        self.executions = 0
        self.degraded: List[str] = []
        self._docker_checked: Optional[bool] = None

    # ------------------------------------------------------------ 能力探测
    def docker_available(self) -> bool:
        if self._docker_checked is not None:
            return self._docker_checked
        found = shutil.which("docker")
        if not found:
            self._docker_checked = False
            return False
        try:
            completed = subprocess.run([found, "info", "--format", "{{.ServerVersion}}"],
                                       capture_output=True, text=True, timeout=15)
            self._docker_checked = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            self._docker_checked = False
        return bool(self._docker_checked)

    def backend_info(self) -> Dict[str, Any]:
        requested = self.cfg.backend
        docker_ok = self.docker_available()
        effective = "docker" if (requested == "docker" and docker_ok) else "subprocess"
        return {
            "requested_backend": requested,
            "docker_available": docker_ok,
            "effective_backend": effective,
            "isolated": effective == "docker",
            "docker_image": self.cfg.docker_image,
            "timeout_s": self.cfg.timeout_s,
        }

    def _resolve_backend(self) -> str:
        info = self.backend_info()
        if info["requested_backend"] == "docker" and not info["docker_available"]:
            note = ("请求容器沙箱但未检测到可用的容器运行时，隔离强度降级为进程级；"
                    "执行外部不可信用例前必须修复")
            if note not in self.degraded:
                self.degraded.append(note)
            if not self.allow_fallback:
                raise RuntimeError(note)
        return info["effective_backend"]

    # ------------------------------------------------------------ 执行
    def run_poc(self, poc_source: str, target_root: str,
                keep_dir: Optional[str] = None) -> ExecResult:
        started = time.time()
        workdir = keep_dir or tempfile.mkdtemp(prefix="vulnagent-sbx-")
        target_copy = os.path.join(workdir, "target")
        poc_path = os.path.join(workdir, "poc_case.py")
        result = ExecResult()
        try:
            backend = self._resolve_backend()
            if os.path.exists(target_copy):
                shutil.rmtree(target_copy, ignore_errors=True)
            shutil.copytree(target_root, target_copy)
            with open(poc_path, "w", encoding="utf-8") as handle:
                handle.write(poc_source)
            if backend == "docker":
                cmd, cwd = self._docker_cmd(workdir), None
            else:
                cmd, cwd = [sys.executable, "-I", "-X", "utf8", poc_path, target_copy], workdir
            result.backend = backend
            try:
                completed = subprocess.run(
                    cmd, cwd=cwd, env=_minimal_env(workdir),
                    capture_output=True, timeout=self.cfg.timeout_s,
                    # 子进程被强制 PYTHONIOENCODING=utf-8（见 _minimal_env），
                    # 父进程就必须按 utf-8 解。用 text=True 会走系统区域编码——
                    # 在中文 Windows 上是 gbk，与子进程输出不匹配，读取线程会直接
                    # 抛 UnicodeDecodeError（而且是在后台线程里，报错很难定位）。
                    encoding="utf-8", errors="replace")
                result.exit_code = completed.returncode
                result.stdout = (completed.stdout or "")[:self.cfg.max_output_bytes]
                result.stderr = (completed.stderr or "")[:self.cfg.max_output_bytes]
                result.ok = completed.returncode == 0
            except subprocess.TimeoutExpired as exc:
                result.timed_out = True
                result.error = "执行超时（%ss）" % self.cfg.timeout_s
                result.stdout = (exc.stdout or b"").decode("utf-8", "ignore") \
                    if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            result.parsed = self._parse(result.stdout)
        except Exception as exc:
            result.error = repr(exc)
        finally:
            self.executions += 1
            result.duration_ms = int((time.time() - started) * 1000)
            if keep_dir is None:
                shutil.rmtree(workdir, ignore_errors=True)
        return result

    def _docker_cmd(self, workdir: str) -> List[str]:
        """容器执行命令：无网络、只读目标目录、资源与进程数限额。"""
        return [
            "docker", "run", "--rm",
            "--network", "none" if not self.cfg.allow_network else "bridge",
            "--memory", "%dm" % self.cfg.memory_mb,
            "--memory-swap", "%dm" % self.cfg.memory_mb,
            "--cpus", "1",
            "--pids-limit", "128",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m,noexec",
            "-v", "%s:/w:rw" % workdir,
            "-w", "/w",
            self.cfg.docker_image,
            "python", "-I", "/w/poc_case.py", "/w/target",
        ]

    @staticmethod
    def _parse(stdout: str) -> Dict[str, Any]:
        for line in reversed((stdout or "").splitlines()):
            if line.startswith(RESULT_PREFIX):
                try:
                    return json.loads(line[len(RESULT_PREFIX):])
                except json.JSONDecodeError:
                    return {}
        return {}


# ---------------------------------------------------------------- PoC 生成

POC_HEADER = '''# -*- coding: utf-8 -*-
"""自动生成的验证用例（由 vulnagent 生成，禁止人工修改后复用）。"""
import base64
import importlib
import json
import os
import pathlib
import pickle
import sys

TARGET = sys.argv[1]
sys.path.insert(0, TARGET)
MODULE = "%(module)s"
FUNC = "%(func)s"
result = {}
try:
    mod = importlib.import_module(MODULE)
    fn = getattr(mod, FUNC, None)
    if fn is None:
        print("VULNAGENT_RESULT " + json.dumps({"error": "目标函数不存在: " + FUNC}))
        sys.exit(0)
except Exception as exc:                     # 模块无法导入即视为不可验证
    print("VULNAGENT_RESULT " + json.dumps({"import_error": repr(exc)}))
    sys.exit(0)
'''

POC_FOOTER = '''
print("VULNAGENT_RESULT " + json.dumps(result, ensure_ascii=False))
'''

POC_SQL = '''
factory = getattr(mod, "get_conn", None)
if factory is None:
    result["error"] = "目标模块未提供连接工厂 get_conn，无法构造差分用例"
else:
    try:
        conn = factory()
        rows = fn(conn, "' OR '1'='1")
        result["rows"] = len(rows)
        result["data"] = [list(r) for r in rows]
    except Exception as exc:
        result["error"] = repr(exc)
'''

POC_SHELL = '''
SEP = "&" if os.name == "nt" else ";"
try:
    out = fn("hello " + SEP + " echo VULNAGENT_MARK")
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    result["lines"] = lines
    result["marker_line"] = any(l == "VULNAGENT_MARK" for l in lines)
except Exception as exc:
    result["error"] = repr(exc)
'''

POC_PATH = '''
TOKEN = "VULNAGENT_LEAK_9f2c41"
try:
    with open(os.path.join(TARGET, "leaked_secret.txt"), "w", encoding="utf-8") as fh:
        fh.write(TOKEN)
    data = fn("../leaked_secret.txt") or ""
    result["leaked"] = TOKEN in data
except Exception as exc:
    result["leaked"] = False
    result["error"] = repr(exc)
'''

POC_DESER = '''
MARKER = os.path.join(os.getcwd(), "vulnagent_pwn.marker")
if os.path.exists(MARKER):
    os.remove(MARKER)

class Evil:
    def __reduce__(self):
        return (os.system, ("echo VULNAGENT_PWN > " + MARKER,))

try:
    blob = base64.b64encode(pickle.dumps(Evil())).decode()
    fn(blob)
except Exception as exc:
    result["error"] = repr(exc)
result["marker"] = os.path.exists(MARKER)
'''

# 弱哈希：用常见口令的已知摘要判断"是否可被彩虹表直接还原"。
# 这是一个客观、可执行、可差分的性质：修复版改用 PBKDF2 后必然不匹配。
POC_WEAK_HASH = '''
KNOWN_MD5_OF_123456 = "e10adc3949ba59abbe56e057f20f883e"
try:
    value = fn("123456")
    result["digest"] = str(value)[:64]
    result["recovered"] = (str(value).lower() == KNOWN_MD5_OF_123456)
except Exception as exc:
    result["recovered"] = False
    result["error"] = repr(exc)
'''

POC_XSS = '''
PAYLOAD = "<script>alert(1)</script>"
try:
    out = fn(PAYLOAD) or ""
    result["raw_payload"] = PAYLOAD in out
    result["output_head"] = out[:120]
except Exception as exc:
    result["raw_payload"] = False
    result["error"] = repr(exc)
'''

POC_CODE_INJECTION = '''
MARKER = os.path.join(os.getcwd(), "vulnagent_eval.marker")
if os.path.exists(MARKER):
    os.remove(MARKER)
# 用 pathlib 落盘，避免 shell 引号与空格带来的不可移植问题
PAYLOAD = "__import__('pathlib').Path(%r).write_text('VULNAGENT_EVAL')" % MARKER
try:
    fn(PAYLOAD)
except Exception as exc:
    result["error"] = repr(exc)
result["marker"] = os.path.exists(MARKER)
'''

POC_SSRF = '''
TOKEN = "VULNAGENT_SSRF_5c1d"
secret_path = os.path.join(TARGET, "ssrf_secret.txt")
try:
    with open(secret_path, "w", encoding="utf-8") as fh:
        fh.write(TOKEN)
    body = fn(pathlib.Path(secret_path).as_uri()) or ""
    result["leaked"] = TOKEN in body
except Exception as exc:
    result["leaked"] = False
    result["error"] = repr(exc)
'''

# CWE -> (PoC 片段, 效果判定函数, 说明)
POC_TEMPLATES: Dict[str, Tuple[str, Callable[[Dict[str, Any]], bool], str]] = {
    "CWE-89": (POC_SQL, lambda d: int(d.get("rows", 0)) >= 2,
               "注入载荷使查询返回了多于正常调用的记录数"),
    "CWE-78": (POC_SHELL, lambda d: bool(d.get("marker_line")),
               "拼接载荷执行了第二条命令（输出出现独立标记行）"),
    "CWE-22": (POC_PATH, lambda d: bool(d.get("leaked")),
               "读取到了文档目录之外的文件内容"),
    "CWE-502": (POC_DESER, lambda d: bool(d.get("marker")),
                "反序列化过程执行了攻击者构造的副作用命令"),
    "CWE-327": (POC_WEAK_HASH, lambda d: bool(d.get("recovered")),
                "常见口令的摘要与已知彩虹表值一致，可被直接还原"),
    "CWE-79": (POC_XSS, lambda d: bool(d.get("raw_payload")),
               "恶意脚本未经转义直接出现在输出中"),
    "CWE-95": (POC_CODE_INJECTION, lambda d: bool(d.get("marker")),
               "eval/exec 执行了载荷并产生副作用"),
    "CWE-918": (POC_SSRF, lambda d: bool(d.get("leaked")),
                "服务端按外部输入的 URL 读取了本地文件"),
}


def build_poc(cwe: str, file_path: str, func: str) -> Optional[str]:
    """按 CWE 生成验证用例源码；不支持的类型返回 None（转为静态确认）。"""
    if cwe not in POC_TEMPLATES:
        return None
    module = os.path.splitext(os.path.basename(file_path))[0]
    body = POC_TEMPLATES[cwe][0]
    return POC_HEADER % {"module": module, "func": func} + body + POC_FOOTER


def poc_effect(cwe: str, parsed: Dict[str, Any]) -> bool:
    entry = POC_TEMPLATES.get(cwe)
    if not entry:
        return False
    try:
        return bool(entry[1](parsed or {}))
    except Exception:
        return False


def poc_explanation(cwe: str) -> str:
    entry = POC_TEMPLATES.get(cwe)
    return entry[2] if entry else ""


# ---------------------------------------------------------------- 差分验证

class DifferentialVerifier:
    """差分验证器：缺陷版复现 + 修复版不复现 => 判定漏洞成立。"""

    def __init__(self, runner: SandboxRunner, registry: Optional[ToolRegistry] = None,
                 patched_root: Optional[str] = None):
        self.runner = runner
        self.registry = registry
        self.patched_root = patched_root
        self.stats = {"verified": 0, "refuted": 0, "inconclusive": 0, "static": 0}

    def _execute(self, poc: str, target_root: str) -> ExecResult:
        """统一经由工具白名单执行，保证每次执行都有审计记录。"""
        if self.registry is None:
            return self.runner.run_poc(poc, target_root)
        result = self.registry.invoke("sandbox_exec", poc_source=poc,
                                      target_root=target_root)
        if isinstance(result, ExecResult):
            return result
        fallback = ExecResult(error="沙箱工具返回异常: %r" % (result,))
        return fallback

    def verify(self, hypothesis, vuln_root: str) -> Dict[str, Any]:
        cwe = hypothesis.cwe
        poc = build_poc(cwe, hypothesis.file, hypothesis.sink_symbol)
        if poc is None:
            self.stats["static"] += 1
            return {
                "verdict": "confirmed_static", "mode": "static", "poc": "",
                "runs": [], "observed": {}, "duration_ms": 0,
                "note": "该类型无法构造可执行验证用例，依据源码证据人工确认；"
                        "交付时须标注证据来源为静态分析。",
            }

        runs: List[Dict[str, Any]] = []
        vuln = self._execute(poc, vuln_root)
        runs.append({"target": "vulnerable", **vuln.to_dict()})

        # 关键约束：拿不到执行结果（沙箱异常/超时/用例未输出）时，
        # 只能判"待人工确认"，绝不能当成"没复现"——后者会把真实漏洞误判成误报。
        if not vuln.parsed:
            self.stats["inconclusive"] += 1
            return {
                "verdict": "inconclusive", "mode": "dynamic", "poc": poc,
                "runs": runs, "observed": {"executed": False},
                "duration_ms": sum(r.get("duration_ms", 0) for r in runs),
                "note": "缺陷版未取得执行结果（沙箱异常或超时），无法判定：%s"
                        % (vuln.error or "用例无结构化输出"),
            }
        effect_vuln = poc_effect(cwe, vuln.parsed)
        # 缺陷版上"没效果"但伴随异常，说明用例没能真正执行完（例如调用签名不匹配、
        # 目标函数抛错）。这属于"没验证成功"，不能据此判定漏洞不存在——否则会把
        # 真漏洞误报为误报。只有用例正常执行完成、判定条件明确不成立，才算误报。
        if not effect_vuln and vuln.parsed.get("error"):
            self.stats["inconclusive"] += 1
            return {
                "verdict": "inconclusive", "mode": "dynamic", "poc": poc,
                "runs": runs,
                "observed": {"executed": True, "effect_on_vulnerable": False,
                             "poc_error": str(vuln.parsed.get("error"))[:200]},
                "duration_ms": sum(r.get("duration_ms", 0) for r in runs),
                "note": "验证用例未能在缺陷版上完成执行（%s），无法判定，转人工确认"
                        % str(vuln.parsed.get("error"))[:120],
            }

        effect_patched = None
        if self.patched_root:
            patched = self._execute(poc, self.patched_root)
            runs.append({"target": "patched", **patched.to_dict()})
            if not patched.parsed:
                # 修复版无结果时同样不能反推"修复版不复现"，否则会假阳性确认
                self.stats["inconclusive"] += 1
                return {
                    "verdict": "inconclusive", "mode": "dynamic", "poc": poc,
                    "runs": runs,
                    "observed": {"effect_on_vulnerable": effect_vuln,
                                 "patched_executed": False},
                    "duration_ms": sum(r.get("duration_ms", 0) for r in runs),
                    "note": "修复版未取得执行结果，缺少有效差分基线，转人工确认",
                }
            effect_patched = poc_effect(cwe, patched.parsed)

        if effect_vuln and effect_patched is False:
            verdict, note = "verified", "缺陷版复现效果、修复版未复现，差分成立"
            self.stats["verified"] += 1
        elif effect_vuln and effect_patched is None:
            verdict, note = "inconclusive", "缺陷版复现效果，但未提供修复版基线，无法差分确认"
            self.stats["inconclusive"] += 1
        elif effect_vuln and effect_patched:
            verdict, note = "inconclusive", "两版均复现，用例不具区分度，转人工确认"
            self.stats["inconclusive"] += 1
        else:
            verdict, note = "refuted", "缺陷版未复现效果，判定为误报并从交付清单剔除"
            self.stats["refuted"] += 1

        return {
            "verdict": verdict, "mode": "dynamic", "poc": poc, "runs": runs,
            "observed": {"effect_on_vulnerable": effect_vuln,
                         "effect_on_patched": effect_patched},
            "duration_ms": sum(r.get("duration_ms", 0) for r in runs),
            "note": note,
        }


# ---------------------------------------------------------------- 外部工具适配

def run_external_sast(paths: List[str], tools_cfg) -> Dict[str, Any]:
    """调用外部静态分析工具（CodeQL / Semgrep）。

    设计约定：外部工具是"能力增强项"而非依赖项。未安装或未配置时返回
    available=False，由编排器记录一条降级事件，主流程继续用内置规则引擎。
    """
    import shutil as _shutil
    probes = []
    if tools_cfg.codeql_path:
        probes.append(("codeql", tools_cfg.codeql_path))
    elif _shutil.which("codeql"):
        probes.append(("codeql", _shutil.which("codeql")))
    if tools_cfg.semgrep_path:
        probes.append(("semgrep", tools_cfg.semgrep_path))
    elif _shutil.which("semgrep"):
        probes.append(("semgrep", _shutil.which("semgrep")))
    if not probes:
        return {"available": False, "reason": "未安装或未配置 CodeQL/Semgrep",
                "results": []}
    results = []
    for name, exe in probes:
        try:
            completed = subprocess.run([exe, "--version"], capture_output=True,
                                       text=True, timeout=20)
            results.append({"tool": name, "path": exe, "exit_code": completed.returncode,
                            "version": (completed.stdout or completed.stderr).strip()[:120]})
        except Exception as exc:
            results.append({"tool": name, "path": exe, "error": repr(exc)})
    return {"available": True, "results": results, "scanned_paths": len(paths)}
