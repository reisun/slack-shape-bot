# slack-shape-bot

自宅環境でホスティングされた Claude 会話サービス。Slack チャンネルでの汎用的な会話に応答し、ソフトウェア開発の要望があれば自動でリポジトリ作成・実装・PR作成まで行う。

## Flow

```
You (Slack)         Bot                        Workspace
-----------         ---                        ---------
何でも話しかける
                → Claude (Opus) で応答
                ← 会話の返答 (thread)

「○○を作りたい」
                → Claude が自然に評価
                ← **GO** + タスク記述 (thread)
"my-app"  ←←←← プロジェクト名を質問
                → /workspace/my-app を作成
                → GitHub repo 作成 & push
                → Claude (Sonnet) で実装
                → gh pr create
                ← PR URL (thread)
```

## Stack

- Python 3.11+
- [slack_bolt](https://github.com/slackapi/bolt-python) (Socket Mode)
- [claude CLI](https://github.com/anthropics/claude-code) — 会話応答 (Opus) + 実装 (Sonnet)
- Docker / docker compose

## Setup (Docker)

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

> `ANTHROPIC_API_KEY` は不要です。Claude の認証はホストの `~/.claude` をシンボリックリンクで共有しています。

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
| `~/.config/gh` | `/mnt/gh` → `$HOME/.config/gh` (symlink) | gh CLI 認証 |
| `~/.claude` | `/mnt/claude` → `$HOME/.claude` (symlink) | Claude CLI 認証（ホストと共有） |
| `/var/run/docker.sock` | `/var/run/docker.sock` | 他プロジェクトのデプロイ用 |
