#!/usr/bin/env bash
# Forge bootstrap installer (spec §17). DRAFT: structure only until Phase 9.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | bash
#
# Environment:
#   FORGE_VERSION   git ref to install (tag or branch).
#   FORGE_REPO_URL  repository URL.
#
# This script never reads, stores or sends credentials. Authentication with
# GitHub is done by the user with `gh auth login`.
set -euo pipefail

# Defaults to `main` while Forge is unreleased. At release this becomes the
# latest tag (for example v0.1.0).
FORGE_VERSION="${FORGE_VERSION:-main}"
FORGE_REPO_URL="${FORGE_REPO_URL:-https://github.com/dragonfly25/forge_it}"

# PATH as the user's shell sees it, before this script extends it.
ORIGINAL_PATH="${PATH}"

info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

# 1. Operating system: macOS or Linux only (Windows via WSL).
check_os() {
  local os
  os="$(uname -s)"
  case "${os}" in
    Darwin | Linux) info "Detected ${os}." ;;
    *) die "Unsupported OS '${os}'. Forge supports macOS and Linux. On Windows, install WSL and run this script inside it: https://learn.microsoft.com/windows/wsl/install" ;;
  esac
}

# 2. git >= 2.38.
check_git() {
  have git || die "git is not installed. Install git >= 2.38 and run this script again."
  local raw version major minor
  raw="$(git --version)" # e.g. "git version 2.39.3 (Apple Git-146)"
  version="$(printf '%s' "${raw}" | sed -E 's/^git version ([0-9]+)\.([0-9]+).*/\1.\2/')"
  case "${version}" in
    *[!0-9.]* | "" | .* | *.) die "Could not parse the git version from '${raw}'." ;;
    *.*) ;;
    *) die "Could not parse the git version from '${raw}'." ;;
  esac
  major="${version%%.*}"
  minor="${version#*.}"
  if [ "${major}" -lt 2 ] || { [ "${major}" -eq 2 ] && [ "${minor}" -lt 38 ]; }; then
    die "git ${version} is too old. Forge needs git >= 2.38. Please upgrade git."
  fi
  info "git ${version} is OK."
}

# Ask a y/N question. Reads from /dev/tty so it works when piped into bash.
# Returns 1 (no) when there is no terminal.
confirm() {
  local prompt="$1" answer=""
  # Opening /dev/tty fails without a controlling terminal (CI, Docker, ssh without -t).
  if ! { : >/dev/tty; } 2>/dev/null; then
    return 1
  fi
  printf '%s [y/N] ' "${prompt}" >/dev/tty
  read -r answer </dev/tty || return 1
  case "${answer}" in
    y | Y | yes | YES) return 0 ;;
    *) return 1 ;;
  esac
}

print_gh_instructions() {
  warn "GitHub CLI (gh) is not installed. Install it from https://cli.github.com and run this script again."
  warn "  macOS (Homebrew): brew install gh"
  warn "  Debian/Ubuntu, Fedora, other Linux: see https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
}

# 3. GitHub CLI.
check_gh() {
  if have gh; then
    info "gh is installed."
    return 0
  fi
  if [ "$(uname -s)" = "Darwin" ] && have brew; then
    if confirm "gh is missing. Install it now with 'brew install gh'?"; then
      brew install gh || die "'brew install gh' failed."
      have gh || die "gh is still not on PATH after 'brew install gh'."
      return 0
    fi
  fi
  print_gh_instructions
  die "gh is required. Install it, then run this script again."
}

# 4. uv, using the official installer.
ensure_uv() {
  if have uv; then
    info "uv is installed."
  else
    have curl || die "curl is required to install uv."
    info "Installing uv with the official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh || die "The uv installer failed. See https://docs.astral.sh/uv/getting-started/installation/"
  fi
  # Make sure uv is on PATH for this script. The uv installer honours
  # UV_INSTALL_DIR, then XDG_BIN_HOME, then ~/.local/bin.
  local uv_bin="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-${HOME}/.local/bin}}"
  case ":${PATH}:" in
    *":${uv_bin}:"*) ;;
    *) export PATH="${uv_bin}:${PATH}" ;;
  esac
  have uv || die "uv is not on PATH after installation. Add ${uv_bin} to PATH and run this script again."
}

# 5. Install Forge as a uv tool. uv provisions Python 3.12 itself.
install_forge() {
  info "Installing Forge (${FORGE_VERSION}) from ${FORGE_REPO_URL}..."
  uv tool install --force --python 3.12 "git+${FORGE_REPO_URL}@${FORGE_VERSION}" ||
    die "Installing Forge with uv failed. Check FORGE_VERSION='${FORGE_VERSION}' and your network."
}

# 6. Next steps.
print_next_steps() {
  local tool_bin forge_bin found
  tool_bin="$(uv tool dir --bin)"
  forge_bin="${tool_bin}/forge"
  [ -x "${forge_bin}" ] || die "Forge was installed but ${forge_bin} is missing."
  info "Installed: $("${forge_bin}" --version)"
  # Check against the user's own PATH, not the one this script extended.
  found="$(PATH="${ORIGINAL_PATH}" command -v forge 2>/dev/null || true)"
  if [ -z "${found}" ]; then
    warn "'forge' is not on PATH in your shell yet. Run 'uv tool update-shell', then restart your shell."
  elif [ "${found}" != "${forge_bin}" ]; then
    warn "'forge' on your PATH is ${found}, not Forge (${forge_bin}). It may be Foundry's forge: put ${tool_bin} first on PATH."
  fi
  printf '\nNext steps:\n'
  if ! gh auth status >/dev/null 2>&1; then
    printf '  1. gh auth login            # authenticate with the repo scope\n'
    printf '  2. cd <project> && forge setup\n'
  else
    printf '  1. cd <project> && forge setup\n'
  fi
  printf '\nNote: the "forge" command name collides with Foundry'"'"'s forge. If both are installed, put the one you want first on PATH.\n'
}

main() {
  check_os
  check_git
  check_gh
  ensure_uv
  install_forge
  print_next_steps
}

main "$@"
