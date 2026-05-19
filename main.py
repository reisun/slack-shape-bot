import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)

WORKSPACE = Path(os.environ["WORKSPACE_DIR"])
NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
AGENT_GATEWAY_URL = os.environ.get("AGENT_GATEWAY_URL", "http://llm-internal-proxy/agent")
_AGENT_GATEWAY_BASE = AGENT_GATEWAY_URL.rsplit("/agent", 1)[0] or AGENT_GATEWAY_URL

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# ---------------------------------------------------------------------------
# Agent Gateway settings
# ---------------------------------------------------------------------------
_POLL_INTERVAL = 5  # seconds
_TOKEN_CHECK_INTERVAL = 6 * 60 * 60  # 6 hours

_SYSTEM_PROMPT = (
    "あなたは自宅サーバー上の Docker コンテナ内で動作しています。"
    " /workspace/ に全プロジェクトがマウントされており、"
    " docker / docker compose / gh CLI が利用可能です。"
    " 日本語で応答してください。"
)


# ---------------------------------------------------------------------------
# Slack thread history
# ---------------------------------------------------------------------------

def _fetch_thread_history(client, channel: str, thread_ts: str) -> list[dict]:
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        return result.get("messages", [])
    except Exception:
        logger.warning("Failed to fetch thread history", exc_info=True)
        return []


def _build_conversation_prompt(messages: list[dict], bot_user_id: str | None) -> str:
    now = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
    lines = [f"現在時刻（日本時間）: {now}", "", "=== 会話履歴 ==="]
    for msg in messages:
        if msg.get("subtype"):
            continue
        text = msg.get("text", "")
        if not text:
            continue
        if msg.get("bot_id") or msg.get("user") == bot_user_id:
            lines.append(f"あなた: {text}")
        else:
            lines.append(f"ユーザー: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude CLI via Agent Gateway
# ---------------------------------------------------------------------------

def run_claude(prompt: str, timeout: int = 1800,
               on_progress=None) -> tuple[str, list[str]]:
    """Run claude via Agent Gateway and return (output, warnings)."""
    payload = {
        "agent": "claude",
        "prompt": prompt,
        "cwd": str(WORKSPACE),
        "timeout": timeout,
        "permissions": "full",
        "system_prompt": _SYSTEM_PROMPT,
    }

    try:
        resp = requests.post(f"{AGENT_GATEWAY_URL}/run", json=payload, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[agent-gateway] Failed to submit job: %s", e)
        return f"Agent Gateway error: {e}", []

    job_id = resp.json()["job_id"]
    logger.info("[agent-gateway] Job submitted: %s", job_id)

    start = time.monotonic()
    while True:
        time.sleep(_POLL_INTERVAL)
        elapsed = int(time.monotonic() - start)

        if on_progress and elapsed >= 10:
            on_progress(elapsed)

        try:
            status_resp = requests.get(
                f"{AGENT_GATEWAY_URL}/jobs/{job_id}", timeout=10
            )
            status_resp.raise_for_status()
            data = status_resp.json()
        except requests.RequestException as e:
            logger.warning("[agent-gateway] Poll error: %s", e)
            continue

        warnings = data.get("warnings") or []
        status = data.get("status")
        if status == "done":
            return data.get("result", ""), warnings
        elif status == "failed":
            error = data.get("error", "Job failed")
            if "[token-expired]" in error or "[auth-hint]" in error:
                warnings.append(error)
            return error, warnings

        if timeout and elapsed > timeout + 60:
            logger.error("[agent-gateway] Job %s exceeded timeout", job_id)
            return "Agent Gateway job timed out", []


# ---------------------------------------------------------------------------
# Slack event handler
# ---------------------------------------------------------------------------

def _respond_with_progress(channel: str, reply_thread: str, text: str, thread_ts: str | None):
    resp = app.client.chat_postMessage(
        channel=channel,
        thread_ts=reply_thread,
        text=":thinking_face: 考え中...",
    )
    status_ts = resp.get("ts", "")

    def on_progress(elapsed_sec: int):
        elapsed_min = elapsed_sec // 60
        elapsed_remaining = elapsed_sec % 60
        if elapsed_min > 0:
            time_str = f"{elapsed_min}分{elapsed_remaining}秒"
        else:
            time_str = f"{elapsed_sec}秒"
        try:
            app.client.chat_update(
                channel=channel, ts=status_ts,
                text=f":thinking_face: 考え中... ({time_str}経過)",
            )
        except Exception:
            logger.warning("Failed to update progress message", exc_info=True)

    bot_user_id = None
    try:
        auth = app.client.auth_test()
        bot_user_id = auth.get("user_id")
    except Exception:
        pass

    if thread_ts:
        messages = _fetch_thread_history(app.client, channel, thread_ts)
        prompt = _build_conversation_prompt(messages, bot_user_id)
    else:
        now = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
        prompt = f"現在時刻（日本時間）: {now}\n\nユーザー: {text}"

    try:
        response, warnings = run_claude(prompt, on_progress=on_progress)
    except Exception:
        logger.exception("Claude chat failed")
        response, warnings = "ちょっとエラーが起きちゃった :sweat_smile: もう一度試してみて！", []

    if warnings:
        warning_text = "\n".join(f":warning: {w}" for w in warnings)
        response = f"{response}\n\n{warning_text}"

    try:
        app.client.chat_update(channel=channel, ts=status_ts, text=response)
    except Exception:
        logger.exception("Failed to update final message")


@app.event("message")
def handle_message(event, say, client):
    if event.get("bot_id") or event.get("subtype"):
        return

    text = event.get("text", "").strip()
    thread_ts = event.get("thread_ts")
    channel = event.get("channel", "")
    ts = event.get("ts")
    reply_thread = thread_ts or ts

    threading.Thread(
        target=_respond_with_progress,
        args=(channel, reply_thread, text, thread_ts),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Token expiry monitor
# ---------------------------------------------------------------------------

def _check_token_health():
    if not NOTIFY_CHANNEL:
        return
    try:
        resp = requests.get(f"{_AGENT_GATEWAY_BASE}/health", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("[token-monitor] Health check failed: %s", e)
        return

    warning = data.get("token_warning")
    if not warning:
        return

    days = data.get("token_days_remaining", "?")
    expires_at = data.get("token_expires_at", "不明")
    if isinstance(days, int) and days < 0:
        icon = ":rotating_light:"
        text = f"{icon} *Claude トークン期限切れ*\n{warning}\n有効期限: {expires_at}"
    else:
        icon = ":warning:"
        text = f"{icon} *Claude トークン期限警告*\n{warning}\n残り {days} 日 (有効期限: {expires_at})"

    try:
        app.client.chat_postMessage(channel=NOTIFY_CHANNEL, text=text)
        logger.info("[token-monitor] Notified channel %s: %s", NOTIFY_CHANNEL, warning)
    except Exception:
        logger.exception("[token-monitor] Failed to send notification")


def _token_monitor_loop():
    time.sleep(10)
    while True:
        _check_token_health()
        time.sleep(_TOKEN_CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=_token_monitor_loop, daemon=True).start()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
