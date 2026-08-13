# DuMateBench LLM-as-Judge 方案及实现

本文说明 DuMateBench 在原有 checklist evaluator 之上接入 LLM-as-Judge 的设计和实现。目标是保留确定性检查的稳定性，同时补充对内容质量、视觉质量、媒体质量和任务完成度的语义判断。

## 1. Checklist 与 LLM-as-Judge 的职责划分

DuMateBench 的评分分为两层：checklist 负责确定性、可程序化的检查；LLM-as-Judge 负责开放式、语义型和质量型判断。两者不是互相替代，而是覆盖不同风险。

Checklist 适合检查明确、机械、可复现的条件。例如：

- 输出文件是否存在。
- 输出路径是否正确。
- 文件格式是否可读取，例如 `docx`、`pptx`、`xlsx`、`pdf`。
- Excel 是否包含指定 sheet、指定单元格值、公式或样式。
- 文档或表格是否包含指定关键词。
- 输出目录是否只包含预期交付文件。
- 邮件、日历、日志等结构化副产物是否符合规则。

这类检查的优点是稳定、便宜、可解释，适合作为交付门控。它们能快速发现“没有交付”“交付错路径”“文件打不开”“缺少必需字段”等硬错误。

LLM-as-Judge 负责 checklist 难以可靠判断的部分。例如：

- 文档内容是否真正回答了任务要求。
- PPT 是否保留了原始信息、布局是否清晰、设计是否一致。
- Word、Markdown、TXT、JSON 等文本产物是否结构合理、内容完整、语言清楚。
- Excel 表格是否符合业务意图，是否有合理的数据组织、公式解释和分析结论。
- PDF 是否可读、页面结构是否清楚、表格和图像是否没有裁切或重叠。
- 图片是否表达了指定主题，视觉质量是否达到任务要求。
- MP3 是否语音清晰、内容正确、切分自然。
- MP4 是否画面流畅、音画同步、镜头和叙事符合要求。

简言之，checklist 判断“交付是否满足硬约束”，LLM-as-Judge 判断“交付质量是否满足任务目标”。这种分工让确定性 evaluator 继续发挥门控作用，同时避免把所有复杂质量问题塞进关键词检查。

## 2. 按产物类型设计的 LLM-as-Judge

统一入口位于 `dumatebench/evaluator/llm_judge/unified.py`。调用方传入 `output_file` 后，系统根据文件后缀选择对应 judge。当前路由如下：

| 产物类型 | 后缀 | Judge |
|---|---|---|
| PPT | `ppt`, `pptx` | `llm_judge.ppt` |
| 文本文档 | `doc`, `docx`, `md`, `txt`, `json`, `html`, `htm` | `llm_judge.runner` |
| Excel | `xls`, `xlsx`, `xlsm`, `xltx`, `xltm` | `excel_llm_judge` |
| PDF | `pdf` | `llm_judge_pdf` |
| 图片 | `png`, `jpg`, `jpeg`, `webp` | `llm_judge_image` |
| 音频 | `mp3`, `wav`, `m4a`, `flac`, `aac`, `ogg` | `llm_judge_mm` |
| 视频 | `mp4`, `mov`, `webm`, `mkv` | `llm_judge_mm` |
| 代码 | `py`, `js`, `ts`, `tsx`, `java`, `go`, `rs`, `cpp`, `c`, `sh`, `sql` 等 | `llm_judge_code` |

### 2.1 通用流程

LLM-as-Judge 的主流程是：

1. 读取任务目录中的 `instruction.md`。
2. 读取 checklist 结果，通常来自 `run_outputs/reward.json`。
3. 根据 `output_file` 推断产物类型。
4. 收集候选产物、workspace/reference 材料和 checklist 摘要。
5. 生成或读取任务专属 rubric。
6. 调用支持 JSON 输出的 LLM，对候选产物逐项评分。
7. 将 judge 原始分数归一化为 0 到 1。
8. 写出统一报告 `run_outputs/llm_judge_score.json`。
9. 与 checklist 分数合并，写出 `run_outputs/reward_with_llm_judge.json`。

批量运行时，`dumatebench/scripts/run_task_batch.py` 会在 agent 和 checklist evaluator 结束后主动调用 unified judge。它会先从 checklist detail 或 `evaluator/checks.yaml` 推断预期产物路径。若产物存在，则运行 LLM-as-Judge；若产物不存在，则 judge 分数记为 0，并在最终报告中标记 `missing_artifact`。

统一入口默认把任务目录下的 `workspace_seed` 作为 `reference_dir`。这些文件是 agent 执行任务时可见的初始 workspace，可以作为数据来源、改动范围、ground truth 或用户偏好的证据。调用方也可以通过 `reference_dir` 指定其他目录，或传 `reference_dir: "-"` 禁用 reference 输入。

调用方也可以固定每个 task 的 LLM judge criteria，避免每次由模型重新生成 rubric。统一入口支持在 `args` 中传 `criteria`、`task_rubrics`、`rubric` 对象，或传 `criteria_file` / `rubric_file` 路径；batch 脚本对应参数为 `--llm-judge-criteria-file`。文件可以是 `{"criteria":[...]}` 或 `{"task_rubrics":[...]}`。提供固定 criteria 后，judge 直接基于输入 criteria 和 `reference_dir` 证据评分，不再执行 rubric 生成调用。

如果一个 task 产生多个文件，调用方可以显式固定待评估产物列表，避免从 checklist 或 instruction 中自动推断。`run_task_batch.py` 和 `run_llm_judge_only.py` 支持 `--llm-judge-artifacts-file evaluator/llm_judge_artifacts.json`。manifest 可以是字符串数组，也可以是对象数组；每个产物项支持 `id`、`path`/`output_file`、`type`/`artifact_type`、`criteria_file`/`rubric_file`、`reference_file`/`references_file`、`reference_dir`、`web_reference_dir` 和额外 `judge_args`。manifest 顶层的 `criteria_file`、`reference_file`、`reference_dir` 等字段作为默认值，单个产物项可覆盖。criterion 与产物的对应关系由产物项上的 `criteria_file` 或内联 `criteria` 决定；未指定时继承命令行全局 `--llm-judge-criteria-file` 或 task 默认 `evaluator/llm_judge_criteria.json`。reference 推荐用 `reference_file` 固定为文件清单：JSON 形如 `{"references":[{"path":"workspace_seed/source.pdf","as":"source.pdf"}]}`，也支持纯文本每行一个路径；运行时会物化成临时 reference 目录供各类型 judge 共用。

为了减少噪声，系统会先根据 `instruction.md` 选择任务真正需要的输入文件：如果 instruction 明确提到文件相对路径、文件名或文件 stem，则优先把这些文件物化到 `.llm_judge_selected_references/<judge>/`，并只把这批文件传给 judge；如果 instruction 没有明确点名文件，则保留原始 `reference_dir`，不根据 `annotation_review.json`、输出文件名或同类型后缀做强行猜测。选择报告会记录在 judge 报告的 `reference_selection` 字段中，便于排查。

reference 覆盖范围不是“PPT judge 只看 PPT reference、Excel judge 只看 Excel reference”。统一 reference summary 会尽量解析 instruction 指向的多类型输入文件：文本类文件读取内容摘录，PPT 抽取页数和文本摘要，Excel 抽取 workbook/sheet/单元格/公式证据，PDF 抽取文本和页面结构，图片与音视频保留可传输清单或附件证据。各专用 judge 仍会深读自己最擅长的产物类型，但不会因为产物类型不同而提前丢掉任务所需的 reference 文件。

### 2.2 PPT Judge

PPT judge 针对 `.ppt` 和 `.pptx`。它会读取任务说明、workspace/reference 摘要、输入文件和输出文件。若调用方没有显式提供 `input_file`，它会优先从 reference 中选择 instruction 明确提到的 PPT 文件作为原始输入；同时 workspace/reference summary 会保留其他被 instruction 指向的跨类型材料。PPT judge 会抽取演示文稿的文本、页数、版式摘要，并可选择渲染幻灯片图片作为视觉证据。

评分重点包括：

- 是否遵循任务要求。
- 内容是否正确和完整。
- 是否保留原始信息。
- 布局是否清晰。
- 视觉设计是否一致。
- 是否存在明显无关修改。

PPT judge 的报告默认写入：

```text
run_outputs/ppt_llm_judge.json
```

PPT judge 当前使用和其他开放式产物一致的 criterion-level rubric：先生成或读取 3 到 16 条原子 `task_rubrics`，每条包含 `weight`、`evidence_required` 和 0/1/2/3/4 五档 `levels`；再输出逐条 `criteria_results`，包含整数 `score`、`evidence`、`rationale` 和 `confidence`。`evidence_required=true` 只作为评分时的 prompt 行为约束，要求 judge 主动寻找 reference、正确答案或 ground truth 证据并记录找到或缺失的情况，不在程序层自动改分或改置信度。旧版 dimension-level `dimensions[].score` 报告仍可被解析器兼容读取。

### 2.3 文本文档 Judge

文本 judge 覆盖 `doc`、`docx`、`md`、`txt`、`json`、`html` 和 `htm`。它会从输出目录收集文本证据，并从 instruction 指向的 reference 文件中读取可解析文本证据，再基于任务说明生成 rubric。

评分重点包括：

- 内容是否覆盖任务要求。
- 事实、数字和结论是否合理。
- 结构是否清楚。
- 语言表达是否适合目标场景。
- JSON 或 HTML 等结构化文本是否符合预期语义。

该 judge 使用两阶段流程：先生成 rubric，再评价候选产物。rubric 中的每个 criterion 都有权重、等级描述和证据要求。

### 2.4 Code Judge

Code judge 覆盖常见代码文件后缀，如 `py`、`js`、`jsx`、`ts`、`tsx`、`java`、`go`、`rs`、`cpp`、`c`、`h`、`sh`、`sql` 等。它会读取候选代码文本和 reference 中的接口说明、测试期望、schema、正确答案或 ground truth。除 `DIMENSIONS` 使用代码专属维度外，criterion 格式、judge 输出格式、`evidence_required` 行为和加权聚合方式都与其他统一 LLM judge 保持一致。

代码专属维度包括：

- `functional_correctness`
- `bug_risk_defect`
- `reference_fidelity`
- `repo_integration`
- `regression_safety`
- `edge_case_robustness`
- `algorithmic_efficiency`
- `maintainability_readability`
- `security_safety`

每条 criterion 仍包含 `weight`、`evidence_required` 和 0 到 4 五档 `levels`；judge 对每条 criterion 输出 `score`、`evidence`、`rationale` 和 `confidence`，再按权重聚合为 0 到 1 的统一 `judge_score`。

### 2.5 Excel Judge

Excel judge 覆盖 `xls`、`xlsx`、`xlsm`、`xltx` 和 `xltm`。它会汇总候选 workbook 的 sheet、单元格内容、公式、结构、workspace/reference 摘要和相关文本证据。若 reference 中包含 Excel 输入文件，会额外生成 reference workbook summary；若任务输入是 Word、PDF、PPT、JSON、图片或音视频等其他类型，则通过 workspace/reference summary 提供对应解析摘要或清单证据。编辑、清洗、补全、格式调整和公式修复类任务可以用这些 reference evidence 判断改动前状态和 ground truth。

评分重点包括：

- sheet 和表格结构是否符合任务。
- 数据是否完整、合理。
- 公式和计算逻辑是否正确。
- 输出是否符合业务场景。
- 表格可读性和组织方式是否清楚。

Excel judge 的详细报告默认写入：

```text
run_outputs/excel_llm_judge/judge_result.json
```

Excel judge 也使用 criterion-level rubric：`task_rubrics` 是原子标准列表，每条有权重、证据要求和 0 到 4 五档描述；`criteria_results` 对每条 criterion 给整数 0 到 4 分、证据、理由和置信度。系统按权重聚合为 0 到 1 的 `overall_score` 和统一 `judge_score`。旧版 dimension-level `dimension_scores` 仍作为兼容输入读取。

### 2.6 PDF Judge

PDF judge 覆盖 `.pdf`。它会读取候选 PDF 文本、页数、页面字符统计，并用 PyMuPDF 将页面渲染为图片，作为视觉证据。reference 侧会优先使用 instruction 指向的文件；PDF/text reference 会被 PDF judge 直接读取，其他类型 reference 会通过统一 workspace/reference summary 提供给上层报告和相关 judge。

评分重点包括：

- 内容是否完整。
- 页面顺序和文档结构是否合理。
- 文字、表格和图像是否可读。
- 是否存在裁切、重叠、溢出或异常空白。
- 编辑类任务是否只修改指定内容。

PDF judge 的详细报告默认写入：

```text
run_outputs/pdf_llm_judge/judge_result.json
```

PDF judge 需要 `pypdf` 和 `PyMuPDF`。若依赖缺失或 PDF 无法解析，系统会写结构化失败报告，judge 分数为 0。

### 2.7 图片 Judge

图片 judge 覆盖 `png`、`jpg`、`jpeg` 和 `webp`。它会读取候选图片和 instruction 指向的 reference 图片，检查文件大小、格式、可传输性，并将图片以 `image_url` data URL 或 URL 的方式传给支持视觉输入的模型。若任务还需要文档、表格、PPT 或 PDF 作为参考，统一 workspace/reference summary 会进入图片 judge 的文本 prompt。

评分重点包括：

- 图片是否符合任务主题和内容要求。
- 视觉构图、层级和主体是否清楚。
- 图片是否有明显缺失、损坏、拉伸、裁切或异常遮挡。
- 若任务要求参考图一致性，是否保留关键视觉元素。

图片 judge 的详细报告默认写入：

```text
run_outputs/image_llm_judge/judge_result.json
```

图片 judge 的原始加权分数为 0 到 4，统一入口会除以 4 归一化到 0 到 1。

### 2.8 音视频多模态 Judge

音视频多模态 judge 覆盖音频和视频：

- 音频：`mp3`、`wav`、`m4a`、`flac`、`aac`、`ogg`
- 视频：`mp4`、`mov`、`webm`、`mkv`

它会收集候选媒体和 instruction 指向的 reference 文件的 MIME 类型、大小、路径和传输状态。文本类 reference 会作为内容摘录进入 prompt；媒体 reference 不会被当作文本读取，而是按配置使用 data URL、URL 或视频抽帧方式传输。视频默认使用抽帧模式，需要运行环境提供 `ffmpeg` 和 `ffprobe`；也可以通过配置切换为 URL 传输模式。

评分重点包括：

- 音频是否清晰、内容是否正确、切分是否自然。
- 视频是否流畅、音画是否同步、镜头是否连贯。
- 多模态内容是否整体满足任务目标。
- 缺少可靠 reference 或 golden 时，相关 criterion 是否需要人工复核。

多模态 judge 的详细报告默认写入：

```text
run_outputs/multimodal_llm_judge/judge_result.json
```

多模态 judge 使用 `PyYAML` 读取 `checks.yaml`，以避免 rubric 重复评价文件存在、格式有效等机械检查。

## 3. 分数汇总方式

系统将 complete pass、partial pass 和 LLM-as-Judge 分数归一化到 0 到 1，然后按固定比例计算最终分数。

Checklist 的等权分数来自 `partial_pass`：

```text
checklist_score = passed_check_count / total_check_count
```

若所有 checklist 都通过，`complete_pass = 1`；否则为 0。`partial_pass` 保留部分通过的比例。

LLM-as-Judge 的分数根据 judge 类型做归一化：

- PPT judge 对原子 criterion 使用 0 到 4 整数分，按权重聚合为 0 到 100 分后除以 100。
- 文本 judge 返回 conservative score，归一化到 0 到 1。
- Code judge 对原子 criterion 使用 0 到 4 分，按权重聚合为 0 到 1 的 conservative score。
- Excel judge 对原子 criterion 使用 0 到 4 整数分，按权重聚合为 0 到 1 的 `overall_score`。
- PDF judge 的 conservative score 是 0 到 100，除以 100。
- 图片 judge 的 weighted score 是 0 到 4，除以 4。
- 音视频多模态 judge 对原子 criterion 使用 0 到 4 整数分，conservative score 是 0 到 1，直接使用。

最终分数计算公式为：

```text
final_score = 0.3 * complete_pass + 0.3 * checklist_score + 0.4 * llm_judge_score
```

统一报告 `run_outputs/llm_judge_score.json` 包含：

```json
{
  "artifact_type": "pdf",
  "judge_kind": "pdf",
  "checklist_score": 0.8,
  "judge_score": 0.7,
  "final_score": 0.52,
  "pass": false,
  "rule_result": {},
  "judge_report": {}
}
```

最终评分文件 `run_outputs/reward_with_llm_judge.json` 在原 checklist reward 基础上增加：

```json
{
  "base_complete_pass": 0,
  "base_partial_pass": 0.8,
  "llm_judge_score": 0.7,
  "final_score": 0.52,
  "complete_pass_with_llm_judge": 0,
  "partial_pass_with_llm_judge": 0.52
}
```

默认通过阈值为 0.7，可通过 `min_final_score` 调整。批量脚本中即使 agent 没有产物，系统也会写出最终分数文件。此时 LLM judge 分数为 0，`llm_judge.status` 会说明原因，例如 `missing_artifact`、`failed` 或 `not_run`。

## 4. 输出文件与故障处理

主要输出文件如下：

| 文件 | 作用 |
|---|---|
| `run_outputs/reward.json` | checklist evaluator 的原始结果 |
| `run_outputs/llm_judge_score.json` | unified LLM-as-Judge 总报告 |
| `run_outputs/reward_with_llm_judge.json` | checklist 与 LLM-as-Judge 合并后的最终分数 |
| `run_outputs/ppt_llm_judge.json` | PPT judge 详细报告 |
| `run_outputs/excel_llm_judge/judge_result.json` | Excel judge 详细报告 |
| `run_outputs/pdf_llm_judge/judge_result.json` | PDF judge 详细报告 |
| `run_outputs/image_llm_judge/judge_result.json` | 图片 judge 详细报告 |
| `run_outputs/multimodal_llm_judge/judge_result.json` | 音频、视频 judge 详细报告 |
| `run_outputs/llm_judge_score.json` | Code judge 详细结果嵌入统一报告的 `judge_report` |

LLM-as-Judge 失败不会阻断最终分数产出。系统会写结构化失败报告，并将 `llm_judge_score` 置为 0。常见失败原因包括模型 API 超时、缺少 `OPENAI_API_KEY`、缺少 PDF 渲染依赖、媒体超过大小限制、provider 不支持音频或视频 payload。

这种处理方式保证批量评估总能落盘最终结果，同时把失败原因保留在报告中，便于后续排查。

## 5. 运行方式

单个 evaluator 可通过 `evaluate_llm_judge_score` 或 `run_llm_judge_score` 调用 unified judge。批量任务推荐使用：

```bash
python3 dumatebench/scripts/run_task_batch.py \
  --tasks-dir dumatebench/datasets/dev \
  --template-task dumatebench/datasets/dev/odyssey_2_12_smoke \
  --task-glob '*' \
  --max-steps 20
```

若只测试 batch 框架，不希望调用 LLM-as-Judge，可加：

```bash
--skip-llm-judge
```

批量结果可用以下脚本统计：

```bash
python3 dumatebench/scripts/summarize_llm_judge_rewards.py <tasks-dir> --summary-only
```

该统计脚本会递归查找 `run_outputs/reward_with_llm_judge.json`，支持 task 目录中间多一层分组目录的情况。
