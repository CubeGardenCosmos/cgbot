# cgbot-sandbox: per-user Codex app-server sandbox image.
# Build context: the sandbox/ directory. bridge/ (bridge MCP) and skills/image-gen are produced
# by their owning slices and copied in at build time.
# ---- cgmusic-build: 编译 sandbox/tools/cgmusic(C++17/CMake) ----
# 与最终镜像同为 Debian bookworm(node:22-bookworm-slim),动态库 ABI 一致,运行库见最终阶段 apt 层。
FROM node:22-bookworm-slim AS cgmusic-build
ENV DEBIAN_FRONTEND=noninteractive
RUN node -e "const fs=require('node:fs'); const p='/etc/apt/sources.list.d/debian.sources'; fs.writeFileSync(p,fs.readFileSync(p,'utf8').replaceAll('deb.debian.org','mirrors.aliyun.com').replaceAll('security.debian.org','mirrors.aliyun.com'))" \
    && printf '%s\n' 'Acquire::http::Proxy "DIRECT";' 'Acquire::https::Proxy "DIRECT";' > /etc/apt/apt.conf.d/99direct \
    && apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config \
      libfluidsynth-dev libebur128-dev libsndfile1-dev libmp3lame-dev libpugixml-dev libzip-dev \
    && rm -rf /var/lib/apt/lists/*
COPY tools/cgmusic /src/cgmusic
RUN cmake -S /src/cgmusic -B /src/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /src/build --parallel \
    && mkdir -p /out && cp /src/build/cgmusic /out/cgmusic && strip /out/cgmusic

# ---- final image ----
FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.166.0
ARG PLAYWRIGHT_MCP_VERSION=0.0.83
ARG CCLSP_VERSION=0.7.0
ARG PYRIGHT_VERSION=1.1.414
ARG TYPESCRIPT_LANGUAGE_SERVER_VERSION=6.0.2
ARG TYPESCRIPT_VERSION=5.9.3

ENV DEBIAN_FRONTEND=noninteractive \
    npm_config_registry=https://registry.npmmirror.com \
    UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MATPLOTLIBRC=/etc/matplotlibrc \
    PATH=/home/agent/.local/bin:$PATH \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_BIN_DIR=/usr/local/bin \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium \
    HOME=/home/agent \
    CODEX_HOME=/home/agent/.codex

RUN node -e "const fs=require('node:fs'); const p='/etc/apt/sources.list.d/debian.sources'; fs.writeFileSync(p,fs.readFileSync(p,'utf8').replaceAll('deb.debian.org','mirrors.aliyun.com').replaceAll('security.debian.org','mirrors.aliyun.com'))" \
    && printf '%s\n' 'Acquire::http::Proxy "DIRECT";' 'Acquire::https::Proxy "DIRECT";' > /etc/apt/apt.conf.d/99direct \
    && apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git bash tini chromium fonts-noto-cjk fontconfig \
      python3 python3-markdown weasyprint xz-utils procps openssh-client netcat-openbsd \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim imagemagick ffmpeg \
      libfluidsynth3 libebur128-1 libsndfile1 libmp3lame0 libpugixml1v5 libzip4 \
    && rm -rf /var/lib/apt/lists/*

COPY fontconfig/99-cgbot-cjk.conf /etc/fonts/conf.d/99-cgbot-cjk.conf
COPY requirements.txt /opt/python-requirements.txt
COPY matplotlibrc /etc/matplotlibrc
COPY --chmod=555 pip /usr/local/bin/pip
# uv bootstraps the image-owned default runtime; it does not manage its packages.
# Opt only /opt/python into pip/user installs, preserving Debian's PEP 668 policy.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && uv python install 3.12 --default \
    && python3 -c 'from pathlib import Path; import sysconfig; stdlib = Path(sysconfig.get_path("stdlib")).resolve(); assert stdlib.is_relative_to("/opt/python"), stdlib; (stdlib / "EXTERNALLY-MANAGED").unlink(missing_ok=True)' \
    && python3 -m ensurepip --upgrade --default-pip \
    && ln -s pip /usr/local/bin/pip3 \
    && python3 -m pip install --no-cache-dir -r /opt/python-requirements.txt \
    && chmod -R a+rX /opt/python

# Agent CLIs and language servers, pinned. Browser binaries are not downloaded:
# PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 keeps the system Chromium as the only browser.
RUN npm install -g --no-audit --no-fund \
      @openai/codex@${CODEX_VERSION} \
      @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
      cclsp@${CCLSP_VERSION} \
      pyright@${PYRIGHT_VERSION} \
      typescript-language-server@${TYPESCRIPT_LANGUAGE_SERVER_VERSION} \
      typescript@${TYPESCRIPT_VERSION} \
    && npm cache clean --force \
    && codex --version | grep -q "codex-cli ${CODEX_VERSION}$" \
    && pyright --version >/dev/null \
    && typescript-language-server --version >/dev/null

COPY tools/findata /opt/cgbot/tools/findata
RUN python3 -m pip install --no-cache-dir --no-deps /opt/cgbot/tools/findata \
    && ln -sf "$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/findata" /usr/local/bin/findata \
    && findata --help >/dev/null

COPY tools/cgdl /opt/cgbot/tools/cgdl
RUN python3 -m pip install --no-cache-dir --no-deps /opt/cgbot/tools/cgdl \
    && ln -sf "$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/cgdl" /usr/local/bin/cgdl \
    && cgdl --help >/dev/null

COPY tools/cgmidi /opt/cgbot/tools/cgmidi
RUN python3 -m pip install --no-cache-dir --no-deps /opt/cgbot/tools/cgmidi \
    && ln -sf "$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/cgmidi" /usr/local/bin/cgmidi \
    && cgmidi --help >/dev/null

# cgmusic:由 cgmusic-build 阶段编译的原生合成器;运行库在上面的 apt 层安装。
COPY --from=cgmusic-build /out/cgmusic /usr/local/bin/cgmusic
RUN ! ldd /usr/local/bin/cgmusic | grep -q 'not found'

COPY tools/richdoc /opt/cgbot/tools/richdoc
RUN cd /opt/cgbot/tools/richdoc \
    && npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force \
    && chmod 0755 bin/richdoc.js \
    && ln -s /opt/cgbot/tools/richdoc/bin/richdoc.js /usr/local/bin/richdoc

COPY --chmod=555 md2pdf /usr/local/bin/md2pdf
COPY --chmod=555 bin/cgbot-sandbox-entrypoint /usr/local/bin/cgbot-sandbox-entrypoint
COPY bridge /opt/cgbot/bridge
COPY codex /opt/cgbot/codex
COPY lsp/cclsp.json /opt/cgbot/lsp/cclsp.json
COPY skills /opt/cgbot/skills
COPY tools/findata/SKILL.md /opt/cgbot/skills/findata/SKILL.md

# 音色挂载点:音色文件不进镜像,宿主机目录由 bot 以只读方式挂到 /opt/soundfonts
# (见 docs/reference/soundfonts.md)。
RUN mkdir -p /opt/soundfonts && chmod 0755 /opt/soundfonts

RUN userdel node && useradd --uid 1000 --create-home --shell /bin/bash agent \
    && mkdir -p /workspace /home/agent \
    && chown -R agent:agent /workspace /home/agent \
    && chmod -R a-w /opt/cgbot /usr/local/bin/cgbot-sandbox-entrypoint

USER agent
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/cgbot-sandbox-entrypoint"]
