# ima.copilot (Tencent, Windows-only) under a memory-patched Wine, headless, CDP exposed.
# The image carries Wine + tooling only; ima itself is downloaded from Tencent on first start
# (see docker/entrypoint.sh), so the image stays redistributable.

ARG DEBIAN=trixie

# ---------- 1. build Wine with the Chromium memory patches ----------
FROM debian:${DEBIAN} AS wine
ARG WINE_VERSION=11.21
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get build-dep -y wine \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git ca-certificates gcc-mingw-w64-x86-64 \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch wine-${WINE_VERSION} https://gitlab.winehq.org/wine/wine.git /src
COPY wine/ /patches/
RUN cd /src && for p in /patches/*.patch; do git apply --whitespace=nowarn "$p"; done
# 64-bit only: ima is x86_64 and wow64 would double the build for nothing
RUN mkdir /build && cd /build \
 && /src/configure --enable-archs=x86_64 --disable-tests --prefix=/opt/wine \
 && make -j"$(nproc)" && make install \
 && cd /opt/wine \
 && find . -path '*x86_64-windows*' -type f ! -name '*.a' -exec x86_64-w64-mingw32-strip --strip-unneeded {} + 2>/dev/null; \
    find . -type f \( -name '*.so' -o -path './bin/*' \) -exec strip --strip-unneeded {} + 2>/dev/null; \
    rm -rf include share/man lib/wine/*/*.a

# ksm_preload.so: marks every process KSM-mergeable (PR_SET_MEMORY_MERGE). Kernels < 6.7 drop the
# flag across exec, so setting it in each process is the portable way.
COPY docker/ksm_preload.c /tmp/
RUN gcc -shared -fPIC -O2 -o /opt/wine/lib/ksm_preload.so /tmp/ksm_preload.c

# ---------- 2. runtime ----------
FROM debian:${DEBIAN}
LABEL org.opencontainers.image.source=https://github.com/rqi14/ima-wine \
      org.opencontainers.image.description="Tencent ima.copilot under a memory-patched Wine, with an MCP server for agents" \
      org.opencontainers.image.licenses=LGPL-2.1-or-later
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libx11-6 libxext6 libxrender1 libxrandr2 libxi6 libxcursor1 libxcomposite1 libxfixes3 \
      libxinerama1 libxxf86vm1 libxkbcommon0 libfreetype6 libfontconfig1 libgnutls30t64 \
      libdbus-1-3 libgl1 libegl1 libvulkan1 libudev1 \
      xvfb xauth xdotool imagemagick fonts-wqy-microhei poppler-utils \
      tesseract-ocr tesseract-ocr-chi-sim \
      ca-certificates curl 7zip socat python3 python3-venv tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 ima && mkdir -p /data && chown ima:ima /data
# mcp<2: 2.x renamed FastMCP and reshuffled the API; the wire protocol is the same
RUN python3 -m venv /opt/imactl/venv \
 && /opt/imactl/venv/bin/pip install --no-cache-dir 'mcp<2' websocket-client
COPY --from=wine /opt/wine /opt/wine
COPY docker/entrypoint.sh /usr/local/bin/ima-entrypoint
COPY imactl/*.py /opt/imactl/
RUN printf '#!/bin/sh\nexec /opt/imactl/venv/bin/python /opt/imactl/imactl.py "$@"\n' > /usr/local/bin/imactl \
 && chmod 755 /usr/local/bin/imactl /usr/local/bin/ima-entrypoint
# UTF-8 is not cosmetic: Wine maps Windows file names through the Unix locale, and under the
# default C locale every non-ASCII name (i.e. every Chinese download) fails to be created.
# No Mono/Gecko: whenever wineboot updates the prefix (any rebuilt Wine) it offers to install
# them in a dialog on the invisible display and hangs there; ima needs neither.
ENV PATH=/opt/wine/bin:$PATH \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    WINEPREFIX=/data/prefix \
    WINEDEBUG=-all \
    WINEDLLOVERRIDES="mscoree,mshtml=" \
    DISPLAY=:99
USER ima
VOLUME /data
# 8765: MCP (streamable HTTP, /mcp) for agents; 9223: raw CDP, for debugging only
EXPOSE 8765 9223
# give Browser.close time to flush the rotated login token (see entrypoint)
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/ima-entrypoint"]
