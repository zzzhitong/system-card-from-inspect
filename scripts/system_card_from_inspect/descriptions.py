from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LocalDescriptionSources:
    task_file: Path | None
    readme: Path | None
    eval_yaml: Path | None


def resolve_task_sources(repo_root: Path, task_file: str | None) -> LocalDescriptionSources:
    if not task_file:
        return LocalDescriptionSources(task_file=None, readme=None, eval_yaml=None)

    candidate_roots = [
        repo_root,
        repo_root / "inspect_evals",
    ]
    task_path: Path | None = None
    for root in candidate_roots:
        candidate = (root / task_file).resolve()
        if candidate.exists():
            task_path = candidate
            break
    if task_path is None:
        return LocalDescriptionSources(task_file=None, readme=None, eval_yaml=None)

    parent = task_path.parent
    readme = parent / "README.md"
    eval_yaml = parent / "eval.yaml"
    return LocalDescriptionSources(
        task_file=task_path,
        readme=readme if readme.exists() else None,
        eval_yaml=eval_yaml if eval_yaml.exists() else None,
    )


def load_eval_yaml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def extract_task_docstring(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return ""
    return ast.get_docstring(module) or ""


def extract_readme_summary(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current).strip())
                current = []
            continue
        if line.startswith("```"):
            break
        if line.startswith("#") and current:
            paragraphs.append(" ".join(current).strip())
            current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current).strip())
    for paragraph in paragraphs:
        if len(paragraph) > 40:
            return paragraph
    return paragraphs[0] if paragraphs else ""


def source_quality_label(local_sources: list[str], warnings: list[str]) -> str:
    if "benchmark_hints" in local_sources and ("eval_yaml" in local_sources or "readme" in local_sources):
        return "high"
    if "eval_yaml" in local_sources or "readme" in local_sources:
        return "medium"
    if warnings:
        return "low"
    return "unknown"

