"""Pins for start_agents.sh, the @reboot starter - provable WITHOUT rebooting.

The finding, verified by reading the script and the crontab rather than by trusting a log: the @reboot entry never ran
(the log it writes to does not exist, and the host has been up for eleven days), and the script's map was out of date.
Measured against the six live sessions, it knew three:

  session        live start command                                   in the script?
  deepcode       (interactive; the script resumes a uuid)             yes
  codex          (interactive; the script resumes --last)             yes
  gemini         .venv/bin/python scripts/ops/gemini_agent.py         yes
  glm-builder    /root/.nvm/versions/node/v24.19.0/bin/deepcode       NO
  glm            bash -lc ".venv/bin/python scripts/ops/api_agent_bridge.py ..."  NO
  claude         (interactive, human-facing)                          opt-in, by design

The docstring claimed "restores the 4 agent tmux sessions" while starting three. And the coordinator path is real code
behind START_COORDINATOR=1 with nothing in the repo saying where to set it.

NO PIN HERE REBOOTS ANYTHING. The script is idempotent by construction - it skips a session that already exists - so
its whole behaviour is provable by running it against a tmux server it cannot find, and asserting it does not crash and
does not start anything twice.
"""
import pathlib
import subprocess

SCRIPT = pathlib.Path("/opt/agent-relay/start_agents.sh")
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")


def test_the_script_names_every_session_the_relay_declares():
    """The measured gap: glm-builder and glm did not come back after a reboot, and the loss is silent.

    LANE A (2026-09-20) separated IDENTITY from SESSION: the agent keeps `name: glm-builder` (it is the
    mailbox key) while `tmux_session: glm53flash-1` is what tmux must start. Hardcoding the session names
    here - as this test did - turned a rename into a failing pin and hid the property that actually
    matters: **every session the relay DECLARES must be started by the reboot script**. So the names are
    DERIVED from relay.yaml, which is the declaration the nudges themselves use; a rename now updates one
    file instead of two, and a genuinely missing session still fails by name.
    """
    import yaml
    cfg = yaml.safe_load(pathlib.Path("/opt/agent-relay/relay.yaml").read_text(encoding="utf-8"))
    declared = [(a.get("name"), a.get("tmux_session")) for a in cfg.get("agents", [])]
    sessions = [(n, s) for n, s in declared if s]
    assert sessions, "relay.yaml declares no tmux_session: nothing to check, which is itself a defect"
    for name, session in sessions:
        if name == "claude":            # the human-facing seat is opt-in by design (START_COORDINATOR=1)
            continue
        assert f"start_session {session}" in SCRIPT_TEXT, (
            f"{name} declares tmux_session={session} but the reboot script does not start it: it would not "
            f"survive a reboot, and the loss is silent")


def test_glm_builder_is_started_with_the_command_it_actually_runs_today():
    """Measured from the live pane, not assumed: it is a deepcode instance. A plausible-but-wrong invocation would
    start an empty session that looks alive, which is worse than not starting it."""
    import re

    import yaml
    cfg = yaml.safe_load(pathlib.Path("/opt/agent-relay/relay.yaml").read_text(encoding="utf-8"))
    session = next(a["tmux_session"] for a in cfg["agents"] if a["name"] == "glm-builder")
    m = re.search(rf"start_session {re.escape(session)}[^\n]*", SCRIPT_TEXT)
    assert m, f"the reboot script must start the declared session {session!r} for glm-builder"
    line = m.group(0)
    assert "deepcode" in line, f"glm-builder runs deepcode today: {line}"


def test_the_docstring_states_the_number_it_actually_starts():
    """The docstring said four while starting three. A comment that is confidently wrong is how the next reader stops
    counting - which is exactly how glm-builder stayed missing."""
    import re

    m = re.search(r"restores the (\d+) agent tmux sessions", SCRIPT_TEXT)
    assert m, "the docstring must state a number so this pin can hold it"
    claimed = int(m.group(1))
    started = len(re.findall(r"^\s*start_session (\S+)", SCRIPT_TEXT, re.MULTILINE))
    assert claimed == started, f"the docstring claims {claimed} sessions and the script starts {started}"


def test_the_coordinator_path_says_where_the_flag_is_set():
    """START_COORDINATOR=1 was real code with nothing anywhere saying where to set it, so the coordinator could never
    come back by accident OR on purpose."""
    assert "START_COORDINATOR" in SCRIPT_TEXT
    assert SCRIPT_TEXT.count("START_COORDINATOR") >= 2, (
        "the flag must be named where it can be set, not only where it is tested")


def test_the_script_is_IDEMPOTENT_when_no_tmux_server_is_reachable(tmp_path):
    """Provable without a reboot, which is the point: no session can exist on a tmux server that is not there, so the
    script must run to completion and report rather than crash. This is the property that makes the reboot entry safe
    to have at all - a starter that crashed on a half-started machine would be worse than none."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMUX_TMPDIR": str(tmp_path / "no-such-tmux")}
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
    combined = (r.stdout + r.stderr).lower()
    # The property is that the script COMPLETES, not that nothing anywhere printed a traceback. The governance ack is
    # deliberately tolerated ("|| true") because a half-configured environment must not stop the sessions coming back:
    # a starter that dies before starting them is worse than one that starts them and logs a noise line. My first
    # version asserted "no traceback anywhere", which fails on that design - a green-for-the-wrong-reason pin in
    # reverse, strict where the design is lenient.
    assert "start_agents done" in combined, f"the starter must run to its last line: {combined[-200:]}"
    started = combined.count("start ") - combined.count("skip ")
    assert started <= 0 or "not found" in combined, (
        f"with no tmux server reachable, nothing can legitimately be reported as STARTed: {combined[:200]}")
