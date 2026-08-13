from __future__ import annotations

import json
from typing import Any

from .schema import DIMENSIONS


def rubric_messages(*, task_id: str, instruction: str, checks_summary: str, reference_inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    media_rules = (
        "媒体任务请覆盖：内容正确性、语音/音乐/环境声可懂度、音画同步、剪辑连续性、技术质量、任务要求完成度。"
        "criteria 必须原子化、候选无关、证据可定位（文本引用或媒体时间戳）。"
        "需要基于 reference、正确答案或 ground truth 才能可靠评分的 criterion 设置 evidence_required=true。"
        "仅当当前 session 的 checks.yaml 已覆盖机械项时排除该项，不要全局或跨 session 去重。"
        "reference artifact 只是 judge 运行时可用的证据池，不要在 rubric criterion 中显式绑定某个 reference 文件、"
        "golden source 或 reference evidence 类型。不要生成需要人工 gold review 才能评分的 criterion；"
        "若某个需求需要参考材料，请改写成基于当前产物、任务说明和可用 reference 可直接评估的 criterion。"
    )
    schema = {"criteria": [{"id": "...", "dimension": "...", "description": "...", "weight": 1,
        "evidence_required": True, "levels": {str(i): "..." for i in range(5)}, "modality": "audio|video|multimodal|text",
        "covered_check_ids": []}]}
    return [
        {"role": "system", "content": "你是 DuMateBench 的资深 rubric 设计者。只返回 JSON，不要输出 Markdown。"},
        {"role": "user", "content": (
            f"task_id={task_id}\n任务说明：\n{instruction}\n\n" + media_rules +
            "\n已有同 session checks 摘要（不要重复其中机械检查）：\n" + checks_summary +
            "\n参考 artifact inventory（仅用于理解可用证据）：\n" + json.dumps(reference_inventory, ensure_ascii=False, indent=2) +
            "\n维度：\n" + json.dumps(DIMENSIONS, ensure_ascii=False) +
            "\n输出 schema 示例：\n" + json.dumps(schema, ensure_ascii=False))},
    ]


def judge_messages(*, instruction: str, rubric: dict[str, Any], artifacts: list[dict[str, Any]], references: list[dict[str, Any]], media_messages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    text = (
        "按 rubric 对产物评分。引用 artifact 的 path；媒体证据引用时间戳或片段。不得因无法听看而猜测。"
        "当视频以 sampled image frames 表示时，只能评价帧中可见内容；不能据此猜测音频、音画同步、完整运动连续性或帧间未展示内容。"
        "每个已评估 criterion 的 score 必须是 0、1、2、3、4 中的整数，不能使用小数。"
        "对于 evidence_required=true 的 criterion，评分前必须主动在参考 inventory、transcript/reference 和可用媒体证据中寻找 reference、正确答案或 ground truth，"
        "并在 evidence/rationale/confidence 中说明找到或未找到的情况。"
        "依赖 transcript/reference 且当前没有可靠依据时，criterion 必须输出 status=\"cannot_assess\"、score=null，"
        "并说明原因；不要把 cannot_assess 当作通过。只返回 JSON。\n\n"
        f"任务：{instruction}\nRubric：{json.dumps(rubric, ensure_ascii=False)}\n"
        f"产物 inventory：{json.dumps(artifacts, ensure_ascii=False)}\n"
        f"参考 inventory：{json.dumps(references, ensure_ascii=False)}"
    )
    if media_messages:
        return [{"role": "system", "content": "你是严格、可审计的多模态 LLM judge。"}, {"role": "user", "content": [{"type": "text", "text": text}] + media_messages}]
    return [{"role": "system", "content": "你是严格、可审计的 LLM judge。"}, {"role": "user", "content": text}]
