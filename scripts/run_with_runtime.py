#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from system_card_from_inspect.runtime import build_resolved_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve runtime config, environment files, and Python interpreter before launching run_pipeline.py."
    )
    parser.add_argument("--runtime-config", help="Optional runtime config path, resolved from the repo root if relative.")
    parser.add_argument("--print-runtime", action="store_true", help="Print resolved runtime information before launching the pipeline.")
    parser.add_argument("pipeline_args", nargs=argparse.REMAINDER, help="Arguments forwarded to run_pipeline.py")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    skill_root = SCRIPT_DIR.parent
    resolved = build_resolved_runtime(skill_root, args.runtime_config)
    pipeline_args = list(args.pipeline_args)
    if pipeline_args[:1] == ["--"]:
        pipeline_args = pipeline_args[1:]

    if args.print_runtime:
        print(f"runtime_config_path={resolved.config_path or '<none>'}")
        print(f"python_path={resolved.python_path}")
        print(f"python_source={resolved.python_source}")
        print(f"working_directory={resolved.working_directory}")
        if resolved.env_files:
            print("env_files=" + ";".join(str(path) for path in resolved.env_files))
        else:
            print("env_files=<none>")
        if resolved.env:
            print("env_keys=" + ",".join(sorted(resolved.env.keys())))
        else:
            print("env_keys=<none>")
        if not pipeline_args:
            return 0

    if not pipeline_args:
        parser.error("pipeline command required unless --print-runtime is used without further args")

    pipeline_path = SCRIPT_DIR / "run_pipeline.py"
    env = os.environ.copy()
    env.update(resolved.env)
    completed = subprocess.run(
        [resolved.python_path, str(pipeline_path), *pipeline_args],
        cwd=str(resolved.working_directory),
        env=env,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
