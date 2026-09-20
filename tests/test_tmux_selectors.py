"""Guard for THIS repo (agent-relay): no bare tmux selector may survive here.

Codex seq 2848: the guard living in the tradingadvisor repo does NOT automatically cover the
external agent-relay repo, so this repo carries its own guard and its own commit. The scan is
deliberately narrow and readable: it walks the repo's own .py/.sh files, finds `-t <target>`
selectors, and requires the exact form.

FORMS: a SESSION target is `=name`; a PANE target is `=name:` (measured: capture-pane and
send-keys fail with "can't find pane" without the trailing colon). `new -s NAME` is CREATION,
not a selector, and is exempt — no measurement shows `=` applies there, so no claim is made.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXCLUDE = (".git", "__pycache__", "/tests/", "node_modules")
PY_SELECTOR = re.compile(r'"-t",\s*(f?"[^"]*"|f?\'[^\']*\'|[A-Za-z_]\w*(?![\w.(]))')
SH_SELECTOR = re.compile(r"(?<![\w-])-t\s+(\S+)")
CREATION = re.compile(r"\bnew(-session)?\b.*\s-s\b")


def _is_exact(token: str) -> bool:
    cleaned = token.replace("\\", "").strip().lstrip("f").strip('"').strip("'").lstrip("f")
    return cleaned.strip('"').strip("'").startswith("=")


def _bare(line: str) -> bool:
    if CREATION.search(line):
        return False
    if any(not _is_exact(m.group(1)) for m in PY_SELECTOR.finditer(line)):
        return True
    return any(not _is_exact(m.group(1)) for m in SH_SELECTOR.finditer(line))


def find_bare() -> list[str]:
    hits = []
    for path in REPO.rglob("*"):
        text = str(path)
        if path.suffix not in (".py", ".sh") or any(x in text for x in EXCLUDE):
            continue
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if "tmux" in line and "-t" in line and _bare(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()[:110]}")
    return hits


def test_no_bare_tmux_selector_in_agent_relay():
    hits = find_bare()
    assert hits == [], "bare selectors resolve by prefix:\n  " + "\n  ".join(hits)


def test_the_guard_can_fail(tmp_path):
    bad = tmp_path / "x.sh"
    bad.write_text('tmux kill-session -t glm\n')
    assert _bare('tmux kill-session -t glm') is True
    assert _bare('tmux kill-session -t "=glm"') is False
    assert _bare('tmux new -d -s "glm" "cmd"') is False
