# slack-shape-bot

Slack bot that receives app ideas and evaluates them using shape-request logic (GO / WAIT / REJECT) via Claude API.

## Overview

Post a message like "I want to build ○○ app" in Slack, and the bot automatically:
1. Detects the request
2. Evaluates feasibility using Claude API (shape-request prompt)
3. Returns GO / WAIT / REJECT judgment with reasoning to the thread

## Stack

- Python 3.11+
- [slack_bolt](https://github.com/slackapi/bolt-python) (Socket Mode)
- [anthropic](https://github.com/anthropics/anthropic-sdk-python)
- Docker / docker compose

## Trigger

Messages containing any of: `作りたい` `したい` `欲しい` `アプリ` `ツール` `bot` `システム` `サービス` `機能` `自動化`

The bot replies to the message thread with a GO / WAIT / REJECT evaluation.

## Setup (Docker — recommended)

```bash
cp .env.example .env
# Fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ANTHROPIC_API_KEY

docker compose up -d
```

The bot runs as a persistent container (`restart: unless-stopped`).

```bash
# View logs
docker compose logs -f

# Stop
docker compose down
```

## Setup (local)

```bash
cp .env.example .env
pip install -r requirements.txt
python main.py
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | App-Level Token for Socket Mode (`xapp-...`) |
| `ANTHROPIC_API_KEY` | Anthropic API key |
