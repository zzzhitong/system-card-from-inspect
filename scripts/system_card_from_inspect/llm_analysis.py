from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openai import AzureOpenAI, OpenAI

from .config import env_value, load_env_from_candidates


def resolve_llm_analysis_config(skill_root: Path) -> dict[str, Any]:
    repo_root = skill_root.parents[2]
    dotenv_values = load_env_from_candidates(
        [
            repo_root / ".env",
            repo_root / "inspect_evals" / ".env",
        ]
    )

    provider = env_value("SYSTEM_CARD_ANALYSIS_PROVIDER", dotenv_values, "azure")
    model = env_value("SYSTEM_CARD_ANALYSIS_MODEL", dotenv_values, "gpt-5.4")
    temperature = float(env_value("SYSTEM_CARD_ANALYSIS_TEMPERATURE", dotenv_values, "0") or "0")
    max_tokens = int(env_value("SYSTEM_CARD_ANALYSIS_MAX_TOKENS", dotenv_values, "1800") or "1800")

    if provider == "azure":
        api_key = env_value("SYSTEM_CARD_ANALYSIS_API_KEY", dotenv_values) or env_value("AZUREAI_OPENAI_API_KEY", dotenv_values)
        base_url = env_value("SYSTEM_CARD_ANALYSIS_BASE_URL", dotenv_values) or env_value("AZUREAI_OPENAI_BASE_URL", dotenv_values)
        api_version = env_value("SYSTEM_CARD_ANALYSIS_API_VERSION", dotenv_values) or env_value("AZUREAI_OPENAI_API_VERSION", dotenv_values, "2025-03-01-preview")
        return {
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "api_version": api_version,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    api_key = env_value("SYSTEM_CARD_ANALYSIS_API_KEY", dotenv_values) or env_value("OPENAI_API_KEY", dotenv_values)
    base_url = env_value("SYSTEM_CARD_ANALYSIS_BASE_URL", dotenv_values) or env_value("OPENAI_BASE_URL", dotenv_values)
    return {
        "provider": "openai",
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def llm_analysis_available(config: dict[str, Any]) -> tuple[bool, str | None]:
    if not config.get("api_key"):
        return False, "No API key configured for benchmark analysis."
    if not config.get("base_url"):
        return False, "No base URL configured for benchmark analysis."
    if not config.get("model"):
        return False, "No model configured for benchmark analysis."
    return True, None


def build_benchmark_analysis_prompt(benchmark: dict[str, Any], description: dict[str, Any]) -> tuple[str, str]:
    system_prompt = (
        "You are writing benchmark-level system card analysis for a model evaluation pipeline.\n"
        "You must only use the evidence provided in the prompt. Do not invent scores, benchmark semantics, or mitigation claims.\n"
        "Return strict JSON with these keys: description_summary, key_findings, implications, confidence_notes.\n"
        "key_findings, implications, confidence_notes must each be arrays of short strings.\n"
        "description_summary must be a short paragraph.\n"
    )
    user_payload = {
        "benchmark_id": benchmark.get("benchmark_id"),
        "display_name": benchmark.get("display_name"),
        "dimension_id": benchmark.get("dimension_id"),
        "summary_focus": benchmark.get("summary_focus"),
        "coverage": benchmark.get("coverage"),
        "primary_metrics": benchmark.get("primary_metrics"),
        "score_distribution": benchmark.get("score_distribution"),
        "slice_analysis": benchmark.get("slice_analysis"),
        "failure_patterns": benchmark.get("failure_patterns"),
        "selected_samples": [
            {
                "sample_id": sample.get("sample_id"),
                "selection_reason": sample.get("selection_reason"),
                "input_excerpt": sample.get("input_excerpt"),
                "output_excerpt": sample.get("output_excerpt"),
                "primary_score_explanation_excerpt": sample.get("primary_score_explanation_excerpt"),
                "sample_highlights": sample.get("sample_highlights"),
            }
            for sample in benchmark.get("selected_samples", [])[:4]
        ],
        "benchmark_description": {
            "resolved_description": description.get("resolved_description"),
            "evaluation_questions": description.get("evaluation_questions"),
            "assessed_risk_or_capability_scope": description.get("assessed_risk_or_capability_scope"),
            "source_quality": description.get("source_quality"),
            "warnings": description.get("warnings"),
        },
    }
    user_prompt = (
        "Analyze the following benchmark evidence and produce benchmark-level system card analysis.\n"
        "Keep findings concrete and deployment-relevant. Mention partial coverage when it matters.\n"
        "Do not repeat raw tables; synthesize what matters most.\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )
    return system_prompt, user_prompt


def call_benchmark_analysis_llm(config: dict[str, Any], benchmark: dict[str, Any], description: dict[str, Any]) -> dict[str, Any]:
    system_prompt, user_prompt = build_benchmark_analysis_prompt(benchmark, description)
    return call_llm_json(config, system_prompt, user_prompt)


def build_dimension_analysis_prompt(dimension: dict[str, Any]) -> tuple[str, str]:
    system_prompt = (
        "You are writing dimension-level system card analysis.\n"
        "Use only the provided benchmark-analysis evidence.\n"
        "Return strict JSON with keys: key_findings, cross_benchmark_consensus, cross_benchmark_tensions, risk_interpretation, implications.\n"
        "Each value must be an array of short strings.\n"
        "Do not invent scores or benchmarks.\n"
    )
    user_payload = {
        "dimension_id": dimension.get("dimension_id"),
        "title": dimension.get("title"),
        "description": dimension.get("description"),
        "benchmarks": [
            {
                "benchmark_id": benchmark.get("benchmark_id"),
                "display_name": benchmark.get("display_name"),
                "coverage": benchmark.get("coverage"),
                "primary_metrics": benchmark.get("primary_metrics"),
                "description_summary": benchmark.get("description_summary"),
                "key_findings": benchmark.get("key_findings"),
                "implications": benchmark.get("implications"),
                "failure_patterns": benchmark.get("failure_patterns"),
                "slice_analysis": benchmark.get("slice_analysis"),
                "confidence_notes": benchmark.get("confidence_notes"),
            }
            for benchmark in dimension.get("benchmarks", [])
        ],
    }
    user_prompt = (
        "Synthesize the following benchmark analyses into a dimension-level system card section.\n"
        "If only one benchmark is present, make that explicit rather than pretending there is consensus.\n"
        "Keep output concise, evidence-based, and deployment-relevant.\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )
    return system_prompt, user_prompt


def call_dimension_analysis_llm(config: dict[str, Any], dimension: dict[str, Any]) -> dict[str, Any]:
    system_prompt, user_prompt = build_dimension_analysis_prompt(dimension)
    return call_llm_json(config, system_prompt, user_prompt)


def build_system_card_analysis_prompt(system_card: dict[str, Any]) -> tuple[str, str]:
    system_prompt = (
        "You are writing top-level system card synthesis.\n"
        "Use only the provided dimension and benchmark evidence.\n"
        "Return strict JSON with keys: key_findings, release_considerations, recommended_mitigations.\n"
        "Each value must be an array of short strings.\n"
        "Keep the findings high-signal and publication-oriented.\n"
    )
    user_payload = {
        "report": system_card.get("report"),
        "benchmark_rows": system_card.get("benchmark_rows"),
        "dimension_summaries": system_card.get("dimension_summaries"),
        "overall_risk_profile": system_card.get("overall_risk_profile"),
        "coverage_and_caveats": system_card.get("coverage_and_caveats"),
        "warnings": system_card.get("warnings"),
    }
    user_prompt = (
        "Synthesize the following system-card fact package into top-level findings and release considerations.\n"
        "Do not repeat tables. Focus on what a reader should take away.\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )
    return system_prompt, user_prompt


def call_system_card_analysis_llm(config: dict[str, Any], system_card: dict[str, Any]) -> dict[str, Any]:
    system_prompt, user_prompt = build_system_card_analysis_prompt(system_card)
    return call_llm_json(config, system_prompt, user_prompt)


def build_translation_prompt(layer_name: str, payload: dict[str, Any], target_language: str) -> tuple[str, str]:
    system_prompt = (
        "You translate system-card analysis artifacts.\n"
        "Translate only human-facing prose into the requested target language.\n"
        "Preserve JSON structure, arrays, ids, metric names, benchmark ids, sample ids, file paths, and numeric values.\n"
        "Do not summarize or omit fields. Return strict JSON with the same structure as the input payload.\n"
    )
    user_prompt = (
        f"Translate the following {layer_name} payload into {target_language}. "
        "Keep technical identifiers and numeric values unchanged.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return system_prompt, user_prompt


def call_translation_llm(config: dict[str, Any], layer_name: str, payload: dict[str, Any], target_language: str) -> dict[str, Any]:
    system_prompt, user_prompt = build_translation_prompt(layer_name, payload, target_language)
    return call_llm_json(config, system_prompt, user_prompt)


def call_llm_json(config: dict[str, Any], system_prompt: str, user_prompt: str) -> dict[str, Any]:
    if config["provider"] == "azure":
        client = AzureOpenAI(
            api_key=config["api_key"],
            azure_endpoint=config["base_url"],
            api_version=config["api_version"],
        )
    else:
        client = OpenAI(
            api_key=config["api_key"],
            base_url=config["base_url"],
        )

    response = client.chat.completions.create(
        model=config["model"],
        temperature=config["temperature"],
        max_completion_tokens=config["max_tokens"],
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM analysis did not return a JSON object.")
    return parsed
