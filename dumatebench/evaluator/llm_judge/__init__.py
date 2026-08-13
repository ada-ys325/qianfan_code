"""LLM-as-judge helpers for DuMateBench evaluators."""

from .runner import JudgeRunner

__all__ = [
    "JudgeRunner",
    "evaluate_pptx_llm_judge",
    "run_llm_judge_score",
    "run_pptx_judge",
]


def __getattr__(name):
    if name in {"evaluate_pptx_llm_judge", "run_pptx_judge"}:
        from . import ppt

        return getattr(ppt, name)
    if name == "run_llm_judge_score":
        from . import unified

        return getattr(unified, name)
    raise AttributeError(name)
