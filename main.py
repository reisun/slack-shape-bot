import logging
import os
import re
import threading
import subprocess
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
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
AGENT_GATEWAY_URL = os.environ.get("AGENT_GATEWAY_URL", "http://llm-internal-proxy/agent")
_AGENT_GATEWAY_BASE = AGENT_GATEWAY_URL.rsplit("/agent", 1)[0] or AGENT_GATEWAY_URL

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Tracks threads where a pipeline is already running to avoid duplicates
_running: set[str] = set()

# ---------------------------------------------------------------------------
# Agent Gateway settings
# ---------------------------------------------------------------------------
_POLL_INTERVAL = 5  # seconds
_TOKEN_CHECK_INTERVAL = 6 * 60 * 60  # 6 hours


def _fetch_thread_history(client, channel: str, thread_ts: str) -> list[dict]:
    """Fetch conversation history for a thread via Slack API."""
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        return result.get("messages", [])
    except Exception:
        logger.warning("Failed to fetch thread history", exc_info=True)
        return []


def _build_conversation_prompt(messages: list[dict], bot_user_id: str | None) -> str:
    """Build a conversation prompt from Slack thread messages."""
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
# claude CLI helper
# ---------------------------------------------------------------------------

def run_claude(prompt: str, cwd: str | None = None, timeout: int = 1800,
               system_prompt: str | None = None,
               model: str | None = None,
               on_progress=None,
               permissions: str = "full") -> tuple[int, str, list[str]]:
    """Run claude via Agent Gateway and return (returncode, output, warnings).

    on_progress(elapsed_sec: int) is called periodically while the job runs.
    """
    payload = {
        "agent": "claude",
        "prompt": prompt,
        "cwd": cwd or str(WORKSPACE),
        "timeout": timeout,
        "permissions": permissions,
    }
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if model:
        payload["model"] = model

    try:
        resp = requests.post(f"{AGENT_GATEWAY_URL}/run", json=payload, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[agent-gateway] Failed to submit job: %s", e)
        return 1, f"Agent Gateway error: {e}", []

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
            return data.get("exit_code", 0), data.get("result", ""), warnings
        elif status == "failed":
            error = data.get("error", "Job failed")
            if "[token-expired]" in error or "[auth-hint]" in error:
                warnings.append(error)
            return data.get("exit_code", 1), error, warnings

        if timeout and elapsed > timeout + 60:
            logger.error("[agent-gateway] Job %s exceeded timeout", job_id)
            return 1, "Agent Gateway job timed out", []


def chat(prompt: str, on_progress=None) -> tuple[str, str | None, str | None, str, list[str]]:
    """Send a prompt to Claude and return (response, task_description, repo_name, mode, warnings).

    mode is "new", "update", or "chat".
    """
    _, output, warnings = run_claude(prompt, on_progress=on_progress)

    task_description = None
    repo_name = None
    mode = "chat"

    if "**UPDATE**" in output:
        mode = "update"
    elif "**GO**" in output:
        mode = "new"

    if mode in ("new", "update"):
        task_match = re.search(r"```task\s*\n(.*?)```", output, re.DOTALL)
        if task_match:
            task_description = task_match.group(1).strip()
        else:
            task_description = prompt

        name_match = re.search(r"```name\s*\n(.*?)```", output, re.DOTALL)
        if name_match:
            raw = name_match.group(1).strip()
            # For updates, try to match an existing directory name exactly
            if mode == "update":
                existing = {d.name for d in WORKSPACE.iterdir() if d.is_dir() and not d.name.startswith(".")}
                if raw in existing:
                    repo_name = raw
                elif raw.lower() in {n.lower() for n in existing}:
                    repo_name = next(n for n in existing if n.lower() == raw.lower())
            # For new projects or if no exact match found, normalize
            if repo_name is None:
                normalized = re.sub(r"[^a-z0-9_-]", "-", raw.lower()).strip("-")
                normalized = re.sub(r"-{2,}", "-", normalized)
                if normalized and len(normalized) <= 100:
                    repo_name = normalized

        # Clean markers from displayed response
        output = re.sub(r"\s*```task\s*\n.*?```", "", output, flags=re.DOTALL).strip()
        output = re.sub(r"\s*```name\s*\n.*?```", "", output, flags=re.DOTALL).strip()
        output = output.replace("**GO**", "").replace("**UPDATE**", "").strip()

    return output, task_description, repo_name, mode, warnings


# ---------------------------------------------------------------------------
# Pipeline: repo setup → implementation → PR
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _run_checked(cmd: list[str], cwd: str | None = None, label: str = "") -> tuple[int, str, str]:
    """Run a command and log failures."""
    rc, stdout, stderr = _run(cmd, cwd)
    if rc != 0:
        logger.error("[%s] command failed (rc=%d): %s\nstdout: %s\nstderr: %s",
                     label, rc, " ".join(cmd), stdout[:500], stderr[:500])
    return rc, stdout, stderr


def setup_repo(repo_name: str, task_description: str) -> Path:
    project_dir = WORKSPACE / repo_name
    project_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "README.md").write_text(f"# {repo_name}\n\n{task_description}\n")
    (project_dir / ".gitignore").write_text("__pycache__/\n*.py[cod]\n.env\n.venv/\n")
    (project_dir / "TASK.md").write_text(
        "# TASK\n\n## Backlog\n\n- [ ] Initial implementation\n"
    )

    cwd = str(project_dir)
    _run_checked(["git", "init"], cwd, "git-init")
    _run_checked(["git", "checkout", "-b", "main"], cwd, "git-checkout-main")
    _run_checked(["git", "config", "user.email", "slack-shape-bot@local"], cwd, "git-config")
    _run_checked(["git", "config", "user.name", "slack-shape-bot"], cwd, "git-config")
    _run_checked(["git", "add", "."], cwd, "git-add")
    _run_checked(["git", "commit", "-m", "Initial scaffold"], cwd, "git-commit")

    rc, _, _ = _run(
        ["gh", "repo", "create", f"{GITHUB_OWNER}/{repo_name}",
         "--private", "--source=.", "--remote=origin", "--push"],
        cwd,
    )
    if rc != 0:
        logger.info("[setup] gh repo create failed (rc=%d), adding remote manually", rc)
        _run_checked(
            ["git", "remote", "add", "origin",
             f"https://github.com/{GITHUB_OWNER}/{repo_name}.git"],
            cwd, "git-remote-add",
        )
        _run_checked(["git", "push", "-u", "origin", "main"], cwd, "git-push-main")

    _run_checked(["git", "checkout", "-b", "feature/initial-implementation"], cwd, "git-checkout-feature")
    return project_dir


def setup_update(repo_name: str) -> Path:
    """既存リポジトリを更新用にセットアップ."""
    project_dir = WORKSPACE / repo_name
    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    cwd = str(project_dir)

    # mainを最新に
    _run_checked(["git", "checkout", "main"], cwd, "git-checkout-main")
    _run_checked(["git", "pull", "origin", "main"], cwd, "git-pull")

    # 更新用ブランチ作成（タイムスタンプ付き）
    branch = f"feature/update-{datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y%m%d-%H%M%S')}"
    _run_checked(["git", "checkout", "-b", branch], cwd, "git-checkout-feature")

    return project_dir


def run_implementation(project_dir: Path, task_description: str,
                       on_progress=None) -> bool:
    """Run claude for implementation via Agent Gateway.

    on_progress(elapsed_min: int) is called periodically while claude runs.
    """
    logger.info("[implementation] starting claude in %s", project_dir)

    def on_progress_min(elapsed_sec: int):
        elapsed_min = elapsed_sec // 60
        if on_progress and elapsed_min > 0:
            on_progress(elapsed_min)

    returncode, output, warnings = run_claude(
        prompt=f"/direct-task {task_description}",
        cwd=str(project_dir),
        timeout=1800,
        model="sonnet",
        on_progress=on_progress_min,
        permissions="full",
    )

    logger.info("[implementation] claude finished (rc=%d), output length=%d",
                returncode, len(output))
    logger.debug("[implementation] output: %s", output[:1000])

    if warnings:
        for w in warnings:
            logger.warning("[implementation] %s", w)
    if returncode != 0:
        logger.error("[implementation] claude failed (rc=%d): %s", returncode, output[:500])
    return returncode == 0


def create_pr(project_dir: Path, repo_name: str, task_description: str,
              commit_msg: str = "Implement initial version",
              pr_title: str | None = None) -> str:
    cwd = str(project_dir)
    _run_checked(["git", "add", "."], cwd, "pr-git-add")
    rc, _, _ = _run(["git", "diff", "--cached", "--quiet"], cwd)
    if rc != 0:
        _run_checked(["git", "commit", "-m", commit_msg], cwd, "pr-git-commit")
    else:
        logger.warning("[pr] no changes to commit")

    # 現在のブランチ名を取得
    _, branch, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    branch = branch.strip()

    _run_checked(["git", "push", "-u", "origin", branch], cwd, "pr-git-push")

    title = pr_title or f"Implement {repo_name}"
    rc, stdout, stderr = _run_checked(
        [
            "gh", "pr", "create",
            "--title", title,
            "--body",
            f"## Summary\n\n{task_description}\n\n"
            "🤖 Generated by slack-shape-bot",
            "--base", "main",
        ],
        cwd, "gh-pr-create",
    )
    return stdout.strip().split("\n")[-1]


def run_pipeline(repo_name: str, task_description: str, channel: str, thread_ts: str,
                 mode: str = "new"):
    def post(msg: str) -> str:
        """Post a new message and return its ts."""
        resp = app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)
        return resp.get("ts", "")

    def update(ts: str, msg: str):
        """Update an existing message by ts."""
        app.client.chat_update(channel=channel, ts=ts, text=msg)

    results = []
    try:
        if mode == "update":
            post(f":wrench: `{repo_name}` の更新を開始します...")
            project_dir = setup_update(repo_name)
            results.append(f"リポジトリ {repo_name} の更新用ブランチを作成しました。")
        else:
            post(f":hammer_and_wrench: `{repo_name}` の作成を開始します...")
            project_dir = setup_repo(repo_name, task_description)
            results.append(f"リポジトリ {repo_name} を作成しました。")

        impl_ts = post(":robot_face: 実装中... しばらくお待ちください")

        def on_progress(elapsed_min: int):
            update(impl_ts, f":robot_face: 実装中... ({elapsed_min}分経過)")

        ok = run_implementation(project_dir, task_description, on_progress=on_progress)
        update(impl_ts, ":robot_face: 実装完了")
        results.append(f"実装の終了コード: {'成功' if ok else '失敗'}")

        if mode == "update":
            pr_url = create_pr(project_dir, repo_name, task_description,
                               commit_msg="Update: " + task_description[:60],
                               pr_title=f"Update {repo_name}")
        else:
            pr_url = create_pr(project_dir, repo_name, task_description)
        results.append(f"PR URL: {pr_url}" if pr_url else "PR の作成に失敗、または変更なし。")

    except Exception as e:
        logger.exception("[pipeline] error")
        results.append(f"エラー: {e}")
    finally:
        _running.discard(thread_ts)

    # Let Claude summarize the results naturally
    summary_prompt = (
        f"以下はプロジェクト「{repo_name}」の{'更新' if mode == 'update' else '作成'}パイプラインの実行結果です。\n"
        f"ユーザーにわかりやすく結果を報告してください。\n\n"
        + "\n".join(f"- {r}" for r in results)
    )
    _, summary, _ = run_claude(summary_prompt)
    post(summary)


# ---------------------------------------------------------------------------
# Slack event handler
# ---------------------------------------------------------------------------

def _respond_with_progress(channel: str, reply_thread: str, text: str, thread_ts: str | None):
    """Post a placeholder message, update it with Claude's response, then launch the pipeline."""
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
        response, task_description, repo_name, mode, warnings = chat(prompt, on_progress=on_progress)
    except Exception:
        logger.exception("Claude chat failed")
        response, task_description, repo_name, mode, warnings = (
            "ちょっとエラーが起きちゃった :sweat_smile: もう一度試してみて！", None, None, "chat", [],
        )

    if warnings:
        warning_text = "\n".join(f":warning: {w}" for w in warnings)
        response = f"{response}\n\n{warning_text}"

    try:
        app.client.chat_update(channel=channel, ts=status_ts, text=response)
    except Exception:
        logger.exception("Failed to update final message")

    if task_description and repo_name:
        _running.add(reply_thread)
        threading.Thread(
            target=run_pipeline,
            args=(repo_name, task_description, channel, reply_thread, mode),
            daemon=True,
        ).start()


@app.event("message")
def handle_message(event, say, client):
    if event.get("bot_id") or event.get("subtype"):
        return

    text = event.get("text", "").strip()
    thread_ts = event.get("thread_ts")
    channel = event.get("channel", "")
    ts = event.get("ts")
    reply_thread = thread_ts or ts

    # Skip if a pipeline is already running in this thread
    if reply_thread in _running:
        return

    threading.Thread(
        target=_respond_with_progress,
        args=(channel, reply_thread, text, thread_ts),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Token expiry monitor
# ---------------------------------------------------------------------------

def _check_token_health():
    """Check Agent Gateway /health for token expiry and notify via Slack."""
    if not NOTIFY_CHANNEL:
        logger.debug("[token-monitor] SLACK_NOTIFY_CHANNEL not set, skipping")
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
    """Background loop that checks token health periodically."""
    time.sleep(10)
    while True:
        _check_token_health()
        time.sleep(_TOKEN_CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=_token_monitor_loop, daemon=True).start()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
