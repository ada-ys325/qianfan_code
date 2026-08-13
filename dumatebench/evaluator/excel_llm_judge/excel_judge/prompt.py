"""Prompt template for checklist-aware Excel LLM judging."""
from __future__ import annotations

import json
from typing import Any

RUBRIC_DIMENSIONS = [
    "instruction_coverage",
    "data_fidelity_and_internal_consistency",
    "workbook_structure_completeness",
    "formula_and_computation_integrity",
    "formatting_and_readability",
    "robustness_and_cleanliness",
]

SYSTEM_PROMPT = """你是一个严格的 Excel 办公产物 LLM-judge。你的评估必须综合 spreadsheet/Excel agent benchmark 的经验，但不要依赖 gold answer。

评估思想来源：
- SpreadsheetBench / SpreadsheetBench Verified: 只看单元格值不够，还要看结构、结果落点、图表/透视表、格式、可维护性。
- SheetCopilot / SheetAgent / SheetMind: Excel 任务覆盖 entry/manipulation、formatting、management、charts、pivot tables、formulas；执行成功和功能正确要分开看。
- WorkstreamBench: 值看起来正确但硬编码、缺少公式、缺单位标签、格式不一致，仍然是不合格的办公交付。
- G-Eval / rubric-based judge: 先按维度生成 task-specific checks，再基于证据逐条打分，输出严格 JSON。

你将收到：
1. task instruction
2. 已有 checklist
3. agent 产物目录的 Excel artifact summary
4. workspace/reference summary，包括执行任务所需的初始文件清单和可读文本摘录
5. reference Excel artifact summary，即 `reference_dir` 中 Excel 输入文件的 workbook evidence；其他类型 reference 会通过 workspace/reference summary 提供文本、PDF、PPT 或文件清单证据

关键规则：
- checklist 是显式验收标准，优先级高于 rubric。checklist 已覆盖的要求，不得重复生成到 rubric 中。
- rubric 只评估 checklist 未覆盖、但会影响 Excel 办公交付质量的内容。
- 不要要求 gold answer；不要声称你能验证所有最终数值的绝对正确性。
- 对 correctness 类判断，只能基于 instruction、checklist、agent workbook summary、reference workbook summary、workspace/reference summary、公式/引用/结构/样式等可观察证据，检查数据忠实性和内部一致性。
- workspace/reference summary 是任务初始上下文，不是 agent 产物。它优先包含 instruction.md 明确提到的任务输入文件，可以作为数据来源、改动范围、ground truth 或用户偏好的证据；不要把未被 instruction 指向的干扰文件当成必须满足的目标。
- reference Excel artifact summary 来自 `reference_dir` 中的输入 workbook。对编辑、清洗、补全、格式调整、公式修复类任务，应把它作为改动前状态或 ground truth 来源之一；若任务输入不是 Excel，则主要依据 workspace/reference summary 中解析出的跨类型证据判断。
- 如果证据不足，降低 evidence_level 和置信度，不要编造结论。
- formula_and_computation_integrity 是条件适用维度：无计算需求，或 checklist 已覆盖公式/计算检查时，标记 not_applicable，不计入总分。
- 如果某维度不适用，normalized_weight 必须为 0；总分只对 applicable=true 的维度重新归一化。
- 输出必须是严格 JSON 对象，不要 markdown 围栏，不要解释性前后缀。

Rubric dimensions:
1. instruction_coverage: checklist 未覆盖的 instruction 覆盖、交付对象、约束和使用场景适配问题。
2. data_fidelity_and_internal_consistency: checklist 未覆盖的数据忠实性和内部一致性问题，例如单位/口径冲突、表格与图表结论不一致、无依据业务结论、关键字段语义不一致。
3. workbook_structure_completeness: checklist 未覆盖的 workbook 结构质量，例如 sheet 组织、区域布局、图表/透视表与表格关系。
4. formula_and_computation_integrity: checklist 未覆盖且任务需要计算时，评估公式/计算逻辑是否可审计、范围是否合理、是否用硬编码替代应有公式。
5. formatting_and_readability: checklist 未覆盖的视觉层级、数字格式一致性、图表可读性、冻结/筛选等办公可读性。
6. robustness_and_cleanliness: checklist 未覆盖的文件可打开性、损坏/多余文件、命名混乱、隐藏异常内容、可复用性。

Output JSON schema:
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
      "levels": {
        "0": "not satisfied or opposite of requirement",
        "1": "severely deficient",
        "2": "partially satisfied",
        "3": "mostly satisfied with minor gaps",
        "4": "fully satisfied"
      }
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

Scoring:
- task_rubrics 是原子 criterion 列表，每条必须有 weight、evidence_required 和 0/1/2/3/4 五档 task-specific levels。
- evidence_required=true 表示该 criterion 评分前必须主动在 workspace/reference summary、reference Excel artifact summary 或其他可用 reference 中寻找 reference、正确答案或 ground truth，并在 evidence/rationale/confidence 中说明找到或未找到的情况。
- criteria_results 必须逐条对应 task_rubrics；score 必须是 0 到 4 的整数，confidence 必须在 0 到 1。
- overall_score 是按 normalized weight 聚合的 0 到 1 分数。
- verdict: overall_score >= 0.8 为 pass，0.6 到 0.8 为 borderline，低于 0.6 为 fail；如果存在文件无法打开等关键失败，可降为 fail。
- evidence_level: strong 表示有明确 workbook/公式/结构证据；medium 表示有部分证据但不能完全验证；weak 表示主要依赖 instruction 和表面内容。
"""


def build_user_prompt(
    instruction: str,
    checklist: str,
    artifact_summary: dict[str, Any],
    reference_summary: dict[str, Any] | None = None,
    reference_artifact_summary: dict[str, Any] | None = None,
    locked_rubrics: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "task_instruction": instruction.strip(),
        "existing_checklist": checklist.strip() or "(empty checklist)",
        "artifact_summary": artifact_summary,
        "workspace_reference_summary": reference_summary or {
            "status": "not_provided",
            "text_artifacts": [],
            "file_inventory": [],
        },
        "reference_excel_artifact_summary": reference_artifact_summary or {
            "status": "not_provided",
            "excel_file_count": 0,
            "workbooks": [],
        },
        "locked_task_rubrics": locked_rubrics or None,
        "judge_request": (
            "If locked_task_rubrics is present, use those exact criteria and only score them. "
            "Otherwise generate task-specific rubrics that do not duplicate checklist items, then evaluate "
            "the Excel artifact evidence against the rubrics. Return strict JSON only."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_dry_run_result() -> dict[str, Any]:
    """Return a deterministic placeholder for dry-run report generation."""
    return {
        "checklist_deduplication": {
            "covered_by_checklist": [],
            "excluded_from_rubric": [],
        },
        "task_rubrics": [
            {
                "id": dim,
                "dimension": dim,
                "weight": 1.0 / len(RUBRIC_DIMENSIONS),
                "evidence_required": True,
                "description": f"dry-run placeholder criterion for {dim}",
                "levels": {
                    "0": "Not satisfied.",
                    "1": "Severely deficient.",
                    "2": "Partially satisfied.",
                    "3": "Mostly satisfied with minor gaps.",
                    "4": "Fully satisfied.",
                },
            }
            for dim in RUBRIC_DIMENSIONS
        ],
        "criteria_results": [
            {
                "id": dim,
                "score": None,
                "evidence": "",
                "rationale": "dry-run did not call LLM judge",
                "confidence": 0.0,
            }
            for dim in RUBRIC_DIMENSIONS
        ],
        "check_results": [],
        "dimension_scores": {
            dim: {
                "applicable": dim != "formula_and_computation_integrity",
                "score": None,
                "evidence_level": "weak",
                "reason": "dry-run did not call LLM judge",
            }
            for dim in RUBRIC_DIMENSIONS
        },
        "overall_score": None,
        "verdict": "dry_run",
        "failure_modes": [],
        "recommendations": ["Run without --dry-run to call the configured OpenAI-compatible judge."],
    }
