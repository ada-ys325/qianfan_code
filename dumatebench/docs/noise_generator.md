# DuMateBench Noise 生成器说明

本文说明 DuMateBench 随机 noise 注入器的用途、支持的噪声类型、CLI/API 用法、manifest 格式和测试方式。代码位于：

```text
dumatebench/noise/
```

运行 DuMateBench 相关命令时，默认使用 `dumatebench` conda 环境：

```bash
conda run -n dumatebench python ...
```

建议从仓库上一层目录运行命令，也就是 `DuMateBench/`，这样 Python 可以正确导入 `dumatebench` 包。

## 目标

noise 生成器用于根据给定数据文件生成干扰文件，帮助构造“不充分、不稳定、有噪声”的任务工作区。它默认不修改原始文件，而是在输出目录中生成噪声文件，并写出 manifest 记录每个噪声文件的来源和类型。

当前支持两类噪声：

```text
文件噪声：相似命名文件、历史版本、临时文件、备份文件、无关项目文件
数据噪声：按文件格式生成内容级干扰
```

## CLI 用法

基本用法：

```bash
conda run -n dumatebench python -m dumatebench.noise \
  dumatebench/datasets/dev/odyssey_2_12_smoke/workspace_seed/files/data/meeting_agenda.pdf \
  --output-dir /tmp/dumate-noise \
  --seed 20260706
```

生成目录下会包含噪声文件和 `noise_manifest.json`。默认每个输入文件生成 3 个文件噪声和 2 个数据噪声。

常用参数：

```text
inputs                         输入文件或目录
-o, --output-dir               噪声文件输出目录；默认写到输入文件同级目录
--seed                         随机种子，用于可复现生成
--file-noise-count             每个输入文件生成多少个文件级噪声
--data-noise-count             每个输入文件生成多少个内容级噪声
--no-file-noise                关闭文件级噪声
--no-data-noise                关闭内容级噪声
--manifest                     指定 manifest 输出路径
--recursive                    输入为目录时递归扫描文件
```

只生成数据噪声：

```bash
conda run -n dumatebench python -m dumatebench.noise \
  path/to/data.xlsx \
  --output-dir path/to/noise \
  --no-file-noise \
  --data-noise-count 3
```

递归处理目录：

```bash
conda run -n dumatebench python -m dumatebench.noise \
  path/to/workspace_seed/files \
  --recursive \
  --output-dir path/to/generated_noise \
  --manifest path/to/noise_manifest.json
```

## Python API

可以在任务构建脚本中直接调用：

```python
from pathlib import Path

from dumatebench.noise import NoiseConfig, inject_noise

manifest = inject_noise(
    [Path("workspace_seed/files/data/meeting_agenda.pdf")],
    NoiseConfig(
        output_dir=Path("workspace_seed/files/data"),
        seed=20260706,
        file_noise_count=3,
        data_noise_count=2,
    ),
)
```

也可以显式创建生成器：

```python
from dumatebench.noise import NoiseConfig, NoiseInjector

injector = NoiseInjector(NoiseConfig(seed=42))
manifest = injector.generate(["path/to/report.docx", "path/to/metrics.xlsx"])
```

## 噪声类型

### 文件噪声

文件噪声强调“路径和文件名看起来可信，但关键内容不同”。当前内置类型包括：

```text
historical_version   例如 report_old.docx
temporary_file       例如 ~$report.tmp
backup_file          例如 report.backup.docx
unrelated_project    例如 project_notes_1234.txt
```

这些文件会保留相似文件名、相似关键词或源文件名提示，但会写入不同项目、不同数值、过期状态等内容，避免与原文件关键答案一致。

### Excel 数据噪声

对 `.xlsx` 文件，生成器会尽量使用 `openpyxl` 创建真实工作簿噪声：

```text
distractor_sheet     干扰 sheet，表头相似但实体和数值不同
unit_conversion      单位转换干扰，例如不同货币、距离或质量单位
reordered_summary    重排摘要，改变排序或统计口径
```

如果运行环境缺少 `openpyxl`，生成器会用标准库写出最小 OOXML `.xlsx` 文件作为兜底。

### Word/PPT/PDF 数据噪声

对 `.docx`、`.pptx`、`.pdf` 文件，生成器支持：

```text
similar_keywords     包含相似关键字，但事实、日期和数值不同
duplicated_content   重复相似段落，干扰检索和摘要
truncated_page       截断页，只保留不完整上下文
```

`.docx` 和 `.pptx` 优先使用 `python-docx`、`python-pptx` 写结构化文件；依赖缺失时，会写出最小 OOXML 兜底文件。`.pdf` 使用标准库写出最小可识别 PDF。

### 图片、音频和视频噪声

对图片、音频和视频类文件：

```text
图片：生成带噪点、条纹或无关视觉素材的图片内容
WAV：生成带白噪声和提示音的一秒音频
视频：生成无关视频噪声占位素材
```

图片生成不强依赖 Pillow。没有 Pillow 时，生成器会写出 PPM/BMP 风格的最小图像内容；如果扩展名是 `.png` 或 `.jpg`，内容仍可能是可嗅探的 PPM 字节，因此对严格依赖扩展名的图像查看器不一定友好。需要更真实的 PNG/JPEG 噪声时，可以在环境中加入 Pillow 后扩展该 writer。

## Manifest 格式

生成结果会返回并写出 JSON manifest，示例：

```json
{
  "schema_version": "0.1",
  "seed": 20260706,
  "target_files": [
    "workspace_seed/files/data/meeting_agenda.pdf"
  ],
  "records": [
    {
      "source_file": "workspace_seed/files/data/meeting_agenda.pdf",
      "noise_file": "workspace_seed/files/data/meeting_agenda_old.pdf",
      "category": "file_noise",
      "noise_type": "historical_version",
      "description": "历史版本文件，保留相似命名但内容改为过期项目。"
    }
  ],
  "distractor_files": [
    "workspace_seed/files/data/meeting_agenda_old.pdf"
  ],
  "noise_types": [
    "historical_version"
  ]
}
```

字段含义：

```text
schema_version    manifest 格式版本
seed              本次生成使用的随机种子
target_files      输入源文件列表
records           每个噪声文件的详细记录
distractor_files  噪声文件路径列表，便于任务配置直接引用
noise_types       本次生成包含的噪声类型集合
```

## 可复现性和覆盖策略

同一组输入、同一输出目录状态和同一 `seed` 会生成稳定的噪声命名与内容。默认 `overwrite=False`，如果目标文件已存在，会自动追加 `_1`、`_2` 等后缀寻找可用路径。若任务构建脚本需要完全复现文件名，建议先清理输出目录，或使用新的空目录生成。

生成器不删除文件，也不修改输入源文件。将噪声注入正式 `workspace_seed` 前，建议先在临时目录生成并检查 manifest。

## 测试

运行 noise 生成器测试：

```bash
conda run -n dumatebench python -m unittest dumatebench.tests.test_noise_injector
```

运行 DuMateBench 全量单元测试：

```bash
conda run -n dumatebench python -m unittest discover dumatebench/tests
```

注意：以上命令应从 `DuMateBench/` 目录执行。如果从 `DuMateBench/dumatebench/` 子目录执行，部分测试会因为 Python 包导入路径不同而找不到 `dumatebench` 顶层包。
