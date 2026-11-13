#!/usr/bin/env bash
# Forge bootstrap installer for macOS and Linux (spec §17, ADR 0015).
# On native Windows use install.ps1 (ADR 0021).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | bash
#
# Pin a version when piping into bash:
#   curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | FORGE_VERSION=v0.1.0 bash
#
# Environment:
#   FORGE_VERSION   git ref to install (tag or branch). Default: the newest
#                   release tag (v<major>.<minor>.<patch>) of the repository.
#                   When the repository has no release tag, `main` is installed.
#                   When the tags cannot be listed (network, access), the script
#                   stops: set FORGE_VERSION to skip the listing.
#   FORGE_REPO_URL  repository URL.
#
# This script never reads, stores or sends credentials. Authentication with
# GitHub is done by the user with `gh auth login`.
set -euo pipefail

# Empty means "choose the newest release tag" (see resolve_version).
FORGE_VERSION="${FORGE_VERSION:-}"
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

# 1. Operating system: macOS or Linux (WSL counts as Linux). Native Windows uses install.ps1.
check_os() {
  local os
  os="$(uname -s)"
  case "${os}" in
    Darwin | Linux) info "Detected ${os}." ;;
    MINGW* | MSYS* | CYGWIN*) die "This is Windows (${os}). Install Forge from PowerShell instead: irm https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.ps1 | iex" ;;
    *) die "Unsupported OS '${os}'. Forge supports macOS, Linux and Windows (install.ps1)." ;;
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

# Print the newest release tag found in the `git ls-remote` output given as
# $1 (for example v0.1.0), or nothing when there is none.
#
# Only stable tags of the form v<number>.<number>.<number> count. The newest is
# found with awk, comparing the three numbers one by one, because `sort -V` is
# missing from some macOS versions.
newest_tag() {
  printf '%s\n' "$1" | awk '
    {
      ref = $2
      sub(/^refs\/tags\//, "", ref)
      if (ref !~ /^v[0-9]+\.[0-9]+\.[0-9]+$/) next
      split(substr(ref, 2), part, ".")
      major = part[1] + 0
      minor = part[2] + 0
      patch = part[3] + 0
      if (!found || major > best_major ||
          (major == best_major && (minor > best_minor ||
          (minor == best_minor && patch > best_patch)))) {
        found = 1
        best_major = major
        best_minor = minor
        best_patch = patch
        best = ref
      }
    }
    END { if (found) print best }
  '
}

# 5. Choose the version: FORGE_VERSION if set, else the newest release tag,
# else `main` with a notice.
resolve_version() {
  if [ -n "${FORGE_VERSION}" ]; then
    info "Using FORGE_VERSION=${FORGE_VERSION}."
    return 0
  fi
  local refs tag
  # A failed listing (network, authentication, wrong URL) is an error. Falling
  # back to `main` is only for a repository that has no release tag yet.
  if ! refs="$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags --refs "${FORGE_REPO_URL}")"; then
    die "Could not list the release tags of ${FORGE_REPO_URL}. Check your network and your access to the repository, or set FORGE_VERSION (for example FORGE_VERSION=v0.1.0, or FORGE_VERSION=main) and run this script again."
  fi
  tag="$(newest_tag "${refs}")"
  if [ -n "${tag}" ]; then
    FORGE_VERSION="${tag}"
    info "Newest release tag: ${FORGE_VERSION}."
    return 0
  fi
  FORGE_VERSION="main"
  warn "${FORGE_REPO_URL} has no release tag (vX.Y.Z) yet."
  warn "Installing the 'main' branch, which can change at any time. Set FORGE_VERSION to pin a tag."
}

# 6. Install Forge as a uv tool. uv provisions Python 3.12 itself.
install_forge() {
  info "Installing Forge (${FORGE_VERSION}) from ${FORGE_REPO_URL}..."
  uv tool install --force --python 3.12 "git+${FORGE_REPO_URL}@${FORGE_VERSION}" ||
    die "Installing Forge with uv failed. Check FORGE_VERSION='${FORGE_VERSION}' and your network."
}

# 7. Next steps.
print_next_steps() {
  local tool_bin forge_bin found login_note
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
  if gh auth status >/dev/null 2>&1; then
    login_note="gh is already authenticated; run it again only to switch account"
  else
    login_note="authenticate with the repo scope"
  fi
  printf '\nNext steps:\n'
  printf '  1. gh auth login            # %s\n' "${login_note}"
  printf '  2. cd <project> && forge setup\n'
  printf '\nNote: the "forge" command name collides with Foundry'"'"'s forge. If both are installed, put the one you want first on PATH.\n'
}

main() {
  check_os
  check_git
  check_gh
  ensure_uv
  resolve_version
  install_forge
  print_next_steps
}

main "$@"
