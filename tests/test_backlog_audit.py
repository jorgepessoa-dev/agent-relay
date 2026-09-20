"""R-FIRE tests for the backlog_audit lane (codex seq3795 design).

The audit is READ-ONLY: it measures pending work (unread mail, open findings,
unchecked queue items, unresolved decisions, non-PASS verdicts) WITHOUT
consuming mail, touching cursors, or creating queues. State is written
atomically per seat under <state_dir>/<agent>.json. With --act, ONE bounded
notification goes to to_coordinator ONLY on a transition into OPEN/UNKNOWN
(codex: EMPTY_CONFIRMED so com todas fontes legiveis e zero itens; erro de
leitura vira UNKNOWN/escalacao).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import backlog_audit
import relay

FINDINGS = """findings:
- id: F-9001
  status: OPEN
- id: F-9002
  status: RESOLVED_DOCUMENTED
"""

FINDINGS_EMPTY = "findings: []\n"

WORK_QUEUE = """- [ ] pending item one
- [x] done item
- [ ] pending item two
"""


@pytest.fixture
def world(tmp_path):
    """Minimal relay world: relay.json, a shared seat repo with all sources,
    two TUI seats, a dedicated state dir."""
    mail = tmp_path / "mail"
    mail.mkdir()
    repo = tmp_path / "repo"
    (repo / "memory").mkdir(parents=True)
    (repo / "data" / "state").mkdir(parents=True)
    state_dir = tmp_path / "state" / "backlog_audit"
    agents = [
        {"name": "coordinator", "tmux_session": "c", "busy_regex": "B",
         "input_prefix": "> ", "repo_path": str(repo)},
        {"name": "worker", "tmux_session": "w", "busy_regex": "B",
         "input_prefix": "> ", "repo_path": str(repo)},
    ]
    cfg_path = tmp_path / "relay.json"
    cfg_path.write_text(json.dumps({"box_dir": str(mail), "agents": agents}))
    return {
        "tmp": tmp_path, "mail": mail, "repo": repo,
        "state_dir": state_dir, "cfg_path": cfg_path,
    }


def write_repo_sources(repo: Path, *, findings=FINDINGS, queue=WORK_QUEUE,
                       verdicts=(), decisions=()):
    (repo / "memory" / "findings_index.yaml").write_text(findings)
    (repo / "memory" / "WORK_QUEUE.md").write_text(queue)
    vpath = repo / "data" / "state" / "orthogonal_verdicts.jsonl"
    vpath.write_text("".join(json.dumps(v) + "\n" for v in verdicts))
    dpath = repo / "data" / "state" / "consensus_decisions.jsonl"
    dpath.write_text("".join(json.dumps(d) + "\n" for d in decisions))


def run(world, *, act=False, notify=None):
    return backlog_audit.run(
        config_path=world["cfg_path"], state_dir=world["state_dir"],
        act=act, notify=notify,
    )


def test_rfire_open_finding_and_veto_make_status_open(world):
    """Codex R-FIRE: fake review (VETO verdict) + open finding must surface
    as OPEN, not be silently ignored."""
    write_repo_sources(world["repo"], verdicts=[{"verdict": "PASS"}],
                       decisions=[{"kind": "decision", "resolved_by": "x"}])
    (world["repo"] / "memory" / "findings_index.yaml").write_text(FINDINGS)
    verdicts = world["repo"] / "data" / "state" / "orthogonal_verdicts.jsonl"
    verdicts.write_text(json.dumps({"verdict": "PASS"}) + "\n" +
                        json.dumps({"verdict": "VETO"}) + "\n")
    state = run(world)["agents"]["worker"]
    assert state["status"] == "OPEN"
    assert state["sources"]["findings"]["open"] == 1
    assert state["sources"]["findings"]["total"] == 2
    assert state["sources"]["orthogonal_verdicts"]["non_pass"] == 1
    assert state["sources"]["work_queue"]["unchecked"] == 2
    assert state["sources"]["consensus_decisions"]["pending"] == 1


def test_empty_confirmed_when_all_sources_readable_and_zero(world):
    write_repo_sources(world["repo"], findings=FINDINGS_EMPTY, queue="no items here\n",
                       verdicts=[{"verdict": "PASS"}],
                       decisions=[{"kind": "decision", "resolved_by": "x"}])
    state = run(world)["agents"]["worker"]
    assert state["status"] == "EMPTY_CONFIRMED"
    assert state["sources"]["mail"]["unread"] == 0


def test_read_error_becomes_unknown_and_notifies_once(world):
    """Codex: erro de leitura vira UNKNOWN/escalacao — and only ONCE
    (transition-only), not every cycle."""
    write_repo_sources(world["repo"])
    (world["repo"] / "memory" / "findings_index.yaml").write_text("findings: [broken\n")
    sent = []
    result = run(world, act=True, notify=sent.append)
    assert result["status"] == "unknown"
    state = result["agents"]["worker"]
    assert state["status"] == "UNKNOWN"
    assert state["sources"]["findings"]["state"] == "error"
    assert state["read_errors"]
    assert len(sent) == 1
    run(world, act=True, notify=sent.append)
    assert len(sent) == 1, "UNKNOWN→UNKNOWN transition must not re-notify"


def test_notification_only_on_transition_into_open_or_unknown(world):
    """run1 OPEN (fresh) → 1 notify; run2 same OPEN → 0; recovery to
    EMPTY_CONFIRMED → 0 (codex: notify on transition INTO OPEN/UNKNOWN)."""
    write_repo_sources(world["repo"], verdicts=[{"verdict": "VETO"}])
    sent = []
    run(world, act=True, notify=sent.append)
    assert len(sent) == 1
    run(world, act=True, notify=sent.append)
    assert len(sent) == 1
    # clear the veto → EMPTY_CONFIRMED → no notification, but state updated
    (world["repo"] / "data" / "state" / "orthogonal_verdicts.jsonl").write_text("")
    result = run(world, act=True, notify=sent.append)
    assert len(sent) == 1
    assert result["agents"]["worker"]["status"] == "EMPTY_CONFIRMED"


def test_audit_never_consumes_mail_or_writes_cursor(world):
    """Read-only proof: cursor bytes and mailbox lines are identical after a
    full --act run; no new files appear in the box dir."""
    write_repo_sources(world["repo"])
    relay.append_message(world["mail"], "worker", "coordinator", "m1")
    relay.append_message(world["mail"], "worker", "coordinator", "m2")
    relay.write_cursor_atomic(world["mail"], "worker", 1)
    cursor_before = (world["mail"] / ".cursor_worker").read_text()
    lines_before = len((world["mail"] / "to_worker.jsonl").read_text().splitlines())
    files_before = sorted(p.name for p in world["mail"].iterdir())
    run(world, act=True, notify=lambda body: None)
    assert (world["mail"] / ".cursor_worker").read_text() == cursor_before
    lines_after = len((world["mail"] / "to_worker.jsonl").read_text().splitlines())
    assert lines_after == lines_before
    assert sorted(p.name for p in world["mail"].iterdir()) == files_before


def test_mail_unread_measured_from_cursor(world):
    write_repo_sources(world["repo"], findings=FINDINGS_EMPTY, queue="", verdicts=[],
                       decisions=[])
    relay.append_message(world["mail"], "worker", "coordinator", "m1")
    relay.append_message(world["mail"], "worker", "coordinator", "m2")
    relay.write_cursor_atomic(world["mail"], "worker", 1)
    state = run(world)["agents"]["worker"]
    assert state["sources"]["mail"]["unread"] == 1
    assert state["sources"]["mail"]["cursor"] == 1
    assert state["sources"]["mail"]["max_seq"] == 2
    assert state["status"] == "OPEN"  # 1 unread item is pending work


def test_wrapper_consumer_reads_declared_marker_and_creates_no_cursor(world):
    """qwen-reserve pattern: no relay cursor; unread comes from the seat's own
    durable marker; the audit must not create a cursor file."""
    write_repo_sources(world["repo"], findings=FINDINGS_EMPTY, queue="", verdicts=[],
                       decisions=[])
    marker = world["repo"] / "data" / "state" / "qwen_reserve" / "last_seq.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"last_seq": 1}))
    reserve_cfg = {"name": "reserve", "tmux_session": None,
                   "mail_consumer": "qwen-wrapper", "busy_regex": "B",
                   "input_prefix": "", "repo_path": str(world["repo"]),
                   "mail_consumer_marker": str(marker)}
    cfg = json.loads(world["cfg_path"].read_text())
    cfg["agents"].append(reserve_cfg)
    world["cfg_path"].write_text(json.dumps(cfg))
    relay.append_message(world["mail"], "reserve", "coordinator", "a")
    relay.append_message(world["mail"], "reserve", "coordinator", "b")
    relay.append_message(world["mail"], "reserve", "coordinator", "c")
    state = run(world)["agents"]["reserve"]
    assert state["sources"]["mail"]["unread"] == 2
    assert not (world["mail"] / ".cursor_reserve").exists()


def test_wrapper_without_marker_is_unknown(world):
    """A wrapper seat with neither relay cursor nor declared marker cannot be
    measured without consuming -> mail source error -> UNKNOWN (fail-closed)."""
    write_repo_sources(world["repo"], findings=FINDINGS_EMPTY, queue="", verdicts=[],
                       decisions=[])
    reserve_cfg = {"name": "reserve", "tmux_session": None,
                   "mail_consumer": "qwen-wrapper", "busy_regex": "B",
                   "input_prefix": "", "repo_path": str(world["repo"])}
    cfg = json.loads(world["cfg_path"].read_text())
    cfg["agents"].append(reserve_cfg)
    world["cfg_path"].write_text(json.dumps(cfg))
    relay.append_message(world["mail"], "reserve", "coordinator", "a")
    result = run(world)
    state = result["agents"]["reserve"]
    assert state["sources"]["mail"]["state"] == "error"
    assert state["status"] == "UNKNOWN"


def test_missing_tracked_source_is_unknown(world):
    write_repo_sources(world["repo"])
    (world["repo"] / "memory" / "WORK_QUEUE.md").unlink()
    result = run(world)
    state = result["agents"]["worker"]
    assert state["sources"]["work_queue"]["state"] == "error"
    assert result["status"] == "unknown"


def test_state_file_written_atomically_and_valid(world):
    write_repo_sources(world["repo"])
    run(world, act=True, notify=lambda body: None)
    path = world["state_dir"] / "worker.json"
    data = json.loads(path.read_text())
    assert data["agent"] == "worker"
    assert data["repo_path"] == str(world["repo"])
    assert data["status"] in ("OPEN", "EMPTY_CONFIRMED", "UNKNOWN")
    leftovers = [p.name for p in world["state_dir"].iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_notification_body_is_bounded(world):
    """C1: the coordinator notification stays under the relay body limit."""
    write_repo_sources(world["repo"], verdicts=[{"verdict": "VETO"}])
    sent = []
    run(world, act=True, notify=sent.append)
    assert len(sent) == 1
    assert len(sent[0]) <= relay.DEFAULT_MAX_BODY_LEN


def test_cli_end_to_end_real_caller(world, tmp_path):
    """R-SEAM: the real CLI against the test world; state file on disk and
    exactly one notification appended to the real coordinator mailbox."""
    write_repo_sources(world["repo"], verdicts=[{"verdict": "VETO"}])
    proc = subprocess.run(
        [sys.executable, str(Path(backlog_audit.__file__).resolve()),
         "--config", str(world["cfg_path"]),
         "--state-dir", str(world["state_dir"]), "--act"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads((world["state_dir"] / "worker.json").read_text())["status"] == "OPEN"
    box = (world["mail"] / "to_coordinator.jsonl").read_text().splitlines()
    assert len(box) == 1
    rec = json.loads(box[0])
    assert rec["from"] == "scheduler"
    assert "worker" in rec["body"] and "OPEN" in rec["body"]
    assert len(rec["body"]) <= relay.DEFAULT_MAX_BODY_LEN


def test_second_cli_run_same_status_sends_nothing(world):
    write_repo_sources(world["repo"], verdicts=[{"verdict": "VETO"}])
    args = [sys.executable, str(Path(backlog_audit.__file__).resolve()),
            "--config", str(world["cfg_path"]),
            "--state-dir", str(world["state_dir"]), "--act"]
    subprocess.run(args, capture_output=True, text=True, check=False)
    box = world["mail"] / "to_coordinator.jsonl"
    n1 = len(box.read_text().splitlines())
    subprocess.run(args, capture_output=True, text=True, check=False)
    assert len(box.read_text().splitlines()) == n1