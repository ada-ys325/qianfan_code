from __future__ import annotations

import json
from typing import Any
from .schema import DIMENSIONS

def rubric_messages(
    instruction: str,
    checks: dict[str, Any],
    inventory: list[dict[str, Any]],
    reference_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    return [{"role": "system", "content": "你是静态图片任务的评分标准设计者。只输出 JSON，不要 Markdown。生成候选无关、原子、可定位证据的 criteria。排除文件存在、格式、尺寸等机械检查。每项必须有 id, dimension, description, evidence_hint, weight, evidence_required, levels；levels 必须包含 0 到 4 五个分数锚点。需要基于 reference、正确答案或 ground truth 才能可靠评分的 criterion 设置 evidence_required=true。"}, {"role": "user", "content": json.dumps({"instruction": instruction, "objective_checks": checks, "reference_inventory": inventory, "workspace_reference_summary": reference_context, "dimensions": DIMENSIONS, "required_schema": {"criteria": "array"}}, ensure_ascii=False)}]

def judge_messages(
    instruction: str,
    checks: dict[str, Any],
    rubric: dict[str, Any],
    inventory: list[dict[str, Any]],
    reference_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    return [{"role": "system", "content": "你是视觉图片评审员。附件按清单中的 id 和 role 分组，候选在前、参考在后。只根据可见证据和 workspace reference 摘要评分，不臆测不可见内容。对于 evidence_required=true 的 criterion，评分前必须主动在参考附件和 workspace reference 摘要中寻找 reference、正确答案或 ground truth，并在 evidence/rationale/confidence 中说明找到或未找到的情况。只有在所需图片缺失、损坏、不可传输或完全无法看到时，才允许 status=cannot_assess 且 score=null；如果候选图片可见但无法确认满足要求，必须判为 fail 并给 0-4 整数分，不能用 cannot_assess 代替失败。只输出 JSON，不要 Markdown。每项返回 id,status(pass/fail/cannot_assess),score(0-4或null),evidence,rationale,confidence；另含 summary。"}, {"role": "user", "content": json.dumps({"instruction": instruction, "objective_checks": checks, "rubric": rubric, "attachments": inventory, "workspace_reference_summary": reference_context}, ensure_ascii=False)}]
