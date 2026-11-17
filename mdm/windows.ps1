# Install git ai-usage for the signed-in user of a Windows computer. Run it as the user, not
# as SYSTEM: in Intune, set "Run this script using the logged on credentials" to Yes; with
# Group Policy, use a user logon script. It is safe to run many times.
#
# $Remote: the hooks run only in repositories whose remote URL contains this text. Put the
# organization of your git provider here, for example "github.com/acme/". Separate two or
# more values with a space. Set $Remote = "" to run in all repositories.
$Remote = if ($null -ne $env:GIT_AI_USAGE_REMOTE) { $env:GIT_AI_USAGE_REMOTE } else { "github.com/REPLACE_ORG/" }
$Source = if ($env:GIT_AI_USAGE_SOURCE) { $env:GIT_AI_USAGE_SOURCE } else { "https://raw.githubusercontent.com/Pensero/ai-usage/main/git-ai-usage" }
$ErrorActionPreference = "Stop"

if ($Remote -like "*REPLACE_ORG*") {
    Write-Error "Set `$Remote to the organization of your git provider, for example github.com/acme/."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git ai-usage needs Git for Windows (https://git-scm.com/download/win)."
}

$Python = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
    $arguments = @($candidate | Select-Object -Skip 1) + @("-c", "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')")
    $found = (& $candidate[0] @arguments 2>$null | Select-Object -Last 1)
    if ($found -and (Test-Path $found)) { $Python = $found; break }
}
if (-not $Python) {
    Write-Error "git ai-usage needs Python 3.9 or later (https://www.python.org/downloads/windows/)."
}

$Folder = Join-Path $env:LOCALAPPDATA "git-ai-usage"
$Script = Join-Path $Folder "git-ai-usage"
New-Item -ItemType Directory -Force -Path $Folder | Out-Null
if (Test-Path $Source) {
    Copy-Item $Source "$Script.tmp" -Force
} else {
    Invoke-WebRequest -UseBasicParsing -Uri $Source -OutFile "$Script.tmp"
}
Move-Item "$Script.tmp" $Script -Force

$Flags = @()
foreach ($value in ($Remote -split "\s+" | Where-Object { $_ })) { $Flags += @("--remote", $value) }
& $Python $Script install --global @Flags
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$PythonPath = $Python -replace "\\", "/"
$ScriptPath = $Script -replace "\\", "/"
git config --global alias.ai-usage "!cd `"`${GIT_PREFIX:-.}`" && '$PythonPath' '$ScriptPath'"
