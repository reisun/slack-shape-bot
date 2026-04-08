import logging
import os
import re
import threading
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from slack_bolt import App, Assistant, Say, SetStatus, SetSuggestedPrompts, SetTitle
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

# thread_ts -> {original: str, history: list[str], channel: str}
waiting: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# claude CLI helper
# ---------------------------------------------------------------------------

def run_claude(prompt: str, cwd: str | None = None, timeout: int = 300) -> tuple[int, str]:
    """Run claude -p <prompt> and return (returncode, stdout)."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions"],
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


# ---------------------------------------------------------------------------
# Idea evaluation via /shape-request skill
# ---------------------------------------------------------------------------

def evaluate_idea(text: str) -> tuple[str, str, str]:
    """Returns (judgment, full_response, task_description)."""
    _, output = run_claude(f"/shape-request {text}")

    if "**GO**" in output:
        judgment = "GO"
    elif "**WAIT**" in output:
        judgment = "WAIT"
    else:
        judgment = "REJECT"

    # Extract task description from "GO 時の粗いタスク文" section (blockquote)
    task_match = re.search(r"(?:GO\s*時の)?粗いタスク文.*?\n(>.+?)(?:\n#{2,}|\n[^>])", output, re.DOTALL)
    if task_match:
        # Strip leading '> ' from blockquote lines
        task_description = re.sub(r"^>\s?", "", task_match.group(1).strip(), flags=re.MULTILINE)
    else:
        task_description = text

    return judgment, output, task_description


def reevaluate_idea(original: str, history: list[str]) -> tuple[str, str, str]:
    """Re-evaluate with conversation history appended to the original request."""
    conversation = original + "\n\n--- 追加情報 ---\n" + "\n".join(history)
    return evaluate_idea(conversation)


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
        # Repo may already exist — add remote and push manually
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


def run_pipeline(repo_name: str, task_description: str, post):
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
# Slack Assistant
# ---------------------------------------------------------------------------

assistant = Assistant()


@assistant.thread_started
def handle_thread_started(say: Say, set_suggested_prompts: SetSuggestedPrompts):
    say("こんにちは！作りたいものやアイデアを教えてください。評価してフィードバックします。")
    set_suggested_prompts(prompts=[
        {"title": "アイデアを相談", "message": "こんなアプリを作りたいです: "},
        {"title": "ツールの自動化", "message": "この作業を自動化したいです: "},
    ])


@assistant.user_message
def handle_user_message(payload: dict, say: Say, set_status: SetStatus, set_title: SetTitle):
    text = payload.get("text", "")
    thread_ts = payload.get("thread_ts", "")
    channel = payload.get("channel", "")

    logger.debug("user_message: thread_ts=%s pending=%s waiting=%s text=%s",
                 thread_ts, list(pending.keys()), list(waiting.keys()), text[:80])

    # Reply in a thread waiting for a project name (GO confirmed)
    if thread_ts in pending:
        raw = text.strip().lower()
        repo_name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
        repo_name = re.sub(r"-{2,}", "-", repo_name)
        if not repo_name or len(repo_name) > 100:
            say(
                "リポジトリ名が不正です。英数字とハイフンのみ・100文字以内で入力してください。\n"
                "例: `my-app`"
            )
            return

        info = pending.pop(thread_ts)
        say(f"`{repo_name}` で作業を開始します！")

        def post(msg: str):
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)

        threading.Thread(
            target=run_pipeline,
            args=(repo_name, info["task"], post),
            daemon=True,
        ).start()
        return

    # Reply in a thread waiting for clarification (WAIT)
    if thread_ts in waiting:
        info = waiting[thread_ts]
        info["history"].append(text)

        set_status("再評価中...")
        judgment, result_text, task_description = reevaluate_idea(
            info["original"], info["history"]
        )
        say(result_text)

        if judgment == "GO":
            waiting.pop(thread_ts)
            pending[thread_ts] = {"task": task_description, "channel": channel}
            say(
                "GO です！プロジェクト名を教えてください（例: `my-app`）\n"
                "※ 英数字・ハイフンのみ"
            )
        elif judgment == "REJECT":
            waiting.pop(thread_ts)
        return

    # New idea — evaluate
    set_title(text[:50])
    set_status("アイデアを評価中...")

    judgment, result_text, task_description = evaluate_idea(text)
    say(result_text)

    if judgment == "GO":
        pending[thread_ts] = {"task": task_description, "channel": channel}
        say(
            "GO です！プロジェクト名を教えてください（例: `my-app`）\n"
            "※ 英数字・ハイフンのみ"
        )
    elif judgment == "WAIT":
        waiting[thread_ts] = {
            "original": text,
            "history": [],
            "channel": channel,
        }


app.use(assistant)


@app.event("message")
def handle_message():
    pass


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
