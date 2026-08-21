# DuMateBench MP3/MP4 多模态 LLM-as-Judge 方案与 group2 分析

## 1. 结论

本方案在不修改原 `DataAnnotation` 框架、历史 session 和已有 `checks.yaml` 的前提下，为 DuMateBench 增加独立的 MP3/MP4 多模态评估链路。新增实现位于：

`/mnt/cfs_algo_bj/workspace/sijinhua/benchmark/multimodal_llm_judge`

抽查的三个 `group_2` session 共包含 25 条规则检查：

- 15 条（60.0%）检查文件存在；
- 2 条（8.0%）检查文件格式有效；
- 4 条（16.0%）检查关键词或章节内容；
- 其余 4 条检查目录结构、额外文件或禁止内容。

这些规则适合作为 L0 交付门控，却无法直接判断：

- MP3 是否为中文、发音是否正确、内容是否与文档逐段一致、切分点是否自然；
- MP4 是否卡顿、音画是否同步、镜头是否连续、商业卖点是否真正被表现；
- 视频分析报告是否忠实于原视频，而非只包含规定章节和关键词；
- 缺少可靠参考答案、原视频或事实依据时，模型是否在猜测。

因此采用“确定性 checks + 多模态 LLM judge + 人工 golden review”三级协议：

1. `checks.yaml` 继续负责文件存在、格式、目录、关键词等机械条件；
2. 多模态 judge 负责直接听看后的内容、可懂度、同步、连续性、技术质量和任务完成度；
3. 依赖缺失 golden 的事实或对照项不在 rubric 中绑定 reference；自动评分时若当前证据池不足，则该项输出 `cannot_assess`，不能猜分或静默判定为通过。

核心原则是：媒体必须作为媒体传输，不能把二进制误读为文本；无法可靠听看时必须失败得可理解，而不是转化为正向分数。

## 2. 相关 benchmark 对方案的启发

1. **MMAU-Pro（2025）**强调综合音频智能，而非只测 ASR。DuMateBench 的音频 rubric 因此不能只检查“有没有声音”或“能否转写”，还应区分内容理解、语音质量、非语音声音、场景和任务完成度。
2. **AIR-Bench**提供开放式音频问答与 judge 协议的参考。这里借鉴其开放式判断思路，但要求 judge 输出结构化 criterion、证据和 `cannot_assess`，不保留不可审计的自由文本总分。
3. **MMAR**将音频能力区分为 signal、perception、semantic、cultural 等层次。对应到交付评估，应分别考虑底层技术质量、可感知事件、语义正确性和文化/语境判断，避免把所有问题压成一个“音频质量”分数。
4. **AudioBench**强调任务路由和多数据集覆盖。DuMateBench 同样需要根据 artifact modality 路由到文本、音频、视频或多模态 criterion，而不是让所有任务共享同一套泛化 prompt。
5. **MMAU**适合作为大规模客观音频 QA 回归的参照。对于 DuMateBench，本方案保留确定性 evaluator 和 focused payload tests，用于防止 provider 或消息格式变化造成静默回归。

这些 benchmark 提供的是能力分层和评估协议启发，并不直接给出 DuMateBench 的任务 golden。具体 session 是否具有可靠参考，仍必须在 rubric criterion 级别声明。

## 3. group2 三个任务的现有覆盖与缺口

| Session | 主要交付物 | 当前 checks 覆盖 | checks 无法可靠判断 | 建议的多模态 criterion |
|---|---|---|---|---|
| `41691...ses_0f22...` | 原视频对比分析 Markdown | 报告存在、Markdown 格式、章节/关键词、无额外文件等 6 项 | 对比结论是否真的来自原视频；文案、视觉、配乐差异是否准确；建议是否由观察证据支持 | 原视频忠实度、差异证据可定位性、分析完整性、改进建议可执行性 |
| `20bc...ses_0cc9...` | 按文档拆分的中文 MP3 和压缩包 | 12 个文件存在检查和 1 个目录结构检查，共 13 项 | 是否为中文；是否漏读/错读；多音字是否正确；切分是否对应段落；音量、噪声和可懂度是否合格 | 文档内容一致性、中文发音、切分边界、语音可懂度、音频技术质量 |
| `2a80...ses_116b...` | 核桃油广告 MP4 和 storyboard JSON | 当前磁盘快照中有 6 项 checks | 成片流畅度、卖点表达、河套人文场景、镜头连续性、音画同步、商业片技术质量 | 商业信息表达、镜头/剪辑连续性、音画同步、视觉技术质量、storyboard 与成片一致性 |

### 3.1 当前数据一致性风险

现场读取发现，`2a80...ses_116b.../instruction.md` 要求制作核桃油商业广告，并指定：

- `/outputs/video/storyboard_walnut.json`
- `/outputs/video/WalnutOil_Final_Smooth.mp4`

但同 session 当前磁盘上的 `evaluator/checks.yaml` 实际检查：

- `run_outputs/reunion/重逢_杜甫_分镜脚本.md`
- “第一人称重逢”“非告别”等杜甫短片内容
- `run_outputs/reunion/重逢_杜甫_最终版.mp4`

这与任务指令不一致。该问题不属于多模态 judge 代码缺陷，也不应由本实现自动改写历史 checks。它说明：

1. “只读取同 session checks”可以防止跨 session 去重污染；
2. 但同 session 内 checks 本身仍可能错配；
3. 上线前应增加 `instruction` 与 checks 目标路径/关键词的一致性审核；
4. 发现明显错配时，应阻断 rubric 锁定并进入 annotation review，而不是把错误 checks 当成可信覆盖项。

## 4. 通用维度与 rubric 生成规则

新 rubric 使用 schema `1.1`，同时允许旧 schema `1.0` 通过验证。每个 criterion 只保留对自动路由必要的模态字段；reference artifact 与其他 judge 保持一致，只作为 judge 运行时可用的证据池，不在 criterion 中显式绑定：

| 字段 | 取值 | 语义 |
|---|---|---|
| `modality` | `text`、`audio`、`video`、`multimodal`、`structured` | criterion 依赖的 artifact 类型 |

rubric 生成遵守以下规则：

1. **原子化**：一个 criterion 只判断一个可观察属性。例如“发音正确”和“切分自然”必须分开。
2. **候选无关**：rubric 只根据任务指令、参考 inventory 和当前 session checks 生成，不读取待评分候选内容。
3. **证据可定位**：文本引用 path 和原文片段；媒体引用 path 和时间戳/片段；结构化文件引用字段路径。
4. **机械项去重**：不重复已有 checks 覆盖的文件存在、格式、目录或关键词检查。
5. **只按当前 session 去重**：不按 task id 全局聚合，不读取其他 session 的 checks。
6. **保留可执行项**：某个事实项需要人工 golden，不影响音质、同步、连续性等其他 criterion 自动执行。
7. **不把主观性等同于不可评分**：例如“语音是否清晰”“视频是否明显卡顿”可以通过媒体直接观察；只有缺少必要外部依据时才使用 human review 标记。

### 4.1 推荐维度

- `content_relevance`：内容是否紧扣任务和目标受众；
- `factual_correctness_faithfulness`：事实、数字、引用和对照结论是否有依据；
- `requirement_completeness`：实质要求是否完成，不重复机械交付检查；
- `structure_coherence`：文本结构、镜头顺序、叙事和段落衔接是否连贯；
- `technical_quality`：编码、音量、噪声、卡顿、画面稳定等技术质量；
- `audio_visual_quality`：语音、音乐、环境声、音画同步和剪辑连续性。

### 4.2 golden review 判定示例

| 判断项 | 可用依据 | 自动状态 |
|---|---|---|
| MP3 是否清晰可懂 | 候选 MP3 可正常播放 | 可自动评分 |
| MP3 是否逐字忠实于文档 | 候选 MP3 + 完整文档文本均可用 | 可自动评分，但低置信度时应保留人工复核 |
| “yuxi”音色是否完全一致 | 没有 yuxi 参考音频或可靠 voice id golden | 不生成绑定 golden 的 criterion；证据不足时 `cannot_assess` |
| 视频是否明显卡顿 | 候选 MP4 可正常播放 | 可自动评分 |
| 报告是否忠实比较原视频 | 候选报告 + 原视频均可用 | 可自动评分并引用视频时间戳 |
| 核桃油卖点是否符合真实产品事实 | 没有产品事实表或授权材料 | 证据不足时 `cannot_assess` |
| 河套文化描述是否事实准确 | 没有可靠来源或人工 golden | 证据不足时 `cannot_assess` |

## 5. Artifact 采集与多模态消息

### 5.1 Artifact 表示

| Artifact | 采集内容 | 是否进入文本 excerpt | 传输方式 |
|---|---|---|---|
| TXT/Markdown/HTML/YAML | path、MIME、大小、抽样文本 | 是 | Chat Completions 文本 |
| JSON | path、MIME、大小、文本、root type、key/item 结构 | 是 | 文本 inventory |
| DOCX | 段落、表格和文本结构 | 是 | 提取后的文本 inventory |
| MP3/WAV | path、MIME、大小、audio 类别 | 否 | `input_audio` base64 或 `audio_url` |
| MP4/MOV/WebM | path、MIME、大小、video 类别 | 否 | `video_url` data URL 或 URL |
| M4A/FLAC/AAC/OGG | path、MIME、大小、audio 类别 | 否 | URL/provider adapter；标准 data payload 不静默伪装为文本 |

媒体采集阶段不读取二进制，只生成延迟传输引用。构造 judge 请求时才打开文件，并在打开后的文件描述符上重新执行 `fstat` 和有界读取，防止采集后文件增长或替换绕过大小限制。

默认大小限制为 20 MiB，可通过 `DU_MATE_MEDIA_MAX_BYTES` 修改。超过限制时 artifact 的 transport 状态为 `cannot_assess`，不会把文件截断后冒充完整媒体。

### 5.2 媒体模式

- `DU_MATE_MEDIA_MODE=data_url`：MP3/WAV 使用 `input_audio` base64，视频使用 `video_url` data URL；
- `DU_MATE_MEDIA_MODE=url`：使用 `DU_MATE_MEDIA_BASE_URL` 构造媒体 URL；
- `DU_MATE_MEDIA_MODE=disabled`：媒体 criterion 明确输出 `cannot_assess`；
- URL 模式必须配置 base URL，否则启动时直接报错。

OpenAI-compatible Chat Completions 并没有统一所有 provider 的视频和 URL 音频字段。实现提供规范化基线，不宣称对任意 provider 自动兼容。provider 返回多模态 HTTP 4xx 时抛出 `MediaTransportError`，提示检查媒体支持或切换模式，不会把请求失败转成通过。

### 5.3 可审计附件清单

judge 结果只记录：

- scope：`output` 或 `reference`；
- path；
- MIME；
- size；
- category；
- transport status；
- media mode。

日志和结果不记录完整 base64、API key 或二进制内容。同一组媒体 content parts 在多个 judge runs 间复用，媒体不会按运行次数重复读取。

## 6. Judge prompt、状态和评分

Judge prompt 明确要求：

1. 必须引用 artifact path；
2. 媒体证据应引用时间戳或可定位片段；
3. 无法听看时不得依据文件名、任务描述或猜测给分；
4. 缺少 transcript/reference/golden 等可靠依据的事实项输出 `cannot_assess` 和 `score=null`；
5. `cannot_assess` 不是通过，也不是默认中间分；
6. 其他具备证据的 criterion 继续评分。

模型输出经过严格归一化：

- criterion id 必须来自 locked rubric；
- 不允许未知或重复 id；
- `status` 只能是 `assessed` 或 `cannot_assess`；
- assessed 分数必须是 0 到 4 的有限数；
- cannot-assess 分数必须为 null；
- evidence 必须为 list；
- confidence 必须在 0 到 1 之间；
- 模型遗漏的 criterion 自动补为 `cannot_assess`，不能被当作满分。

某个不可传输视频只阻断 `video` 和 `multimodal` criterion，不会让可独立验证的文本项全部失效。旧 rubric 中的 `needs_human_golden_review`、`golden_source` 或 `evidence_type` 会被归一化流程忽略；是否可评估由当前可见的候选、reference 证据池和媒体传输状态共同决定。

### 6.1 分数定义

设 criterion 归一化权重为 `w_i`，0 到 4 分评分为 `s_i`，已评分集合为 `A`：

```text
assessment_coverage = sum(w_i for i in A)
judge_score_conservative = sum(w_i * s_i / 4 for i in A)
judge_score_assessed_only = judge_score_conservative / assessment_coverage
```

其中：

- conservative score 将不可评分权重按 0 计入总任务尺度；
- assessed-only score 只反映已有证据部分的质量；
- coverage 单独报告，防止高分掩盖大量不可评项；
- coverage 小于 1 或存在 cannot-assess 时，`needs_human_review=true`。

## 7. 代码结构与运行方式

### 7.1 主要文件

- `llm_judge_mm/artifacts.py`：文本/媒体识别、MIME、大小限制和安全采集；
- `llm_judge_mm/llm.py`：OpenAI-compatible payload 和 provider 错误；
- `llm_judge_mm/checks.py`：当前 session checks 摘要与机械项去重；
- `llm_judge_mm/prompts.py`：rubric/judge 多模态 prompt；
- `llm_judge_mm/schema.py`：rubric 1.1、旧 schema 兼容和 judgment 校验；
- `llm_judge_mm/runner.py`：rubric 生成、附件构造、judge runs 和聚合；
- `llm_judge_mm/cli.py`：命令行入口；
- `tests/test_multimodal_judge.py`：focused tests。

### 7.2 生成 rubric

```bash
export OPENAI_API_KEY=...
export DU_MATE_MEDIA_MODE=data_url
export DU_MATE_MEDIA_MAX_BYTES=20971520

PYTHONPATH=/mnt/cfs_algo_bj/workspace/sijinhua/benchmark/multimodal_llm_judge \
python -m llm_judge_mm.cli generate-rubric \
  --task-dir /path/to/task-session \
  --reference-dir /path/to/reference \
  --model <provider-model> \
  --rubric-out /path/to/rubric.json
```

生成阶段读取：

- `instruction.md`；
- 当前 task session 的 `evaluator/checks.yaml`；
- reference artifact inventory。

生成阶段不读取候选输出内容，避免 rubric 被候选质量反向污染。

### 7.3 执行 judge

```bash
PYTHONPATH=/mnt/cfs_algo_bj/workspace/sijinhua/benchmark/multimodal_llm_judge \
python -m llm_judge_mm.cli judge \
  --task-dir /path/to/task-session \
  --outputs-dir /path/to/run_outputs \
  --reference-dir /path/to/reference \
  --model <provider-model> \
  --rubric /path/to/rubric.json \
  --judge-runs 3 \
  --result-out /path/to/judge_result.json
```

建议把 rubric 和 judge result 写到 `run_outputs` 之外。即使误放入 artifact 目录，采集器也会忽略标准 rubric/judge/reward JSON 前缀产物。

## 8. 测试与当前验证结果

新增 focused suite 共 11 项，全部通过：

1. MP3 MIME 和 audio 类别识别；
2. MP4 MIME 和 video 类别识别；
3. 媒体不进入文本 excerpt；
4. JSON 结构提取和 rubric 派生产物忽略；
5. 超大小媒体明确标记 cannot-assess；
6. `input_audio` 与 `video_url` payload 结构；
7. 不支持的 data-mode 音频格式返回清晰错误；
8. 纯文本消息继续使用 string content；
9. 当前 session checks 的机械项去重；
10. human golden marker 强制 cannot-assess；
11. 临时 task session 同时加载 MP3/MP4、执行完整 runner payload，并验证多轮 judge 只读取一次媒体。

验证结果：

```text
Ran 11 tests in 0.034s
OK
AST_OK 9 files
```

原 `DataAnnotation` judge 回归测试中，不依赖 DOCX 的 7 项全部通过。完整 8 项中的 DOCX 用例在当前 `srre` pod 中因缺少 `python-docx` 无法启动：

```text
ModuleNotFoundError: No module named 'docx'
```

这是测试环境依赖缺口，不是本次代码回归。本实现遇到 DOCX 且环境缺依赖时也会显式报出 `python-docx is required`，不会把 DOCX 二进制按 UTF-8 文本读取。

测试使用 fake client 验证了完整消息构造与 runner 行为，尚未对具体线上 provider 发起真实媒体 API 请求。上线前仍需为目标 provider 执行 MP3、MP4 各一条真实 smoke test。

## 9. 上线门槛与风险

### 必须完成

1. 选择目标 provider/model，验证其实际支持 `input_audio`、`video_url` 或 URL 媒体格式；
2. 使用小型 MP3 和 MP4 做真实 API smoke test，确认响应不是只基于文本 prompt；
3. 修复或重新审核 `2a80...` instruction/checks 错配；
4. 对每个媒体 task 锁定 rubric 后再评候选，不能边看候选边改标准；
5. 建立人工复核样本，校准 0 到 4 分边界和 cannot-assess 使用率；
6. 对事实、参考视频对照和音色身份 criterion 检查 evidence pool 是否足够支撑自动评分；
7. 监控附件大小、provider 4xx、cannot-assess 比例和 assessment coverage。

### 已知风险

- **Provider 差异**：OpenAI-compatible 只说明 HTTP 接口相似，不保证媒体 content part 一致；
- **上下文与内存**：base64 约增加三分之一体积，超大媒体应使用 URL 或离线抽帧/转码 adapter；
- **长视频定位**：provider 可能无法稳定处理长视频，需要后续增加可审计的抽帧和音轨策略；
- **Judge 偏差**：模型可能偏好制作精良但内容不忠实的媒体，必须分离内容与技术质量 criterion；
- **Golden 缺失**：human review 标记能阻止猜分，但不能自动创造正确参考；
- **Checks 错配**：同 session 去重只能避免跨 session 污染，不能证明 checks 与 instruction 语义一致；
- **多轮一致性**：多次 judge 可降低随机性，但不能替代人工校准和 provider 回归。

## 10. 最终建议

该实现已经满足本地框架级验收：

- MP3/MP4 被识别并作为媒体附件传输；
- 二进制不会进入文本 excerpt；
- 当前 session 已覆盖的机械 checks 不重复生成 rubric；
- 缺少 golden 的 criterion 不猜分；
- 不支持媒体时返回明确错误；
- 文本 criterion 和旧 rubric 保持兼容；
- 原历史 session 和已有 checks 未被修改。

下一阶段不应继续扩展更多媒体后缀，而应先完成两件事：

1. 对选定线上 provider 做真实 MP3/MP4 smoke test，锁定它实际接受的 content-part 协议；
2. 修复 group2 样例中的 instruction/checks 一致性，并用人工评分集校准 judge 与 human 的相关性、cannot-assess 精度和 coverage 阈值。

只有完成这两项，才能从“payload 和协议实现正确”推进到“benchmark 分数具有可信解释”。
