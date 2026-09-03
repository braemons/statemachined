# Reproducible build environment for fsmd.
#
# The point of pinning here is that CI, this container, and a developer's laptop
# should disagree about nothing that can change a build result. Two things in
# this repo are genuinely version-sensitive:
#
#   * clang-format -- its output differs between major versions, so an unpinned
#     one turns `make format` into a version lottery against CI's check.
#   * the compilers -- the reproducibility job asserts that a seed gives the
#     same durations under gcc and clang at -O0 and -O3. Naming the versions
#     makes "it passed here" mean something.
#
# Board toolchains are NOT baked in: PlatformIO downloads a few hundred MB per
# platform on first `pio run`. They live in a named volume instead (see
# devcontainer.json) so they survive a rebuild.

FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

ENV DEBIAN_FRONTEND=noninteractive

# Ubuntu 24.04 ships gcc-13, clang-18, cmake 3.28 -- all comfortably past the
# 3.16 floor in CMakeLists.txt, and the same generation CI's ubuntu-latest uses.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      gcc-13 g++-13 \
      clang-18 \
      cmake \
      ninja-build \
      make \
      git \
      ca-certificates \
      python3 \
      python3-dev \
      udev \
      usbutils \
      srecord \
    && rm -rf /var/lib/apt/lists/*

# Unsuffixed names, so CC=gcc / CXX=clang++ mean something definite.
RUN update-alternatives --install /usr/bin/gcc  gcc  /usr/bin/gcc-13 100 \
      --slave /usr/bin/g++ g++ /usr/bin/g++-13 \
 && update-alternatives --install /usr/bin/clang   clang   /usr/bin/clang-18   100 \
      --slave /usr/bin/clang++ clang++ /usr/bin/clang++-18

# uv drives every Python install here -- CI does the same, via the Makefile's
# install-* targets -- so the system pip and its PEP-668 "externally managed"
# refusal never enter into it. Pinned like everything else in this file.
COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /uvx /usr/local/bin/

# Ubuntu 24.04's system Python is externally managed (PEP 668), so tools still
# want their own environment. A venv on PATH is the honest way round that, and
# it also keeps PlatformIO's dependency tree away from anything apt owns.
ENV VIRTUAL_ENV=/opt/venv
RUN uv venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# clang-format is pinned to exactly what .github/workflows/ci.yml installs. If
# you bump one, bump both -- a mismatch shows up as a red format job on a diff
# you never wrote. platformio matches Makefile's PLATFORMIO_PIN.
RUN uv pip install --python "$VIRTUAL_ENV/bin/python" \
      platformio==6.1.16 \
      clang-format==23.1.0

# PlatformIO writes its toolchains, board packages and caches here. The
# devcontainer mounts a volume over it; this just makes the path exist and be
# owned by the non-root user for the case where it does not.
ENV PLATFORMIO_CORE_DIR=/home/vscode/.platformio
RUN mkdir -p "$PLATFORMIO_CORE_DIR" && chown -R vscode:vscode "$PLATFORMIO_CORE_DIR" /opt/venv

# Renode: whole-system emulation of the reference board, so the HAL has tests.
#
# It is the only way to exercise firmware/hal/ without hardware -- the pin map,
# the timer ISR and a whole session over the UART are otherwise covered by
# nothing, because they are exactly the code the host build cannot compile.
#
# What it does NOT give is timing. Renode runs on virtual time with a nominal
# MIPS figure, so it will tell you a scan is correct and never how long one
# takes. The 10 kHz claim needs a board; see dev/HARDWARE.md.
#
# The portable-dotnet tarball carries its own runtime, so this needs no mono and
# no dotnet from apt. Pinned like everything else here.
ARG RENODE_URL=https://github.com/renode/renode/releases/download/v1.16.1/renode-1.16.1.linux-portable-dotnet.tar.gz
RUN curl -fsSL "$RENODE_URL" -o /tmp/renode.tar.gz \
 && mkdir -p /opt/renode \
 && tar xzf /tmp/renode.tar.gz -C /opt/renode --strip-components=1 \
 && rm /tmp/renode.tar.gz \
 && ln -s /opt/renode/renode /usr/local/bin/renode \
 && ln -s /opt/renode/renode-test /usr/local/bin/renode-test

# renode-test drives Robot Framework, which is Python and lives outside the
# tarball. Renode pins these itself; take its list rather than guessing, or
# renode-test fails on an import at the least useful possible moment. (The
# pinned psutil predates cp312 wheels and builds from source, which is what
# python3-dev is doing in the apt list above.)
RUN uv pip install --python "$VIRTUAL_ENV/bin/python" -r /opt/renode/tests/requirements.txt

# Flashing a board from inside a container needs the device passed through and
# these rules present on the *host* as well; see BUILD.md.
RUN curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core/v6.1.16/platformio/assets/system/99-platformio-udev.rules \
      -o /etc/udev/rules.d/99-platformio-udev.rules

USER vscode
