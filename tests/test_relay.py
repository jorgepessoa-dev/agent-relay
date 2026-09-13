import json
import re
import stat
import threading
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import relay


def test_load_config_json(tmp_path, monkeypatch):
    cfg = {
        "box_dir": "./mail",
        "agents": [
            {"name": "a", "tmux_session": "sess-a", "busy_regex": "BUSY", "input_prefix": "> "},
            {"name": "b", "tmux_session": "sess-b", "busy_regex": "BUSY", "input_prefix": "> "},
        ],
    }
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    loaded = relay.load_config()
    assert loaded["box_dir"] == "./mail"
    assert len(loaded["agents"]) == 2


def test_load_config_missing_file_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(relay.RelayConfigError, match="relay.yaml"):
        relay.load_config()


def test_get_agent_missing_required_field_raises(tmp_path, monkeypatch):
    cfg = {"box_dir": "./mail", "agents": [{"name": "a", "tmux_session": "sess-a"}]}
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    loaded = relay.load_config()
    with pytest.raises(relay.RelayConfigError, match="busy_regex"):
        relay.get_agent(loaded, "a")


def test_get_agent_unknown_name_raises(tmp_path, monkeypatch):
    cfg = {
        "box_dir": "./mail",
        "agents": [{"name": "a", "tmux_session": "s", "busy_regex": "B", "input_prefix": "> "}],
    }
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    loaded = relay.load_config()
    with pytest.raises(relay.RelayConfigError, match="'z'"):
        relay.get_agent(loaded, "z")


def test_resolve_box_dir_relative_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    box = relay.resolve_box_dir({"box_dir": "./mail"})
    assert box == (tmp_path / "mail").resolve()


def test_resolve_box_dir_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    box = relay.resolve_box_dir({})
    assert box == (tmp_path / "agent-relay-mail").resolve()


def test_redeliver_nudges_only_oldest_unread_after_age_threshold(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}
    relay.append_message(tmp_path, "a", "x", "one")
    relay.append_message(tmp_path, "a", "x", "two")
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: "delivered")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    assert relay.redeliver_unread(cfg, now=now) == [("a", 1, "renudged_delivered")]


def test_redeliver_persists_backoff_and_cursor_clears_it(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}
    relay.append_message(tmp_path, "a", "x", "one")
    calls = []
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: calls.append(seq) or "delivered")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    assert relay.redeliver_unread(cfg, now=now, base_backoff_s=60) == [("a", 1, "renudged_delivered")]
    assert relay.redeliver_unread(cfg, now=now + timedelta(seconds=30), base_backoff_s=60) == [("a", 1, "pending_backoff")]
    relay.read_messages(tmp_path, "a", advance=True)
    assert json.loads(relay.redelivery_state_path(tmp_path).read_text())["entries"] == {}
    assert calls == [1]


def test_redeliver_never_types_into_busy_agent_and_pauses_absent_session(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}
    relay.append_message(tmp_path, "a", "x", "one")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: (_ for _ in ()).throw(relay.RelaySendRefused()))
    monkeypatch.setattr(relay, "nudge", lambda *args: (_ for _ in ()).throw(AssertionError("must not type")))
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    assert relay.redeliver_unread(cfg, now=now) == [("a", 1, "renudged_unsafe_busy_or_input")]


def test_redeliver_skips_malformed_line_and_delivers_valid_mail(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}
    relay.append_message(tmp_path, "a", "x", "one")
    with relay.mailbox_path(tmp_path, "a").open("a") as fh:
        fh.write("not json\n")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda *args: "delivered")
    assert relay.redeliver_unread(cfg, now=datetime.now(timezone.utc) + timedelta(hours=1)) == [("a", 1, "renudged_delivered")]


def test_redeliver_missing_timestamp_falls_back_to_sequence_order(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}
    relay.append_message(tmp_path, "a", "x", "one")
    record = json.loads(relay.mailbox_path(tmp_path, "a").read_text())
    del record["ts_utc"]
    relay.mailbox_path(tmp_path, "a").write_text(json.dumps(record) + "\n")
    relay.append_message(tmp_path, "a", "x", "two")
    sent = []
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: sent.append(seq) or "delivered")
    assert relay.redeliver_unread(cfg, now=datetime.now(timezone.utc)) == [("a", 1, "renudged_delivered")]
    assert sent == [1]


def test_redeliver_caps_sweep_and_marks_hard_cap_for_escalation(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": name, "tmux_session": name, "busy_regex": "B", "input_prefix": "> "}
        for name in ("a", "b", "c")
    ]}
    for name in ("a", "b", "c"):
        relay.append_message(tmp_path, name, "x", "one")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda *args: "delivered")
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    results = relay.redeliver_unread(cfg, now=now, max_renudges=2)
    assert results == [("a", 1, "renudged_delivered"), ("b", 1, "renudged_delivered"), ("c", 1, "sweep_cap")]

    state = {"version": 1, "entries": {"a:1": {"attempts": 6, "next_due_at": 0}}}
    relay._write_redelivery_state_atomic(tmp_path, state)
    assert relay.redeliver_unread(cfg, now=now, max_renudges=0)[0] == ("a", 1, "failed_cap")
    entry = json.loads(relay.redelivery_state_path(tmp_path).read_text())["entries"]["a:1"]
    assert entry["last_status"] == "failed_cap"
    assert "escalated_at" in entry


def test_redeliver_session_absent_uses_longer_pause(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}
    relay.append_message(tmp_path, "a", "x", "one")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    monkeypatch.setattr(relay, "nudge", lambda *args: "session_absent")
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    assert relay.redeliver_unread(cfg, now=now, base_backoff_s=60) == [("a", 1, "renudged_session_absent")]
    entry = json.loads(relay.redelivery_state_path(tmp_path).read_text())["entries"]["a:1"]
    assert entry["next_due_at"] == now.timestamp() + 240


def test_redeliver_skips_noninteractive_scheduler(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "scheduler", "mail_consumer": "none", "tmux_session": None, "busy_regex": "NEVER_MATCHES", "input_prefix": ""},
    ]}
    relay.append_message(tmp_path, "scheduler", "x", "one")
    monkeypatch.setattr(relay, "nudge", lambda *args: (_ for _ in ()).throw(AssertionError("must not nudge")))
    assert relay.redeliver_unread(cfg, now=datetime.now(timezone.utc) + timedelta(hours=1)) == []


def test_cli_send_to_api_consumer_persists_without_tmux_nudge(tmp_path, monkeypatch):
    cfg = {"box_dir": "./mail", "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "X", "input_prefix": "> "},
        {"name": "api", "mail_consumer": "api", "tmux_session": None, "busy_regex": "NEVER_MATCHES", "input_prefix": ""},
    ]}
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    assert relay.main(["send", "--from", "a", "--to", "api", "--body", "job"]) == 0
    assert relay.read_messages(relay.resolve_box_dir(cfg), "api", advance=False)[0]["body"] == "job"


@pytest.mark.parametrize("body", ("", " \t\n "))
def test_cli_send_rejects_blank_body_without_persisting(tmp_path, monkeypatch, capsys, body):
    """R-FIRE F-987: unguarded send must not report SENT for a null message."""
    cfg = {"box_dir": "./mail", "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "X", "input_prefix": "> "},
        {"name": "api", "mail_consumer": "api", "tmux_session": None,
         "busy_regex": "NEVER_MATCHES", "input_prefix": ""},
    ]}
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    assert relay.main(["send", "--from", "a", "--to", "api", "--body", body]) == 1
    captured = capsys.readouterr()
    assert "empty or whitespace-only body" in captured.err
    assert "SENT" not in captured.out
    assert not (tmp_path / "mail").exists()


def test_safe_send_rejects_blank_body_before_recipient_probe(tmp_path, monkeypatch, capsys):
    """The common boundary refuses blank input before guarded tmux inspection."""
    cfg = {"box_dir": "./mail", "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "X", "input_prefix": "> "},
        {"name": "api", "mail_consumer": "api", "tmux_session": None,
         "busy_regex": "NEVER_MATCHES", "input_prefix": ""},
    ]}
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(relay, "check_safe_to_send",
                        lambda *_: (_ for _ in ()).throw(AssertionError("probed")))
    assert relay.main(["safe-send", "--from", "a", "--to", "api", "--body", " "]) == 1
    assert "empty or whitespace-only body" in capsys.readouterr().err
    assert not (tmp_path / "mail").exists()


def test_cli_redeliver_returns_nonzero_on_retry_cap(tmp_path, monkeypatch):
    cfg = {"box_dir": str(tmp_path / "mail"), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    relay.append_message(relay.resolve_box_dir(cfg), "a", "x", "one")
    record = json.loads(relay.mailbox_path(relay.resolve_box_dir(cfg), "a").read_text())
    record["ts_utc"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    relay.mailbox_path(relay.resolve_box_dir(cfg), "a").write_text(json.dumps(record) + "\n")
    relay._write_redelivery_state_atomic(relay.resolve_box_dir(cfg), {
        "version": 1, "entries": {"a:1": {"attempts": relay.REDELIVERY_MAX_ATTEMPTS}},
    })
    monkeypatch.chdir(tmp_path)
    assert relay.main(["redeliver", "--due"]) == 2


def test_head_of_none_repo_path():
    assert relay.head_of(None) == "unknown"


def test_head_of_invalid_repo_path(tmp_path):
    assert relay.head_of(str(tmp_path)) == "unknown"


def test_head_of_valid_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    head = relay.head_of(str(tmp_path))
    assert head != "unknown"
    assert len(head) >= 7


def test_append_message_returns_seq_starting_at_1(tmp_path):
    box = tmp_path / "mail"
    seq1 = relay.append_message(box, "b", "a", "primeira")
    seq2 = relay.append_message(box, "b", "a", "segunda")
    assert seq1 == 1
    assert seq2 == 2


@pytest.mark.parametrize("body", ("", " \t\n "))
def test_append_message_rejects_blank_body_without_creating_box(tmp_path, body):
    """Direct writers cannot bypass the F-987 common persistence boundary."""
    box = tmp_path / "mail"
    with pytest.raises(relay.RelaySendRefused, match="empty or whitespace-only body"):
        relay.append_message(box, "b", "a", body)
    assert not box.exists()


def test_append_message_preserves_nonblank_body_exactly(tmp_path):
    """Validation uses strip only to decide validity; it never rewrites content."""
    box = tmp_path / "mail"
    relay.append_message(box, "b", "a", "  hello\n")
    assert relay.read_messages(box, "b", advance=False)[0]["body"] == "  hello\n"


def test_append_message_creates_file_with_0600(tmp_path):
    box = tmp_path / "mail"
    relay.append_message(box, "b", "a", "oi")
    path = relay.mailbox_path(box, "b")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_append_message_record_shape(tmp_path):
    box = tmp_path / "mail"
    relay.append_message(box, "b", "a", "corpo", tokens=42)
    path = relay.mailbox_path(box, "b")
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["seq"] == 1
    assert rec["from"] == "a"
    assert rec["body"] == "corpo"
    assert rec["tokens"] == 42
    assert "ts_utc" in rec
    assert rec["head"] == "unknown"  # sem sender_repo_path


def test_concurrent_appends_produce_ascending_unique_seq(tmp_path):
    box = tmp_path / "mail"
    errors = []

    def worker(i):
        try:
            relay.append_message(box, "b", "a", f"msg-{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    path = relay.mailbox_path(box, "b")
    lines = path.read_text().splitlines()
    seqs = sorted(json.loads(l)["seq"] for l in lines)
    assert seqs == list(range(1, 21))


def test_read_messages_returns_unread_and_advances_cursor(tmp_path):
    box = tmp_path / "mail"
    relay.append_message(box, "b", "a", "um")
    relay.append_message(box, "b", "a", "dois")

    unread = relay.read_messages(box, "b", advance=True)
    assert [r["body"] for r in unread] == ["um", "dois"]
    assert relay.read_cursor(box, "b") == 2

    unread_again = relay.read_messages(box, "b", advance=True)
    assert unread_again == []


def test_peek_does_not_advance_cursor(tmp_path):
    box = tmp_path / "mail"
    relay.append_message(box, "b", "a", "um")

    peeked = relay.read_messages(box, "b", advance=False)
    assert len(peeked) == 1
    assert relay.read_cursor(box, "b") == 0

    peeked_again = relay.read_messages(box, "b", advance=False)
    assert len(peeked_again) == 1


def test_read_messages_no_mailbox_returns_empty(tmp_path):
    box = tmp_path / "mail"
    assert relay.read_messages(box, "nobody", advance=True) == []


def test_write_cursor_atomic_uses_tmp_then_rename(tmp_path):
    box = tmp_path / "mail"
    box.mkdir()
    relay.write_cursor_atomic(box, "b", 5)
    assert relay.read_cursor(box, "b") == 5
    assert not (box / ".cursor_b.tmp").exists()


def _run(returncode=0, stdout=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


def test_tmux_has_session_true(monkeypatch):
    with patch("subprocess.run", return_value=_run(returncode=0)) as mock_run:
        assert relay.tmux_has_session("sess") is True
        assert mock_run.call_args[0][0] == ["tmux", "has-session", "-t", "sess"]


def test_tmux_has_session_false(monkeypatch):
    with patch("subprocess.run", return_value=_run(returncode=1)):
        assert relay.tmux_has_session("sess") is False


def test_tmux_capture_pane_success(monkeypatch):
    with patch("subprocess.run", return_value=_run(returncode=0, stdout="hello\n")):
        ok, text = relay.tmux_capture_pane("sess")
        assert ok is True
        assert text == "hello\n"


def test_tmux_capture_pane_failure(monkeypatch):
    with patch("subprocess.run", return_value=_run(returncode=1, stdout="")):
        ok, text = relay.tmux_capture_pane("sess")
        assert ok is False


AGENT = {
    "name": "dc",
    "tmux_session": "dc",
    "busy_regex": r"status: (processing|pending)|esc to interrupt",
    "input_prefix": "> ",
}


def test_nudge_reports_session_absent_never_delivered(monkeypatch):
    # Regression test: this is the exact bug DeepCode caught live (seq=156) —
    # the original nudge printed "delivered" when the tmux session did not exist.
    with patch("relay.tmux_has_session", return_value=False):
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "session_absent"
    assert status != "delivered"


def test_nudge_reports_capture_failed(monkeypatch):
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", return_value=(False, "")):
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "capture_failed"


def test_nudge_defers_without_typing_when_approval_dialog_visible(monkeypatch):
    monkeypatch.setattr(relay, "tmux_has_session", lambda session: True)
    monkeypatch.setattr(
        relay, "tmux_capture_pane",
        lambda session: (True, "Would you like to run the following command?\nPress enter to confirm or esc to cancel"),
    )
    monkeypatch.setattr(
        relay.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not type into approval dialog")),
    )
    assert relay.nudge(AGENT, 1, "corpo") == "awaiting_approval"


def test_nudge_reports_delivered_when_input_box_clear(monkeypatch):
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", return_value=(True, "algum texto\n> \n")):
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "delivered"


def test_nudge_reports_stuck_when_mail_marker_still_in_box(monkeypatch):
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", return_value=(True, "> [MAIL seq=1] corpo...\n")):
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "stuck"


def test_nudge_does_not_report_stuck_for_marker_only_in_scrollback(monkeypatch):
    # Regression test: checking the last N lines of the pane (instead of just
    # the current input line) produced false positives in live testing —
    # the mail marker is expected to still be visible in scrollback right
    # after a successful send, that alone doesn't mean it's stuck in the box.
    pane = "> [MAIL seq=1] corpo...\n" + "some reply text\n" * 5 + "> \n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "delivered"


def test_nudge_retries_and_clears_on_second_enter(monkeypatch):
    # Real cause observed live (5 occurrences, 2026-08-29 session): a
    # half-typed prior message sitting in the input box; a second Enter
    # reliably clears it. The retry must not sleep for real in the test.
    monkeypatch.setattr("relay.time.sleep", lambda s: None)
    stuck_pane = (True, "> [MAIL seq=1] corpo...\n")
    clear_pane = (True, "> [MAIL seq=1] corpo...\n> \n")
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", side_effect=[stuck_pane, clear_pane]):
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "delivered"


def test_nudge_reports_stuck_only_after_exhausting_both_retries(monkeypatch):
    # Bounded, not silent: a persistent failure must still surface as
    # "stuck" — the retry must never mask a genuine, non-transient failure.
    monkeypatch.setattr("relay.time.sleep", lambda s: None)
    stuck_pane = (True, "> [MAIL seq=1] corpo...\n")
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)) as mock_run, \
         patch("relay.tmux_capture_pane", return_value=stuck_pane) as mock_capture:
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "stuck"
    # 1 initial Enter + 2 retries = 3 Enter keypresses total (plus the 1
    # message-typing send-keys call = 4 subprocess.run calls).
    enter_calls = [c for c in mock_run.call_args_list if c[0][0][-1] == "Enter"]
    assert len(enter_calls) == 3
    # One pre-send safety capture plus one after each Enter.
    assert mock_capture.call_count == 4


def test_nudge_capture_failed_during_retry_short_circuits(monkeypatch):
    monkeypatch.setattr("relay.time.sleep", lambda s: None)
    stuck_pane = (True, "> [MAIL seq=1] corpo...\n")
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", side_effect=[stuck_pane, (False, "")]):
        status = relay.nudge(AGENT, 1, "corpo")
    assert status == "capture_failed"


def test_nudge_injects_real_recipient_name_not_placeholder(monkeypatch):
    # Regression test: nudge() used to inject a literal "<you>" placeholder
    # instead of the recipient's actual agent name, causing recipients to
    # guess wrong (Codex tried "read --as root" before finding the real name).
    captured = {}

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "send-keys"] and cmd[-1].startswith("[MAIL"):
            captured["msg"] = cmd[-1]
        return _run(returncode=0)

    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("relay.tmux_capture_pane", return_value=(True, "> \n")):
        relay.nudge(AGENT, 1, "corpo")

    assert "<you>" not in captured["msg"]
    assert f"read --as {AGENT['name']}" in captured["msg"]


def test_check_safe_to_send_refuses_when_session_absent(monkeypatch):
    with patch("relay.tmux_has_session", return_value=False):
        with pytest.raises(relay.RelaySendRefused, match="sessão"):
            relay.check_safe_to_send(AGENT, "oi")


def test_check_safe_to_send_refuses_when_input_box_has_text(monkeypatch):
    pane = "algum log\n> mensagem por enviar\n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        with pytest.raises(relay.RelaySendRefused, match="caixa de input"):
            relay.check_safe_to_send(AGENT, "oi")


def test_check_safe_to_send_allows_placeholder_in_input_box(monkeypatch):
    pane = "algum log\n> Type your message...\n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        relay.check_safe_to_send(AGENT, "oi")  # não deve levantar


def test_check_safe_to_send_refuses_when_busy(monkeypatch):
    pane = "> Type your message...\nstatus: processing · tokens: 100\n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        with pytest.raises(relay.RelaySendRefused, match="meio de turno"):
            relay.check_safe_to_send(AGENT, "oi")


def test_check_safe_to_send_refuses_when_body_too_long(monkeypatch):
    pane = "> Type your message...\n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        with pytest.raises(relay.RelaySendRefused, match="1600"):
            relay.check_safe_to_send(AGENT, "x" * 1601)


def test_check_safe_to_send_ok_when_idle_and_clear(monkeypatch):
    pane = "> Type your message...\n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        relay.check_safe_to_send(AGENT, "mensagem curta")  # não deve levantar


def test_doctor_check_session_absent():
    with patch("relay.tmux_has_session", return_value=False):
        result = relay.doctor_check(AGENT)
    assert result["session_ok"] is False


def test_doctor_check_reports_pane_and_matches():
    pane = "> Type your message...\nstatus: pending\n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        result = relay.doctor_check(AGENT)
    assert result["session_ok"] is True
    assert result["pane_tail"] == pane
    assert result["busy_regex_matched"] is True
    assert result["input_prefix_found"] is True


def test_doctor_check_no_input_prefix_found():
    pane = "sem prefixo aqui\n"
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        result = relay.doctor_check(AGENT)
    assert result["input_prefix_found"] is False


def test_doctor_marks_approval_when_only_start_marker_survives_near_tail():
    """F-980: a real dialog without its footer must not look idle/healthy."""
    pane = (
        "Would you like to run the following command?\n"
        "python3 relay.py send --body long-payload\n"
        "argument 1\n"
        "argument 2\n"
        "argument 3\n"
        "argument 4\n"
        "argument 5\n"
        "argument 6\n"
        "argument 7\n"
        "argument 8\n"
        "> Type your message...\n"
    )
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        result = relay.doctor_check(AGENT)
    assert result["status"] == relay.STATUS_APPROVAL


def test_doctor_ignores_approval_marker_outside_interaction_window():
    """A quoted old dialog must not make an otherwise idle pane look blocked."""
    pane = (
        "mail quote: Would you like to run the following command?\n"
        + "old scrollback\n" * 12
        + "> Type your message...\n"
    )
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        result = relay.doctor_check(AGENT)
    assert result["status"] == relay.STATUS_IDLE


def test_doctor_marks_quota_when_the_message_wraps_across_lines():
    """Measured 2026-09-12 on the real codex pane: the usage-limit text WRAPS.

    "...purchase more credits or try" / "again at 1:09 PM." straddles a newline, so the
    marker "try again at" never matched the raw "\\n"-joined tail and a quota-blocked
    agent was classified IDLE - the watchdog then ASKED it for a priority instead of
    escalating the block. Session/prompt state is not the ability to respond.
    """
    pane = (
        "previous turn\n"
        "\u25a0 You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro),\n"
        "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try\n"
        "again at 1:09 PM.\n"
        "\u203a Ask Codex to do anything\n"
        "gpt-5.6-terra medium \u00b7 /opt/tradingadvisor \u00b7 Executar boot e verificar \u00b7 Main\u2026\n"
    )
    with patch("relay.tmux_has_session", return_value=True), \
         patch("relay.tmux_capture_pane", return_value=(True, pane)):
        result = relay.doctor_check(AGENT)
    assert result["status"] == relay.STATUS_QUOTA


def test_cli_send_then_read_roundtrip(tmp_path, monkeypatch):
    cfg = {
        "box_dir": "./mail",
        "agents": [
            {"name": "a", "tmux_session": "sess-a", "busy_regex": "X", "input_prefix": "> "},
            {"name": "b", "tmux_session": "sess-b", "busy_regex": "X", "input_prefix": "> "},
        ],
    }
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)

    with patch("relay.tmux_has_session", return_value=False):
        # sessão "b" ausente -> nudge falha, mas a mensagem TEM de ficar na mailbox
        code = relay.main(["send", "--from", "a", "--to", "b", "--body", "oi"])
    assert code == 0

    unread = relay.read_messages(relay.resolve_box_dir(cfg), "b", advance=True)
    assert len(unread) == 1
    assert unread[0]["body"] == "oi"
    assert unread[0]["from"] == "a"


def test_cli_send_require_delivery_fails_but_keeps_durable_mailbox(tmp_path, monkeypatch):
    cfg = {
        "box_dir": "./mail",
        "agents": [
            {"name": "a", "tmux_session": "sess-a", "busy_regex": "X", "input_prefix": "> "},
            {"name": "b", "tmux_session": "sess-b", "busy_regex": "X", "input_prefix": "> "},
        ],
    }
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)

    with patch("relay.tmux_has_session", return_value=False):
        assert relay.main([
            "send", "--from", "a", "--to", "b", "--body", "oi", "--require-delivery",
        ]) == 2
    unread = relay.read_messages(relay.resolve_box_dir(cfg), "b", advance=False)
    assert [record["body"] for record in unread] == ["oi"]


def test_cli_delivery_json_surfaces_non_delivery(tmp_path, monkeypatch, capsys):
    cfg = {
        "box_dir": "./mail",
        "agents": [
            {"name": "a", "tmux_session": "sess-a", "busy_regex": "X", "input_prefix": "> "},
            {"name": "b", "tmux_session": "sess-b", "busy_regex": "X", "input_prefix": "> "},
        ],
    }
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    with patch("relay.tmux_has_session", return_value=False):
        assert relay.main([
            "send", "--from", "a", "--to", "b", "--body", "oi", "--delivery-json",
        ]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines()
                if line.startswith("DELIVERY_RESULT "))
    assert json.loads(line.removeprefix("DELIVERY_RESULT ")) == {
        "delivered": False, "nudge_status": "session_absent", "seq": 1,
    }


def test_cli_safe_send_refuses_and_returns_nonzero(tmp_path, monkeypatch):
    cfg = {
        "box_dir": "./mail",
        "agents": [
            {"name": "a", "tmux_session": "sess-a", "busy_regex": "X", "input_prefix": "> "},
            {"name": "b", "tmux_session": "sess-b", "busy_regex": "X", "input_prefix": "> "},
        ],
    }
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)

    with patch("relay.tmux_has_session", return_value=False):
        code = relay.main(["safe-send", "--from", "a", "--to", "b", "--body", "oi"])
    assert code == 1


def test_cli_doctor_returns_nonzero_when_session_absent(tmp_path, monkeypatch):
    cfg = {
        "box_dir": "./mail",
        "agents": [{"name": "a", "tmux_session": "ghost", "busy_regex": "X", "input_prefix": "> "}],
    }
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)

    with patch("relay.tmux_has_session", return_value=False):
        code = relay.main(["doctor", "--agent", "a"])
    assert code == 1


def test_cli_missing_config_fails_legibly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = relay.main(["read", "--as", "a"])
    assert code == 1
    err = capsys.readouterr().err
    assert "relay.yaml" in err or "relay.json" in err


# ── F-977: ONE consumer-configuration rule, and the unknown role is REFUSED ──

def _cfg_with(tmp_path, agent_entry):
    cfg = {"box_dir": "./mail", "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "X", "input_prefix": "> "},
        agent_entry,
    ]}
    (tmp_path / "relay.json").write_text(json.dumps(cfg))
    return cfg


def test_an_unknown_role_without_tmux_is_refused_not_labelled_api(tmp_path, monkeypatch, capsys):
    """THE oracle of F-977: no tmux used to mean api_consumer.

    The relay persisted and then labelled ANY tmux-less recipient `NUDGE
    api_consumer`, while the bridge admitted only an explicit `mail_consumer: api`.
    A configuration with an unknown role and no tmux had one caller treating it as
    API and the other refusing it - the disagreement that let a mailbox nobody can
    poll look like a working channel.
    """
    _cfg_with(tmp_path, {"name": "ghost", "mail_consumer": "bogus",
                         "tmux_session": None, "busy_regex": "NEVER_MATCHES",
                         "input_prefix": ""})
    monkeypatch.chdir(tmp_path)
    assert relay.main(["send", "--from", "a", "--to", "ghost", "--body", "job"]) == 1
    captured = capsys.readouterr()          # captured ONCE: a second read is empty
    combined = captured.out + captured.err
    # The VETO (seq1829) proved this test was passing for the wrong reason: it only
    # checked that 'api_consumer' did not appear, while the invalid recipient was
    # still APPENDED to and reported SENT. The oracle is that nothing is delivered
    # to at all.
    assert "SENT" not in captured.out, "an invalid recipient must not be accepted"
    assert "api_consumer" not in combined
    assert "unknown" in combined.lower()
    assert not (tmp_path / "mail" / "to_ghost.jsonl").exists(), (
        "a mailbox must not be created for a recipient the rule refuses")


def test_the_query_answers_structurally_for_a_valid_api_agent(tmp_path, monkeypatch, capsys):
    _cfg_with(tmp_path, {"name": "glm", "mail_consumer": "api", "tmux_session": None,
                         "busy_regex": "NEVER_MATCHES", "input_prefix": ""})
    monkeypatch.chdir(tmp_path)
    assert relay.main(["check-consumer", "glm"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["role"] == "api"
    assert payload["valid"] is True


def test_the_query_refuses_non_zero_for_an_unknown_role(tmp_path, monkeypatch, capsys):
    _cfg_with(tmp_path, {"name": "ghost", "mail_consumer": "bogus", "tmux_session": None,
                         "busy_regex": "NEVER_MATCHES", "input_prefix": ""})
    monkeypatch.chdir(tmp_path)
    assert relay.main(["check-consumer", "ghost"]) == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["role"] == "unknown"
    assert payload["valid"] is False


def test_an_agent_with_tmux_and_no_declared_role_stays_a_tmux_consumer(tmp_path, monkeypatch, capsys):
    """The regression guard: most agents declare no role and DO have a pane.

    If the new rule refuses those, the change breaks the fleet it protects.
    """
    _cfg_with(tmp_path, {"name": "dc", "tmux_session": "dc",
                         "busy_regex": "X", "input_prefix": "> "})
    monkeypatch.chdir(tmp_path)
    assert relay.main(["check-consumer", "dc"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["role"] == "tmux"


# ── F-977: the RECOVERY template must contain a valid API consumer ───────────

def _example_config():
    """The tracked recovery template: the only PORTABLE config there is.

    The live relay.yaml is gitignored, so on a fresh host this file IS the whole
    configuration. If it lacks the roles the live one has, a recovered system refuses
    the agents the live one serves - which is what the re-review found.
    """
    # Resolved from THIS file, not relay.__file__, which can point at a .pyc inside
    # __pycache__ and would look for the template in the wrong directory.
    path = Path(__file__).resolve().parents[1] / "relay.yaml.example"
    return yaml.safe_load(path.read_text())


def test_the_recovery_template_validates_glm_as_an_api_consumer():
    cfg = _example_config()
    glm = next(a for a in cfg["agents"] if a["name"] == "glm")
    assert relay.consumer_role(glm) == "api"
    answer = relay.check_consumer(cfg, "glm")
    assert answer["valid"] is True and answer["role"] == "api"


def _recover_into(tmp_path, source="relay.yaml.example"):
    """TEST FIXTURE ONLY - this is not the recovery mechanism.

    It copies the tracked template and then REWRITES box_dir into tmp_path, which is a
    local substitution - and criterion 5 (5ff1e38) says the allowed substitutions are
    NONE. It exists so these tests never touch the production mailbox, and no reader
    should mistake it for a production recovery path. See
    docs/F977_CRITERIA_5_6_NON_REPRODUCIBLE_2026_09_12.md.
    """
    import shutil

    src = Path(__file__).resolve().parents[1] / source
    text = src.read_text()
    # The tracked template carries the LIVE absolute box_dir, so a naive copy writes
    # into the production mailbox - which is what happened when this test was first
    # written, and is exactly the side effect a recovery test must not have.
    rewritten, n = re.subn(r"(?m)^box_dir:.*$", "box_dir: ./mail", text)
    assert n == 1, f"expected exactly one box_dir line, found {n}"
    (tmp_path / "relay.yaml").write_text(rewritten)
    return tmp_path / "relay.yaml"


def test_recovery_processes_a_real_send_and_preserves_the_append_for_none(tmp_path, monkeypatch, capsys):
    """`none` is DECLARED, so its refusal is recorded after the append (F-979).

    The previous version asserted the predicate, which is not the same claim: what
    matters is that the delivery is attempted and recorded, not refused before anything
    exists.
    """
    _recover_into(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert relay.main(["send", "--from", "deepcode", "--to", "scheduler",
                       "--body", "job"]) == 1
    captured = capsys.readouterr()
    assert "SENT" in captured.out, "a declared none keeps the append"
    assert (tmp_path / "mail" / "to_scheduler.jsonl").exists()
    assert "no_consumer" in captured.err


def test_a_generic_template_refuses_and_names_the_recovery_one(tmp_path, monkeypatch, capsys):
    """Recovering from the generic relay.example.yaml is the WRONG template: it has no
    api consumer, so the refusal must name the tracked file to use instead."""
    _recover_into(tmp_path, source="relay.example.yaml")
    monkeypatch.chdir(tmp_path)
    assert relay.main(["check-consumer", "glm"]) == 1
    assert "relay.yaml.example" in capsys.readouterr().err


def test_altering_the_api_role_away_fires_not_only_removing_the_agent():
    """R-FIRE (c): removing the agent is one way to lose the consumer; changing its role
    is another, and the coverage must fire on both."""
    cfg = _example_config()
    for agent in cfg["agents"]:
        if agent["name"] == "glm":
            agent["mail_consumer"] = "none"
    answer = relay.check_consumer(cfg, "glm")
    assert answer["valid"] is False
    assert answer["role"] == "none"


def test_the_recovery_template_declares_scheduler_as_none_not_unknown():
    """`none` is a DECLARED refusal, recorded after the append (F-979); `unknown` is a
    configuration error refused BEFORE it. A recovered host must behave like the live
    one, not like a misconfigured one."""
    assert relay.check_consumer(_example_config(), "scheduler")["role"] == "none"


def test_removing_the_api_consumer_from_the_template_makes_it_fire():
    cfg = _example_config()
    cfg["agents"] = [a for a in cfg["agents"] if a["name"] != "glm"]
    answer = relay.check_consumer(cfg, "glm")
    assert answer["valid"] is False
    assert answer["role"] == "unknown"


def test_normal_send_appends_durably_but_never_types_into_a_busy_pane(tmp_path, monkeypatch, capsys):
    """F-1017: the normal send path (guarded=False) appends the mail durably but must NOT
    type into a busy/dirty pane. The nudge becomes a typed PENDING state - the message
    stays in the mailbox for the existing redelivery path (not delivered, not lost) - and
    ZERO tmux send-keys calls are made. Driven by a CAPTURED pane, not by mocking the
    caller chain."""
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "BUSY", "input_prefix": "> "},
    ]}
    monkeypatch.setattr(relay, "tmux_has_session", lambda session: True)
    monkeypatch.setattr(relay, "tmux_capture_pane", lambda session: (True, "BUSY working\n> "))
    monkeypatch.setattr(relay.time, "sleep", lambda *_a: None)
    typed = []
    monkeypatch.setattr(relay.subprocess, "run", lambda argv, **kw: typed.append(list(argv)))
    args = relay.build_parser().parse_args(
        ["send", "--from", "a", "--to", "a", "--body", "hello",
         "--require-delivery", "--delivery-json"])
    rc = relay._cmd_send(cfg, args, guarded=False)
    out = capsys.readouterr().out
    assert [m["body"] for m in relay.read_messages(tmp_path, "a", advance=False)] == ["hello"]
    assert [c for c in typed if "send-keys" in c] == []      # ZERO send-keys
    assert "NUDGE pending" in out
    assert '"delivered": false' in out
    assert '"nudge_status": "pending"' in out
    assert rc == 2


def test_normal_send_nudges_exactly_once_when_the_pane_is_ready(tmp_path, monkeypatch, capsys):
    """Control: a ready, clear pane keeps the normal ONE-message nudge (its text plus the
    Enter) and exit 0 - the readiness gate must not become a general refusal."""
    cfg = {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "BUSY", "input_prefix": "> "},
    ]}
    monkeypatch.setattr(relay, "tmux_has_session", lambda session: True)
    monkeypatch.setattr(relay, "tmux_capture_pane", lambda session: (True, "ready\n"))
    monkeypatch.setattr(relay.time, "sleep", lambda *_a: None)
    typed = []
    monkeypatch.setattr(relay.subprocess, "run", lambda argv, **kw: typed.append(list(argv)))
    args = relay.build_parser().parse_args(
        ["send", "--from", "a", "--to", "a", "--body", "hello", "--delivery-json"])
    rc = relay._cmd_send(cfg, args, guarded=False)
    out = capsys.readouterr().out
    assert len([c for c in typed if "send-keys" in c]) == 2   # the message and its Enter, once
    assert '"delivered": true' in out
    assert rc == 0

