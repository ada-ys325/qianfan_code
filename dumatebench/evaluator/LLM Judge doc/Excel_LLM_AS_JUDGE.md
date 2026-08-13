# Excel LLM-as-Judge
## 背景
Excel LLM-judge 用于评估办公 agent 生成的 Excel 文件。评估目标不是复现某个固定 benchmark 的逐格标准答案检查，而是在没有 gold answer 的情况下，基于 task instruction、已有 checklist 和 workbook evidence 判断产物是否具备办公交付质量。

## Benchmark 参考
* SpreadsheetBench / SpreadsheetBench Verified：强调真实 spreadsheet 任务不能只比较单元格值，还需要检查结构、结果位置、图表、格式、公式和可维护性。
* SheetCopilot / SheetAgent / SheetMind：覆盖 Excel entry/manipulation、formatting、management、charts、pivot tables、formulas 等任务类型，区分执行成功和功能满足。
* WorkstreamBench：关注值正确但硬编码、缺少可审计公式、单位缺失、格式不一致等办公交付失败模式。
* G-Eval / rubric-based judge：提供多维度 rubric、task-specific checks、结构化评分和证据化理由的 LLM-as-judge 范式。

## 评估原则
已有 checklist 作为显式验收标准，优先级高于 LLM rubric。LLM judge 在生成 rubric 前需要识别 checklist 已覆盖的要求，避免重复评价。rubric 主要覆盖 checklist 之外、但会影响 Excel 办公交付质量的开放式质量项。

该评估不依赖 gold answer。对于数值和内容正确性，judge 只基于 instruction、checklist、workbook summary、公式、表头、图表、样式和文件清单等可观察证据进行判断，并通过 `evidence_level` 标识置信度。

## Rubric 维度
* `instruction_coverage`：评估产物是否覆盖任务指令，包括交付对象、输出约束和使用场景适配。
* `data_fidelity_and_internal_consistency`：评估数据、单位、统计口径、表格、图表和结论之间是否一致，是否存在无依据内容或明显冲突。
* `workbook_structure_completeness`：评估文件、sheet、表头、区域布局、图表、透视表和数据源关系是否完整清晰。
* `formula_and_computation_integrity`：条件适用维度。任务涉及计算时，评估公式/计算逻辑是否可审计、引用范围是否合理、是否用硬编码替代应有公式；无计算需求时不计入总分。
* `formatting_and_readability`：评估字体、颜色、数字格式、对齐、冻结窗格、筛选、图表可读性和整体专业度。
* `robustness_and_cleanliness`：评估文件是否可打开、目录是否整洁、命名是否清晰、是否存在多余/损坏文件或隐藏异常内容。

## 输入
* `task instruction`：用户任务说明，可以是文本或文件路径。
* `checklist`：已有显式验收项，可以是文本或文件路径。
* `artifact-dir`：agent 生成产物目录，工具只扫描该目录。

## 输出
* `judge_input.json`：评估输入和模型配置。
* `judge_result.json`：模型输出、解析结果、耗时和运行状态。
* `judge_report.md`：人工可读评估报告。

## JSON Schema
```json
{
  "checklist_deduplication": {
    "covered_by_checklist": [{"checklist_item": "...", "covered_requirement": "..."}],
    "excluded_from_rubric": [{"candidate_check": "...", "reason": "already covered by checklist item ..."}]
  },
  "task_rubrics": [
    {
      "id": "snake_case_atomic_id",
      "dimension": "instruction_coverage|data_fidelity_and_internal_consistency|workbook_structure_completeness|formula_and_computation_integrity|formatting_and_readability|robustness_and_cleanliness",
      "weight": 0.0,
      "evidence_required": true,
      "description": "one observable task-specific requirement not covered by checklist",
      "levels": {"0": "...", "1": "...", "2": "...", "3": "...", "4": "..."}
    }
  ],
  "criteria_results": [
    {"id": "same as task_rubrics[].id", "score": 0, "evidence": "...", "rationale": "...", "confidence": 0.8}
  ],
  "overall_score": 0.0,
  "verdict": "pass|borderline|fail",
  "failure_modes": ["..."],
  "recommendations": ["..."]
}
```
## 实现文件
* `excel_llm_judge.py`：CLI 入口，负责读取输入、调用摘要抽取、请求 LLM judge、写 JSON 和 Markdown 报告。
* `excel_judge/artifact_summary.py`：用 `openpyxl` 抽取 workbook evidence，包括 sheet、表头候选、非空单元格、公式样本、样式信号、冻结窗格、筛选、表格、图表和图片数量。
* `excel_judge/prompt.py`：定义 judge role、rubric 维度、checklist 去重规则、JSON schema 和评分规则。
* `tests/test_excel_llm_judge.py`：覆盖 workbook 摘要、prompt 约束和 CLI dry-run。

## 已知限制
* `openpyxl` 不能重新计算公式，因此公式评估基于公式字符串、引用范围和 workbook evidence。
* 没有 gold answer 时，judge 不断言所有最终数值的绝对正确性。
* `.xls` 旧格式不能用 `openpyxl` 深度解析。
* LLM judge 输出质量依赖模型能力，正式批量使用前应抽样校准阈值和维度权重。
