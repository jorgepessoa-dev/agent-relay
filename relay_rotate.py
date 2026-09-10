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
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

BOX_DEFAULT = Path("/opt/agent-relay/mail")

# A message body may POINT at a payload file (F1-style pointer). Archiving the
# pointer without the payload leaves the archive incomplete: the archived
# message references a file that only exists on the live disk, so deleting it
# "as an orphan" would break the archive and the never-delete guarantee.
PAYLOAD_RE = re.compile(r"/[A-Za-z0-9_./-]+\.(?:md|json|jsonl|txt|yaml|yml)")


def payload_refs(body: str, *, box: Path | None = None) -> list[Path]:
    """Absolute paths referenced by a message body that a reader would need."""
    out: list[Path] = []
    for m in PAYLOAD_RE.finditer(body or ""):
        cand = Path(m.group(0))
        base = (box or BOX_DEFAULT)
        if (cand.exists() and cand.is_file() and base in cand.parents
                and cand not in out):
            out.append(cand)
    return out


def archive_payload(ref: Path, archive_dir: Path) -> dict:
    """Copy a referenced payload into the archive (content-addressed)."""
    digest = hashlib.sha256(ref.read_bytes()).hexdigest()
    payload_dir = archive_dir / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    dest = payload_dir / f"{digest[:16]}__{ref.name}"
    if not dest.exists():
        dest.write_bytes(ref.read_bytes())
    return {"orig": str(ref), "sha256": digest, "copy": str(dest),
            "bytes": ref.stat().st_size}


def raw_payload_refs(body: str, *, box: Path) -> list[Path]:
    """Referenced in-box paths, WITHOUT requiring them to exist on disk.

    Reconstructing from the archive must not depend on the live file being
    present — that is the whole point of archiving the payload.
    """
    out: list[Path] = []
    for m in PAYLOAD_RE.finditer(body or ""):
        cand = Path(m.group(0))
        if box in cand.parents and cand not in out:
            out.append(cand)
    return out


def reconstruct(archive_gz: Path, seq: int, archive_dir: Path) -> dict | None:
    """Rebuild a FULL message from the archive alone (no live disk needed)."""
    with gzip.open(archive_gz, "rt") as fh:
        for line in fh.read().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("seq") != seq:
                continue
            payloads = {}
            payload_dir = archive_dir / "payload"
            for ref in raw_payload_refs(rec.get("body", ""), box=archive_dir.parent):
                matches = sorted(payload_dir.glob(f"*__{ref.name}"))                     if payload_dir.exists() else []
                payloads[str(ref)] = (matches[0].read_text()
                                      if len(matches) == 1 else None)
            rec["_payload_resolved"] = payloads
            rec["_fully_reconstructible"] = all(v is not None
                                                for v in payloads.values())
            return rec
    return None


def repair_archive(box: Path, archive_dir: Path) -> list[dict]:
    """Archive payloads referenced by ALREADY-archived messages.

    Repair mode: messages rotated before the payload mechanism existed point at
    notes that live only on the live disk. Without this, the archive is
    incomplete and those notes cannot be deleted safely.
    """
    fixed: list[dict] = []
    manifest = archive_dir / "MANIFEST.jsonl"
    done = set()
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                for p in rec.get("payloads", []):
                    done.add(p["orig"])
    for gz in sorted(archive_dir.glob("*.jsonl.gz")):
        with gzip.open(gz, "rt") as fh:
            for line in fh.read().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                for ref in payload_refs(rec.get("body", ""), box=box):
                    if str(ref) in done:
                        continue
                    entry = archive_payload(ref, archive_dir)
                    entry.update({"repair": True, "for_seq": rec.get("seq"),
                                  "for_archive": gz.name})
                    with manifest.open("a") as mf:
                        mf.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    done.add(str(ref))
                    fixed.append(entry)
    return fixed


def _log(msg: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(f"{datetime.now(UTC).isoformat()} {msg}\n")


def rotate_one(mailbox: Path, archive_dir: Path, keep: int, *,
               max_bytes: int | None = None,
               now: datetime | None = None, dry_run: bool = False) -> dict:
    """Keep the last `keep` messages (and fit `max_bytes`), archive the rest.

    Rotation triggers on EITHER threshold: line count > keep OR file size >
    max_bytes. A mailbox with few but huge messages (e.g. 323 lines / 1.1 MB)
    must rotate too — a line-only threshold silently skips it.
    """
    now = now or datetime.now(UTC)
    lines = [ln for ln in mailbox.read_text().splitlines() if ln.strip()]
    size = mailbox.stat().st_size
    over_lines = len(lines) > keep
    over_bytes = max_bytes is not None and size > max_bytes
    if not (over_lines or over_bytes):
        return {"mailbox": mailbox.name, "total": len(lines), "archived": 0,
                "kept": len(lines), "skipped": True,
                "bytes_before": size, "bytes_after": size,
                "trigger": None}
    old, kept = lines[: len(lines) - keep], lines[len(lines) - keep:]
    # byte budget: drop oldest kept messages until the live file fits
    if max_bytes is not None:
        def _size(seq_lines):
            return sum(len(ln.encode()) + 1 for ln in seq_lines)
        while kept and _size(kept) > max_bytes and len(kept) > 1:
            old.append(kept.pop(0))
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    arch = archive_dir / f"{mailbox.stem}.{stamp}.jsonl.gz"
    rec = {
        "mailbox": mailbox.name,
        "total": len(lines),
        "archived": len(old),
        "kept": len(kept),
        "bytes_before": size,
        "bytes_after": sum(len(ln.encode()) + 1 for ln in kept),
        "trigger": "lines" if over_lines else "bytes",
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
    # the archive is only COMPLETE if it carries the payloads its messages point
    # at: otherwise a "pointer" message becomes a dead reference once the live
    # note is removed (owner 2026-09-10: fix the cause, not the symptom).
    payloads = []
    for line in old:
        try:
            body = json.loads(line).get("body", "")
        except json.JSONDecodeError:
            continue
        for ref in payload_refs(body, box=mailbox.parent):
            entry = archive_payload(ref, archive_dir)
            entry["for_seq"] = json.loads(line).get("seq")
            payloads.append(entry)
    if payloads:
        rec["payloads"] = payloads
    tmp = mailbox.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(ln + "\n" for ln in kept))
    os.replace(tmp, mailbox)
    # manifest is append-only evidence
    with (archive_dir / "MANIFEST.jsonl").open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def orphan_scan(box: Path, archive_dir: Path | None = None) -> dict:
    """Genuine orphans only, with the MANIFEST as the source of truth.

    A detector that greps only the live disk reports archived payloads as
    orphans, and acting on that destroys evidence (2026-09-10: a first count
    said 7 orphans; the truth was 1 — the other 6 were referenced by archived
    messages). References therefore come from:
      (a) the append-only MANIFEST (payloads archived with their messages), and
      (b) the LIVE mailboxes.
    """
    archive_dir = archive_dir or (box / "archive")
    referenced: set[str] = set()
    manifest = archive_dir / "MANIFEST.jsonl"
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("orig"):
                referenced.add(rec["orig"])
            for p in rec.get("payloads", []):
                referenced.add(p["orig"])
    for mb in sorted(box.glob("to_*.jsonl")):
        for line in mb.read_text().splitlines():
            if line.strip():
                for ref in raw_payload_refs(line, box=box):
                    referenced.add(str(ref))
    files = sorted(p for p in box.glob("*.md"))
    orphans = [str(p) for p in files if str(p) not in referenced]
    return {"notes": len(files), "referenced": len(referenced),
            "orphans": orphans, "source_of_truth": "MANIFEST.jsonl + live mailboxes"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", default=str(BOX_DEFAULT))
    ap.add_argument("--keep", type=int, default=500,
                    help="messages to keep live per mailbox")
    ap.add_argument("--max-bytes", type=int, default=1_000_000,
                    help="rotate when a mailbox exceeds this size (bytes)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repair", action="store_true",
                    help="archive payloads referenced by already-archived messages")
    ap.add_argument("--orphans", action="store_true",
                    help="list GENUINE orphan notes (source: MANIFEST + live mailboxes)")
    ap.add_argument("--restore", metavar="MAILBOX",
                    help="reconstruct a message from the archive (use with --seq)")
    ap.add_argument("--seq", type=int, default=None)
    args = ap.parse_args()
    box = Path(args.box)
    archive_dir = box / "archive"
    log_path = box.parent / "logs" / "relay_rotate.log"

    import fcntl

    archive_dir.mkdir(parents=True, exist_ok=True)
    if args.orphans:
        print(json.dumps(orphan_scan(box), indent=2, ensure_ascii=False))
        return 0
    if args.repair:
        fixed = repair_archive(box, archive_dir)
        for f in fixed:
            print(json.dumps(f, ensure_ascii=False))
        print(f"repair: {len(fixed)} payload(s) archived")
        return 0
    if args.restore:
        if args.seq is None:
            print("--restore requires --seq", file=sys.stderr)
            return 1
        cands = sorted(archive_dir.glob(f"{args.restore}.*.jsonl.gz"))
        for gz in reversed(cands):
            rec = reconstruct(gz, args.seq, archive_dir)
            if rec is not None:
                print(json.dumps(rec, ensure_ascii=False))
                return 0 if rec.get("_fully_reconstructible", True) else 5
        print(f"seq {args.seq} not found in {len(cands)} archive(s)", file=sys.stderr)
        return 1

    lock_path = box / ".rotate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            results = []
            for mailbox in sorted(box.glob("to_*.jsonl")):
                rec = rotate_one(mailbox, archive_dir, args.keep,
                                 max_bytes=args.max_bytes,
                                 dry_run=args.dry_run)
                results.append(rec)
                _log(json.dumps(rec, ensure_ascii=False), log_path)
                print(json.dumps(rec, ensure_ascii=False))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
