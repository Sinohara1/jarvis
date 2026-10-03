# Publish Jarvis.exe as a GitHub Release of the public releases-only repo Sinohara1/jarvis-releases (code lives in the private Sinohara1/jarvis).
# Requires gh CLI (gh auth login). No token is stored in the project or the exe.
# Usage: .\publish_release.ps1 -Version 1.1.0 [-ExePath .\Jarvis.exe] [-Notes "что нового"] [-Prerelease]
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    [string]$ExePath = ".\Jarvis.exe",
    [string]$Repo = "Sinohara1/jarvis-releases",
    [string]$Notes = "",
    [switch]$Prerelease
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path $ExePath)) { throw "Нет файла $ExePath — сначала собери build_exe.ps1" }
$tag = "v$Version"
$args = @("release", "create", $tag, $ExePath, "--repo", $Repo, "--title", "Джарвис $Version", "--notes", $(if ($Notes) { $Notes } else { "Джарвис $Version" }))
if ($Prerelease) { $args += "--prerelease" }
& gh @args
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
Write-Host "Готово: https://github.com/$Repo/releases/tag/$tag"
