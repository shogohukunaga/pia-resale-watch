# pia-resale-watch

チケットぴあのリセール出品一覧（`https://cloak.pia.jp/resale/item/list?eventCd=...`）を監視し、
出品があったら Gmail でメール通知する小さなスクリプトです（Python 標準ライブラリのみ）。

- ローカル常駐: `install_task.ps1` でタスクスケジューラに登録（60秒間隔）。停止は `uninstall_task.ps1`。
- バックアップ: GitHub Actions（`.github/workflows/watch.yml`、5分間隔）。
  Secrets に `GMAIL_USER` / `GMAIL_APP_PASSWORD` / `MAIL_TO` を設定する。

```
python resale_watch.py --test-mail      # テストメール
python resale_watch.py --once           # 1回だけチェック
python resale_watch.py --check-file x   # 保存した HTML を判定（送信なし）
```

設定は `.env`（`.env.example` を参照）または環境変数で渡します。監視対象は `EVENT_CD`（カンマ区切りで複数可）。
