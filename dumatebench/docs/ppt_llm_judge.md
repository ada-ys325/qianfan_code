# PPT LLM-as-Judge

本文说明 DuMateBench 当前 PPT LLM-as-judge evaluator 的设计和使用方式。读者是需要运行、调试或扩展 PPT 评测的 DuMateBench 开发者。

PPT LLM judge 是确定性检查之上的进阶评估器。确定性检查负责回答“文件是否存在、格式是否有效、输出目录是否干净”；LLM judge 负责回答“这个 PPT 是否真的完成了用户要求，内容是否保留，布局和视觉效果是否变好”。

## 背景：如何 follow 相关 benchmark 的 LLM judge

现有实现主要 follow 相关 PPT benchmark 的 LLM judge 流程，而不是照搬某一个 benchmark 的完整任务设置。

PPTBench 的 LLM judge 用在开放生成类任务上。此类任务没有唯一标准答案，因此 benchmark 不只比较文件名或固定 reference。它先把 PPT 转成 judge 可读的证据，包括 slide screenshot 和结构化 slide JSON，再让 judge 根据任务要求和证据给 0 到 5 分。DuMateBench follow 了这个思路：judge 不直接“看路径”，而是读取 PPT 结构摘要，并在工具可用时加入渲染后的 slide 图片。

PPT-Eval 的 LLM judge 更强调 task-specific rubric。它把一个 PowerPoint 编辑任务拆成多个可评分节点，允许 partial credit，并惩罚无关修改、破坏性修改和差审美。DuMateBench follow 了这个思路：prompt 先要求 judge 从通用维度生成任务专属的原子 `task_rubrics`，再对每条 criterion 给 0 到 4 整数分。也就是说，通用维度提供稳定框架，具体任务决定每条原子标准评什么。

PresentBench 和 SlideBench 类评测强调 evidence surface 和结构化评分。它们的共同流程是：先把演示文稿转换为 judge 可读证据，再让 judge 按 rubric 输出可复查的评分结果。DuMateBench follow 了这个流水线：准备证据、构造 prompt、调用 judge model、解析 JSON、写出报告。

当前实现形成了一个混合方案：基础 checklist 先做硬性门控；PPT LLM judge 再用结构摘要、渲染图片和 task-specific rubric 评估质量；最终报告同时保留基础分、judge 分和合并分。

## 当前实现位置

核心模块：

```text
dumatebench/evaluator/llm_judge/ppt.py
```

主要入口：

- `run_pptx_judge(...)`：运行一次 PPT judge，并写出 JSON 报告。
- `evaluate_pptx_llm_judge(testbed_dir, args) -> bool`：供 `checks.yaml` 调用的 evaluator 函数。
- CLI：`python -m dumatebench.evaluator.llm_judge.ppt ...`

辅助脚本：

- `dumatebench/scripts/run_ppt_llm_judge_task_agent.sh`：完整运行 Docker agent、基础 checklist 和 PPT judge。
- `dumatebench/scripts/run_ppt_llm_judge_only.sh`：只运行 PPT judge，不构建 Docker，不运行 agent，不跑基础 checklist。

默认模型是 `gpt-4o`。可以通过 `DUMATE_JUDGE_MODEL` 或 CLI 的 `--model` 覆盖。API 通过环境变量读取：

```bash
export OPENAI_BASE_URL="https://cn.huayanapi.com:27502/v1"
export OPENAI_API_KEY="..."
```

API key 不应写入代码、文档、任务文件或日志。

## 评估流程

一次 PPT judge 的流程如下：

1. 读取任务目录、instruction、输入 PPT 和输出 PPT。
2. 检查输出 PPTX 是否存在且结构有效。
3. 如果输出文件缺失或不可读，直接写失败报告，不调用 LLM。
4. 使用 `python-pptx` 提取输入和输出 PPT 的结构摘要。
5. 如果 `render_slides=true` 且宿主机有 `soffice` 和 `pdftoppm`，把输入和输出 PPT 渲染成 slide PNG。
6. 构造 OpenAI-compatible chat messages。
7. 要求 judge 根据通用维度生成 task-specific atomic `rubric_items`。
8. 要求 judge 对每条原子 criterion 给 0 到 4 整数分，并输出 slide-level evidence。
9. 解析 judge 返回的 JSON。
10. 写出 `ppt_llm_judge.json`，并在存在 `reward.json` 时写出合并结果。

缺失输出文件是 critical failure。此时报告示例为：

```json
{
  "score": 0.0,
  "pass": false,
  "dimensions": [],
  "critical_failures": ["missing or unreadable output PPTX"],
  "summary": "The output PowerPoint file is missing or invalid."
}
```

## 评估证据

Judge 会收到三类证据。

第一类是任务文本：

- `instruction.md` 的内容；
- 输入文件路径；
- 输出文件路径。

第二类是 PPT 结构摘要。该摘要由 `python-pptx` 提取，包含：

- slide 数量；
- 每页文本；
- shape 数量；
- shape 名称和类型；
- shape 的近似位置和尺寸；
- 文本 run 的字体、字号、粗体、斜体和颜色线索。

第三类是渲染图片。当前实现使用两步渲染：

1. `soffice --headless --convert-to pdf` 把 PPTX 转为 PDF；
2. `pdftoppm` 把 PDF 页面转为 PNG。

渲染图片会作为 multimodal message 中的 `image_url` 传给模型。默认最多渲染 8 页，可以用 `--max-rendered-slides` 或 `DUMATE_MAX_RENDERED_SLIDES` 修改。

## `render_status` 的含义

报告中的 `render_status` 描述渲染证据是否可用：

```json
"render_status": {
  "input": {
    "status": "skipped",
    "reason": "soffice not found",
    "image_count": 0
  },
  "output": {
    "status": "skipped",
    "reason": "soffice not found",
    "image_count": 0
  }
}
```

其中：

- `input` 指原始 PPT，例如 `workspace_seed/uploads/演示文稿9.pptx`。
- `output` 指 agent 产物，例如 `run_outputs/pptx/演示文稿9_优化.pptx`。
- `status=rendered` 表示已生成 slide 图片。
- `status=skipped` 表示没有尝试或不能渲染。
- `status=failed` 表示尝试渲染但失败。

如果原因是 `render_slides is false`，说明调用时显式关闭了图片渲染。如果原因是 `soffice not found` 或 `pdftoppm not found`，说明宿主机缺系统命令。此时 judge 会降级为 structure-only mode。它仍能评估页数、文本、shape 和部分样式，但视觉布局、美观、遮挡和裁切的判断会弱很多。

## 宿主机依赖

Python 依赖在 `dumatebench/requirements.txt` 中：

```bash
conda run -n dumatebench python -m pip install -r dumatebench/requirements.txt
```

PPT 图片渲染还需要宿主机系统依赖。它们不能由 pip 安装：

- LibreOffice，提供 `soffice`；
- Poppler，提供 `pdftoppm`。

macOS 安装示例：

```bash
brew install --cask libreoffice
brew install poppler
export PATH="/Applications/LibreOffice.app/Contents/MacOS:$PATH"

soffice --version
pdftoppm -v
```

Linux 安装示例：

```bash
apt-get update
apt-get install -y libreoffice poppler-utils
```

如果这些命令缺失，judge 不会中断，但报告会显示渲染状态为 `skipped`。

## Rubric 设计

当前实现使用固定 general dimensions 作为骨架：

- `instruction_following`：是否遵循显式任务要求。
- `content_correctness`：事实、文字、数字和结论是否正确。
- `content_preservation`：编辑类任务是否保留源 PPT 的核心内容。
- `text_quality`：文字是否通顺，是否没有明显错别字、标点错误和表达问题。
- `layout_and_readability`：元素是否对齐、可读，是否没有溢出或重叠。
- `visual_design`：PPT 是否美观，视觉设计是否适合任务场景。
- `professional_consistency`：字体、配色、间距和整体风格是否一致。
- `no_unnecessary_changes`：agent 是否避免无关修改或破坏性编辑。

这些维度不是最终的细粒度评分点。Prompt 会要求 judge 先从这些维度生成 task-specific atomic `task_rubrics`。每条 rubric 必须包含 `weight`、`critical`、`evidence_required` 和 0/1/2/3/4 五档描述。
`critical=true` 的 rubric 在归一化前有效权重会乘以 2。

以当前 PPT 优化任务为例，judge 可以把维度展开成：

- `instruction_following`：是否输出到指定路径；是否保留 3 页；是否覆盖文字审查、布局调整和美化。
- `content_preservation`：是否保留原有日期、人员、图示关系和核心内容。
- `layout_and_readability`：是否改善对齐、间距、重叠、溢出和可读性。
- `visual_design`：字体、配色、字号和整体风格是否更专业。
- `no_unnecessary_changes`：是否避免新增无关页面、复制原始文件到输出目录或破坏原 deck。

最终报告仍按维度给分。这样做有两个目的：报告结构稳定，扣分原因也能追溯到具体 item。

## Prompt 要求

系统角色要求模型作为严格 PPT evaluator，只能根据 task instruction 和提供的 PPT evidence 判断。它不能假设看不到的内容，也不能因为文件名正确就给高分。

用户消息包含一个 JSON payload：

- `task_instruction`；
- `general_dimensions`；
- `expected_json_schema`；
- `input_ppt_summary`；
- `output_ppt_summary`；
- `render_status`；
- `scoring_rules`。

如果 slide 图片渲染成功，消息中还会包含输入和输出 PPT 的 slide images。

Scoring rules 要求：

- 每条原子 criterion 使用 0 到 4 整数分。
- 使用 `rubric_items` 表达任务专属原子标准。
- 不要把互不相关的要求压缩成一个 item。
- 最终分数是 0 到 100 的加权分。
- 如果存在 critical failure，`pass` 必须为 `false`。

## Judge 输出 JSON

Judge 必须返回 JSON object。当前 schema 为：

```json
{
  "score": 0,
  "pass": false,
  "dimensions": [
    {
      "name": "layout_and_readability",
      "weight": 0.2,
      "score": 0,
      "rubric": "short summary of this dimension-specific rubric",
      "rubric_items": [
        {
          "criterion": "atomic task-specific requirement",
          "full_credit": "what a score-5 answer satisfies",
          "partial_credit": "what earns partial credit",
          "failure": "what loses credit",
          "evidence": "specific slide-level evidence"
        }
      ],
      "evidence": "brief overall slide-level evidence for the dimension"
    }
  ],
  "critical_failures": [],
  "summary": "short explanation"
}
```

字段含义：

- `score`：0 到 100 的总分。
- `pass`：是否通过。解析器会按 `score >= min_score` 且无 critical failure 重新计算。
- `dimensions`：维度级评分列表。
- `task_rubrics[].weight`：原子 criterion 的基础权重；若 `critical=true`，归一化前有效权重乘以 2。
- `task_rubrics[].levels`：该 criterion 的 0 到 4 五档描述。
- `criteria_results[].score`：该 criterion 的 0 到 4 整数分。
- `criteria_results[].evidence`：支持该 criterion 评分的页级证据。
- `critical_failures`：阻断性失败。
- `summary`：简短结论。

如果 judge 没有返回 `score`，解析器会用 `dimensions[].weight` 和 `dimensions[].score` 聚合：

```text
score = sum(score_0_to_5 * weight) / (5 * sum(weight)) * 100
```

如果 judge 返回 malformed JSON，或 API 调用失败，报告会写入失败信息：

```json
{
  "score": 0.0,
  "pass": false,
  "dimensions": [],
  "critical_failures": ["PPT LLM judge failed: ..."],
  "summary": "The PPT LLM judge could not complete. See critical_failures for details."
}
```

## 分数和合并结果

PPT judge 自身使用百分制。默认通过阈值是 70：

```text
pass = score >= min_score and critical_failures is empty
```

如果任务目录中已经有基础 evaluator 写出的 `run_outputs/reward.json`，PPT judge 会额外写出合并结果。合并公式为：

```text
base_score = reward.partial_pass
judge_score = ppt_llm_judge.score / 100
final_score = 0.3 * reward.complete_pass + 0.3 * base_score + 0.4 * judge_score
```

合并结果默认写到：

```text
run_outputs/reward_with_ppt_judge.json
```

其中包含：

- `base_complete_pass`
- `base_partial_pass`
- `ppt_llm_judge_score`
- `final_score`
- `complete_pass_with_ppt_judge`
- `partial_pass_with_ppt_judge`

注意，`complete_pass_with_ppt_judge` 当前使用 `final_score >= min_score/100` 判断。因此即使基础 checklist 没有 `complete_pass`，只要合并分达到阈值，该字段也可能为 1。需要更严格门控时，可以后续改成“基础 complete pass 且 judge pass”。

## 运行完整任务

完整脚本会运行 Docker agent、基础 checklist 和 PPT judge：

```bash
export OPENAI_BASE_URL="https://cn.huayanapi.com:27502/v1"
export OPENAI_API_KEY="..."
export DUMATE_MODEL="gpt-4o"
export DUMATE_JUDGE_MODEL="gpt-4o"

dumatebench/scripts/run_ppt_llm_judge_task_agent.sh --max-steps 25
```

默认任务 ID：

```text
24d6778af4354ccbbd19ee5a5e529beb_ses_124aedee2ffeZgWPdornLUhXG6
```

默认输入和输出：

```text
workspace_seed/uploads/演示文稿9.pptx
run_outputs/pptx/演示文稿9_优化.pptx
```

完整脚本输出：

- `run_outputs/reward.json`：基础 checklist 结果。
- `run_outputs/ppt_llm_judge.json`：PPT judge 报告。
- `run_outputs/reward_with_ppt_judge.json`：合并结果。
- `run_logs/agent_llm.log`：agent 运行日志。
- `run_logs/docker_build.log`：Docker build 日志。

基础 checklist 的失败会让 `evaluator.py` 返回非 0。若脚本在未激活 conda 环境时使用 `conda run`，conda 可能额外打印 `ERROR conda.cli.main_run`。真正原因应看 `reward.json` 中每个 check 的 `passed` 字段。

## 只运行 PPT judge

如果已有 agent 输出，可以只测试 PPT judge：

```bash
export OPENAI_BASE_URL="https://cn.huayanapi.com:27502/v1"
export OPENAI_API_KEY="..."
export DUMATE_JUDGE_MODEL="gpt-4o"

dumatebench/scripts/run_ppt_llm_judge_only.sh
```

该脚本不构建 Docker，不运行 agent，不跑基础 checklist。默认输出：

```text
run_outputs/ppt_llm_judge_only.json
run_outputs/reward_with_ppt_judge_only.json
```

如果暂时不想渲染 slide 图片：

```bash
export DUMATE_RENDER_SLIDES=false
dumatebench/scripts/run_ppt_llm_judge_only.sh
```

如果输出 PPT 文件名不是默认值：

```bash
export DUMATE_JUDGE_OUTPUT_FILE="run_outputs/pptx/your_output.pptx"
dumatebench/scripts/run_ppt_llm_judge_only.sh
```

离线 smoke test 可以使用 mock response：

```bash
export DUMATE_RENDER_SLIDES=false
export DUMATE_JUDGE_MOCK_RESPONSE='{"score":85,"critical_failures":[],"dimensions":[],"summary":"mock"}'
dumatebench/scripts/run_ppt_llm_judge_only.sh
```

## 直接使用 CLI

也可以直接调用 Python module：

```bash
python -m dumatebench.evaluator.llm_judge.ppt \
  --task-dir "dumatebench/datasets/dev/24d6778af4354ccbbd19ee5a5e529beb_ses_124aedee2ffeZgWPdornLUhXG6" \
  --instruction-file instruction.md \
  --input-file "workspace_seed/uploads/演示文稿9.pptx" \
  --output-file "run_outputs/pptx/演示文稿9_优化.pptx" \
  --model gpt-4o \
  --min-score 70 \
  --judge-output-file "run_outputs/ppt_llm_judge.json"
```

可用参数：

- `--task-dir`：任务目录。
- `--instruction-file`：instruction 文件，默认 `instruction.md`。
- `--input-file`：原始 PPT，相对任务目录或绝对路径。
- `--output-file`：agent 输出 PPT，必填。
- `--model`：judge 模型，默认使用 `OPENAI_MODEL` 或 `gpt-4o`。
- `--min-score`：通过阈值，默认 70。
- `--judge-output-file`：judge 报告输出路径。
- `--combined-reward-file`：合并结果输出路径。
- `--no-render-slides`：关闭 slide 图片渲染。
- `--max-rendered-slides`：最多渲染页数，默认 8。
- `--mock-response`：离线测试用的 JSON 字符串。

## `checks.yaml` 集成

可以把 PPT judge 作为 evaluator 函数接入 `checks.yaml`：

```yaml
- id: ppt_llm_judge
  type: evaluate_pptx_llm_judge
  weight: 0.5
  args:
    instruction_file: instruction.md
    input_file: workspace_seed/uploads/演示文稿9.pptx
    output_file: run_outputs/pptx/演示文稿9_优化.pptx
    model: gpt-4o
    min_score: 70
    judge_output_file: run_outputs/ppt_llm_judge.json
    combined_reward_file: run_outputs/reward_with_ppt_judge.json
    render_slides: true
    max_rendered_slides: 8
```

当前 PPT 测试任务没有把 LLM judge 直接写入 `checks.yaml`。默认做法是先跑基础 checklist，再由脚本单独运行 PPT judge。这样可以清楚地区分规则型失败和 LLM judge 评分。

## 常见问题

### `render_status` 是 `skipped`

看 `reason` 字段。

- `render_slides is false`：调用时关闭了渲染。
- `soffice not found`：宿主机没有 LibreOffice，或 PATH 中没有 `soffice`。
- `pdftoppm not found`：宿主机没有 Poppler。

这不是 fatal error。它只表示 judge 没有 slide 图片证据。

### `missing or unreadable output PPTX`

目标输出文件不存在或不是有效 PPTX。对于当前任务，目标路径是：

```text
run_outputs/pptx/演示文稿9_优化.pptx
```

如果 agent 只生成了 `run_outputs/pptx/演示文稿9.pptx`，基础 checklist 和 PPT judge 都会失败。

### `no_extra_output_files` 失败

输出目录里有额外文件。当前 checklist 只允许：

```text
run_outputs/pptx/演示文稿9_优化.pptx
```

如果 agent 把原始 PPT、临时文件或其他产物放进 `run_outputs`，该 check 会失败。

### `openai>=1.x is required`

宿主机运行 judge 的 Python 环境缺少 OpenAI SDK。安装：

```bash
conda run -n dumatebench python -m pip install -r dumatebench/requirements.txt
```

如果默认 PyPI 访问慢，可以使用镜像源：

```bash
conda run -n dumatebench python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r dumatebench/requirements.txt
```

### Docker build 卡在 `apt-get`

切换 `python:3.12-slim` 只能解决 base image 拉取问题。Dockerfile 里的 `apt-get update` 仍会访问 Debian 源。可以设置：

```bash
export DUMATE_APT_DEBIAN_MIRROR="http://mirrors.aliyun.com/debian"
export DUMATE_APT_SECURITY_MIRROR="http://mirrors.aliyun.com/debian-security"
```

然后重新运行完整脚本。

## 当前限制

第一版 PPT judge 仍有几个限制：

- 图片渲染依赖宿主机的 LibreOffice 和 Poppler。
- `python-pptx` 对复杂图表、嵌入对象和图片语义的理解有限。
- 结构摘要中的位置和样式是近似信息，不能完全替代截图。
- Judge 分数受模型稳定性影响，需要后续做人类校准。
- 合并分由 `complete_pass`、`partial_pass` 和 judge 分按 0.3、0.3、0.4 的比例计算。

这些限制不影响它作为进阶评估器使用，但解释结果时应同时查看 `evidence.render_status`、`critical_failures` 和每个维度的 `evidence`。
