from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"YAML file not found: {file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in YAML file: {file_path}")
    return data


def load_manual_overrides(directory: str | Path | None) -> dict[str, dict[str, Any]]:
    if directory is None:
        return {}
    root = Path(directory)
    if not root.exists():
        return {}
    overrides: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.yaml")):
        data = load_yaml(path)
        dimension_id = data.get("dimension_id") or path.stem
        overrides[str(dimension_id)] = data
    return overrides


def normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): normalize_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(v) for v in value]
    return value


def normalize_task_args(task_args: dict[str, Any] | None) -> dict[str, Any]:
    return normalize_value(task_args or {})


def task_args_to_json(task_args: dict[str, Any] | None) -> str:
    return json.dumps(normalize_task_args(task_args), ensure_ascii=False, sort_keys=True)


def task_args_match(actual: dict[str, Any] | None, expected: dict[str, Any] | None) -> bool:
    actual_norm = normalize_task_args(actual)
    expected_norm = normalize_task_args(expected)
    for key, expected_value in expected_norm.items():
        if actual_norm.get(key) != expected_value:
            return False
    return True


def slugify(value: str) -> str:
    chars: list[str] = []
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
        else:
            chars.append("-")
    text = "".join(chars)
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def load_dotenv_file(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    env: dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_env_from_candidates(candidates: list[str | Path]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for candidate in candidates:
        merged.update(load_dotenv_file(candidate))
    return merged


def env_value(name: str, dotenv_values: dict[str, str], default: str | None = None) -> str | None:
    return os.environ.get(name) or dotenv_values.get(name) or default
