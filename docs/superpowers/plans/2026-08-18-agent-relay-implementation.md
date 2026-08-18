# agent-relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `relay.py`, a standalone, dependency-free-by-default Python script that gives N named AI agents (each in its own tmux session) a reliable JSONL mailbox with automatic tmux nudge and a busy/stuck send-guard, generalized from the working `/opt/agent-relay` mechanism on the trading-advisor droplet.

**Architecture:** Single-file CLI (`relay.py`) with pure functions for config, mailbox I/O, and tmux interaction, wired together by an `argparse` subcommand dispatcher. Config lives in a per-project `relay.yaml` (or `relay.json` if PyYAML isn't installed) describing each agent's tmux session and TUI-specific patterns. State (mailbox JSONL files + read cursors) lives in a `box_dir` next to the config.

**Tech Stack:** Python 3.10+ stdlib (`argparse`, `json`, `fcntl`, `subprocess`, `re`, `pathlib`, `os`, `time`, `datetime`). Optional `pyyaml` only if the project uses `relay.yaml` instead of `relay.json`. `pytest` for tests (dev-only, not a runtime dependency).

## Global Constraints

(Copied verbatim from `docs/superpowers/specs/2026-08-18-agent-relay-design.md`, commit `1e7e9c0` — every task below implicitly must honor these.)

- N agentes nomeados, não 2 fixos. Ficheiro standalone, sem instalação — cada projecto copia `relay.py`.
- `tmux_session`, `busy_regex`, `input_prefix` são obrigatórios por agente em `relay.yaml`/`relay.json` — sem default silencioso partilhado entre agentes.
- `box_dir` resolve contra a CWD do processo, não contra a localização do script — o comando deve correr da raiz do projecto (onde está o ficheiro de config); se o ficheiro de config não for encontrado na CWD, falha com erro legível.
- Mailbox: um `to_<nome>.jsonl` por destinatário, append-only, criado com permissões `0600` (assume mesmo utilizador OS — declarado, não escondido).
- Append protegido por `fcntl.flock` **exclusivo**; `read`/`peek` tomam `flock` **partilhado**.
- `seq` estritamente ascendente e sem duplicados sob sends concorrentes.
- Cursor por agente (`.cursor_<nome>`) escrito por write-temp-then-rename (atómico).
- Campo `head` = git HEAD curto do remetente no momento do `send`, comparado no `read` contra o HEAD **actual** do `repo_path` do remetente (não do leitor).
- Read receipt: fora de âmbito — não implementar.
- Retry = mensagem nova, sem deduplicação — não implementar dedup.
- Nudge: sequência `has-session` → `send-keys` texto → sleep → `send-keys` Enter → `capture-pane` com verificação de returncode → só então veredicto. Nunca reportar "delivered" se `has-session` falhar ou `capture-pane` falhar — reportar "sessão ausente" / "captura falhou" distintamente.
- `safe-send` recusa se: (a) caixa de input já tem texto não-placeholder, (b) `busy_regex` casa nas últimas 14 linhas do pane, (c) corpo > 1600 caracteres. Excepção: se a última linha da caixa de input contém o placeholder configurado (default `"Type your message"`), NÃO conta como texto pendente.
- `beat` não existe no v1 — heartbeat compõe-se com `send --body "[heartbeat] tokens=N"`.
- `doctor --agent X`: verifica `tmux has-session`, captura o pane, reporta se `busy_regex`/`input_prefix` aparentam estar calibrados; falha com erro claro se a sessão não existir.

---

## File Structure

- **Create:** `relay.py` — todo o mecanismo (config, mailbox, tmux, CLI). Ficheiro único, standalone, por decisão do spec.
- **Create:** `relay.example.yaml` — exemplo de config com 2 agentes (documenta todos os campos).
- **Create:** `relay.example.json` — o mesmo exemplo em JSON, para quem não quer a dependência `pyyaml`.
- **Create:** `tests/test_relay.py` — suite de testes (mocka `subprocess`/`tmux`, usa `tmp_path` do pytest para mailbox real em disco).
- **Create:** `README.md` — instalação (copiar o ficheiro), configuração, comandos, e a proveniência (extraído do trading-advisor).
- **Create:** `LICENSE` — MIT.
- **Create:** `.gitignore` — `__pycache__/`, `*.pyc`, `.pytest_cache/`.

---

### Task 1: Config loading — `relay.yaml`/`relay.json` + validação obrigatória por agente

**Files:**
- Create: `relay.py` (funções: `RelayConfigError`, `load_config`, `get_agent`)
- Test: `tests/test_relay.py`

**Interfaces:**
- Produces: `load_config(config_path: str | None = None) -> dict` — lê `relay.yaml` (requer `pyyaml`) ou `relay.json` (stdlib) a partir da CWD (ou de `config_path` se dado); levanta `RelayConfigError` com mensagem legível se nenhum ficheiro existir.
- Produces: `get_agent(config: dict, name: str) -> dict` — devolve o dict do agente `name`; levanta `RelayConfigError` se o agente não existir ou lhe faltar `tmux_session`, `busy_regex` ou `input_prefix`.
- Produces: `RelayConfigError(Exception)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_relay.py
import json
import os
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jorge/projects/agent-relay && python3 -m pytest tests/test_relay.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'relay'` (or `ImportError`) — `relay.py` doesn't exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
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


if __name__ == "__main__":
    sys.exit(0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jorge/projects/agent-relay && python3 -m pytest tests/test_relay.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd /home/jorge/projects/agent-relay
git add relay.py tests/test_relay.py
git commit -m "relay: config loading with mandatory per-agent fields"
```

---

### Task 2: `box_dir` resolution + git HEAD helper

**Files:**
- Modify: `relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: nothing new from Task 1.
- Produces: `resolve_box_dir(config: dict) -> Path` — `Path(config.get("box_dir", "./agent-relay-mail"))` resolvido contra a CWD (não contra a localização de `relay.py`), com `.resolve()` para caminho absoluto.
- Produces: `head_of(repo_path: str | None) -> str` — `git -C <repo_path> rev-parse --short HEAD`; devolve `"unknown"` se `repo_path` for `None`, não existir, ou o comando falhar.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_relay.py -v -k "box_dir or head_of"`
Expected: FAIL with `AttributeError: module 'relay' has no attribute 'resolve_box_dir'`

- [ ] **Step 3: Write minimal implementation**

Add to `relay.py` (after `get_agent`):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_relay.py -v -k "box_dir or head_of"`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add relay.py tests/test_relay.py
git commit -m "relay: box_dir resolution + git HEAD helper"
```

---

### Task 3: Mailbox write — atomic `seq`, exclusive lock, 0600 creation

**Files:**
- Modify: `relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `resolve_box_dir` (Task 2).
- Produces: `append_message(box_dir: Path, to_name: str, from_name: str, body: str, tokens: int | None = None, sender_repo_path: str | None = None) -> int` — devolve o `seq` atribuído. Cria `box_dir` se preciso, cria o ficheiro `to_<to_name>.jsonl` com `0600` na primeira escrita (via `os.open` com `O_CREAT` + `mode=0o600`, para não haver janela de corrida entre criar e definir permissões).
- Produces: `mailbox_path(box_dir: Path, name: str) -> Path` = `box_dir / f"to_{name}.jsonl"`.

- [ ] **Step 1: Write the failing tests**

```python
import stat
import threading


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_relay.py -v -k append_message`
Expected: FAIL with `AttributeError: module 'relay' has no attribute 'append_message'`

- [ ] **Step 3: Write minimal implementation**

Add to `relay.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_relay.py -v -k append_message`
Expected: PASS (4 tests, incluindo a concorrência de 20 threads)

- [ ] **Step 5: Commit**

```bash
git add relay.py tests/test_relay.py
git commit -m "relay: mailbox append with flock, atomic seq, 0600 creation"
```

---

### Task 4: Mailbox read — shared lock, cursor read/advance atómico, `peek` vs `read`

**Files:**
- Modify: `relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `mailbox_path`, `append_message` (Task 3).
- Produces: `cursor_path(box_dir: Path, name: str) -> Path` = `box_dir / f".cursor_{name}"`.
- Produces: `read_cursor(box_dir: Path, name: str) -> int` — 0 se o ficheiro não existir.
- Produces: `write_cursor_atomic(box_dir: Path, name: str, seq: int) -> None` — escreve num `.tmp` e faz `rename` (atómico).
- Produces: `read_messages(box_dir: Path, name: str, advance: bool = True) -> list[dict]` — lê com `flock` partilhado, devolve só as não-lidas (`seq > cursor`), avança o cursor se `advance=True`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_relay.py -v -k "read_messages or cursor"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation**

Add to `relay.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_relay.py -v -k "read_messages or cursor"`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add relay.py tests/test_relay.py
git commit -m "relay: mailbox read with shared lock + atomic cursor advance"
```

---

### Task 5: tmux primitives + corrected nudge sequence

**Files:**
- Modify: `relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `tmux_has_session(session: str) -> bool`.
- Produces: `tmux_capture_pane(session: str) -> tuple[bool, str]` — `(sucesso, texto)`; `sucesso=False` se `capture-pane` falhar (returncode != 0) ou levantar excepção.
- Produces: `nudge(session: str, seq: int, body: str) -> str` — devolve um de `"delivered"`, `"stuck"`, `"session_absent"`, `"capture_failed"`. **Nunca** devolve `"delivered"` a menos que `tmux_has_session` seja `True` e `tmux_capture_pane` tenha sucesso — este é o teste de regressão do bug real apanhado pelo DeepCode (seq=156 no mailbox do droplet).

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import patch, MagicMock


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_relay.py -v -k "tmux or nudge"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation**

Add to `relay.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_relay.py -v -k "tmux or nudge"`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add relay.py tests/test_relay.py
git commit -m "relay: tmux primitives + nudge that never false-positives on missing session"
```

---

### Task 6: `send` guard (`safe-send`) — busy check, placeholder exception, length limit

**Files:**
- Modify: `relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `tmux_has_session`, `tmux_capture_pane` (Task 5), `get_agent` (Task 1).
- Produces: `check_safe_to_send(agent_cfg: dict, body: str, max_len: int = DEFAULT_MAX_BODY_LEN) -> None` — levanta `RelaySendRefused(msg)` num dos 3 casos; não devolve nada se estiver tudo ok. Usa `agent_cfg.get("placeholder", DEFAULT_PLACEHOLDER)`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_relay.py -v -k check_safe_to_send`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation**

Add to `relay.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_relay.py -v -k check_safe_to_send`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add relay.py tests/test_relay.py
git commit -m "relay: safe-send guard with placeholder exception"
```

---

### Task 7: `doctor` — validar config contra a realidade

**Files:**
- Modify: `relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `get_agent`, `tmux_has_session`, `tmux_capture_pane` (Tasks 1, 5).
- Produces: `doctor_check(agent_cfg: dict) -> dict` — devolve `{"session_ok": bool, "pane_tail": str, "busy_regex_matched": bool, "input_prefix_found": bool}`. Não levanta excepção — é diagnóstico, não guarda; `session_ok=False` é o único resultado que o CLI trata como falha.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_relay.py -v -k doctor_check`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation**

Add to `relay.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_relay.py -v -k doctor_check`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add relay.py tests/test_relay.py
git commit -m "relay: doctor diagnostic for tmux_session/busy_regex/input_prefix"
```

---

### Task 8: CLI wiring — `send`, `safe-send`, `read`, `peek`, `doctor`

**Files:**
- Modify: `relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: `build_parser() -> argparse.ArgumentParser`.
- Produces: `main(argv: list[str] | None = None) -> int` — entry point, devolve exit code (0 sucesso, 1 erro).

CLI shape (do spec):

```
relay.py send      --from A --to B --body "..." [--tokens N] [--config PATH]
relay.py safe-send  --from A --to B --body "..."            [--config PATH]
relay.py read       --as B                                   [--config PATH]
relay.py peek        --as B                                   [--config PATH]
relay.py doctor     --agent X                                [--config PATH]
```

- [ ] **Step 1: Write the failing tests**

```python
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

    captured_records = []

    def fake_read(argv=None):
        unread = relay.read_messages(relay.resolve_box_dir(cfg), "b", advance=True)
        captured_records.extend(unread)
        return 0

    # chama a função de leitura directamente (o CLI 'read' imprime; testamos a função central)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_relay.py -v -k "cli_"`
Expected: FAIL with `AttributeError: module 'relay' has no attribute 'main'`

- [ ] **Step 3: Write minimal implementation**

Add to `relay.py`, replacing the `if __name__ == "__main__":` block at the end:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_relay.py -v -k "cli_"`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add relay.py tests/test_relay.py
git commit -m "relay: CLI wiring (send, safe-send, read, peek, doctor)"
```

---

### Task 9: Full test suite, examples, README, licence, packaging

**Files:**
- Create: `relay.example.yaml`
- Create: `relay.example.json`
- Create: `README.md`
- Create: `LICENSE`
- Create: `.gitignore`

**Interfaces:**
- Consumes: nothing new — this task documents and packages what Tasks 1-8 built.

- [ ] **Step 1: Run the full test suite and confirm everything passes**

Run: `cd /home/jorge/projects/agent-relay && python3 -m pytest tests/test_relay.py -v`
Expected: PASS — todos os testes das Tasks 1-8 (≈32 testes).

- [ ] **Step 2: Write `relay.example.yaml`**

```yaml
box_dir: ./agent-relay-mail
agents:
  - name: coord
    tmux_session: claude
    busy_regex: 'esc to interrupt'
    input_prefix: "> "
    repo_path: /opt/my-project        # opcional — activa o head-check de staleness
    placeholder: "Type your message"  # opcional — default já é este valor

  - name: dc
    tmux_session: deepcode_session
    busy_regex: 'status: (processing|pending)'
    input_prefix: "> "
    repo_path: /opt/my-project
```

- [ ] **Step 3: Write `relay.example.json`**

```json
{
  "box_dir": "./agent-relay-mail",
  "agents": [
    {
      "name": "coord",
      "tmux_session": "claude",
      "busy_regex": "esc to interrupt",
      "input_prefix": "> ",
      "repo_path": "/opt/my-project",
      "placeholder": "Type your message"
    },
    {
      "name": "dc",
      "tmux_session": "deepcode_session",
      "busy_regex": "status: (processing|pending)",
      "input_prefix": "> ",
      "repo_path": "/opt/my-project"
    }
  ]
}
```

- [ ] **Step 4: Write `README.md`**

```markdown
# agent-relay

Mailbox JSONL fiável entre N agentes de IA, cada um numa sessão tmux própria.
Substitui `tmux send-keys` fire-and-hope: sem recibo de entrega, sem
ordenação, mensagens engolidas se o receptor estiver a meio de turno.

Extraído e generalizado a partir de um mecanismo que já correu em produção
num sistema de trading algorítmico, coordenando um agente decisor (Claude)
com um agente gerador de hipóteses (DeepCode) — ver
`docs/superpowers/specs/2026-08-18-agent-relay-design.md` para o histórico
completo, incluindo os bugs reais que motivaram cada correcção.

## Instalar

Sem instalação. Copia `relay.py` para `scripts/` do teu projecto.

Config em `relay.yaml` (precisa de `pip install pyyaml`) ou `relay.json`
(zero dependências, stdlib apenas) — ver `relay.example.yaml` /
`relay.example.json`.

## Usar

```bash
python3 relay.py send      --from coord --to dc --body "mensagem"
python3 relay.py safe-send --from coord --to dc --body "mensagem"  # com guardas
python3 relay.py read      --as dc
python3 relay.py peek      --as dc
python3 relay.py doctor    --agent dc
```

## Garantias

- `seq` estritamente ascendente e sem duplicados sob sends concorrentes
  (`fcntl.flock` exclusivo).
- Nunca reporta "delivered" se a sessão tmux do destinatário não existir ou
  a captura do pane falhar.
- `safe-send` recusa enviar se a caixa de input do destinatário já tem
  texto por enviar, se ele está a meio de turno, ou se o corpo excede 1600
  caracteres — com excepção do texto de placeholder da TUI.

## Fora de âmbito (deliberado)

Sem fila de tarefas, sem routing por custo — isso é decisão de produto de
cada projecto, não deste mecanismo de transporte. Sem read receipt (garante
entrega, não leitura). Sem deduplicação de retries. Sem autenticação
cross-host — assume mesmo utilizador OS, mesmo host, agentes confiados.

## Licença

MIT.
```

- [ ] **Step 5: Write `LICENSE`**

```
MIT License

Copyright (c) 2026 Jorge Pessoa

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 6: Write `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
agent-relay-mail/
```

- [ ] **Step 7: Commit**

```bash
cd /home/jorge/projects/agent-relay
git add relay.example.yaml relay.example.json README.md LICENSE .gitignore
git commit -m "docs: README, examples, MIT licence, gitignore"
```

- [ ] **Step 8: Report to owner before touching GitHub**

Nenhum passo deste plano cria ou faz push para o repositório GitHub
`jorgepessoa-dev/agent-relay` — isso fica para depois de o owner ver o
código funcionar localmente (código externo/visível = confirmação
explícita, por regra global). Parar aqui e reportar: testes a passar,
ficheiros prontos, a aguardar luz verde para `gh repo create` + push.

---

## Self-Review Notes

- **Spec coverage:** config obrigatória (Task 1), box_dir/head (Task 2),
  mailbox write/flock/0600 (Task 3), mailbox read/LOCK_SH/cursor atómico
  (Task 4), tmux + nudge corrigido (Task 5), safe-send + placeholder (Task
  6), doctor (Task 7), CLI + staleness no `read` (Task 8), README/exemplos/
  licença (Task 9). `beat` deliberadamente ausente (removido do v1 no
  spec). Read receipt e dedup de retry deliberadamente ausentes (fora de
  âmbito declarado no spec).
- **Placeholder scan:** sem TBD/TODO; todos os passos têm código completo.
- **Type consistency:** `append_message` devolve `int` (seq); `read_messages`
  devolve `list[dict]`; `check_safe_to_send` devolve `None`/levanta
  `RelaySendRefused`; `nudge` devolve `str` num conjunto fechado de 4
  valores usado consistentemente nos testes de `_cmd_send` e do próprio
  `nudge`. Nomes de agente usados como `--from`/`--to`/`--as` em todas as
  tasks batem certo com `get_agent(config, name)`.
