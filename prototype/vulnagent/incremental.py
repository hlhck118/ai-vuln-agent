# -*- coding: utf-8 -*-
"""增量扫描：只审"变更文件及其影响面"，让系统能嵌入持续集成流水线。

设计要点与边界（必须先说清楚，否则容易误解）：

  * **省的是推理与验证成本，不是解析成本。** 调用图与可达性必须全量构建，
    否则无法判断"某个未变更的函数是否被变更的入口影响"。图谱构建是确定性
    计算，成本远低于模型推理与沙箱验证，因此这个取舍是划算的。
  * **影响面按双向传播计算。** 变更函数的调用者（上游入口）与被调用者（下游
    汇聚点）都要纳入：前者决定"缺陷是否可达"，后者决定"改动是否引入新的
    危险路径"。
  * **删除文件同样影响可达性。** 被删除文件中的符号会从图中消失，可能让原本
    被判为可达的路径变为不可达，因此删除项计入影响面。
  * **不做增量时行为完全不变。** 增量只是给规划阶段加一个过滤器。

对外提供两种变更来源：
  1. 清单比对（默认）：与上次扫描保存的指纹清单比对，适用于任何环境；
  2. Git 比对（可选）：目标为 Git 仓库时，直接取 `git diff --name-only <ref>`。
"""
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

MANIFEST_VERSION = 1


# ---------------------------------------------------------------- 清单

def build_manifest(root: str, exts, exclude_dirs, max_bytes: int) -> Dict[str, str]:
    """构建"相对路径 -> 内容摘要"的指纹清单。"""
    exclude = set(exclude_dirs)
    manifest: Dict[str, str] = {}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1] not in exts:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > max_bytes:
                    continue
                with open(full, "rb") as handle:
                    blob = handle.read()
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            manifest[rel] = hashlib.sha256(blob).hexdigest()[:16]
    return manifest


def save_manifest(path: str, manifest: Dict[str, str]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": MANIFEST_VERSION, "files": manifest},
                  handle, ensure_ascii=False, indent=2)


def load_manifest(path: str) -> Optional[Dict[str, str]]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        return None
    files = data.get("files")
    return files if isinstance(files, dict) else None


@dataclass
class DiffResult:
    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    source: str = "manifest"

    @property
    def changed(self) -> List[str]:
        return sorted(set(self.added) | set(self.modified) | set(self.deleted))

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)

    def to_dict(self) -> Dict[str, object]:
        return {"source": self.source, "added": self.added,
                "modified": self.modified, "deleted": self.deleted,
                "changed_count": len(self.changed)}


def diff_manifests(old: Optional[Dict[str, str]],
                   new: Dict[str, str]) -> DiffResult:
    """比对两次清单，得出新增、修改与删除的文件（相对路径）。"""
    result = DiffResult(source="manifest")
    if old is None:
        result.added = sorted(new)
        return result
    for rel, digest in new.items():
        if rel not in old:
            result.added.append(rel)
        elif old[rel] != digest:
            result.modified.append(rel)
    for rel in old:
        if rel not in new:
            result.deleted.append(rel)
    result.added.sort()
    result.modified.sort()
    result.deleted.sort()
    return result


def git_changed_files(root: str, ref: str) -> Optional[List[str]]:
    """用 git 取变更文件清单；非 Git 仓库或 git 不可用时返回 None。"""
    if not ref:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", root, "diff", "--name-only", ref],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return [line.strip().replace("\\", "/")
            for line in (completed.stdout or "").splitlines() if line.strip()]


# ---------------------------------------------------------------- 影响面

def impacted_units(changed_files: List[str], root: str,
                   edges: List[Tuple[str, str]],
                   all_keys: Set[str], depth: int = 2) -> Set[str]:
    """计算受影响的分析单元。

    规则：
      1. 变更文件内的所有单元都是直接受影响单元；
      2. 沿调用边向上游追溯 depth 层（谁调用了它 —— 决定可达性）；
      3. 沿调用边向下游追溯 depth 层（它调用了谁 —— 决定危险汇聚面）。
    """
    root = os.path.abspath(root)
    changed_paths = {os.path.abspath(os.path.join(root, rel)) for rel in changed_files}
    direct = {key for key in all_keys if key.split("::")[0] in changed_paths}

    adjacency: Dict[str, Set[str]] = {}
    reverse: Dict[str, Set[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, set()).add(dst)
        reverse.setdefault(dst, set()).add(src)

    impacted = set(direct)
    frontier = set(direct)
    for _ in range(max(0, depth)):
        nxt: Set[str] = set()
        for node in frontier:
            nxt |= adjacency.get(node, set()) | reverse.get(node, set())
        nxt -= impacted
        if not nxt:
            break
        impacted |= nxt
        frontier = nxt
    return impacted


@dataclass
class IncrementalPlan:
    """增量扫描的执行计划与统计，用于写进报告。"""
    enabled: bool = False
    diff: Optional[DiffResult] = None
    impacted: Set[str] = field(default_factory=set)
    total_slices: int = 0
    selected_slices: int = 0
    skipped_slices: int = 0
    manifest_path: str = ""

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "enabled": self.enabled,
            "manifest_path": self.manifest_path,
            "total_slices": self.total_slices,
            "selected_slices": self.selected_slices,
            "skipped_slices": self.skipped_slices,
            "impacted_units": len(self.impacted),
        }
        if self.diff:
            data["diff"] = self.diff.to_dict()
        return data
