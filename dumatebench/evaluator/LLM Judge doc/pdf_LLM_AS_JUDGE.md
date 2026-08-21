PDF LLM-as-Judge

# PDF LLM-as-Judge
 DuMateBench PDF 方向 LLM-as-Judge evaluator。确定性 evaluator 负责检查输出路径、文件存在、PDF 可解析性和任务中的机械规则；LLM Judge 负责评估内容正确性、要求完整性、文档结构、版式可读性、视觉一致性和编辑忠实性。

## 背景
PDF 生成和编辑任务允许多种合理结果。文件存在、页数正确或 PDF 可打开并不代表内容正确，也不能判断文字是否裁切、表格是否可读、页面层级是否清楚、源文档是否被无关修改。因此需要先构建文本、结构和页面图像组成的 evidence surface，再按任务专属 rubric 逐项评分。

本实现只采用已由论文或官方仓库核实的 benchmark 范式：

* [PaperBench（Starace et al., ICML 2025）](https://proceedings.mlr.press/v267/starace25a.html) 为每篇论文构建层级化、任务专属 rubric，并用 Judge 对 agent 产物逐项评分。DuMateBench 借鉴“候选产物出现前确定 rubric、逐项评分、加权聚合和保留可审计依据”的流程。PaperBench 的 rubric 由论文作者参与构建；本实现则根据 task instruction、`checks.yaml` 和 reference 清单自动生成，因此不能声称二者 rubric 来源相同。
* [OmniDocBench（Ouyang et al., CVPR 2025）](https://openaccess.thecvf.com/content/CVPR2025/html/Ouyang_OmniDocBench_Benchmarking_Diverse_PDF_Document_Parsing_with_Comprehensive_Annotations_CVPR_2025_paper.html) 将 PDF 证据拆分为文本、版面、表格、公式、阅读顺序等层次。DuMateBench 借鉴这种分层 evidence surface，联合使用 PDF 提取文本、页面统计和渲染图像。OmniDocBench 是文档解析 benchmark，使用专门的解析指标，不使用 LLM-as-Judge，也不评估生成或编辑后的 PDF 质量。

## Rubric 维度
Judge 从以下通用维度生成 3 到 16 条任务专属、原子化评分项：

* `instruction_following`：遵循显式要求、输出约束和目标读者要求。
* `content_correctness_faithfulness`：事实、数字、引文和结论正确，并忠实于输入材料。
* `requirement_completeness`：覆盖全部必需内容、页面、字段和交付物。
* `document_structure`：页面顺序、章节层级、阅读顺序和信息组织完整连贯。
* `layout_readability`：文字、表格和图形可读，无裁切、重叠、溢出和异常留白。
* `visual_quality_consistency`：字体、色彩、间距、对齐和视觉层级一致。
* `edit_fidelity`：只修改指定内容，保留源文件中未要求改变的信息和样式。
* `artifact_integrity`：PDF 可用、页数和结构合理，指定输出存在且无异常附加产物。

每条 rubric 使用 0 到 4 分：0 表示未满足，4 表示完全满足。Rubric 包含 `weight`、`evidence_required` 和五档任务专属描述。`evidence_required=true` 只要求 judge 在评分前主动寻找 reference、正确答案或 ground truth 证据，并在 evidence/rationale/confidence 中说明找到或缺失情况；程序层不因该字段自动改分或改置信度。权重归一化为 1，并通过 `instruction_hash` 和 `rubric_hash` 锁定。生成 rubric 时 Judge 看不到候选产物，避免根据结果反向修改标准。

## 评估证据
`llm_judge_pdf/artifacts.py` 为候选目录和 reference 目录分别准备证据：

1. 使用 `pypdf` 检查 PDF、提取页数、逐页文本和字符统计。
2. 使用 `PyMuPDF` 将页面渲染为 PNG，并以 OpenAI-compatible `image_url` data URI 传给原生多模态模型。
3. 页数不超过 `--max-pages` 时全部送入；超过时等距采样并固定保留首尾页。结果记录 `sampled_pages` 和 `omitted_pages`。
4. `.txt`、`.md`、`.json` 辅助产物作为文本证据；超长文本按预算截断并记录 `text_truncated`。
5. reference PDF 使用相同的文本和渲染流程，但与 candidate 明确标记，供内容忠实性和编辑保真度比较。
6. `reward.json`、`rubric.json`、`judge_result.json` 被排除，避免 evaluator 读取自己的输出。

Judge 必须为每条 criterion 返回文件路径，并尽可能返回页码、原文 quote 或 `visual_observation`。不存在的路径和越界页码会被丢弃；若 `evidence_required=true` 且没有找到 reference 或正确答案证据，judge 应在 rationale/confidence 中说明，但程序不会因此自动把该项置 0。

## Judge 输出
`rubric.json` 保存锁定的任务专属标准：

```json
{
  "schema_version": "1.0",
  "task_id": "task-id",
  "instruction_hash": "...",
  "criteria": [
    {
      "id": "required_sections",
      "dimension": "requirement_completeness",
      "description": "...",
      "weight": 0.3,
      "evidence_required": true,
      "levels": {"0": "...", "1": "...", "2": "...", "3": "...", "4": "..."}
    }
  ],
  "rubric_hash": "..."
}
```
`judge_result.json` 保存 candidate/reference 清单、采样页、解析错误、确定性 gate、逐项分数与证据、覆盖率、人工复核标记和最终分数。多次 Judge 运行使用逐项中位数；只有 judge 明确返回 `unassessed` 或硬门控失败的项才按不可评估处理。

确定性结果存在时，默认按 40% deterministic + 60% LLM Judge 计算；没有确定性结果时使用 LLM Judge 分数。无有效 PDF、PDF 无法解析或硬性文件检查失败时直接置 0；其他规则失败默认把上限限制为 80。

## CLI 用法
### 一次生成 rubric 并运行 PDF judge
以下命令在项目使用的 `dev` Docker 中运行：

```bash
docker exec dev sh -lc '
  cd /mnt/cfs_algo_bj/workspace/tianhan/DataAnnotation &&
  export OPENAI_API_KEY="..." &&
  export OPENAI_BASE_URL="https://<openai-compatible-endpoint>/v1" &&
  python -m pdf_bench.llm_judge_pdf.cli run \
    --task-dir pdf_bench/generated_tasks/<task-id> \
    --outputs-dir /path/to/agent/outputs \
    --reference-dir pdf_bench/generated_tasks/<task-id>/workspace_seed \
    --model <multimodal-model> \
    --rubric-out /path/to/rubric.json \
    --result-out /path/to/judge_result.json \
    --rule-result /path/to/reward.json \
    --max-pages 12 \
    --judge-runs 1
'
```
模型接口必须兼容 Chat Completions JSON mode，并支持 user message 中的 `image_url` data URI。`--reference-dir -` 可禁用 reference evidence。

### 单独生成和复用 rubric
```bash
docker exec dev sh -lc '
  cd /mnt/cfs_algo_bj/workspace/tianhan/DataAnnotation &&
  python -m pdf_bench.llm_judge_pdf.cli generate-rubric \
    --task-dir pdf_bench/generated_tasks/<task-id> \
    --rubric-out /path/to/rubric.json \
    --model <multimodal-model>
'
```
```bash
docker exec dev sh -lc '
  cd /mnt/cfs_algo_bj/workspace/tianhan/DataAnnotation &&
  python -m pdf_bench.llm_judge_pdf.cli evaluate \
    --task-dir pdf_bench/generated_tasks/<task-id> \
    --outputs-dir /path/to/agent/outputs \
    --rubric /path/to/rubric.json \
    --result-out /path/to/judge_result.json \
    --model <multimodal-model>
'
```
`evaluate` 会重新计算 instruction hash；任务指令改变后不能继续使用旧 rubric。
