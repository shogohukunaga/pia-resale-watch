# ログオン時にリセール監視を自動起動するタスクを登録し、すぐに起動する
$ErrorActionPreference = 'Stop'
$taskName = 'PiaResaleWatch'
$script = Join-Path $PSScriptRoot 'resale_watch.py'
$pythonw = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe が見つかりません: $pythonw" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
# ログオン時に起動。加えて5分ごとに起動を試み、プロセスが止まっていたら再開させる（動作中なら IgnoreNew で何もしない）
$trigger = @(
    New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
)
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'チケットぴあ リセール出品を監視してメール通知' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
