"""Pins for the relay receipt contract - transport receipt and pending visibility (F-1140).

Codex's design, docs/RELAY_RECEIPT_CONTRACT_DESIGN_2026_09_19.md in THIS repo, and the owner's maximum priority. The
distinction the pins hold is the one the design names: three independent facts that must never collapse into one word.

  mailbox_persisted  - the durable mailbox has the seq. Proves nothing about notification, reading or work.
  notification       - a named seq left the recipient's input box. Proves nothing about reading or work.
  outcome_receipt    - the requested class of result occurred. Proves nothing about a different request.

WHAT ALREADY EXISTS, measured before writing anything (the day's lesson, and it is the ninth time): the mailbox is
append-only and keyed by seq; the redelivery state is ALREADY keyed by `(name, seq)` via `_entry_key(name, seq)` and
ALREADY records age via `_record_age_s`; `append_message` carries the F-987 doctrine verbatim - "a durable seq and a
successful transport result are not evidence that anything was communicated". So this is NOT a new transport log: it is
making the existing one QUERYABLE per sequence, and adding the reporting rule that stops a sender saying "sent".

The pins are written against the seam the existing tests already use: append_message / nudge / redeliver_unread /
redelivery_state_path, driven with tmp_path and monkeypatch.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import relay


def _cfg(tmp_path) -> dict:
    return {"box_dir": str(tmp_path), "agents": [
        {"name": "a", "tmux_session": "a", "busy_regex": "B", "input_prefix": "> "},
    ]}


# --- Pin 2: pending is queryable by exact sequence, with age, and is NOT delivered ---------------

def test_P2_a_pending_message_is_queryable_by_sequence_with_an_age_and_is_not_delivered(tmp_path):
    """The design's second falsifier, and the defect of 2026-09-19: a sender that cannot ask 'did seq N leave the
    box?' says 'sent' and the recipient's coordinator asks again. Persisted is not delivered, and the age makes the
    difference between 'just left' and 'stuck for an hour' visible."""
    relay.append_message(tmp_path, "a", "deepcode", "hello")
    record = relay.delivery_record(tmp_path, "a", 1)
    assert record["recipient"] == "a" and record["seq"] == 1
    assert record["mailbox_persisted"] is True
    assert record["notification_state"] == "pending"
    assert record["pending_age_seconds"] >= 0
    assert record["notification_state"] != "delivered", "a persisted row is not a delivered notification"


def test_P2_the_age_GROWS_so_a_stuck_row_can_be_told_from_a_fresh_one(tmp_path):
    relay.append_message(tmp_path, "a", "deepcode", "hello")
    early = relay.delivery_record(tmp_path, "a", 1, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    later = relay.delivery_record(tmp_path, "a", 1, now=datetime.now(timezone.utc) + timedelta(seconds=3600))
    assert later["pending_age_seconds"] > early["pending_age_seconds"] + 3000


def test_P2_a_sequence_that_is_not_in_the_mailbox_is_NOT_a_delivery_record(tmp_path):
    """Fail-closed: asking about a seq the mailbox does not have must not invent one. Absence is absence."""
    relay.append_message(tmp_path, "a", "deepcode", "only one")
    record = relay.delivery_record(tmp_path, "a", 99)
    assert record["mailbox_persisted"] is False
    assert record["notification_state"] == "unknown"


# --- Pin 3: redelivery changes the notification state WITHOUT a duplicate mailbox row ------------

def test_P3_redelivery_moves_the_state_without_appending_a_second_row(tmp_path, monkeypatch):
    """A retry is nudge-only, never a second append. If redelivery duplicated the row, the recipient would read the
    request twice and the mailbox would grow with every attempt - so the pin counts the rows, not just the state."""
    cfg = _cfg(tmp_path)
    relay.append_message(tmp_path, "a", "deepcode", "one")
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: "delivered")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)

    before = len(relay.read_messages(tmp_path, "a", advance=False))
    moved = relay.redeliver_unread(cfg, now=datetime.now(timezone.utc) + timedelta(hours=1))
    after = len(relay.read_messages(tmp_path, "a", advance=False))

    assert moved, "the sweep must have attempted something"
    assert after == before, "a retry must not append a second mailbox row"
    record = relay.delivery_record(tmp_path, "a", 1)
    assert record["notification_state"] == "delivered"
    assert record["notification_attempts"], "the attempt must be recorded"


def test_P3_a_failed_nudge_leaves_the_state_not_delivered_with_its_reason(tmp_path, monkeypatch):
    """The useful half: a failed attempt is named, not silent. And 'blocked' is an outcome, not a delivery failure -
    the design says so explicitly."""
    cfg = _cfg(tmp_path)
    relay.append_message(tmp_path, "a", "deepcode", "one")
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: "busy")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    relay.redeliver_unread(cfg, now=datetime.now(timezone.utc) + timedelta(hours=1))
    record = relay.delivery_record(tmp_path, "a", 1)
    assert record["notification_state"] != "delivered"
    assert record["last_reason"], "a failed attempt must name its reason"


# --- Pin 4: doctor is session health, NEVER a receipt for a sequence ----------------------------

def test_P4_the_reporting_rule_refuses_to_let_a_health_check_confirm_a_sequence(tmp_path):
    """The design's fourth falsifier, and the one that matters most for honesty: `doctor` has no sequence input, so a
    healthy doctor cannot be returned or formatted as confirmation that seq N was delivered. This pin is the BLOCKER on
    that sentence being written by accident."""
    assert not hasattr(relay, "doctor_confirming"), "no API may exist that confirms a seq from a health check"

    sentence = relay.sender_report(delivery_record=relay.delivery_record(tmp_path, "a", 1))
    assert "pending" in sentence or "persisted" in sentence
    assert "delivered" not in sentence, f"an undelivered seq must not be reported as delivered: {sentence}"
    assert "processed" not in sentence


def test_P4_the_DELIVERED_may_be_reported_as_notification_confirmed_never_as_processed(tmp_path, monkeypatch):
    """The other half of the rule: once notification is confirmed, a sender may say so - and still may NOT say the
    request was processed, because that is the outcome receipt's job and it is a different fact."""
    cfg = _cfg(tmp_path)
    relay.append_message(tmp_path, "a", "deepcode", "one")
    monkeypatch.setattr(relay, "nudge", lambda agent, seq, body: "delivered")
    monkeypatch.setattr(relay, "check_safe_to_send", lambda agent, body: None)
    relay.redeliver_unread(cfg, now=datetime.now(timezone.utc) + timedelta(hours=1))

    sentence = relay.sender_report(delivery_record=relay.delivery_record(tmp_path, "a", 1))
    assert "confirmed" in sentence
    # NOT a substring test on the word: the sentence says "notification confirmed (not processed)" precisely to be
    # explicit, so asserting the word is absent fails on a CORRECT sentence - my first version did exactly that.
    # What must be absent is a CLAIM of processing, so the pin asserts the explicit negation instead.
    assert "not processed" in sentence, f"the sentence must explicitly refuse the processing claim: {sentence}"


# --- The bounded pending list ------------------------------------------------------------------

def test_the_pending_list_is_BOUNDED_and_aged_rows_come_first(tmp_path):
    """The design asks for a bounded pending list so aged rows surface without a manual doctor loop."""
    for i in range(12):
        relay.append_message(tmp_path, "a", "deepcode", f"m{i}")
    rows = relay.pending_deliveries(tmp_path, limit=5)
    assert len(rows) == 5, "the list must respect its bound"
    assert all(r["notification_state"] == "pending" for r in rows)
    assert rows[0]["seq"] == 1, "the oldest pending row must come first, because it is the one stuck"
