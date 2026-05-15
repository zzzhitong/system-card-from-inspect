from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import normalize_task_args, task_args_match, task_args_to_json


def discover_logs(input_target: str | Path | list[str] | list[Path] | None) -> list[Path]:
    if input_target is None:
        raise FileNotFoundError("No input path or directory was provided.")

    if isinstance(input_target, (str, Path)):
        candidates = [Path(input_target)]
    else:
        candidates = [Path(item) for item in input_target]

    discovered: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            raise FileNotFoundError(f"Input path not found: {candidate}")
        if candidate.is_file():
            if candidate.suffix.lower() not in {".json", ".eval"}:
                raise ValueError(f"Unsupported log file format: {candidate}")
            discovered.append(candidate)
            continue
        discovered.extend(path for path in candidate.rglob("*") if path.suffix.lower() in {".json", ".eval"})

    unique_paths = sorted({path.resolve() for path in discovered})
    return [Path(path) for path in unique_paths]


def _load_eval_with_inspect(path: Path) -> dict[str, Any]:
    from inspect_ai.log import read_eval_log  # type: ignore
    from inspect_ai.log._file import eval_log_json_str  # type: ignore

    log = read_eval_log(str(path))
    return json.loads(eval_log_json_str(log))


def load_log_document(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if file_path.suffix.lower() == ".json":
        with file_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    if file_path.suffix.lower() == ".eval":
        try:
            return _load_eval_with_inspect(file_path)
        except Exception as exc:  # pragma: no cover - fallback path
            raise RuntimeError(
                f"Unable to read .eval log {file_path}. Export it to JSON first or run inside an environment with inspect_ai installed."
            ) from exc
    raise ValueError(f"Unsupported log format: {file_path}")


def parse_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def build_run_record(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    eval_meta = document.get("eval", {})
    task_args = normalize_task_args(eval_meta.get("task_args") or eval_meta.get("task_args_passed") or {})
    model_name = eval_meta.get("model") or "unknown"
    created_at = parse_timestamp(eval_meta.get("created"))
    status = document.get("status", "unknown")
    run_record = {
        "input_path": str(path),
        "source_type": path.suffix.lower().lstrip("."),
        "status": status,
        "eval_id": eval_meta.get("eval_id"),
        "run_id": eval_meta.get("run_id"),
        "task_name": eval_meta.get("task"),
        "task_id": eval_meta.get("task_id"),
        "task_file": eval_meta.get("task_file"),
        "task_args": task_args,
        "task_args_json": task_args_to_json(task_args),
        "model_name": model_name,
        "created_at": created_at,
        "total_samples": document.get("results", {}).get("total_samples"),
        "completed_samples": document.get("results", {}).get("completed_samples"),
    }
    run_record["run_key"] = f"{run_record['task_name']}::{run_record['task_args_json']}"
    return run_record


def select_runs(
    run_records: list[dict[str, Any]],
    manifest: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    by_key: dict[str, list[dict[str, Any]]] = {}
    for run in run_records:
        by_key.setdefault(run["run_key"], []).append(run)

    for records in by_key.values():
        records.sort(key=lambda item: (item.get("status") == "success", item.get("created_at") or "", item["input_path"]), reverse=True)

    selectors = (manifest or {}).get("include_runs") or []
    if selectors:
        selected: list[dict[str, Any]] = []
        for selector in selectors:
            selector_task = selector.get("task")
            selector_args = selector.get("task_args")
            matched = [run for run in run_records if run.get("task_name") == selector_task]
            if selector_args:
                matched = [run for run in matched if task_args_match(run.get("task_args"), selector_args)]
                matched.sort(
                    key=lambda item: (item.get("status") == "success", item.get("created_at") or "", item["input_path"]),
                    reverse=True,
                )
                if not matched:
                    warnings.append(f"No log matched manifest selector: task={selector_task} args={selector_args}")
                    continue
                selected.append(matched[0])
                continue

            if not matched:
                warnings.append(f"No log matched manifest selector: task={selector_task} args={selector.get('task_args', {})}")
                continue

            by_run_key: dict[str, list[dict[str, Any]]] = {}
            for run in matched:
                by_run_key.setdefault(run["run_key"], []).append(run)
            for records in by_run_key.values():
                records.sort(
                    key=lambda item: (item.get("status") == "success", item.get("created_at") or "", item["input_path"]),
                    reverse=True,
                )
                selected.append(records[0])
        return dedupe_selected_runs(selected), warnings

    selected = [records[0] for records in by_key.values()]
    for run in selected:
        if run.get("status") != "success":
            warnings.append(f"Selected non-success run because no newer successful run exists: {run['input_path']}")
    return dedupe_selected_runs(selected), warnings


def dedupe_selected_runs(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for run in selected:
        path = run["input_path"]
        if path in seen:
            continue
        seen.add(path)
        deduped.append(run)
    return deduped


def extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [extract_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if "completion" in value and isinstance(value["completion"], str):
            return value["completion"].strip()
        if "text" in value and isinstance(value["text"], str):
            return value["text"].strip()
        if "content" in value and isinstance(value["content"], str):
            return value["content"].strip()
        if "message" in value and isinstance(value["message"], str):
            return value["message"].strip()
        if "message" in value and isinstance(value["message"], dict):
            return extract_text(value["message"])
        if "choices" in value and isinstance(value["choices"], list) and value["choices"]:
            return extract_text(value["choices"][0])
        if "content" in value and isinstance(value["content"], list):
            parts = []
            for item in value["content"]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"].strip())
            if parts:
                return "\n".join(part for part in parts if part)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def flatten_sample_rows(
    document: dict[str, Any],
    run_record: dict[str, Any],
    benchmark_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sample in document.get("samples", []) or []:
        scores = sample.get("scores") or {}
        primary_score_name = next(iter(scores.keys()), "")
        primary_score = scores.get(primary_score_name, {}) if primary_score_name else {}
        row = {
            "input_path": run_record["input_path"],
            "status": run_record["status"],
            "created_at": run_record["created_at"],
            "task_name": run_record["task_name"],
            "task_args_json": run_record["task_args_json"],
            "benchmark_id": benchmark_id,
            "model_name": run_record["model_name"],
            "sample_id": sample.get("id") or sample.get("uuid"),
            "input_text": extract_text(sample.get("input")),
            "target_text": extract_text(sample.get("target")),
            "output_text": extract_text(sample.get("output")),
            "primary_score_name": primary_score_name,
            "primary_score_value": primary_score.get("value"),
            "primary_score_answer": primary_score.get("answer"),
            "primary_score_explanation": primary_score.get("explanation"),
            "scores_json": json.dumps(scores, ensure_ascii=False, sort_keys=True),
            "metadata_json": json.dumps(sample.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
            "events_json": json.dumps(sample.get("events") or [], ensure_ascii=False),
        }
        rows.append(row)
    return rows


def extract_metrics(document: dict[str, Any]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for score_entry in document.get("results", {}).get("scores", []) or []:
        scorer_name = score_entry.get("name") or score_entry.get("scorer") or "unknown"
        for metric_name, metric in (score_entry.get("metrics") or {}).items():
            metrics.append(
                {
                    "score_name": scorer_name,
                    "metric_name": metric_name,
                    "value": metric.get("value"),
                    "reducer": metric.get("reducer"),
                }
            )
    return metrics
