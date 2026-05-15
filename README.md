# system-card-from-inspect

`system-card-from-inspect` 是一个把 Inspect eval 日志自动整理成 system card 产物的 Skill。

它的目标不是直接生成 PDF，而是先把日志沉淀成一套**可追溯、可审阅、可增量更新**的结构化 facts 与 markdown，方便后续人工审阅、LaTeX 排版或正式发布。

## 功能概览

- 读取 Inspect `.eval` 日志或导出的 `.json` 日志
- 生成 benchmark / dimension / top-level 的结构化 facts
- 生成可审阅的 markdown 报告
- 支持 benchmark、dimension、top-level 三层 GPT-5.4 分析增强
- 支持中英文最终输出
- 支持在已有 artifact 基础上增量加入新 benchmark
- 支持对未正式注册的 benchmark 生成 registry suggestions

## 当前边界

- 不直接生成 LaTeX / PDF
- 不做 Overleaf 同步
- 不自动联网补 benchmark 说明
- 目前仍以**单模型 system card**为主，不支持多模型同 benchmark 的自动对比报告

## 安装

### 方式 1：作为独立仓库使用

```bash
git clone https://github.com/zzzhitong/system-card-from-inspect.git
cd system-card-from-inspect
```

### 方式 2：作为 Codex Skill 安装

把**整个仓库根目录**放到你的 Codex skills 目录中，并保持目录名为 `system-card-from-inspect`。安装后命令仍应从这个仓库根目录执行。

## 依赖安装

建议先创建虚拟环境，再安装依赖：

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 运行入口

推荐使用跨平台 launcher：

```bash
python scripts/run_with_runtime.py --print-runtime
```

如果你的环境把 Python 暴露成 `python3`：

```bash
python3 scripts/run_with_runtime.py --print-runtime
```

这条命令会打印：

- 当前使用的 runtime config
- 解析到的 Python 路径
- Python 来源
- 工作目录
- `.env` 文件来源
- 注入的环境变量 key

## Runtime config

launcher 会按顺序查找：

1. `references/runtime_config.local.json`
2. `references/runtime_config.json`

默认配置支持这些字段：

- `python_path`
  - 显式指定解释器路径，可选
- `python_candidates`
  - 候选解释器列表
- `working_directory`
  - 运行主流程时使用的工作目录
- `env_files`
  - 需要加载的 `.env` 文件列表
- `env`
  - 额外注入的环境变量

共享默认配置示例：

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
    "env": {}
  }
}
```

建议：

- 共享仓库里保留 `runtime_config.json`
- 本机差异配置写到 `runtime_config.local.json`
- 密钥优先放 `.env`，不要直接写入共享配置

## 环境变量

如果要启用 GPT-5.4 分析增强，Skill 会优先读取：

- 仓库根目录 `.env`
- `inspect_evals/.env`

你也可以参考 `.env.example`：

```bash
cp .env.example .env
```

主要环境变量：

- `SYSTEM_CARD_ANALYSIS_PROVIDER`
- `SYSTEM_CARD_ANALYSIS_MODEL`
- `SYSTEM_CARD_ANALYSIS_API_KEY`
- `SYSTEM_CARD_ANALYSIS_BASE_URL`
- `SYSTEM_CARD_ANALYSIS_API_VERSION`
- `SYSTEM_CARD_ANALYSIS_TEMPERATURE`
- `SYSTEM_CARD_ANALYSIS_MAX_TOKENS`

也支持 fallback 到：

- `AZUREAI_OPENAI_API_KEY`
- `AZUREAI_OPENAI_BASE_URL`
- `AZUREAI_OPENAI_API_VERSION`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`

## 最小样例

仓库包含一个最小可运行的示例日志：

- `examples/minimal_inspect_log.json`

你可以直接用它做 smoke test：

```bash
python scripts/run_with_runtime.py run \
  --input-path examples/minimal_inspect_log.json \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --analysis-mode rule \
  --artifacts-dir tmp_artifacts
```

## 常用命令

### 1. 批量生成一套 system card

```bash
python scripts/run_with_runtime.py run \
  --input-dir path/to/logs_json_final \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --analysis-mode auto \
  --artifacts-dir system_card_artifacts
```

### 2. 从单个 `.eval` 生成

```bash
python scripts/run_with_runtime.py run \
  --input-path path/to/benchmark.eval \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --analysis-mode auto \
  --artifacts-dir system_card_artifacts
```

### 3. 显式指定 manifest

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

### 4. 增量加入一个新 benchmark

```bash
python scripts/run_with_runtime.py update-system-card \
  --existing-artifacts-dir existing_system_card_artifacts \
  --input-path path/to/new_benchmark.eval \
  --benchmark-registry references/benchmark_registry.yaml \
  --dimension-registry references/dimension_registry.yaml \
  --analysis-mode auto
```

### 5. 生成 registry suggestions

```bash
python scripts/run_with_runtime.py suggest-registry \
  --summary system_card_artifacts/summary.json \
  --benchmark-registry references/benchmark_registry.yaml \
  --yaml-out system_card_artifacts/registry_suggestions.yaml \
  --json-out system_card_artifacts/registry_suggestions.json
```

## `analysis-mode` 说明

- `rule`
  - 只使用规则分析
- `llm`
  - 尝试使用配置好的 LLM 做 benchmark / dimension / top-level 分析；当前实现缺配置时会退回规则分析并写 warning
- `hybrid`
  - 先做规则分析，再让 GPT-5.4 重写关键自然语言字段
- `auto`
  - 若检测到可用配置则等同于 `hybrid`，否则退回 `rule`

一般推荐：

- 快速跑通：`auto`
- 更稳的增强版：`hybrid`
- 明确想尽量使用 LLM：`llm`

## 输出产物

执行完成后，`--artifacts-dir` 下通常会生成：

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
  benchmark_descriptions/
  benchmark_analysis/
  benchmarks/
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

重点关注：

- `benchmark_analysis/{id}.json`
  - 单 benchmark 的分析事实包
- `dimensions/{id}.json`
  - 维度级综合事实包
- `system_card.json`
  - 顶层 facts
- `system_card_en.md`
  - 英文版最终 markdown
- `system_card_zh.md`
  - 中文版最终 markdown
- `build_report.json`
  - 本次构建模式、warnings、主要产物路径

## 注册表与维度说明

### benchmark registry

`references/benchmark_registry.yaml` 用于注册：

- benchmark 身份
- 主指标
- 默认维度
- 样本选择策略

### dimension registry

`references/dimension_registry.yaml` 主要存放：

- 维度标题
- 顺序
- 默认描述

它**不是** benchmark 成员列表的唯一来源。当前 pipeline 会优先按 benchmark 自带的 `dimension_id` 自动入桶。

需要注意的是：

- `benchmark_registry.yaml` 中可能已经有大量自动生成条目
- 这些条目引用的 dimension 不一定都已经在 `dimension_registry.yaml` 中精修完成
- 当维度元信息缺失时，pipeline 会保留 warning，或自动生成临时维度 bucket

## 当前限制

### 1. 这是单模型 system card 流程

当前 pipeline 仍然主要服务于**单模型** system card。

也就是说：

- 你可以分别给不同模型各生成一套 system card
- 但还不能直接在同一套流程里自动对比“不同模型在相同 benchmark 上的结果”

这个能力已经作为 TODO 记在 `SKILL.md` 中，后续计划用 `compare-models` 之类的专门模式来实现。

### 2. benchmark 说明仍以本地来源为主

当前 `benchmark_descriptions` 主要使用：

- `benchmark_hints.yaml`
- `README.md`
- `eval.yaml`
- task docstring

若本地说明不足，会写 warning，但不会自动联网搜索。

### 3. 终点是 markdown，不是 PDF

当前 Skill 的边界是：

- 生成结构化 facts
- 生成中英文 markdown

LaTeX、PDF、Overleaf 应该在后续发布阶段再处理。

## 更多说明

- 若你是人类使用者，优先看本 README
- 若你在调试 agent 行为或想看更细的约束，阅读 [SKILL.md](./SKILL.md)
