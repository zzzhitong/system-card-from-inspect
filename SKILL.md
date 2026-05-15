---
name: system-card-from-inspect
description: Use this skill when generating reviewable system-card markdown artifacts from Inspect `.eval` logs or exported Inspect JSON logs.
---

# System Card from Inspect

Use this skill to build the `logs -> benchmark facts -> benchmark analysis -> dimension facts -> system card facts -> markdown` portion of a system-card workflow.

All paths in this file are relative to the **repository root / skill root**.

## Primary workflow

1. Prefer exported Inspect JSON logs when available, especially curated final-log directories.
2. Support `.eval` archives as input when `inspect_ai` is available locally.
3. Resolve Python, working directory, `.env` files, and extra environment variables from runtime config before launching the pipeline.
4. Normalize selected runs into reviewable intermediate artifacts:
   - `run_index.json`
   - `results.parquet`
   - `summary.json`
   - `samples.json`
5. Build `benchmark_descriptions/{id}.json` from local benchmark metadata and hints before any benchmark analysis is generated.
6. Build benchmark facts under `artifacts/benchmarks/` and benchmark analysis artifacts under `artifacts/benchmark_analysis/`.
7. Group benchmark analysis artifacts into system-card dimensions using `references/dimension_registry.yaml` plus optional manual overrides.
8. Build dimension fact packages under `artifacts/dimensions/`.
9. Assemble a top-level `artifacts/system_card.json`, then render:
   - `artifacts/system_card.md`
   - `artifacts/system_card_en.md`
   - `artifacts/system_card_zh.md`
10. Generate `registry_suggestions.yaml` whenever new or partially mapped benchmarks are detected.
11. Write warnings whenever benchmark mappings, metrics, coverage, or selected samples are incomplete.
12. Support incremental updates: ingest one or more newly evaluated benchmarks into an existing artifact directory without rebuilding old benchmark analysis from scratch.
13. Optionally enhance benchmark-, dimension-, and top-level system-card analysis with GPT-5.4 when API configuration is available.

## Hard requirements

- Never invent benchmark results, sample outcomes, or explanations.
- Keep every reported number traceable to log-derived artifacts.
- Treat structured artifacts as the source of truth. Markdown is the reviewable rendering layer.
- Let manual overrides change organization, emphasis, and preferred examples, but never raw scores or counts.
- Prefer the latest successful run for the same task plus task-args combination unless a report manifest explicitly selects otherwise.
- Preserve missing mappings and non-success runs as warnings instead of silently dropping them.
- Keep benchmark facts, benchmark analysis, dimension facts, and top-level system-card facts separable so incremental updates remain possible.
- Prefer the cross-platform Python launcher over OS-specific wrappers when writing reusable instructions.

## Default artifact layout

```text
artifacts/
  run_index.json
  results.parquet
  summary.json
  samples.json
  benchmark_facts_index.json
  benchmark_descriptions_index.json
  benchmark_analysis_index.json
  benchmark_index.json
  registry_suggestions.yaml
  registry_suggestions.json
  dimension_index.json
  benchmarks/
    *.json
    *.md
  benchmark_descriptions/
    *.json
  benchmark_analysis/
    *.json
  dimensions/
    *.json
    *.md
    *_en.md
    *_zh.md
  system_card.json
  system_card.md
  system_card_en.md
  system_card_zh.md
  build_report.json
```

## Key resources

- `references/benchmark_registry.yaml`
  Map task plus task-args combinations to stable benchmark identifiers, primary metrics, default dimensions, and sample-selection rules.
- `references/dimension_registry.yaml`
  Define dimension metadata such as title, ordering, and default descriptions. The primary grouping mechanism is each benchmark's `dimension_id`; this registry is not the source of truth for benchmark membership.
- `references/report_manifest.example.yaml`
  Show how to pin a report to specific runs, language, and metadata.
- `references/manual_overrides/`
  Store optional per-dimension overrides for benchmark inclusion, exclusion, and wording emphasis.
- `references/benchmark_hints.yaml`
  Store local benchmark-level semantic hints, evaluation questions, and deployment context that feed the description and analysis layers.
- `references/runtime_config.json`
  Shared runtime defaults for interpreter candidates, working directory, `.env` files, and extra environment variables.
- `references/runtime_config.local.json`
  Optional uncommitted override file for local machine paths or extra environment variables. If present, prefer it over the shared default config.
- `references/runtime_config.local.example.json`
  Example local override config.
- `scripts/run_with_runtime.py`
  Preferred cross-platform launcher. It resolves the runtime config, then starts `run_pipeline.py` with the configured interpreter and environment.
- `scripts/run_with_runtime.ps1`
  Windows convenience wrapper that delegates to `run_with_runtime.py`.
- `scripts/run_pipeline.py`
  Entry point for the end-to-end logs-to-markdown pipeline.

## Recommended runtime

Use the runtime config and the cross-platform Python launcher when possible:

```bash
python scripts/run_with_runtime.py --print-runtime
```

If your machine exposes Python as `python3`, use:

```bash
python3 scripts/run_with_runtime.py --print-runtime
```

By default, the launcher searches for:

1. `references/runtime_config.local.json`
2. `references/runtime_config.json`

The shared default config resolves Python from these candidates, in order:

```text
inspect_evals/.venv/Scripts/python.exe
inspect_evals/.venv/bin/python
.venv/Scripts/python.exe
.venv/bin/python
python
python3
```

## Runtime config schema

All relative paths are resolved from the repo root.

```json
{
  "runtime": {
    "python_candidates": [
      "inspect_evals/.venv/Scripts/python.exe",
      "inspect_evals/.venv/bin/python",
      ".venv/Scripts/python.exe",
      ".venv/bin/python",
      "python",
      "python3"
    ],
    "working_directory": ".",
    "env_files": [
      ".env",
      "inspect_evals/.env"
    ],
    "env": {
      "SYSTEM_CARD_ANALYSIS_PROVIDER": "azure",
      "SYSTEM_CARD_ANALYSIS_MODEL": "gpt-5.4"
    }
  }
}
```

If you need to pin an exact interpreter for one machine, add `python_path` in `runtime_config.local.json`. When present, it takes precedence over `python_candidates`.

## Recommended commands

Run the full pipeline on a directory of JSON logs:

```bash
python scripts/run_with_runtime.py run \
  --input-dir path/to/logs_json_final \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --analysis-mode auto \
  --artifacts-dir system_card_artifacts
```

Run the full pipeline on a single `.eval` log:

```bash
python scripts/run_with_runtime.py run \
  --input-path path/to/benchmark.eval \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --analysis-mode auto \
  --artifacts-dir system_card_artifacts
```

Render from an explicit report manifest:

```bash
python scripts/run_with_runtime.py run \
  --input-dir path/to/logs_json_final \
  --report-manifest references/report_manifest.example.yaml \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --manual-overrides-dir references/manual_overrides \
  --analysis-mode hybrid \
  --artifacts-dir system_card_artifacts
```

Incrementally add a newly evaluated benchmark into an existing artifact directory:

```bash
python scripts/run_with_runtime.py update-system-card \
  --existing-artifacts-dir existing_system_card_artifacts \
  --input-path path/to/new_benchmark.eval \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --analysis-mode auto
```

Generate registration suggestions from an existing summary:

```bash
python scripts/run_with_runtime.py suggest-registry \
  --summary system_card_artifacts/summary.json \
  --benchmark-registry references/benchmark_registry.yaml \
  --yaml-out system_card_artifacts/registry_suggestions.yaml \
  --json-out system_card_artifacts/registry_suggestions.json
```

## Analysis modes

- `rule`
  Use only the built-in rule-based analyzers for benchmark, dimension, and top-level system-card synthesis.
- `llm`
  Attempt to use configured LLM analysis for benchmark, dimension, and top-level synthesis. The current implementation falls back to rule-based analysis with warnings if configuration is unavailable.
- `hybrid`
  Start from rule-based facts, then let GPT-5.4 rewrite benchmark descriptions, benchmark findings, dimension synthesis, and top-level release synthesis.
- `auto`
  Use `hybrid` when LLM configuration is available, otherwise `rule`.

## LLM analysis configuration

The LLM analysis and translation stages look for configuration in environment variables first, then in `.env` files under the repo root and `inspect_evals/.env`.

Primary variables:

- `SYSTEM_CARD_ANALYSIS_PROVIDER`
- `SYSTEM_CARD_ANALYSIS_MODEL`
- `SYSTEM_CARD_ANALYSIS_API_KEY`
- `SYSTEM_CARD_ANALYSIS_BASE_URL`
- `SYSTEM_CARD_ANALYSIS_API_VERSION`
- `SYSTEM_CARD_ANALYSIS_TEMPERATURE`
- `SYSTEM_CARD_ANALYSIS_MAX_TOKENS`

Azure/OpenAI fallbacks:

- `AZUREAI_OPENAI_API_KEY`
- `AZUREAI_OPENAI_BASE_URL`
- `AZUREAI_OPENAI_API_VERSION`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`

## Notes for future phases

- This skill currently stops at markdown generation.
- LaTeX, PDF, and Overleaf steps are intentionally out of scope for now.
- Local benchmark descriptions are implemented; web enrichment for benchmark descriptions is still a future extension.

## TODO

### Multi-model comparison on shared benchmarks

- The current pipeline is still optimized for single-model system cards. In particular, run selection and benchmark aggregation effectively collapse results by `task_name + task_args`, so multiple models evaluated on the same benchmark are not yet preserved as first-class parallel results.
- Add a dedicated comparison mode such as `compare-models` instead of overloading the default single-model `run` flow.
- Separate benchmark identity from run identity:
  - `benchmark_key = task_name + task_args`
  - `run_key = benchmark_key + model_name`
- Introduce comparison artifacts such as:
  - `benchmark_comparisons/{benchmark_id}.json`
  - `dimension_comparisons/{dimension_id}.json`
  - `comparison_report.json`
  - `comparison_report_en.md`
  - `comparison_report_zh.md`
- The comparison flow should support:
  - benchmark-level model deltas on the same eval
  - dimension-level cross-model synthesis
  - top-level conclusions about which model is stronger, weaker, or less stable on each risk/capability area
- Preserve current single-model behavior as the default path so existing system-card generation and incremental updates do not break.
