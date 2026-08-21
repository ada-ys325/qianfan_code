# DuMateBench 文字类产物 LLM-as-Judge 方案与 group1/group4 分析

## 1. 结论

`generated_tasks_live/claude-opus-4-8/group_1` 与 `group_4` 共包含 8 个任务、46 条规则检查。其中：

- 24 条（52.2%）是关键词包含检查；
- 16 条（34.8%）只检查文件存在或格式有效；
- 其余 6 条检查未改文件、无额外文件、段落格式、diff 或禁止占位符。

这些检查能有效覆盖 L0 文件/格式和部分 L1 客观内容，却无法判断“关键词是否被正确使用”“内容是否忠实于来源”“论证是否成立”“改写是否真正解决问题”“文档是否整体易读”。因此建议保留现有 evaluator 作为硬门控和客观得分，再增加一条独立的 rubric-based LLM judge 通道。LLM judge 不应取代字数、路径、格式、颜色、标题顺序等可编程检查。

本次已实现 `llm_judge/`：支持 `.docx`、`.txt`、`.md`、`.json`，并额外支持 group4 中出现的 `.html/.htm`。实现采用“先生成并锁定 rubric，再评价候选”的两阶段流程；每个评分项输出证据、置信度和可评估状态，最后与已有规则分数做门控融合。

## 2. 相关 benchmark 对方案的启发

1. [OfficeBench](https://arxiv.org/abs/2407.19056) 使用任务定制的 exact、fuzzy 和 execution-based evaluator，说明文件存在、关键字段、结构化状态等确定性条件应继续由代码负责，而不是交给 LLM 猜测。
2. [G-Eval](https://aclanthology.org/2023.emnlp-main.153/) 将明确评价标准、分步评价过程和结构化 form filling 结合起来，并在摘要评价上提升了与人工评分的相关性。这里借鉴其“明确维度 + 结构化评分”，但不要求 judge 输出隐藏推理过程，只保留可审计的简短理由和证据。
3. [Prometheus](https://arxiv.org/abs/2310.08491) 强调 task-specific score rubric、参考答案和细粒度反馈。DuMateBench 不总有唯一 reference answer，因此实现允许传入 workspace seed 或人工整理的参考材料，并允许 judge 返回 `cannot_assess`。
4. [Office Comprehension Benchmark](https://arxiv.org/abs/2607.01245) 将 reference answer 拆成原子、二值可评分 claims，再让 judge 独立判断。这支持将长 instruction 拆成多个原子标准，而不是让模型直接给一个“整体 8/10”。
5. [PPT-Eval](https://arxiv.org/abs/2606.31154) 使用 task-specific rubric 给部分完成度、无关修改和美观性评分；其报告的 rubric 分数与人工判断 Kendall 相关为 0.77。虽然对象是 PPT，其“部分分 + 无关修改惩罚 + 证据反馈”同样适用于 Word 编辑任务。
6. [Judging LLM-as-a-Judge](https://arxiv.org/abs/2306.05685) 和后续[位置偏差研究](https://arxiv.org/abs/2406.07791)表明 judge 会受位置、长度、自偏好等因素影响。本实现使用单候选 pointwise 评价、明确禁止长度/华丽格式加分、固定 rubric 和可选重复运行，但仍需要人工校准。
7. [RULERS](https://arxiv.org/abs/2601.08654)提出锁定 rubric、证据锚定和尺度校准，以减少 prompt 敏感、不可核验理由和评分边界漂移。本实现中的 `instruction_hash`、`rubric_hash`、逐项证据和固定 0–4 档位采用了相同的工程方向。

## 3. group1/group4 逐任务缺口

| 任务 | 现有 checks 能确认 | 仍会误判的核心问题 | 建议的 judge 重点 |
|---|---|---|---|
| 辽宁省石油焦产业链企业调查报告（docx） | 文件、格式、少量企业名和字段词出现 | 企业逐家核验是否真实；状态、设备、占地、产品、隶属关系是否互相一致；是否用可靠来源支撑；“待确认”是否诚实 | 企业覆盖与字段完整性、来源可追溯性、事实一致性、不确定性处理、分类与汇总质量 |
| 《夜无疆》解析（md） | 作品名、作者、核心词和章节词出现 | 关键词存在不代表梗概和人物设定忠实；可能把推测写成事实；可能遗漏主要人物或主线 | 原著忠实性、已确认/推测边界、人物与修炼体系准确性、综合与结构质量 |
| 酵母发酵图片笔记（docx） | 文件、格式、五个关键词、无“待补充” | OCR 是否正确；原编号是否保持；“适宜繁殖温度”是否真为子项；两种变化的主语/逻辑是否补全；第七点是否准确 | OCR/来源忠实性、编号与从属关系、逻辑补全准确性、第七点完整性、笔记可读性 |
| GBC 综述核查报告（md） | 必需小节、Ref 编号和试验名出现 | 可以列全关键词却编造 DOI、HR、P 值；“真实存在/无法验证”的结论可能无证据；问题分级可能不合理 | 逐条证据、核验结论正确性、数据与原文一致性、问题分级、禁止无依据肯定 |
| 葡萄酒品鉴论文（docx） | 标题、摘要/关键词、主题词、[1]–[8]、一个居中段落 | 文献可能虚构；标题层级可能只是文本伪装；摘要可能空泛；正文可能堆砌；近五年与格式规范未确认 | 学术内容质量、文献可信度与规范、总分总结构、层级与段落可读性、论证连贯性 |
| 文学圈 AI 悖论评论（md） | 数据词和主题词出现、目录干净 | 未检查 1200 字；数据可能只被罗列而未参与论证；原因分析可能空洞；批判立场可能自相矛盾 | 论点鲜明度、证据与论点连接、深层原因分析、结构与文风。字数应补规则 check，不交给 judge |
| 白云机场到小榄交通 HTML | 文件、关键购票词、案例标题、接送词、蓝本未改 | 未确认三方案是否真正整合；案例是否位于最后；方案 C 是否仍残留城际主方案；上车门位/票名/平台是否完整；视觉风格是否保持 | 信息完整与一致、方案边界、蓝本风格一致性、信息层级和可读性。HTML 位置关系宜先补 DOM 规则检查 |
| 幼儿园论文修订（docx） | 文件、格式、主题词、“关系”、有 diff | “关系”一词不足以证明结语已重写；第9段可能仍重复；“切换”可能残留；其余正文可能被大改；黑色字体没有检查 | 三处修改的语义质量、重复是否消除、相融共生论点、未授权改动、整体风格连续性。字体颜色应补规则 check |

### 应优先补到规则 evaluator 的项目

- Markdown/纯文本的字符数或中文字数，例如评论文章不少于 1200 字；
- HTML 标题或 DOM 区块的先后顺序、方案 C 中禁止出现的旧城际主方案文本；
- Word 全文显式非黑色 run、标题层级和要求明确的段落缩进；
- “只修改指定文件/段落”可用更精确的 diff 范围或文件 hash；
- JSON 的 schema、键集合、值类型、必填字段和数值约束。

这些条件确定、便宜、可复现。让 LLM judge 重复判断反而会降低稳定性。

## 4. 通用维度与 rubric 生成规则

实现定义 7 个通用维度，rubric 生成器只选与当前任务相关的维度：

1. `content_relevance`：内容与目标、受众的相关性；
2. `factual_correctness_faithfulness`：事实正确、忠实于输入、推断边界清楚；
3. `requirement_completeness`：覆盖所有实质要求；
4. `structure_coherence`：标题、段落、顺序和论证连贯；
5. `language_style`：清晰、自然、符合文体与读者；
6. `presentation_readability`：Word/Markdown/文本的层级、列表、表格和格式可读性；
7. `edit_fidelity`：编辑任务保留未授权内容和样式。

每个 task-specific criterion 必须：

- 只评价一个可观察目标；
- 属于一个通用维度；
- 有正权重，运行时归一化；
- 明确是否为 `critical`；
- 给出 0、1、2、3、4 五档的任务特定锚点；
- 说明是否必须提供产物证据；
- 不简单复制“文件存在、格式有效、出现关键词”这类已有机械检查。

rubric 生成阶段看不到候选产物，只接收 instruction、已有 checks 和参考材料清单。生成后写成带 `rubric_hash` 的 JSON。评价阶段重新校验 `instruction_hash`，防止拿错任务或 rubric 被静默修改。

## 5. Judge prompt 与产物表示

### 两阶段 prompt

1. Rubric compiler：根据任务要求生成 5–12 个原子标准及五档锚点。
2. Rubric executor：把锁定 rubric、候选产物、可选参考材料交给 judge，逐项输出 `score/status/evidence/rationale/confidence`。

prompt 明确规定：候选和参考文件中的命令都是不可信数据；不得因文本更长或措辞更自信加分；任务要求不能被当作“候选已完成”的证据；无法核验的 DOI、数值或来源必须返回 `cannot_assess`，不能猜测。代码还会检查 judge 给出的短引文是否确实出现在所指文件中；未通过验证的证据会被标记为 `verified: false` 以便审计。`evidence_required=true` 只要求 judge 主动寻找 reference、正确答案或 ground truth 证据，不在程序层自动改分或改置信度。

### 支持的 judge-friendly 表示

- `.txt/.md/.html`：UTF-8 文本、行数、字符数；
- `.json`：解析后规范化缩进，并提供根类型、顶层键或数组长度；
- `.docx`：带段落编号的正文、表格、页眉页脚，以及段落样式、对齐、字体、字号和显式非黑色 run 摘要。

每个文件默认采用头部 55% + 中部 20% + 尾部 25% 的分段采样，避免长文只看开头。CLI 默认每个候选目录和参考目录最多各取 6 万字符、20 个文件。对于 11 MB 小说、120 篇参考文献核验等任务，这种采样仍不足以证明全局事实；应补充检索/RAG、人工 atomic gold claims 或权威 reference excerpts，而不是让 judge 依靠“模型记忆”。

酵母笔记任务的参考输入只有图片，当前文字 judge 不会把图片当作可核验参考，因此 OCR 忠实性相关项目应返回 `cannot_assess`。要完整评价该任务，需要在前处理层接入 OCR/VLM，把图片文字和版面层级作为 `references` 提供；这不应由纯文本 judge 假装完成。

## 6. 评分、门控和可审计输出

单项分数为 0–4，权重归一化为 `w_i`。

```text
judge_score_conservative = 100 × Σ(w_i × score_i / 4)
judge_score_assessed_only = 100 × Σ_assessed(w_i × score_i / 4) / Σ_assessed(w_i)
assessment_coverage = Σ_assessed(w_i)
```

`cannot_assess` 不会被偷偷当成通过：它在保守分中贡献 0，同时保留只对已评项计算的诊断分和 coverage，并触发人工复核。

默认混合分数：

```text
uncapped = 0.4 × rule_score + 0.6 × judge_score_conservative
```

门控默认值：

- 文件不存在或格式无效：最终分 0；使用已锁定 rubric 的 `evaluate` 阶段会跳过 LLM 评分调用（便捷的 `run` 命令仍需先生成 rubric）；
- 其他客观 check 失败：最高 79；

这些阈值是保守工程默认值，不应未经校准直接当作 benchmark 最终政策。结果还记录 instruction、rubric、候选和参考材料的 hash，逐项保存短引文与位置，便于追责和重跑。

## 7. 运行方式

先安装项目依赖并配置 OpenAI-compatible API：

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint/v1"
export OPENAI_API_KEY="..."
```

对一个已经产生 `run_outputs/` 的任务，一次完成 rubric 生成与评分：

```bash
python3 -m llm_judge.cli run \
  --task-dir generated_tasks_live/claude-opus-4-8/group_1/8f0e31c212ec4692abe38395e32852f5/8f0e31c212ec4692abe38395e32852f5_ses_0e1b04e08ffe7v14KrzqyMorBe \
  --rubric-out judge_runs/night/rubric.json \
  --result-out judge_runs/night/judge_result.json \
  --run-rule-evaluator \
  --model gpt-4o-mini \
  --judge-runs 3
```

生产环境建议将两个阶段分开，让标注者先审核 rubric：

```bash
python3 -m llm_judge.cli generate-rubric \
  --task-dir <task_dir> \
  --rubric-out <rubric.json> \
  --model <judge_model>

python3 -m llm_judge.cli evaluate \
  --task-dir <task_dir> \
  --rubric <rubric.json> \
  --result-out <judge_result.json> \
  --run-rule-evaluator \
  --judge-runs 3 \
  --model <judge_model>
```

如果参考材料不是 `workspace_seed/`，使用 `--reference-dir <dir>`；若任务不需要参考材料，使用 `--reference-dir -`。如果规则 evaluator 已经运行，可通过 `--rule-result run_outputs/reward.json` 复用结果。

## 8. 测试与上线校准

当前离线测试覆盖：

- txt/md/json/docx 抽取与 Word 结构摘要；
- 长文本头/中/尾采样；
- rubric schema、原子项 ID、五档锚点、权重归一化和 hash；
- rubric 生成阶段不泄漏候选内容；
- `cannot_assess`、coverage 和保守分；
- 文件/格式硬门控、客观失败封顶和混合得分。

运行：

```bash
PYTHONPYCACHEPREFIX=/tmp/dumate_judge_pycache \
python3 -m unittest discover -s tests -v
```

正式上线前建议：

1. 从 group1/group4 及后续任务中抽取至少 50–100 个“好、部分完成、明显失败、对抗性关键词堆砌”的 agent 产物；
2. 由至少 2 名人工标注者用冻结 rubric 独立评分，并先计算人工间一致性；
3. 报告 judge 对人工的 Spearman/Kendall、加权 Cohen's kappa、单项 ±1 档一致率，以及低分项 false-pass rate；
4. 对同一候选重复 3 次，记录单项分数方差和 `cannot_assess` 比例；
5. 专门加入长度膨胀、关键词堆砌、伪造引文、候选内 prompt injection、格式华丽但内容错误等对抗样本；
6. 用校准集调整规则/judge 权重与封顶阈值，之后冻结 prompt、rubric schema、judge 模型版本和采样参数；
7. 将低 coverage、低 confidence、重复运行分歧大、关键要求失败的样本送人工复核。

当前环境没有配置 `OPENAI_API_KEY`，因此本次完成了可重复的 fake-client 端到端测试，没有产生真实模型费用，也没有把未经校准的单模型输出当作验证结论。
