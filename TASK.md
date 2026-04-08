# TASK

## Done

- [x] Repository initialized
- [x] README, .gitignore, .env.example created
- [x] Slack bot skeleton (slack_bolt Socket Mode)
- [x] Message pattern detection ("作りたい", "したい", etc.)
- [x] Claude API integration with shape-request system prompt
- [x] Reply to Slack thread with GO/WAIT/REJECT result
- [x] .env loading (python-dotenv)
- [x] requirements.txt
- [x] Dockerfile + docker-compose.yml
- [x] GO pipeline: project name prompt → repo setup → implementation → PR

## Backlog

- [ ] Slack App の設定手順書（Socket Mode 有効化・権限スコープ）
- [ ] エラー時の Slack 通知の改善（Claude API 障害など）
- [ ] WAIT / REJECT 後の再提案フロー
- [ ] 複数チャンネル同時対応のテスト
