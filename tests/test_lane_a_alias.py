"""Pins for LANE A: the canonical agent identity and the legacy alias.

Criteria (Codex, relay seq 2834): unambiguous resolution of BOTH names to the same destination; the old alias covered;
no history rewritten; regression test plus ruff plus a real caller/E2E because this touches the relay. And the reason
these pins exist at all is a defect I found by measuring before writing:

    relay.py:935   seq = append_message(box_dir, args.to, args.from_, args.body, ...)

`_cmd_send` VALIDATES the recipient with `get_agent(config, args.to)` and then WRITES THE MAILBOX WITH `args.to`. So an
alias added only to `get_agent` would validate fine and create `to_glm53flash-1.jsonl` - a SECOND MAILBOX, which the
criteria forbid in the same sentence that asks for the alias. Validate-one-name-write-another is the F-1143 family, and
this is what the pins hold.

THE DESIGN, from the measurement: `name` is the key of the mailbox, the seq counter and the cursor (`to_{name}.jsonl`,
`.seq_{name}`, `.cursor_{name}`), so it is NOT renamed - renaming would break the mailbox history, which the criteria
prohibit. Instead `get_agent` resolves an alias to the agent, and the WRITE SITES use the resolved agent's own name.
The new fields separate what the criteria ask to separate: `agent_id` (canonical identity), `model`, `role`, plus
`aliases` for the legacy name. History stays untouched by construction.
"""
import json
import pathlib
import subprocess
import sys

RELAY = pathlib.Path("/opt/agent-relay/relay.py")


def _run(args, *, cwd):
    return subprocess.run([sys.executable, str(RELAY), "--config", str(cwd / "relay.yaml"), *args],
                          cwd=str(cwd), capture_output=True, text=True, check=False, timeout=60)


def _config(cwd: pathlib.Path, *, name="glm-builder", extra=None) -> pathlib.Path:
    """A minimal config with one real agent, a canonical id, and a legacy alias."""
    agents = [
        {"name": "deepcode", "tmux_session": "deepcode", "busy_regex": "esc", "input_prefix": "",
         "repo_path": "/tmp"},
        {"name": name, "tmux_session": name, "busy_regex": "esc", "input_prefix": "",
         "repo_path": "/tmp", "agent_id": "glm53flash-1", "model": "glm-5.3-flash", "role": "builder",
         "aliases": ["glm53flash-1"]},
    ]
    if extra:
        agents.extend(extra)
    (cwd / "relay.yaml").write_text(json.dumps({"box_dir": "./mail", "agents": agents}, indent=2), encoding="utf-8")
    (cwd / "mail").mkdir(exist_ok=True)
    return cwd


def test_the_ALIAS_resolves_to_the_same_agent_as_the_canonical_name(tmp_path):
    _config(tmp_path)
    import importlib.util

    spec = importlib.util.spec_from_file_location("_relay_laneA", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = mod.load_config(str(tmp_path / "relay.yaml"))
    by_name = mod.get_agent(cfg, "glm-builder")
    by_alias = mod.get_agent(cfg, "glm53flash-1")
    assert by_name is not None and by_alias is not None
    assert by_alias["name"] == by_name["name"] == "glm-builder", "both names must land on ONE agent"


def test_sending_to_the_ALIAS_writes_the_CANONICAL_mailbox_and_not_a_second_one(tmp_path):
    """The pin for the defect I found by measuring. If the write site keeps using the raw argument, this fails and
    to_glm53flash-1.jsonl appears - which is the second mailbox the criteria forbid."""
    _config(tmp_path)
    r = _run(["send", "--from", "deepcode", "--to", "glm53flash-1", "--body", "hello via alias"], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    canonical = tmp_path / "mail" / "to_glm-builder.jsonl"
    second = tmp_path / "mail" / "to_glm53flash-1.jsonl"
    assert canonical.exists(), f"the canonical mailbox must receive it: {list((tmp_path / 'mail').iterdir())}"
    assert not second.exists(), "a second mailbox was created - validate-one-name-write-another"


def test_the_LEGACY_name_still_works_unchanged(tmp_path):
    """The alias is additive: everything that used the old name before must keep working, or the change breaks the
    repository it was meant to make compatible."""
    _config(tmp_path)
    r = _run(["send", "--from", "deepcode", "--to", "glm-builder", "--body", "hello legacy"], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "mail" / "to_glm-builder.jsonl").exists()


def test_an_UNKNOWN_name_is_still_refused(tmp_path):
    """The alias must not become a wildcard: a name nobody declared is still a configuration error."""
    _config(tmp_path)
    r = _run(["send", "--from", "deepcode", "--to", "not-a-real-agent", "--body", "x"], cwd=tmp_path)
    assert r.returncode != 0
    assert "not-a-real-agent" in r.stderr


def test_the_HISTORY_is_not_rewritten(tmp_path):
    """No history rewritten: the mail directory holds only what the run created, and the pre-existing archive files
    (if any) are byte-identical afterwards. Exercised by hashing before and after."""
    _config(tmp_path)
    hist = tmp_path / "mail" / "note_glm-builder_20260101T000000Z.md"
    hist.write_text("historical note, must not be touched\n", encoding="utf-8")
    before = hist.read_bytes()
    _run(["send", "--from", "deepcode", "--to", "glm53flash-1", "--body", "x"], cwd=tmp_path)
    assert hist.read_bytes() == before, "history was rewritten"


def test_the_FIELDS_are_separated_and_the_alias_is_declared(tmp_path):
    """agent_id, model and role are separate fields, and the legacy name is declared as an alias rather than implied -
    so a reader can tell identity from model from role, which is what the criteria asked to separate."""
    _config(tmp_path)
    cfg_text = (tmp_path / "relay.yaml").read_text(encoding="utf-8")
    for field in ("agent_id", "model", "role", "aliases"):
        assert field in cfg_text, field
    import importlib.util

    spec = importlib.util.spec_from_file_location("_relay_laneA2", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    agent = mod.get_agent(mod.load_config(str(tmp_path / "relay.yaml")), "glm-builder")
    assert agent["agent_id"] == "glm53flash-1"
    assert agent["agent_id"] != agent["name"], "identity and mailbox key are different things"
