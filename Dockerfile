# DevFlow AI 服务器镜像：Python 3.12 + Playwright Chromium + git + gh CLI + Claude Code
# 登录态（gh / claude）放在 /home/devflow 卷里，重建镜像不丢
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright DISABLE_AUTOUPDATER=1

RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Claude Code 原生二进制（无需 node）：装到 /usr/local/bin，不受 /home 卷影响
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && cp -L /root/.local/bin/claude /usr/local/bin/claude && chmod 755 /usr/local/bin/claude \
    && rm -rf /root/.local/share/claude /root/.local/bin/claude \
    && claude --version

WORKDIR /app
COPY pyproject.toml README.md ./
COPY devflow ./devflow
RUN pip install . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

RUN useradd -m -u 1000 devflow && mkdir -p /app/data /opt/repos && chown -R devflow:devflow /app /opt/repos
USER devflow
ENV HOME=/home/devflow
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8765/health || exit 1
CMD ["devflow", "serve", "--no-browser"]
