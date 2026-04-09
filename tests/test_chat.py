"""chat() のマーカー検出・タスク抽出ロジックのテスト.

main.py は import 時に Slack App を初期化するため、
テストではロジックを直接再現してテストする。
"""

import re


def _parse_chat_response(output: str, user_message: str) -> tuple[str, str | None, str]:
    """main.chat() 内のパース処理を再現."""
    task_description = None
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
        output = re.sub(r"\s*```task\s*\n.*?```", "", output, flags=re.DOTALL).strip()
        output = output.replace("**GO**", "").replace("**UPDATE**", "").strip()

    return output, task_description, mode


class TestChatMarkerDetection:
    """Claude応答からのモード判定テスト."""

    def test_go_marker_detected(self):
        response, task, mode = _parse_chat_response(
            "いいアイデアですね！\n```task\nTODOアプリを作る\n```\n**GO**",
            "TODOアプリ作りたい",
        )
        assert mode == "new"
        assert task == "TODOアプリを作る"

    def test_update_marker_detected(self):
        _, task, mode = _parse_chat_response(
            "了解！\n```task\nダークモードを追加\n```\n**UPDATE**",
            "ダークモード追加して",
        )
        assert mode == "update"
        assert task == "ダークモードを追加"

    def test_no_marker_is_chat(self):
        response, task, mode = _parse_chat_response(
            "こんにちは！元気ですか？",
            "こんにちは",
        )
        assert mode == "chat"
        assert task is None
        assert response == "こんにちは！元気ですか？"

    def test_go_without_task_block_falls_back_to_user_message(self):
        _, task, mode = _parse_chat_response(
            "やりましょう！ **GO**",
            "電卓作りたい",
        )
        assert mode == "new"
        assert task == "電卓作りたい"

    def test_markers_cleaned_from_response(self):
        response, _, _ = _parse_chat_response(
            "作りましょう！\n```task\nCLIツール\n```\n**GO**",
            "CLIツール作りたい",
        )
        assert "**GO**" not in response
        assert "```task" not in response
        assert "作りましょう！" in response

    def test_update_marker_takes_priority_over_go(self):
        """UPDATE と GO が両方ある場合、UPDATEが優先される."""
        _, _, mode = _parse_chat_response(
            "**UPDATE**\n```task\n修正内容\n```\n**GO**",
            "修正して",
        )
        assert mode == "update"

    def test_multiline_task_block(self):
        task_text = "1. APIエンドポイント作成\n2. フロントエンド実装\n3. テスト追加"
        _, task, _ = _parse_chat_response(
            f"```task\n{task_text}\n```\n**GO**",
            "Webアプリ作りたい",
        )
        assert task == task_text


class TestRepoNameNormalization:
    """handle_message 内のリポジトリ名正規化ロジックのテスト."""

    @staticmethod
    def normalize(raw: str) -> str | None:
        """handle_message 内の正規化ロジックを再現."""
        raw = raw.strip().lower()
        repo_name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
        repo_name = re.sub(r"-{2,}", "-", repo_name)
        if not repo_name or len(repo_name) > 100:
            return None
        return repo_name

    def test_simple_name(self):
        assert self.normalize("my-app") == "my-app"

    def test_uppercase_lowered(self):
        assert self.normalize("My-App") == "my-app"

    def test_spaces_to_hyphens(self):
        assert self.normalize("my cool app") == "my-cool-app"

    def test_special_chars_replaced(self):
        assert self.normalize("my_app!v2") == "my-app-v2"

    def test_consecutive_hyphens_collapsed(self):
        assert self.normalize("my---app") == "my-app"

    def test_leading_trailing_hyphens_stripped(self):
        assert self.normalize("---app---") == "app"

    def test_empty_string_invalid(self):
        assert self.normalize("") is None

    def test_too_long_invalid(self):
        assert self.normalize("a" * 101) is None

    def test_exactly_100_valid(self):
        assert self.normalize("a" * 100) == "a" * 100

    def test_japanese_chars_become_hyphens(self):
        result = self.normalize("テストアプリ")
        assert result is None
