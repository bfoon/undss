<#
  NES Sentinel — Windows installer. Run in an elevated PowerShell:

      Set-ExecutionPolicy -Scope Process Bypass
      .\install-windows.ps1 -Server https://unpass.example.org -Code NES-XXXXX-XXXXX-XXXXX-XXXXX

  What it does
    * copies nes-sentinel.exe to "C:\Program Files\NES Sentinel"
      (checking SHA256SUMS if it is next to this script)
    * creates "C:\ProgramData\NES Sentinel", readable only by SYSTEM,
      Administrators and LOCAL SERVICE
    * enrols (you confirm the server fingerprint ICT reads to you)
    * registers a scheduled task that runs at start-up as LOCAL SERVICE —
      the least-privileged built-in account — and restarts it if it stops

  The node key is protected with DPAPI bound to this computer: the file is
  useless if copied anywhere else.
#>
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [Parameter(Mandatory = $true)][string]$Code,
  [string]$Fingerprint = ""
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this from an elevated (Administrator) PowerShell."
}
if (-not $Server.StartsWith("https://")) { throw "The server address must start with https://" }

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe     = Join-Path $here "nes-sentinel.exe"
$prefix  = Join-Path $env:ProgramFiles "NES Sentinel"
$data    = Join-Path $env:ProgramData "NES Sentinel"
if (-not (Test-Path $exe)) { throw "nes-sentinel.exe was not found next to this script." }

$sums = Join-Path $here "SHA256SUMS"
if (Test-Path $sums) {
  $want = (Get-Content $sums | Where-Object { $_ -match "nes-sentinel.exe" }) -split "\s+" | Select-Object -First 1
  $have = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
  if ($want -and $want.ToLower() -ne $have) { throw "Checksum mismatch — do not install this file." }
}

New-Item -ItemType Directory -Force -Path $prefix | Out-Null
Copy-Item $exe (Join-Path $prefix "nes-sentinel.exe") -Force
New-Item -ItemType Directory -Force -Path $data | Out-Null
# SYSTEM, Administrators: full. LOCAL SERVICE: modify. Nobody else. Inherited.
icacls $data /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-19:(OI)(CI)M" | Out-Null

$bin = Join-Path $prefix "nes-sentinel.exe"
$env:NES_SENTINEL_HOME = $data
$enrolArgs = @("--home", $data, "enroll", "--server", $Server, "--code", $Code)
if ($Fingerprint) { $enrolArgs += @("--expect-fingerprint", $Fingerprint) }
& $bin @enrolArgs
if ($LASTEXITCODE -ne 0) { throw "Enrolment did not complete." }

# Register the Event Log source now, as Administrator: LOCAL SERVICE may
# write to it but could not create it.
eventcreate /T INFORMATION /ID 1 /L APPLICATION /SO "NES Sentinel" /D "NES Sentinel installed." | Out-Null

$action   = New-ScheduledTaskAction -Execute $bin -Argument "--home `"$data`" run"
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$who      = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\LOCAL SERVICE" -LogonType ServiceAccount -RunLevel Limited
Register-ScheduledTask -TaskName "NES Sentinel" -Action $action -Trigger $trigger -Settings $settings `
  -Principal $who -Description "Independent tamper witness for UN PASS NES documents." -Force | Out-Null
Start-ScheduledTask -TaskName "NES Sentinel"

Write-Host ""
Write-Host "Installed and running. Next, so it can raise the alarm without UN PASS:"
Write-Host "  & `"$bin`" --home `"$data`" set-email --host smtp.your-office.org --port 587 --user sentinel@your-office.org --from sentinel@your-office.org --to ict@your-office.org"
Write-Host "  & `"$bin`" --home `"$data`" test-alert"
Write-Host "Check a document any time:  & `"$bin`" --home `"$data`" verify C:\path\to\signed.pdf"
