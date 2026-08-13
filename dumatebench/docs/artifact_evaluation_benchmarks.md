# Office Artifact Evaluation Benchmarks

本文面向 DuMateBench 的任务设计者、标注者和 evaluator 实现者，梳理办公产物评估的常见做法。这里的“产物”主要指 Word/docs、PowerPoint/PPT、PDF 和 Excel。本文关注三个问题：评什么，怎么评，怎么标注。

## 1. OfficeBench 和 OdysseyBench

### 1.1 总体范式

OfficeBench 将办公自动化任务放在 Docker 文件系统中执行。Agent 通过 Word、Excel、PDF、Email、Calendar、OCR、Shell 等应用完成任务，提交后保存整个文件系统，再运行任务定制 evaluator。论文明确使用 exact matching、fuzzy matching 和 execution-based evaluation 来检查最终产物是否正确，并报告任务 pass rate。参考：[OfficeBench paper](https://arxiv.org/abs/2407.19056)。

OdysseyBench 继承 OfficeBench 的产物评估思想，但把任务改造成长历史、多会话办公 workflow。Agent 必须从历史对话中找出完成任务所需的信息，再操作 Word、Excel、PDF、Email 和 Calendar。评估仍然是保存最终文件系统，然后用 exact matching、fuzzy matching 和 execution-based code snippets 检查所有 evaluation criteria；全部 criteria 满足才算任务成功，指标是 pass rate。参考：[OdysseyBench paper](https://arxiv.org/html/2508.09124)。

两者的核心相同：它们评估“任务是否被正确完成”，而不是单独评估文档美观度。标注者需要为每个任务写出可执行、可复现的成功条件。

### 1.2 Docs / Word

评估维度：

- 文件是否存在，文件名和路径是否正确。
- 文档正文是否包含关键内容，例如人名、日期、金额、结论、摘要要点。
- 文档是否与参考答案完全一致，适用于标准化填表、固定文本输出等低自由度任务。
- diff 是否包含指定修改，适用于“在原文档中补充/替换某些内容”的任务。

评估方法：

- `evaluate_contain` 读取 Word 文档文本，检查所有关键词是否出现。
- `evaluate_exact_match` 将结果文档与 reference 文档比较。
- `evaluate_diff_contain_text` 比较输入和输出的 diff，检查修改是否覆盖指定关键词。
- 对开放式改写或总结，OfficeBench/OdysseyBench 通常只检查关键事实，而不是要求长文本逐字一致。

标注方法：

- 标注者给出目标文件、必须出现的关键词、不能丢失的信息和必要的 reference 文件。
- 对总结类任务，应把 gold answer 拆成短关键词或短事实，避免用完整自然语言句子做硬匹配。

### 1.3 PPT

OfficeBench 和 OdysseyBench 的公开任务空间主要覆盖 Word、Excel、PDF、Email 和 Calendar。它们没有把 PowerPoint 作为核心应用，也没有原生的 PPT 结构、布局或美观度 evaluator。因此，如果要用 OfficeBench/OdysseyBench 的基础范式评 PPT，只能把 PPT 当作文件系统产物处理，检查文件存在、格式有效、文本关键词、或转换后的文本/图片是否满足要求。

这说明一个边界：OfficeBench/OdysseyBench 的规则型评估适合检查确定性任务要求，但不足以评估 PPT 的视觉层级、布局对齐、设计质量和演示逻辑。PPT 类任务需要额外借鉴 PPTBench 或 PPT-Eval 的做法。

### 1.4 PDF

评估维度：

- 是否正确读取 PDF 中的信息。
- 是否把 PDF 中的信息转写、汇总或迁移到目标产物。
- 是否生成有效 PDF，或是否把 PDF 转换为 Word、图片等中间格式。

评估方法：

- 对读取类任务，使用 PDF 文本抽取后做关键词匹配。
- 对转换类任务，检查目标文件是否存在、格式是否有效、是否包含关键内容。
- 对没有唯一输出的 PDF 任务，使用执行式检查，例如运行脚本确认某个字段、日期、数值或页内信息是否被正确提取。

标注方法：

- 标注者需要给出 PDF 中的 gold facts，例如合同编号、发票金额、表格数值、页码或通知时间。
- 如果 PDF OCR 或版面复杂，应标注允许的关键字段，而不是要求完整文本完全一致。

### 1.5 Excel

评估维度：

- 单元格值是否正确。
- 公式、排序、筛选、统计、汇总是否正确。
- 生成或修改后的 workbook 是否与 reference workbook 一致。
- 跨表依赖是否保持正确。

评估方法：

- `evaluate_excel_cell_value` 检查指定单元格的字符串值。
- `evaluate_excel_cell_comparator` 用比较器检查数值条件，例如大于阈值或在合理范围内。
- `evaluate_exact_match` 对 workbook 的活动表做双向逐单元格比较。
- 对日历或跨应用任务，可以用 execution-based code snippets 检查结果是否满足约束。

标注方法：

- 标注者应标出 sheet、row、column、expected value、公式或 comparator。
- 对计算任务，优先标注关键输出单元格和容差，而不是要求整表完全一致。

## 2. 外部专项 Benchmark

### 2.1 Docs: DocBench 和 Office Comprehension Benchmark

DocBench 评估 LLM-based document reading systems。它收集 229 个 PDF 文档和 1,102 个问题，覆盖 Academia、Finance、Government、Law 和 News 五个领域。问题类型包括 text-only、multimodal、metadata 和 unanswerable；答案类型包括 numerical、textual、boolean 和 others。参考：[DocBench paper](https://arxiv.org/html/2407.10701)。

评估维度：

- 长文档读取。
- 表格、图片、版面和 metadata 理解。
- 不可回答问题的拒答能力。
- 数值、布尔和文本答案的准确性。

评估方法：

- DocBench 用 GPT-4 evaluator 给回答打 0/1，最终报告 accuracy。
- 论文报告 GPT-4 evaluator 与人类标注者在 200 个样本上达到 98% agreement。
- 对数值和布尔答案，字符串匹配或数字抽取足够；对文本答案，LLM evaluator 更稳。

标注方法：

- 先从真实文档中生成或人工编写 QA。
- GPT-4/GPT-4V 生成部分 text-only 和 multimodal QA。
- 人工标注者补充 metadata、unanswerable 等真实用户常问的问题。
- 再由人工和领域从业者审核问题质量与答案正确性。

Office Comprehension Benchmark (OCB) 更接近原生办公文件理解。它覆盖 Word、Excel 和 PowerPoint 的 native formats，并把 reference answer 分解成 atomic binary-gradable claims，再用 LLM judge ensemble 独立判断每个 claim。参考：[OCB arXiv entry](https://arxiv.org/abs/2607.01245)。这对 DuMateBench 的进阶版有启发：把一个长答案拆成多个原子事实，比直接给整体分更稳定。

### 2.2 PPT: PPTBench 和 PPT-Eval

PPTBench 是面向 PowerPoint 布局和设计理解的 multimodal benchmark。它基于 958 个 PPTX 文件构造 4,439 个样本，覆盖 Detection、Understanding、Modification 和 Generation 四类任务。每个样本使用 slide screenshot 和结构化 JSON 作为双模态输入。参考：[PPTBench paper](https://arxiv.org/html/2512.02624)，代码入口：[PPTBench-Eval](https://github.com/Gastronomicluna/PPTBench-Eval)。

评估维度：

- Detection：内容抽取、样式识别、布局检测。
- Understanding：文本层级、图表、表格和语义关系理解。
- Modification：元素增删改、文本修改、布局 refinement。
- Generation：从输入材料生成新的 slide 或 speaker notes。

评估方法：

- Detection 用 exact match。
- Understanding 用四选一 accuracy。
- Modification 要求模型输出 PowerPoint API calls，由 deterministic executor 执行后，把生成的 slide JSON 和 ground-truth JSON exact match。
- Generation 使用 Gemini-2.0 作为 LLM-as-a-Judge，按 0 到 5 六档 rubric 评分，再映射到百分制。

标注方法：

- PPTBench 先把 PPTX 转成 compact JSON，保留元素类型、文本、位置、尺寸和样式。
- Detection 标签直接从 JSON 字段读取。
- Understanding 标签通过 JSON/视觉解析、LLM 生成选择题、人工复核获得。
- Modification 标签通过受控 API 操作生成 post-edit slide state。
- Generation 不设唯一 reference label，而用 judge rubric 评分。

PPT-Eval 关注 GUI 级 PowerPoint 编辑。它包含 120 个任务，覆盖 12 个 PowerPoint 文件，任务按 easy、medium、hard 分层。与 PPTBench 的 API 范式不同，PPT-Eval 让 agent 在 PowerPoint Online 中操作完整 GUI。参考：[PPT-Eval paper](https://arxiv.org/html/2606.31154)。

评估维度：

- 内容创建和整 deck 编辑。
- 文本、图片、形状、表格、动画、主题、非标准布局等 PowerPoint 结构。
- 部分完成度、无关修改、视觉美观和自然语言反馈。

评估方法：

- 为每个任务设计 task-specific rubric。
- Rubric 给中间步骤 partial credit，惩罚无关或破坏性编辑，并输出自然语言反馈。
- 论文报告 rubric 分数与人类判断 Kendall correlation 0.77、Spearman correlation 0.84。

标注方法：

- 标注者需要把任务目标拆成可评分节点。
- 对每个节点定义满分条件、部分完成条件和扣分条件。
- 对主观项保留自然语言描述，例如“布局清晰”“无明显重叠”“未破坏原有主题”。

### 2.3 PDF: OmniDocBench 和 DocBench

OmniDocBench 评估 PDF/document parsing，而不是办公 agent 任务完成。它从 20 万多个 PDF 文档中筛选页面，最终包含 981 个 PDF pages，覆盖 9 类文档。它提供 19 类 layout labels 和多种属性标签，支持文档解析、布局检测、表格识别、公式识别、OCR 和 reading order 的综合评估。参考：[OmniDocBench paper](https://arxiv.org/html/2412.07626)。

评估维度：

- 页面级布局检测。
- 文本、公式、表格、代码块等内容识别。
- reading order。
- 表格结构、公式 LaTeX、跨元素 affiliation。

评估方法：

- 先把模型输出 markdown 预处理。
- 用规则抽取 LaTeX table、HTML table、display formula、markdown table、code block 和纯文本。
- 再与 ground truth 对齐并计算 TextEdit、FormulaEdit、FormulaCDM、TableTEDS、TableEdit、ReadOrderEdit 和 OverallEdit 等指标。

标注方法：

- 自动预标注：LayoutLMv3、PaddleOCR、UniMERNet、GPT-4o 等。
- 人工校正 bounding boxes、reading order、affiliation 和字符内容。
- 对公式和表格做专家质检，确保 LaTeX/HTML 可渲染。

DocBench 同样以 PDF 为输入，但评的是 document reading QA。它更适合借鉴到 DuMateBench 的 PDF 问答、PDF 信息抽取和 PDF 摘要任务；OmniDocBench 更适合借鉴到 PDF 解析质量和版面还原任务。

### 2.4 Excel: SpreadsheetBench

SpreadsheetBench 面向真实电子表格操作。它从 Excel 论坛和博客收集 912 条真实用户问题，并构造 2,729 个 test cases，平均每条 instruction 有 3 个 test cases。参考：[SpreadsheetBench paper](https://arxiv.org/html/2406.14991)。

评估维度：

- 查找、抽取、求和、删除、修改、计数、计算、展示等真实 spreadsheet 操作。
- 多 sheet、多表格、非标准关系表、嵌套或缺失表头。
- 解决方案对不同 cell values 和 corner cases 的泛化能力。

评估方法：

- 采用 Online Judge-style evaluation。
- 模型为每条 instruction 生成一个通用解决方案，再把该方案应用到多个 spreadsheet test cases。
- 每个 test case 的结果 spreadsheet 与 ground truth 比较，结果标为 ACC 或 fail。
- soft restriction 给通过部分 test cases 的方案 partial credit；hard restriction 要求所有 test cases 通过。

标注方法：

- 从真实论坛问题和原始 spreadsheet 提取 instruction。
- 如果论坛只有方案没有派生答案，人工按方案操作原始 spreadsheet 得到答案。
- 标注者再修改原始 spreadsheet 两次，制造不同数据值和 corner cases。
- 通过同一方案得到额外 ground-truth answers，从而形成多 test case。
- 数据验证由 Excel 经验标注者和作者二次质检完成。

## 3. DuMateBench

### 3.1 目标差异

DuMateBench 不只评估可靠环境里的局部办公操作。它希望评估 agent 在真实复杂环境中进行多办公工具联合调用的能力，包括：

- 环境不充分：缺工具、缺组件、资源不足。
- 环境不稳定：网络失败、API 错误、工具 wrapper 故障。
- 环境有噪声：相似文件名、历史版本、干扰 sheet、重复内容、截断页等。
- 个性化上下文：用户配置文件、长期偏好和历史 session。
- 复杂 workflow：网络检索、代码执行、文件编辑、内容生成和多产物交付。

现有 proposal 已把基础指标定义为 `partial pass@K`、`complete pass@K` 和 cost，进阶指标定义为内容质量得分和格式美观得分。参考：[DuMateBench Proposal V2.md](/Users/niuzechun/Documents/DuMateBench/DuMateBench%20Proposal%20V2.md)。

### 3.2 基础版：扩展 OfficeBench / OdysseyBench

基础版应保持可复现、低主观性和低成本。做法是人工或 LLM-assisted 生成 `checks.yaml`，再用规则 evaluator 检查 `run_outputs/` 和 `run_logs/`。

总体评估维度：

- 产物存在性：目标文件、目录、日志和导出文件是否存在。
- 格式有效性：docx、pptx、xlsx、pdf、json、ics 等是否能被结构化读取。
- 内容正确性：关键词、gold facts、关键数值、日期、实体名和结论是否出现。
- 数据正确性：Excel 单元格值、公式、容差、sheet 结构、样式。
- 格式约束：Word 字体/段落格式，PPT 文本 run 样式，Excel 单元格样式。
- 误改保护：不应修改的文件是否 hash 一致，diff 是否只出现在允许区域。
- 交付结构：目录结构是否正确，是否有多余文件。
- 环境恢复：日志中是否记录工具安装、故障恢复、网络/API fallback。
- 成本：token 和 elapsed time 是否在预算内。

已实现函数见 [dumatebench/evaluator/EVALUATOR_FUNCTIONS.md](/Users/niuzechun/Documents/DuMateBench/dumatebench/evaluator/EVALUATOR_FUNCTIONS.md)。目前 DuMateBench 已兼容 OfficeBench/OdysseyBench 的核心函数，并新增格式、目录、样式、diff、预算等规则型 evaluator。

#### Docs / Word

基础版评估：

- `evaluate_file_exist`：检查目标 docx 是否生成。
- `evaluate_file_format_valid`：检查 docx 是否包含 `word/document.xml`。
- `evaluate_contain`：检查正文关键词和 gold facts。
- `evaluate_docx_font_style`：检查指定 run 的字体、字号、粗体、斜体、下划线、颜色。
- `evaluate_docx_paragraph_format`：检查段落样式、对齐、缩进、段前段后和行距。
- `evaluate_files_unchanged`：保护原始附件或历史文件。

标注建议：

- 对内容要求，拆成短事实，不要用完整自然句做 exact match。
- 对格式要求，明确 paragraph_number、text、run_text、style_name、font_size_pt 等定位字段。
- 对“润色/改写”类任务，基础版只检查事实保真和关键格式；语言质量留给进阶版。

#### PPT

基础版评估：

- `evaluate_file_exist`：检查 pptx 是否生成。
- `evaluate_file_format_valid`：检查 pptx 是否包含 `ppt/presentation.xml`。
- `evaluate_pptx_text_style`：检查指定 slide/text/run 的字体、字号、粗体、斜体、颜色。
- `evaluate_contain` 可在转换为文本或另存辅助文本后检查关键内容。
- `evaluate_directory_structure` 和 `evaluate_no_extra_files` 检查交付目录。

基础版的局限：

- 现有规则函数只能可靠检查 PPT 文件结构和文本样式。
- 布局对齐、元素重叠、视觉层级、页面美观、整 deck 叙事逻辑需要额外 evaluator。

标注建议：

- 对基础版，标注 slide_number、必须出现的标题/数值/结论、指定文本样式和文件路径。
- 对视觉任务，可以先要求 agent 同步导出每页 PNG，再用脚本或 judge 做进阶评估。

#### PDF

基础版评估：

- `evaluate_file_exist`：检查 PDF 是否生成或转换结果是否存在。
- `evaluate_file_format_valid`：检查 PDF 文件头。
- `evaluate_contain`：从 PDF 抽取文本后检查关键词。
- `evaluate_files_unchanged`：保护原始 PDF。
- 对 OCR 或结构化抽取任务，可把答案写到 txt/json/xlsx，再检查这些结构化结果。

标注建议：

- 标注 PDF 中的页码、字段名、关键数字和 expected answer。
- 对扫描件或复杂版面，优先检查下游结构化答案，不直接要求 PDF 全文抽取一致。

#### Excel

基础版评估：

- `evaluate_file_exist` 和 `evaluate_file_format_valid`：检查 workbook 是否存在且可打开。
- `evaluate_excel_sheet_exists`：检查必要 sheet 和禁止 sheet。
- `evaluate_excel_cell_value`：检查关键单元格。
- `evaluate_excel_formula`：检查公式字符串。
- `evaluate_excel_cell_number_tolerance`：检查浮点计算结果。
- `evaluate_excel_cell_style`：检查字体、填充、对齐、数字格式。
- `evaluate_exact_match`：对低自由度任务做 reference workbook 比较。

标注建议：

- 对每个关键结果标注 sheet、row、col、expected value 或 tolerance。
- 对公式任务同时标注公式和值，防止 agent 只填死值。
- 对真实复杂表格，可以借鉴 SpreadsheetBench，为同类 instruction 构造多个 workbook test cases。

### 3.3 进阶版：使用 LLM-as-Judge

进阶版应处理规则 evaluator 难以覆盖的维度：内容质量、格式美观、视觉布局、演示叙事、跨材料综合质量和用户偏好遵循。建议采用“规则检查 + judge rubric”的混合架构。

总体流程：

1. 先跑基础版 evaluator。文件缺失、格式损坏、关键事实错误应直接限制最高分。
2. 将产物转换成 judge-friendly 表示：docx 提取文本和样式摘要；pptx 导出 PNG、提取 slide JSON；pdf 渲染页面并提取文本；xlsx 导出关键 sheet、公式、预览图和统计摘要。
3. 对每个任务生成 task-specific rubric。Rubric 应由多个原子项组成，每项有满分、部分分和扣分条件。
4. 使用 LLM/VLM judge 独立评分每个原子项，并要求输出证据位置。
5. 对关键样本做人类复核，估计 judge 与人工的一致性。
6. 最终分数 = 基础检查门控 + rubric 加权分 + cost/robustness 附加项。

在 DuMateBench 的 PPT judge 第一版中，报告按通用维度聚合分数，但每个维度下应包含多个 task-specific 原子 rubric items。这样既能保留稳定的总分结构，也能像 PPT-Eval 一样表达 partial credit、扣分条件和具体 slide-level evidence。

#### Docs / Word 进阶评分

可评维度：

- 事实完整性：是否覆盖用户要求、历史上下文和检索结果。
- 忠实性：是否引入幻觉，是否误读来源。
- 结构质量：标题层级、段落组织、表格可读性。
- 风格匹配：是否符合用户配置、语气和目标读者。
- 格式美观：字体、间距、标题、表格样式是否一致。

标注方式：

- 将 gold answer 拆成 atomic claims。
- 为每个 claim 标注必要性和权重。
- 为风格和结构写 rubric，而不是写单一 reference answer。

#### PPT 进阶评分

可评维度：

- 内容完整性：是否覆盖所有关键信息。
- 页面结构：标题、正文、图表、注释是否层级清楚。
- 视觉布局：对齐、留白、重叠、裁切、视觉平衡。
- 设计一致性：配色、字体、图标、页间风格。
- 演示逻辑：章节组织、过渡、speaker notes 和受众适配。

可借鉴 PPTBench：

- 对 detection/understanding/modification 用结构化 JSON 和 exact match。
- 对 generation 用 VLM/LLM judge 的 0 到 5 分 rubric。

可借鉴 PPT-Eval：

- 每个任务写 task-specific rubric。
- 将“删除了错误文本但没删除箭头”这类情况拆成多个 scoring nodes，给 partial credit。
- 明确惩罚无关修改和破坏原 deck 风格的行为。

建议落地格式：

- 用固定维度承载汇总分，例如 `content_preservation`、`layout_and_readability`、`visual_design`。
- 在每个维度内生成 1 到 3 个原子评分点，而不是把整个维度压缩成一句模糊标准。
- 每个原子评分点写清楚满分条件、部分完成条件、失败条件和对应页码证据。

标注方式：

- 为每页导出 screenshot，保存 slide JSON。
- 标注必须出现的内容、应修改的元素、不可修改区域。
- 对美观项写可观察标准，例如“无明显文本重叠”“主标题在首屏可见”“图表标签可读”。

#### PDF 进阶评分

可评维度：

- 信息抽取准确性。
- OCR 和版面理解。
- 表格、公式、图片和跨页内容处理。
- 转换质量，例如 PDF 转 Word 后是否保留阅读顺序和关键结构。

可借鉴 OmniDocBench：

- 对解析任务使用 text edit distance、table structure metrics、formula metrics 和 reading order metrics。
- 对复杂 PDF，不只看全文文本，还要看表格、公式和布局组件。

标注方式：

- 标注页面级区域、reading order、表格 HTML/LaTeX、公式 LaTeX 和关键文本。
- 若成本较高，可只对任务相关页面做局部标注。

#### Excel 进阶评分

可评维度：

- 计算正确性和公式泛化。
- 多 sheet workflow 是否保持依赖关系。
- 图表和可视化是否表达正确。
- 表格格式是否利于阅读和复用。
- 代码/公式是否可维护，而不是只填静态答案。

可借鉴 SpreadsheetBench：

- 对同一 instruction 构造多个 test workbook。
- 要求 agent 生成通用解决方案，再在多个 test cases 上运行。
- 同时报告 soft score 和 hard score。

标注方式：

- 为每个 task 保存原始 workbook、多个扰动 workbook 和对应 ground truth workbook。
- 标注关键输出区域、公式区域、允许改变区域和不可改变区域。
- 对图表任务额外导出图片，由 VLM judge 检查图表类型、轴、单位、标题和趋势表达。

### 3.4 建议的 DuMateBench 评估分层

建议将 DuMateBench evaluator 分成四层：

| 层级 | 目的 | 适用产物 | 主要方法 |
| --- | --- | --- | --- |
| L0 文件与格式 | 确认交付物存在且可打开 | docs/PPT/PDF/Excel | file existence, format validation |
| L1 客观内容 | 检查关键事实和结构化结果 | docs/PDF/Excel/PPT text | keyword, exact match, cell value, formula, diff |
| L2 产物结构与样式 | 检查格式、布局和误改 | docs/PPT/Excel | style checks, directory checks, unchanged checks |
| L3 质量与偏好 | 评估美观、表达和复杂开放任务 | all | LLM/VLM-as-judge, rubric, human calibration |

基础版先覆盖 L0-L2。进阶版补 L3，并把 L3 的 judge 结果与 L0-L2 绑定：如果文件不存在或关键事实错误，judge 不能给高分。

## 4. References

- OfficeBench: [OfficeBench: Benchmarking Language Agents across Multiple Applications for Office Automation](https://arxiv.org/abs/2407.19056)
- OdysseyBench: [OdysseyBench: Evaluating LLM Agents on Long-Horizon Complex Office Application Workflows](https://arxiv.org/html/2508.09124)
- PPTBench: [PPTBench: Towards Holistic Evaluation of Large Language Models for PowerPoint Layout and Design Understanding](https://arxiv.org/html/2512.02624)
- PPT-Eval: [PPT-Eval: A Benchmark for Computer-Use Agents on PowerPoint Tasks](https://arxiv.org/html/2606.31154)
- DocBench: [DOCBENCH: A Benchmark for Evaluating LLM-based Document Reading Systems](https://arxiv.org/html/2407.10701)
- Office Comprehension Benchmark: [Office Comprehension Benchmark](https://arxiv.org/abs/2607.01245)
- OmniDocBench: [OmniDocBench: Benchmarking Diverse PDF Document Parsing with Comprehensive Annotations](https://arxiv.org/html/2412.07626)
- SpreadsheetBench: [SpreadsheetBench: Towards Challenging Real World Spreadsheet Manipulation](https://arxiv.org/html/2406.14991)
- DuMateBench local proposal: [DuMateBench Proposal V2.md](/Users/niuzechun/Documents/DuMateBench/DuMateBench%20Proposal%20V2.md)
- DuMateBench evaluator functions: [EVALUATOR_FUNCTIONS.md](/Users/niuzechun/Documents/DuMateBench/dumatebench/evaluator/EVALUATOR_FUNCTIONS.md)
- DuMateBench annotation pipeline: [data_annotation/README.md](/Users/niuzechun/Documents/DuMateBench/data_annotation/README.md)
