"""Deterministic multi-agent failure replays for relay delivery semantics."""

import json
import threading
from datetime import datetime, timedelta, timezone

import relay


def _config(tmp_path, *names):
    return {
        "box_dir": str(tmp_path),
        "agents": [
            {"name": name, "tmux_session": name, "busy_regex": "BUSY", "input_prefix": "> "}
            for name in names
        ],
    }


def _old_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def test_replay_concurrent_sweeps_lease_one_oldest_message(tmp_path, monkeypatch):
    """Two watchdog invocations cannot nudge the same pending record twice."""
    cfg = _config(tmp_path, "receiver")
    relay.append_message(tmp_path, "receiver", "sender", "please inspect")
    calls = []
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: calls.append(seq) or "delivered")
    start = threading.Barrier(3)
    results = []

    def sweep():
        start.wait()
        results.append(relay.redeliver_unread(cfg, now=_old_now(), base_backoff_s=60))

    threads = [threading.Thread(target=sweep), threading.Thread(target=sweep)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert calls == [1]
    assert sorted(status for batch in results for _, _, status in batch) == [
        "pending_backoff", "renudged_delivered",
    ]


def test_replay_busy_edit_defers_then_delivers_after_recovery(tmp_path, monkeypatch):
    """A mid-edit recipient is never typed into; its old mail remains retriable."""
    cfg = _config(tmp_path, "receiver")
    relay.append_message(tmp_path, "receiver", "sender", "do not interrupt edit")
    monkeypatch.setattr(
        relay, "check_safe_to_send",
        lambda agent, body: (_ for _ in ()).throw(relay.RelaySendRefused("busy")),
    )
    monkeypatch.setattr(relay, "nudge", lambda *args: (_ for _ in ()).throw(AssertionError("typed")))
    now = _old_now()
    assert relay.redeliver_unread(cfg, now=now, base_backoff_s=60) == [
        ("receiver", 1, "renudged_unsafe_busy_or_input"),
    ]

    delivered = []
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: delivered.append(seq) or "delivered")
    assert relay.redeliver_unread(cfg, now=now + timedelta(seconds=61), base_backoff_s=60) == [
        ("receiver", 1, "renudged_delivered"),
    ]
    assert delivered == [1]


def test_replay_session_dies_after_safe_check_then_recovers(tmp_path, monkeypatch):
    """A session loss is paused longer, then redelivered without losing mail."""
    cfg = _config(tmp_path, "receiver")
    relay.append_message(tmp_path, "receiver", "sender", "session lifecycle")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda *args: "session_absent")
    now = _old_now()
    assert relay.redeliver_unread(cfg, now=now, base_backoff_s=60) == [
        ("receiver", 1, "renudged_session_absent"),
    ]
    monkeypatch.setattr(relay, "nudge", lambda *args: "delivered")
    assert relay.redeliver_unread(cfg, now=now + timedelta(seconds=239), base_backoff_s=60) == [
        ("receiver", 1, "pending_backoff"),
    ]
    assert relay.redeliver_unread(cfg, now=now + timedelta(seconds=241), base_backoff_s=60) == [
        ("receiver", 1, "renudged_delivered"),
    ]


def test_replay_three_agent_conversation_survives_corrupt_record(tmp_path):
    """One truncated JSONL line cannot suppress later conversation turns."""
    for recipient, sender, body in (
        ("beta", "alpha", "request"),
        ("gamma", "beta", "analysis"),
        ("alpha", "gamma", "result"),
    ):
        relay.append_message(tmp_path, recipient, sender, body)
    with relay.mailbox_path(tmp_path, "beta").open("a") as mailbox:
        mailbox.write('{"seq": truncated\n')
    relay.append_message(tmp_path, "beta", "gamma", "follow-up")

    assert [record["body"] for record in relay.read_messages(tmp_path, "alpha")] == ["result"]
    assert [record["body"] for record in relay.read_messages(tmp_path, "beta")] == ["request", "follow-up"]
    assert [record["body"] for record in relay.read_messages(tmp_path, "gamma")] == ["analysis"]


def test_replay_clock_skew_never_turns_future_mail_immediately_due(tmp_path, monkeypatch):
    """A future sender timestamp is clamped pending until its deterministic age is due."""
    cfg = _config(tmp_path, "receiver")
    relay.append_message(tmp_path, "receiver", "sender", "clock-skewed request")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    path = relay.mailbox_path(tmp_path, "receiver")
    record = json.loads(path.read_text())
    record["ts_utc"] = future.isoformat()
    path.write_text(json.dumps(record) + "\n")
    sent = []
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: sent.append(seq) or "delivered")

    assert relay.redeliver_unread(cfg, now=future - timedelta(seconds=1)) == [
        ("receiver", 1, "pending_age"),
    ]
    assert relay.redeliver_unread(cfg, now=future + timedelta(minutes=16)) == [
        ("receiver", 1, "renudged_delivered"),
    ]
    assert sent == [1]
