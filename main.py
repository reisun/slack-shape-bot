import logging
import os
import re
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
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

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# thread_ts -> {task: str, channel: str}
pending: dict[str, dict] = {}

SYSTEM_PROMPT = """\
あなたは自宅環境でホスティングされたClaudeの会話サービスです。
ユーザーの質問や相談に自然に応じてください。
日本語で、カジュアルな口調で応答してください。

あなたには以下の特別な能力があります:

## プロジェクト作成機能
ユーザーが何かを「作りたい」「自動化したい」といったソフトウェア開発の要望を持っていると\
判断した場合、会話の中で自然にアイデアを評価し、以下の判定を行ってください:

- アイデアが具体的で実現可能 → 応答の末尾に **GO** マーカーを付与
- 情報が不足していて判断できない → 追加質問をして深掘り（マーカー不要）
- 実現が難しい・推奨できない → その旨を伝える（マーカー不要）

**GO** を付ける場合、応答の最後に以下の形式でタスク記述を含めてください:
```task
（ここにタスクの具体的な記述を書く）
```

この機能はあくまで会話の一部です。ユーザーが雑談や質問をしているだけなら、\
普通に会話してください。プロジェクト作成を押し付けないでください。

## コンテキスト
応答は短めに（3-5文以内）。
"""


# ---------------------------------------------------------------------------
# claude CLI helper
# ---------------------------------------------------------------------------

def run_claude(prompt: str, cwd: str | None = None, timeout: int = 300,
               system_prompt: str | None = None,
               model: str | None = None) -> tuple[int, str]:
    """Run claude -p <prompt> and return (returncode, stdout)."""
    cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    if model:
        cmd.extend(["--model", model])
    result = subprocess.run(
        cmd,
        cwd=cwd or str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout.strip()
    if not output and result.stderr.strip():
        logger.warning("claude stderr: %s", result.stderr.strip()[:500])
        output = result.stderr.strip()
    return result.returncode, output


def chat(user_message: str) -> tuple[str, str | None]:
    """Send a message to Claude and return (response, task_description or None).

    If response contains **GO** and a ```task block, extracts the task description.
    """
    now = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
    prompt = f"現在時刻（日本時間）: {now}\n\nユーザー: {user_message}"

    _, output = run_claude(prompt, system_prompt=SYSTEM_PROMPT)

    task_description = None
    if "**GO**" in output:
        task_match = re.search(r"```task\s*\n(.*?)```", output, re.DOTALL)
        if task_match:
            task_description = task_match.group(1).strip()
        else:
            task_description = user_message
        # Clean markers from displayed response
        output = re.sub(r"\s*```task\s*\n.*?```", "", output, flags=re.DOTALL).strip()

    return output, task_description


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


def run_implementation(project_dir: Path, task_description: str) -> bool:
    logger.info("[implementation] starting claude in %s", project_dir)
    code, output = run_claude(
        f"/direct-task {task_description}",
        cwd=str(project_dir),
        timeout=900,
        model="sonnet",
    )
    logger.info("[implementation] claude finished (rc=%d), output length=%d", code, len(output))
    logger.debug("[implementation] output: %s", output[:1000])
    if code != 0:
        logger.error("[implementation] claude failed (rc=%d): %s", code, output[:500])
    return code == 0


def create_pr(project_dir: Path, repo_name: str, task_description: str) -> str:
    cwd = str(project_dir)
    _run_checked(["git", "add", "."], cwd, "pr-git-add")
    rc, _, _ = _run(["git", "diff", "--cached", "--quiet"], cwd)
    if rc != 0:
        _run_checked(["git", "commit", "-m", "Implement initial version"], cwd, "pr-git-commit")
    else:
        logger.warning("[pr] no changes to commit")

    _run_checked(["git", "push", "-u", "origin", "feature/initial-implementation"], cwd, "pr-git-push")

    rc, stdout, stderr = _run_checked(
        [
            "gh", "pr", "create",
            "--title", f"Implement {repo_name}",
            "--body",
            f"## Summary\n\n{task_description}\n\n"
            "🤖 Generated by slack-shape-bot",
            "--base", "main",
        ],
        cwd, "gh-pr-create",
    )
    return stdout.strip().split("\n")[-1]


def run_pipeline(repo_name: str, task_description: str, channel: str, thread_ts: str):
    def post(msg: str):
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)

    try:
        post(f":hammer_and_wrench: リポジトリ `{repo_name}` を作成中...")
        project_dir = setup_repo(repo_name, task_description)
        post(":robot_face: 実装中... しばらくお待ちください")
        ok = run_implementation(project_dir, task_description)
        if not ok:
            post(":warning: 実装に失敗しましたが、PR作成を試みます")
        post(":twisted_rightwards_arrows: PR を作成中...")
        pr_url = create_pr(project_dir, repo_name, task_description)
        if pr_url:
            post(f":white_check_mark: 完了！\nPR: {pr_url}")
        else:
            post(":warning: 完了しましたが、PR URLを取得できませんでした。GitHub を確認してください。")
    except Exception as e:
        logger.exception("[pipeline] error")
        post(f":x: エラーが発生しました:\n```{e}```")


# ---------------------------------------------------------------------------
# Slack event handler
# ---------------------------------------------------------------------------

@app.event("message")
def handle_message(event, say, client):
    if event.get("bot_id") or event.get("subtype"):
        return

    text = event.get("text", "")
    thread_ts = event.get("thread_ts")
    channel = event.get("channel", "")

    logger.debug("message: thread_ts=%s text=%s pending=%s",
                 thread_ts, text[:80], list(pending.keys()))

    # Reply in a thread waiting for a project name (GO confirmed)
    if thread_ts and thread_ts in pending:
        raw = text.strip().lower()
        repo_name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
        repo_name = re.sub(r"-{2,}", "-", repo_name)
        if not repo_name or len(repo_name) > 100:
            say(
                "リポジトリ名が不正です。英数字とハイフンのみ・100文字以内で入力してください。\n"
                "例: `my-app`",
                thread_ts=thread_ts,
            )
            return

        info = pending.pop(thread_ts)
        say(f"`{repo_name}` で作業を開始します！", thread_ts=thread_ts)
        threading.Thread(
            target=run_pipeline,
            args=(repo_name, info["task"], channel, thread_ts),
            daemon=True,
        ).start()
        return

    # General conversation
    ts = event.get("ts")
    reply_thread = thread_ts or ts

    try:
        client.reactions_add(channel=channel, name="thinking_face", timestamp=ts)
    except Exception:
        pass

    try:
        response, task_description = chat(text)
    except Exception:
        logger.exception("Claude chat failed")
        response, task_description = "ちょっとエラーが起きちゃった :sweat_smile: もう一度試してみて！", None

    say(text=response, thread_ts=reply_thread)

    try:
        client.reactions_remove(channel=channel, name="thinking_face", timestamp=ts)
    except Exception:
        pass

    if task_description:
        pending[reply_thread] = {"task": task_description, "channel": channel}
        say(
            "プロジェクト名を教えてください（例: `my-app`）\n"
            "※ 英数字・ハイフンのみ",
            thread_ts=reply_thread,
        )


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
