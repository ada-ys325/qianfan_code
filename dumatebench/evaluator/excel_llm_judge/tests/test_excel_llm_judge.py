from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from excel_judge.artifact_summary import summarize_artifacts
from excel_judge.prompt import SYSTEM_PROMPT, build_user_prompt

PROJECT_DIR = Path(__file__).resolve().parents[1]


def _make_workbook(path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = "A1:C4"
    sheet["A1"] = "Region"
    sheet["B1"] = "Sales"
    sheet["C1"] = "Total"
    for cell in sheet[1]:
        cell.font = openpyxl.styles.Font(bold=True)
        cell.fill = openpyxl.styles.PatternFill("solid", fgColor="FFDDDDDD")
    sheet["A2"] = "North"
    sheet["B2"] = 120
    sheet["A3"] = "South"
    sheet["B3"] = 80
    sheet["C2"] = "=SUM(B2:B3)"
    sheet["B2"].number_format = "$#,##0"
    workbook.save(path)
    workbook.close()


def test_summarize_artifacts_extracts_excel_evidence(tmp_path: Path) -> None:
    workbook_path = tmp_path / "result.xlsx"
    _make_workbook(workbook_path)
    (tmp_path / "notes.txt").write_text("extra note", encoding="utf-8")

    summary = summarize_artifacts(tmp_path)

    assert summary["excel_file_count"] == 1
    assert summary["non_excel_file_count"] == 1
    signals = summary["aggregate_signals"]
    assert signals["has_readable_workbook"] is True
    assert signals["has_formulas"] is True
    workbook = summary["workbooks"][0]
    assert workbook["sheet_names"] == ["Sales"]
    sheet = workbook["sheets"][0]
    assert sheet["freeze_panes"] == "A2"
    assert sheet["auto_filter_ref"] == "A1:C4"
    assert {item["cell"] for item in sheet["formula_samples"]} == {"C2"}
    assert any("Region" in candidate["values"] for candidate in sheet["header_candidates"])


def test_prompt_requires_reference_free_checklist_dedup(tmp_path: Path) -> None:
    workbook_path = tmp_path / "result.xlsx"
    _make_workbook(workbook_path)
    summary = summarize_artifacts(tmp_path)

    user_prompt = build_user_prompt(
        "Create a regional sales summary workbook.",
        "- Must include a Sales sheet\n- Must freeze header row",
        summary,
    )
    payload = json.loads(user_prompt)

    assert "gold answer" in SYSTEM_PROMPT
    assert "checklist" in SYSTEM_PROMPT.lower()
    assert "instruction_coverage" in SYSTEM_PROMPT
    assert "data_fidelity_and_internal_consistency" in SYSTEM_PROMPT
    assert payload["existing_checklist"].startswith("- Must include")
    assert payload["artifact_summary"]["excel_file_count"] == 1


def test_cli_dry_run_writes_outputs(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    _make_workbook(artifact_dir / "result.xlsx")
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Create a regional sales summary workbook.", encoding="utf-8")
    checklist = tmp_path / "checklist.md"
    checklist.write_text("- Must include a Sales sheet", encoding="utf-8")
    out_dir = tmp_path / "judge_out"

    env_cmd = [
        sys.executable,
        str(PROJECT_DIR / "excel_llm_judge.py"),
        "--instruction",
        str(instruction),
        "--checklist",
        str(checklist),
        "--artifact-dir",
        str(artifact_dir),
        "--out-dir",
        str(out_dir),
        "--dry-run",
    ]
    result = subprocess.run(env_cmd, cwd=PROJECT_DIR, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    judge_input = json.loads((out_dir / "judge_input.json").read_text(encoding="utf-8"))
    judge_result = json.loads((out_dir / "judge_result.json").read_text(encoding="utf-8"))
    report = (out_dir / "judge_report.md").read_text(encoding="utf-8")
    assert judge_input["user_prompt"]["existing_checklist"].startswith("- Must include")
    assert judge_result["dry_run"] is True
    assert judge_result["result"]["verdict"] == "dry_run"
    assert "instruction_coverage" in judge_result["result"]["dimension_scores"]
    assert "formula_and_computation_integrity" in judge_result["result"]["dimension_scores"]
    assert "Excel LLM Judge Report" in report
    assert "Checklist Deduplication" in report
