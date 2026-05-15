from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_dotenv_file


DEFAULT_PYTHON_CANDIDATES = [
    "inspect_evals/.venv/Scripts/python.exe",
    "inspect_evals/.venv/bin/python",
    ".venv/Scripts/python.exe",
    ".venv/bin/python",
    "python",
    "python3",
]


@dataclass
class ResolvedRuntime:
    config_path: Path | None
    python_path: str
    python_source: str
    working_directory: Path
    env_files: list[Path]
    env: dict[str, str]


def repo_root_from_skill_root(skill_root: str | Path) -> Path:
    return Path(skill_root).resolve().parents[2]


def resolve_repo_path(value: str | Path, repo_root: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path(repo_root).resolve() / path).resolve()


def resolve_runtime_config_path(skill_root: str | Path, runtime_config_path: str | Path | None) -> Path | None:
    skill_root_path = Path(skill_root).resolve()
    repo_root = repo_root_from_skill_root(skill_root_path)
    candidates: list[Path] = []
    if runtime_config_path is not None:
        candidates.append(resolve_repo_path(runtime_config_path, repo_root))
    else:
        candidates.append(skill_root_path / "references" / "runtime_config.local.json")
        candidates.append(skill_root_path / "references" / "runtime_config.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_runtime_config(skill_root: str | Path, runtime_config_path: str | Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    config_path = resolve_runtime_config_path(skill_root, runtime_config_path)
    if config_path is None:
        return None, {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    runtime = payload.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError(f"Expected `runtime` mapping in {config_path}")
    return config_path, runtime


def _candidate_is_path(candidate: str) -> bool:
    return any(sep in candidate for sep in ("/", "\\"))


def _resolve_python_candidate(candidate: str, repo_root: Path) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(candidate))
    if Path(expanded).is_absolute() or _candidate_is_path(expanded):
        path = resolve_repo_path(expanded, repo_root)
        if path.exists():
            return str(path)
        return None
    return shutil.which(expanded)


def resolve_python_runtime(runtime: dict[str, Any], repo_root: str | Path) -> tuple[str, str]:
    repo_root_path = Path(repo_root).resolve()

    python_path = runtime.get("python_path")
    if python_path:
        resolved = _resolve_python_candidate(str(python_path), repo_root_path)
        if resolved is None:
            raise FileNotFoundError(f"Configured python_path not found: {python_path}")
        return resolved, "runtime.python_path"

    seen: set[str] = set()
    candidates: list[str] = []
    for raw in runtime.get("python_candidates", []) or []:
        candidate = str(raw)
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    for candidate in DEFAULT_PYTHON_CANDIDATES:
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    attempted: list[str] = []
    for candidate in candidates:
        attempted.append(candidate)
        resolved = _resolve_python_candidate(candidate, repo_root_path)
        if resolved is not None:
            return resolved, f"python_candidates[{candidate}]"

    attempted_text = ", ".join(attempted) if attempted else "<none>"
    raise FileNotFoundError(
        "No Python runtime found. Set `runtime.python_path` or `runtime.python_candidates` "
        f"in runtime_config.json. Attempted: {attempted_text}"
    )


def resolve_working_directory(runtime: dict[str, Any], repo_root: str | Path) -> Path:
    working_directory = runtime.get("working_directory", ".")
    resolved = resolve_repo_path(str(working_directory), repo_root)
    if not resolved.exists():
        raise FileNotFoundError(f"Configured working_directory does not exist: {resolved}")
    return resolved


def resolve_env_files(runtime: dict[str, Any], repo_root: str | Path) -> list[Path]:
    files: list[Path] = []
    for value in runtime.get("env_files", []) or []:
        files.append(resolve_repo_path(str(value), repo_root))
    return files


def resolve_runtime_env(runtime: dict[str, Any], env_files: list[Path]) -> dict[str, str]:
    env: dict[str, str] = {}
    for env_file in env_files:
        env.update(load_dotenv_file(env_file))
    explicit_env = runtime.get("env", {}) or {}
    if not isinstance(explicit_env, dict):
        raise ValueError("Expected `runtime.env` to be a mapping")
    for key, value in explicit_env.items():
        env[str(key)] = str(value)
    return env


def build_resolved_runtime(skill_root: str | Path, runtime_config_path: str | Path | None = None) -> ResolvedRuntime:
    skill_root_path = Path(skill_root).resolve()
    repo_root = repo_root_from_skill_root(skill_root_path)
    config_path, runtime = load_runtime_config(skill_root_path, runtime_config_path)
    python_path, python_source = resolve_python_runtime(runtime, repo_root)
    working_directory = resolve_working_directory(runtime, repo_root)
    env_files = resolve_env_files(runtime, repo_root)
    env = resolve_runtime_env(runtime, env_files)
    return ResolvedRuntime(
        config_path=config_path,
        python_path=python_path,
        python_source=python_source,
        working_directory=working_directory,
        env_files=env_files,
        env=env,
    )
