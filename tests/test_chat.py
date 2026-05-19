"""会話プロンプト構築ロジックのテスト."""


class TestConversationPromptBuilder:
    """会話履歴からプロンプトを構築するテスト."""

    @staticmethod
    def _build(messages, bot_user_id=None):
        lines = ["=== 会話履歴 ==="]
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

    def test_user_and_bot_messages(self):
        messages = [
            {"user": "U123", "text": "TODOアプリ作って"},
            {"user": "BXYZ", "bot_id": "B001", "text": "いいですね！"},
            {"user": "U123", "text": "React で"},
        ]
        result = self._build(messages)
        assert "ユーザー: TODOアプリ作って" in result
        assert "あなた: いいですね！" in result
        assert "ユーザー: React で" in result

    def test_subtypes_skipped(self):
        messages = [
            {"user": "U123", "text": "hello"},
            {"user": "U123", "text": "edited", "subtype": "message_changed"},
        ]
        result = self._build(messages)
        assert "hello" in result
        assert "edited" not in result
