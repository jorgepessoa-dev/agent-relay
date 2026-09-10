#!/usr/bin/env python3
"""Mailbox rotation — archive older messages, never destroy the evidence trail.

Design constraints (why this is safe):
- relay.py seq now comes from a durable per-mailbox counter (.seq_<name>), so
  compacting the live file can NEVER re-issue a used seq.
- Nothing is deleted: messages move to mail/archive/to_<name>.<ts>.jsonl.gz and
  a manifest records counts + sha256 of each archive (append-only evidence).
- Runs under an exclusive flock on mail/.rotate.lock so two rotations (or a
  rotation while another agent rotates) cannot interleave.
- The live file is rewritten atomically (tmp + os.replace) with the messages
  that are KEPT; the archive receives the rest.

Usage:
  relay_rotate.py [--box /opt/agent-relay/mail] [--keep 500] [--dry-run]
Exit: 0 ok, 1 error.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

BOX_DEFAULT = Path("/opt/agent-relay/mail")


def _log(msg: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(f"{datetime.now(UTC).isoformat()} {msg}\n")


def rotate_one(mailbox: Path, archive_dir: Path, keep: int, *,
               now: datetime | None = None, dry_run: bool = False) -> dict:
    """Keep the last `keep` messages, archive the rest. Returns a record."""
    now = now or datetime.now(UTC)
    lines = [ln for ln in mailbox.read_text().splitlines() if ln.strip()]
    if len(lines) <= keep:
        return {"mailbox": mailbox.name, "total": len(lines), "archived": 0,
                "kept": len(lines), "skipped": True}
    old, kept = lines[: len(lines) - keep], lines[len(lines) - keep:]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    arch = archive_dir / f"{mailbox.stem}.{stamp}.jsonl.gz"
    rec = {
        "mailbox": mailbox.name,
        "total": len(lines),
        "archived": len(old),
        "kept": len(kept),
        "archive": str(arch),
        "rotated_at": now.isoformat(),
        "first_archived_seq": json.loads(old[0]).get("seq") if old else None,
        "last_archived_seq": json.loads(old[-1]).get("seq") if old else None,
        "skipped": False,
    }
    if dry_run:
        return rec
    archive_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(arch, "wt") as fh:
        fh.write("\n".join(old) + "\n")
    rec["archive_sha256"] = hashlib.sha256(arch.read_bytes()).hexdigest()
    rec["archive_lines"] = len(old)
    tmp = mailbox.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(ln + "\n" for ln in kept))
    os.replace(tmp, mailbox)
    # manifest is append-only evidence
    with (archive_dir / "MANIFEST.jsonl").open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", default=str(BOX_DEFAULT))
    ap.add_argument("--keep", type=int, default=500,
                    help="messages to keep live per mailbox")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    box = Path(args.box)
    archive_dir = box / "archive"
    log_path = box.parent / "logs" / "relay_rotate.log"

    import fcntl

    lock_path = box / ".rotate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            results = []
            for mailbox in sorted(box.glob("to_*.jsonl")):
                rec = rotate_one(mailbox, archive_dir, args.keep,
                                 dry_run=args.dry_run)
                results.append(rec)
                _log(json.dumps(rec, ensure_ascii=False), log_path)
                print(json.dumps(rec, ensure_ascii=False))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
