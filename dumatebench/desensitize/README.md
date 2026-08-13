# DuMateBench 脱敏逻辑说明

本目录的 Go 代码包含两套相关逻辑：

1. `desensitize.go`：真正执行字符串替换的脱敏算子，主要覆盖云厂商密钥、OpenAI/BCE/AWS/阿里云等 secret key。
2. `engine.go` + `dlp_v1.go`：DLP 审计引擎和规则集，原始 Go 逻辑用于发现命中项。Python 版本将这些审计规则也实现为替换规则，便于批量改写数据集文件。

Python 实现入口：

- 核心逻辑：[core.py](/Users/niuzechun/Documents/DuMateBench/dumatebench/desensitize/core.py)
- 批处理命令：[__main__.py](/Users/niuzechun/Documents/DuMateBench/dumatebench/desensitize/__main__.py)

## 1. Go `StringDesensitize` 逻辑

### 输入处理

- 输入为空时不处理。
- 优先按 JSON 对象解析。
- JSON 解析失败时，按普通字符串扫描并替换。
- JSON 对象中只处理字符串、对象、数组；数字、布尔、空值不处理。
- 发生替换后，会在顶层 JSON 对象追加 `desensitize_rule_hits` 字段，记录规则命中次数。

Python 版本扩展为支持顶层 JSON 数组；顶层对象仍会追加 `desensitize_rule_hits`。JSON 文件输出时使用 2 空格缩进并保留多行格式，不会压缩成单行。JSONL 文件按行处理，保持一行一条记录。

### 白名单字段

以下字段默认跳过，不做脱敏：

- `account_id`
- `account_type`
- `appid_v2`
- `cloud_id`
- `url`
- `as_id`
- `request_id`

匹配方式：

- 字段名精确匹配，例如任意层级的 `request_id`。
- dot path 精确匹配，例如 `payload.request_id`。

Python CLI 可通过 `--whitelist-fields` 增加白名单，支持逗号字符串或 JSON 数组。

### Secret Key 规则

所有命中值都替换为 `***`，规则命中统一记为 `secret_key`。

| 覆盖类型 | 规则摘要 | 替换范围 |
| --- | --- | --- |
| 阿里云 AccessKey ID | `LTAI` 开头，后接 20 位字母数字 | 仅 key 本身 |
| 阿里云相关赋值 | `alibaba... =/ := / > / <= / => / ||:` 后跟 30 位值 | 仅引号内 30 位值 |
| AWS Access Key ID | `AKIA`、`ASIA`、`AIDA` 等前缀，后接 16 位大写字母数字 | 整个 key |
| AWS Secret Access Key | 字段名形似 `aws_secret_access_key`，后跟 40 位 base64 风格值 | 仅 secret 值 |
| BCE/百度云 AK | 32 位十六进制特征值或 `ALTAK` 后接 21 位 | 整个 key |
| 通用 secret 赋值 | `ACCESS_KEY_SECRET`、`secret_access_key`、`sk` 等后接 32 位字母数字 | 仅赋值 |
| OpenAI 风格 key | `sk-` 开头，后接 20 位以上字母数字、下划线或连字符 | 仅 key 本身 |
| BCE v3 签名串 | `bce-v3/ALTAK.../<40位签名>` | AK 和签名分别替换 |
| BCE v1-v3 签名串 | `bce-v1/2/3/ALTAK-.../<20位以上签名>` | AK 和签名分别替换 |
| ALTAK 独立 key | `ALTAK-` 后接 21 位 | 仅 key 本身 |
| 40 位十六进制串 | 边界内的 40 位 hex | 仅 hex 值 |

## 2. DLP v1 审计规则

Go 审计引擎会遍历 JSON 任意对象/数组；字符串若本身是 JSON 字符串，会继续按嵌套 JSON 解析。对普通文本，按规则扫描；对字段级规则，还会构造 `field="value"` 形式来判断敏感字段值。

Python 版本把这些规则用于替换：

| 规则 ID | 状态 | 说明 | 替换范围 |
| --- | --- | --- | --- |
| `ID_CARD` | active | 中国大陆 18 位身份证号；额外校验出生日期合法性 | 身份证号 |
| `EMAIL` | active | 邮箱地址 | 整个邮箱 |
| `PHONE_CN` | active | 中国大陆手机号，`1[3-9]` 开头共 11 位 | 手机号 |
| `PLATE_CN` | active | 中国车牌号 | 整个车牌 |
| `PRIVATE_KEY` | candidate | PEM 私钥块，包括 RSA/EC/DSA/OpenSSH/PGP/Encrypted | 整个私钥块 |
| `DB_CONN` | candidate | `mysql://`、`postgres://`、`mongodb://`、`redis://` 等连接串 | 整个连接串 |
| `JWT` | candidate | JWT 三段式 token | 整个 token |
| `GITHUB_TOKEN` | candidate | `ghp_`、`gho_`、`ghu_`、`ghs_`、`ghr_` 开头的 GitHub token | 整个 token |
| `SECRET_KV_QUOTED` | candidate | `api-key`、`access_token`、`password`、`密码`、`密钥` 等字段赋值 | 引号内值；JSON 敏感字段值会整体替换 |
| `PASSWORD_FUNC` | candidate | 函数调用第一参数位置的明文口令 | 第一参数值 |

## 3. 批处理范围

命令行默认遍历 `dumatebench/datasets/dev` 下非隐藏文件；隐藏文件例如 `.DS_Store` 默认跳过，可用 `--include-hidden` 打开。

默认只处理每个任务目录内的以下文件：

- `instruction.md`
- `session_chat_history.json`
- `workspace_seed/` 或 `work_space_seed/` 下的 `.md`、`.json` 文件
- `evaluator/checks.yaml`

其它文件一律不处理，包括：

- `environment/` 下的代码和日志
- `evaluator/evaluator.py` 等评测代码
- `task.yaml`、`annotation_review.json`、`manifest.json`、`task_type_features.json`
- `run_logs/`、`run_outputs/`、批处理汇总文件
- `workspace_seed/` 下的 `.py`、`.txt`、Office/PDF/图片等非 `.md/.json` 文件

以下二进制或富文档文件默认跳过，避免直接正则改字节导致文件损坏：

- `.docx`、`.xlsx`、`.pptx`
- `.pdf`
- `.png`、`.jpg`、`.jpeg`
- `.mp4`
- `.pyc`、`.zip` 等

如果需要处理 Office/PDF/图片中的文本，需要分别通过文档解析、PDF 提取或 OCR 管道实现，不能直接套用当前文本正则脚本。

## 4. 运行命令

先做 dry run 查看命中：

```bash
conda run -n dumatebench python -m dumatebench.desensitize --input dumatebench/datasets/dev --dry-run
```

原地改写 `dumatebench/datasets/dev` 下的文本文件：

```bash
conda run -n dumatebench python -m dumatebench.desensitize --input dumatebench/datasets/dev --in-place
```

输出一份脱敏副本，不改原目录：

```bash
conda run -n dumatebench python -m dumatebench.desensitize --input dumatebench/datasets/dev --output-dir /tmp/dumatebench-dev-desensitized
```

增加白名单字段：

```bash
conda run -n dumatebench python -m dumatebench.desensitize --input dumatebench/datasets/dev --in-place --whitelist-fields 'trace_id,metadata.token'
```

只使用 secret key 规则，不使用 DLP v1 个人信息规则：

```bash
conda run -n dumatebench python -m dumatebench.desensitize --input dumatebench/datasets/dev --in-place --no-include-dlp
```
