# Relay receipt contract — minimum transport and outcome evidence

**Status:** design for the owner-prioritized receipt implementation. No delivery, processing, authority, or execution is asserted by this document.

## The distinction the system must preserve

One request has independent facts. They must never be collapsed into one word such as “sent” or “delivered”.

| Fact | Producer | What it proves | What it cannot prove |
|---|---|---|---|
| `mailbox_persisted` | relay append | the durable mailbox has `seq` | pane notification, read, understanding, or work |
| `notification` | relay nudge/redelivery | a named `seq` left the recipient input box | `relay.py read`, task processing, or correctness |
| `outcome_receipt` | recipient / owning mechanism | the requested class of result occurred | a different request, or broader task completion |

`relay.py doctor` is health diagnosis only: it has no request sequence input and is not a receipt for any row above.

## Transport receipt and pending visibility

The relay owns one durable, append-only delivery record keyed by `(recipient, seq)`. It is queryable by sender and coordinator without pane inspection. The record contains:

```json
{
  "recipient": "deepcode",
  "seq": 2750,
  "mailbox_persisted": true,
  "created_at": "RFC3339",
  "notification_state": "pending|delivered|failed",
  "notification_attempts": [{"at": "RFC3339", "result": "pending|delivered|..."}],
  "pending_age_seconds": 0,
  "last_reason": "named relay status"
}
```

The record is written at append time and updated only by a nudge/redelivery attempt. A retry is nudge-only; it never appends the body again. `delivered` has exactly the existing narrow meaning: notification of that `seq` was confirmed by the relay. It must not be rendered as “processed”. A query such as `delivery-status --seq N` and a bounded `pending` list expose this state; aged pending rows alert the coordinator rather than requiring a manual `doctor` loop.

## Request-class outcome receipts

No language model parses arbitrary prose to infer completion. The request asks for, or the recipient emits, one minimal structured receipt linked to `request_seq`.

| Request class | Minimum receipt | Existing mechanism to reuse |
|---|---|---|
| Factual verification | `request_seq`, `receipt_kind=factual`, exact artifact path(s) and hash/measurement command where applicable | `verified_report.py`, raw-output pointers |
| Explicit dispatch | `request_seq`, `dispatch_id`, expected artifact/path or expected HEAD movement, baseline | `DISPATCH:` marker plus `dispatch_derivation.py` / `dispatch_watchdog.py`; no marker, no dispatch record |
| Dispatch completion | `dispatch_id`, actual artifact/path or HEAD, outcome (`completed|refused|blocked`), and evidence | existing scoped commit/test receipts; lifecycle record consumes it |
| Liveness | nonce and reply sequence | `agent_alive_by_attempt.py` |

A generic acknowledgement, pane prompt, mailbox cursor, or unrelated HEAD movement is not any of these receipts. The recipient can reply `blocked` with named cause; that is a useful outcome, not a delivery failure.

## Sender reporting rule

Until a transport record says `notification_state=delivered`, a sender may say only: **“mailbox persisted; notification pending/failed”**. After it is delivered, the sender may say **“notification confirmed”**, not “processed”. Only the class-appropriate receipt permits reporting the factual answer, dispatch acceptance/completion, or liveness result.

## First build boundary and falsifiers

DeepCode implements; Codex only arbitrates this contract; independent reviewer is assigned before promotion.

1. Start with existing mailbox sequence and nudge/redelivery path; do not replace Agent Relay or add a second queue.
2. Pin: a `pending` send is queryable by exact sequence with nonzero/increasing age and remains not-delivered.
3. Pin: redelivery changes notification state without a duplicate mailbox row.
4. Pin: a healthy `doctor` result cannot be returned or formatted as confirmation for a sequence.
5. Pin: a factual receipt without its artifact/evidence path is rejected as incomplete; a liveness receipt requires the exact nonce.
6. Pin: `DISPATCH:` absence creates no dispatch record; an explicit dispatch cannot complete from unrelated HEAD movement.
7. Real caller receipt: create a pending row, query it, redeliver it, and retain raw output for each transition.

## Closure evidence

The item is not closed by a green local test. Closure requires: independent review of the scoped `agent-relay` diff; the named real-caller receipt; a scoped commit in this repository; `git push` to its tracked remote; and a post-push check showing the implementation HEAD equals its upstream (no ahead count). The reviewer records the commit and push evidence. Unrelated dirty files remain outside the item and must not be swept into its commit.

This is deliberately smaller than “exactly once processing”. Agent work is not generally transactional. The contract removes false claims and makes loss/back-pressure visible; idempotency and completion semantics remain domain-specific.
