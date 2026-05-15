from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import (
    aggregate_summary,
    build_benchmark_descriptions,
    build_benchmark_analysis_artifacts,
    build_system_card_facts,
    group_dimensions,
    normalize_logs,
    render_benchmark_reports,
    render_dimension_reports,
    render_system_card,
    run_full_pipeline,
    select_representative_samples,
    suggest_registry_entries,
    update_system_card_incremental,
    write_benchmark_facts,
)
from .registry_sync import sync_benchmark_registry_from_inspect_evals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate benchmark-, dimension-, and system-card markdown from Inspect eval logs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--benchmark-registry", required=True)
    common.add_argument("--artifacts-dir", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--input-dir")
    run_parser.add_argument("--input-path", action="append", dest="input_paths")
    run_parser.add_argument("--benchmark-registry", required=True)
    run_parser.add_argument("--dimension-registry", required=True)
    run_parser.add_argument("--artifacts-dir", required=True)
    run_parser.add_argument("--report-manifest")
    run_parser.add_argument("--manual-overrides-dir")
    run_parser.add_argument("--analysis-mode", default="auto")

    update_parser = subparsers.add_parser("update-system-card")
    update_parser.add_argument("--existing-artifacts-dir", required=True)
    update_parser.add_argument("--input-dir")
    update_parser.add_argument("--input-path", action="append", dest="input_paths")
    update_parser.add_argument("--benchmark-registry", required=True)
    update_parser.add_argument("--dimension-registry", required=True)
    update_parser.add_argument("--report-manifest")
    update_parser.add_argument("--manual-overrides-dir")
    update_parser.add_argument("--analysis-mode", default="auto")

    normalize_parser = subparsers.add_parser("normalize")
    normalize_parser.add_argument("--input-dir")
    normalize_parser.add_argument("--input-path", action="append", dest="input_paths")
    normalize_parser.add_argument("--benchmark-registry", required=True)
    normalize_parser.add_argument("--artifacts-dir", required=True)
    normalize_parser.add_argument("--report-manifest")

    aggregate_parser = subparsers.add_parser("aggregate")
    aggregate_parser.add_argument("--run-index", required=True)
    aggregate_parser.add_argument("--benchmark-registry", required=True)
    aggregate_parser.add_argument("--out", required=True)
    aggregate_parser.add_argument("--report-manifest")

    samples_parser = subparsers.add_parser("select-samples")
    samples_parser.add_argument("--run-index", required=True)
    samples_parser.add_argument("--benchmark-registry", required=True)
    samples_parser.add_argument("--out", required=True)

    benchmark_facts_parser = subparsers.add_parser("write-benchmark-facts")
    benchmark_facts_parser.add_argument("--summary", required=True)
    benchmark_facts_parser.add_argument("--out-dir", required=True)
    benchmark_facts_parser.add_argument("--index-out", required=True)

    describe_benchmarks_parser = subparsers.add_parser("describe-benchmarks")
    describe_benchmarks_parser.add_argument("--summary", required=True)
    describe_benchmarks_parser.add_argument("--benchmark-registry", required=True)
    describe_benchmarks_parser.add_argument("--out-dir", required=True)
    describe_benchmarks_parser.add_argument("--index-out", required=True)

    analyze_benchmarks_parser = subparsers.add_parser("analyze-benchmarks")
    analyze_benchmarks_parser.add_argument("--summary", required=True)
    analyze_benchmarks_parser.add_argument("--samples", required=True)
    analyze_benchmarks_parser.add_argument("--descriptions-index", required=True)
    analyze_benchmarks_parser.add_argument("--out-dir", required=True)
    analyze_benchmarks_parser.add_argument("--index-out", required=True)
    analyze_benchmarks_parser.add_argument("--analysis-mode", default="auto")

    render_benchmarks_parser = subparsers.add_parser("render-benchmarks")
    render_benchmarks_parser.add_argument("--benchmark-analysis-index", required=True)
    render_benchmarks_parser.add_argument("--out-dir", required=True)
    render_benchmarks_parser.add_argument("--index-out", required=True)
    render_benchmarks_parser.add_argument("--language", default="zh")

    group_parser = subparsers.add_parser("group-dimensions")
    group_parser.add_argument("--benchmark-analysis-index", required=True)
    group_parser.add_argument("--dimension-registry", required=True)
    group_parser.add_argument("--out-dir", required=True)
    group_parser.add_argument("--index-out", required=True)
    group_parser.add_argument("--manual-overrides-dir")
    group_parser.add_argument("--analysis-mode", default="auto")

    render_dimensions_parser = subparsers.add_parser("render-dimensions")
    render_dimensions_parser.add_argument("--dimension-index", required=True)
    render_dimensions_parser.add_argument("--out-dir", required=True)
    render_dimensions_parser.add_argument("--report-manifest")

    render_system_parser = subparsers.add_parser("render-system-card")
    render_system_parser.add_argument("--system-card-json", required=True)
    render_system_parser.add_argument("--dimension-index", required=True)
    render_system_parser.add_argument("--out", required=True)

    build_system_parser = subparsers.add_parser("build-system-card-facts")
    build_system_parser.add_argument("--summary", required=True)
    build_system_parser.add_argument("--dimension-index", required=True)
    build_system_parser.add_argument("--out", required=True)
    build_system_parser.add_argument("--analysis-mode", default="auto")

    suggest_registry_parser = subparsers.add_parser("suggest-registry")
    suggest_registry_parser.add_argument("--summary", required=True)
    suggest_registry_parser.add_argument("--benchmark-registry", required=True)
    suggest_registry_parser.add_argument("--yaml-out", required=True)
    suggest_registry_parser.add_argument("--json-out")

    sync_registry_parser = subparsers.add_parser("sync-benchmark-registry")
    sync_registry_parser.add_argument("--inspect-root", required=True)
    sync_registry_parser.add_argument("--benchmark-registry", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    skill_root = Path(__file__).resolve().parents[2]

    if args.command == "run":
        input_target = resolve_input_target(args.input_dir, getattr(args, "input_paths", None), parser)
        run_full_pipeline(
            skill_root=skill_root,
            input_target=input_target,
            benchmark_registry_path=args.benchmark_registry,
            dimension_registry_path=args.dimension_registry,
            artifacts_dir=args.artifacts_dir,
            report_manifest_path=args.report_manifest,
            manual_overrides_dir=args.manual_overrides_dir,
            analysis_mode=args.analysis_mode,
        )
        return 0

    if args.command == "update-system-card":
        input_target = resolve_input_target(args.input_dir, getattr(args, "input_paths", None), parser)
        update_system_card_incremental(
            skill_root=skill_root,
            existing_artifacts_dir=args.existing_artifacts_dir,
            input_target=input_target,
            benchmark_registry_path=args.benchmark_registry,
            dimension_registry_path=args.dimension_registry,
            report_manifest_path=args.report_manifest,
            manual_overrides_dir=args.manual_overrides_dir,
            analysis_mode=args.analysis_mode,
        )
        return 0

    if args.command == "normalize":
        input_target = resolve_input_target(args.input_dir, getattr(args, "input_paths", None), parser)
        normalize_logs(input_target, args.benchmark_registry, args.report_manifest, args.artifacts_dir)
        return 0

    if args.command == "aggregate":
        aggregate_summary(args.run_index, args.benchmark_registry, args.report_manifest, args.out)
        return 0

    if args.command == "select-samples":
        select_representative_samples(args.run_index, args.benchmark_registry, args.out)
        return 0

    if args.command == "write-benchmark-facts":
        write_benchmark_facts(args.summary, args.out_dir, args.index_out)
        return 0

    if args.command == "describe-benchmarks":
        build_benchmark_descriptions(skill_root, args.summary, args.benchmark_registry, args.out_dir, args.index_out)
        return 0

    if args.command == "analyze-benchmarks":
        build_benchmark_analysis_artifacts(
            skill_root,
            args.summary,
            args.samples,
            args.descriptions_index,
            args.out_dir,
            args.index_out,
            args.analysis_mode,
        )
        return 0

    if args.command == "render-benchmarks":
        render_benchmark_reports(
            skill_root,
            args.benchmark_analysis_index,
            args.out_dir,
            args.index_out,
            args.language,
        )
        return 0

    if args.command == "group-dimensions":
        group_dimensions(
            args.benchmark_analysis_index,
            args.dimension_registry,
            args.manual_overrides_dir,
            args.out_dir,
            args.index_out,
            analysis_mode=args.analysis_mode,
        )
        return 0

    if args.command == "render-dimensions":
        render_dimension_reports(skill_root, args.dimension_index, args.out_dir, args.report_manifest)
        return 0

    if args.command == "render-system-card":
        render_system_card(skill_root, args.system_card_json, args.dimension_index, args.out)
        return 0

    if args.command == "build-system-card-facts":
        build_system_card_facts(args.summary, args.dimension_index, args.out, analysis_mode=args.analysis_mode)
        return 0

    if args.command == "suggest-registry":
        suggest_registry_entries(args.summary, args.benchmark_registry, args.yaml_out, args.json_out)
        return 0

    if args.command == "sync-benchmark-registry":
        sync_benchmark_registry_from_inspect_evals(args.inspect_root, args.benchmark_registry)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


def resolve_input_target(
    input_dir: str | None,
    input_paths: list[str] | None,
    parser: argparse.ArgumentParser,
) -> str | list[str]:
    paths = [path for path in (input_paths or []) if path]
    if input_dir:
        if paths:
            paths.insert(0, input_dir)
            return paths
        return input_dir
    if paths:
        return paths
    parser.error("one of --input-dir or --input-path must be provided")
    raise AssertionError("unreachable")
