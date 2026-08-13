# DuMateBench 评估函数说明

DuMateBench 首先兼容 OfficeBench 和 OdysseyBench 在 `utils/evaluate.py` 中实现的核心评估函数。此外，新增了一批规则型评估函数。

## 函数分类

### OfficeBench / OdysseyBench 兼容的评估函数
- 文件存在性检查：`evaluate_file_exist`、`evaluate_file_not_exist`。
- 文本包含检查：`evaluate_contain`、`evaluate_not_contain`，以及内部函数 `_evaluate_contain_text`。
- 文件精确匹配：`evaluate_exact_match`。
- diff 内容检查：`evaluate_diff_contain_text`，以及内部函数 `_helper_diff_contain_text`。
- Excel 检查：`evaluate_excel_cell_value`、`evaluate_excel_cell_comparator`。
- 日历检查：`evaluate_calendar_no_overlap`。
- 分发/辅助函数：`evaluate`、`_is_number`。

### DuMateBench 新增评估函数

- 文件格式有效性：`evaluate_file_format_valid`。
- 未误改文件保护：`evaluate_files_unchanged`。
- 允许范围内 diff 检查：`evaluate_no_unexpected_diff`。
- 目录和产物结构检查：`evaluate_directory_structure`、`evaluate_no_extra_files`。
- Excel 结构、数值与样式检查：`evaluate_excel_sheet_exists`、`evaluate_excel_formula`、`evaluate_excel_cell_number_tolerance`、`evaluate_excel_cell_style`。
- Word 格式检查：`evaluate_docx_font_style`、`evaluate_docx_paragraph_format`。
- PPT 格式检查：`evaluate_pptx_text_style`。
- cost/time 预算检查：`evaluate_log_budget`。

## 支持的文档类型

- `txt` 和 `ics`：按纯文本读取。
- `xlsx`：支持单元格文本读取、精确 workbook 比较，以及部分结构/公式/数值/样式检查。
- `doc` / `docx`：在本地依赖可用时提取 Word 文档文本，并可检查 Word 字体和段落格式。
- `pptx`：在本地依赖可用时检查 PPT 文本字体格式。
- `pdf`：在本地依赖可用时提取 PDF 文本。
- `email`：扫描 `emails/<username>/` 目录下的 `.eml` 文件。

## 主函数参考

本节按用途分组。每个函数说明都保留四项信息：功能、输入、输出、逻辑。

### 1. 文件存在性与文件结构

#### `evaluate_file_exist(testbed_dir, args)`

- 功能：检查指定文件或目录是否存在。
- 输入：`testbed_dir`；`args.file`，相对于 `testbed_dir` 的目标路径。
- 输出：`bool`。路径存在返回 `True`，否则返回 `False`。
- 逻辑：拼接 `testbed_dir` 与 `args.file`，调用 `os.path.exists`。

#### `evaluate_file_not_exist(testbed_dir, args)`

- 功能：检查指定文件或目录是否不存在。
- 输入：`testbed_dir`；`args.file`，相对于 `testbed_dir` 的目标路径。
- 输出：`bool`。路径不存在返回 `True`，否则返回 `False`。
- 逻辑：拼接目标路径，对 `os.path.exists` 的结果取反。

#### `evaluate_file_format_valid(testbed_dir, args)`

- 功能：检查文件是否能按声明类型进行结构化读取，适合验证“文件格式是否正确”。
- 输入：`testbed_dir`；`args.file`；可选 `args.doc_type`。支持 `txt`、`md`、`csv`、`json`、`docx`、`pptx`、`xlsx`、`pdf`、`ics`。
- 输出：`bool`。文件存在且格式检查通过返回 `True`。
- 逻辑：文本类文件尝试严格读取；`json` 用 `json.loads`；`docx`/`pptx` 检查 OOXML 关键文件；`xlsx` 用 `openpyxl` 打开；`pdf` 检查 `%PDF` 文件头；`ics` 检查 `BEGIN:VCALENDAR` 和 `END:VCALENDAR`。

### 2. 文本、内容匹配与 diff

#### `evaluate_contain(testbed_dir, args)`

- 功能：检查文档或邮件账户内容是否包含所有关键词。
- 输入：`testbed_dir`；`args.doc_type`；`args.keywords`；非 email 类型使用 `args.file`；email 类型使用 `args.username`。
- 输出：`bool`。所有关键词都出现返回 `True`，否则返回 `False`。
- 逻辑：email 会读取 `emails/<username>/` 下的 `.eml` 文件，并尝试大小写/模糊账户匹配；普通文件按 `doc_type` 读取文本。内容和关键词统一转小写；数值型关键词会先删除内容中的逗号，减少 `1,000` 与 `1000` 的格式差异。

#### `evaluate_not_contain(testbed_dir, args)`

- 功能：检查文档或邮件账户是否缺少至少一个关键词。
- 输入：与 `evaluate_contain` 相同。
- 输出：`bool`。`evaluate_contain` 返回 `False` 时返回 `True`。
- 逻辑：调用 `evaluate_contain(testbed_dir, args)`，再对结果取反。

#### `evaluate_exact_match(testbed_dir, args)`

- 功能：检查结果文件与预期文件是否完全匹配。
- 输入：`testbed_dir`；`args.result_file`；`args.expected_file`；`args.doc_type`。
- 输出：`bool`。内容完全匹配返回 `True`，文件缺失、依赖缺失或内容不同返回 `False`。
- 逻辑：非 `xlsx` 文件按类型读取后直接比较字符串；`xlsx` 文件用 `openpyxl` 读取活动工作表，并双向逐单元格比较，避免遗漏额外行列。

#### `evaluate_diff_contain_text(testbed_dir, args)`

- 功能：检查两个文件的 unified diff 是否包含所有指定关键词。
- 输入：`testbed_dir`；`args.doc_type`；`args.input_file`；`args.output_file`；`args.keywords`。
- 输出：`bool`。两个文件有差异且 diff 包含所有关键词时返回 `True`。
- 逻辑：按 `doc_type` 读取两个文件；内容相同则返回 `False`；否则用 `difflib.unified_diff` 生成 diff，并检查关键词是否全部出现。

#### `evaluate_no_unexpected_diff(testbed_dir, args)`

- 功能：检查 unified diff 中所有变化行是否都匹配允许的正则模式，适合“只有这些区域可以变化”的约束。
- 输入：`testbed_dir`；`args.input_file`；`args.output_file`；`args.allowed_patterns`。
- 输出：`bool`。所有新增/删除行都在允许范围内返回 `True`。
- 逻辑：读取两个文本文件，生成 unified diff，跳过 diff 头和 hunk 标记；对每一行 `+`/`-` 变化内容检查是否匹配任一允许正则。

#### `evaluate_files_unchanged(testbed_dir, args)`

- 功能：检查结果文件是否与参考文件逐字节一致，适合验证“不应被修改的文件是否保持不变”。
- 输入：`testbed_dir`；`args.matches`，每项包含 `file` 和 `reference_file`。
- 输出：`bool`。所有文件对的 SHA-256 哈希一致返回 `True`。
- 逻辑：检查每组文件都存在，分块计算 SHA-256；任一文件对哈希不同则返回 `False`。

### 3. 目录和交付产物结构

#### `evaluate_directory_structure(testbed_dir, args)`

- 功能：检查目录下必须存在和禁止存在的文件/目录。
- 输入：`testbed_dir`；可选 `args.root`；`args.required_files`；`args.required_dirs`；`args.forbidden_paths`。
- 输出：`bool`。所有必需路径存在且所有禁止路径不存在时返回 `True`。
- 逻辑：以 `root` 为检查根目录，逐项判断必需文件、必需目录和禁止路径。

#### `evaluate_no_extra_files(testbed_dir, args)`

- 功能：检查某个目录中是否没有超出预期集合的额外文件，适合验证交付产物是否干净。
- 输入：`testbed_dir`；可选 `args.root`；`args.expected_files`；可选 `args.ignore_patterns`。
- 输出：`bool`。实际文件集合扣除忽略项后是 `expected_files` 的子集时返回 `True`。
- 逻辑：递归收集 `root` 下所有文件的相对路径，用 glob 忽略模式过滤，再检查剩余文件是否都在允许集合中。

### 4. Excel 内容、结构和样式

#### `evaluate_excel_cell_value(testbed_dir, args)`

- 功能：检查 Excel 指定单元格是否与预期值的字符串表示完全匹配。
- 输入：`testbed_dir`；`args.file` 或 `args.output_file`；`args.matches`，每项包含 `row`、`col`、`value`。
- 输出：`bool`。所有指定单元格都匹配返回 `True`。
- 逻辑：将 workbook 中非空单元格转为 `(row, col): value` 文本，再逐项检查构造出的目标字符串是否出现。

#### `evaluate_excel_cell_comparator(testbed_dir, args)`

- 功能：检查 Excel 指定单元格的值是否满足给定比较器表达式。
- 输入：`testbed_dir`；`args.file` 或 `args.output_file`；`args.matches`，每项包含 `row`、`col`、`comparator`。`comparator` 是字符串形式的 Python 表达式，例如 `lambda x: float(x) > 10`。
- 输出：`bool`。所有单元格都满足比较器返回 `True`。
- 逻辑：读取 workbook 文本，用正则提取指定单元格值；对 `comparator` 执行 `eval` 得到函数，再传入单元格值判断。

#### `evaluate_excel_sheet_exists(testbed_dir, args)`

- 功能：检查 Excel workbook 中必须存在或禁止存在的 sheet 名称。
- 输入：`testbed_dir`；`args.file`；可选 `args.required_sheets`；可选 `args.forbidden_sheets`。
- 输出：`bool`。必需 sheet 全部存在且禁止 sheet 全部不存在时返回 `True`。
- 逻辑：用 `openpyxl` 只读打开 workbook，读取 `sheetnames`，检查 required 子集关系和 forbidden 交集。

#### `evaluate_excel_formula(testbed_dir, args)`

- 功能：检查 Excel 指定单元格中的公式字符串是否精确匹配。
- 输入：`testbed_dir`；`args.file`；`args.matches`，每项包含 `row`、`col`、`formula`，可选 `sheet`。
- 输出：`bool`。所有指定单元格公式都匹配返回 `True`。
- 逻辑：用 `openpyxl(data_only=False)` 打开 workbook，定位单元格，读取 `.value` 并与 `formula` 精确比较。

#### `evaluate_excel_cell_number_tolerance(testbed_dir, args)`

- 功能：按绝对误差容差检查 Excel 指定单元格的数值。
- 输入：`testbed_dir`；`args.file` 或 `args.output_file`；`args.matches`，每项包含 `row`、`col`、`value`，可选 `sheet`、`tolerance`；可选全局 `args.tolerance`。
- 输出：`bool`。所有指定单元格数值都在容差内返回 `True`。
- 逻辑：用 `openpyxl(data_only=True)` 打开 workbook，定位单元格，将实际值和预期值转为浮点数，检查 `abs(actual - expected) <= tolerance`。

#### `evaluate_excel_cell_style(testbed_dir, args)`

- 功能：检查 Excel 单元格的字体、填充、对齐和数字格式。
- 输入：`testbed_dir`；`args.file`；`args.matches`，每项包含 `row`、`col`，可选 `sheet`。可检查 `font_name`、`font_size_pt`、`bold`、`italic`、`underline`、`font_color_rgb`、`fill_color_rgb`、`number_format`、`horizontal_alignment`、`vertical_alignment`。
- 输出：`bool`。所有指定样式都匹配返回 `True`。
- 逻辑：用 `openpyxl` 定位单元格并逐项比较样式；RGB 颜色统一取末 6 位，兼容 `AARRGGBB` 和 `RRGGBB`。

### 5. Office 文档格式

#### `evaluate_docx_font_style(testbed_dir, args)`

- 功能：检查 Word 文档 run 级别的字体设置。
- 输入：`testbed_dir`；`args.file`；`args.matches`。可用 `paragraph_number`、`paragraph_index` 或 `text` 定位段落；可用 `run_index` 或 `run_text` 定位 run；可检查 `font_name`、`font_size_pt`、`bold`、`italic`、`underline`、`color_rgb`。
- 输出：`bool`。每个匹配项都能找到至少一个满足条件的 run 时返回 `True`。
- 逻辑：用 `python-docx` 打开文档，先定位候选段落，再定位候选 run，逐项比较字体名、字号、粗体、斜体、下划线和颜色。

#### `evaluate_docx_paragraph_format(testbed_dir, args)`

- 功能：检查 Word 文档段落级别的格式设置。
- 输入：`testbed_dir`；`args.file`；`args.matches`。可用 `paragraph_number`、`paragraph_index` 或 `text` 定位段落；可检查 `style_name`、`alignment`、`left_indent_pt`、`right_indent_pt`、`first_line_indent_pt`、`space_before_pt`、`space_after_pt`、`line_spacing`。
- 输出：`bool`。每个匹配项都能找到至少一个满足条件的段落时返回 `True`。
- 逻辑：用 `python-docx` 打开文档并定位段落，读取样式、对齐方式和 `paragraph_format`；缩进与段前/段后间距按 pt 比较，`line_spacing` 按数值比较。

#### `evaluate_pptx_text_style(testbed_dir, args)`

- 功能：检查 PowerPoint 文本 run 的字体设置。
- 输入：`testbed_dir`；`args.file`；`args.matches`。可用 `slide_number`、`slide_index`、`text`、`run_text` 定位文本；可检查 `font_name`、`font_size_pt`、`bold`、`italic`、`color_rgb`。
- 输出：`bool`。每个匹配项都能找到至少一个满足条件的文本 run 时返回 `True`；缺少 `python-pptx` 依赖时返回 `False`。
- 逻辑：用 `python-pptx` 打开演示文稿，按 slide 限定范围，遍历文本框、段落和 run；先做文本过滤，再逐项比较字体属性。

### 6. 日历、日志和分发

#### `evaluate_calendar_no_overlap(testbed_dir, args)`

- 功能：检查指定用户的 `.ics` 日历中是否没有重叠事件。
- 输入：`testbed_dir`；`args.username`，对应 `calendar/<username>.ics`。
- 输出：`bool`。所有事件都不重叠返回 `True`。
- 逻辑：用 `icalendar` 读取所有 `VEVENT`，将 naive datetime 转为 UTC aware datetime，按开始时间排序；若任一事件结束时间晚于下一个事件开始时间，则返回 `False`。

#### `evaluate_log_budget(testbed_dir, args)`

- 功能：从 JSON 日志或简单文本日志中检查 token 和耗时预算，适合 Proposal 中的 cost 指标。
- 输入：`testbed_dir`；`args.log_file`；可选 `args.max_tokens`；可选 `args.max_time_seconds`。
- 输出：`bool`。日志存在且指定预算都未超出时返回 `True`。
- 逻辑：优先按 JSON 解析，识别 `tokens`、`total_tokens`、`time_seconds`、`elapsed_seconds`；非 JSON 时用正则提取数值。设置预算但缺少对应数值，或任一数值超过预算，返回 `False`。

#### `evaluate(testbed_dir, evaluate_type, args)`

- 功能：OfficeBench 兼容的轻量分发函数。
- 输入：`testbed_dir`，对 `contain_text` 类型而言直接作为待检查文本；`evaluate_type`，目前仅支持 `contain_text`；`args.keywords`。
- 输出：`bool`。分发到对应检查函数后的布尔结果。
- 逻辑：`evaluate_type == "contain_text"` 时调用内部 `_evaluate_contain_text`；其他类型抛出 `ValueError`。
