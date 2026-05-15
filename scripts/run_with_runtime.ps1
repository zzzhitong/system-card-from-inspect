[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$RuntimeConfig,
    [switch]$PrintRuntime,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PipelineArgs
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    param([string]$SkillRoot)
    return [System.IO.Path]::GetFullPath((Join-Path $SkillRoot "..\..\.."))
}

function Resolve-FromRepo {
    param(
        [string]$Value,
        [string]$RepoRoot
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Value))
}

function Resolve-PythonCandidate {
    param(
        [string]$Candidate,
        [string]$RepoRoot
    )
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return $null
    }
    if ($Candidate.Contains("/") -or $Candidate.Contains("\")) {
        $resolved = Resolve-FromRepo -Value $Candidate -RepoRoot $RepoRoot
        if (Test-Path -LiteralPath $resolved) {
            return $resolved
        }
        return $null
    }
    $command = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    return $null
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$skillRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDir ".."))
$repoRoot = Get-RepoRoot -SkillRoot $skillRoot
$pythonLauncherPath = Join-Path $scriptDir "run_with_runtime.py"

$configCandidates = @()
if ($RuntimeConfig) {
    $configCandidates += (Resolve-FromRepo -Value $RuntimeConfig -RepoRoot $repoRoot)
} else {
    $configCandidates += [System.IO.Path]::GetFullPath((Join-Path $skillRoot "references\runtime_config.local.json"))
    $configCandidates += [System.IO.Path]::GetFullPath((Join-Path $skillRoot "references\runtime_config.json"))
}

$configPath = $null
foreach ($candidate in $configCandidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
        $configPath = $candidate
        break
    }
}

$pythonPath = $null
if ($configPath) {
    $configPayload = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $runtime = $configPayload.runtime
    if ($runtime -and $runtime.python_path) {
        $pythonPath = Resolve-PythonCandidate -Candidate ([string]$runtime.python_path) -RepoRoot $repoRoot
        if (-not $pythonPath) {
            throw "Configured python_path not found: $($runtime.python_path)"
        }
    }
    if (-not $pythonPath -and $runtime -and $runtime.python_candidates) {
        foreach ($candidate in @($runtime.python_candidates)) {
            $pythonPath = Resolve-PythonCandidate -Candidate ([string]$candidate) -RepoRoot $repoRoot
            if ($pythonPath) {
                break
            }
        }
    }
}

if (-not $pythonPath) {
    foreach ($candidate in @(
        "inspect_evals/.venv/Scripts/python.exe",
        "inspect_evals/.venv/bin/python",
        ".venv/Scripts/python.exe",
        ".venv/bin/python",
        "python",
        "python3"
    )) {
        $pythonPath = Resolve-PythonCandidate -Candidate $candidate -RepoRoot $repoRoot
        if ($pythonPath) {
            break
        }
    }
}

if (-not $pythonPath) {
    throw "No Python runtime found. Set runtime.python_path or runtime.python_candidates in runtime_config.json."
}

$launcherArgs = @()
if ($RuntimeConfig) {
    $launcherArgs += "--runtime-config"
    $launcherArgs += $RuntimeConfig
}
if ($PrintRuntime) {
    $launcherArgs += "--print-runtime"
}
if ($PipelineArgs) {
    $launcherArgs += $PipelineArgs
}

& $pythonPath $pythonLauncherPath @launcherArgs
exit $LASTEXITCODE
