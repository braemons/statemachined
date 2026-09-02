# Building fsmd

There are two builds in this repo and they do not know about each other:

| | |
|---|---|
| **the core, on the host** | `cmake` + `ctest`. Plain C++17, no `Arduino.h`. This is the fast loop and it needs no board. |
| **the firmware, for a board** | `pio`. Compiles `firmware/src/` alongside the same `firmware/core/`. |

Everything below is about getting the tools for those two. If you would rather
not install anything, skip to [the devcontainer](#the-devcontainer) — it is the
same environment CI uses and it is one command.

## What you actually need

| tool | floor | why |
|---|---|---|
| CMake | 3.16 | the `cmake_minimum_required` in `CMakeLists.txt` |
| a C++17 compiler | gcc 9 / clang 10 | the core is C++17 and builds `-Werror` |
| GNU Make or Ninja | any | the generator CMake drives |
| Python | 3.9 | only to run PlatformIO |
| PlatformIO | 6.x | board builds and flashing |
| clang-format | **exactly 23.1.0** | see the warning below |

> [!IMPORTANT]
> **clang-format is pinned on purpose.** Its output changes between major
> versions, so a distro-provided one can reformat files your CI check then
> rejects — a red build on a diff you never wrote. Install it from pip
> (`pip install clang-format==23.1.0`), not from your package manager, and keep
> it in step with the pin in `.github/workflows/ci.yml`. As of this writing a
> Fedora 44 host's clang-format 22.1.8 happens to agree with 23.1.0 on this
> codebase, but that is luck, not a guarantee.

## The devcontainer

The fastest route to a build environment that matches CI. Requires Docker (or
Podman) and either VS Code with the Dev Containers extension, or the
[`devcontainer` CLI](https://github.com/devcontainers/cli).

**VS Code:** open the repo, then *Reopen in Container* when prompted (or
`F1` → *Dev Containers: Reopen in Container*).

**CLI:**

```sh
npm install -g @devcontainers/cli
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . make test
```

**Plain Docker,** if you want no devcontainer tooling at all:

```sh
docker build -f .devcontainer/Dockerfile -t fsmd-dev .
docker run --rm -it -v "$PWD":/w -w /w fsmd-dev make test
```

It pins Ubuntu 24.04, gcc 13, clang 18, CMake 3.28, PlatformIO 6.1.16 and
clang-format 23.1.0. Board toolchains are *not* baked into the image — the first
`make firmware` downloads a few hundred MB into a named volume, where it
survives container rebuilds.

Flashing from inside the container needs the board passed through: uncomment the
`runArgs` line in `.devcontainer/devcontainer.json` and adjust the device path.
It is commented out by default because a device path that does not exist is a
hard container-startup failure rather than a warning. Many people find it
simpler to build in the container and run `make upload` on the host.

## Ubuntu 24.04

```sh
sudo apt update
sudo apt install -y build-essential clang cmake ninja-build git \
                    python3 python3-venv python3-pip
```

That gives gcc 13, clang 18 and CMake 3.28 — all well past the floors above.

PlatformIO and clang-format go in a virtualenv. Ubuntu 24.04's system Python is
*externally managed* (PEP 668), so `pip install` into it will refuse:

```sh
python3 -m venv ~/.venvs/fsmd
source ~/.venvs/fsmd/bin/activate
pip install platformio==6.1.16 clang-format==23.1.0
```

Add `source ~/.venvs/fsmd/bin/activate` to your shell rc, or use
`pipx install platformio` if you prefer the tools on `PATH` permanently.

**For flashing,** install PlatformIO's udev rules and put yourself in the
serial group:

```sh
curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core/master/platformio/assets/system/99-platformio-udev.rules \
  | sudo tee /etc/udev/rules.d/99-platformio-udev.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG dialout "$USER"    # log out and back in
```

## Fedora 44+

```sh
sudo dnf install -y gcc-c++ clang cmake ninja-build make git \
                    python3 python3-pip
```

Fedora tracks upstream closely, so you get a much newer gcc and clang than
Ubuntu. That is a feature here: the core builds `-Wall -Wextra -Wpedantic
-Werror`, and a newer compiler will find things CI's gcc 13 does not. If a
warning fires only on Fedora, it is still a real warning — fix it rather than
waiting for CI to agree.

> Do **not** `dnf install clang-tools-extra` for clang-format. It will give you a
> version matching your system clang, which is not the pinned 23.1.0. Use pip.

Fedora's Python is externally managed too, so the same venv applies:

```sh
python3 -m venv ~/.venvs/fsmd
source ~/.venvs/fsmd/bin/activate
pip install platformio==6.1.16 clang-format==23.1.0
```

**For flashing:**

```sh
curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core/master/platformio/assets/system/99-platformio-udev.rules \
  | sudo tee /etc/udev/rules.d/99-platformio-udev.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG dialout "$USER"    # log out and back in
```

## WSL

Use **WSL2** with the Ubuntu 24.04 image and follow the Ubuntu section verbatim —
the host build and the firmware *compile* work exactly as they do on native
Linux. The devcontainer works too; Docker Desktop with the WSL2 backend, or
Docker installed inside the distro, both do the job.

```powershell
wsl --install -d Ubuntu-24.04
```

Two things differ, and one of them will bite you:

**Keep the repo on the Linux filesystem.** Clone into `~/code/fsmd`, not
`/mnt/c/Users/...`. Builds across the Windows filesystem boundary are slower by
a large factor, and file-watching tools miss changes there.

**USB is not passed through by default,** so `make upload` cannot see a board
until you attach it. WSL2 needs
[usbipd-win](https://github.com/dorssel/usbipd-win). In an *administrator*
PowerShell:

```powershell
winget install usbipd
usbipd list                      # find the board's BUSID
usbipd bind --busid <BUSID>      # once per device, persists
usbipd attach --wsl --busid <BUSID>
```

Then in WSL, `ls /dev/ttyACM*` should show it, and the udev-rules and `dialout`
steps from the Ubuntu section apply. You must re-run `usbipd attach` after
unplugging the board or restarting WSL.

If that is more than you want to deal with: build in WSL, and flash from Windows
with `pio run -t upload` in a native Windows PlatformIO install. The firmware
image is identical.

## Verifying the setup

```sh
make test        # the core unit tests -- should be 3/3
make sanitize    # the same under ASan and UBSan
make firmware    # compiles for the Uno R4 Minima
make format      # clang-format in place
```

`make test` passing is the real check; it exercises the same `firmware/core/`
that goes on the board.

To reproduce what CI does, build under both compilers and at both optimisation
levels — the reproducibility claim is that a seed gives the same durations
everywhere, and differing `-O` levels is the cheapest way to catch a dependence
on undefined behaviour:

```bash
# bash, not fish -- the compiler is passed to CMake explicitly rather than
# through CC/CXX, so there is no chance of a pass silently using the default.
set -e
for cxx in g++ clang++; do
  for opt in -O0 -O3; do
    dir="build-$cxx$opt"
    cmake -S . -B "$dir" -DCMAKE_BUILD_TYPE=Debug \
          -DCMAKE_CXX_COMPILER="$cxx" -DCMAKE_CXX_FLAGS="$opt"
    cmake --build "$dir" -j
    ctest --test-dir "$dir" --output-on-failure
  done
done
```

And check formatting the way CI checks it — `--dry-run --Werror` reports rather
than rewrites:

```sh
find firmware tests -name '*.cpp' -o -name '*.h' \
  | grep -v third_party | xargs clang-format --dry-run --Werror
```

## Editor setup

Export a compilation database and point clangd at it, so the editor sees exactly
the flags the build uses:

```sh
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
ln -sf build/compile_commands.json .
```

`compile_commands.json` is already in `.gitignore`. The devcontainer configures
this for VS Code automatically.

## Troubleshooting

**`make format` produces a diff CI rejects.** Your clang-format is not 23.1.0.
Check with `clang-format --version`; a pip install inside an active venv shadows
the system one.

**`pio run` fails to download a platform.** PlatformIO fetches toolchains on
first use and needs network. Behind a proxy, set `HTTP_PROXY`/`HTTPS_PROXY`.
To clear a half-downloaded platform: `rm -rf ~/.platformio/platforms/renesas-ra`.

**`Permission denied` on `/dev/ttyACM0`.** The udev rules or the `dialout` group
membership have not taken effect. Group changes need a full logout, not just a
new shell.

**CMake cannot find a compiler after switching containers.** A `build/` tree
caches absolute compiler paths. Delete it and reconfigure — `make clean` removes
all of them.

**An untracked `Build/` directory appears.** Some editor integrations configure
CMake into a capitalised directory. `.gitignore` matches `build*/`, which is
case-sensitive, so that one shows up as untracked.
