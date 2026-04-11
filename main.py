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

# thread_ts -> {task: str, channel: str, repo_name: str, mode: str}
pending: dict[str, dict] = {}

_CONFIRM_YES = re.compile(r"^(ok|yes|はい|うん|お願い|いいよ|よろしく|やって|頼む|go|おk|おけ|いける|大丈夫|おなしゃす|y)$", re.IGNORECASE)
_CONFIRM_NO = re.compile(r"^(no|いいえ|やめ|キャンセル|cancel|stop|やっぱ|やだ|ストップ|なし|n)$", re.IGNORECASE)

SYSTEM_PROMPT_TEMPLATE = """\
あなたは自宅環境でホスティングされたClaudeの会話サービスです。
ユーザーの質問や相談に自然に応じてください。
日本語で、カジュアルな口調で応答してください。
Slackチャンネルに投稿されるため書式に注意してください。

あなたには以下の特別な能力があります:

## プロジェクト作成・更新機能
ユーザーがソフトウェア開発の要望を持っていると判断した場合、\
会話の中で自然にアイデアを評価してください。

### 新規プロジェクト
まだ存在しないものを作りたい場合:
- アイデアが具体的で実現可能 → 応答の末尾に **GO** マーカーを付与
- 情報が不足 → 追加質問（マーカー不要）

**GO** を付ける場合、プロジェクト名とタスクの両方を含めてください:
```name
（英数字・ハイフンのみのリポジトリ名。内容に合った簡潔な名前）
```
```task
（タスクの具体的な記述）
```

### 既存プロジェクトの更新
既にあるリポジトリやプロジェクトを修正・機能追加したい場合:
- 要望が具体的 → 応答の末尾に **UPDATE** マーカーを付与
- 情報が不足 → 追加質問（マーカー不要）

**UPDATE** を付ける場合、対象のプロジェクト名とタスクの両方を含めてください:
```name
（既存リポジトリ一覧から正確な名前を選ぶこと）
```
```task
（変更内容の具体的な記述）
```

### 判断のポイント
- ユーザーが「〇〇を作りたい」→ **GO**（新規）
- ユーザーが「〇〇を修正して」「〇〇に機能追加して」「〇〇のバグを直して」→ **UPDATE**（既存）
- ユーザーが対象のプロジェクト名/リポジトリ名に言及している場合は **UPDATE**
- 雑談や質問なら普通に会話（マーカー不要）

この機能はあくまで会話の一部です。プロジェクト作成・更新を押し付けないでください。

### 既存リポジトリ一覧
{repo_list}

## コンテキスト
応答は短めに（3-5文以内）。
"""


def _build_system_prompt() -> str:
    """Build system prompt with current list of existing repositories."""
    repos = sorted(d.name for d in WORKSPACE.iterdir() if d.is_dir() and not d.name.startswith("."))
    repo_list = ", ".join(repos) if repos else "（なし）"
    return SYSTEM_PROMPT_TEMPLATE.format(repo_list=repo_list)


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


def chat(user_message: str) -> tuple[str, str | None, str | None, str]:
    """Send a message to Claude and return (response, task_description, repo_name, mode).

    mode is "new", "update", or "chat".
    """
    now = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
    prompt = f"現在時刻（日本時間）: {now}\n\nユーザー: {user_message}"

    _, output = run_claude(prompt, system_prompt=_build_system_prompt())

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
            task_description = user_message

        name_match = re.search(r"```name\s*\n(.*?)```", output, re.DOTALL)
        if name_match:
            raw = name_match.group(1).strip().lower()
            repo_name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
            repo_name = re.sub(r"-{2,}", "-", repo_name)
            if not repo_name or len(repo_name) > 100:
                repo_name = None

        # Clean markers from displayed response
        output = re.sub(r"\s*```task\s*\n.*?```", "", output, flags=re.DOTALL).strip()
        output = re.sub(r"\s*```name\s*\n.*?```", "", output, flags=re.DOTALL).strip()
        output = output.replace("**GO**", "").replace("**UPDATE**", "").strip()

    return output, task_description, repo_name, mode


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
    """Run claude for implementation with progress reporting via on_progress callback.

    on_progress(elapsed_min: int) is called periodically while claude runs.
    No hard timeout — runs until completion.
    """
    logger.info("[implementation] starting claude in %s", project_dir)
    cmd = [
        "claude", "-p", f"/direct-task {task_description}",
        "--dangerously-skip-permissions", "--model", "sonnet",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    start = time.monotonic()
    while proc.poll() is None:
        time.sleep(30)
        elapsed = int((time.monotonic() - start) / 60)
        if on_progress and elapsed > 0:
            on_progress(elapsed)

    stdout = proc.stdout.read()
    stderr = proc.stderr.read()
    code = proc.returncode

    logger.info("[implementation] claude finished (rc=%d, %.0fs), output length=%d",
                code, time.monotonic() - start, len(stdout))
    logger.debug("[implementation] output: %s", (stdout or stderr)[:1000])
    if code != 0:
        logger.error("[implementation] claude failed (rc=%d): %s", code, (stdout or stderr)[:500])
    return code == 0


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

    try:
        if mode == "update":
            post(f":wrench: リポジトリ `{repo_name}` を更新準備中...")
            project_dir = setup_update(repo_name)
        else:
            post(f":hammer_and_wrench: リポジトリ `{repo_name}` を作成中...")
            project_dir = setup_repo(repo_name, task_description)

        impl_ts = post(":robot_face: 実装中... しばらくお待ちください")

        def on_progress(elapsed_min: int):
            update(impl_ts, f":robot_face: 実装中... ({elapsed_min}分経過)")

        ok = run_implementation(project_dir, task_description, on_progress=on_progress)

        if ok:
            update(impl_ts, ":robot_face: 実装完了")
        else:
            update(impl_ts, ":warning: 実装に問題がありましたが、PR作成を試みます")

        pr_ts = post(":twisted_rightwards_arrows: PR を作成中...")
        if mode == "update":
            pr_url = create_pr(project_dir, repo_name, task_description,
                               commit_msg="Update: " + task_description[:60],
                               pr_title=f"Update {repo_name}")
        else:
            pr_url = create_pr(project_dir, repo_name, task_description)
        if pr_url:
            update(pr_ts, f":white_check_mark: 完了！\nPR: {pr_url}")
        else:
            update(pr_ts, ":warning: 完了しましたが、PR URLを取得できませんでした。GitHub を確認してください。")
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

    # Reply in a thread waiting for confirmation (OK / cancel)
    if thread_ts and thread_ts in pending:
        answer = text.strip()
        if _CONFIRM_NO.match(answer):
            pending.pop(thread_ts)
            say("了解、キャンセルしました :+1:", thread_ts=thread_ts)
            return
        if not _CONFIRM_YES.match(answer):
            say("「OK」で開始、「やめる」でキャンセルできます。", thread_ts=thread_ts)
            return

        info = pending.pop(thread_ts)
        repo_name = info["repo_name"]
        mode = info.get("mode", "new")
        if mode == "update":
            say(f"`{repo_name}` の更新を開始します！", thread_ts=thread_ts)
        else:
            say(f"`{repo_name}` で作業を開始します！", thread_ts=thread_ts)
        threading.Thread(
            target=run_pipeline,
            args=(repo_name, info["task"], channel, thread_ts, mode),
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
        response, task_description, repo_name, mode = chat(text)
    except Exception:
        logger.exception("Claude chat failed")
        response, task_description, repo_name, mode = "ちょっとエラーが起きちゃった :sweat_smile: もう一度試してみて！", None, None, "chat"

    say(text=response, thread_ts=reply_thread)

    try:
        client.reactions_remove(channel=channel, name="thinking_face", timestamp=ts)
    except Exception:
        pass

    if task_description and repo_name:
        pending[reply_thread] = {"task": task_description, "channel": channel, "mode": mode, "repo_name": repo_name}
        if mode == "update":
            say(
                f"`{repo_name}` を更新します。よろしいですか？（OK / やめる）",
                thread_ts=reply_thread,
            )
        else:
            say(
                f"`{repo_name}` を作成します。よろしいですか？（OK / やめる）",
                thread_ts=reply_thread,
            )
    elif task_description:
        # repo_name could not be determined — fall back to asking
        say(
            "プロジェクト名を決められませんでした。もう少し詳しく教えてもらえますか？",
            thread_ts=reply_thread,
        )


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
