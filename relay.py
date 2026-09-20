#!/usr/bin/env python3
"""agent-relay — reliable JSONL mailbox between N tmux-hosted AI agents.

Generalized from /opt/agent-relay on the trading-advisor droplet. See
docs/superpowers/specs/2026-08-18-agent-relay-design.md for the design
rationale and the concrete bugs (message loss, false-positive delivery,
race conditions) this fixes.
"""
import argparse
import fcntl
import gzip
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
# A current approval dialog may display a multi-line command between its
# opening marker and the prompt; the real capture for F-980 had ten such
# lines after the opening marker but lost the footer.  Four lines is enough
# for quota text, but falsely reports that dialog as idle.  This remains a
# bounded current-interaction window, not a scan of quoted scrollback.
APPROVAL_SCAN_TAIL_LINES = 12
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


def _validate_agent(agent: dict, name: str) -> dict:
    missing = [f for f in REQUIRED_AGENT_FIELDS if f not in agent]
    if missing:
        raise RelayConfigError(
            f"agente '{name}' não tem os campos obrigatórios: {missing}. "
            "tmux_session/busy_regex/input_prefix não têm default partilhado."
        )
    return agent


def get_agent(config: dict, name: str) -> dict:
    """Resolve a name to its agent. The CANONICAL name wins; an alias resolves to the same agent.

    LANE A (2026-09-20): an agent may carry `agent_id` (canonical identity), `model`, `role` and `aliases`. `name` is
    NOT renamed by that change, because `name` is the key of the mailbox, the seq counter and the cursor
    (to_{name}.jsonl, .seq_{name}, .cursor_{name}) - renaming it would break the mailbox history, which the criteria
    forbid. So the alias is resolved HERE and the callers write with the RESOLVED agent's name, which keeps one mailbox
    per agent and leaves the history alone by construction.

    The exact match is tried first so that an alias can never shadow a declared name: a config mistake that lists an
    existing name as somebody else's alias resolves to the agent that OWNS the name, which is the safe direction.
    """
    for agent in config.get("agents", []):
        if agent.get("name") == name:
            return _validate_agent(agent, name)
    for agent in config.get("agents", []):
        if name in (agent.get("aliases") or []):
            return _validate_agent(agent, name)
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


def high_seq(box_dir: Path, name: str) -> int:
    """Highest seq ever issued in a mailbox: live records AND archived (.gz).

    Counting lines is what corrupted codex's counter (2026-09-10): after rotation
    the live file is shorter than the history, so a line count is below the real
    high-water mark and new messages are born behind the cursor.
    """
    high = 0
    live = box_dir / f"to_{name}.jsonl"
    if live.exists():
        for line in live.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                high = max(high, int(json.loads(line).get("seq", 0)))
            except (ValueError, json.JSONDecodeError):
                continue
    for gz in sorted(box_dir.glob(f"*to_{name}*.gz")) + sorted(box_dir.glob(f"archive/*to_{name}*.gz")):
        if not gz.exists():
            continue
        try:
            with gzip.open(gz, "rt", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        high = max(high, int(json.loads(line).get("seq", 0)))
                    except (ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            continue
    return high


def append_message(
    box_dir: Path,
    to_name: str,
    from_name: str,
    body: str,
    tokens: int | None = None,
    sender_repo_path: str | None = None,
) -> int:
    # F-987: a durable seq and a successful transport result are not evidence
    # that anything was communicated.  Refuse before creating a mailbox or
    # allocating a sequence, and keep this at the common writer boundary so
    # neither `send`, `safe-send`, nor a future direct caller can bypass it.
    if not body.strip():
        raise RelaySendRefused("empty or whitespace-only body: nothing to send")
    box_dir.mkdir(parents=True, exist_ok=True)
    path = mailbox_path(box_dir, to_name)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fh = os.fdopen(fd, "r+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            # Seq comes from a DURABLE counter, not the line count: after a
            # mailbox rotation the live file is shorter, and counting lines
            # re-issues already-used seqs (cursor corruption). MEASURED BUG
            # 2026-09-10: the legacy fallback seeded the counter from the LINE
            # COUNT, so codex's box got .seq=503 while its real max(seq) was 649 —
            # every new message was born BELOW the cursor (647) and never seen.
            # The seed is now the real high-water mark over LIVE + ARCHIVED
            # (.gz) records, never a count of lines.
            counter = seq_path(box_dir, to_name)
            seq = None
            if counter.exists():
                try:
                    seq = int(counter.read_text().strip() or 0) + 1
                except ValueError:
                    seq = None
            if seq is None or seq <= high_seq(box_dir, to_name):
                seq = high_seq(box_dir, to_name) + 1
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



@contextmanager
def _consume_lock(box_dir: Path, name: str):
    """EXCLUSIVE consumption lock for one mailbox key (F5, 2026-09-20).

    WHY: `read_messages` read the unread set and only afterwards advanced the cursor, so two
    consumers overlapping in time BOTH read the same messages — measured: 10 reads for 5 messages,
    each message delivered twice. With ≥2 live sessions of one agent (or a second consumer added
    deliberately) that is the same work done twice, and for a seat that ACTS on a message it is a
    duplicate action. LANE C's `=name` fix removed the accidental route to two sessions; this lock
    covers every other route.

    SCOPE OF THE FIX, stated so it is not overread: overlapping consumers now PARTITION the unread
    set (the later one finds what the earlier has not taken). Sequential behaviour is unchanged, and
    a consumer that arrives late still sees anything the earlier one left — nothing can be lost.
    `peek` deliberately does NOT take this lock: it is a reader, not a consumer.

    The lock file is distinct from the mailbox and never matches the mailbox/archive globs.
    """
    lock_path = box_dir / f".lock_{name}"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:
        # Bounded polite wait, then a BLOCKING acquire: a timeout that gave up would re-create the
        # very defect (two consumers taking the same set), so waiting is the correct failure mode.
        for _ in range(40):                       # ~2s of non-blocking attempts
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.05)
        else:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

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
    if not advance:
        seen = read_cursor(box_dir, name)
        return [r for r in records if r["seq"] > seen]

    # BOTH the read of the cursor AND the advance happen inside the lock. My first version released
    # the lock before writing the cursor, which re-created the race the lock exists to remove: the
    # second consumer would acquire the lock, still see the old cursor, and take the same set again.
    with _consume_lock(box_dir, name):
        seen = read_cursor(box_dir, name)
        unread = [r for r in records if r["seq"] > seen]
        if unread:
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
        # Scheduler is a sender/cron role, not a mailbox consumer.  Retrying a
        # message to it can only burn the bounded retry budget forever.
        role = consumer_role(agent)
        if role in ("none", "api", "unknown"):
            # none: sender/cron only. api: durable mailbox, no nudge needed.
            # unknown: no consumer can be established; send already refuses it.
            continue
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




def tmux_target(session: str) -> str:
    """EXACT session target for `has-session` / `kill-session`: `=name`.

    WHY (2026-09-20, LANE C): tmux resolves a BARE name by PREFIX. Measured with two real
    sessions — with `lane-probe` absent and `lane-probe-builder` present, a bare
    `has-session -t lane-probe` SUCCEEDS (false positive) and a bare `kill-session -t
    lane-probe` closes `lane-probe-builder` silently, printing success (it killed the glm
    session this morning).

    SCOPE OF THE CLAIM, corrected after review (codex seq 2847): `=NAME` is the exact
    SELECTOR when passed to `-t`. It says nothing about other arguments: `new -s NAME` /
    `new-session -s NAME` is CREATION, not a selector, and there is NO measurement showing
    that `=` applies there — so no claim is made about it.
    """
    return f"={session}"


def tmux_pane_target(session: str) -> str:
    """EXACT PANE target for `capture-pane` / `send-keys`: `=name:` — MEASURED, not assumed.

    The trailing colon is not decoration: tmux parses these arguments as PANE targets
    (`session:window.pane`), and `=name` WITHOUT the colon fails with "can't find pane"
    even when the session exists. Measured, with a live session:
      capture-pane -t "=probe"   -> rc!=0, can't find pane
      capture-pane -t "=probe:"  -> rc=0, content read
      send-keys    -t "=probe"   -> rc!=0, keystrokes NOT delivered
      send-keys    -t "=probe:"  -> rc=0, keystrokes delivered
    and pointing at an absent session whose PREFIX exists gives rc!=0 with NO text
    leaking into the prefixed session — which is the property the lane required.
    """
    return f"={session}:"

def tmux_has_session(session: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", tmux_target(session)],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def tmux_capture_pane(session: str) -> tuple:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux_pane_target(session), "-p"],
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


def nudge(to_agent: dict, seq: int, body: str, *, require_ready: bool = False) -> str:
    """Push a mailbox notification into the recipient's tmux pane.

    F-1017: with require_ready=True (the NORMAL send path) this refuses to TYPE while
    the pane is mid-turn or its input box already holds unsent text, returning the
    typed status "pending" instead. The mail is already appended durably by the
    caller, so PENDING is not an error and the existing redelivery path nudges later.
    session_absent / capture_failed / awaiting_approval are detected below and never
    type, so they keep their own distinct statuses in both modes.

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
    # F-1017: with require_ready (the NORMAL send path) do not TYPE while the pane is
    # mid-turn or its input box already holds unsent text. These are the SAME
    # single-source helpers check_safe_to_send uses, so this gate cannot drift from the
    # pre-append refusal. PENDING is not an error - the caller already appended the
    # mail durably; redelivery nudges later.
    # ROOT DECLARED (same root the independent VETO found): a readiness predicate WEAKER
    # than the claim it supports. The claim is "this pane is ready to be typed into"; the
    # predicate must therefore require POSITIVE evidence (the input box on screen), not
    # merely the ABSENCE of a busy marker - an occupied pane, or one whose marker scrolled
    # out of the tail, satisfied the old predicate and was typed into anyway. METHOD
    # HARDENED: for each guard, name the object that satisfies the predicate and fails the
    # claim - here "a pane with no prompt and no busy match in the tail" - and keep it as a
    # test. busy/unsent remain ADDITIONAL blocks; a missing prompt is PENDING, fail-closed.
    if require_ready and (
        not _input_prompt_visible(to_agent, pane)
        or _unsent_input_text(to_agent, pane) is not None
        or _pane_mid_turn(to_agent, pane)
    ):
        return "pending"

    first = body[:180].replace("\n", " ").replace('"', "'")
    msg = f"[MAIL seq={seq}] {first}... -> relay.py read --as {to_agent['name']}"
    subprocess.run(["tmux", "send-keys", "-t", tmux_pane_target(session), msg],
                   timeout=10, check=False)
    time.sleep(1.5)
    subprocess.run(["tmux", "send-keys", "-t", tmux_pane_target(session), "Enter"],
                   timeout=10, check=False)
    time.sleep(0.3)  # let the pane redraw before capturing -- see retry docstring

    ok, pane = tmux_capture_pane(session)
    if not ok:
        return "capture_failed"
    if _input_box_clear(pane, to_agent, seq):
        return "delivered"

    for delay in (2, 3):
        time.sleep(delay)
        subprocess.run(["tmux", "send-keys", "-t", tmux_pane_target(session), "Enter"],
                       timeout=10, check=False)
        time.sleep(0.3)
        ok, pane = tmux_capture_pane(session)
        if not ok:
            return "capture_failed"
        if _input_box_clear(pane, to_agent, seq):
            return "delivered"

    return "stuck"


def _unsent_input_text(agent_cfg: dict, pane: str) -> str | None:
    """The last input-box line when it already holds UNSENT text, else None.

    F-1017 single source of truth: shared by check_safe_to_send (the pre-append
    refusal used by `safe-send` and redelivery) and by nudge's readiness gate (the
    normal path), so the two can never diverge on what "the box already has text"
    means.
    """
    box_lines = [l for l in pane.splitlines() if l.startswith(agent_cfg["input_prefix"])]
    if not box_lines:
        return None
    last = box_lines[-1]
    if agent_cfg.get("placeholder", DEFAULT_PLACEHOLDER) in last:
        return None
    return last


def _pane_mid_turn(agent_cfg: dict, pane: str) -> bool:
    """True when the pane's recent tail matches the recipient's busy_regex.

    F-1017 single source of truth, shared exactly as _unsent_input_text is.
    """
    return bool(re.search(agent_cfg["busy_regex"], "\n".join(pane.splitlines()[-14:])))


def _input_prompt_visible(agent_cfg: dict, pane: str) -> bool:
    """POSITIVE evidence that the agent's input box is on screen (F-1017).

    The independent VETO proved the ROOT: *a readiness predicate WEAKER than the claim
    it supports*. The absence of a busy marker does NOT establish readiness - an occupied
    pane (a live foreground process, or a marker that scrolled out of the tail) has no
    busy match yet is NOT at its input box, and typing would go nowhere. So the gate now
    demands POSITIVE evidence: a tail line starting with the recipient's input_prefix.
    Measured on the live panes: deepcode shows ">   Type your message...", codex
    "Ask Codex to do anything", coordinator its chevron prompt - all inside the tail
    window. An empty prefix means the agent has no observable input box (role none/api),
    where this requirement does not apply.
    """
    prefix = agent_cfg.get("input_prefix") or ""
    if not prefix:
        return True  # no observable input box configured -> requirement not applicable
    return any(l.startswith(prefix) for l in pane.splitlines()[-14:])


def check_safe_to_send(agent_cfg: dict, body: str, max_len: int = DEFAULT_MAX_BODY_LEN) -> None:
    session = agent_cfg["tmux_session"]
    if not tmux_has_session(session):
        raise RelaySendRefused(f"sessão tmux '{session}' não existe")

    ok, pane = tmux_capture_pane(session)
    if not ok:
        raise RelaySendRefused(f"capture-pane falhou para '{session}'")

    last = _unsent_input_text(agent_cfg, pane)
    if last is not None:
        raise RelaySendRefused(
            f"caixa de input já tem texto por enviar: {last[:60]!r}"
        )

    if any(marker in pane for marker in APPROVAL_DIALOG_MARKERS):
        raise RelaySendRefused(f"'{session}' está a aguardar aprovação")
    if _pane_mid_turn(agent_cfg, pane):
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

    # Pane scrollback contains quoted mail.  Only the current interaction tail
    # can establish an approval/quota block (agent_liveness uses the same rule).
    lines = [line for line in pane.splitlines() if line.strip()]
    # WHITESPACE IS COLLAPSED before matching, because the pane WRAPS the message:
    # measured 2026-09-12, codex printed "...purchase more credits or try" / "again at
    # 1:09 PM.", so the marker "try again at" never matched the raw "\n"-joined tail and
    # a quota-blocked agent was classified IDLE (the watchdog then asked it for a
    # priority instead of escalating the block). Same class as the doctor confusion:
    # session/prompt state is not the ability to respond.
    lower = " ".join("\n".join(lines[-4:]).split()).lower()
    approval_tail = " ".join("\n".join(lines[-APPROVAL_SCAN_TAIL_LINES:]).split()).lower()
    quota_markers = tuple(agent_cfg.get("quota_markers") or ()) + DEFAULT_QUOTA_MARKERS
    quota_hit = any(" ".join(m.split()).lower() in lower for m in quota_markers)
    approval_hit = any(" ".join(m.lower().split()) in approval_tail
                       for m in APPROVAL_DIALOG_MARKERS)

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


def _mailbox_rows(box_dir: Path, name: str) -> list[dict]:
    path = mailbox_path(box_dir, name)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _redelivery_entry(box_dir: Path, name: str, seq: int) -> dict:
    state = _read_redelivery_state(box_dir)
    entry = (state.get("entries") or {}).get(_entry_key(name, seq))
    return entry if isinstance(entry, dict) else {}


def _row_age_s(row: dict, now: datetime) -> float | None:
    return _record_age_s(row, now)


def delivery_record(box_dir: Path, name: str, seq: int, *, now: datetime | None = None) -> dict:
    """The transport fact for one (recipient, seq).

    `mailbox_persisted` is whether the durable mailbox has the row. `notification_state` is one of delivered, pending or
    unknown, and it is computed from evidence that already exists: the read cursor having passed the sequence (a read
    implies the notification happened) or a redelivery attempt that reported delivered. `unknown` is for a sequence the
    mailbox does not have - absence is not a delivery.

    This function PROVES NOTHING ABOUT WORK. A delivered notification says the seq left the recipient's input box; it
    says nothing about reading, understanding, or the request being carried out. That is the outcome receipt's job, and
    the design keeps them separate on purpose.
    """
    moment = now or datetime.now(UTC)
    rows = _mailbox_rows(box_dir, name)
    row = next((r for r in rows if r.get("seq") == seq), None)
    if row is None:
        return {"recipient": name, "seq": seq, "mailbox_persisted": False,
                "notification_state": "unknown", "notification_attempts": [],
                "pending_age_seconds": None, "last_reason": "no mailbox row for this sequence"}

    entry = _redelivery_entry(box_dir, name, seq)
    attempts: list[dict] = []
    if entry:
        attempts.append({"at": entry.get("last_nudged_at"), "result": entry.get("last_status")})

    cursor = read_cursor(box_dir, name)
    status = str(entry.get("last_status") or "")
    if status == "delivered" or cursor >= seq:
        state, reason = "delivered", (status or f"the read cursor reached {cursor}")
    else:
        state = "pending"
        reason = status or "no delivery evidence recorded yet"

    return {
        "recipient": name, "seq": seq,
        "mailbox_persisted": True,
        "notification_state": state,
        "notification_attempts": attempts,
        "pending_age_seconds": None if state == "delivered" else _row_age_s(row, moment),
        "created_at": row.get("ts_utc"),
        "last_reason": reason,
    }


def pending_deliveries(box_dir: Path, *, limit: int = 20, now: datetime | None = None) -> list[dict]:
    """A BOUNDED list of sequences whose notification has not been confirmed, OLDEST FIRST.

    The bound and the ordering are the point: the aged row is the one stuck, and a list that cannot be bounded is a list
    nobody reads. The design asks for aged pending rows to surface without a manual `doctor` loop.
    """
    moment = now or datetime.now(UTC)
    out: list[dict] = []
    for path in sorted(box_dir.glob("to_*.jsonl")):
        name = path.name[len("to_"): -len(".jsonl")]
        for row in _mailbox_rows(box_dir, name):
            seq = row.get("seq")
            if not isinstance(seq, int):
                continue
            record = delivery_record(box_dir, name, seq, now=moment)
            if record["notification_state"] != "delivered":
                out.append(record)
    out.sort(key=lambda r: (r.get("pending_age_seconds") or 0), reverse=True)
    return out[: max(0, int(limit))]


def sender_report(*, delivery_record: dict) -> str:
    """What a sender is ALLOWED to say, derived from the record rather than chosen by the sender.

    The design's reporting rule, and the sentence this exists to prevent: until the notification is confirmed, the only
    honest report is "mailbox persisted; notification pending". After it is confirmed, "notification confirmed" - and
    never "processed", because that is a different fact with a different producer (the outcome receipt).
    """
    state = delivery_record.get("notification_state")
    seq = delivery_record.get("seq")
    # THE RECIPIENT IS PART OF THE REPORT. Found by running the real list: without it, four rows reading
    # "pending 740711s" could not be attributed to a mailbox, so an operator could see that something was stuck for
    # eight days and not know whose queue it was. A receipt that cannot say WHERE is half a receipt.
    who = delivery_record.get("recipient") or "?"
    if state == "delivered":
        return f"{who} seq {seq}: mailbox persisted; notification confirmed (not processed)"
    if state == "pending":
        age = delivery_record.get("pending_age_seconds")
        age_txt = f", pending {age:.0f}s" if isinstance(age, (int, float)) else ""
        return f"{who} seq {seq}: mailbox persisted; notification pending{age_txt}"
    return f"{who} seq {seq}: no mailbox row; nothing persisted to report"


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

    check = sub.add_parser("check-consumer", help="THE consumer role rule")
    check.add_argument("agent")
    d = sub.add_parser("doctor")
    d.add_argument("--agent", required=True)
    redeliver = sub.add_parser("redeliver")
    ds = sub.add_parser("delivery-status", help="the transport fact for one (recipient, seq)")
    ds.add_argument("--recipient", required=True)
    ds.add_argument("--seq", type=int, required=True)
    pend = sub.add_parser("pending", help="bounded list of unconfirmed notifications, oldest first")
    pend.add_argument("--limit", type=int, default=20)

    redeliver.add_argument("--due", action="store_true",
                           help="compatibility marker: only due mail is swept")

    return p


def _cmd_send(config, args, guarded: bool) -> int:
    to_agent = get_agent(config, args.to)
    from_agent = get_agent(config, args.from_)
    box_dir = resolve_box_dir(config)

    # This check has no side effect and avoids probing a recipient for a body
    # that the common writer will inevitably reject.
    if not args.body.strip():
        print("REFUSED: empty or whitespace-only body: nothing to send", file=sys.stderr)
        return 1

    # F-977 VETO remediation: an INVALID recipient must not be delivered to at all -
    # no mailbox, no seq, no SENT. This runs BEFORE the append, unlike `none`, whose
    # refusal is a POLICY for a KNOWN role and stays after it so the sender's mistake
    # is recorded (F-979). The cases differ: `none` is declared and refused by design;
    # `unknown` is a configuration error, and creating a mailbox for it would BE the
    # delivery the rule forbids. Preserving evidence does not authorise the append.
    role = consumer_role(to_agent)
    if role == "unknown":
        print(f"REFUSED: {args.to!r} declares mail_consumer="
              f"{to_agent.get('mail_consumer')!r} and has no tmux_session, so the role is unknown and no consumer can be established; "
              f"nothing was written (F-977). "
              f"{REL_TEMPLATE_HINT}",
              file=sys.stderr)
        return 1

    if guarded:
        try:
            check_safe_to_send(to_agent, args.body)
        except RelaySendRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1

    seq = append_message(
        box_dir, to_agent["name"], from_agent["name"], args.body,
        tokens=args.tokens, sender_repo_path=from_agent.get("repo_path"),
    )
    print(f"SENT seq={seq} to={args.to}")

    if role == "none":
        print("NUDGE no_consumer", file=sys.stderr)
        return 1
    if role == "api":
        print("NUDGE api_consumer")
        return 0

    # F-1017: the NORMAL (unguarded) path asks nudge to CONFIRM READINESS before it
    # types (require_ready). The mail is already appended durably, so an unready pane
    # is NOT an error: it becomes the typed PENDING state and the existing redelivery
    # path nudges later. `safe-send` already refused PRE-append in the guarded branch
    # above, so its contract is unchanged. The gate lives INSIDE nudge, the single
    # place that types - so a busy/dirty pane gets ZERO send-keys.
    status = nudge(to_agent, seq, args.body, require_ready=not guarded)
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
    # LANE A: resolve the alias before touching the mailbox, or an alias read would look in a different box
    # from the one the alias write used - the write-only fix would be half a fix.
    unread = read_messages(box_dir, get_agent(config, args.who)["name"], advance=advance)
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


def _cmd_delivery_status(config, args) -> int:
    """The transport fact for one (recipient, seq), plus what a SENDER may say about it.

    Separate from `doctor` on purpose: doctor has no sequence input and is session health, never a receipt. Asking
    "did seq N leave the box" is this command's whole job."""
    box_dir = resolve_box_dir(config)
    get_agent(config, args.recipient)
    record = delivery_record(box_dir, get_agent(config, args.recipient)["name"], args.seq)
    if getattr(args, "json", False):
        print(json.dumps(record, sort_keys=True))
    else:
        print(json.dumps(record, indent=2, sort_keys=True))
        print(sender_report(delivery_record=record))
    return 0


def _cmd_pending(config, args) -> int:
    """A bounded list of unconfirmed notifications, oldest first - the stuck ones surface without a doctor loop."""
    box_dir = resolve_box_dir(config)
    rows = pending_deliveries(box_dir, limit=args.limit)
    if not rows:
        print("pending: none (every persisted sequence has a confirmed notification)")
        return 0
    for record in rows:
        print(sender_report(delivery_record=record))
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


#: The recognised consumer roles. ANYTHING else is `unknown` and is refused, because
#: "no tmux" used to mean "api" and that let a mailbox nobody can poll look like a
#: working channel (F-977).
CONSUMER_ROLES = ("tmux", "api", "none")

#: Named in every refusal, so an operator who recovered from the generic
#: relay.example.yaml learns which tracked template is correct (F-977).
REL_TEMPLATE_HINT = "the tracked recovery template is relay.yaml.example"


def consumer_role(agent: dict) -> str:
    """THE ONE consumer-configuration rule (F-977).

    `tmux` gets nudges; `api` is a durable mailbox that is polled and needs no
    nudge; `none` is a sender/cron role whose delivery is refused. A recipient with
    no pane and no recognised role is `unknown`, and both delivery and polling must
    refuse it BEFORE treating it as a consumer. Callers must not re-implement this:
    the bridge asks for it through the `check-consumer` query instead, because a
    duplicated rule is exactly how these two callers came to disagree.
    """
    role = str(agent.get("mail_consumer") or "").strip()
    if role in CONSUMER_ROLES:
        return role
    if agent.get("tmux_session"):
        return "tmux"
    return "unknown"


def check_consumer(config: dict, name: str) -> dict:
    """Structured answer for the CLI query: role, validity, and why."""
    agent = next((a for a in config.get("agents", []) if a.get("name") == name), None)
    if agent is None:
        return {"agent": name, "role": "unknown", "valid": False,
                "reason": "no such agent in the relay configuration"}
    role = consumer_role(agent)
    valid = role in ("tmux", "api")
    reason = {
        "tmux": "interactive pane: nudged, delivery allowed",
        "api": "durable mailbox: polled by the agent, no nudge, delivery allowed",
        "none": "sender/cron only: delivery is refused by design",
        "unknown": ("no tmux_session and mail_consumer is not one of "
                    + "/".join(CONSUMER_ROLES) + ": no consumer can be established"),
    }[role]
    return {"agent": name, "role": role, "valid": valid, "reason": reason,
            "mail_consumer": agent.get("mail_consumer")}


def _cmd_check_consumer(config: dict, args) -> int:
    """The query the bridge invokes by subprocess instead of keeping its own rule."""
    answer = check_consumer(config, args.agent)
    print(json.dumps(answer, ensure_ascii=False))
    if answer["valid"]:
        return 0
    print(f"REFUSED: {args.agent!r} is not a valid consumer: {answer['reason']} "
          f"{REL_TEMPLATE_HINT}", file=sys.stderr)
    return 1


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
        if args.cmd == "check-consumer":
            return _cmd_check_consumer(config, args)
        if args.cmd == "redeliver":
            return _cmd_redeliver(config)
        if args.cmd == "delivery-status":
            return _cmd_delivery_status(config, args)
        if args.cmd == "pending":
            return _cmd_pending(config, args)
    except RelayConfigError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
