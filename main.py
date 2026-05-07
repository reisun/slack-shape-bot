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

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Tracks threads where a pipeline is already running to avoid duplicates
_running: set[str] = set()

# ---------------------------------------------------------------------------
# Agent Gateway polling interval
# ---------------------------------------------------------------------------
_POLL_INTERVAL = 5  # seconds

SYSTEM_PROMPT_TEMPLATE = """\
あなたは自宅環境でホスティングされたClaudeの会話サービスです。
ユーザーの質問や相談に自然に応じてください。
日本語で、カジュアルな口調で応答してください。
Slackチャンネルに投稿されるため書式に注意してください。

## あなたの実行環境
あなたはユーザーの自宅サーバー上の Docker コンテナ内で動作しています。
- ワークスペース: /workspace/ に全プロジェクトのソースコードがマウントされている
- Docker: Docker socket がマウントされており、docker / docker compose コマンドが使える
- GitHub: gh CLI が認証済みで利用可能
- つまり、あなたはコードの参照・修正・ビルド・デプロイを自分自身で実行できる環境にいます
- 「ユーザーに手動で実行してもらう」必要はほとんどありません

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
               permissions: str = "full") -> tuple[int, str]:
    """Run claude via Agent Gateway and return (returncode, output).

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
        return 1, f"Agent Gateway error: {e}"

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

        status = data.get("status")
        if status == "done":
            return data.get("exit_code", 0), data.get("result", "")
        elif status == "failed":
            return data.get("exit_code", 1), data.get("error", "Job failed")

        if timeout and elapsed > timeout + 60:
            logger.error("[agent-gateway] Job %s exceeded timeout", job_id)
            return 1, "Agent Gateway job timed out"


def chat(prompt: str, on_progress=None) -> tuple[str, str | None, str | None, str]:
    """Send a prompt to Claude and return (response, task_description, repo_name, mode).

    mode is "new", "update", or "chat".
    """
    _, output = run_claude(prompt, system_prompt=_build_system_prompt(), on_progress=on_progress)

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
    """Run claude for implementation via Agent Gateway.

    on_progress(elapsed_min: int) is called periodically while claude runs.
    """
    logger.info("[implementation] starting claude in %s", project_dir)

    def on_progress_min(elapsed_sec: int):
        elapsed_min = elapsed_sec // 60
        if on_progress and elapsed_min > 0:
            on_progress(elapsed_min)

    returncode, output = run_claude(
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
    _, summary = run_claude(summary_prompt, system_prompt=_build_system_prompt())
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
        response, task_description, repo_name, mode = chat(prompt, on_progress=on_progress)
    except Exception:
        logger.exception("Claude chat failed")
        response, task_description, repo_name, mode = "ちょっとエラーが起きちゃった :sweat_smile: もう一度試してみて！", None, None, "chat"

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


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
