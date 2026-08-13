ARG DUMATE_BASE_IMAGE=python:3.12-slim
FROM ${DUMATE_BASE_IMAGE}
ARG DUMATE_APT_DEBIAN_MIRROR=
ARG DUMATE_APT_SECURITY_MIRROR=

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

RUN useradd -ms /bin/bash agent && \
    echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && \
    chmod 0440 /etc/sudoers.d/agent && \
    mkdir -p /workspace /outputs /logs /opt/dumate/bin /opt/dumate/wrappers /opt/dumate/task

COPY agents/command_agent.py /opt/dumate/command_agent.py
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/workspace_seed/ /workspace_seed/
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/instruction.md /opt/dumate/task/instruction.md
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/task.yaml /opt/dumate/task/task.yaml
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/evaluator/ /opt/dumate/task/evaluator/
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/network_faults.yaml /opt/dumate/network_faults.yaml
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/tool_faults.yaml /opt/dumate/tool_faults.yaml
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/setup.sh /opt/dumate/setup.sh
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/entrypoint.sh /opt/dumate/entrypoint.sh
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/agent_smoke.sh /opt/dumate/agent_smoke.sh
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/network_fault_daemon.py /opt/dumate/network_fault_daemon.py
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/tool_wrapper.py /opt/dumate/tool_wrapper.py
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/calendar_write_real.py /opt/dumate/bin/calendar_write_real.py
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/mail_send_real.py /opt/dumate/bin/mail_send_real.py
COPY datasets/dev/c55e3de1c8b843cfbacf728852d2e92b/c55e3de1c8b843cfbacf728852d2e92b_ses_112dead3cffeft0GRI5w4RduKj/.batch_runtime/smoke/environment/ocr_extract_real.py /opt/dumate/bin/ocr_extract_real.py

RUN chmod +x \
      /opt/dumate/command_agent.py \
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
