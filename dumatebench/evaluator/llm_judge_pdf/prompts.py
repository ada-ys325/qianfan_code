from __future__ import annotations

import json
from typing import Any

from .schema import GENERAL_DIMENSIONS

Message = dict[str, Any]


def rubric_messages(
    *,
    task_id: str,
    instruction: str,
    objective_checks: str,
    reference_inventory: list[dict[str, Any]],
) -> list[Message]:
    payload = {
        "task_id": task_id,
        "instruction": instruction,
        "objective_checks": objective_checks,
        "reference_inventory": reference_inventory,
        "general_dimensions": GENERAL_DIMENSIONS,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 DuMateBench 的 PDF 评估标准设计者。候选产物尚不可见。请仅根据任务要求和参考材料清单，"
                "生成 3 到 16 条候选无关、原子、可审计的 task-specific rubric。只选择与任务相关的通用维度，"
                "不要把文件存在、PDF 可解析等确定性检查伪装成主观质量项。每条必须包含 id、dimension、"
                "description、positive weight、evidence_required，以及 0/1/2/3/4 五档互斥的 levels。"
                "需要基于 reference、正确答案或 ground truth 才能可靠评分的 criterion 设置 evidence_required=true。"
                "0 表示未满足，4 表示完全满足。返回严格 JSON 对象，顶层仅包含 criteria。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def judge_messages(
    *,
    instruction: str,
    rubric: dict[str, Any],
    artifacts: list[dict[str, Any]],
    reference_artifacts: list[dict[str, Any]],
) -> list[Message]:
    evidence = []
    reference_evidence = []
    image_parts: list[dict[str, Any]] = []
    for source_label, path_prefix, source, target in (
        ("Candidate", "outputs", artifacts, evidence),
        ("Reference", "references", reference_artifacts, reference_evidence),
    ):
        for artifact in source:
            tagged = {key: value for key, value in artifact.items() if key != "images"}
            tagged["path"] = f"{path_prefix}/{artifact['path']}"
            target.append(tagged)
            for image in artifact.get("images", []):
                image_parts.append({"type": "text", "text": f"{source_label} {path_prefix}/{artifact['path']}, rendered page {image['page']}:"})
                image_parts.append({"type": "image_url", "image_url": {"url": image["data_url"], "detail": "high"}})

    payload = {
        "instruction": instruction,
        "locked_rubric": rubric,
        "candidate_evidence": evidence,
        "reference_evidence": reference_evidence,
    }
    user_content: list[dict[str, Any]] = [{
        "type": "text",
        "text": json.dumps(payload, ensure_ascii=False, indent=2),
    }]
    user_content.extend(image_parts)
    return [
        {
            "role": "system",
            "content": (
                "你是 DuMateBench 的严格 PDF 质量评审。必须按 locked rubric 逐项核对，不得新增、删除或合并评分项。"
                "综合 PDF 提取文本、结构统计和渲染页面评分。每个 criterion 返回 id、0-4 整数 score、0-1 confidence、"
                "evidence 数组和简短 rationale。evidence 必须定位到 artifact path，并尽可能给出 page、quote 或 visual_observation。"
                "对于 evidence_required=true 的 criterion，评分前必须主动在 reference_evidence 中寻找 reference、正确答案或 ground truth，"
                "并在 evidence/rationale/confidence 中说明找到或未找到的情况。"
                "未采样页面或无法观察的要求不得臆测，应降低 confidence 并解释。不要用整体印象替代证据。"
                "顶层严格返回 {criteria: [...], summary: string} JSON。"
            ),
        },
        {"role": "user", "content": user_content},
    ]
