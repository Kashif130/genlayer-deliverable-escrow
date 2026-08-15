# DeliverableEscrow

A reusable GenLayer Intelligent Contract primitive that holds funds and
releases them only when validator consensus agrees a submitted
deliverable satisfies stated acceptance criteria.

## Why this exists

Escrow is one of the oldest primitives in software, but it has always
depended on a deterministic release condition (a signature, a hash, a
timelock) or a human/off-chain arbitrator for anything requiring
judgment. Most real payments — freelance work, bounties, commissioned
content, contract milestones — aren't deterministic: "did they deliver
what was agreed" requires actually reading and judging the work.

`DeliverableEscrow` moves that judgment call on-chain using GenLayer's
validator consensus, so payer and payee don't need a trusted third party
(Upwork, a DAO multisig vote, a human arbiter) to decide whether a
deliverable is acceptable — while still keeping the *movement of funds*
fully deterministic and auditable.

## How consensus is used

The core judgment call — "does this submission satisfy the brief and
criteria" — happens in `verify_deliverable()`, a non-deterministic block.
Each validator independently:

1. reads the `brief` (what was requested) and `criteria` (how to judge it),
2. reads the payee's `submission` (their proof of work),
3. returns a structured verdict: `APPROVED`, `REJECTED`, or
   `NEEDS_REVISION`, with reasoning.

Because independently-run LLM judgments are never byte-identical, this
contract uses **`gl.eq_principle.prompt_non_comparative`** with an
explicit equivalence criteria:

> Two verdicts are equivalent if they reach the same `verdict` category
> given the stated brief and acceptance criteria, even if `reasoning`
> wording differs. If the verdict category differs, the two are **not**
> equivalent.

This is the actual primitive being demonstrated: a genuine judgment call
— "is this deliverable good enough" — reconciled across independently
reasoning validators by semantic agreement on the *decision*, not by
comparing exact text. This is fundamentally different from `strict_eq`,
which only works for byte-deterministic operations (hashing a fixed
input), and is inappropriate here because two honest validators will
almost never phrase their reasoning identically.

## Lifecycle / state machine

```
FUNDED --submit_deliverable()--> SUBMITTED --verify_deliverable()--> APPROVED --withdraw()--> RELEASED
                                     ^                                   |
                                     |                              (if refund_enabled)
                              (REJECTED: resubmit)                       |
                                     |                                   v
                                  REJECTED <---verify_deliverable()---   |
                                     |                                REFUNDED
                              (if refund_enabled: refund())  ------------^
```

- **FUNDED**: payer has deposited funds, payee hasn't submitted yet.
- **SUBMITTED**: payee has submitted proof of work, awaiting verification.
- **APPROVED**: validators approved the submission — payee can withdraw.
- **REJECTED**: validators rejected (or asked for revision) — payee may
  resubmit revised work, re-opening the SUBMITTED path.
- **RELEASED**: terminal. Funds have been sent to the payee.
- **REFUNDED**: terminal. Funds have been returned to the payer (only
  possible if `refund_enabled` was set `True` at creation, and only
  before an APPROVED verdict).

Funds only ever move in `withdraw()` (to the payee, gated on APPROVED)
or `refund()` (to the payer, gated on `refund_enabled` and non-approval).
Both are single-shot: the escrow moves to a terminal status in the same
call that triggers the transfer, so a second call reverts rather than
double-spending.

## Public interface

| Method | Type | Purpose |
|---|---|---|
| `create_escrow(payee, brief, criteria, refund_enabled)` | write, payable | Fund a new escrow. Returns nothing; read `escrow_count()` for the new id. |
| `submit_deliverable(escrow_id, submission)` | write | Payee submits proof of work. Callable again after a REJECTED verdict. |
| `verify_deliverable(escrow_id)` | write | Runs validator consensus on the current submission. |
| `withdraw(escrow_id)` | write | Payee claims funds after APPROVED. |
| `refund(escrow_id)` | write | Payer reclaims funds if `refund_enabled` and not yet APPROVED. |
| `get_status(escrow_id)` | view | Current status code (see constants below). |
| `get_amount(escrow_id)` | view | Escrowed amount. |
| `get_submission(escrow_id)` | view | Payee's current submission text. |
| `get_verdict_reasoning(escrow_id)` | view | Validators' reasoning from the last verification round. |
| `get_payee(escrow_id)` | view | Payee address as a string. |
| `escrow_count()` | view | Total escrows created. |

Status codes: `0=FUNDED, 1=SUBMITTED, 2=APPROVED, 3=REJECTED, 4=RELEASED, 5=REFUNDED`

## Using `refund_enabled`

This is the key trust-tradeoff knob, set once at creation:

- **`True`**: payer-friendly. Payer can reclaim funds any time before
  approval — protects against an unresponsive or non-delivering payee.
- **`False`**: payee-friendly. Once funded, the only way funds leave
  escrow is an APPROVED verdict followed by `withdraw()` — protects the
  payee against a payer who funds an escrow and then never engages with
  the verification process to avoid paying.

Real deployments composing this primitive should pick (or expose) this
per their own trust model — e.g. a bounty platform might always set it
`True` with a cooldown built into its own layer, while a "the funds are
committed the moment you accept the job" platform might set it `False`.

## Integrating from another contract

Any platform contract (a bounty board, a freelance marketplace, a DAO
milestone tracker) can deploy against or call `DeliverableEscrow`
directly: fund an escrow when work is assigned, let the worker call
`submit_deliverable`, trigger `verify_deliverable` when they want a
judgment, and read `get_status` to know when to update its own UI/state.
Because verification is a separate, re-runnable step, a rejected
submission doesn't require a whole new escrow — the same one supports
a natural revise-and-resubmit loop.

## What this is *not*

This is a primitive, not a marketplace. It doesn't include dispute
escalation beyond resubmission (a real deployment might add a bonded
"appeal to a larger validator set" step, following the same pattern as
GenLayer's own optimistic-democracy appeal window), partial/milestone
payouts within a single escrow, or fee handling. Kept minimal and
composable deliberately, so it stays auditable as a dependency.

## Files

```
contract/deliverable_escrow.py     — the Intelligent Contract
tests/test_deliverable_escrow.py   — gltest-style test suite
docs/README.md                     — this file
```
