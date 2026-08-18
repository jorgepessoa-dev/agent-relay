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
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc  # datetime.UTC needs 3.11+; keep 3.10 compatible

REQUIRED_AGENT_FIELDS = ("tmux_session", "busy_regex", "input_prefix")
DEFAULT_PLACEHOLDER = "Type your message"
DEFAULT_MAX_BODY_LEN = 1600


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
            fh.seek(0)
            seq = sum(1 for _ in fh) + 1
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
    path = cursor_path(box_dir, name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(seq))
    tmp.replace(path)


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

    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    seen = read_cursor(box_dir, name)
    unread = [r for r in records if r["seq"] > seen]

    if advance and unread:
        write_cursor_atomic(box_dir, name, unread[-1]["seq"])

    return unread


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


def nudge(session: str, seq: int, body: str) -> str:
    """Push a mailbox notification into the recipient's tmux pane.

    Never returns "delivered" unless the session was confirmed present and
    the post-send capture succeeded — the original relay conflated "capture
    failed / session gone" with "message scrolled off screen", both of which
    look like an empty tail. That conflation caused a real false-positive
    (DeepCode caught it live during this module's own verification).
    """
    if not tmux_has_session(session):
        return "session_absent"

    first = body[:180].replace("\n", " ").replace('"', "'")
    msg = f"[MAIL seq={seq}] {first}... -> relay.py read --as <you>"
    subprocess.run(["tmux", "send-keys", "-t", session, msg], timeout=10, check=False)
    time.sleep(1.5)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], timeout=10, check=False)

    ok, pane = tmux_capture_pane(session)
    if not ok:
        return "capture_failed"

    tail = "\n".join(pane.splitlines()[-12:])
    if f"MAIL seq={seq}" in tail:
        return "stuck"
    return "delivered"


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
    if re.search(agent_cfg["busy_regex"], recent):
        raise RelaySendRefused(f"'{session}' está a meio de turno")

    if len(body) > max_len:
        raise RelaySendRefused(
            f"corpo tem {len(body)} chars, limite {max_len}. "
            "Põe o detalhe num ficheiro e manda um ponteiro."
        )


def doctor_check(agent_cfg: dict) -> dict:
    session = agent_cfg["tmux_session"]
    if not tmux_has_session(session):
        return {
            "session_ok": False,
            "pane_tail": "",
            "busy_regex_matched": False,
            "input_prefix_found": False,
        }

    ok, pane = tmux_capture_pane(session)
    if not ok:
        pane = ""

    return {
        "session_ok": True,
        "pane_tail": pane,
        "busy_regex_matched": bool(re.search(agent_cfg["busy_regex"], pane)),
        "input_prefix_found": any(
            l.startswith(agent_cfg["input_prefix"]) for l in pane.splitlines()
        ),
    }


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

    for name in ("read", "peek"):
        r = sub.add_parser(name)
        r.add_argument("--as", dest="who", required=True)

    d = sub.add_parser("doctor")
    d.add_argument("--agent", required=True)

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

    status = nudge(to_agent["tmux_session"], seq, args.body)
    print(f"NUDGE {status}")
    return 0


def _cmd_read(config, args, advance: bool) -> int:
    box_dir = resolve_box_dir(config)
    agent = get_agent(config, args.who)
    unread = read_messages(box_dir, args.who, advance=advance)
    if not unread:
        print(f"(sem mensagens novas para {args.who})")
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
    if not result["session_ok"]:
        print(f"ERRO: sessão tmux '{agent['tmux_session']}' não existe", file=sys.stderr)
        return 1
    print(f"busy_regex_matched={result['busy_regex_matched']} (normal variar consoante o estado actual)")
    print(f"input_prefix_found={result['input_prefix_found']}")
    print("--- pane tail ---")
    print(result["pane_tail"])
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
    except RelayConfigError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
