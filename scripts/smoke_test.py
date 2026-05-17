from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from astrbot_plugin_custom_persona.core.compression import (
    CompressionHandler,
)
from astrbot_plugin_custom_persona.core.history import render_chat_history_text
from astrbot_plugin_custom_persona.core.models import PersonaConfig
from astrbot_plugin_custom_persona.core.persona_store import PersonaStore
from astrbot_plugin_custom_persona.core.renderer import PreambleRenderer
from astrbot_plugin_custom_persona.core.state import SessionStateManager


def test_persona_render() -> None:
    persona = PersonaConfig.from_dict(
        {
            "name": "demo",
            "activation": {"global_default": True},
            "segments": [
                {"id": "s", "role": "SYSTEM", "depth": 0, "template": "sys {{ x }}"},
                {
                    "id": "u",
                    "role": "USER",
                    "depth": 1,
                    "condition": "!streaming",
                    "template": "history {{ chat_history }}",
                },
            ],
        }
    )
    rendered = PreambleRenderer().render(
        persona, {"x": "ok", "chat_history": "h", "streaming": False}
    )
    assert rendered.system_prompt == "sys ok"
    assert rendered.contexts[0]["_no_save"] is True


def test_history() -> None:
    persona = PersonaConfig.from_dict({"name": "demo"})
    text, from_preset = render_chat_history_text(
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "content": "tool"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "orphan"},
        ],
        persona.chat_history,
        use_preset_if_empty=True,
    )
    assert "u1" in text
    assert "orphan" not in text
    assert "tool" not in text
    assert from_preset is False


async def test_state() -> None:
    states = SessionStateManager()
    persona = PersonaConfig.from_dict({"name": "demo"})
    window = persona.dialogue_window
    window.max_messages = 3
    window.keep_messages = 2

    # Set up persona first (as main.py does in on_llm_request).
    await states.reset_if_persona_changed("s", "demo")

    slid = await states.append_l2(
        "s",
        [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "answer"},
        ],
        window,
    )
    assert slid is True
    stored = await states.contexts_for_request("s")
    assert stored[0]["role"] == "user"

    slid = await states.append_l2(
        "s",
        [{"role": "user", "content": "kept"}, {"role": "assistant", "content": "ok"}],
        window,
    )
    # With keep_messages=2 max_messages=3, appending 2 more (total 4) should slide
    stored = await states.contexts_for_request("s")
    assert stored[0]["content"] == "kept"

    # Test explicit dialogue_window
    explicit = PersonaConfig.from_dict(
        {
            "name": "explicit",
            "dialogue_window": {"max_messages": 100, "keep_messages": 60},
        }
    )
    implicit = PersonaConfig.from_dict({"name": "implicit"})
    assert explicit.dialogue_window.explicit is True
    assert implicit.dialogue_window.explicit is False

    # Test persona change reset
    await states.reset_if_persona_changed("s", "other")
    stored = await states.contexts_for_request("s")
    assert len(stored) == 0


def test_ledger_soft_delete() -> None:
    from astrbot_plugin_custom_persona.core.ledger import ConversationLedger

    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.sqlite3"
        ledger = ConversationLedger(db)
        sid = "test:session:1"
        ledger.record(session_id=sid, role="user", content="hello")
        ledger.record(session_id=sid, role="assistant", content="hi there")
        assert len(ledger.recent(sid, limit=10)) == 2

        count = ledger.soft_delete_session(sid)
        assert count == 2
        assert len(ledger.recent(sid, limit=10)) == 0
        ledger.close()


def test_store_resolve() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "p.yaml").write_text(
            "name: p\nactivation:\n  global_default: true\n", encoding="utf-8"
        )
        store = PersonaStore(root)
        assert store.resolve("any").name == "p"
        renamed = store.rename("p", "q")
        assert renamed.name == "q"
        assert not (root / "p.yaml").exists()
        assert (root / "q.yaml").exists()
        store.delete("q")
        assert not (root / "q.yaml").exists()


def test_compression_estimate() -> None:
    tokens = CompressionHandler.estimate_tokens([{"role": "user", "content": "hello world " * 40}])
    assert tokens > 0
    assert CompressionHandler.should_compress(tokens, 100) is True
    assert CompressionHandler.should_compress(tokens, 100000) is False


def test_split_preamble() -> None:
    preamble, body = CompressionHandler.split_preamble(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "pre1", "_no_save": True},
            {"role": "assistant", "content": "pre2", "_no_save": True},
            {"role": "user", "content": "real1"},
            {"role": "assistant", "content": "real2"},
        ]
    )
    assert len(preamble) == 3
    assert len(body) == 2
    assert body[0]["content"] == "real1"


def test_strip_tools() -> None:
    cleaned = CompressionHandler.strip_tool_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert len(cleaned) == 2
    assert cleaned[0]["role"] == "user"
    assert cleaned[1]["role"] == "assistant"


if __name__ == "__main__":
    test_persona_render()
    test_history()
    asyncio.run(test_state())
    test_store_resolve()
    test_ledger_soft_delete()
    test_compression_estimate()
    test_split_preamble()
    test_strip_tools()
    print("smoke ok")
