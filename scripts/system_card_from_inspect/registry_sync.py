from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import slugify


GROUP_DIMENSION_MAP = {
    "Safeguards": "safeguards",
    "Bias": "fairness",
    "Scheming": "agentic_safety",
    "Knowledge": "knowledge",
    "Reasoning": "reasoning",
    "Mathematics": "mathematics",
    "Multimodal": "multimodal",
    "Coding": "coding",
    "Cybersecurity": "cybersecurity",
    "Assistants": "assistants",
    "Personality": "personality",
    "Writing": "writing",
}

ACCURACY_LIKE_GROUPS = {
    "Bias",
    "Knowledge",
    "Mathematics",
    "Multimodal",
    "Reasoning",
}


def sync_benchmark_registry_from_inspect_evals(
    inspect_root: str | Path,
    registry_path: str | Path,
) -> dict[str, Any]:
    inspect_root = Path(inspect_root)
    registry_path = Path(registry_path)
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    benchmarks = registry.get("benchmarks", {})
    if not isinstance(benchmarks, dict):
        raise ValueError("benchmark_registry.yaml does not contain a `benchmarks` mapping.")

    benchmarks = dict(benchmarks)
    existing_task_names = {entry.get("task_name") for entry in benchmarks.values() if isinstance(entry, dict)}
    added: list[str] = []

    for eval_yaml in sorted((inspect_root / "src" / "inspect_evals").rglob("eval.yaml")):
        data = yaml.safe_load(eval_yaml.read_text(encoding="utf-8")) or {}
        tasks = data.get("tasks") or []
        if not tasks:
            continue
        folder = eval_yaml.parent.name
        title = data.get("title")
        description = (data.get("description") or "").strip().replace("\n", " ")
        group = data.get("group") or "UNSPECIFIED"
        for task in tasks:
            task_name = task.get("name")
            if not task_name or task_name in existing_task_names:
                continue
            benchmark_id = unique_benchmark_id(task_name, benchmarks)
            benchmarks[benchmark_id] = build_registry_entry(
                task_name=task_name,
                folder=folder,
                title=title,
                description=description,
                group=group,
                source_eval_yaml=eval_yaml.relative_to(inspect_root / "src" / "inspect_evals"),
            )
            added.append(benchmark_id)
            existing_task_names.add(task_name)

    registry["benchmarks"] = dict(benchmarks)
    registry_path.write_text(
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {
        "added_count": len(added),
        "added_benchmark_ids": added,
        "total_registry_entries": len(benchmarks),
    }


def unique_benchmark_id(task_name: str, benchmarks: dict[str, Any]) -> str:
    candidate = task_name
    if candidate not in benchmarks:
        return candidate
    counter = 2
    while f"{candidate}_{counter}" in benchmarks:
        counter += 1
    return f"{candidate}_{counter}"


def build_registry_entry(
    task_name: str,
    folder: str,
    title: str | None,
    description: str,
    group: str,
    source_eval_yaml: Path,
) -> dict[str, Any]:
    return {
        "display_name": generated_display_name(task_name, folder, title),
        "task_name": task_name,
        "match": {},
        "dimension": GROUP_DIMENSION_MAP.get(group, slugify(group)),
        "summary_focus": first_sentence(description) or title or generated_display_name(task_name, folder, title),
        "primary_metrics": generated_primary_metrics(group),
        "sample_selection": generated_sample_selection(group),
        "auto_generated": True,
        "eval_group": group,
        "source_eval_yaml": str(source_eval_yaml).replace("\\", "/"),
    }


def generated_display_name(task_name: str, folder: str, title: str | None) -> str:
    title_short = (title or folder).split(":", 1)[0].strip()
    normalized_folder = folder.replace("-", "_")
    if task_name == normalized_folder:
        return title_short
    if task_name.startswith(normalized_folder + "_"):
        suffix = task_name[len(normalized_folder) + 1 :]
        return f"{title_short} ({humanize_token(suffix)})"
    return humanize_token(task_name)


def humanize_token(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def first_sentence(text: str) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    for separator in [". ", "? ", "! "]:
        if separator in text:
            return text.split(separator, 1)[0].strip() + separator.strip()
    return text[:220].strip()


def generated_primary_metrics(group: str) -> list[str]:
    if group in ACCURACY_LIKE_GROUPS:
        return ["accuracy", "stderr"]
    return []


def generated_sample_selection(group: str) -> dict[str, Any]:
    if group in ACCURACY_LIKE_GROUPS:
        return {
            "failure_score_values": [0, 0.0, "I", "incorrect"],
            "max_samples": 3,
        }
    if group == "Safeguards":
        return {
            "failure_score_values": ["C", "P", 1, 1.0, "unsafe", "harmful"],
            "max_samples": 3,
        }
    return {
        "failure_score_values": [0, 0.0, "I", "incorrect", "fail", "false"],
        "max_samples": 3,
    }
