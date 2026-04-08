# slack-shape-bot

Slack bot that receives app ideas, evaluates them with shape-request logic (GO / WAIT / REJECT), and — when GO — automatically creates a GitHub repo, implements the MVP, and opens a PR.

## Flow

```
You (Slack)         Bot                        Workspace
-----------         ---                        ---------
"○○なアプリ作りたい"
                → shape-request 評価
                ← GO / WAIT / REJECT (thread)
"my-app"  ←←←← (GO の場合) プロジェクト名を質問
                → /workspace/my-app を作成
                → GitHub repo 作成 & push
                → claude -p で実装
                → gh pr create
                ← PR URL (thread)
```

## Stack

- Python 3.11+
- [slack_bolt](https://github.com/slackapi/bolt-python) (Socket Mode)
- [anthropic](https://github.com/anthropics/anthropic-sdk-python)
- [claude CLI](https://github.com/anthropics/claude-code) — MVP 実装に使用
- Docker / docker compose

## Trigger keywords

`作りたい` `したい` `欲しい` `アプリ` `ツール` `bot` `システム` `サービス` `機能` `自動化`

## Setup (Docker — recommended)

```bash
cp .env.example .env
# .env を編集してトークンを記入

docker compose up -d
```

```bash
docker compose logs -f   # ログ確認
docker compose down      # 停止
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | App-Level Token for Socket Mode (`xapp-...`) |
| `GITHUB_OWNER` | GitHub ユーザー名または Org 名 |
| `HOST_WORKSPACE` | ホスト側の workspace パス（絶対パス） |
| `WORKSPACE_DIR` | コンテナ内の workspace パス（例: `/workspace`） |

> `ANTHROPIC_API_KEY` は不要です。Claude の認証は `~/.claude` マウントで行われます。

## Slack App の設定

1. https://api.slack.com/apps でアプリを作成
2. **Socket Mode** を有効化 → App-Level Token (`xapp-...`) を発行
3. **Event Subscriptions** → Subscribe to bot events:
   - `message.channels`（パブリックチャンネル）
   - `message.groups`（プライベートチャンネル）
4. **OAuth & Permissions** → Bot Token Scopes:
   - `chat:write`
   - `reactions:write`
   - `channels:history`（または `groups:history`）
5. ワークスペースにインストール → Bot Token (`xoxb-...`) をコピー

## Volume mounts (docker-compose.yml)

| Host | Container | 用途 |
|------|-----------|------|
| `HOST_WORKSPACE` | `/workspace` | 新規プロジェクトの作成先 |
| `~/.config/gh` | `/root/.config/gh` | gh CLI 認証 |
| `~/.claude` | `/root/.claude` | claude CLI 認証 |
