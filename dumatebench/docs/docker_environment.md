# DuMateBench Docker 环境说明

本文说明当前 DuMateBench smoke task 的 Docker 环境、故障注入机制、agent 交互方式、评分逻辑和日志产物。当前示例任务位于：

```text
dumatebench/datasets/dev/template_task/
```

该任务用 OdysseyBench 2-12 作为 smoke test。目标是让 agent 从工作区中的会议议程 PDF 提取信息，并生成 `run_outputs/calendar/Alice.ics`。

## 任务包结构

当前任务包采用 task-local 结构：

```text
instruction.md                  面向 agent 的最小任务描述和目标产物路径
task.yaml                       任务元数据和环境契约
network_faults.yaml             网络栈故障配置
tool_faults.yaml                内置工具和 API wrapper 故障配置
workspace_seed/                 容器启动时复制到 /workspace 的初始文件
environment/Dockerfile          任务镜像定义
environment/docker-compose.yaml Docker Compose 服务定义
environment/entrypoint.sh       容器入口脚本
environment/setup.sh            工作区初始化脚本
environment/network_fault_daemon.py
environment/tool_wrapper.py
environment/*_real.py           wrapper 背后的真实工具实现
evaluator/checks.yaml           评分检查项
evaluator/evaluator.py          评分程序
run_logs/                       每次运行重新生成的日志目录
run_outputs/                    每次运行重新生成的输出目录
```

`workspace_seed/` 保存任务初始文件。对当前 smoke task 来说，关键输入文件是：

```text
workspace_seed/files/data/meeting_agenda.pdf
```

此外，`workspace_seed/` 还包含会复制到 `/workspace` 的辅助上下文文件：

```text
workspace_seed/user.md
workspace_seed/soul.md
workspace_seed/session_chat_history.json
```

system prompt 会提示 agent：这些文件可能包含用户偏好、用户配置，以及当前 session 的历史交互记录，可用于理解用户需求和完成任务。

容器启动时，`setup.sh` 会清空并重建 `/workspace`，把 `workspace_seed` 复制进去，创建 `/workspace/calendar`、`/outputs/calendar` 和 `/outputs/emails`，并加入干扰文件，例如 `meeting_agenda_old.pdf` 和 `notes_tmp.txt`。

## 基础镜像

任务镜像基于：

```dockerfile
ARG DUMATE_BASE_IMAGE=python:3.12-slim
FROM ${DUMATE_BASE_IMAGE}
```

Compose 会把 `DUMATE_BASE_IMAGE` 作为 build arg 传入 Dockerfile。默认使用 `python:3.12-slim`，也可以在运行时覆盖为本地镜像、内网镜像或镜像源地址：

```bash
DUMATE_BASE_IMAGE=your-mirror/python:3.12-slim dumatebench/scripts/run_template_task.sh
```

注意，使用本地已有的 Python base image 只会跳过拉取 base image 的网络请求。Dockerfile 之后仍会执行 `apt-get update` 和 `apt-get install`，这一步会访问 Debian 软件源。若构建日志停在 `deb.debian.org/debian trixie`，可以覆盖 apt 源：

```bash
DUMATE_APT_DEBIAN_MIRROR=http://mirrors.aliyun.com/debian \
DUMATE_APT_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security \
dumatebench/scripts/run_template_task.sh
```

镜像预装一组小型 Linux 命令行工具：

```text
bash
ca-certificates
curl
dnsutils
git
iproute2
iptables
iputils-ping
jq
poppler-utils
procps
sudo
unzip
vim-tiny
```

这些工具支持 shell 操作、网络诊断、网页访问、依赖安装、PDF 转换，以及通过 `iptables` 和 `tc netem` 做网络栈级故障注入。

镜像不会预装所有任务专用依赖。当前 smoke task 仍要求 agent 在运行时自行安装或补齐 `tesseract-ocr`、`icalendar`、`requests`、`pypdf`、`pillow`、`pytesseract` 等依赖。这一设计用于模拟“不充分”的办公任务环境。

### LLM Judge 运行环境依赖

Checklist evaluator 和 unified LLM-as-Judge 通常在宿主侧 Python 环境中运行；批量脚本默认使用 `--evaluator-python` 指定的解释器，而不是在 task 容器内运行。因此，LLM-as-Judge 的依赖应安装在 evaluator 运行环境中。

视频产物的多模态 judge 默认使用抽帧模式：

```text
DU_MATE_VIDEO_MODE=frames
```

该模式需要 `ffmpeg` 和 `ffprobe` 能在 evaluator 运行环境的 `PATH` 中找到。也可以用显式路径覆盖：

```bash
DU_MATE_FFMPEG_PATH=/path/to/ffmpeg \
DU_MATE_FFPROBE_PATH=/path/to/ffprobe \
python3 dumatebench/scripts/run_task_batch.py ...
```

常见安装方式如下：

```bash
# macOS
brew install ffmpeg

# conda 环境
conda install -n dumatebench -c conda-forge ffmpeg

# Debian/Ubuntu evaluator 容器或主机
apt-get update && apt-get install -y --no-install-recommends ffmpeg
```

如果把 evaluator 也放到 Docker 镜像中运行，需要在对应 evaluator 镜像里安装 `ffmpeg`。当前 smoke task 的 task 容器不预装 `ffmpeg`，因为它主要约束 agent 执行环境；宿主侧 batch evaluator 不依赖 task 容器中的 `ffmpeg`。

注意：镜像会预置 `tesseract` 的 wrapper 入口，但不会预装真实的系统 OCR 程序 `/usr/bin/tesseract`。agent 安装 `tesseract-ocr` 后，普通命令 `tesseract ...` 仍会先命中 wrapper，因为 `/opt/dumate/wrappers` 位于 `PATH` 最前面。只有直接调用 `/usr/bin/tesseract` 或重写 `PATH` 才会绕过 wrapper。当前最小改进版本在 agent 的 system prompt 中明确要求使用 `PATH` 暴露的工具名，不直接调用绝对系统路径，也不通过改写 `PATH` 绕过 wrapper；这是一层软约束，不是强安全边界。

镜像创建了 `agent` 用户，并授予免密 `sudo`：

```text
agent ALL=(ALL) NOPASSWD:ALL
```

容器入口、工作区初始化和网络故障 daemon 以 root 身份运行。LLM agent 发出的任务命令则通过 `sudo -E -u agent bash -lc ...` 以 `agent` 用户执行，因此 agent 可以安装依赖，但网络故障也可以按用户范围精确命中这些任务命令。

镜像还把 `command_agent.py` 复制到：

```text
/opt/dumate/command_agent.py
```

这意味着 LLM command agent 可以在 task 容器内部运行，而不是由宿主侧 runner 逐条 `docker compose exec` 命令。

## Docker Compose 配置

当前 compose 文件只定义一个服务：

```yaml
services:
  task:
    build:
      context: ../../../..
      dockerfile: datasets/dev/template_task/environment/Dockerfile
    image: dumatebench-template-task:latest
    working_dir: /workspace
    environment:
      DUMATE_TASK_SEED: "20260706"
      DUMATE_NETWORK_FAULT_CONFIG: "/opt/dumate/network_faults.yaml"
      DUMATE_TOOL_FAULT_CONFIG: "/opt/dumate/tool_faults.yaml"
    cap_add:
      - NET_ADMIN
    volumes:
      - ../run_outputs:/outputs
      - ../run_logs:/logs
```

`NET_ADMIN` 是必需权限。没有该 capability，容器内部无法修改 `iptables` 或 `tc` 规则。

`run_outputs/` 和 `run_logs/` 从宿主挂载进容器。这样容器清理后，评分结果、agent trace、故障日志和 Docker Compose 日志仍保留在任务目录中。

## “不充分、不稳定、有噪声”的环境

当前环境模拟三类挑战：依赖不充分、网络/API 不稳定、文件系统有噪声。

### 不充分环境

基础镜像只提供通用工具，不保证任务专用工具存在。agent 需要检查环境，并在需要时安装依赖。

确定性 smoke 脚本会安装：

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends tesseract-ocr
python3 -m pip install --no-cache-dir --break-system-packages \
  icalendar requests pypdf pillow pytesseract
```

LLM agent 不需要使用完全相同的命令。它可以自主执行 shell 命令、访问网络、安装系统包和 Python 包，也可以选择其他命令行策略完成任务。

### 网络栈级故障注入

网络故障配置在：

```text
network_faults.yaml
```

容器启动时，`entrypoint.sh` 会在后台启动：

```bash
/opt/dumate/network_fault_daemon.py \
  --config "${DUMATE_NETWORK_FAULT_CONFIG:-/opt/dumate/network_faults.yaml}" \
  --log /logs/network_faults.jsonl &
```

当前配置包含两层：启动期故障和周期性故障。启动期故障用于保证每次任务开始时都能观察到一次网络异常；周期性故障用于让长任务中的联网搜索、依赖安装、网页访问等操作也可能遇到网络不稳定，而不是只在最初几秒受影响。

当前配置如下：

```yaml
enabled: true
seed: 42
affected_user: "agent"
apply_tc_to_all_traffic: false
exempt_base_urls: []
exempt_domains: []
startup_faults:
  - kind: dns_fail
    probability: 1.0
    duration_seconds: 8
  - kind: latency_loss
    probability: 1.0
    duration_seconds: 8
    delay_ms: 400
    loss_percent: 15
  - kind: block_ip
    probability: 1.0
    duration_seconds: 8
    ip: "93.184.216.34"
    ports: [80, 443]
periodic_faults:
  enabled: true
  interval_seconds: 45
  faults:
    - kind: dns_fail
      probability: 0.35
      duration_seconds: 6
    - kind: latency_loss
      probability: 0.45
      duration_seconds: 10
      delay_ms: 300
      loss_percent: 10
    - kind: block_ip
      probability: 0.25
      duration_seconds: 8
      ip: "93.184.216.34"
      ports: [80, 443]
```

`startup_faults` 会在容器启动后按顺序执行。当前三个启动期故障的概率都是 `1.0`，所以每次运行都会触发一次短窗口故障。

`periodic_faults` 已开启。daemon 在启动期故障结束后进入循环，每 45 秒抽样一轮周期性故障。每个周期故障按自己的 `probability` 独立判断是否触发；触发后持续 `duration_seconds`，然后清除规则。因此，网络错误现在不是只在最初触发，长时间运行的 agent 也可能在后续联网操作中遇到 DNS 失败、延迟丢包或特定 IP 阻断。

`dns_fail` 通过 `iptables` 拒绝出站 TCP/UDP 53 端口流量。`block_ip` 通过 `iptables` 拒绝访问指定 IP 和端口。两者默认都带有 `-m owner --uid-owner <agent uid>` 条件，因此只影响 `agent` 用户发出的任务命令。

`latency_loss` 使用 `tc netem` 注入延迟和丢包。普通 `tc qdisc add dev eth0 root netem ...` 会影响整张网卡，无法直接按 URL、域名或 Linux 用户隔离。当前实现使用两步规则把它限制到任务命令：

1. `iptables -t mangle -m owner --uid-owner <agent uid> -j MARK --set-mark 0x10` 给 `agent` 用户发出的包打 mark。
2. `tc htb` 和 `tc filter fw` 只把带有该 mark 的包导入带 `netem` 的 class。

因此，`agent` 用户执行的任务命令会遇到 `latency_loss`，容器内 root 进程发起的 LLM API 调用不会被该 `tc` class 命中。`apply_tc_to_all_traffic: false` 表示不使用全网卡 `netem`；只要配置了 `affected_user`，daemon 仍会使用 fwmark scoped 模式应用 `latency_loss`。若以后确实需要测试全容器网络抖动，可以清空 `affected_user` 并显式设置 `apply_tc_to_all_traffic: true`。

`exempt_base_urls` 和 `exempt_domains` 记录受信任 LLM endpoint。仓库默认两个字段都为空列表，需要由运行方填入自己的 OpenAI-compatible endpoint，例如：

```text
https://your-openai-compatible-endpoint/v1
your-openai-compatible-endpoint
```

这两个字段用于防止未来新增按域名或 endpoint 的网络规则时误伤 LLM 调用。当前 `iptables -m owner` 规则已经把网络故障限制在 `agent` 用户任务命令上；容器内 `command_agent.py` 的 LLM 请求由 root 进程发起，不会被这些按用户注入的规则命中。

网络 daemon 会把事件写入 `/logs/network_faults.jsonl`。当前日志会标记故障阶段：

- `phase: startup` 表示启动期故障。
- `phase: periodic` 表示周期性故障。
- `iteration` 表示第几轮周期抽样。

示例：

```json
{"event": "selected", "kind": "dns_fail", "phase": "startup", "probability": 1.0, "duration_seconds": 8}
{"event": "apply", "kind": "dns_fail", "affected_user": "agent", "duration_seconds": 8}
{"event": "clear", "kind": "dns_fail", "phase": "startup"}
{"event": "apply", "kind": "latency_loss", "mode": "fwmark_user_scoped", "affected_user": "agent", "fwmark": "0x10"}
{"event": "clear", "kind": "latency_loss", "phase": "startup"}
{"event": "skip", "kind": "dns_fail", "phase": "periodic", "iteration": 1, "probability": 0.35}
{"event": "selected", "kind": "latency_loss", "phase": "periodic", "iteration": 1, "probability": 0.45, "duration_seconds": 10}
{"event": "apply", "kind": "latency_loss", "mode": "fwmark_user_scoped", "affected_user": "agent", "fwmark": "0x10"}
{"event": "clear", "kind": "latency_loss", "phase": "periodic", "iteration": 1}
```

### 工具和 API 层故障注入

工具故障配置在：

```text
tool_faults.yaml
```

镜像把 wrapper 目录放在 `PATH` 最前面：

```dockerfile
ENV PATH="/opt/dumate/wrappers:${PATH}"
```

并创建以下 wrapper 命令：

```text
/opt/dumate/wrappers/tesseract -> /opt/dumate/tool_wrapper.py
/opt/dumate/wrappers/calendar_write -> /opt/dumate/tool_wrapper.py
/opt/dumate/wrappers/mail_send -> /opt/dumate/tool_wrapper.py
/opt/dumate/wrappers/ocr_extract -> /opt/dumate/tool_wrapper.py
```

当 agent 调用这些命令时，`tool_wrapper.py` 会读取 `tool_faults.yaml`，记录本次调用，并按配置决定是否注入错误。若不注入错误，wrapper 会转发给真实工具实现。真实工具位于：

```text
/opt/dumate/bin/calendar_write_real.py
/opt/dumate/bin/mail_send_real.py
/opt/dumate/bin/ocr_extract_real.py
```

`mail_send_real.py` 使用 OfficeBench/OdysseyBench 风格的文件系统邮箱，不连接真实 SMTP 服务。agent 调用：

```bash
mail_send --sender Alice --to Bob --subject "Subject text" --body "Email body text"
```

默认会在 `/outputs/emails/Bob/` 和 `/outputs/emails/Alice/` 下各写入一份 `.eml` 文件。`--to` 可以重复传入，也可以用逗号分隔多个收件人；`--mail-root` 可改写邮箱根目录，默认是 `/outputs/emails`。由于 docker compose 会把 `/outputs` 挂到 task-local 的 `run_outputs`，评估时可以按 `emails/<username>/*.eml` 检查邮件内容。

`tesseract` 是一个特殊情况。Dockerfile 创建了 `tesseract` wrapper，但没有安装真实的 `/usr/bin/tesseract`。agent 安装 `tesseract-ocr` 后，普通命令名 `tesseract` 仍会先进入 wrapper，再由 wrapper 调用 `/usr/bin/tesseract`。如果 agent 直接调用绝对路径 `/usr/bin/tesseract`，或者把 `/usr/bin` 放到 `/opt/dumate/wrappers` 前面，就会绕过工具故障注入。

为了降低误绕过概率，通用 LLM agent system prompt 要求 agent 通过 `PATH` 中暴露的命令名调用 benchmark 工具，例如 `tesseract`、`ocr_extract`、`calendar_write`，不要直接调用 `/usr/bin/tesseract` 等绝对系统路径，也不要重写 `PATH` 来绕过 wrapper。这个方案实现成本最低，但仍属于 prompt 级软约束；如果需要强制防绕过，还需要把真实系统工具移动到 agent 不可直接执行的位置，或通过受控 helper/daemon 调用真实工具。

当前 wrapper 支持以下工具故障类型：

| 故障类型 | 行为 | 默认/建议错误码 |
| --- | --- | --- |
| `OCR_TEMPORARY_UNAVAILABLE`、`CALENDAR_PERMISSION_DENIED`、`MAIL_RATE_LIMIT` | 直接返回配置中的 `exit_code` 和 `stderr`，不调用真实工具。 | 75、77、78 |
| `DELAYED_RESPONSE` | 先 sleep `delay_seconds`，再调用真实工具并返回真实工具退出码。 | 真实工具退出码 |
| `OUTPUT_FIELD_MISSING` | 先调用真实工具，再从输出 artifact 中删除配置的字段。当前支持从 `.ics` 删除 `DESCRIPTION` 等字段。 | 默认真实工具退出码，可配置为 0 |
| `NONDETERMINISTIC_TIMEOUT` | 用 seed 生成 `min_timeout_seconds` 到 `max_timeout_seconds` 之间的超时时间，sleep 后返回 timeout 错误。 | 124 |

注入故障时，`stderr` 只描述错误原因，不带 “retry ...” 这类修复提示。agent 需要自己判断是否重试、切换路径或修复环境。

当前配置如下：

```yaml
enabled: true
seed: 42
tools:
  tesseract:
    faults:
      - kind: OCR_TEMPORARY_UNAVAILABLE
        probability: 1.0
        max_injections: 1
        exit_code: 75
        stderr: "OCR service temporarily unavailable."
      - kind: DELAYED_RESPONSE
        probability: 0.4
        max_injections: 1
        delay_seconds: 5
  calendar_write:
    faults:
      - kind: CALENDAR_PERMISSION_DENIED
        probability: 1.0
        max_injections: 1
        exit_code: 77
        stderr: "Calendar backend returned a transient permission error."
      - kind: OUTPUT_FIELD_MISSING
        probability: 0.35
        max_injections: 1
        fields: [DESCRIPTION]
        exit_code: 0
        stderr: "Calendar backend omitted DESCRIPTION from the output artifact."
  mail_send:
    faults:
      - kind: MAIL_RATE_LIMIT
        probability: 0.15
        max_injections: 1
        exit_code: 78
        stderr: "Mail backend rate limit."
      - kind: NONDETERMINISTIC_TIMEOUT
        probability: 0.25
        max_injections: 1
        min_timeout_seconds: 3
        max_timeout_seconds: 9
        exit_code: 124
        stderr: "Mail backend request timed out."
```

`max_injections: 1` 表示同一工具的同一种故障最多注入一次。这个设置用于测试 agent 是否会观察错误、重试命令、验证输出或切换方案。

工具 wrapper 会把调用写入 `/logs/tool_faults.jsonl`，例如：

```json
{"tool": "tesseract", "fault_injected": true, "kind": "OCR_TEMPORARY_UNAVAILABLE"}
{"tool": "tesseract", "fault_injected": true, "kind": "DELAYED_RESPONSE", "delay_seconds": 5}
{"tool": "calendar_write", "fault_injected": true, "kind": "CALENDAR_PERMISSION_DENIED"}
{"tool": "calendar_write", "fault_injected": true, "kind": "OUTPUT_FIELD_MISSING", "removed_fields": ["DESCRIPTION"]}
{"tool": "mail_send", "fault_injected": true, "kind": "NONDETERMINISTIC_TIMEOUT", "timeout_seconds": 7.214}
```

### 文件系统噪声

`setup.sh` 在复制 seed workspace 后会加入干扰文件：

```bash
cp /workspace/files/data/meeting_agenda.pdf /workspace/files/data/meeting_agenda_old.pdf
printf 'Unrelated temporary note. Do not use.\n' > /workspace/files/data/notes_tmp.txt
```

agent 因此需要检查文件内容和文件名，不能假设目录中只有一个相关文件。

`noise_manifest.json` 记录当前任务的噪声设计：

```json
{
  "target_files": ["files/data/meeting_agenda.pdf"],
  "distractor_files": [
    "files/data/meeting_agenda_old.pdf",
    "files/data/notes_tmp.txt"
  ],
  "noise_types": ["similar_filename", "temporary_file"]
}
```

当前 noise 是确定性的文件系统注入。它不是按 seed 随机生成的 noise generator；seed 主要用于记录任务配置和后续扩展。

## Agent 交互模型

DuMateBench 当前提供两个 runner：

```text
dumatebench/scripts/run_template_task.sh        不调用 LLM 的确定性环境测试
dumatebench/scripts/run_template_task_agent.sh  使用 LLM command agent 的测试
```

确定性 runner 会构建镜像、启动容器，并在容器中运行 `/opt/dumate/agent_smoke.sh`。它用于验证 Docker 环境、依赖安装、网络故障日志、工具故障日志和 evaluator 是否连通。

LLM runner 会构建镜像并运行容器内 agent：

```bash
/opt/dumate/command_agent.py \
  --in-container \
  --task-dir /opt/dumate/task \
  --trusted-base-url "https://your-openai-compatible-endpoint/v1" \
  "$@"
```

宿主侧脚本负责清空旧输出、构建镜像、启动容器、传入环境变量、运行 evaluator、收集 compose 日志和清理 Docker。它不再逐条接收 JSON 命令，也不再通过 `docker compose exec -T --user agent task bash -lc ...` 转发命令。

容器内 `command_agent.py` 的工作流程如下：

1. 读取 `/opt/dumate/task/instruction.md`，构造 LLM user prompt。`task.yaml` 只作为 runner/evaluator 元数据使用，不直接发送给 agent。
2. 校验 `OPENAI_BASE_URL` 是否在受信任列表中，且必须使用 HTTPS。
3. 调用 OpenAI-compatible `/chat/completions` API，要求模型返回 JSON。
4. 若模型返回 `{"command": "...", "reason": "..."}`，则用 `sudo -E -u agent bash -lc ...` 在容器内执行命令。
5. 把命令返回码、耗时、stdout 和 stderr 作为 observation 追加回对话。
6. 若模型返回 `{"finish": true, "reason": "..."}`，则停止执行命令并写出 agent 状态。
7. 若达到 `--max-steps` 仍未 `finish=true`，runner 记录 `max_steps_reached` 并写出 agent 状态。
8. 宿主侧脚本在容器退出后运行 `evaluator/evaluator.py`，并把 evaluator 返回码写回 `run_logs/agent_status.json`。

模型 action 协议是：

```json
{"command": "bash command to run", "reason": "short reason"}
```

任务完成协议是：

```json
{"finish": true, "reason": "short reason"}
```

每条 observation 形如：

```json
{
  "returncode": 0,
  "elapsed_sec": 1.234,
  "output": "command stdout and stderr"
}
```

### 自定义 Agent 接入

DuMateBench 发布数据集和 Docker 环境后，外部用户如果要运行自己的 agent，通常不需要修改任务数据、Dockerfile、故障配置或 evaluator。推荐只替换 agent runner 层，并保持环境契约不变。

需要理解和遵守的固定契约如下：

| 契约 | 当前约定 |
| --- | --- |
| 任务说明 | 从容器内 `/opt/dumate/task/instruction.md` 读取。该文件只包含最小任务描述和目标产物路径。 |
| 任务元数据 | `/opt/dumate/task/task.yaml` 供 runner/evaluator 使用，包括 timeout、工作目录、允许写入路径、故障配置和 evaluator 入口；不直接发送给 agent。 |
| 工作目录 | agent 命令在 `/workspace` 下执行。 |
| 最终产物 | 写到 instruction 要求的路径，通常在 `/outputs` 下。 |
| 日志目录 | 环境和工具日志写到 `/logs`，宿主侧映射为 task-local `run_logs/`。 |
| 输出目录 | `/outputs` 映射为 task-local `run_outputs/`。 |
| 工具入口 | 使用 `PATH` 中暴露的命令名，例如 `tesseract`、`ocr_extract`、`calendar_write`、`mail_send`。 |
| 完成条件 | agent 自己确认目标产物存在后结束；宿主侧随后运行 evaluator。 |

最简单的接入方式是使用轻量 adapter runner。外部 agent 只需要实现一个可执行程序：每一步从 stdin 读取 JSON state，向 stdout 输出 JSON action。接口文档位于：

```text
dumatebench/agents/agent_contract.md
```

运行方式示例：

```bash
dumatebench/scripts/run_task_with_agent.sh 'python3 dumatebench/agents/examples/echo_agent.py' --max-steps 3
```

真实 agent 可以替换为任意命令，例如：

```bash
dumatebench/scripts/run_task_with_agent.sh 'python3 /path/to/my_agent.py'
```

adapter 输入包含 `instruction`、`system_prompt`、历史 action 和 observation。`task.yaml` 是 runner/evaluator 元数据，不发送给 adapter。adapter 输出仍是同一个动作协议：

```json
{"command": "bash command to run", "reason": "short reason"}
```

或：

```json
{"finish": true, "reason": "short reason"}
```

adapter runner 会负责启动 Docker、执行命令、收集 observation、运行 evaluator 和写日志。这样外部用户通常不需要复制或修改 `command_agent.py`。

另一种接入方式是复用现有 LLM runner，只把 `command_agent.py` 内的 LLM 调用或策略逻辑替换成自己的 agent：

```text
Docker 环境、故障注入、/workspace、/outputs、/logs、evaluator 保持不变
替换 dumatebench/agents/command_agent.py 中的决策部分
继续返回 command / finish 形式的动作
```

这两种方式都适合 OpenAI-compatible API agent、本地模型 agent 或基于规则的 agent。只要它能不断产生命令并读取 observation，就可以接入。

更自由的方式是实现自己的宿主侧 runner。此时 runner 需要负责：

1. 启动容器。填充后的任务目录不再自带 `environment/docker-compose.yaml`：`dumate run` 会生成 `.dumate-compose.yaml`，`harbor run` 使用 Harbor 自己的 base overlay。自定义 runner 应复用其中之一,或按同样的服务契约自行生成 compose 文件。
2. 等待环境初始化完成。
3. 将 agent 的命令以 `agent` 用户身份在容器内执行，工作目录为 `/workspace`。
4. 保留 `PATH=/opt/dumate/wrappers:...`，否则会绕过工具故障注入。
5. 保留 `DUMATE_TOOL_FAULT_CONFIG=/opt/dumate/tool_faults.yaml` 和 `DUMATE_TOOL_FAULT_LOG=/logs/tool_faults.jsonl`。
6. 不直接改写 `/logs` 和 `/outputs` 的语义。
7. agent 结束后运行任务声明的 evaluator，并读取 `run_outputs/reward.json`。

如果用户只想评测“自己的 agent 能不能完成任务”，推荐不要改：

```text
instruction.md
task.yaml
network_faults.yaml
tool_faults.yaml
environment/*
evaluator/*
workspace_seed/*
```

这些文件定义 benchmark 实例本身。修改它们会改变任务和评测条件，除非用户是在开发新任务或新环境。

当前设计对自定义 agent 基本友好：任务说明已经压缩为最小目标，环境规则和工具说明集中在 system prompt/runner 层，Docker 通过 `/workspace`、`/outputs`、`/logs` 提供清晰的 I/O 边界，evaluator 独立于 agent。新增的 adapter runner 已经提供了最小 CLI 接口；后续如果要发布成更完整的公共 SDK，可以继续补任务发现、批量运行、结果汇总和更严格的 adapter schema 校验。

`command_agent.py` 默认只信任公开的 OpenAI endpoint:

```text
https://api.openai.com/v1
```

也可以通过 `--trusted-base-url` 或 `DUMATE_TRUSTED_BASE_URLS` 增加可信 base URL。未受信任或非 HTTPS 的 `OPENAI_BASE_URL` 会被拒绝。

## 评分

评分文件位于：

```text
evaluator/checks.yaml
evaluator/evaluator.py
```

当前检查项是：

```yaml
checks:
  - id: calendar_exists
    type: file_exists
    path: run_outputs/calendar/Alice.ics
    weight: 0.40
  - id: api_failure_observed
    type: log_contains
    path: run_logs/tool_faults.jsonl
    value: tool": "tesseract", "fault_injected": true
    weight: 0.20
  - id: network_stack_fault_observed
    type: log_contains
    path: run_logs/network_faults.jsonl
    value: event": "apply
    weight: 0.20
  - id: calendar_wrapper_recovered
    type: log_contains
    path: run_logs/tool_faults.jsonl
    value: tool": "calendar_write", "fault_injected": true
    weight: 0.20
```

evaluator 在宿主侧执行，并写入：

```text
run_outputs/reward.json
```

task-local 的 `evaluator/evaluator.py` 已内联 `dumatebench/evaluator/evaluate.py` 中的通用评估函数，因此 smoke task 的 evaluator 在 Docker 任务包内是自包含的，不需要额外 import 外层 `dumatebench.evaluator` 包。旧的 `file_exists` 和 `log_contains` 检查仍然可用，其中 `file_exists` 通过内联的 `evaluate_file_exist` 实现。

需要调用其他通用函数时，可以在 `checks.yaml` 中使用 `type: evaluator_function` 和 `function`：

```yaml
checks:
  - id: calendar_text
    type: evaluator_function
    function: evaluate_contain
    testbed_dir: run_outputs
    args:
      doc_type: ics
      file: calendar/Alice.ics
      keywords: [meeting agenda]
    weight: 0.20
```

也可以直接把 `type` 写成任意内联的 `evaluate_*` 函数名：

```yaml
checks:
  - id: calendar_text
    type: evaluate_contain
    testbed_dir: run_outputs
    args:
      doc_type: ics
      file: calendar/Alice.ics
      keywords: [meeting agenda]
    weight: 0.20
```

`testbed_dir` 是相对 task 目录的路径；不写时默认使用 task 根目录。`args` 会原样传给对应的 evaluator 函数。

输出对象包含：

```json
{
  "task_id": "template_task",
  "complete_pass": 1,
  "partial_pass": 1.0,
  "environment_recovery": 1,
  "network_recovery": 1,
  "checks": []
}
```

`complete_pass` 只有在所有检查项通过时才为 `1`。`partial_pass` 是通过检查项数量占全部检查项数量的比例，每个检查项权重相同。`environment_recovery` 当前表示是否生成预期 calendar artifact。`network_recovery` 当前表示是否观察到网络栈故障应用事件。

即使 LLM agent 因达到 `--max-steps` 停止，宿主侧 evaluator 也会正常运行，`agent_status.json` 会记录该状态和 evaluator 返回码，Docker 也会按 runner 的 `trap` 逻辑清理。

## 运行日志和输出

每次运行前，runner 会清空 `run_logs/` 和 `run_outputs/`。常见文件如下：

```text
run_logs/agent_llm.jsonl       模型 action 和命令 observation 的 JSONL trace
run_logs/agent_llm.log         适合人工阅读的 LLM agent trace
run_logs/agent_status.json     agent 是否完成、步数、max-steps 状态、evaluator 返回码
run_logs/llm_endpoint.jsonl    受信任 LLM base URL 记录
run_logs/network_faults.jsonl  网络故障 daemon 事件
run_logs/tool_faults.jsonl     工具 wrapper 调用和注入事件
run_logs/compose.log           清理阶段收集的 Docker Compose 日志
run_logs/agent_smoke.log       确定性 smoke runner 日志
run_outputs/calendar/Alice.ics 预期任务产物
run_outputs/reward.json        evaluator 输出
```

`agent_smoke.log` 只在确定性 smoke runner 中生成。LLM agent 运行时主要看 `agent_llm.jsonl`、`agent_llm.log`、`agent_status.json`、`network_faults.jsonl`、`tool_faults.jsonl` 和 `reward.json`。

## 常用命令

运行确定性 smoke test：

```bash
dumatebench/scripts/run_template_task.sh
```

运行 LLM command agent：

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint/v1"
# 只有 https://api.openai.com/v1 是默认可信的；其他 endpoint 必须在这里显式声明。
export DUMATE_TRUSTED_BASE_URLS="https://your-openai-compatible-endpoint/v1"
export DUMATE_MODEL="gpt-4o"
# 可选：指定装好 evaluator 依赖的宿主侧 Python/venv
export DUMATE_EVALUATOR_PYTHON=".venv/bin/python"

dumatebench/scripts/run_template_task_agent.sh --max-steps 20
```

查看主要日志：

```bash
sed -n '1,160p' dumatebench/datasets/dev/template_task/run_logs/agent_status.json
sed -n '1,160p' dumatebench/datasets/dev/template_task/run_logs/agent_llm.log
sed -n '1,160p' dumatebench/datasets/dev/template_task/run_logs/network_faults.jsonl
sed -n '1,160p' dumatebench/datasets/dev/template_task/run_logs/tool_faults.jsonl
sed -n '1,160p' dumatebench/datasets/dev/template_task/run_outputs/reward.json
```
