# Excel LLM-judge

Excel LLM-judge 是一个独立的 Excel 生成产物评估工具，用于评估办公 agent 生成的 Excel 文件是否满足任务要求，并具备可读、可审计、可复用的办公交付质量。

评估不依赖 gold answer。工具会读取 task instruction、已有 checklist 和 agent 产物目录，抽取 workbook evidence，并调用 OpenAI-compatible LLM judge 生成 task-specific rubric 和评估结果。已有 checklist 覆盖的显式验收点会被排除，避免重复评价。

## 文件结构

```text
excel_llm_judge/
├── excel_llm_judge.py              CLI 入口
├── excel_judge/
│   ├── artifact_summary.py         Excel workbook evidence 抽取
│   └── prompt.py                   LLM judge prompt 和 rubric schema
├── tests/
│   └── test_excel_llm_judge.py     单元测试和 dry-run 冒烟测试
├── requirements.txt                运行和测试依赖
├── README.md                       使用说明
└── excel_llm_judge_report.md       调研和设计报告
```

## Rubric 维度

- `instruction_coverage`：任务指令覆盖度，包括交付对象、输出约束和使用场景适配。
- `data_fidelity_and_internal_consistency`：数据忠实性和内部一致性，包括单位、口径、表格、图表、结论之间是否一致。
- `workbook_structure_completeness`：工作簿结构完整性，包括文件、sheet、表头、区域布局、图表和透视表组织。
- `formula_and_computation_integrity`：公式和计算完整性。仅在任务涉及计算时适用，检查公式/计算逻辑是否可审计、范围是否合理、是否存在应使用公式却硬编码的情况。
- `formatting_and_readability`：格式和可读性，包括视觉层级、数字格式、对齐、冻结窗格、筛选和图表可读性。
- `robustness_and_cleanliness`：交付稳健性和整洁度，包括文件可打开性、命名清晰度、多余/损坏文件、隐藏异常内容和复用性。

## 安装依赖

```bash
cd /mnt/cfs_algo_bj/workspace/zhangjx09/baidu/personal-code/excel_llm_judge
python3 -m pip install -r requirements.txt
```

## Dry-run

`--dry-run` 只生成评估输入摘要和占位报告，不调用模型，适合检查 Excel 证据抽取是否正常。

```bash
python3 excel_llm_judge.py \
    --instruction ./instruction.md \
    --checklist ./checklist.md \
    --artifact-dir ./agent_outputs \
    --out-dir ./excel_judge_out \
    --dry-run
```

## 真实评估

配置 OpenAI-compatible API 环境变量并去掉 `--dry-run`：

```bash
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o

python3 excel_llm_judge.py \
    --instruction ./instruction.md \
    --checklist ./checklist.md \
    --artifact-dir ./agent_outputs \
    --out-dir ./excel_judge_out
```

也可以用 CLI 覆盖模型配置：

```bash
python3 excel_llm_judge.py \
    --instruction ./instruction.md \
    --checklist ./checklist.md \
    --artifact-dir ./agent_outputs \
    --out-dir ./excel_judge_out \
    --base-url https://cn.huayanapi.com:27502/v1 \
    --model claude-opus-4-8
```

## 输出文件

- `judge_input.json`：评估输入，包括 system prompt、task instruction、checklist、artifact summary 和模型配置。
- `judge_result.json`：模型原始输出、解析结果、耗时和运行状态。
- `judge_report.md`：人工可读报告，包括总体结论、维度分、checklist 去重审计、失败模式和建议。

## 测试

```bash
cd /mnt/cfs_algo_bj/workspace/zhangjx09/baidu/personal-code/excel_llm_judge
python3 -m pytest tests/test_excel_llm_judge.py -v
```

## 已知限制

- `openpyxl` 不能像 Excel 桌面端一样重新计算公式，因此工具只抽取公式字符串和 workbook evidence，不把公式执行结果当成唯一真值。
- 没有 gold answer 时，数值绝对正确性不能完全保证；报告会通过 `evidence_level` 暴露判断置信度。
- `.xls` 旧格式会被识别为 Excel 文件，但不能用 `openpyxl` 深度解析；报告会记录为不可检查 evidence。
- LLM judge 输出质量依赖模型能力和 prompt 遵循度，建议在正式批量使用前抽样人工校准阈值和维度权重。
