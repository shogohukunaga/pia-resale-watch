# リセール監視タスクを停止して削除する
$taskName = 'PiaResaleWatch'
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
"$taskName を削除しました"
