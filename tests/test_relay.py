import json
import os
import stat
import threading
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


def test_nudge_reports_session_absent_never_delivered(monkeypatch):
    # Regression test: this is the exact bug DeepCode caught live (seq=156) —
    # the original nudge printed "delivered" when the tmux session did not exist.
    with patch("relay.tmux_has_session", return_value=False):
        status = relay.nudge("ghost-session", 1, "corpo")
    assert status == "session_absent"
    assert status != "delivered"


def test_nudge_reports_capture_failed(monkeypatch):
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", return_value=(False, "")):
        status = relay.nudge("sess", 1, "corpo")
    assert status == "capture_failed"


def test_nudge_reports_delivered_when_input_box_clear(monkeypatch):
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", return_value=(True, "algum texto\n> \n")):
        status = relay.nudge("sess", 1, "corpo")
    assert status == "delivered"


def test_nudge_reports_stuck_when_mail_marker_still_in_box(monkeypatch):
    with patch("relay.tmux_has_session", return_value=True), \
         patch("subprocess.run", return_value=_run(returncode=0)), \
         patch("relay.tmux_capture_pane", return_value=(True, "> [MAIL seq=1] corpo...\n")):
        status = relay.nudge("sess", 1, "corpo")
    assert status == "stuck"


AGENT = {
    "name": "dc",
    "tmux_session": "dc",
    "busy_regex": r"status: (processing|pending)|esc to interrupt",
    "input_prefix": "> ",
}


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
