# -*- coding: utf-8 -*-
"""符号解析与调用图构建。

解决的问题：早期版本用"调用写法最后一段"（如 `save`）做调用边匹配，会把
不同类、不同模块的同名方法混为一谈，导致可达性判断既可能漏（把真实路径判为
不可达）也可能错（把无关路径判为可达）。本模块引入作用域与轻量类型推断：

  1. 作用域：模块级函数、类与方法分别建立限定名（module::Class.method）；
  2. 导入解析：`import a.b as c`、`from a import b as c` 均解析到确切符号；
  3. 轻量类型推断：`x = UserDao()`、`x: UserDao = ...`、`self.x = UserDao()`
     与函数注解可推断出接收者类型，从而把 `self.dao.save()` 解析到
     `dao::UserDao.save`，而不是任意的 `save`；
  4. 未解析留痕：动态调用、反射、回调等无法静态确定的目标如实记为 unknown，
     并统计解析率，供反证环节下调置信度使用。

**键格式约定**：所有调用方与目标统一使用 `文件路径::限定名`（如
`/proj/dao.py::UserDao.save`）。统一键格式是可达性分析正确的前提——一旦
调用方用路径、被调用方用模块名，边就接不上，可达性会静默失效。

设计上坚持"宁可标未知，不可猜一个"：解析不确定时返回 unknown，而不是回落到
按名字匹配——后者正是误报与漏报的来源。
"""
import ast
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

BUILTIN_CALLABLES = {
    "print", "len", "open", "eval", "exec", "compile", "input", "range",
    "enumerate", "zip", "sorted", "list", "dict", "set", "tuple", "str", "int",
    "float", "bool", "bytes", "type", "isinstance", "getattr", "setattr",
    "hasattr", "super", "format", "repr", "hash", "map", "filter", "sum",
    "min", "max", "abs", "any", "all", "iter", "next", "dir", "vars", "id",
    "__import__",
}

ENTRY_NAMES = {"main", "dispatch", "handler", "app", "run", "cli", "entrypoint"}


def make_key(path: str, qualname: str) -> str:
    """统一的符号键：文件路径::限定名。"""
    return "%s::%s" % (path, qualname)


# ---------------------------------------------------------------- 数据结构

@dataclass
class ClassInfo:
    name: str
    line: int = 0
    methods: Dict[str, int] = field(default_factory=dict)     # 方法名 -> 行号
    bases: List[str] = field(default_factory=list)
    attr_types: Dict[str, str] = field(default_factory=dict)  # self.x 的推断类型


@dataclass
class FileSymbols:
    path: str
    module: str
    functions: Dict[str, int] = field(default_factory=dict)   # 模块级函数 -> 行号
    classes: Dict[str, ClassInfo] = field(default_factory=dict)
    imports: Dict[str, Tuple[str, str]] = field(default_factory=dict)   # 别名 -> (模块, 原名)
    module_aliases: Dict[str, str] = field(default_factory=dict)        # 别名 -> 模块路径
    tree: Optional[ast.AST] = None                                      # 语法树（不参与序列化）


@dataclass
class CallSite:
    caller: str          # 调用方：文件路径::限定名
    raw: str             # 源码中的调用写法
    target: str          # 被调用方：文件路径::限定名；未解析为 unknown，外部为 external:xxx
    kind: str            # local/method/instantiation/cross_module/cross_module_method/external/builtin/unknown
    line: int = 0
    confidence: float = 0.0

    @property
    def is_internal(self) -> bool:
        """目标是否落在被分析代码集合内（只有内部目标才构成调用边）。"""
        return self.kind in ("local", "method", "instantiation",
                             "cross_module", "cross_module_method")


# ---------------------------------------------------------------- 索引构建

class CallGraphIndex:
    """被分析代码集合的符号索引。"""

    def __init__(self):
        self.files: Dict[str, FileSymbols] = {}       # module -> FileSymbols
        self.by_path: Dict[str, FileSymbols] = {}     # path -> FileSymbols
        self.class_index: Dict[str, str] = {}         # 类名 -> 模块（同名取先出现者）
        self.sites: List[CallSite] = []
        self.parse_errors: List[str] = []

    # ---------------------------------------------------------- 索引
    def add_file(self, path: str, source: str) -> Optional[FileSymbols]:
        module = os.path.splitext(os.path.basename(path))[0]
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            self.parse_errors.append("%s: %s" % (path, exc))
            return None
        fs = FileSymbols(path=path, module=module, tree=tree)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fs.functions[node.name] = node.lineno
            elif isinstance(node, ast.ClassDef):
                ci = ClassInfo(name=node.name, line=node.lineno,
                               bases=[self._dotted(b) or "" for b in node.bases])
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        ci.methods[sub.name] = sub.lineno
                fs.classes[node.name] = ci
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        fs.module_aliases[alias.asname] = alias.name
                    else:
                        fs.module_aliases[alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    fs.imports[local] = (mod, alias.name)
        self.files[module] = fs
        self.by_path[path] = fs
        for name in fs.classes:
            self.class_index.setdefault(name, module)
        return fs

    # ---------------------------------------------------------- 解析
    def resolve_all(self) -> None:
        for module, fs in self.files.items():
            for node, qual, cls in self._iter_functions(fs):
                self._resolve_function(fs, node, qual, cls)

    def _iter_functions(self, fs: FileSymbols):
        """遍历模块级函数与类方法，产出 (节点, 限定名, 所属类信息)。"""
        tree = fs.tree
        if tree is None:
            return []
        items = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                items.append((node, node.name, None))
            elif isinstance(node, ast.ClassDef):
                ci = fs.classes[node.name]
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        items.append((sub, "%s.%s" % (node.name, sub.name), ci))
        return items

    def _resolve_function(self, fs: FileSymbols, node, qual: str,
                          cls: Optional[ClassInfo]) -> None:
        caller = make_key(fs.path, qual)
        params = {a.arg for a in list(node.args.args) + list(node.args.kwonlyargs)}
        params -= {"self", "cls"}
        local_types: Dict[str, str] = dict(self._annotations(node))
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
                inferred = self._infer_type(value, fs, cls)
                if not inferred:
                    continue
                if isinstance(target, ast.Name):
                    local_types[target.id] = inferred
                elif isinstance(target, ast.Attribute) and cls is not None \
                        and isinstance(target.value, ast.Name) \
                        and target.value.id in ("self", "cls"):
                    cls.attr_types[target.attr] = inferred
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                self.sites.append(self._resolve_call(
                    sub, fs, caller, cls, local_types, params))

    @staticmethod
    def _annotations(node) -> List[Tuple[str, str]]:
        """函数参数注解，如 `dao: UserDao`，用于推断接收者类型。"""
        out = []
        for a in list(node.args.args) + list(node.args.kwonlyargs):
            ann = a.annotation
            if isinstance(ann, ast.Name):
                out.append((a.arg, ann.id))
            elif isinstance(ann, ast.Attribute):
                out.append((a.arg, ann.attr))
        return out

    # ---------------------------------------------------------- 内部目标
    def _internal(self, module: str, qual: str, caller: str, raw: str,
                  kind: str, line: int, confidence: float) -> Optional[CallSite]:
        """构造内部调用点；模块不在被分析集合内时返回 None。"""
        fs = self.files.get(module)
        if fs is None:
            return None
        return CallSite(caller, raw, make_key(fs.path, qual), kind, line, confidence)

    # ---------------------------------------------------------- 单点解析
    def _resolve_call(self, call: ast.Call, fs: FileSymbols, caller: str,
                      cls: Optional[ClassInfo], local_types: Dict[str, str],
                      params: Set[str]) -> CallSite:
        raw = self._dotted(call.func) or "<expr>"
        line = getattr(call, "lineno", 0)
        func = call.func

        # ---- 直接名字调用：func(...)
        if isinstance(func, ast.Name):
            name = func.id
            if name in local_types and local_types[name] in fs.classes:
                return self._internal(fs.module, local_types[name], caller, raw,
                                      "instantiation", line, 0.9) or \
                    CallSite(caller, raw, "unknown", "unknown", line, 0.2)
            if name in params or name in local_types:
                return CallSite(caller, raw, "unknown", "unknown", line, 0.0)
            if name in fs.classes:
                return CallSite(caller, raw, make_key(fs.path, name),
                                "instantiation", line, 1.0)
            if name in fs.functions:
                return CallSite(caller, raw, make_key(fs.path, name),
                                "local", line, 1.0)
            if name in fs.imports:
                mod, orig = fs.imports[name]
                return self._cross_module(mod, orig or name, caller, raw, line) or \
                    CallSite(caller, raw, "external:%s.%s" % (mod, orig or name),
                             "external", line, 0.6)
            if name in fs.module_aliases:
                return CallSite(caller, raw, "external:%s" % fs.module_aliases[name],
                                "external", line, 0.6)
            if name in BUILTIN_CALLABLES:
                return CallSite(caller, raw, "builtin:%s" % name, "builtin", line, 1.0)
            return CallSite(caller, raw, "unknown", "unknown", line, 0.0)

        # ---- 属性调用：obj.method(...)
        if isinstance(func, ast.Attribute):
            attr = func.attr
            base = func.value

            # self.method() / cls.method()
            if isinstance(base, ast.Name) and base.id in ("self", "cls"):
                return self._resolve_self_call(fs, cls, attr, line, raw, caller)

            # self.holder.method()
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) \
                    and base.value.id in ("self", "cls"):
                if cls is not None:
                    holder_type = cls.attr_types.get(base.attr)
                    if holder_type:
                        return self._resolve_type_method(fs, holder_type, attr,
                                                         line, raw, caller, 0.9)
                return CallSite(caller, raw, "unknown", "unknown", line, 0.2)

            # name.method()：本地实例、模块别名、导入的类或函数
            if isinstance(base, ast.Name):
                n = base.id
                inst_type = local_types.get(n)
                if inst_type:
                    return self._resolve_type_method(fs, inst_type, attr,
                                                     line, raw, caller, 0.85)
                if n in fs.module_aliases:
                    mod = fs.module_aliases[n]
                    target_fs = self.files.get(mod)
                    if target_fs:
                        if attr in target_fs.functions:
                            return CallSite(caller, raw, make_key(target_fs.path, attr),
                                            "cross_module", line, 0.9)
                        if attr in target_fs.classes:
                            return CallSite(caller, raw, make_key(target_fs.path, attr),
                                            "instantiation", line, 0.9)
                    return CallSite(caller, raw, "external:%s.%s" % (mod, attr),
                                    "external", line, 0.7)
                if n in fs.imports:
                    mod, orig = fs.imports[n]
                    target_fs = self.files.get(mod)
                    if target_fs:
                        if orig in target_fs.classes and attr in target_fs.classes[orig].methods:
                            return CallSite(caller, raw,
                                            make_key(target_fs.path, "%s.%s" % (orig, attr)),
                                            "cross_module_method", line, 0.85)
                        if orig in target_fs.functions:
                            return CallSite(caller, raw, make_key(target_fs.path, orig),
                                            "cross_module", line, 0.8)
                    return CallSite(caller, raw, "external:%s.%s.%s" % (mod, orig, attr),
                                    "external", line, 0.5)
                if n in fs.classes and attr in fs.classes[n].methods:
                    return CallSite(caller, raw, make_key(fs.path, "%s.%s" % (n, attr)),
                                    "method", line, 0.8)
                if n in self.class_index:
                    mod = self.class_index[n]
                    target_fs = self.files.get(mod)
                    if target_fs and n in target_fs.classes \
                            and attr in target_fs.classes[n].methods:
                        return CallSite(caller, raw,
                                        make_key(target_fs.path, "%s.%s" % (n, attr)),
                                        "cross_module_method", line, 0.7)
                return CallSite(caller, raw, "unknown", "unknown", line, 0.1)

            # 更长的链：os.path.join(...)
            dotted = self._dotted(base)
            if dotted:
                head = dotted.split(".")[0]
                tail = ".".join(dotted.split(".")[1:] + [attr])
                if head in fs.module_aliases:
                    return CallSite(caller, raw,
                                    "external:%s.%s" % (fs.module_aliases[head], tail),
                                    "external", line, 0.7)
                if head in fs.imports:
                    mod, _orig = fs.imports[head]
                    return CallSite(caller, raw, "external:%s.%s" % (mod, tail),
                                    "external", line, 0.6)
            return CallSite(caller, raw, "unknown", "unknown", line, 0.2)

        return CallSite(caller, raw, "unknown", "unknown", line, 0.0)

    def _resolve_self_call(self, fs: FileSymbols, cls: Optional[ClassInfo], attr: str,
                           line: int, raw: str, caller: str) -> CallSite:
        if cls is None:
            return CallSite(caller, raw, "unknown", "unknown", line, 0.2)
        if attr in cls.methods:
            return CallSite(caller, raw, make_key(fs.path, "%s.%s" % (cls.name, attr)),
                            "method", line, 1.0)
        for base in cls.bases:
            if base in fs.classes and attr in fs.classes[base].methods:
                return CallSite(caller, raw, make_key(fs.path, "%s.%s" % (base, attr)),
                                "method", line, 0.7)
        return CallSite(caller, raw, "unknown", "unknown", line, 0.2)

    def _resolve_type_method(self, fs: FileSymbols, type_name: str, attr: str,
                             line: int, raw: str, caller: str,
                             confidence: float) -> CallSite:
        """把 `实例.方法()` 解析到确切的类方法。"""
        if type_name in fs.classes and attr in fs.classes[type_name].methods:
            return CallSite(caller, raw, make_key(fs.path, "%s.%s" % (type_name, attr)),
                            "method", line, confidence)
        mod = self.class_index.get(type_name)
        if mod:
            target_fs = self.files.get(mod)
            if target_fs and type_name in target_fs.classes \
                    and attr in target_fs.classes[type_name].methods:
                return CallSite(caller, raw,
                                make_key(target_fs.path, "%s.%s" % (type_name, attr)),
                                "cross_module_method", line, confidence - 0.05)
        return CallSite(caller, raw, "unknown", "unknown", line, 0.2)

    def _cross_module(self, mod: str, name: str, caller: str, raw: str,
                      line: int) -> Optional[CallSite]:
        target_fs = self.files.get(mod)
        if not target_fs:
            return None
        if name in target_fs.functions:
            return CallSite(caller, raw, make_key(target_fs.path, name),
                            "cross_module", line, 0.9)
        if name in target_fs.classes:
            return CallSite(caller, raw, make_key(target_fs.path, name),
                            "instantiation", line, 0.9)
        return None

    # ---------------------------------------------------------- 工具
    @staticmethod
    def _dotted(node: ast.AST) -> str:
        parts: List[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name):
            return ".".join(reversed(parts + [cur.func.id]))
        return ""

    def _infer_type(self, value: ast.AST, fs: FileSymbols,
                    cls: Optional[ClassInfo]) -> Optional[str]:
        """轻量类型推断：只识别"变量 = 类名(...)"与"变量 = self"两类确定场景。"""
        if isinstance(value, ast.Call):
            func = value.func
            if isinstance(func, ast.Name):
                if func.id in fs.classes:
                    return func.id
                if func.id in fs.imports:
                    return fs.imports[func.id][1]
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id in fs.module_aliases:
                    return func.attr
                dotted = self._dotted(func.value)
                if dotted and dotted.split(".")[0] in fs.module_aliases:
                    return func.attr
        if isinstance(value, ast.Name) and value.id in ("self", "cls") and cls is not None:
            return cls.name
        return None


# ---------------------------------------------------------------- 调用图

@dataclass
class CallGraph:
    edges: List[Tuple[str, str]] = field(default_factory=list)
    entries: List[str] = field(default_factory=list)
    reachable: Set[str] = field(default_factory=set)
    sites: List[CallSite] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)

    def callees_of(self, caller: str) -> List[str]:
        return [dst for src, dst in self.edges if src == caller]


def build_call_graph(files: List[str], sources: Dict[str, str]) -> CallGraph:
    """从文件集合构建调用图。

    files   : 文件路径列表
    sources : 路径 -> 源码（由调用方一次读入，避免重复 IO）
    """
    index = CallGraphIndex()
    for path in files:
        source = sources.get(path)
        if source is None:
            continue
        index.add_file(path, source)
    index.resolve_all()

    edges = [(site.caller, site.target) for site in index.sites if site.is_internal]

    entries: List[str] = []
    for module, fs in index.files.items():
        tree = fs.tree
        if tree is None:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name in ENTRY_NAMES:
                entries.append(make_key(fs.path, node.name))
            elif isinstance(node, ast.If):
                try:
                    test = ast.unparse(node.test)
                except Exception:
                    continue
                if "__main__" not in test:
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                            and sub.func.id in fs.functions:
                        entries.append(make_key(fs.path, sub.func.id))
    if not entries:      # 没有显式入口时，退化为"所有公开模块级函数都是入口"
        for module, fs in index.files.items():
            for name in fs.functions:
                if not name.startswith("_"):
                    entries.append(make_key(fs.path, name))

    adjacency: Dict[str, Set[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, set()).add(dst)
    reachable, stack = set(), list(dict.fromkeys(entries))
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(adjacency.get(cur, ()))

    internal = [s for s in index.sites if s.is_internal]
    unresolved = [s for s in index.sites if s.kind == "unknown"]
    external = [s for s in index.sites if s.kind == "external"]
    stats = {
        "call_sites": len(index.sites),
        "resolved_internal": len(internal),
        "external": len(external),
        "unresolved": len(unresolved),
        "resolution_rate": round(len(internal) / len(index.sites), 3) if index.sites else 0.0,
        "parse_errors": index.parse_errors,
    }
    return CallGraph(edges=edges, entries=list(dict.fromkeys(entries)),
                     reachable=reachable, sites=index.sites, stats=stats)


def unresolved_by_unit(graph: CallGraph) -> Dict[str, int]:
    """每个调用方的未解析调用数，供反证环节下调置信度。"""
    out: Dict[str, int] = {}
    for site in graph.sites:
        if site.kind == "unknown":
            out[site.caller] = out.get(site.caller, 0) + 1
    return out
