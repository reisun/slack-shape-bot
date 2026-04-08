import os
import re
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TRIGGER_PATTERN = re.compile(
    r"(作りたい|したい|欲しい|○○な|アプリ|ツール|bot|Bot|システム|サービス|機能|自動化)",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """あなたはアプリ要望の受付役・プロデューサーです。
ユーザーから「○○なアプリを作りたい」などの要望を受け取り、以下の観点で評価してください。

評価観点:
1. 1日で動く最小形（MVP）にできそうか
2. シンプルに始められそうか（複雑な依存・設計が不要か）
3. やる価値があるか（明確なユースケースがあるか）

判定基準:
- GO: 上記3つを満たす
- WAIT: 価値はあるが、そのままでは1日で動かない / 複雑で工夫が必要
- REJECT: 価値が弱い / 複雑さに見合わない

出力フォーマット（マークダウン）:
## 要望の要約
（1〜2文で）

## 利用場面
（誰がいつどう使うか）

## 狙い
（解決したい課題・得たい価値）

## 判定
**GO** / **WAIT** / **REJECT**（どれか一つ）

## 判定理由
（2〜3文で）

## 次アクション
（GO の場合は粗いタスク文、WAIT は何が解消されれば GO になるか、REJECT は代替案など）

回答は日本語で、簡潔に。余計な前置きは不要。"""


@app.event("message")
def handle_message(event, say, client):
    # ignore bot messages and edits
    if event.get("bot_id") or event.get("subtype"):
        return

    text = event.get("text", "")
    if not TRIGGER_PATTERN.search(text):
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]

    # post a "thinking" reaction to acknowledge
    try:
        client.reactions_add(channel=channel, name="thinking_face", timestamp=event["ts"])
    except Exception:
        pass

    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )

    result = response.content[0].text

    say(text=result, thread_ts=thread_ts)

    try:
        client.reactions_remove(channel=channel, name="thinking_face", timestamp=event["ts"])
    except Exception:
        pass


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
