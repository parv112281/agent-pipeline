"""Budget (spec section 11) and the notification outbox."""

import json
from datetime import datetime


def test_dollar_spend_never_warns_or_pauses(env):
    env.spend(9999.0)
    for _ in range(3):
        assert env.fresh() == []
    assert env.board.paused is False
    assert env.ok("budget") == "Dollar budget enforcement is disabled for headless subscription agents. The board is running."


def test_legacy_budget_pause_is_released_without_waiting_for_month(env):
    env.board.paused = True
    engine = env.engine()
    engine.state["budget"] = {"paused_by_engine": True, "exhausted_month": "2026-10"}
    engine.flush()
    assert env.fresh() == []
    assert env.board.paused is False
    assert env.read("tasks.json")["budget"]["paused_by_engine"] is False


def test_manual_pause_is_preserved_even_with_large_spend(env):
    env.spend(9999.0)
    env.ok("pause")
    env.now = datetime(2026, 11, 2, 9, 0).timestamp()
    assert env.fresh() == []
    assert env.board.paused is True
    assert "board is paused" in env.ok("budget")


def test_disabled_budget_does_not_read_history(env):
    engine = env.engine()
    def unexpected(*_args):
        raise AssertionError("history should not be read for dollar enforcement")
    engine.store.read_history = unexpected
    assert "disabled" in engine.cmd_budget()
    engine.clear_legacy_budget_pause()


def test_outbox_cap_ack_and_size(env):
    engine = env.engine()
    for i in range(8):
        engine.notify(f"message {i} " + "x" * 2000)
    engine.flush()
    code, out, _ = env.run("tick")
    assert len(out) < 6000
    notes = json.loads(out)["notifications"]
    assert [n["text"][:9] for n in notes] == [f"message {i}" for i in range(5)]
    assert all(len(n["text"]) <= 900 for n in notes)
    assert all(n["id"].startswith("n-") for n in notes)
    # nothing acknowledged: the same five come back, oldest first
    assert env.tick() == notes
    assert env.ok("ack", notes[0]["id"], notes[1]["id"], "n-999") == "ok"
    assert [n["text"][:9] for n in env.tick()] == [f"message {i}" for i in range(2, 7)]
    assert len(env.read("outbox.json")["items"]) == 6
    assert env.run("ack")[0] == 1


def test_ack_keeps_a_sent_history(env):
    engine = env.engine()
    for i in range(25):
        engine.notify(f"message {i}")
    engine.flush()
    for _ in range(5):
        env.ok("ack", *[n["id"] for n in env.tick()])
    sent = env.read("outbox.json")["sent"]
    assert [n["text"] for n in sent] == [f"message {i}" for i in range(5, 25)]
    assert all(n["at"] for n in sent)


def test_notify_queues_a_notification(env):
    note_id = env.ok("notify", "Fix", "a1b2c3", "finished")
    assert note_id.startswith("n-")
    assert env.tick() == [{"id": note_id, "text": "Fix a1b2c3 finished"}]
    assert env.run("notify")[0] == 1


def test_config_defaults_are_written(tmp_path):
    from pipeline.store import Store
    store = Store(tmp_path / "fresh")
    config = store.load_config()
    assert config["board_url"] == "http://127.0.0.1:3738" and config["max_reworks"] == 3
    saved = json.loads((tmp_path / "fresh" / "state" / "config.json").read_text())
    assert saved == config
    assert "budget_usd" not in saved and "budget_warn_fraction" not in saved
    assert saved["github_owner"] == "parv112281" and saved["ideas_repo"] == "ideas"


def test_lock_keeps_a_second_command_out(tmp_path):
    import pytest
    from pipeline.errors import PipelineError
    from pipeline.store import Store
    first, second = Store(tmp_path), Store(tmp_path)
    with first.lock():
        with pytest.raises(PipelineError):
            with second.lock(timeout=0.3):
                pass
    with second.lock(timeout=0.3):
        pass
