from __future__ import annotations

import json
import math
import re
import statistics
import hashlib
import tempfile
import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from jinja2 import Environment, FileSystemLoader

from .config import (
    load_manual_overrides,
    load_yaml,
    normalize_task_args,
    slugify,
    task_args_match,
    task_args_to_json,
)
from .loaders import (
    build_run_record,
    discover_logs,
    extract_metrics,
    flatten_sample_rows,
    load_log_document,
    select_runs,
)
from .descriptions import (
    extract_readme_summary,
    extract_task_docstring,
    load_eval_yaml,
    resolve_task_sources,
    source_quality_label,
)
from .llm_analysis import (
    call_benchmark_analysis_llm,
    call_dimension_analysis_llm,
    call_system_card_analysis_llm,
    call_translation_llm,
    llm_analysis_available,
    resolve_llm_analysis_config,
)


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_json(path: str | Path, data: Any) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_yaml(path: str | Path, data: Any) -> None:
    Path(path).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_parquet(path: str | Path, rows: list[dict[str, Any]]) -> None:
    path = Path(path)
    if not rows:
        table = pa.table({"empty": []})
    else:
        columns: dict[str, list[Any]] = {}
        keys = sorted({key for row in rows for key in row.keys()})
        for key in keys:
            columns[key] = [serialize_parquet_value(row.get(key)) for row in rows]
        table = pa.table(columns)
    pq.write_table(table, path)


def serialize_parquet_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def load_jinja_env(skill_root: Path) -> Environment:
    env = Environment(loader=FileSystemLoader(str(skill_root / "assets")), autoescape=False, trim_blocks=True, lstrip_blocks=True)
    return env


def format_metric_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def build_report_defaults(report_manifest: dict[str, Any], selected_runs: list[dict[str, Any]]) -> dict[str, Any]:
    report = dict((report_manifest or {}).get("report", {}) if "report" in (report_manifest or {}) else (report_manifest or {}))
    model_names = [run.get("model_name") for run in selected_runs if run.get("model_name")]
    unique_models = sorted(set(model_names))
    default_model = unique_models[0] if len(unique_models) == 1 else None
    if not report.get("model_name") and default_model:
        report["model_name"] = default_model
    if not report.get("model_id") and default_model:
        report["model_id"] = default_model
    if not report.get("title"):
        if report.get("model_name"):
            report["title"] = f"{report['model_name']} System Card"
        elif selected_runs:
            benchmark_name = selected_runs[0].get("benchmark_display_name") or selected_runs[0].get("task_name") or "System Card"
            report["title"] = f"{benchmark_name} System Card"
        else:
            report["title"] = "System Card"
    if "organization" not in report:
        report["organization"] = None
    if "language" not in report:
        report["language"] = "zh"
    return report


def merge_report_metadata(explicit_report: dict[str, Any], fallback_report: dict[str, Any]) -> dict[str, Any]:
    merged = dict(fallback_report or {})
    for key, value in (explicit_report or {}).items():
        if value is not None and value != "":
            merged[key] = value
    return merged


def infer_benchmark_id(
    task_name: str | None,
    task_args: dict[str, Any] | None,
    benchmark_registry: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    task_name = task_name or "unknown"
    task_args = normalize_task_args(task_args or {})
    benchmarks = benchmark_registry.get("benchmarks", {})
    for benchmark_id, entry in benchmarks.items():
        if entry.get("task_name") != task_name:
            continue
        if task_args_match(task_args, entry.get("match") or {}):
            return benchmark_id, entry
    filtered_match = filter_match_task_args(task_args)
    if not filtered_match:
        return slugify(task_name), None
    suffix = "-".join(f"{slugify(str(k))}-{slugify(str(v))}" for k, v in sorted(filtered_match.items()))
    candidate = f"{slugify(task_name)}_{suffix}".strip("_")
    if len(candidate) <= 80:
        return candidate, None
    digest = hashlib.sha1(task_args_to_json(filtered_match).encode("utf-8")).hexdigest()[:10]
    return f"{slugify(task_name)}-{digest}", None


def filter_match_task_args(task_args: dict[str, Any] | None) -> dict[str, Any]:
    if not task_args:
        return {}
    excluded = {
        "scorer_model",
        "grader_model",
        "judge_model",
        "judge_llm",
        "binary_judge_model",
        "numeric_judge_model",
        "shuffle",
        "seed",
        "epochs",
        "prod",
        "test_eval_awareness",
        "extra_system_instructions",
        "attack_kwargs",
        "agent_kwargs",
        "user_task_ids",
        "injection_task_ids",
        "prompt_template",
        "limit_samples_per_lang",
        "max_non_cot_tokens",
        "numeric_tol",
        "no_belief_handling",
        "judge_max_tokens",
        "judge_temperature",
        "judge_reasoning_effort",
        "include_core_metrics",
        "include_statistical_summary",
        "include_stratification",
        "include_normalisation",
        "dataset",
        "limit",
    }
    filtered: dict[str, Any] = {}
    for key, value in normalize_task_args(task_args).items():
        if key in excluded:
            continue
        if value is None or value == [] or value == {}:
            continue
        filtered[key] = value
    return filtered


def prettify_label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def suggest_display_name(task_name: str, match_args: dict[str, Any]) -> str:
    base = prettify_label(task_name)
    if not match_args:
        return base
    parts: list[str] = []
    for key, value in match_args.items():
        if isinstance(value, list):
            value_text = ",".join(str(item) for item in value)
        else:
            value_text = str(value)
        parts.append(f"{prettify_label(key)}={value_text}")
    return f"{base} ({'; '.join(parts)})"


def existing_dimension_for_task(task_name: str, benchmark_registry: dict[str, Any]) -> str | None:
    dimensions = {
        entry.get("dimension")
        for entry in benchmark_registry.get("benchmarks", {}).values()
        if entry.get("task_name") == task_name and entry.get("dimension")
    }
    if len(dimensions) == 1:
        return next(iter(dimensions))
    return None


def suggest_dimension(task_name: str, metric_names: list[str], benchmark_registry: dict[str, Any]) -> tuple[str | None, str]:
    reused = existing_dimension_for_task(task_name, benchmark_registry)
    if reused:
        return reused, f"Reused dimension `{reused}` because existing registry entries for task `{task_name}` all map there."

    task_lower = task_name.lower()
    keyword_rules = [
        ("safeguards", ["reject", "jailbreak", "harm", "xstest", "refusal"]),
        ("honesty", ["truth", "simpleqa", "fact", "mask", "honesty"]),
        ("agentic_safety", ["agent", "tool", "dojo", "misalignment", "injection", "b3"]),
        ("memory_and_sycophancy", ["sycoph", "persistbench", "memory"]),
        ("contextual_awareness", ["sad", "oversight", "influence"]),
        ("fairness", ["bbq", "bias", "fairness"]),
        ("capabilities", ["mmlu", "mgsm", "healthbench", "capabil"]),
    ]
    for dimension_id, patterns in keyword_rules:
        if any(pattern in task_lower for pattern in patterns):
            return dimension_id, f"Matched task name `{task_name}` to dimension `{dimension_id}` using keyword heuristics."

    metric_set = set(metric_names)
    if {"refusal_rate", "jailbreak_rate"} & metric_set:
        return "safeguards", "Used safeguard-related metric names to infer the dimension."
    if {"overall_honesty", "overall_accuracy", "correct", "incorrect"} & metric_set:
        return "honesty", "Used honesty/factuality metric names to infer the dimension."
    if {"failure_rate"} & metric_set and "persist" in task_lower:
        return "memory_and_sycophancy", "Used PersistBench-like failure metrics to infer the dimension."
    if "accuracy" in metric_set:
        return "capabilities", "Fell back to capabilities because only generic accuracy-like metrics were available."
    return None, "Could not infer a dimension with the current heuristics."


def suggest_primary_metrics(task_name: str, metric_names: list[str], benchmark_registry: dict[str, Any]) -> tuple[list[str], str]:
    for entry in benchmark_registry.get("benchmarks", {}).values():
        if entry.get("task_name") == task_name and entry.get("primary_metrics"):
            return list(entry["primary_metrics"]), f"Reused primary metrics from an existing registry entry for task `{task_name}`."

    metric_set = set(metric_names)
    if {"correct", "incorrect", "not_attempted"} <= metric_set:
        ordered = ["correct", "incorrect", "not_attempted"]
        for extra in ["correct_given_attempted", "f_score"]:
            if extra in metric_set:
                ordered.append(extra)
        return ordered, "Selected QA-style correctness metrics."
    if {"jailbreak_rate", "strong_reject_metric"} <= metric_set:
        return ["jailbreak_rate", "strong_reject_metric"], "Selected StrongREJECT-style primary metrics."
    if {"accuracy", "stderr"} <= metric_set:
        return ["accuracy", "stderr"], "Selected accuracy and stderr because they are the standard aggregate metrics available."
    if "failure_rate" in metric_set:
        return ["failure_rate"], "Selected failure_rate because it is the most informative available metric."
    if metric_names:
        return metric_names[:3], "Fell back to the first available metric names."
    return [], "No aggregate metrics were available."


def suggest_sample_selection(
    task_name: str,
    match_args: dict[str, Any],
    metric_names: list[str],
    benchmark_registry: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    for entry in benchmark_registry.get("benchmarks", {}).values():
        if entry.get("task_name") == task_name and entry.get("sample_selection"):
            return dict(entry["sample_selection"]), f"Reused sample-selection settings from an existing registry entry for task `{task_name}`."

    if task_name == "xstest":
        subset = str(match_args.get("subset", "")).lower()
        if subset == "safe":
            return {
                "failure_score_values": ["I", "P"],
                "success_score_values": ["C"],
                "max_samples": 3,
            }, "Used the built-in XSTest safe-subset sample-selection heuristic."
        if subset == "unsafe":
            return {
                "failure_score_values": ["C", "P"],
                "success_score_values": ["I"],
                "max_samples": 3,
            }, "Used the built-in XSTest unsafe-subset sample-selection heuristic."

    metric_set = set(metric_names)
    if {"correct", "incorrect", "not_attempted"} & metric_set or "accuracy" in metric_set:
        return {
            "failure_score_values": [0, 0.0, "I", "incorrect"],
            "max_samples": 3,
        }, "Used a generic accuracy/factuality sample-selection heuristic."
    return {"max_samples": 3}, "Fell back to a minimal sample-selection rule."


def normalize_logs(
    input_target: str | Path | list[str] | list[Path],
    benchmark_registry_path: str | Path,
    report_manifest_path: str | Path | None,
    artifacts_dir: str | Path,
) -> dict[str, Any]:
    benchmark_registry = load_yaml(benchmark_registry_path)
    report_manifest = load_yaml(report_manifest_path) if report_manifest_path else {}

    discovered = discover_logs(input_target)
    run_records: list[dict[str, Any]] = []
    build_warnings: list[str] = []

    for path in discovered:
        try:
            document = load_log_document(path)
        except Exception as exc:
            build_warnings.append(f"Failed to load log {path}: {exc}")
            continue
        run_records.append(build_run_record(path, document))

    selected_runs, selection_warnings = select_runs(run_records, report_manifest)
    build_warnings.extend(selection_warnings)

    selected_runs_with_benchmarks: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for run in selected_runs:
        document = load_log_document(run["input_path"])
        metrics = extract_metrics(document)
        benchmark_id, registry_entry = infer_benchmark_id(run["task_name"], run["task_args"], benchmark_registry)
        run_copy = dict(run)
        run_copy["benchmark_id"] = benchmark_id
        run_copy["benchmark_display_name"] = (registry_entry or {}).get("display_name", benchmark_id)
        metric_names = [metric.get("metric_name") for metric in metrics if metric.get("metric_name")]
        suggested_dimension, dimension_reason = suggest_dimension(run["task_name"], metric_names, benchmark_registry)
        run_copy["dimension_id"] = (registry_entry or {}).get("dimension") or suggested_dimension
        run_copy["dimension_reason"] = dimension_reason
        run_copy["dimension_source"] = "registry" if (registry_entry or {}).get("dimension") else ("suggested" if suggested_dimension else "unmapped")
        run_copy["summary_focus"] = (registry_entry or {}).get("summary_focus")
        run_copy["warnings"] = []
        if registry_entry is None:
            run_copy["warnings"].append(f"Benchmark mapping missing for task={run['task_name']} args={run['task_args_json']}")
        if run_copy["dimension_source"] == "suggested":
            run_copy["warnings"].append(
                f"Dimension `{run_copy['dimension_id']}` was assigned heuristically for benchmark `{benchmark_id}`; review the registry suggestion before publication."
            )
        total_samples = run.get("total_samples")
        completed_samples = run.get("completed_samples")
        if isinstance(total_samples, int) and isinstance(completed_samples, int) and completed_samples < total_samples:
            coverage_warning = (
                f"Run {run['input_path']} completed only {completed_samples}/{total_samples} samples; treat the reported results as partial coverage."
            )
            run_copy["warnings"].append(coverage_warning)
            build_warnings.append(coverage_warning)
        selected_runs_with_benchmarks.append(run_copy)
        rows.extend(flatten_sample_rows(document, run_copy, benchmark_id))

    artifacts_root = ensure_dir(artifacts_dir)
    run_index_path = artifacts_root / "run_index.json"
    results_path = artifacts_root / "results.parquet"
    write_json(
        run_index_path,
        {
            "report_defaults": build_report_defaults(report_manifest, selected_runs_with_benchmarks),
            "selected_runs": selected_runs_with_benchmarks,
            "warnings": build_warnings,
        },
    )
    write_parquet(results_path, rows)

    return {
        "run_index_path": str(run_index_path),
        "results_path": str(results_path),
        "warnings": build_warnings,
        "selected_runs": selected_runs_with_benchmarks,
        "rows": rows,
        "benchmark_registry": benchmark_registry,
        "report_manifest": report_manifest,
    }


def aggregate_summary(
    run_index_path: str | Path,
    benchmark_registry_path: str | Path,
    report_manifest_path: str | Path | None,
    out_path: str | Path,
) -> dict[str, Any]:
    run_index = load_yaml_json(run_index_path)
    benchmark_registry = load_yaml(benchmark_registry_path)
    report_manifest = load_yaml(report_manifest_path) if report_manifest_path else {}

    benchmark_results: list[dict[str, Any]] = []
    warnings = list(run_index.get("warnings") or [])
    for run in run_index.get("selected_runs", []):
        document = load_log_document(run["input_path"])
        metrics = extract_metrics(document)
        registry_entry = benchmark_registry.get("benchmarks", {}).get(run["benchmark_id"], {})
        primary_metric_names = registry_entry.get("primary_metrics") or [metric["metric_name"] for metric in metrics[:3]]
        primary_metrics = [metric for metric in metrics if metric["metric_name"] in primary_metric_names]
        if not primary_metrics:
            primary_metrics = metrics[:3]
            warnings.append(f"No primary metrics configured for {run['benchmark_id']}; using the first available metrics instead.")
        benchmark_results.append(
            {
                "benchmark_id": run["benchmark_id"],
                "display_name": run.get("benchmark_display_name", run["benchmark_id"]),
                "task_name": run["task_name"],
                "task_args": run.get("task_args") or {},
                "task_args_json": task_args_to_json(run.get("task_args") or {}),
                "input_path": run["input_path"],
                "status": run["status"],
                "dimension_id": run.get("dimension_id"),
                "dimension_source": run.get("dimension_source"),
                "dimension_reason": run.get("dimension_reason"),
                "summary_focus": run.get("summary_focus"),
                "total_samples": document.get("results", {}).get("total_samples"),
                "completed_samples": document.get("results", {}).get("completed_samples"),
                "coverage": build_coverage_info(
                    document.get("results", {}).get("total_samples"),
                    document.get("results", {}).get("completed_samples"),
                ),
                "primary_metrics": [materialize_metric(metric) for metric in primary_metrics],
                "all_metrics": [materialize_metric(metric) for metric in metrics],
                "warnings": list(run.get("warnings") or []),
            }
        )
        if benchmark_results[-1]["coverage"]["is_partial"]:
            warnings.append(
                f"Benchmark `{run['benchmark_id']}` is based on a partial run ({benchmark_results[-1]['coverage']['completed_samples']}/{benchmark_results[-1]['coverage']['total_samples']} samples completed)."
            )

    summary = {
        "report": merge_report_metadata(report_manifest.get("report", {}), run_index.get("report_defaults", {})),
        "selected_runs": run_index.get("selected_runs", []),
        "benchmark_results": benchmark_results,
        "warnings": warnings,
    }
    write_json(out_path, summary)
    return summary


def materialize_metric(metric: dict[str, Any]) -> dict[str, Any]:
    metric_copy = dict(metric)
    metric_copy["name"] = metric.get("metric_name", metric.get("name"))
    metric_copy["formatted_value"] = format_metric_value(metric.get("value"))
    return metric_copy


def build_coverage_info(total_samples: Any, completed_samples: Any) -> dict[str, Any]:
    total = int(total_samples) if isinstance(total_samples, (int, float)) else None
    completed = int(completed_samples) if isinstance(completed_samples, (int, float)) else None
    coverage_rate = None
    if total and completed is not None and total > 0:
        coverage_rate = completed / total
    is_partial = bool(total and completed is not None and completed < total)
    return {
        "total_samples": total,
        "completed_samples": completed,
        "coverage_rate": coverage_rate,
        "coverage_rate_formatted": format_metric_value(coverage_rate) if coverage_rate is not None else "n/a",
        "is_partial": is_partial,
    }


def load_benchmark_hints(skill_root: Path) -> dict[str, Any]:
    hints_path = skill_root / "references" / "benchmark_hints.yaml"
    if not hints_path.exists():
        return {}
    return load_yaml(hints_path).get("benchmark_hints", {})


def build_benchmark_descriptions(
    skill_root: Path,
    summary_path: str | Path,
    benchmark_registry_path: str | Path,
    out_dir: str | Path,
    index_path: str | Path,
    benchmark_ids: set[str] | None = None,
) -> dict[str, Any]:
    summary = load_yaml_json(summary_path)
    benchmark_registry = load_yaml(benchmark_registry_path).get("benchmarks", {})
    benchmark_hints = load_benchmark_hints(skill_root)
    repo_root = skill_root.parents[2]
    out_root = ensure_dir(out_dir)
    index: dict[str, Any] = {"benchmark_descriptions": []}

    for run in summary.get("selected_runs", []):
        benchmark_id = run["benchmark_id"]
        if benchmark_ids is not None and benchmark_id not in benchmark_ids:
            continue
        registry_entry = benchmark_registry.get(benchmark_id, {})
        hints = benchmark_hints.get(benchmark_id, {})
        sources = resolve_task_sources(repo_root, run.get("task_file"))
        eval_meta = load_eval_yaml(sources.eval_yaml)
        readme_summary = extract_readme_summary(sources.readme)
        task_docstring = extract_task_docstring(sources.task_file)
        local_sources: list[str] = []
        warnings: list[str] = []
        if hints:
            local_sources.append("benchmark_hints")
        if sources.eval_yaml is not None:
            local_sources.append("eval_yaml")
        if sources.readme is not None:
            local_sources.append("readme")
        if sources.task_file is not None and task_docstring:
            local_sources.append("task_docstring")

        resolved_description = (
            hints.get("description")
            or eval_meta.get("description")
            or readme_summary
            or task_docstring
            or registry_entry.get("summary_focus")
            or f"Local description unavailable for benchmark `{benchmark_id}`."
        )
        if not (hints.get("description") or eval_meta.get("description") or readme_summary or task_docstring):
            warnings.append(
                f"Local benchmark description for `{benchmark_id}` is weak; a web-search enrichment stage is still needed if higher-quality public description is required."
            )

        evaluation_questions = (
            hints.get("evaluation_questions")
            or derive_evaluation_questions(registry_entry, eval_meta, resolved_description)
        )
        assessed_scope = (
            hints.get("assessed_risk_or_capability_scope")
            or eval_meta.get("group")
            or registry_entry.get("dimension")
            or registry_entry.get("summary_focus")
            or "unknown"
        )
        payload = {
            "benchmark_id": benchmark_id,
            "task_name": run.get("task_name"),
            "display_name": run.get("benchmark_display_name", benchmark_id),
            "local_sources": local_sources,
            "resolved_description": resolved_description,
            "evaluation_questions": evaluation_questions,
            "assessed_risk_or_capability_scope": assessed_scope,
            "source_quality": source_quality_label(local_sources, warnings),
            "used_web_search": False,
            "warnings": warnings,
            "source_paths": {
                "task_file": str(sources.task_file) if sources.task_file else None,
                "readme": str(sources.readme) if sources.readme else None,
                "eval_yaml": str(sources.eval_yaml) if sources.eval_yaml else None,
            },
            "metadata": {
                "eval_yaml_title": eval_meta.get("title"),
                "eval_yaml_description": eval_meta.get("description"),
                "readme_summary": readme_summary,
                "task_docstring": task_docstring,
                "deployment_focus": hints.get("deployment_focus") or [],
            },
        }
        json_path = out_root / f"{benchmark_id}.json"
        write_json(json_path, payload)
        index["benchmark_descriptions"].append(
            {
                "benchmark_id": benchmark_id,
                "json_path": str(json_path),
                "source_quality": payload["source_quality"],
            }
        )

    write_json(index_path, index)
    return index


def derive_evaluation_questions(
    registry_entry: dict[str, Any],
    eval_meta: dict[str, Any],
    resolved_description: str,
) -> list[str]:
    questions = []
    dimension = registry_entry.get("dimension")
    if dimension == "agentic_safety":
        questions.extend(
            [
                "Where does the benchmark show agent/tool security failures most clearly?",
                "Which attack families or application slices produce the highest-risk outcomes?",
            ]
        )
    elif dimension == "honesty":
        questions.extend(
            [
                "How accurate or truthful is the model on this benchmark?",
                "Do the most concerning failures involve incorrect answers, overconfidence, or refusal behavior?",
            ]
        )
    elif dimension == "safeguards":
        questions.extend(
            [
                "How well does the model refuse harmful or disallowed requests?",
                "Where are the calibration or jailbreak weaknesses concentrated?",
            ]
        )
    if not questions:
        summary_focus = registry_entry.get("summary_focus") or eval_meta.get("description") or resolved_description
        questions.append(f"What is the benchmark primarily measuring, and what does the result say about {summary_focus}?")
    return questions


def select_representative_samples(
    run_index_path: str | Path,
    benchmark_registry_path: str | Path,
    out_path: str | Path,
) -> dict[str, Any]:
    run_index = load_yaml_json(run_index_path)
    benchmark_registry = load_yaml(benchmark_registry_path)
    selected_samples: list[dict[str, Any]] = []
    warnings: list[str] = []

    for run in run_index.get("selected_runs", []):
        benchmark_id = run["benchmark_id"]
        registry_entry = benchmark_registry.get("benchmarks", {}).get(benchmark_id, {})
        selection_cfg = registry_entry.get("sample_selection") or {}
        failure_values = {canonicalize_score_value(value) for value in selection_cfg.get("failure_score_values", [])}
        success_values = {canonicalize_score_value(value) for value in selection_cfg.get("success_score_values", [])}
        failure_score_min = parse_numeric_score(selection_cfg.get("failure_score_min"))
        success_score_max = parse_numeric_score(selection_cfg.get("success_score_max"))
        max_samples = int(selection_cfg.get("max_samples", 3))

        document = load_log_document(run["input_path"])
        rows = dedupe_sample_rows(flatten_sample_rows(document, run, benchmark_id))
        failures = [row for row in rows if canonicalize_score_value(row.get("primary_score_value")) in failure_values]
        if failure_score_min is not None:
            failures.extend(row for row in rows if score_meets_min_threshold(row, failure_score_min))
            failures = dedupe_sample_rows(failures)
        failures.sort(key=sample_sort_key, reverse=True)
        picked = failures[:max_samples]

        if not picked and success_values:
            successes = [row for row in rows if canonicalize_score_value(row.get("primary_score_value")) in success_values]
            successes.sort(key=sample_sort_key, reverse=True)
            picked = successes[: min(2, max_samples)]

        if not picked and success_score_max is not None:
            successes = [row for row in rows if score_meets_max_threshold(row, success_score_max)]
            successes.sort(key=sample_sort_key)
            picked = successes[: min(2, max_samples)]

        if not picked and rows:
            rows.sort(key=sample_sort_key, reverse=True)
            picked = rows[: min(2, max_samples)]
            warnings.append(f"No configured failure samples were found for {benchmark_id}; selected fallback examples instead.")

        for row in picked:
            row_copy = dict(row)
            row_copy["selection_reason"] = build_selection_reason(row_copy, failure_values)
            selected_samples.append(row_copy)

    payload = {"samples": selected_samples, "warnings": warnings}
    write_json(out_path, payload)
    return payload


def canonicalize_score_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).strip().lower()


def parse_numeric_score(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def numeric_score_value(sample: dict[str, Any]) -> float | None:
    return parse_numeric_score(sample.get("primary_score_value"))


def score_meets_min_threshold(sample: dict[str, Any], threshold: float) -> bool:
    value = numeric_score_value(sample)
    return value is not None and value >= threshold


def score_meets_max_threshold(sample: dict[str, Any], threshold: float) -> bool:
    value = numeric_score_value(sample)
    return value is not None and value <= threshold


def dedupe_sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_sample_id: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=sample_sort_key, reverse=True):
        sample_id = str(row.get("sample_id") or "")
        if not sample_id:
            sample_id = json.dumps(
                {
                    "input_text": row.get("input_text"),
                    "output_text": row.get("output_text"),
                    "primary_score_value": row.get("primary_score_value"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        best_by_sample_id.setdefault(sample_id, row)
    return list(best_by_sample_id.values())


def sample_sort_key(sample: dict[str, Any]) -> tuple[float, int, int, int]:
    numeric_score = numeric_score_value(sample)
    explanation_len = len(sample.get("primary_score_explanation") or "")
    output_len = len(sample.get("output_text") or "")
    input_len = len(sample.get("input_text") or "")
    return (
        -1.0 if numeric_score is None else numeric_score,
        1 if explanation_len else 0,
        min(output_len, 4000),
        min(input_len, 4000),
    )


def build_selection_reason(sample: dict[str, Any], failure_values: set[str]) -> str:
    score_value = canonicalize_score_value(sample.get("primary_score_value"))
    if score_value in failure_values:
        return f"Representative failure with score value `{sample.get('primary_score_value')}`."
    numeric_score = numeric_score_value(sample)
    if numeric_score is not None:
        return f"Representative example chosen for a high continuous score (`{sample.get('primary_score_value')}`)."
    return f"Representative example with score value `{sample.get('primary_score_value')}`."


def parse_json_mapping(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
SESSION_RE = re.compile(r"\bsess_[A-Za-z0-9]+\b")
THINK_RE = re.compile(r"(?is)<think>.*?</think>")


def redact_text(text: str) -> str:
    redacted = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    redacted = SESSION_RE.sub("[REDACTED_SESSION]", redacted)
    return redacted


def sanitize_text_for_report(text: str, *, strip_reasoning: bool = False, max_chars: int = 1200) -> str:
    if not text:
        return ""
    sanitized = redact_text(text)
    if strip_reasoning:
        sanitized = THINK_RE.sub("[internal reasoning omitted]", sanitized)
    sanitized = sanitized.strip()
    if len(sanitized) <= max_chars:
        return sanitized
    return sanitized[: max_chars - 15].rstrip() + "\n...[truncated]"


def score_quantile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    sorted_values = sorted(values)
    position = max(0, min(len(sorted_values) - 1, math.ceil(quantile * len(sorted_values)) - 1))
    return sorted_values[position]


def summarize_score_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [value for row in rows if (value := numeric_score_value(row)) is not None]
    if not values:
        return {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p90": None,
            "high_risk_threshold": 0.7,
            "high_risk_count": None,
            "high_risk_rate": None,
            "zero_risk_count": None,
            "zero_risk_rate": None,
        }
    high_risk_threshold = 0.7
    high_risk_count = sum(1 for value in values if value >= high_risk_threshold)
    zero_risk_count = sum(1 for value in values if value <= 0.01)
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "p90": score_quantile(values, 0.9),
        "high_risk_threshold": high_risk_threshold,
        "high_risk_count": high_risk_count,
        "high_risk_rate": high_risk_count / len(values),
        "zero_risk_count": zero_risk_count,
        "zero_risk_rate": zero_risk_count / len(values),
    }


def risk_level_from_mean(mean_value: float | None) -> str:
    if mean_value is None:
        return "unknown"
    if mean_value >= 0.5:
        return "high"
    if mean_value >= 0.2:
        return "elevated"
    if mean_value >= 0.05:
        return "non-trivial"
    return "low"


def analyze_slices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidate_fields = ["app_name", "task_type", "scoring_type", "app_slug", "level"]
    analyses: list[dict[str, Any]] = []
    for field in candidate_fields:
        grouped: dict[str, list[float]] = {}
        for row in rows:
            score = numeric_score_value(row)
            if score is None:
                continue
            metadata = parse_json_mapping(row.get("metadata_json"))
            value = metadata.get(field)
            if value is None:
                continue
            value_key = str(value)
            grouped.setdefault(value_key, []).append(score)
        if len(grouped) < 2:
            continue
        stats = []
        for value_key, scores in grouped.items():
            stats.append(
                {
                    "value": value_key,
                    "mean": statistics.fmean(scores),
                    "count": len(scores),
                }
            )
        stats.sort(key=lambda item: item["mean"], reverse=True)
        analyses.append(
            {
                "field": field,
                "highest_risk": stats[:3],
                "lowest_risk": list(reversed(stats[-3:])),
            }
        )
    return analyses


def synthesize_failure_patterns(rows: list[dict[str, Any]], selected_samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pattern_rules = [
        ("conversation-history exfiltration", ["chat history", "full chat transcript", "conversation history", "transcript"]),
        ("tool-parameter injection", ["notes parameter", "parameter", "\"notes\":", "required"]),
        ("tool-description hijack", ["tool description", "\"behavior\":", "\"description\":", "always include"]),
        ("sensitive-context leakage", ["email", "session id", "timezone", "user information"]),
    ]
    counts: dict[str, dict[str, Any]] = {}
    pool = selected_samples or rows[:10]
    for sample in pool:
        metadata = parse_json_mapping(sample.get("metadata_json"))
        text = " ".join(
            [
                str(metadata.get("attack", "")),
                str(metadata.get("attack_objective", "")),
                str(sample.get("input_text", "")),
                str(sample.get("output_text", "")),
                str(sample.get("target_text", "")),
            ]
        ).lower()
        for label, keywords in pattern_rules:
            if any(keyword in text for keyword in keywords):
                entry = counts.setdefault(label, {"label": label, "count": 0, "sample_ids": []})
                entry["count"] += 1
                if sample.get("sample_id") not in entry["sample_ids"]:
                    entry["sample_ids"].append(sample.get("sample_id"))
    patterns = sorted(counts.values(), key=lambda item: item["count"], reverse=True)
    return patterns[:4]


def build_benchmark_key_findings(
    benchmark: dict[str, Any],
    coverage: dict[str, Any],
    score_distribution: dict[str, Any],
    slice_analysis: list[dict[str, Any]],
    failure_patterns: list[dict[str, Any]],
) -> list[str]:
    findings: list[str] = []
    overall_mean = benchmark_metric_value(benchmark, "mean")
    if overall_mean is None:
        overall_mean = score_distribution.get("mean")
    risk_level = risk_level_from_mean(overall_mean)
    if overall_mean is not None:
        findings.append(
            f"Overall benchmark risk is {risk_level}, with mean={format_metric_value(overall_mean)} and max sample score={format_metric_value(score_distribution['max'])}."
        )
    if coverage.get("is_partial"):
        findings.append(
            f"Coverage is partial: {coverage.get('completed_samples')}/{coverage.get('total_samples')} samples completed."
        )
    if score_distribution.get("high_risk_count") is not None:
        findings.append(
            f"{score_distribution['high_risk_count']} sample(s) scored at or above {score_distribution['high_risk_threshold']}, indicating a non-trivial high-risk tail."
        )
    if slice_analysis:
        worst_slice = slice_analysis[0]["highest_risk"][0]
        findings.append(
            f"The highest-risk {slice_analysis[0]['field']} slice is `{worst_slice['value']}` with mean={format_metric_value(worst_slice['mean'])} across {worst_slice['count']} sample(s)."
        )
    if failure_patterns:
        findings.append(
            f"Representative failure patterns are led by {failure_patterns[0]['label']} ({failure_patterns[0]['count']} selected sample(s))."
        )
    return findings


def build_benchmark_implications(
    benchmark: dict[str, Any],
    failure_patterns: list[dict[str, Any]],
    slice_analysis: list[dict[str, Any]],
) -> list[str]:
    implications: list[str] = []
    dimension_id = benchmark.get("dimension_id")
    if dimension_id == "agentic_safety":
        implications.append("Prioritize guardrails on tool arguments, untrusted tool descriptions, and context passed into tool calls.")
    if any(pattern["label"] == "conversation-history exfiltration" for pattern in failure_patterns):
        implications.append("Add explicit policies and filters that block chat-history or transcript material from being copied into tool parameters.")
    if any(pattern["label"] == "tool-description hijack" for pattern in failure_patterns):
        implications.append("Treat tool descriptions and MCP metadata as untrusted input; sanitize or canonicalize them before exposing them to the model.")
    if slice_analysis:
        implications.append(
            f"Investigate the worst-performing `{slice_analysis[0]['field']}` slices first, because they dominate the current risk profile."
        )
    return implications[:4]


def prepare_sample_for_report(sample: dict[str, Any]) -> dict[str, Any]:
    sample_copy = dict(sample)
    sample_copy["input_excerpt"] = sanitize_text_for_report(str(sample.get("input_text") or ""), max_chars=1400)
    sample_copy["output_excerpt"] = sanitize_text_for_report(
        str(sample.get("output_text") or ""),
        strip_reasoning=True,
        max_chars=1200,
    )
    sample_copy["primary_score_explanation_excerpt"] = sanitize_text_for_report(
        str(sample.get("primary_score_explanation") or ""),
        max_chars=900,
    )
    metadata = parse_json_mapping(sample.get("metadata_json"))
    sample_copy["metadata"] = metadata
    sample_copy["sample_highlights"] = [
        f"{key}={metadata[key]}"
        for key in ["app_name", "task_type", "scoring_type", "app_slug", "level"]
        if metadata.get(key) is not None
    ]
    sample_copy["contains_internal_reasoning"] = "<think>" in str(sample.get("output_text") or "").lower()
    return sample_copy


def enrich_benchmark_payload(
    benchmark: dict[str, Any],
    run_record: dict[str, Any] | None,
    selected_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    benchmark_copy = dict(benchmark)
    if run_record is None:
        benchmark_copy["selected_samples"] = [prepare_sample_for_report(sample) for sample in selected_samples]
        benchmark_copy["coverage"] = benchmark.get("coverage") or {}
        benchmark_copy["score_distribution"] = summarize_score_distribution([])
        benchmark_copy["slice_analysis"] = []
        benchmark_copy["failure_patterns"] = []
        benchmark_copy["key_findings"] = []
        benchmark_copy["implications"] = []
        return benchmark_copy

    document = load_log_document(run_record["input_path"])
    all_rows = dedupe_sample_rows(flatten_sample_rows(document, run_record, benchmark["benchmark_id"]))
    selected_sample_lookup = {sample["sample_id"]: sample for sample in selected_samples}
    prepared_samples = [prepare_sample_for_report(selected_sample_lookup.get(row["sample_id"], row)) for row in all_rows if row["sample_id"] in selected_sample_lookup]
    if not prepared_samples:
        prepared_samples = [prepare_sample_for_report(sample) for sample in selected_samples]

    coverage = benchmark.get("coverage") or build_coverage_info(run_record.get("total_samples"), run_record.get("completed_samples"))
    score_distribution = summarize_score_distribution(all_rows)
    slice_analysis = analyze_slices(all_rows)
    failure_patterns = synthesize_failure_patterns(all_rows, selected_samples)
    key_findings = build_benchmark_key_findings(benchmark, coverage, score_distribution, slice_analysis, failure_patterns)
    implications = build_benchmark_implications(benchmark, failure_patterns, slice_analysis)
    benchmark_copy["selected_samples"] = prepared_samples
    benchmark_copy["coverage"] = coverage
    benchmark_copy["score_distribution"] = materialize_score_distribution(score_distribution)
    benchmark_copy["slice_analysis"] = materialize_slice_analysis(slice_analysis)
    benchmark_copy["failure_patterns"] = failure_patterns
    benchmark_copy["key_findings"] = key_findings
    benchmark_copy["implications"] = implications
    benchmark_copy["coverage_and_caveats"] = build_coverage_caveats(coverage, benchmark_copy)
    return benchmark_copy


def materialize_score_distribution(score_distribution: dict[str, Any]) -> dict[str, Any]:
    output = dict(score_distribution)
    for key in ["mean", "min", "max", "p90", "high_risk_rate", "zero_risk_rate"]:
        output[f"{key}_formatted"] = format_metric_value(score_distribution.get(key))
    return output


def benchmark_metric_value(benchmark: dict[str, Any], metric_name: str) -> float | None:
    for metric in benchmark.get("primary_metrics", []):
        if metric.get("name") == metric_name and isinstance(metric.get("value"), (int, float)):
            return float(metric["value"])
    for metric in benchmark.get("all_metrics", []):
        if metric.get("name") == metric_name and isinstance(metric.get("value"), (int, float)):
            return float(metric["value"])
    return None


def materialize_slice_analysis(slice_analysis: list[dict[str, Any]]) -> list[dict[str, Any]]:
    materialized: list[dict[str, Any]] = []
    for slice_entry in slice_analysis:
        entry_copy = dict(slice_entry)
        for bucket in ["highest_risk", "lowest_risk"]:
            entry_copy[bucket] = [
                {**item, "mean_formatted": format_metric_value(item.get("mean"))}
                for item in slice_entry.get(bucket, [])
            ]
        materialized.append(entry_copy)
    return materialized


def build_coverage_caveats(coverage: dict[str, Any], benchmark: dict[str, Any]) -> list[str]:
    caveats: list[str] = []
    if coverage.get("is_partial"):
        caveats.append(
            f"This benchmark completed only {coverage.get('completed_samples')}/{coverage.get('total_samples')} samples, so the reported results should be treated as preliminary."
        )
    if benchmark.get("dimension_source") == "suggested":
        caveats.append("The benchmark-to-dimension mapping was inferred automatically and should be reviewed before external publication.")
    return caveats


def write_benchmark_facts(
    summary_path: str | Path,
    out_dir: str | Path,
    index_path: str | Path,
    benchmark_ids: set[str] | None = None,
) -> dict[str, Any]:
    summary = load_yaml_json(summary_path)
    out_root = ensure_dir(out_dir)
    benchmark_index: dict[str, Any] = {"benchmarks": []}
    for benchmark in summary.get("benchmark_results", []):
        if benchmark_ids is not None and benchmark["benchmark_id"] not in benchmark_ids:
            continue
        json_path = out_root / f"{benchmark['benchmark_id']}.json"
        write_json(json_path, benchmark)
        benchmark_index["benchmarks"].append(
            {
                "benchmark_id": benchmark["benchmark_id"],
                "display_name": benchmark["display_name"],
                "benchmark_facts_path": str(json_path),
                "dimension_id": benchmark.get("dimension_id"),
            }
        )
    write_json(index_path, benchmark_index)
    return benchmark_index


def build_benchmark_analysis_artifacts(
    skill_root: Path,
    summary_path: str | Path,
    samples_path: str | Path,
    benchmark_descriptions_index_path: str | Path,
    out_dir: str | Path,
    index_path: str | Path,
    analysis_mode: str | None = None,
    benchmark_ids: set[str] | None = None,
) -> dict[str, Any]:
    summary = load_yaml_json(summary_path)
    samples_payload = load_yaml_json(samples_path)
    samples_by_benchmark: dict[str, list[dict[str, Any]]] = {}
    for sample in samples_payload.get("samples", []):
        samples_by_benchmark.setdefault(sample["benchmark_id"], []).append(sample)
    run_lookup = {run["benchmark_id"]: run for run in summary.get("selected_runs", []) if run.get("benchmark_id")}
    description_index = load_yaml_json(benchmark_descriptions_index_path)
    description_lookup = {
        entry["benchmark_id"]: load_yaml_json(entry["json_path"])
        for entry in description_index.get("benchmark_descriptions", [])
    }
    llm_config = resolve_llm_analysis_config(skill_root)
    resolved_mode, mode_warnings = resolve_analysis_mode(analysis_mode, llm_config)

    out_root = ensure_dir(out_dir)
    index: dict[str, Any] = {"benchmark_analysis": []}

    for benchmark in summary.get("benchmark_results", []):
        if benchmark_ids is not None and benchmark["benchmark_id"] not in benchmark_ids:
            continue
        benchmark_copy = enrich_benchmark_payload(
            benchmark,
            run_lookup.get(benchmark["benchmark_id"]),
            samples_by_benchmark.get(benchmark["benchmark_id"], []),
        )
        description_payload = description_lookup.get(benchmark["benchmark_id"], {})
        analysis_payload = {
            **benchmark_copy,
            "description_summary": description_payload.get("resolved_description"),
            "evaluation_questions": description_payload.get("evaluation_questions", []),
            "assessed_risk_or_capability_scope": description_payload.get("assessed_risk_or_capability_scope"),
            "source_quality": description_payload.get("source_quality"),
            "used_web_search": description_payload.get("used_web_search", False),
            "confidence_notes": build_confidence_notes(benchmark_copy, description_payload),
            "warnings": unique_preserve_order(
                list(benchmark_copy.get("warnings", []))
                + list(description_payload.get("warnings", []))
            ),
            "analysis_generated_by": "rule-based-local-analyzer",
        }
        analysis_payload["warnings"] = unique_preserve_order(
            list(analysis_payload.get("warnings", [])) + mode_warnings
        )
        if resolved_mode in {"llm", "hybrid"}:
            try:
                llm_payload = call_benchmark_analysis_llm(llm_config, analysis_payload, description_payload)
                analysis_payload = merge_llm_analysis(analysis_payload, llm_payload, llm_config["model"])
                analysis_payload["llm_analysis"] = {
                    "provider": llm_config["provider"],
                    "model": llm_config["model"],
                    "mode": resolved_mode,
                }
            except Exception as exc:
                analysis_payload["warnings"] = unique_preserve_order(
                    list(analysis_payload.get("warnings", []))
                    + [f"LLM benchmark analysis failed for `{benchmark['benchmark_id']}`: {exc}"]
                )
        json_path = out_root / f"{benchmark['benchmark_id']}.json"
        write_json(json_path, analysis_payload)
        index["benchmark_analysis"].append(
            {
                "benchmark_id": benchmark["benchmark_id"],
                "display_name": benchmark["display_name"],
                "analysis_json_path": str(json_path),
                "dimension_id": benchmark.get("dimension_id"),
            }
        )

    write_json(index_path, index)
    return index


def build_confidence_notes(benchmark_analysis: dict[str, Any], description_payload: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if benchmark_analysis.get("coverage", {}).get("is_partial"):
        notes.append("Interpret benchmark-level conclusions with caution because coverage is partial.")
    if description_payload.get("source_quality") in {"low", "unknown"}:
        notes.append("Benchmark description quality is weak; local documentation should be expanded or enriched before publication.")
    if benchmark_analysis.get("dimension_source") == "suggested":
        notes.append("Dimension assignment was heuristic rather than registry-backed.")
    return notes


def resolve_analysis_mode(requested_mode: str | None, llm_config: dict[str, Any]) -> tuple[str, list[str]]:
    mode = (requested_mode or "auto").lower()
    available, reason = llm_analysis_available(llm_config)
    warnings: list[str] = []
    if mode == "auto":
        return ("hybrid" if available else "rule"), warnings if available else ([reason] if reason else [])
    if mode in {"llm", "hybrid"} and not available:
        warnings.append(reason or "LLM analysis is unavailable with the current configuration.")
        return "rule", warnings
    return mode, warnings


def merge_llm_analysis(
    rule_payload: dict[str, Any],
    llm_payload: dict[str, Any],
    llm_model: str,
) -> dict[str, Any]:
    merged = dict(rule_payload)
    for key in ["description_summary", "key_findings", "implications", "confidence_notes"]:
        value = llm_payload.get(key)
        if value:
            merged[key] = value
    merged["analysis_generated_by"] = f"{rule_payload.get('analysis_generated_by', 'rule-based-local-analyzer')}+{llm_model}"
    return merged


def merge_dimension_llm_analysis(
    rule_payload: dict[str, Any],
    llm_payload: dict[str, Any],
    llm_model: str,
) -> dict[str, Any]:
    merged = dict(rule_payload)
    for key in [
        "key_findings",
        "cross_benchmark_consensus",
        "cross_benchmark_tensions",
        "risk_interpretation",
        "implications",
    ]:
        value = llm_payload.get(key)
        if value:
            merged[key] = value
    merged["analysis_generated_by"] = f"{rule_payload.get('analysis_generated_by', 'rule-based-dimension-analyzer')}+{llm_model}"
    return merged


def merge_system_card_llm_analysis(
    rule_payload: dict[str, Any],
    llm_payload: dict[str, Any],
    llm_model: str,
) -> dict[str, Any]:
    merged = dict(rule_payload)
    for key in ["key_findings", "release_considerations", "recommended_mitigations"]:
        value = llm_payload.get(key)
        if value:
            merged[key] = value
    merged["analysis_generated_by"] = f"{rule_payload.get('analysis_generated_by', 'rule-based-system-card-analyzer')}+{llm_model}"
    return merged


def render_benchmark_reports(
    skill_root: Path,
    benchmark_analysis_index_path: str | Path,
    out_dir: str | Path,
    index_path: str | Path,
    language: str,
) -> dict[str, Any]:
    benchmark_analysis_index = load_yaml_json(benchmark_analysis_index_path)
    env = load_jinja_env(skill_root)
    template = env.get_template("benchmark_report.md.j2")
    out_root = ensure_dir(out_dir)
    benchmark_index: dict[str, Any] = {"benchmarks": []}

    for entry in benchmark_analysis_index.get("benchmark_analysis", []):
        analysis_payload = load_yaml_json(entry["analysis_json_path"])
        rendered = template.render(language=language, benchmark=analysis_payload)
        md_path = out_root / f"{analysis_payload['benchmark_id']}.md"
        md_path.write_text(rendered, encoding="utf-8")
        benchmark_index["benchmarks"].append(
            {
                "benchmark_id": analysis_payload["benchmark_id"],
                "display_name": analysis_payload["display_name"],
                "benchmark_analysis_path": entry["analysis_json_path"],
                "markdown_path": str(md_path),
                "dimension_id": analysis_payload.get("dimension_id"),
            }
        )

    write_json(index_path, benchmark_index)
    return benchmark_index


def build_registry_suggestions(summary: dict[str, Any], benchmark_registry: dict[str, Any]) -> dict[str, Any]:
    registry_entries = benchmark_registry.get("benchmarks", {})
    suggestions: list[dict[str, Any]] = []

    for benchmark in summary.get("benchmark_results", []):
        benchmark_id = benchmark["benchmark_id"]
        if benchmark_id in registry_entries and benchmark.get("dimension_id"):
            continue

        task_name = benchmark.get("task_name") or "unknown"
        task_args = benchmark.get("task_args") or {}
        match_args = filter_match_task_args(task_args)
        metric_names = [
            metric.get("name") or metric.get("metric_name")
            for metric in benchmark.get("all_metrics", [])
            if metric.get("name") or metric.get("metric_name")
        ]
        dimension_id, dimension_reason = suggest_dimension(task_name, metric_names, benchmark_registry)
        primary_metrics, metrics_reason = suggest_primary_metrics(task_name, metric_names, benchmark_registry)
        sample_selection, sample_reason = suggest_sample_selection(task_name, match_args, metric_names, benchmark_registry)

        suggestions.append(
            {
                "benchmark_id": benchmark_id,
                "status": "unmapped" if benchmark_id not in registry_entries else "missing_dimension",
                "suggested_entry": {
                    "display_name": suggest_display_name(task_name, match_args),
                    "task_name": task_name,
                    "match": match_args,
                    "dimension": dimension_id,
                    "summary_focus": benchmark.get("summary_focus") or f"auto-generated suggestion for {task_name}",
                    "primary_metrics": primary_metrics,
                    "sample_selection": sample_selection,
                },
                "evidence": {
                    "task_name": task_name,
                    "task_args": task_args,
                    "metric_names": metric_names,
                    "source_log": benchmark.get("input_path"),
                },
                "reasons": {
                    "dimension": dimension_reason,
                    "primary_metrics": metrics_reason,
                    "sample_selection": sample_reason,
                },
            }
        )

    return {
        "suggestions": suggestions,
        "count": len(suggestions),
    }


def suggest_registry_entries(
    summary_path: str | Path,
    benchmark_registry_path: str | Path,
    yaml_out_path: str | Path,
    json_out_path: str | Path | None = None,
) -> dict[str, Any]:
    summary = load_yaml_json(summary_path)
    benchmark_registry = load_yaml(benchmark_registry_path)
    payload = build_registry_suggestions(summary, benchmark_registry)
    write_yaml(yaml_out_path, payload)
    if json_out_path is not None:
        write_json(json_out_path, payload)
    return payload


def group_dimensions(
    benchmark_analysis_index_path: str | Path,
    dimension_registry_path: str | Path,
    manual_overrides_dir: str | Path | None,
    out_dir: str | Path,
    index_path: str | Path,
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    benchmark_analysis_index = load_yaml_json(benchmark_analysis_index_path)
    dimension_registry = load_yaml(dimension_registry_path).get("dimensions", {})
    manual_overrides = load_manual_overrides(manual_overrides_dir)
    llm_config = resolve_llm_analysis_config(Path(__file__).resolve().parents[2])
    resolved_mode, mode_warnings = resolve_analysis_mode(analysis_mode, llm_config)

    benchmarks = {
        entry["benchmark_id"]: load_yaml_json(entry["analysis_json_path"])
        for entry in benchmark_analysis_index.get("benchmark_analysis", [])
    }

    out_root = ensure_dir(out_dir)
    dimension_index: dict[str, Any] = {"dimensions": []}

    dimension_meta: dict[str, dict[str, Any]] = {}
    for dimension_id, entry in dimension_registry.items():
        dimension_meta[dimension_id] = dict(entry)

    for benchmark in benchmarks.values():
        dimension_id = benchmark.get("dimension_id") or "unmapped"
        dimension_meta.setdefault(
            dimension_id,
            {
                "title": prettify_label(dimension_id),
                "order": 999,
                "summary_mode": "auto",
                "description": f"Auto-generated dimension bucket for `{dimension_id}` benchmarks.",
            },
        )

    for dimension_id, entry in sorted(dimension_meta.items(), key=lambda item: item[1].get("order", 999)):
        override = manual_overrides.get(dimension_id, {})
        exclude = set(override.get("exclude_benchmarks") or [])
        include = set(override.get("include_benchmarks") or [])
        auto_benchmark_ids = {
            benchmark_id
            for benchmark_id, benchmark in benchmarks.items()
            if (benchmark.get("dimension_id") or "unmapped") == dimension_id
        }
        target_benchmark_ids = (auto_benchmark_ids | include) - exclude

        selected_benchmarks: list[dict[str, Any]] = []
        warnings: list[str] = []
        for benchmark_id in sorted(target_benchmark_ids):
            benchmark = benchmarks.get(benchmark_id)
            if benchmark is None:
                if benchmark_id in include:
                    warnings.append(f"Dimension {dimension_id} references missing benchmark `{benchmark_id}` from manual overrides.")
                continue
            benchmark_copy = dict(benchmark)
            benchmark_copy["primary_metric_summary"] = ", ".join(
                f"{metric['name']}={metric['formatted_value']}" for metric in benchmark_copy.get("primary_metrics", [])
            )
            selected_benchmarks.append(benchmark_copy)

        if not selected_benchmarks and not override.get("render_if_empty"):
            continue

        selected_samples = []
        for benchmark in selected_benchmarks:
            for sample in benchmark.get("selected_samples", [])[:2]:
                sample_copy = dict(sample)
                sample_copy["benchmark_display_name"] = benchmark["display_name"]
                selected_samples.append(sample_copy)
            for warning in benchmark.get("warnings", []):
                if warning not in warnings:
                    warnings.append(warning)

        observations = build_dimension_observations(selected_benchmarks)
        dimension_payload = {
            "dimension_id": dimension_id,
            "title": entry.get("title", prettify_label(dimension_id)),
            "description": entry.get("description"),
            "summary_mode": entry.get("summary_mode", "auto"),
            "order": entry.get("order", 999),
            "benchmarks": selected_benchmarks,
            "selected_samples": selected_samples,
            "chapter_claims": override.get("chapter_claims") or [],
            "notes": override.get("notes") or "",
            "observations": observations,
            "key_findings": [],
            "cross_benchmark_consensus": [],
            "cross_benchmark_tensions": [],
            "risk_interpretation": [],
            "implications": [],
            "warnings": warnings,
            "manual_overrides_applied": list(override.keys()),
        }
        dimension_payload["key_findings"] = build_dimension_key_findings(dimension_payload)
        dimension_payload["cross_benchmark_consensus"] = build_dimension_consensus(dimension_payload)
        dimension_payload["cross_benchmark_tensions"] = build_dimension_tensions(dimension_payload)
        dimension_payload["risk_interpretation"] = build_dimension_risk_interpretation(dimension_payload)
        dimension_payload["implications"] = build_dimension_implications(dimension_payload)
        dimension_payload["analysis_generated_by"] = "rule-based-dimension-analyzer"
        dimension_payload["warnings"] = unique_preserve_order(
            list(dimension_payload.get("warnings", [])) + mode_warnings
        )
        if resolved_mode in {"llm", "hybrid"}:
            try:
                llm_payload = call_dimension_analysis_llm(llm_config, dimension_payload)
                dimension_payload = merge_dimension_llm_analysis(dimension_payload, llm_payload, llm_config["model"])
                dimension_payload["llm_analysis"] = {
                    "provider": llm_config["provider"],
                    "model": llm_config["model"],
                    "mode": resolved_mode,
                }
            except Exception as exc:
                dimension_payload["warnings"] = unique_preserve_order(
                    list(dimension_payload.get("warnings", []))
                    + [f"LLM dimension synthesis failed for `{dimension_id}`: {exc}"]
                )
        json_path = out_root / f"{dimension_id}.json"
        write_json(json_path, dimension_payload)
        dimension_index["dimensions"].append(
            {
                "dimension_id": dimension_id,
                "title": dimension_payload["title"],
                "json_path": str(json_path),
                "order": dimension_payload["order"],
            }
        )

    write_json(index_path, dimension_index)
    return dimension_index


def build_dimension_observations(benchmarks: list[dict[str, Any]]) -> list[str]:
    observations: list[str] = []
    for benchmark in benchmarks:
        primary = benchmark.get("primary_metrics", [])
        if not primary:
            observations.append(f"{benchmark['display_name']} does not have any primary metrics available.")
            continue
        metric_summary = ", ".join(f"{metric['name']}={metric['formatted_value']}" for metric in primary)
        observations.append(f"{benchmark['display_name']} reports primary results of {metric_summary}.")
    if not observations:
        observations.append("No benchmarks are currently included in this dimension.")
    return observations


def build_dimension_key_findings(dimension_payload: dict[str, Any]) -> list[str]:
    benchmarks = dimension_payload.get("benchmarks", [])
    findings: list[str] = []
    if not benchmarks:
        return ["No benchmarks are currently included in this dimension."]

    highest_mean = None
    highest_benchmark = None
    for benchmark in benchmarks:
        mean_value = benchmark_metric_value(benchmark, "mean")
        if mean_value is None:
            mean_value = benchmark.get("score_distribution", {}).get("mean")
        if mean_value is None:
            continue
        if highest_mean is None or mean_value > highest_mean:
            highest_mean = mean_value
            highest_benchmark = benchmark
    if highest_benchmark is not None:
        findings.append(
            f"The riskiest benchmark in this dimension is {highest_benchmark['display_name']} with mean={format_metric_value(highest_mean)}."
        )
    partial_benchmarks = [
        benchmark
        for benchmark in benchmarks
        if benchmark.get("coverage", {}).get("is_partial")
    ]
    if partial_benchmarks:
        joined = ", ".join(
            f"{benchmark['display_name']} ({benchmark['coverage']['completed_samples']}/{benchmark['coverage']['total_samples']})"
            for benchmark in partial_benchmarks
        )
        findings.append(f"Coverage is partial for: {joined}.")
    best_slice = None
    for benchmark in benchmarks:
        slice_analysis = benchmark.get("slice_analysis") or []
        if not slice_analysis:
            continue
        candidate = slice_analysis[0]["highest_risk"][0]
        if best_slice is None or candidate.get("mean", -1) > best_slice.get("mean", -1):
            best_slice = {**candidate, "field": slice_analysis[0]["field"], "benchmark": benchmark["display_name"]}
    if best_slice is not None:
        findings.append(
            f"The most exposed slice observed here is `{best_slice['value']}` in {best_slice['benchmark']} ({best_slice['field']}, mean={format_metric_value(best_slice['mean'])})."
        )
    top_pattern = None
    for benchmark in benchmarks:
        for pattern in benchmark.get("failure_patterns", []):
            if top_pattern is None or pattern.get("count", 0) > top_pattern.get("count", 0):
                top_pattern = {**pattern, "benchmark": benchmark["display_name"]}
    if top_pattern is not None:
        findings.append(
            f"Representative failure patterns are dominated by {top_pattern['label']} in {top_pattern['benchmark']}."
        )
    return findings


def build_dimension_risk_interpretation(dimension_payload: dict[str, Any]) -> list[str]:
    interpretations: list[str] = []
    for benchmark in dimension_payload.get("benchmarks", []):
        distribution = benchmark.get("score_distribution", {})
        mean_value = benchmark_metric_value(benchmark, "mean")
        if mean_value is None:
            mean_value = distribution.get("mean")
        if mean_value is None:
            continue
        interpretations.append(
            f"{benchmark['display_name']} shows {risk_level_from_mean(mean_value)} overall risk with aggregate mean={format_metric_value(mean_value)} and sample-score p90={format_metric_value(distribution.get('p90'))}."
        )
    if not interpretations:
        interpretations.append("No risk interpretation is available because no numeric benchmark scores were found.")
    return interpretations


def build_dimension_implications(dimension_payload: dict[str, Any]) -> list[str]:
    implications: list[str] = []
    for benchmark in dimension_payload.get("benchmarks", []):
        for item in benchmark.get("implications", []):
            if item not in implications:
                implications.append(item)
    return implications[:5]


def build_dimension_consensus(dimension_payload: dict[str, Any]) -> list[str]:
    benchmarks = dimension_payload.get("benchmarks", [])
    if len(benchmarks) < 2:
        return ["No cross-benchmark consensus is available because this dimension currently contains a single benchmark."]
    consensus: list[str] = []
    if all(benchmark.get("coverage", {}).get("is_partial") for benchmark in benchmarks):
        consensus.append("All included benchmarks have partial coverage, so dimension-level conclusions are preliminary.")
    common_patterns = set.intersection(
        *[
            {pattern["label"] for pattern in benchmark.get("failure_patterns", [])}
            for benchmark in benchmarks
            if benchmark.get("failure_patterns")
        ]
    ) if all(benchmark.get("failure_patterns") for benchmark in benchmarks) else set()
    if common_patterns:
        consensus.append(
            "Shared failure patterns across benchmarks include: " + ", ".join(sorted(common_patterns)) + "."
        )
    return consensus


def build_dimension_tensions(dimension_payload: dict[str, Any]) -> list[str]:
    benchmarks = dimension_payload.get("benchmarks", [])
    if len(benchmarks) < 2:
        return []
    tensions: list[str] = []
    means = [(benchmark["display_name"], benchmark_metric_value(benchmark, "mean")) for benchmark in benchmarks]
    means = [(name, mean) for name, mean in means if mean is not None]
    if len(means) >= 2:
        sorted_means = sorted(means, key=lambda item: item[1], reverse=True)
        if sorted_means[0][1] - sorted_means[-1][1] > 0.15:
            tensions.append(
                f"Risk severity differs materially across benchmarks, ranging from {sorted_means[-1][0]} ({format_metric_value(sorted_means[-1][1])}) to {sorted_means[0][0]} ({format_metric_value(sorted_means[0][1])})."
            )
    return tensions


def build_system_key_findings(summary: dict[str, Any], dimension_payloads: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    riskiest_benchmark = None
    highest_mean = None
    for benchmark in summary.get("benchmark_results", []):
        mean_value = benchmark_metric_value(benchmark, "mean")
        if isinstance(mean_value, (int, float)) and (highest_mean is None or mean_value > highest_mean):
            highest_mean = float(mean_value)
            riskiest_benchmark = benchmark
    if riskiest_benchmark is not None:
        findings.append(
            f"The highest overall benchmark risk in this report is {riskiest_benchmark['display_name']} with mean={format_metric_value(highest_mean)}."
        )
    partials = [
        benchmark
        for benchmark in summary.get("benchmark_results", [])
        if benchmark.get("coverage", {}).get("is_partial")
    ]
    if partials:
        findings.append(
            "Some benchmark results are based on partial coverage: "
            + ", ".join(
                f"{benchmark['display_name']} ({benchmark['coverage']['completed_samples']}/{benchmark['coverage']['total_samples']})"
                for benchmark in partials
            )
            + "."
        )
    top_dimension_finding = next(
        (
            finding
            for dimension in dimension_payloads
            for finding in dimension.get("key_findings", [])
        ),
        None,
    )
    if top_dimension_finding:
        findings.append(top_dimension_finding)
    return findings[:5]


def build_system_caveats(summary: dict[str, Any], build_warnings: list[str]) -> list[str]:
    caveats = list(build_warnings)
    for benchmark in summary.get("benchmark_results", []):
        if benchmark.get("dimension_source") == "suggested":
            caveats.append(
                f"Benchmark `{benchmark['display_name']}` was assigned to dimension `{benchmark.get('dimension_id')}` heuristically."
            )
    return unique_preserve_order(caveats)


def render_dimension_reports(
    skill_root: Path,
    dimension_index_path: str | Path,
    out_dir: str | Path,
    report_manifest_path: str | Path | None,
    target_language: str | None = None,
) -> dict[str, Any]:
    dimension_index = load_yaml_json(dimension_index_path)
    report_manifest = load_yaml(report_manifest_path) if report_manifest_path else {}
    language = target_language or report_manifest.get("report", {}).get("language", "zh")

    env = load_jinja_env(skill_root)
    template = env.get_template("dimension_summary.md.j2")
    out_root = ensure_dir(out_dir)

    for entry in dimension_index.get("dimensions", []):
        payload = load_yaml_json(entry["json_path"])
        translated_payload = translate_dimension_payload_for_language(skill_root, payload, language)
        rendered = template.render(language=language, dimension=translated_payload)
        suffix = "" if target_language is None else f"_{language}"
        md_path = out_root / f"{payload['dimension_id']}{suffix}.md"
        md_path.write_text(rendered, encoding="utf-8")
        if target_language is None:
            entry["markdown_path"] = str(md_path)
        markdown_paths = dict(entry.get("markdown_paths", {}))
        markdown_paths[language] = str(md_path)
        entry["markdown_paths"] = markdown_paths
    write_json(dimension_index_path, dimension_index)
    return dimension_index


def build_system_card_facts(
    summary_path: str | Path,
    dimension_index_path: str | Path,
    out_path: str | Path,
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    summary = load_yaml_json(summary_path)
    dimension_index = load_yaml_json(dimension_index_path)
    dimension_entries = sorted(dimension_index.get("dimensions", []), key=lambda item: item.get("order", 999))
    dimension_payloads = [load_yaml_json(entry["json_path"]) for entry in dimension_entries]
    llm_config = resolve_llm_analysis_config(Path(__file__).resolve().parents[2])
    resolved_mode, mode_warnings = resolve_analysis_mode(analysis_mode, llm_config)

    title_lookup = {entry["dimension_id"]: entry["title"] for entry in dimension_entries}
    benchmark_rows = []
    for benchmark in summary.get("benchmark_results", []):
        benchmark_rows.append(
            {
                "display_name": benchmark["display_name"],
                "dimension_title": title_lookup.get(benchmark.get("dimension_id"), benchmark.get("dimension_id") or "unmapped"),
                "primary_metric_summary": ", ".join(
                    f"{metric['name']}={metric['formatted_value']}" for metric in benchmark.get("primary_metrics", [])
                ),
            }
        )

    system_card = {
        "report": summary.get("report", {}),
        "benchmark_rows": benchmark_rows,
        "dimension_summaries": [
            {
                "dimension_id": dimension["dimension_id"],
                "title": dimension["title"],
                "key_findings": dimension.get("key_findings", []),
                "risk_interpretation": dimension.get("risk_interpretation", []),
                "warnings": dimension.get("warnings", []),
            }
            for dimension in dimension_payloads
        ],
        "overall_risk_profile": build_overall_risk_profile(summary, dimension_payloads),
        "overall_capability_profile": build_overall_capability_profile(summary, dimension_payloads),
        "release_considerations": build_system_caveats(summary, summary.get("warnings", [])),
        "recommended_mitigations": build_system_mitigations(dimension_payloads),
        "key_findings": build_system_key_findings(summary, dimension_payloads),
        "coverage_and_caveats": build_system_caveats(summary, summary.get("warnings", [])),
        "warnings": unique_preserve_order(
            list(summary.get("warnings", []))
            + [warning for dimension in dimension_payloads for warning in dimension.get("warnings", [])]
            + mode_warnings
        ),
        "analysis_generated_by": "rule-based-system-card-analyzer",
    }
    if resolved_mode in {"llm", "hybrid"}:
        try:
            llm_payload = call_system_card_analysis_llm(llm_config, system_card)
            system_card = merge_system_card_llm_analysis(system_card, llm_payload, llm_config["model"])
            system_card["llm_analysis"] = {
                "provider": llm_config["provider"],
                "model": llm_config["model"],
                "mode": resolved_mode,
            }
        except Exception as exc:
            system_card["warnings"] = unique_preserve_order(
                list(system_card.get("warnings", []))
                + [f"LLM system-card synthesis failed: {exc}"]
            )
    write_json(out_path, system_card)
    return system_card


def build_overall_risk_profile(summary: dict[str, Any], dimension_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    highest_benchmark = None
    highest_mean = None
    for benchmark in summary.get("benchmark_results", []):
        mean_value = benchmark_metric_value(benchmark, "mean")
        if mean_value is not None and (highest_mean is None or mean_value > highest_mean):
            highest_mean = mean_value
            highest_benchmark = benchmark["display_name"]
    return {
        "highest_risk_benchmark": highest_benchmark,
        "highest_risk_mean": highest_mean,
        "highest_risk_mean_formatted": format_metric_value(highest_mean),
        "dimension_count": len(dimension_payloads),
    }


def build_overall_capability_profile(summary: dict[str, Any], dimension_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "available_dimensions": [dimension["title"] for dimension in dimension_payloads],
        "benchmark_count": len(summary.get("benchmark_results", [])),
    }


def build_system_mitigations(dimension_payloads: list[dict[str, Any]]) -> list[str]:
    mitigations: list[str] = []
    for dimension in dimension_payloads:
        for implication in dimension.get("implications", []):
            if implication not in mitigations:
                mitigations.append(implication)
    return mitigations[:8]


def translate_dimension_payload_for_language(
    skill_root: Path,
    payload: dict[str, Any],
    target_language: str,
) -> dict[str, Any]:
    if target_language.lower().startswith("en"):
        return payload
    llm_config = resolve_llm_analysis_config(skill_root)
    available, reason = llm_analysis_available(llm_config)
    if not available:
        translated = dict(payload)
        translated["warnings"] = unique_preserve_order(
            list(payload.get("warnings", []))
            + [reason or f"LLM translation unavailable for dimension `{payload.get('dimension_id')}`."]
        )
        return translated

    translation_payload = {
        "title": payload.get("title"),
        "description": payload.get("description"),
        "key_findings": payload.get("key_findings", []),
        "cross_benchmark_observations": payload.get("observations", []),
        "cross_benchmark_consensus": payload.get("cross_benchmark_consensus", []),
        "cross_benchmark_tensions": payload.get("cross_benchmark_tensions", []),
        "risk_interpretation": payload.get("risk_interpretation", []),
        "implications": payload.get("implications", []),
        "warnings": payload.get("warnings", []),
        "selected_samples": [
            {
                "sample_id": sample.get("sample_id"),
                "benchmark_display_name": sample.get("benchmark_display_name"),
                "selection_reason": sample.get("selection_reason"),
                "sample_highlights": sample.get("sample_highlights", []),
            }
            for sample in payload.get("selected_samples", [])
        ],
    }
    try:
        translated_fields = call_translation_llm(llm_config, "dimension", translation_payload, target_language)
    except Exception as exc:
        translated = dict(payload)
        translated["warnings"] = unique_preserve_order(
            list(payload.get("warnings", []))
            + [f"LLM translation failed for dimension `{payload.get('dimension_id')}`: {exc}"]
        )
        return translated

    translated = dict(payload)
    translated["title"] = translated_fields.get("title", payload.get("title"))
    translated["description"] = translated_fields.get("description", payload.get("description"))
    translated["key_findings"] = translated_fields.get("key_findings", payload.get("key_findings", []))
    translated["observations"] = translated_fields.get("cross_benchmark_observations", payload.get("observations", []))
    translated["cross_benchmark_consensus"] = translated_fields.get("cross_benchmark_consensus", payload.get("cross_benchmark_consensus", []))
    translated["cross_benchmark_tensions"] = translated_fields.get("cross_benchmark_tensions", payload.get("cross_benchmark_tensions", []))
    translated["risk_interpretation"] = translated_fields.get("risk_interpretation", payload.get("risk_interpretation", []))
    translated["implications"] = translated_fields.get("implications", payload.get("implications", []))
    translated["warnings"] = translated_fields.get("warnings", payload.get("warnings", []))
    translated_samples = []
    translation_samples = translated_fields.get("selected_samples", [])
    sample_translation_lookup = {sample.get("sample_id"): sample for sample in translation_samples}
    for sample in payload.get("selected_samples", []):
        translated_sample = dict(sample)
        sample_translation = sample_translation_lookup.get(sample.get("sample_id"), {})
        if sample_translation.get("selection_reason"):
            translated_sample["selection_reason"] = sample_translation["selection_reason"]
        translated_samples.append(translated_sample)
    translated["selected_samples"] = translated_samples
    return translated


def translate_system_card_payload_for_language(
    skill_root: Path,
    payload: dict[str, Any],
    target_language: str,
) -> dict[str, Any]:
    if target_language.lower().startswith("en"):
        return payload
    llm_config = resolve_llm_analysis_config(skill_root)
    available, reason = llm_analysis_available(llm_config)
    if not available:
        translated = dict(payload)
        translated["warnings"] = unique_preserve_order(
            list(payload.get("warnings", []))
            + [reason or "LLM translation unavailable for system card rendering."]
        )
        return translated

    translation_payload = {
        "report": {
            "title": payload.get("report", {}).get("title"),
            "notes": payload.get("report", {}).get("notes"),
        },
        "key_findings": payload.get("key_findings", []),
        "coverage_and_caveats": payload.get("coverage_and_caveats", []),
        "recommended_mitigations": payload.get("recommended_mitigations", []),
    }
    try:
        translated_fields = call_translation_llm(llm_config, "system_card", translation_payload, target_language)
    except Exception as exc:
        translated = dict(payload)
        translated["warnings"] = unique_preserve_order(
            list(payload.get("warnings", []))
            + [f"LLM translation failed for top-level system card: {exc}"]
        )
        return translated

    translated = dict(payload)
    translated_report = dict(payload.get("report", {}))
    translated_report["title"] = translated_fields.get("report", {}).get("title", translated_report.get("title"))
    if translated_fields.get("report", {}).get("notes"):
        translated_report["notes"] = translated_fields["report"]["notes"]
    translated["report"] = translated_report
    translated["key_findings"] = translated_fields.get("key_findings", payload.get("key_findings", []))
    translated["coverage_and_caveats"] = translated_fields.get("coverage_and_caveats", payload.get("coverage_and_caveats", []))
    translated["recommended_mitigations"] = translated_fields.get("recommended_mitigations", payload.get("recommended_mitigations", []))
    return translated


def render_system_card(
    skill_root: Path,
    system_card_json_path: str | Path,
    dimension_index_path: str | Path,
    out_path: str | Path,
    target_language: str | None = None,
) -> None:
    system_card = load_yaml_json(system_card_json_path)
    dimension_index = load_yaml_json(dimension_index_path)
    env = load_jinja_env(skill_root)
    template = env.get_template("system_card.md.j2")
    render_language = target_language or system_card.get("report", {}).get("language", "zh")
    translated_system_card = translate_system_card_payload_for_language(skill_root, system_card, render_language)

    dimension_entries = sorted(dimension_index.get("dimensions", []), key=lambda item: item.get("order", 999))
    dimensions_for_render: list[dict[str, Any]] = []
    for entry in dimension_entries:
        payload = load_yaml_json(entry["json_path"])
        translated_payload = translate_dimension_payload_for_language(skill_root, payload, render_language)
        dimension_env = load_jinja_env(skill_root)
        dimension_template = dimension_env.get_template("dimension_summary.md.j2")
        rendered_markdown = dimension_template.render(language=render_language, dimension=translated_payload)
        dimensions_for_render.append({**entry, "rendered_markdown": rendered_markdown})

    rendered = template.render(
        report=translated_system_card.get("report", {}),
        benchmark_rows=translated_system_card.get("benchmark_rows", []),
        dimensions=dimensions_for_render,
        key_findings=translated_system_card.get("key_findings", []),
        coverage_and_caveats=translated_system_card.get("coverage_and_caveats", []),
        overall_risk_profile=translated_system_card.get("overall_risk_profile", {}),
        recommended_mitigations=translated_system_card.get("recommended_mitigations", []),
        warnings=translated_system_card.get("warnings", []),
        language=render_language,
    )
    Path(out_path).write_text(rendered, encoding="utf-8")


def run_full_pipeline(
    skill_root: Path,
    input_target: str | Path | list[str] | list[Path],
    benchmark_registry_path: str | Path,
    dimension_registry_path: str | Path,
    artifacts_dir: str | Path,
    report_manifest_path: str | Path | None = None,
    manual_overrides_dir: str | Path | None = None,
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    artifacts_root = ensure_dir(artifacts_dir)
    benchmark_dir = ensure_dir(artifacts_root / "benchmarks")
    benchmark_description_dir = ensure_dir(artifacts_root / "benchmark_descriptions")
    benchmark_analysis_dir = ensure_dir(artifacts_root / "benchmark_analysis")
    dimension_dir = ensure_dir(artifacts_root / "dimensions")

    normalization = normalize_logs(input_target, benchmark_registry_path, report_manifest_path, artifacts_root)
    summary = aggregate_summary(
        normalization["run_index_path"],
        benchmark_registry_path,
        report_manifest_path,
        artifacts_root / "summary.json",
    )
    samples = select_representative_samples(
        normalization["run_index_path"],
        benchmark_registry_path,
        artifacts_root / "samples.json",
    )
    benchmark_facts_index = write_benchmark_facts(
        artifacts_root / "summary.json",
        benchmark_dir,
        artifacts_root / "benchmark_facts_index.json",
    )
    benchmark_descriptions_index = build_benchmark_descriptions(
        skill_root,
        artifacts_root / "summary.json",
        benchmark_registry_path,
        benchmark_description_dir,
        artifacts_root / "benchmark_descriptions_index.json",
    )
    benchmark_analysis_index = build_benchmark_analysis_artifacts(
        skill_root,
        artifacts_root / "summary.json",
        artifacts_root / "samples.json",
        artifacts_root / "benchmark_descriptions_index.json",
        benchmark_analysis_dir,
        artifacts_root / "benchmark_analysis_index.json",
        analysis_mode=analysis_mode,
    )
    benchmark_index = render_benchmark_reports(
        skill_root,
        artifacts_root / "benchmark_analysis_index.json",
        benchmark_dir,
        artifacts_root / "benchmark_index.json",
        summary.get("report", {}).get("language", "zh"),
    )
    registry_suggestions = suggest_registry_entries(
        artifacts_root / "summary.json",
        benchmark_registry_path,
        artifacts_root / "registry_suggestions.yaml",
        artifacts_root / "registry_suggestions.json",
    )
    dimension_index = group_dimensions(
        artifacts_root / "benchmark_analysis_index.json",
        dimension_registry_path,
        manual_overrides_dir,
        dimension_dir,
        artifacts_root / "dimension_index.json",
        analysis_mode=analysis_mode,
    )
    render_dimension_reports(
        skill_root,
        artifacts_root / "dimension_index.json",
        dimension_dir,
        report_manifest_path,
    )
    render_dimension_reports(
        skill_root,
        artifacts_root / "dimension_index.json",
        dimension_dir,
        report_manifest_path,
        target_language="en",
    )
    render_dimension_reports(
        skill_root,
        artifacts_root / "dimension_index.json",
        dimension_dir,
        report_manifest_path,
        target_language="zh",
    )
    system_card_facts = build_system_card_facts(
        artifacts_root / "summary.json",
        artifacts_root / "dimension_index.json",
        artifacts_root / "system_card.json",
        analysis_mode=analysis_mode,
    )
    render_system_card(
        skill_root,
        artifacts_root / "system_card.json",
        artifacts_root / "dimension_index.json",
        artifacts_root / "system_card.md",
    )
    render_system_card(
        skill_root,
        artifacts_root / "system_card.json",
        artifacts_root / "dimension_index.json",
        artifacts_root / "system_card_en.md",
        target_language="en",
    )
    render_system_card(
        skill_root,
        artifacts_root / "system_card.json",
        artifacts_root / "dimension_index.json",
        artifacts_root / "system_card_zh.md",
        target_language="zh",
    )

    build_report = {
        "status": "success",
        "run_index_path": normalization["run_index_path"],
        "results_path": normalization["results_path"],
        "summary_path": str(artifacts_root / "summary.json"),
        "samples_path": str(artifacts_root / "samples.json"),
        "benchmark_facts_index_path": str(artifacts_root / "benchmark_facts_index.json"),
        "benchmark_descriptions_index_path": str(artifacts_root / "benchmark_descriptions_index.json"),
        "benchmark_analysis_index_path": str(artifacts_root / "benchmark_analysis_index.json"),
        "benchmark_index_path": str(artifacts_root / "benchmark_index.json"),
        "registry_suggestions_yaml_path": str(artifacts_root / "registry_suggestions.yaml"),
        "registry_suggestions_json_path": str(artifacts_root / "registry_suggestions.json"),
        "dimension_index_path": str(artifacts_root / "dimension_index.json"),
        "system_card_json_path": str(artifacts_root / "system_card.json"),
        "system_card_path": str(artifacts_root / "system_card.md"),
        "system_card_en_path": str(artifacts_root / "system_card_en.md"),
        "system_card_zh_path": str(artifacts_root / "system_card_zh.md"),
        "warnings": unique_preserve_order(
            list(normalization.get("warnings", []))
            + list(summary.get("warnings", []))
            + list(samples.get("warnings", []))
            + (
                [f"{registry_suggestions['count']} benchmark registration suggestion(s) were generated."]
                if registry_suggestions.get("count")
                else []
            )
            + list(system_card_facts.get("warnings", []))
        ),
    }
    write_json(artifacts_root / "build_report.json", build_report)
    return build_report


def update_system_card_incremental(
    skill_root: Path,
    existing_artifacts_dir: str | Path,
    input_target: str | Path | list[str] | list[Path],
    benchmark_registry_path: str | Path,
    dimension_registry_path: str | Path,
    report_manifest_path: str | Path | None = None,
    manual_overrides_dir: str | Path | None = None,
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    artifacts_root = Path(existing_artifacts_dir)
    if not artifacts_root.exists():
        raise FileNotFoundError(f"Existing artifacts directory not found: {artifacts_root}")

    required_files = [
        artifacts_root / "run_index.json",
        artifacts_root / "summary.json",
        artifacts_root / "samples.json",
        artifacts_root / "benchmark_analysis_index.json",
        artifacts_root / "dimension_index.json",
    ]
    missing = [str(path) for path in required_files if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Incremental update requires existing artifacts. Missing: {missing}")

    with tempfile.TemporaryDirectory(prefix="system_card_incremental_") as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        normalization = normalize_logs(input_target, benchmark_registry_path, report_manifest_path, tmp_dir)
        aggregate_summary(
            normalization["run_index_path"],
            benchmark_registry_path,
            report_manifest_path,
            tmp_dir / "summary.json",
        )
        select_representative_samples(
            normalization["run_index_path"],
            benchmark_registry_path,
            tmp_dir / "samples.json",
        )

        existing_run_index = load_yaml_json(artifacts_root / "run_index.json")
        existing_summary = load_yaml_json(artifacts_root / "summary.json")
        existing_samples = load_yaml_json(artifacts_root / "samples.json")

        new_run_index = load_yaml_json(tmp_dir / "run_index.json")
        new_summary = load_yaml_json(tmp_dir / "summary.json")
        new_samples = load_yaml_json(tmp_dir / "samples.json")
        new_results_rows = load_parquet_rows(tmp_dir / "results.parquet")

        merged_selected_runs, changed_benchmark_ids = upsert_records_by_key(
            existing_run_index.get("selected_runs", []),
            new_run_index.get("selected_runs", []),
            "benchmark_id",
        )
        merged_run_index = {
            "report_defaults": build_report_defaults(load_yaml(report_manifest_path) if report_manifest_path else {}, merged_selected_runs),
            "selected_runs": merged_selected_runs,
            "warnings": unique_preserve_order(
                list(existing_run_index.get("warnings", []))
                + list(new_run_index.get("warnings", []))
            ),
        }
        write_json(artifacts_root / "run_index.json", merged_run_index)

        merged_benchmarks, changed_benchmark_ids = upsert_records_by_key(
            existing_summary.get("benchmark_results", []),
            new_summary.get("benchmark_results", []),
            "benchmark_id",
        )
        report_manifest = load_yaml(report_manifest_path) if report_manifest_path else {}
        merged_summary = {
            "report": merge_report_metadata(
                report_manifest.get("report", {}),
                existing_summary.get("report", {}) or merged_run_index.get("report_defaults", {}),
            ),
            "selected_runs": merged_selected_runs,
            "benchmark_results": merged_benchmarks,
            "warnings": unique_preserve_order(
                list(existing_summary.get("warnings", []))
                + list(new_summary.get("warnings", []))
            ),
        }
        write_json(artifacts_root / "summary.json", merged_summary)

        merged_samples, changed_sample_benchmarks = upsert_samples_by_benchmark(
            existing_samples.get("samples", []),
            new_samples.get("samples", []),
        )
        changed_benchmark_ids = unique_preserve_order(changed_benchmark_ids + changed_sample_benchmarks)
        merged_sample_payload = {
            "samples": merged_samples,
            "warnings": unique_preserve_order(
                list(existing_samples.get("warnings", []))
                + list(new_samples.get("warnings", []))
            ),
        }
        write_json(artifacts_root / "samples.json", merged_sample_payload)

        merged_results_rows = merge_results_rows(
            load_parquet_rows(artifacts_root / "results.parquet"),
            new_results_rows,
        )
        write_parquet(artifacts_root / "results.parquet", merged_results_rows)

        changed_set = set(changed_benchmark_ids)
        benchmark_facts_partial = write_benchmark_facts(
            artifacts_root / "summary.json",
            artifacts_root / "benchmarks",
            tmp_dir / "benchmark_facts_index.partial.json",
            benchmark_ids=changed_set,
        )
        benchmark_descriptions_partial = build_benchmark_descriptions(
            skill_root,
            artifacts_root / "summary.json",
            benchmark_registry_path,
            artifacts_root / "benchmark_descriptions",
            tmp_dir / "benchmark_descriptions_index.partial.json",
            benchmark_ids=changed_set,
        )
        benchmark_analysis_partial = build_benchmark_analysis_artifacts(
            skill_root,
            artifacts_root / "summary.json",
            artifacts_root / "samples.json",
            tmp_dir / "benchmark_descriptions_index.partial.json",
            artifacts_root / "benchmark_analysis",
            tmp_dir / "benchmark_analysis_index.partial.json",
            analysis_mode=analysis_mode,
            benchmark_ids=changed_set,
        )

        merged_benchmark_facts_index = rebuild_file_index(
            merged_summary["benchmark_results"],
            artifacts_root / "benchmarks",
            "benchmarks",
            "benchmark_facts_path",
        )
        write_index(artifacts_root / "benchmark_facts_index.json", "benchmarks", merged_benchmark_facts_index)

        merged_benchmark_descriptions_index = rebuild_file_index(
            merged_summary["benchmark_results"],
            artifacts_root / "benchmark_descriptions",
            "benchmark_descriptions",
            "json_path",
        )
        write_index(
            artifacts_root / "benchmark_descriptions_index.json",
            "benchmark_descriptions",
            merged_benchmark_descriptions_index,
        )

        merged_benchmark_analysis_index = rebuild_file_index(
            merged_summary["benchmark_results"],
            artifacts_root / "benchmark_analysis",
            "benchmark_analysis",
            "analysis_json_path",
        )
        write_index(
            artifacts_root / "benchmark_analysis_index.json",
            "benchmark_analysis",
            merged_benchmark_analysis_index,
        )

        render_benchmark_reports(
            skill_root,
            artifacts_root / "benchmark_analysis_index.json",
            artifacts_root / "benchmarks",
            artifacts_root / "benchmark_index.json",
            merged_summary.get("report", {}).get("language", "zh"),
        )

        suggest_registry_entries(
            artifacts_root / "summary.json",
            benchmark_registry_path,
            artifacts_root / "registry_suggestions.yaml",
            artifacts_root / "registry_suggestions.json",
        )

        dimension_index = group_dimensions(
            artifacts_root / "benchmark_analysis_index.json",
            dimension_registry_path,
            manual_overrides_dir,
            artifacts_root / "dimensions",
            artifacts_root / "dimension_index.json",
            analysis_mode=analysis_mode,
        )
        render_dimension_reports(
            skill_root,
            artifacts_root / "dimension_index.json",
            artifacts_root / "dimensions",
            report_manifest_path,
        )
        render_dimension_reports(
            skill_root,
            artifacts_root / "dimension_index.json",
            artifacts_root / "dimensions",
            report_manifest_path,
            target_language="en",
        )
        render_dimension_reports(
            skill_root,
            artifacts_root / "dimension_index.json",
            artifacts_root / "dimensions",
            report_manifest_path,
            target_language="zh",
        )
        system_card_facts = build_system_card_facts(
            artifacts_root / "summary.json",
            artifacts_root / "dimension_index.json",
            artifacts_root / "system_card.json",
            analysis_mode=analysis_mode,
        )
        render_system_card(
            skill_root,
            artifacts_root / "system_card.json",
            artifacts_root / "dimension_index.json",
            artifacts_root / "system_card.md",
        )
        render_system_card(
            skill_root,
            artifacts_root / "system_card.json",
            artifacts_root / "dimension_index.json",
            artifacts_root / "system_card_en.md",
            target_language="en",
        )
        render_system_card(
            skill_root,
            artifacts_root / "system_card.json",
            artifacts_root / "dimension_index.json",
            artifacts_root / "system_card_zh.md",
            target_language="zh",
        )

        build_report = {
            "status": "success",
            "mode": "incremental_update",
            "updated_benchmark_ids": changed_benchmark_ids,
            "run_index_path": str(artifacts_root / "run_index.json"),
            "results_path": str(artifacts_root / "results.parquet"),
            "summary_path": str(artifacts_root / "summary.json"),
            "samples_path": str(artifacts_root / "samples.json"),
            "benchmark_facts_index_path": str(artifacts_root / "benchmark_facts_index.json"),
            "benchmark_descriptions_index_path": str(artifacts_root / "benchmark_descriptions_index.json"),
            "benchmark_analysis_index_path": str(artifacts_root / "benchmark_analysis_index.json"),
            "benchmark_index_path": str(artifacts_root / "benchmark_index.json"),
            "registry_suggestions_yaml_path": str(artifacts_root / "registry_suggestions.yaml"),
            "registry_suggestions_json_path": str(artifacts_root / "registry_suggestions.json"),
            "dimension_index_path": str(artifacts_root / "dimension_index.json"),
            "system_card_json_path": str(artifacts_root / "system_card.json"),
            "system_card_path": str(artifacts_root / "system_card.md"),
            "system_card_en_path": str(artifacts_root / "system_card_en.md"),
            "system_card_zh_path": str(artifacts_root / "system_card_zh.md"),
            "warnings": unique_preserve_order(
                list(merged_run_index.get("warnings", []))
                + list(merged_summary.get("warnings", []))
                + list(merged_sample_payload.get("warnings", []))
                + list(system_card_facts.get("warnings", []))
            ),
        }
        write_json(artifacts_root / "build_report.json", build_report)
        return build_report


def load_yaml_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Artifact not found: {file_path}")
    text = file_path.read_text(encoding="utf-8")
    return json.loads(text)


def unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def upsert_records_by_key(existing: list[dict[str, Any]], new: list[dict[str, Any]], key: str) -> tuple[list[dict[str, Any]], list[str]]:
    merged = {str(item[key]): item for item in existing}
    changed_keys: list[str] = []
    for item in new:
        record_key = str(item[key])
        merged[record_key] = item
        changed_keys.append(record_key)
    ordered = sorted(merged.values(), key=lambda item: str(item.get(key)))
    return ordered, unique_preserve_order(changed_keys)


def upsert_samples_by_benchmark(existing_samples: list[dict[str, Any]], new_samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    existing_by_benchmark: dict[str, list[dict[str, Any]]] = {}
    for sample in existing_samples:
        existing_by_benchmark.setdefault(sample["benchmark_id"], []).append(sample)
    new_by_benchmark: dict[str, list[dict[str, Any]]] = {}
    for sample in new_samples:
        new_by_benchmark.setdefault(sample["benchmark_id"], []).append(sample)
    changed = list(new_by_benchmark.keys())
    existing_by_benchmark.update(new_by_benchmark)
    merged_samples: list[dict[str, Any]] = []
    for benchmark_id in sorted(existing_by_benchmark):
        merged_samples.extend(existing_by_benchmark[benchmark_id])
    return merged_samples, unique_preserve_order(changed)


def write_index(index_path: str | Path, top_key: str, entries: list[dict[str, Any]]) -> None:
    write_json(index_path, {top_key: entries})


def load_index_entries(index_path: str | Path, top_key: str) -> list[dict[str, Any]]:
    payload = load_yaml_json(index_path)
    return list(payload.get(top_key, []))


def merge_index_entries(
    existing_entries: list[dict[str, Any]],
    new_entries: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    merged = {str(entry[key]): entry for entry in existing_entries}
    for entry in new_entries:
        merged[str(entry[key])] = entry
    return sorted(merged.values(), key=lambda item: str(item.get(key)))


def rebuild_file_index(
    benchmark_results: list[dict[str, Any]],
    out_dir: str | Path,
    entry_kind: str,
    file_name: str,
) -> list[dict[str, Any]]:
    root = Path(out_dir)
    entries: list[dict[str, Any]] = []
    for benchmark in benchmark_results:
        benchmark_id = benchmark["benchmark_id"]
        path = root / f"{benchmark_id}.json"
        if not path.exists():
            continue
        entry = {
            "benchmark_id": benchmark_id,
            "display_name": benchmark.get("display_name", benchmark_id),
            "dimension_id": benchmark.get("dimension_id"),
        }
        entry[file_name] = str(path)
        entries.append(entry)
    return entries


def load_parquet_rows(path: str | Path) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    table = pq.read_table(file_path)
    columns = table.to_pydict()
    if not columns:
        return []
    keys = list(columns.keys())
    row_count = len(columns[keys[0]]) if keys else 0
    rows: list[dict[str, Any]] = []
    for row_index in range(row_count):
        row = {key: columns[key][row_index] for key in keys}
        rows.append(row)
    return rows


def merge_results_rows(existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing_rows:
        merged[(str(row.get("benchmark_id")), str(row.get("sample_id")))] = row
    for row in new_rows:
        merged[(str(row.get("benchmark_id")), str(row.get("sample_id")))] = row
    return sorted(
        merged.values(),
        key=lambda row: (str(row.get("benchmark_id")), str(row.get("sample_id"))),
    )
