ARG DUMATE_BASE_IMAGE=python:3.12-slim
FROM ${DUMATE_BASE_IMAGE}
ARG DUMATE_APT_DEBIAN_MIRROR=
ARG DUMATE_APT_SECURITY_MIRROR=
ARG DUMATE_AGENT_BACKEND=react
ARG DUMATE_CLAUDE_CODE_VERSION=stable
ARG DUMATE_CODEX_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive
ENV WORKDIR=/workspace

RUN if [ -n "${DUMATE_APT_DEBIAN_MIRROR}" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|http://deb.debian.org/debian|${DUMATE_APT_DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -n "${DUMATE_APT_SECURITY_MIRROR}" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|http://deb.debian.org/debian-security|${DUMATE_APT_SECURITY_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      dnsutils \
      git \
      iproute2 \
      iptables \
      iputils-ping \
      jq \
      poppler-utils \
      procps \
      sudo \
      unzip \
      vim-tiny && \
    rm -rf /var/lib/apt/lists/*

# Native coding-agent CLIs are opt-in image layers. ReAct remains the default
# lightweight path; Claude Code and Codex install only when selected.
RUN set -eu; \
    if [ "${DUMATE_AGENT_BACKEND}" = "claude-code" ] || [ "${DUMATE_AGENT_BACKEND}" = "codex" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends nodejs npm ripgrep; \
      rm -rf /var/lib/apt/lists/*; \
    fi; \
    if [ "${DUMATE_AGENT_BACKEND}" = "claude-code" ]; then \
      if curl -fsSL https://claude.ai/install.sh -o /tmp/install-claude.sh && \
         bash /tmp/install-claude.sh "${DUMATE_CLAUDE_CODE_VERSION}"; then \
        install -m 0755 /root/.local/bin/claude /usr/local/bin/claude; \
      else \
        claude_npm_version="${DUMATE_CLAUDE_CODE_VERSION}"; \
        if [ "${claude_npm_version}" = "stable" ] || [ "${claude_npm_version}" = "latest" ]; then claude_npm_version=latest; fi; \
        npm install -g "@anthropic-ai/claude-code@${claude_npm_version}"; \
      fi; \
      rm -f /tmp/install-claude.sh; \
      claude --version; \
    elif [ "${DUMATE_AGENT_BACKEND}" = "codex" ]; then \
      if curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh && \
         sh /tmp/install-codex.sh; then \
        install -m 0755 /root/.local/bin/codex /usr/local/bin/codex; \
      else \
        npm install -g "@openai/codex@${DUMATE_CODEX_VERSION}"; \
      fi; \
      rm -f /tmp/install-codex.sh; \
      codex --version; \
    elif [ "${DUMATE_AGENT_BACKEND}" != "react" ]; then \
      echo "Unsupported DUMATE_AGENT_BACKEND=${DUMATE_AGENT_BACKEND}" >&2; \
      exit 2; \
    fi

RUN useradd -ms /bin/bash agent && \
    echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && \
    chmod 0440 /etc/sudoers.d/agent && \
    mkdir -p /workspace /outputs /logs /opt/dumate/bin /opt/dumate/wrappers /opt/dumate/task

COPY agents/command_agent.py /opt/dumate/command_agent.py
COPY agents/native_agent.py /opt/dumate/native_agent.py
COPY datasets/dev/odyssey_2_12_smoke/workspace_seed/ /workspace_seed/
COPY datasets/dev/odyssey_2_12_smoke/instruction.md /opt/dumate/task/instruction.md
COPY datasets/dev/odyssey_2_12_smoke/task.yaml /opt/dumate/task/task.yaml
COPY datasets/dev/odyssey_2_12_smoke/evaluator/ /opt/dumate/task/evaluator/
COPY datasets/dev/odyssey_2_12_smoke/network_faults.yaml /opt/dumate/network_faults.yaml
COPY datasets/dev/odyssey_2_12_smoke/tool_faults.yaml /opt/dumate/tool_faults.yaml
COPY datasets/dev/odyssey_2_12_smoke/environment/setup.sh /opt/dumate/setup.sh
COPY datasets/dev/odyssey_2_12_smoke/environment/entrypoint.sh /opt/dumate/entrypoint.sh
COPY datasets/dev/odyssey_2_12_smoke/environment/agent_smoke.sh /opt/dumate/agent_smoke.sh
COPY datasets/dev/odyssey_2_12_smoke/environment/network_fault_daemon.py /opt/dumate/network_fault_daemon.py
COPY datasets/dev/odyssey_2_12_smoke/environment/tool_wrapper.py /opt/dumate/tool_wrapper.py
COPY datasets/dev/odyssey_2_12_smoke/environment/calendar_write_real.py /opt/dumate/bin/calendar_write_real.py
COPY datasets/dev/odyssey_2_12_smoke/environment/mail_send_real.py /opt/dumate/bin/mail_send_real.py
COPY datasets/dev/odyssey_2_12_smoke/environment/ocr_extract_real.py /opt/dumate/bin/ocr_extract_real.py

RUN chmod +x \
      /opt/dumate/command_agent.py \
      /opt/dumate/native_agent.py \
      /opt/dumate/setup.sh \
      /opt/dumate/entrypoint.sh \
      /opt/dumate/agent_smoke.sh \
      /opt/dumate/network_fault_daemon.py \
      /opt/dumate/tool_wrapper.py \
      /opt/dumate/bin/calendar_write_real.py \
      /opt/dumate/bin/mail_send_real.py \
      /opt/dumate/bin/ocr_extract_real.py && \
    ln -s /opt/dumate/tool_wrapper.py /opt/dumate/wrappers/tesseract && \
    ln -s /opt/dumate/tool_wrapper.py /opt/dumate/wrappers/calendar_write && \
    ln -s /opt/dumate/tool_wrapper.py /opt/dumate/wrappers/mail_send && \
    ln -s /opt/dumate/tool_wrapper.py /opt/dumate/wrappers/ocr_extract && \
    ln -s /outputs /opt/dumate/task/run_outputs && \
    ln -s /logs /opt/dumate/task/run_logs

WORKDIR /workspace
ENV PATH="/opt/dumate/wrappers:${PATH}"
ENV DUMATE_TOOL_FAULT_CONFIG="/opt/dumate/tool_faults.yaml"
ENV DUMATE_TOOL_FAULT_LOG="/logs/tool_faults.jsonl"
ENTRYPOINT ["/opt/dumate/entrypoint.sh"]
