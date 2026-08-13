# DuMateBench LLM Judge Rubric and Evaluation Guide

本文档说明 `dumatebench/evaluator` 中现有各类型 LLM judge 的 rubric dimensions、评估流程，以及每条 criterion 字段的含义。当前实现的总体原则是：所有 judge 都以原子 criterion 为评分单元，每条 criterion 使用 0 到 4 的等级评分，再按权重聚合到统一的 `judge_score`。

## 1. 统一 Criterion 格式

除少数类型扩展字段外，所有 LLM judge 的 rubric criterion 都遵循下面的基础格式：

```json
{
  "id": "snake_case_atomic_id",
  "dimension": "dimension_key",
  "description": "一个原子、可观察的评分标准",
  "weight": 1.0,
  "evidence_required": true,
  "levels": {
    "0": "完全失败或与要求相反",
    "1": "严重不足",
    "2": "部分满足",
    "3": "基本满足但有小缺口",
    "4": "充分满足"
  }
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `id` | criterion 的稳定唯一标识。建议使用 snake_case；judge 输出必须用同一个 `id` 回填评分结果。 |
| `dimension` | criterion 所属评估维度。不同产物类型有不同允许值。 |
| `description` | 原子标准描述，只评价一个可观察目标，不能写成“整体质量”这类泛化项。 |
| `weight` | 权重。输入可以是不归一化正数；规范化时会归一化到总和为 1。 |
| `evidence_required` | 是否要求 judge 在评分前主动寻找 reference、正确答案、ground truth、接口说明或其他可验证证据。它只影响 LLM 的证据查找行为，不在程序层自动扣分或改置信度。 |
| `levels` | 0/1/2/3/4 五档评分锚点。每个等级都应是 task-specific 的清晰描述。 |

Judge 输出每条 criterion 的评分结果时，应包含：

| 字段 | 含义 |
|---|---|
| `id` | 对应 rubric criterion 的 `id`。 |
| `score` | 0 到 4 的整数分；无法评估时可为 `null`。音视频多模态和 code judge 明确不允许小数。 |
| `evidence` | 支撑评分的候选产物证据。文本/code/PDF/图片/多模态通常是列表；PPT/Excel 当前为字符串也可被规范化读取。 |
| `rationale` | 简洁说明为什么给这个分数。 |
| `confidence` | 0 到 1 的置信度。证据缺失时应降低置信度并在 rationale 中说明。 |
| `status` | 部分 judge 使用 `pass`、`partial`、`fail`、`cannot_assess` 或 `assessed` 表示评估状态。 |

类型扩展字段：

| Judge | 扩展字段 | 含义 |
|---|---|---|
| 图片 judge | `evidence_hint` | 提示 judge 应从图片或 reference 中寻找什么视觉证据。 |
| 音视频多模态 judge | `modality` | 标记 criterion 主要评价 `text`、`audio`、`video`、`multimodal` 或 `structured` 证据。 |
| 音视频多模态 judge | `covered_check_ids` | 标记该 criterion 与哪些 checklist 项有关，用于避免重复和解释覆盖关系。 |

## 2. 通用评估方式

统一入口是 `dumatebench/evaluator/llm_judge/unified.py`。调用方传入 `output_file` 后，系统根据后缀或显式 `artifact_type` 选择对应 judge。

基本流程：

1. 读取 `instruction.md`、checklist 结果和候选产物。
2. 收集 reference 证据。可以使用 `reference_dir`，也可以使用 `reference_file` / `references_file` 指向一个包含所有 reference 路径的清单文件。
3. 若传入固定 `criteria_file`、`rubric_file`、`criteria`、`task_rubrics` 或 `rubric`，则直接使用这些 criteria；否则由 LLM judge 生成 task-specific criteria。
4. LLM judge 逐条 criterion 输出 `score`、`evidence`、`rationale`、`confidence`。
5. 程序按归一化权重聚合 criterion 分数，得到 0 到 1 的 `judge_score`。
6. 如果同时有 checklist 分数，统一最终分数通常取 checklist score 和 LLM judge score 的平均。

固定输入接口支持：

- 全局 criteria 文件：`--llm-judge-criteria-file evaluator/llm_judge_criteria.json`
- 产物 manifest：`--llm-judge-artifacts-file evaluator/llm_judge_artifacts.json`
- 每个 artifact 项可指定 `output_file`、`artifact_type`、`criteria_file`、`reference_file`、`judge_output_file` 等。

## 3. 文本文档 Judge

适用类型：`doc`、`docx`、`txt`、`md`、`json`、`html`、`htm`。

实现位置：

- `dumatebench/evaluator/llm_judge/schema.py`
- `dumatebench/evaluator/llm_judge/runner.py`

Dimensions：

| Dimension | 含义 |
|---|---|
| `content_relevance` | 内容紧扣任务目标与目标读者，不以无关篇幅掩盖缺失。 |
| `factual_correctness_faithfulness` | 事实、数字、引文和推断有依据；忠实于输入材料并明确不确定性。 |
| `requirement_completeness` | 覆盖任务中所有实质要求、必需字段和交付约束。 |
| `structure_coherence` | 结构完整，层级与顺序合理，论证或叙述连贯。 |
| `language_style` | 表达清楚、准确、自然，语气和文体适合使用场景。 |
| `presentation_readability` | Word/Markdown/文本的标题、段落、列表、表格和格式具有一致性与可读性。 |
| `edit_fidelity` | 编辑任务只改应改内容，保留未授权内容、结构和样式。 |

评估方式：

- 收集候选输出目录中的可读文本、JSON、HTML、DOCX 内容摘录。
- 收集 reference 中可读文本证据。
- 每条 criterion 使用 0 到 4 整数分。
- 聚合器计算 `judge_score_conservative` 和 `judge_score_assessed_only`；统一入口使用 conservative score 归一化为 0 到 1。

## 4. PPT Judge

适用类型：`ppt`、`pptx`。

实现位置：`dumatebench/evaluator/llm_judge/ppt.py`

Dimensions：

| Dimension | 含义 |
|---|---|
| `instruction_following` | 是否遵循任务显式要求、输出约束和目标场景。 |
| `content_correctness` | 幻灯片内容、事实、数字、结论是否正确。 |
| `content_preservation` | 编辑任务中是否保留源文件应保留的信息。 |
| `text_quality` | 文案是否清楚、简洁、专业。 |
| `layout_and_readability` | 页面布局、阅读顺序、字号、对齐和可读性是否合理。 |
| `visual_design` | 配色、图形、视觉层级和版面设计是否符合任务。 |
| `professional_consistency` | 风格、模板、字体、页间一致性是否专业。 |
| `no_unnecessary_changes` | 是否避免无关修改、过度重排或破坏原始设计。 |

评估方式：

- 读取任务说明、输入 PPT、候选 PPT、reference summary 和可选渲染页图。
- 若传入 locked criteria，则直接使用；否则生成 3 到 16 条 `task_rubrics`。
- 每条 criterion 输出 `criteria_results`，`score` 是 0 到 4 整数。
- 最终 `score` 是按 criterion 权重聚合后的 0 到 100 分；统一入口再归一化到 0 到 1。
- 兼容旧版 dimension-level `dimensions[].score`，但当前推荐 criterion-level rubric。

## 5. Excel Judge

适用类型：`xls`、`xlsx`、`xlsm`、`xltx`、`xltm`。

实现位置：

- `dumatebench/evaluator/excel_llm_judge/excel_judge/prompt.py`
- `dumatebench/evaluator/excel_llm_judge/excel_llm_judge.py`

Dimensions：

| Dimension | 含义 |
|---|---|
| `instruction_coverage` | checklist 未覆盖的 instruction 覆盖、交付对象、约束和使用场景适配问题。 |
| `data_fidelity_and_internal_consistency` | 数据忠实性和内部一致性，例如单位、口径、字段语义、表格与图表结论一致性。 |
| `workbook_structure_completeness` | workbook 结构质量，例如 sheet 组织、区域布局、图表/透视表与表格关系。 |
| `formula_and_computation_integrity` | 任务需要计算时，公式/计算逻辑是否可审计、范围是否合理、是否硬编码。 |
| `formatting_and_readability` | 视觉层级、数字格式、图表可读性、冻结/筛选等办公可读性。 |
| `robustness_and_cleanliness` | 文件可打开性、损坏/多余文件、命名混乱、隐藏异常内容和可复用性。 |

评估方式：

- 解析候选 workbook 的 sheet、单元格、公式、样式和结构摘要。
- 解析 reference workbook，并结合跨类型 workspace/reference summary。
- `task_rubrics` 是原子 criterion 列表；每条 criterion 输出 0 到 4 整数分。
- `overall_score` 按权重聚合为 0 到 1；统一入口直接作为 `judge_score`。
- 兼容旧版 `dimension_scores`，会自动转换为 criterion-level 结果。

## 6. PDF Judge

适用类型：`pdf`。

实现位置：

- `dumatebench/evaluator/llm_judge_pdf/schema.py`
- `dumatebench/evaluator/llm_judge_pdf/runner.py`

Dimensions：

| Dimension | 含义 |
|---|---|
| `instruction_following` | 遵循任务中的显式要求、输出约束和目标读者要求。 |
| `content_correctness_faithfulness` | 事实、数字、引文和结论正确，并忠实于输入或参考材料。 |
| `requirement_completeness` | 覆盖任务要求的全部内容、页面、字段和交付物。 |
| `document_structure` | 页面顺序、章节层级、阅读顺序和信息组织完整连贯。 |
| `layout_readability` | 文字、表格和图形清晰可读，无裁切、重叠、溢出或异常留白。 |
| `visual_quality_consistency` | 字体、色彩、间距、对齐和视觉层级一致且符合使用场景。 |
| `edit_fidelity` | 编辑类任务只改变指定内容，并保留源文件中未要求修改的信息与样式。 |
| `artifact_integrity` | PDF 可打开、页数和文件结构合理，指定输出存在且无异常附加产物。 |

评估方式：

- 提取 PDF 文本、页数和页面结构统计。
- 使用 PyMuPDF 渲染页面图像作为视觉证据。
- 对每条 criterion 输出 0 到 4 整数分、证据、理由和置信度。
- conservative score 是 0 到 100；统一入口除以 100 得到 0 到 1。
- 文件无法解析或依赖缺失时写结构化失败报告。

## 7. 图片 Judge

适用类型：`png`、`jpg`、`jpeg`、`webp`。

实现位置：

- `dumatebench/evaluator/llm_judge_image/schema.py`
- `dumatebench/evaluator/llm_judge_image/runner.py`

Dimensions：

| Dimension | 含义 |
|---|---|
| `instruction_content_fidelity` | 指令与内容忠实度。 |
| `semantic_factual_correctness` | 语义与事实正确性。 |
| `composition_visual_hierarchy` | 构图与视觉层级。 |
| `text_readability` | 图片中文字的可读性。 |
| `style_aesthetic_consistency` | 风格与美学一致性。 |
| `reference_fidelity` | 参考图忠实度。 |
| `technical_completeness` | 技术完整性，例如图片可传输、无损坏、主体完整。 |

评估方式：

- 将候选图片和 reference 图片作为视觉附件传给模型，同时传入 workspace/reference 文本摘要。
- Rubric criterion 除基础字段外还包含 `evidence_hint`。
- 每条 criterion 输出 `criteria_results`，分数为 0 到 4 或无法评估时 `null`。
- `weighted_score` 是 0 到 4；统一入口除以 4 得到 0 到 1。
- 若候选图片缺失、损坏或不可传输，相关 criterion 会进入 `cannot_assess` 或失败状态。

## 8. 音视频多模态 Judge

适用类型：

- 音频：`mp3`、`wav`、`m4a`、`flac`、`aac`、`ogg`
- 视频：`mp4`、`mov`、`webm`、`mkv`

实现位置：

- `dumatebench/evaluator/llm_judge_mm/schema.py`
- `dumatebench/evaluator/llm_judge_mm/runner.py`

Dimensions：

| Dimension | 含义 |
|---|---|
| `content_relevance` | 内容紧扣任务目标与读者。 |
| `factual_correctness_faithfulness` | 事实、数字和引文有可定位依据。 |
| `requirement_completeness` | 覆盖任务的实质要求和交付约束。 |
| `structure_coherence` | 结构、顺序和叙述连贯。 |
| `technical_quality` | 媒体或文件的技术质量满足任务要求。 |
| `audio_visual_quality` | 语音、音乐、环境声、音画同步和剪辑质量可接受。 |

评估方式：

- 收集候选媒体和 reference 媒体的 MIME、大小、路径和传输状态。
- 音频/视频可通过 data URL、URL 或视频抽帧方式传输；视频抽帧依赖 `ffmpeg` 和 `ffprobe`。
- Rubric criterion 除基础字段外还包含 `modality` 和 `covered_check_ids`。
- 每条 criterion 的 `score` 必须是 0、1、2、3、4 的整数，不允许小数。
- `judge_score_conservative` 已经是 0 到 1，统一入口直接使用。

## 9. Code Judge

适用类型：`py`、`js`、`jsx`、`ts`、`tsx`、`java`、`go`、`rs`、`cpp`、`cc`、`cxx`、`c`、`h`、`hpp`、`cs`、`rb`、`php`、`swift`、`kt`、`kts`、`scala`、`sh`、`bash`、`zsh`、`ps1`、`sql`、`r`、`lua`、`pl`、`pm`、`dart`、`ex`、`exs`、`erl`、`hrl`、`clj`、`cljs`、`fs`、`fsx`、`jl`、`nim`、`zig`、`vue`、`svelte`、`astro`，或显式 `artifact_type=code`。

实现位置：

- `dumatebench/evaluator/llm_judge_code/schema.py`
- `dumatebench/evaluator/llm_judge_code/runner.py`

Dimensions：

| Dimension | 含义 |
|---|---|
| `functional_correctness` | 代码是否实现任务要求、核心行为和预期输出。 |
| `bug_risk_defect` | 代码是否存在明显缺陷、运行时错误、逻辑漏洞、状态不一致、异常路径失败或隐藏 bug 风险。 |
| `reference_fidelity` | 代码是否忠实使用给定 reference、接口说明、数据 schema 或正确答案。 |
| `repo_integration` | 代码是否正确集成到现有项目结构、API、依赖和调用链中。 |
| `regression_safety` | 是否避免破坏已有功能、公开接口、文件格式、兼容行为。 |
| `edge_case_robustness` | 是否处理边界条件、异常输入、空值、错误状态和 contracts。 |
| `algorithmic_efficiency` | 时间/空间复杂度、批量数据规模和实现效率是否合理。 |
| `maintainability_readability` | 代码是否清晰、局部、可维护，符合项目风格，避免过度复杂。 |
| `security_safety` | 是否避免注入、路径穿越、敏感信息泄露、危险执行和不安全依赖。 |

评估方式：

- 收集候选代码文件内容，以及 reference 中的代码、说明文档、JSON/YAML/TOML 等可读证据。
- 除 `DIMENSIONS` 外，code judge 的 criterion 和 judge 输出协议与文本 judge 保持一致。
- 每条 criterion 使用 0 到 4 分；输出中整值分数保持为整数。
- 聚合器计算 conservative score，统一入口归一化为 0 到 1。
- 支持固定 criteria 文件，要求 `dimension` 使用上表中的代码专属维度。

## 10. Score 聚合约定

Criterion 加权聚合的通用公式：

```text
criterion_unit_score = score / 4
judge_score = sum(normalized_weight_i * criterion_unit_score_i)
```

不同 judge 的内部原始分数范围略有差异，但统一入口最终都会输出 0 到 1 的 `judge_score`：

| Judge | 内部聚合结果 | 统一入口归一化 |
|---|---|---|
| 文本文档 | `judge_score_conservative`，0 到 100 | 除以 100 |
| PPT | `score`，0 到 100 | 除以 100 |
| Excel | `overall_score`，0 到 1 | 直接使用 |
| PDF | `aggregate.judge_score_conservative`，0 到 100 | 除以 100 |
| 图片 | `weighted_score`，0 到 4 | 除以 4 |
| 音视频多模态 | `judge_score_conservative`，0 到 1 | 直接使用 |
| Code | `judge_score_conservative`，0 到 100 | 除以 100 |

`evidence_required=true` 的 criterion 如果缺少 reference 或正确答案，LLM judge 应主动说明证据缺失并降低置信度；程序层不会因为该字段自动改分。若无法从候选产物、reference 或可靠常识判断，应返回 `cannot_assess` / `score=null`，并在聚合结果中体现 coverage 与 human review 风险。

