# muse-setup: 一次性、按需运行的 Muse Sounds 初始化容器(不属于 bot 镜像)。
# 构建上下文:tools/muse-setup/。说明见 README.md 与 docs/reference/muse-setup.md。
#
# 镜像内只有官方 Muse Sounds Manager(构建时从官方地址下载并校验 sha256)与我们的驱动脚本。
# 不含任何库、样本、登录状态或 machine-id;这些都在运行时通过卷提供。

# debian:bookworm 固定到摘要(2026-11-04 构建的镜像)。
ARG DEBIAN_IMAGE=debian:bookworm@sha256:268fb24c39bca92d326dd83ea3abebe4a3f7751c804ffef23bb6d857dd343ff8
FROM ${DEBIAN_IMAGE}

# 官方下载页:https://support.musehub.com/en/articles/15070606-downloads
# 版本 2.2.1.953。官方地址不带版本号,上游更新后 sha256 会变,构建会失败而不是悄悄换版本;
# 需要新版时用 --build-arg MUSE_DEB_SHA256=<新值> 并先核对。
ARG MUSE_DEB_URL=https://muse-cdn.com/Muse_Sounds_Manager_x64.deb
ARG MUSE_DEB_SHA256=53209132a814c49c6d7c61e1ab28e99adf09eb40a7fdde8f2eeb3f03cf072813
ARG DEBIAN_MIRROR=mirrors.aliyun.com

ENV DEBIAN_FRONTEND=noninteractive

# Debian 镜像与 DIRECT apt:与仓库其他 Dockerfile 相同的网络策略。
RUN set -eux; \
    for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
      if [ -f "$f" ]; then \
        sed -i -e "s#deb.debian.org#${DEBIAN_MIRROR}#g" -e "s#security.debian.org#${DEBIAN_MIRROR}#g" "$f"; \
      fi; \
    done; \
    printf '%s\n' 'Acquire::http::Proxy "DIRECT";' 'Acquire::https::Proxy "DIRECT";' > /etc/apt/apt.conf.d/99direct; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates curl python3 xvfb xauth xdotool imagemagick \
      tesseract-ocr tesseract-ocr-eng iproute2 procps dbus dbus-x11 fonts-dejavu-core; \
    curl --fail --location --retry 3 --output /tmp/manager.deb "${MUSE_DEB_URL}"; \
    echo "${MUSE_DEB_SHA256}  /tmp/manager.deb" | sha256sum -c -; \
    apt-get install -y --no-install-recommends /tmp/manager.deb; \
    rm -f /tmp/manager.deb; \
    rm -rf /var/lib/apt/lists/*

# 非 root 用户 uid 1000,HOME=/state。
# /var/lib/dbus/machine-id 指向 /etc/machine-id,运行时只需挂载一个文件。
RUN set -eux; \
    useradd -u 1000 -m -d /state -s /bin/bash muse; \
    mkdir -p /samples /out /var/lib/dbus; \
    chown muse:muse /state /samples /out; \
    rm -f /etc/machine-id /var/lib/dbus/machine-id; \
    ln -s /etc/machine-id /var/lib/dbus/machine-id

# 浏览器 shim:Manager 想打开浏览器时,只记录链接并打印,不会真的打开任何东西。
COPY muse-open-url /usr/local/bin/muse-open-url
RUN set -eux; \
    chmod 0755 /usr/local/bin/muse-open-url; \
    for n in xdg-open x-www-browser sensible-browser www-browser gnome-open gio firefox chromium google-chrome; do \
      ln -sf muse-open-url /usr/local/bin/$n; \
    done

COPY entrypoint.sh /usr/local/bin/muse-setup-entrypoint
COPY muse_setup.py /usr/local/bin/muse_setup.py
RUN chmod 0755 /usr/local/bin/muse-setup-entrypoint /usr/local/bin/muse_setup.py

ENV HOME=/state \
    BROWSER=/usr/local/bin/muse-open-url \
    DISPLAY=:99 \
    LANG=C.UTF-8

USER 1000:1000
WORKDIR /state
VOLUME ["/state", "/samples", "/out"]

ENTRYPOINT ["/usr/local/bin/muse-setup-entrypoint"]
