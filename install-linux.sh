#!/usr/bin/env bash
# Run as your desktop user. Only distribution packages/AppArmor use sudo.
set -euo pipefail

usage() {
  cat <<'HELP'
GPT Manager Linux installer
Usage: bash install-linux.sh [--skip-system-deps] [--source DIRECTORY | --ref REF]
       bash install-linux.sh --uninstall

Installs for the current user, with a menu icon and a dedicated Node runtime.
Run again to update. --source installs a local checkout; --ref selects a Git ref
(default: main). System packages use sudo. --skip-system-deps also skips system
AppArmor configuration; use it when an administrator already prepared the host.
Uninstall preserves conversations, account credentials and application settings.
HELP
}

main() {
  local skip=0 uninstall=0 source_dir='' ref=main temporary='' data_home
  while (($#)); do
    case "$1" in
      --help|-h) usage; return ;;
      --skip-system-deps) skip=1 ;;
      --uninstall) uninstall=1 ;;
      --source|--ref)
        [[ $# -ge 2 && -n "$2" ]] || { echo "Missing value for $1" >&2; return 1; }
        if [[ $1 == --source ]]; then source_dir=$2; else ref=$2; fi
        shift ;;
      *) echo "Unknown option: $1" >&2; usage >&2; return 1 ;;
    esac
    shift
  done
  [[ $(uname -s) == Linux ]] || { echo 'This installer requires Linux.' >&2; return 1; }
  [[ $ref != -* ]] || { echo 'Git refs cannot start with a dash.' >&2; return 1; }
  [[ $EUID -ne 0 ]] || { echo 'Run this as your desktop user, without sudo. It requests sudo only when needed.' >&2; return 1; }
  data_home=${XDG_DATA_HOME:-$HOME/.local/share}
  if ((uninstall)); then
    local helper="$data_home/gpt-manager-app/current/tools/linux_install.py"
    [[ -f "$helper" ]] || { echo 'No managed GPT Manager installation found.'; return; }
    python3 "$helper" uninstall
    return
  fi
  case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo 'Supported CPUs: x86_64 and ARM64.' >&2; return 1 ;; esac
  printf '\n[1/6] Preparing Linux dependencies…\n'
  if ((!skip)); then install_dependencies; fi
  for dependency in python3 git curl tar; do
    command -v "$dependency" >/dev/null || { echo "Missing $dependency. Re-run without --skip-system-deps." >&2; return 1; }
  done
  python3 -c 'import sys, sqlite3, ssl; assert sys.version_info >= (3,10), "Python 3.10+ is required; upgrade your Linux distribution."'
  printf '\n[2/6] Preparing the application source…\n'
  if [[ -z "$source_dir" ]]; then
    temporary=$(mktemp -d -t gpt-manager-source.XXXXXXXX)
    # The trap owns only the unique directory created above.
    trap "rm -rf -- $(printf '%q' "$temporary")" EXIT
    git init -q "$temporary"
    git -C "$temporary" remote add origin https://github.com/anolis/gpt-manager.git
    git -C "$temporary" fetch --depth 1 origin "$ref"
    git -C "$temporary" checkout -q --detach FETCH_HEAD
    source_dir=$temporary
  fi
  [[ -f "$source_dir/tools/linux_install.py" ]] || { echo 'Source does not contain the Linux installer.' >&2; return 1; }
  local args=(install --source "$source_dir")
  if ((!skip)); then args+=(--allow-system-changes); fi
  python3 "$source_dir/tools/linux_install.py" "${args[@]}"
  if [[ -n "$temporary" ]]; then rm -rf -- "$temporary"; temporary=''; fi
  trap - EXIT
}

install_dependencies() {
  command -v sudo >/dev/null || { echo 'sudo is required to install system packages. Ask an administrator, then use --skip-system-deps.' >&2; return 1; }
  local packages=()
  if command -v apt-get >/dev/null; then
    sudo apt-get update
    packages=(ca-certificates curl git python3 tar xz-utils libstdc++6 libnss3 libgbm1 libxss1 libx11-xcb1 libxkbcommon0 libsecret-1-0 xdg-utils desktop-file-utils)
    local base
    for base in libgtk-3-0 libasound2 libatk-bridge2.0-0; do
      if apt-cache show "${base}t64" 2>/dev/null | grep -q '^Package:'; then packages+=("${base}t64"); else packages+=("$base"); fi
    done
    sudo apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null; then
    sudo dnf install -y ca-certificates curl git python3 tar xz libstdc++ gtk3 nss alsa-lib libXScrnSaver libXtst mesa-libgbm libsecret xdg-utils desktop-file-utils
  elif command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm ca-certificates curl git python tar xz gcc-libs gtk3 nss alsa-lib libxss libxtst mesa libsecret xdg-utils desktop-file-utils
  elif command -v zypper >/dev/null; then
    sudo zypper --non-interactive install ca-certificates curl git python3 tar xz libstdc++6 libgtk-3-0 mozilla-nss libasound2 libXss1 libXtst6 libgbm1 libsecret-1-0 xdg-utils desktop-file-utils
  else
    echo 'Automatic packages support Debian/Ubuntu, Fedora, Arch and openSUSE families.' >&2
    echo 'Install Python 3.10+, Git, curl, tar and Electron runtime libraries, then use --skip-system-deps.' >&2
    return 1
  fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
