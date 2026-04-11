"""chat() のマーカー検出・タスク抽出ロジックのテスト.

main.py は import 時に Slack App を初期化するため、
テストではロジックを直接再現してテストする。
"""

import re


def _parse_chat_response(output: str, user_message: str) -> tuple[str, str | None, str | None, str]:
    """main.chat() 内のパース処理を再現."""
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
            repo_name = re.sub(r"[^a-z0-9_-]", "-", raw).strip("-")
            repo_name = re.sub(r"-{2,}", "-", repo_name)
            if not repo_name or len(repo_name) > 100:
                repo_name = None

        output = re.sub(r"\s*```task\s*\n.*?```", "", output, flags=re.DOTALL).strip()
        output = re.sub(r"\s*```name\s*\n.*?```", "", output, flags=re.DOTALL).strip()
        output = output.replace("**GO**", "").replace("**UPDATE**", "").strip()

    return output, task_description, repo_name, mode


class TestChatMarkerDetection:
    """Claude応答からのモード判定テスト."""

    def test_go_marker_detected(self):
        response, task, name, mode = _parse_chat_response(
            "いいアイデアですね！\n```name\ntodo-app\n```\n```task\nTODOアプリを作る\n```\n**GO**",
            "TODOアプリ作りたい",
        )
        assert mode == "new"
        assert task == "TODOアプリを作る"
        assert name == "todo-app"

    def test_update_marker_detected(self):
        _, task, name, mode = _parse_chat_response(
            "了解！\n```name\nmy-app\n```\n```task\nダークモードを追加\n```\n**UPDATE**",
            "ダークモード追加して",
        )
        assert mode == "update"
        assert task == "ダークモードを追加"
        assert name == "my-app"

    def test_no_marker_is_chat(self):
        response, task, name, mode = _parse_chat_response(
            "こんにちは！元気ですか？",
            "こんにちは",
        )
        assert mode == "chat"
        assert task is None
        assert name is None
        assert response == "こんにちは！元気ですか？"

    def test_go_without_task_block_falls_back_to_user_message(self):
        _, task, _, mode = _parse_chat_response(
            "やりましょう！\n```name\ncalculator\n```\n**GO**",
            "電卓作りたい",
        )
        assert mode == "new"
        assert task == "電卓作りたい"

    def test_go_without_name_block(self):
        _, task, name, mode = _parse_chat_response(
            "作りましょう！\n```task\nCLIツール\n```\n**GO**",
            "CLIツール作りたい",
        )
        assert mode == "new"
        assert task == "CLIツール"
        assert name is None

    def test_markers_cleaned_from_response(self):
        response, _, _, _ = _parse_chat_response(
            "作りましょう！\n```name\nmy-cli\n```\n```task\nCLIツール\n```\n**GO**",
            "CLIツール作りたい",
        )
        assert "**GO**" not in response
        assert "```task" not in response
        assert "```name" not in response
        assert "作りましょう！" in response

    def test_update_marker_takes_priority_over_go(self):
        """UPDATE と GO が両方ある場合、UPDATEが優先される."""
        _, _, _, mode = _parse_chat_response(
            "**UPDATE**\n```name\nmy-app\n```\n```task\n修正内容\n```\n**GO**",
            "修正して",
        )
        assert mode == "update"

    def test_multiline_task_block(self):
        task_text = "1. APIエンドポイント作成\n2. フロントエンド実装\n3. テスト追加"
        _, task, name, _ = _parse_chat_response(
            f"```name\nweb-app\n```\n```task\n{task_text}\n```\n**GO**",
            "Webアプリ作りたい",
        )
        assert task == task_text
        assert name == "web-app"

    def test_name_normalization(self):
        """Name block with uppercase and special chars is normalized."""
        _, _, name, _ = _parse_chat_response(
            "```name\nMy Cool App!\n```\n```task\nアプリ作成\n```\n**GO**",
            "アプリ作って",
        )
        assert name == "my-cool-app"


class TestRepoNameNormalization:
    """handle_message 内のリポジトリ名正規化ロジックのテスト."""

    @staticmethod
    def normalize(raw: str) -> str | None:
        """name ブロックの正規化ロジックを再現."""
        raw = raw.strip().lower()
        repo_name = re.sub(r"[^a-z0-9_-]", "-", raw).strip("-")
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
        assert self.normalize("my_app!v2") == "my_app-v2"

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


class TestConfirmationPatterns:
    """確認フェーズのOK/キャンセル判定テスト."""

    _CONFIRM_YES = re.compile(r"^(ok|yes|はい|うん|お願い|いいよ|よろしく|やって|頼む|go|おk|おけ|いける|大丈夫|おなしゃす|y)$", re.IGNORECASE)
    _CONFIRM_NO = re.compile(r"^(no|いいえ|やめ|キャンセル|cancel|stop|やっぱ|やだ|ストップ|なし|n)$", re.IGNORECASE)

    def test_yes_patterns(self):
        for word in ["OK", "ok", "はい", "うん", "お願い", "いいよ", "よろしく", "やって", "go", "Go", "おk", "y"]:
            assert self._CONFIRM_YES.match(word), f"'{word}' should match YES"

    def test_no_patterns(self):
        for word in ["no", "いいえ", "やめ", "キャンセル", "cancel", "やっぱ", "n"]:
            assert self._CONFIRM_NO.match(word), f"'{word}' should match NO"

    def test_unrecognized_input(self):
        for word in ["todo-appでお願いします", "ちょっと待って", "何それ"]:
            assert not self._CONFIRM_YES.match(word)
            assert not self._CONFIRM_NO.match(word)
