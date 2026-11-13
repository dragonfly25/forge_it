# Forge bootstrap installer for native Windows (spec §17, ADR 0021).
#
# Usage (Windows PowerShell 5.1 or PowerShell 7):
#   irm https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.ps1 | iex
#
# Pin a version:
#   $env:FORGE_VERSION = "v0.2.0"; irm https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.ps1 | iex
#
# From cmd:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.ps1 | iex"
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
#
# The steps run inside a script block, so `irm ... | iex` never closes the
# user's PowerShell window: a failure throws, and the message is printed below.

& {
    Set-StrictMode -Version 3.0
    $ErrorActionPreference = 'Stop'

    # Shared, mutable state (functions see it through PowerShell's dynamic scoping).
    $State = @{ Version = '' }
    if ($env:FORGE_VERSION) { $State.Version = $env:FORGE_VERSION }
    $ForgeRepoUrl = if ($env:FORGE_REPO_URL) { $env:FORGE_REPO_URL } else { 'https://github.com/dragonfly25/forge_it' }

    # PATH as the user's shell sees it, before this script extends it.
    $OriginalPath = $env:Path

    function Info([string]$Message) { Write-Host "==> $Message" }
    function Warn([string]$Message) { [Console]::Error.WriteLine("warning: $Message") }
    function Die([string]$Message) { throw "error: $Message" }
    function Have([string]$Name) { [bool](Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue) }

    # 1. Operating system: this script is for Windows. macOS, Linux and WSL use install.sh.
    function Test-Os {
        $onWindows = $PSVersionTable.PSEdition -eq 'Desktop' -or $IsWindows
        if (-not $onWindows) {
            Die 'This installer is for Windows. On macOS, Linux and WSL run: curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | bash'
        }
        Info "Detected Windows (PowerShell $($PSVersionTable.PSVersion))."
    }

    # 2. git >= 2.38.
    function Test-Git {
        if (-not (Have 'git')) {
            Die 'git is not installed. Install Git for Windows >= 2.38 (https://git-scm.com/download/win, or: winget install --id Git.Git --source winget) and run this script again.'
        }
        $raw = (& git --version) | Out-String # e.g. "git version 2.47.1.windows.1"
        if ($raw -notmatch 'git version (\d+)\.(\d+)') {
            Die "Could not parse the git version from '$($raw.Trim())'."
        }
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -lt 2 -or ($major -eq 2 -and $minor -lt 38)) {
            Die "git $major.$minor is too old. Forge needs git >= 2.38. Please upgrade git."
        }
        Info "git $major.$minor is OK."
    }

    # Ask a y/N question. Returns $false when there is no interactive console.
    function Confirm-Step([string]$Prompt) {
        if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) { return $false }
        $answer = Read-Host "$Prompt [y/N]"
        return $answer -match '^(y|yes)$'
    }

    # Make programs installed in this session visible without a new shell.
    function Update-SessionPath {
        $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $user = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = (@($env:Path, $machine, $user) | Where-Object { $_ }) -join ';'
    }

    # 3. GitHub CLI.
    function Test-Gh {
        if (Have 'gh') {
            Info 'gh is installed.'
            return
        }
        if ((Have 'winget') -and (Confirm-Step "gh is missing. Install it now with 'winget install --id GitHub.cli'?")) {
            & winget install --id GitHub.cli --source winget --exact --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { Die "'winget install --id GitHub.cli' failed." }
            Update-SessionPath
            if (-not (Have 'gh')) { Die "gh is still not on PATH after installing it. Open a new terminal window and run this script again." }
            return
        }
        Warn 'GitHub CLI (gh) is not installed. Install it from https://cli.github.com and run this script again.'
        Warn '  winget install --id GitHub.cli --source winget'
        Warn '  or the MSI from https://github.com/cli/cli/releases/latest'
        Die 'gh is required. Install it, then run this script again.'
    }

    # 4. uv, using the official installer.
    function Install-Uv {
        if (Have 'uv') {
            Info 'uv is installed.'
        }
        else {
            Info 'Installing uv with the official installer...'
            try {
                Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
            }
            catch {
                Die "The uv installer failed: $($_.Exception.Message). See https://docs.astral.sh/uv/getting-started/installation/"
            }
        }
        # The uv installer honours UV_INSTALL_DIR, then XDG_BIN_HOME, then %USERPROFILE%\.local\bin.
        $uvBin = if ($env:UV_INSTALL_DIR) { $env:UV_INSTALL_DIR }
        elseif ($env:XDG_BIN_HOME) { $env:XDG_BIN_HOME }
        else { Join-Path $env:USERPROFILE '.local\bin' }
        if (($env:Path -split ';') -notcontains $uvBin) { $env:Path = "$uvBin;$env:Path" }
        if (-not (Have 'uv')) { Die "uv is not on PATH after installation. Add $uvBin to PATH and run this script again." }
    }

    # Return the newest tag of the form v<number>.<number>.<number> in the
    # `git ls-remote --tags --refs` output, or $null when there is none.
    function Get-NewestTag([string[]]$Lines) {
        $best = $null
        $bestKey = $null
        foreach ($line in $Lines) {
            if ($line -notmatch 'refs/tags/(v(\d+)\.(\d+)\.(\d+))$') { continue }
            $key = [version]::new([int]$Matches[2], [int]$Matches[3], [int]$Matches[4])
            if ($null -eq $bestKey -or $key -gt $bestKey) {
                $bestKey = $key
                $best = $Matches[1]
            }
        }
        return $best
    }

    # 5. Choose the version: FORGE_VERSION if set, else the newest release tag,
    # else `main` with a notice.
    function Resolve-ForgeVersion {
        if ($State.Version) {
            Info "Using FORGE_VERSION=$($State.Version)."
            return
        }
        $previousPrompt = $env:GIT_TERMINAL_PROMPT
        $env:GIT_TERMINAL_PROMPT = '0'
        try {
            $refs = & git ls-remote --tags --refs $ForgeRepoUrl
            $code = $LASTEXITCODE
        }
        finally {
            $env:GIT_TERMINAL_PROMPT = $previousPrompt
        }
        # A failed listing (network, authentication, wrong URL) is an error. Falling
        # back to `main` is only for a repository that has no release tag yet.
        if ($code -ne 0) {
            Die "Could not list the release tags of $ForgeRepoUrl. Check your network and your access to the repository, or set FORGE_VERSION (for example: `$env:FORGE_VERSION = 'v0.2.0', or 'main') and run this script again."
        }
        $tag = Get-NewestTag @($refs)
        if ($tag) {
            $State.Version = $tag
            Info "Newest release tag: $tag."
            return
        }
        $State.Version = 'main'
        Warn "$ForgeRepoUrl has no release tag (vX.Y.Z) yet."
        Warn "Installing the 'main' branch, which can change at any time. Set FORGE_VERSION to pin a tag."
    }

    # 6. Install Forge as a uv tool. uv provisions Python 3.12 itself.
    function Install-Forge {
        Info "Installing Forge ($($State.Version)) from $ForgeRepoUrl..."
        & uv tool install --force --python 3.12 "git+$ForgeRepoUrl@$($State.Version)"
        if ($LASTEXITCODE -ne 0) {
            Die "Installing Forge with uv failed. Check FORGE_VERSION='$($State.Version)' and your network."
        }
    }

    # 7. Next steps.
    function Show-NextSteps {
        $toolBin = (& uv tool dir --bin | Out-String).Trim()
        $forgeExe = Join-Path $toolBin 'forge.exe'
        if (-not (Test-Path $forgeExe)) { Die "Forge was installed but $forgeExe is missing." }
        Info "Installed: $(& $forgeExe --version)"
        # Check against the user's own PATH, not the one this script extended.
        $found = $null
        foreach ($dir in ($OriginalPath -split ';')) {
            if (-not $dir) { continue }
            $candidate = Join-Path $dir 'forge.exe'
            if (Test-Path $candidate) { $found = $candidate; break }
        }
        if (-not $found) {
            Warn "'forge' is not on PATH in your shell yet. Run 'uv tool update-shell', then open a new terminal."
        }
        elseif ((Resolve-Path $found).Path -ne (Resolve-Path $forgeExe).Path) {
            Warn "'forge' on your PATH is $found, not Forge ($forgeExe). It may be Foundry's forge: put $toolBin first on PATH."
        }
        # Windows PowerShell 5.1 turns redirected native stderr into errors; 'Stop' would throw.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & gh auth status *> $null
            $authenticated = $LASTEXITCODE -eq 0
        }
        finally {
            $ErrorActionPreference = $previous
        }
        $loginNote = if ($authenticated) { 'gh is already authenticated; run it again only to switch account' } else { 'authenticate with the repo scope' }
        Write-Host ''
        Write-Host 'Next steps:'
        Write-Host "  1. gh auth login            # $loginNote"
        Write-Host '  2. cd <project>; forge setup'
        Write-Host ''
        Write-Host "Note: the `"forge`" command name collides with Foundry's forge. If both are installed, put the one you want first on PATH."
    }

    try {
        Test-Os
        Test-Git
        Test-Gh
        Install-Uv
        Resolve-ForgeVersion
        Install-Forge
        Show-NextSteps
        # Success: do not leak the exit code of the last native probe (`gh auth status`).
        $global:LASTEXITCODE = 0
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        $global:LASTEXITCODE = 1
    }
}
