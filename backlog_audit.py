"""backlog_audit — READ-ONLY pending-work audit lane (codex seq3795 design).

Measured gap (codex): idle_agent_watchdog sees unread/status and redelivers;
dispatch_watchdog sees only open dispatches; continuity_watchdog treats
liveness/API mail. NOBODY verifies pending decisions / reviews / findings
globally. This lane does that, once per cron cycle, WITHOUT consuming mail,
touching cursors, or creating queues.

Per seat (from relay config `agents`, each with its own `repo_path`):
  - memory/findings_index.yaml      -> open / total findings
  - memory/WORK_QUEUE.md            -> unchecked "- [ ]" items
  - data/state/orthogonal_verdicts.jsonl   -> non-PASS review verdicts
  - data/state/consensus_decisions.jsonl   -> pending consensus decisions
  - its relay mailbox               -> unread (cursor or declared marker)

SEMANTICS OF "PENDING CONSENSUS DECISION": a row without `resolved_by`/
`resolved_at` is pending; a RESOLVED row is REOPENED (counted pending again)
when the LAST row of orthogonal_verdicts.jsonl is a non-PASS verdict — a
trailing VETO on the books means the review a decision leaned on failed, so
acting as if it were reviewed would be exactly the "fake review" codex
designed this lane to catch. (Test-pinned: resolved row + [PASS, VETO] ->
pending=1; resolved row + [PASS] -> pending=0.)

Status per seat:
  UNKNOWN          any tracked source failed to read (fail-closed escalation)
  OPEN             all readable and at least one pending measure > 0
  EMPTY_CONFIRMED  all readable and every pending measure == 0

State is written ATOMICALLY to <state_dir>/<agent>.json every run. With
--act, ONE bounded notification per seat goes to to_coordinator ONLY on a
transition INTO OPEN/UNKNOWN (from the seat's previous state file); a stable
OPEN or a recovery to EMPTY_CONFIRMED never notifies. The coordinator seat
is audited but never notifies itself (it is the escalation target).

Read-only guarantee: this module never calls append_message on a seat box,
never writes a cursor, never advances a marker. The ONLY write it performs
outside its own state dir is the single bounded notification record in
to_coordinator.jsonl.

Status semantics (test-pinned): findings and WORK_QUEUE are GLOBAL repo
sources — they are measured and reported (state + notification summary) but
do NOT flip a seat's status: they are not seat-attributable, so an OPEN
finding or unchecked queue item coexists with EMPTY_CONFIRMED for a seat
whose own mail, verdicts and decisions are clean. A seat is OPEN only when
its OWN pending work is > 0 (non-PASS verdicts, pending decisions, unread
mail); UNKNOWN on any read error (fail-closed).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import relay

DEFAULT_STATE_DIR = Path("/opt/agent-relay/state/backlog_audit")
NOTIFY_TO = "coordinator"
NOTIFY_FROM = "scheduler"  # cron identity: sender-only seat, no consumer role
STATUS_OPEN = "OPEN"
STATUS_EMPTY = "EMPTY_CONFIRMED"
STATUS_UNKNOWN = "UNKNOWN"
NOTIFY_STATUSES = (STATUS_OPEN, STATUS_UNKNOWN)

_UNCHECKED = re.compile(r"^\s*-\s+\[\s\]\s")


def _read_jsonl(path: Path) -> tuple[list[dict] | None, str | None]:
    """Return (rows, error). A malformed line is a READ ERROR (fail-closed)."""
    if not path.exists():
        return None, f"missing file: {path}"
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                return None, f"malformed JSONL in {path.name}: {exc}"
    except OSError as exc:
        return None, f"unreadable {path}: {exc}"
    return rows, None


def _source(state: str, /, **metrics) -> dict:
    out = {"state": state}
    out.update(metrics)
    return out


def _audit_findings(repo: Path) -> tuple[dict, str | None]:
    path = repo / "memory" / "findings_index.yaml"
    if not path.exists():
        return _source("error", error=f"missing file: {path}"), f"missing file: {path}"
    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any parse/read failure escalates
        return _source("error", error=str(exc)), str(exc)
    rows = doc.get("findings", []) if isinstance(doc, dict) else (doc or [])
    if not isinstance(rows, list):
        err = f"findings_index.yaml: unexpected top-level type {type(rows).__name__}"
        return _source("error", error=err), err
    open_count = 0
    for row in rows:
        status = str(row.get("status", "")).strip().upper() if isinstance(row, dict) else ""
        if not status or status.startswith("OPEN"):
            open_count += 1  # unclassified or open = pending (fail-closed)
    return _source("ok", open=open_count, total=len(rows)), None


def _audit_work_queue(repo: Path) -> tuple[dict, str | None]:
    path = repo / "memory" / "WORK_QUEUE.md"
    if not path.exists():
        return _source("error", error=f"missing file: {path}"), f"missing file: {path}"
    try:
        unchecked = sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                        if _UNCHECKED.match(line))
    except OSError as exc:
        return _source("error", error=str(exc)), str(exc)
    return _source("ok", unchecked=unchecked), None


def _audit_verdicts(repo: Path) -> tuple[dict, str | None, bool]:
    """(source, error, last_verdict_non_pass) — the third drives the
    decision-reopen rule; it is False whenever the file is unreadable.

    `non_pass` counts TRAILING non-PASS rows (walking back from the end): a
    VETO deep in history that later PASS rows superseded is resolved history,
    not pending work (measured 2026-09-20: the real ledger carries 5 resolved
    vetoes from Sep 11-14 — a cumulative count would pin every seat OPEN
    forever). A trailing VETO is the unresolved "fake review" case codex
    designed the R-FIRE test around. An ABSENT ledger is zero rows (runtime
    state, not a broken tracked source)."""
    path = repo / "data" / "state" / "orthogonal_verdicts.jsonl"
    if not path.exists():
        return _source("ok", total=0, non_pass=0), None, False
    rows, err = _read_jsonl(path)
    if err is not None:
        return _source("error", error=err), err, False
    trailing = 0
    for row in reversed(rows):
        verdict = str(row.get("verdict", "")).strip().upper() if isinstance(row, dict) else ""
        if not verdict or verdict != "PASS":
            trailing += 1  # unclassifiable review outcome = pending (fail-closed)
        else:
            break
    last_non_pass = trailing > 0
    return _source("ok", total=len(rows), non_pass=trailing), None, last_non_pass


def _audit_decisions(repo: Path, verdicts_reopen: bool) -> tuple[dict, str | None]:
    """Pending CONSENSUS DECISIONS. Only `kind: decision` rows count (the real
    ledger also carries `retraction`/`anomaly` markers — informational rows,
    not pending decisions; a row with NO kind is treated as a decision,
    fail-closed). A decision row is pending when unresolved, or resolved but
    reopened by a trailing non-PASS orthogonal verdict. An ABSENT ledger is
    zero rows (runtime state)."""
    path = repo / "data" / "state" / "consensus_decisions.jsonl"
    if not path.exists():
        return _source("ok", total=0, pending=0), None
    rows, err = _read_jsonl(path)
    if err is not None:
        return _source("error", error=err), err
    pending = 0
    for row in rows:
        if not isinstance(row, dict):
            pending += 1
            continue
        kind = str(row.get("kind", "decision")).strip() or "decision"
        if kind != "decision":
            continue
        resolved = bool(row.get("resolved_by") or row.get("resolved_at"))
        if not resolved or (resolved and verdicts_reopen):
            pending += 1
    return _source("ok", total=len(rows), pending=pending), None


def _audit_mail(cfg: dict, agent: dict, box_dir: Path) -> tuple[dict, str | None]:
    """Unread measurement WITHOUT consuming: TUI/api seats use the relay
    cursor; a wrapper seat uses its declared `mail_consumer_marker` (the
    qwen-reserve pattern); `mail_consumer: none` is sender-only by design."""
    name = agent["name"]
    consumer = str(agent.get("mail_consumer") or "").strip()
    if "mail_consumer_marker" in agent:
        try:
            last_seq = int(json.loads(
                Path(agent["mail_consumer_marker"]).read_text(encoding="utf-8")
            ).get("last_seq", 0))
        except (OSError, ValueError, json.JSONDecodeError, AttributeError) as exc:
            return _source("error", error=f"marker unreadable: {exc}"), str(exc)
        return _source("ok", unread=max(0, relay.high_seq(box_dir, name) - last_seq),
                       cursor=last_seq, max_seq=relay.high_seq(box_dir, name)), None
    if consumer == "none":
        return _source("ok", unread=0, mode="declared-none"), None
    if consumer not in ("", "api") and not agent.get("tmux_session"):
        err = (f"seat {name!r} declares mail_consumer={consumer!r} with no "
               "tmux_session and no mail_consumer_marker: unread cannot be "
               "measured without consuming -> fail-closed")
        return _source("error", error=err), err
    cursor = relay.read_cursor(box_dir, name)
    max_seq = relay.high_seq(box_dir, name)
    return _source("ok", unread=max(0, max_seq - cursor),
                   cursor=cursor, max_seq=max_seq), None


def _audit_agent(cfg: dict, agent: dict, box_dir: Path) -> dict:
    name = agent["name"]
    repo = Path(str(agent.get("repo_path") or "")).resolve()
    sources: dict = {}
    read_errors: list[str] = []

    for key, runner in (("findings", _audit_findings),
                        ("work_queue", _audit_work_queue)):
        src, err = runner(repo)
        sources[key] = src
        if err:
            read_errors.append(f"{key}: {err}")

    verdict_src, verdict_err, reopen = _audit_verdicts(repo)
    sources["orthogonal_verdicts"] = verdict_src
    if verdict_err:
        read_errors.append(f"orthogonal_verdicts: {verdict_err}")

    dec_src, dec_err = _audit_decisions(repo, reopen)
    sources["consensus_decisions"] = dec_src
    if dec_err:
        read_errors.append(f"consensus_decisions: {dec_err}")

    mail_src, mail_err = _audit_mail(cfg, agent, box_dir)
    sources["mail"] = mail_src
    if mail_err:
        read_errors.append(f"mail: {mail_err}")

    if read_errors:
        status = STATUS_UNKNOWN
    else:
        # Seat-attributable pending work ONLY (see module docstring): global
        # findings/WORK_QUEUE counts are reported, not status-driving.
        pending = (
            sources["orthogonal_verdicts"]["non_pass"]
            + sources["consensus_decisions"]["pending"]
            + sources["mail"]["unread"]
        )
        status = STATUS_OPEN if pending > 0 else STATUS_EMPTY
    return {"agent": name, "repo_path": str(repo), "status": status,
            "sources": sources, "read_errors": read_errors,
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}


def _load_prev_state(state_dir: Path, name: str) -> str | None:
    path = state_dir / f"{name}.json"
    try:
        if path.exists():
            return str(json.loads(path.read_text(encoding="utf-8")).get("status"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _write_state_atomic(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{state['agent']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _bounded(body: str) -> str:
    limit = relay.DEFAULT_MAX_BODY_LEN
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _summary_line(state: dict) -> str:
    src = state["sources"]
    parts = []
    for key in ("findings", "work_queue", "orthogonal_verdicts",
                "consensus_decisions", "mail"):
        s = src.get(key, {})
        if s.get("state") == "error":
            parts.append(f"{key}=ERROR")
        else:
            metrics = {k: v for k, v in s.items() if k not in ("state", "error")}
            parts.append(f"{key}=" + (",".join(f"{k}={v}" for k, v in metrics.items()) or "0"))
    errors = f"; read_errors={len(state['read_errors'])}" if state["read_errors"] else ""
    return f"{state['agent']} {state['status']}: " + "; ".join(parts) + errors


def run(config_path: str | None = None, state_dir: str | Path = DEFAULT_STATE_DIR,
        *, act: bool = False, notify=None) -> dict:
    """Audit every seat. With act=True, ONE bounded notification per seat on
    transition INTO OPEN/UNKNOWN (notify callable or the real relay append)."""
    if notify is None:
        notify = _noop

    cfg = relay.load_config(config_path)
    box_dir = Path(str(cfg.get("box_dir") or "mail"))
    state_dir = Path(state_dir)

    agents_out: dict[str, dict] = {}
    unknown_any = False
    for agent in cfg.get("agents", []):
        state = _audit_agent(cfg, agent, box_dir)
        agents_out[state["agent"]] = state
        unknown_any = unknown_any or state["status"] == STATUS_UNKNOWN

        prev = _load_prev_state(state_dir, state["agent"])
        if (act and state["status"] in NOTIFY_STATUSES
                and state["status"] != prev
                and state["agent"] != NOTIFY_TO):
            notify(_bounded("[BACKLOG-AUDIT] " + _summary_line(state)))
        _write_state_atomic(state_dir, state)

    return {"status": "unknown" if unknown_any else "ok", "agents": agents_out}


def _noop(_body: str) -> None:
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None,
                        help="relay.yaml/relay.json path (default: cwd discovery)")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--act", action="store_true",
                        help="send the bounded transition notification to "
                             f"to_{NOTIFY_TO}.jsonl (from {NOTIFY_FROM})")
    args = parser.parse_args(argv)

    import relay as _relay
    cfg = _relay.load_config(args.config)
    box_dir = Path(str(cfg.get("box_dir") or "mail"))

    def real_notify(body: str) -> None:
        _relay.append_message(box_dir, NOTIFY_TO, NOTIFY_FROM, body)

    result = run(config_path=args.config, state_dir=args.state_dir,
                 act=args.act, notify=real_notify if args.act else None)
    print(f"backlog_audit: {result['status']} "
          f"({', '.join(f'{a}={s['status']}' for a, s in result['agents'].items())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())