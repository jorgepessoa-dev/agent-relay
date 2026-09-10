#!/usr/bin/env python3
"""agent-relay — reliable JSONL mailbox between N tmux-hosted AI agents.

Generalized from /opt/agent-relay on the trading-advisor droplet. See
docs/superpowers/specs/2026-08-18-agent-relay-design.md for the design
rationale and the concrete bugs (message loss, false-positive delivery,
race conditions) this fixes.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc  # datetime.UTC needs 3.11+; keep 3.10 compatible

REQUIRED_AGENT_FIELDS = ("tmux_session", "busy_regex", "input_prefix")
DEFAULT_PLACEHOLDER = "Type your message"
DEFAULT_MAX_BODY_LEN = 1600
DEFAULT_REDELIVERY_AFTER_S = 15 * 60
APPROVAL_DIALOG_MARKERS = (
    "Would you like to run the following command?",
    "Press enter to confirm or esc to cancel",
)
REDELIVERY_BASE_BACKOFF_S = 30 * 60
REDELIVERY_MAX_ATTEMPTS = 6
REDELIVERY_MAX_PER_SWEEP = 2


class RelayConfigError(Exception):
    pass


class RelaySendRefused(Exception):
    pass


def load_config(config_path: str | None = None) -> dict:
    if config_path:
        candidates = [Path(config_path)]
    else:
        candidates = [Path("relay.yaml"), Path("relay.json")]

    for path in candidates:
        if not path.exists():
            continue
        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:
                raise RelayConfigError(
                    f"{path} existe mas 'pyyaml' não está instalado. "
                    "Instala com 'pip install pyyaml', ou usa relay.json."
                ) from exc
            with path.open() as fh:
                return yaml.safe_load(fh)
        else:
            with path.open() as fh:
                return json.load(fh)

    raise RelayConfigError(
        f"relay.yaml ou relay.json não encontrado em {Path.cwd()}. "
        "Corre a partir da raiz do projecto (onde vive o ficheiro de config), "
        "ou passa --config <caminho>."
    )


def get_agent(config: dict, name: str) -> dict:
    for agent in config.get("agents", []):
        if agent.get("name") == name:
            missing = [f for f in REQUIRED_AGENT_FIELDS if f not in agent]
            if missing:
                raise RelayConfigError(
                    f"agente '{name}' não tem os campos obrigatórios: {missing}. "
                    "tmux_session/busy_regex/input_prefix não têm default partilhado."
                )
            return agent
    raise RelayConfigError(f"agente '{name}' não existe em relay.yaml/relay.json")


def resolve_box_dir(config: dict) -> Path:
    return Path(config.get("box_dir", "./agent-relay-mail")).resolve()


def head_of(repo_path: str | None) -> str:
    if not repo_path:
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        head = result.stdout.strip()
        return head if result.returncode == 0 and head else "unknown"
    except Exception:
        return "unknown"


def mailbox_path(box_dir: Path, name: str) -> Path:
    return box_dir / f"to_{name}.jsonl"


def seq_path(box_dir: Path, name: str) -> Path:
    """Durable monotonic seq counter for a mailbox (survives rotation)."""
    return box_dir / f".seq_{name}"


def append_message(
    box_dir: Path,
    to_name: str,
    from_name: str,
    body: str,
    tokens: int | None = None,
    sender_repo_path: str | None = None,
) -> int:
    box_dir.mkdir(parents=True, exist_ok=True)
    path = mailbox_path(box_dir, to_name)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fh = os.fdopen(fd, "r+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            # Seq comes from a DURABLE counter, not the line count: after a
            # mailbox rotation the live file is shorter, and counting lines
            # would re-issue already-used seqs (cursor corruption). The
            # counter file is written under the same exclusive lock; a legacy
            # mailbox without one is initialised from its current line count
            # (backwards compatible).
            counter = seq_path(box_dir, to_name)
            if counter.exists():
                try:
                    seq = int(counter.read_text().strip() or 0) + 1
                except ValueError:
                    seq = sum(1 for _ in fh) + 1
            else:
                fh.seek(0)
                seq = sum(1 for _ in fh) + 1
            counter.write_text(str(seq))
            record = {
                "seq": seq,
                "ts_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "from": from_name,
                "head": head_of(sender_repo_path),
                "tokens": tokens,
                "body": body,
            }
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()
    return seq


def cursor_path(box_dir: Path, name: str) -> Path:
    return box_dir / f".cursor_{name}"


def read_cursor(box_dir: Path, name: str) -> int:
    path = cursor_path(box_dir, name)
    if not path.exists():
        return 0
    text = path.read_text().strip()
    return int(text) if text else 0


def write_cursor_atomic(box_dir: Path, name: str, seq: int) -> None:
    with _agent_delivery_lock(box_dir, name):
        path = cursor_path(box_dir, name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w") as fh:
            fh.write(str(seq))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        # Cursor advancement is the sole acknowledgement.  Clear the retry
        # journal while holding the same per-agent lock as the advancement, so
        # a sweep cannot lease a message that was already acknowledged.
        with _redelivery_state_lock(box_dir):
            state = _read_redelivery_state(box_dir)
            if _clear_redelivery_through_state(state, name, seq):
                _write_redelivery_state_atomic(box_dir, state)


def redelivery_state_path(box_dir: Path) -> Path:
    return box_dir / ".redelivery_state.json"


@contextmanager
def _agent_delivery_lock(box_dir: Path, name: str):
    """Serialize a cursor decision with a recipient's acknowledgement."""
    box_dir.mkdir(parents=True, exist_ok=True)
    with (box_dir / f".delivery_{name}.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def _redelivery_state_lock(box_dir: Path):
    """Serialize sweep state updates across watchdog invocations."""
    box_dir.mkdir(parents=True, exist_ok=True)
    lock_path = box_dir / ".redelivery_state.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_redelivery_state(box_dir: Path) -> dict:
    path = redelivery_state_path(box_dir)
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        value = json.loads(path.read_text())
        entries = value.get("entries")
        if isinstance(entries, dict):
            return {"version": 1, "entries": entries}
    except (OSError, ValueError, TypeError):
        pass
    # Do not turn an unread message into a false acknowledgement because the
    # best-effort retry journal was damaged. Start a fresh, bounded schedule.
    return {"version": 1, "entries": {}}


def _write_redelivery_state_atomic(box_dir: Path, state: dict) -> None:
    path = redelivery_state_path(box_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        fh.write(json.dumps(state, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def _entry_key(name: str, seq: int) -> str:
    return f"{name}:{seq}"


def _clear_redelivery_through_state(state: dict, name: str, seq: int) -> bool:
    """Remove acknowledged retry entries; caller holds the state lock."""
    entries = state["entries"]
    removed = False
    for key in list(entries):
        agent, separator, raw_seq = key.rpartition(":")
        if agent == name and separator and raw_seq.isdigit() and int(raw_seq) <= seq:
            del entries[key]
            removed = True
    return removed


def read_messages(box_dir: Path, name: str, advance: bool = True) -> list:
    path = mailbox_path(box_dir, name)
    if not path.exists():
        return []

    with path.open("r") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            raw = fh.read()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    records = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if isinstance(record, dict) and isinstance(record.get("seq"), int):
                records.append(record)
        except json.JSONDecodeError:
            # A torn/corrupt line must not suppress delivery of later valid mail.
            continue
    seen = read_cursor(box_dir, name)
    unread = [r for r in records if r["seq"] > seen]

    if advance and unread:
        write_cursor_atomic(box_dir, name, unread[-1]["seq"])

    return unread


def _record_age_s(record: dict, now: datetime) -> float | None:
    try:
        timestamp = datetime.fromisoformat(record["ts_utc"])
        if timestamp.tzinfo is None:
            return None
        return max(0.0, (now - timestamp).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None


def _state_number(value: object, default: float = 0) -> float:
    """Treat a damaged retry journal as an empty schedule, never a crash."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def redeliver_unread(
    config: dict,
    *,
    now: datetime | None = None,
    min_age_s: int = DEFAULT_REDELIVERY_AFTER_S,
    base_backoff_s: int = REDELIVERY_BASE_BACKOFF_S,
    max_attempts: int = REDELIVERY_MAX_ATTEMPTS,
    max_renudges: int = REDELIVERY_MAX_PER_SWEEP,
) -> list[tuple[str, int, str]]:
    """Re-nudge old unread mail with persistent, bounded exponential backoff.

    A recipient's cursor is the sole acknowledgement. Pane capture only says a
    nudge was submitted, never that the agent consumed the message.
    """
    box_dir = resolve_box_dir(config)
    now = now or datetime.now(UTC)
    results = []
    renudges_issued = 0
    for agent in config.get("agents", []):
        name = agent["name"]
        # This lock defines the precise safety guarantee: mail unread when the
        # decision is made is eligible. It is deliberately released before the
        # potentially seven-second tmux operation, so acknowledgement is never
        # blocked by a slow pane.
        with _agent_delivery_lock(box_dir, name):
            unread = read_messages(box_dir, name, advance=False)
            if not unread:
                continue
            oldest = unread[0]  # oldest by mailbox sequence, never timestamp
            key = _entry_key(name, oldest["seq"])
            age_s = _record_age_s(oldest, now)
            # A legacy/torn timestamp does not get to permanently block the
            # sequence-ordered queue.  It is eligible now, while malformed
            # JSON lines are ignored by read_messages().
            if age_s is None:
                age_s = min_age_s
            if age_s < min_age_s:
                results.append((name, oldest["seq"], "pending_age"))
                continue
            with _redelivery_state_lock(box_dir):
                state = _read_redelivery_state(box_dir)
                entries = state["entries"]
                prior = entries.get(key, {})
                attempts = int(_state_number(prior.get("attempts"), 0))
                if attempts >= max_attempts:
                    if not prior.get("escalated_at"):
                        prior["escalated_at"] = now.isoformat()
                        prior["last_status"] = "failed_cap"
                        _write_redelivery_state_atomic(box_dir, state)
                    results.append((name, oldest["seq"], "failed_cap"))
                    continue
                if now.timestamp() < _state_number(prior.get("next_due_at"), 0):
                    results.append((name, oldest["seq"], "pending_backoff"))
                    continue
                if renudges_issued >= max_renudges:
                    results.append((name, oldest["seq"], "sweep_cap"))
                    continue
                attempts += 1  # lease the attempt before releasing locks
                entries[key] = {
                    "attempts": attempts, "last_nudged_at": now.isoformat(),
                    "last_status": "issuing",
                    "next_due_at": now.timestamp() + base_backoff_s * (2 ** (attempts - 1)),
                }
                _write_redelivery_state_atomic(box_dir, state)
                renudges_issued += 1
        try:
            check_safe_to_send(agent, oldest["body"])
            status = nudge(agent, oldest["seq"], oldest["body"])
        except RelaySendRefused:
            status = "unsafe_busy_or_input"
        except (OSError, subprocess.TimeoutExpired):
            # A tmux timeout must be retried, not kill the sweep.
            status = "nudge_error"
        # Record the outcome unless the cursor has already acknowledged it.
        with _redelivery_state_lock(box_dir):
            if read_cursor(box_dir, name) < oldest["seq"]:
                state = _read_redelivery_state(box_dir)
                entry = state["entries"].get(key)
                if entry:
                    entry["last_status"] = status
                    if status == "session_absent":
                        entry["next_due_at"] = now.timestamp() + base_backoff_s * 4
                    _write_redelivery_state_atomic(box_dir, state)
        results.append((name, oldest["seq"], f"renudged_{status}"))
    return results


def tmux_has_session(session: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def tmux_capture_pane(session: str) -> tuple:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, ""
        return True, result.stdout
    except Exception:
        return False, ""


def _input_box_clear(pane: str, to_agent: dict, seq: int) -> bool:
    # Only the current input line matters for "stuck" — the mail marker text
    # is expected to still be visible in scrollback right after a successful
    # send, so checking the last N lines produced false positives (2/6 in the
    # live test matrix). A stuck send leaves the marker sitting unsent in the
    # input box itself, identified by the recipient's input_prefix.
    box_lines = [l for l in pane.splitlines() if l.startswith(to_agent["input_prefix"])]
    current_input = box_lines[-1] if box_lines else ""
    return f"MAIL seq={seq}" not in current_input


def nudge(to_agent: dict, seq: int, body: str) -> str:
    """Push a mailbox notification into the recipient's tmux pane.

    Never returns "delivered" unless the session was confirmed present and
    the post-send capture succeeded — the original relay conflated "capture
    failed / session gone" with "message scrolled off screen", both of which
    look like an empty tail. That conflation caused a real false-positive
    (DeepCode caught it live during this module's own verification).

    A stuck nudge is retried twice (+2s, +3s) before being reported as
    genuinely stuck — the observed real cause (5 occurrences, 2026-08-29
    session) was a half-typed prior message already sitting in the input
    box, where a second Enter reliably clears it. Bounded, not silent: a
    persistent failure still reports "stuck" exactly as before, it is
    never swallowed.

    A 0.3s settle delay follows every Enter before capturing — found live
    while building the retry's own R-FIRE test: capturing immediately can
    catch the pane before it redraws, reporting "stuck" for an Enter that
    actually cleared a moment later. This race predates the retry (the
    original single-Enter path had the same gap); fixed here since the
    retry is what surfaced it.
    """
    session = to_agent["tmux_session"]
    if not tmux_has_session(session):
        return "session_absent"
    ok, pane = tmux_capture_pane(session)
    if not ok:
        return "capture_failed"
    if any(marker in pane for marker in APPROVAL_DIALOG_MARKERS):
        return "awaiting_approval"

    first = body[:180].replace("\n", " ").replace('"', "'")
    msg = f"[MAIL seq={seq}] {first}... -> relay.py read --as {to_agent['name']}"
    subprocess.run(["tmux", "send-keys", "-t", session, msg], timeout=10, check=False)
    time.sleep(1.5)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], timeout=10, check=False)
    time.sleep(0.3)  # let the pane redraw before capturing -- see retry docstring

    ok, pane = tmux_capture_pane(session)
    if not ok:
        return "capture_failed"
    if _input_box_clear(pane, to_agent, seq):
        return "delivered"

    for delay in (2, 3):
        time.sleep(delay)
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], timeout=10, check=False)
        time.sleep(0.3)
        ok, pane = tmux_capture_pane(session)
        if not ok:
            return "capture_failed"
        if _input_box_clear(pane, to_agent, seq):
            return "delivered"

    return "stuck"


def check_safe_to_send(agent_cfg: dict, body: str, max_len: int = DEFAULT_MAX_BODY_LEN) -> None:
    session = agent_cfg["tmux_session"]
    if not tmux_has_session(session):
        raise RelaySendRefused(f"sessão tmux '{session}' não existe")

    ok, pane = tmux_capture_pane(session)
    if not ok:
        raise RelaySendRefused(f"capture-pane falhou para '{session}'")

    lines = pane.splitlines()
    prefix = agent_cfg["input_prefix"]
    placeholder = agent_cfg.get("placeholder", DEFAULT_PLACEHOLDER)
    box_lines = [l for l in lines if l.startswith(prefix)]
    if box_lines:
        last = box_lines[-1]
        if placeholder not in last:
            raise RelaySendRefused(
                f"caixa de input já tem texto por enviar: {last[:60]!r}"
            )

    recent = "\n".join(lines[-14:])
    if any(marker in pane for marker in APPROVAL_DIALOG_MARKERS):
        raise RelaySendRefused(f"'{session}' está a aguardar aprovação")
    if re.search(agent_cfg["busy_regex"], recent):
        raise RelaySendRefused(f"'{session}' está a meio de turno")

    if len(body) > max_len:
        raise RelaySendRefused(
            f"corpo tem {len(body)} chars, limite {max_len}. "
            "Põe o detalhe num ficheiro e manda um ponteiro."
        )


DEFAULT_QUOTA_MARKERS = (
    # generic
    "402", "insufficient balance", "out of quota", "payment required",
    # codex/OpenAI real wording (2026-09-09 pane): the healthy line
    # "You have N usage limit resets available" must NOT match, so these
    # are anchored to the blocked phrasing only.
    "hit your usage limit", "try again at", "upgrade to plus",
    # generic rate/context limits
    "quota exceeded", "rate limit", "context limit",
)
STATUS_SESSION_ABSENT = "session_absent"
STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_SCRIPT = "script_running"
STATUS_QUOTA = "quota_exhausted"
STATUS_APPROVAL = "blocked_on_approval"
STATUS_STUCK = "stuck_mid_turn"
_UNHEALTHY_PRESENT = (STATUS_QUOTA, STATUS_APPROVAL, STATUS_STUCK)


def _classify_doctor(agent_cfg: dict, signals: dict) -> dict:
    """Add a named failure-mode classification to the raw doctor signals.

    Distinguishes the three externally-indistinguishable failure modes:
    session absent / quota exhausted / input stuck. ``pane_tail`` is lowercased
    for marker scanning; per-agent ``quota_markers`` may extend the defaults.
    """
    session_ok = signals["session_ok"]
    pane = signals.get("pane_tail", "")
    if not session_ok:
        return {**signals, "status": STATUS_SESSION_ABSENT}

    lower = pane.lower()
    quota_markers = tuple(agent_cfg.get("quota_markers") or ()) + DEFAULT_QUOTA_MARKERS
    quota_hit = any(m in lower for m in quota_markers)
    approval_hit = any(m.lower() in lower for m in APPROVAL_DIALOG_MARKERS)

    # Script agents (no TUI: busy_regex NEVER_MATCHES and no placeholder) never
    # show a prompt — without this they are misclassified as stuck_mid_turn.
    is_script = (
        agent_cfg.get("busy_regex") == "NEVER_MATCHES"
        and not agent_cfg.get("placeholder")
    )
    if quota_hit:
        status = STATUS_QUOTA
    elif approval_hit:
        status = STATUS_APPROVAL
    elif is_script:
        status = STATUS_SCRIPT
    elif signals["busy_regex_matched"]:
        status = STATUS_BUSY
    elif signals["input_prefix_found"]:
        status = STATUS_IDLE
    else:
        status = STATUS_STUCK
    return {**signals, "status": status}


def doctor_check(agent_cfg: dict) -> dict:
    session = agent_cfg["tmux_session"]
    if not tmux_has_session(session):
        return {
            "session_ok": False,
            "pane_tail": "",
            "busy_regex_matched": False,
            "input_prefix_found": False,
            "status": STATUS_SESSION_ABSENT,
        }

    ok, pane = tmux_capture_pane(session)
    if not ok:
        pane = ""

    signals = {
        "session_ok": True,
        "pane_tail": pane,
        "busy_regex_matched": bool(re.search(agent_cfg["busy_regex"], pane)),
        "input_prefix_found": any(
            l.startswith(agent_cfg["input_prefix"]) for l in pane.splitlines()
        ),
    }
    return _classify_doctor(agent_cfg, signals)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay.py")
    p.add_argument("--config", default=None, help="caminho para relay.yaml/relay.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("send", "safe-send"):
        s = sub.add_parser(name)
        s.add_argument("--from", dest="from_", required=True)
        s.add_argument("--to", required=True)
        s.add_argument("--body", required=True)
        s.add_argument("--tokens", type=int, default=None)
        s.add_argument(
            "--require-delivery", action="store_true",
            help="return nonzero unless the tmux nudge is confirmed delivered",
        )
        s.add_argument(
            "--delivery-json", action="store_true",
            help="emit a machine-readable notification-delivery result",
        )

    for name in ("read", "peek"):
        r = sub.add_parser(name)
        r.add_argument("--as", dest="who", required=True)
        r.add_argument("--json", action="store_true",
                       help="emit raw JSONL records instead of formatted text")

    d = sub.add_parser("doctor")
    d.add_argument("--agent", required=True)
    redeliver = sub.add_parser("redeliver")
    redeliver.add_argument("--due", action="store_true",
                           help="compatibility marker: only due mail is swept")

    return p


def _cmd_send(config, args, guarded: bool) -> int:
    to_agent = get_agent(config, args.to)
    from_agent = get_agent(config, args.from_)
    box_dir = resolve_box_dir(config)

    if guarded:
        try:
            check_safe_to_send(to_agent, args.body)
        except RelaySendRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1

    seq = append_message(
        box_dir, args.to, args.from_, args.body,
        tokens=args.tokens, sender_repo_path=from_agent.get("repo_path"),
    )
    print(f"SENT seq={seq} to={args.to}")

    status = nudge(to_agent, seq, args.body)
    print(f"NUDGE {status}")
    if args.delivery_json:
        print("DELIVERY_RESULT " + json.dumps({
            "seq": seq,
            "nudge_status": status,
            "delivered": status == "delivered",
        }, sort_keys=True))
    # The mailbox append is durable regardless of nudge outcome, but callers
    # that need notification confirmation (liveness escalation) must not
    # mistake a successful process exit for delivery.
    if args.require_delivery and status != "delivered":
        return 2
    return 0


def _cmd_read(config, args, advance: bool) -> int:
    box_dir = resolve_box_dir(config)
    get_agent(config, args.who)  # validate recipient configuration
    unread = read_messages(box_dir, args.who, advance=advance)
    if not unread:
        print(f"(sem mensagens novas para {args.who})")
        return 0
    if getattr(args, "json", False):
        for rec in unread:
            print(json.dumps(rec, ensure_ascii=False))
        return 0
    for rec in unread:
        stale = ""
        sender_agent = next(
            (a for a in config.get("agents", []) if a.get("name") == rec["from"]), None
        )
        if sender_agent and sender_agent.get("repo_path"):
            current = head_of(sender_agent["repo_path"])
            if rec["head"] not in ("unknown", current):
                stale = " [HEAD DO REMETENTE MUDOU DESDE O ENVIO]"
        print(f"\n--- seq {rec['seq']} | {rec['ts_utc']} | de {rec['from']}{stale} ---")
        print(rec["body"])
    return 0


def _cmd_doctor(config, args) -> int:
    agent = get_agent(config, args.agent)
    result = doctor_check(agent)
    print(f"session_ok={result['session_ok']}")
    print(f"status={result['status']}")
    if not result["session_ok"]:
        print(f"ERRO: sessão tmux '{agent['tmux_session']}' não existe", file=sys.stderr)
        return 1
    if result["status"] in _UNHEALTHY_PRESENT:
        print(
            f"ERRO: agente '{args.agent}' em modo de falha '{result['status']}'",
            file=sys.stderr,
        )
        return 2
    print(f"busy_regex_matched={result['busy_regex_matched']} (normal variar consoante o estado actual)")
    print(f"input_prefix_found={result['input_prefix_found']}")
    print("--- pane tail ---")
    print(result["pane_tail"])
    return 0


def _cmd_redeliver(config) -> int:
    escalated = False
    for name, seq, status in redeliver_unread(config):
        print(f"RENUDGE to={name} seq={seq} {status}")
        escalated = escalated or status == "failed_cap"
    if escalated:
        print("ESCALATE: redelivery retry cap reached", file=sys.stderr)
        return 2
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except RelayConfigError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    try:
        if args.cmd == "send":
            return _cmd_send(config, args, guarded=False)
        if args.cmd == "safe-send":
            return _cmd_send(config, args, guarded=True)
        if args.cmd == "read":
            return _cmd_read(config, args, advance=True)
        if args.cmd == "peek":
            return _cmd_read(config, args, advance=False)
        if args.cmd == "doctor":
            return _cmd_doctor(config, args)
        if args.cmd == "redeliver":
            return _cmd_redeliver(config)
    except RelayConfigError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
