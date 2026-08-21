# RealWorldOracle

A reusable GenLayer Intelligent Contract primitive that resolves ambiguous,
real-world questions on-chain through validator consensus, and exposes the
finalized answer for other contracts to build on.

## Why this exists

Traditional oracles (Chainlink-style) work well for numeric, quotable data
(prices, weather feeds) that already exists in a structured API. They break
down for questions that require *judgment*: "did team X win," "was the
product delivered in acceptable condition," "does this claim satisfy the
insurance policy's terms." Those questions need a reasoning step, which is
exactly what GenLayer's Intelligent Contracts add to the consensus process.

`RealWorldOracle` packages that reasoning step into a standalone,
composable primitive — a question in, a validator-reconciled verdict out —
so prediction markets, insurance payouts, DAO conditionals, escrow
releases, or dispute resolution apps don't each need to re-implement
non-deterministic consensus handling from scratch.

## How consensus is used

GenLayer's core innovation is **Optimistic Democracy**: a leader validator
proposes a result, other validators independently re-run the same logic,
and consensus is reached not by exact output matching but by an
**Equivalence Principle** — a rule for when two independently-produced
outputs should be treated as agreeing.

This contract's resolution step (`request_resolution`) is a non-
deterministic block: each validator independently

1. fetches the `source_hint` URL if one was given (real web data), and
2. asks an LLM to produce a structured verdict — `answer`, `confidence`,
   `reasoning` — against the question's stated `criteria`.

Because different validators (potentially different underlying models,
different phrasing, different emphasis in reasoning) will almost never
produce byte-identical JSON, this contract uses
**`gl.eq_principle.prompt_non_comparative`** rather than `strict_eq`. The
equivalence criteria passed to it is explicit about what "agreement" means
here:

> Two verdicts are equivalent if they reach the same `answer` given the
> stated resolution criteria, even if `reasoning` wording differs.
> Different confidence levels are still equivalent as long as `answer`
> matches. If the answer differs in substance, the verdicts are **not**
> equivalent.

This is the actual primitive being demonstrated: judgment-based semantic
agreement on a subjective/real-world question, rather than a deterministic
string or hash check. It's the difference between "did every validator
compute the same SHA-256" (useless for "did X happen") and "did every
validator, reasoning independently, land on the same real-world
conclusion."

## Lifecycle / state machine

```
PROPOSED --request_resolution()--> RESOLVED --finalize()--> FINALIZED
                                       |    ^
                                  dispute_answer()   (only while window open)
                                       |    |
                                       v    | (window must elapse too)
                                   DISPUTED --request_resolution()--> RESOLVED (round 2...)
```

- **PROPOSED**: question registered, no verdict yet.
- **RESOLVED**: validators reached consensus on a verdict this round. A
  real, wall-clock **dispute window** (`dispute_window_seconds`, set at
  deploy time) opens from this moment (`resolved_at`). Not safe for
  downstream contracts to act on until that window elapses with no open
  dispute — `finalize()` enforces this and will revert if called early.
- **DISPUTED**: someone staked a bond (≥ `min_dispute_bond`) challenging
  the current verdict, before the window closed. `finalize()` is blocked
  until the question is re-resolved.
- **FINALIZED**: immutable. `get_final_answer()` only returns a value in
  this state — this is the read dependent contracts should use.

Separating "resolved" from "finalized" behind an *enforced* timing gate is
the standard oracle safety pattern (a challenge window before funds move),
applied here to LLM-driven verdicts instead of numeric price feeds.

### Dispute bond settlement

A posted bond is never just discarded. When `request_resolution()` re-runs
consensus on a `DISPUTED` question:

- If the new verdict **differs** from the one that was disputed, the
  dispute is treated as upheld and the full bond is refunded to the
  disputer immediately (`gl.ContractAt(disputer).emit_transfer(...)`).
- If the new verdict **matches** the disputed one, the dispute is treated
  as rejected and the bond is forfeited into a contract-held
  `forfeited_bonds` pool — the deterrent against spam/frivolous disputes.
  The contract owner can pay this out via `withdraw_forfeited_bonds(to,
  amount)`.

## Public interface

| Method | Type | Purpose |
|---|---|---|
| `propose_question(text, criteria, source_hint)` | write | Register a new question. Returns `question_id`. |
| `request_resolution(question_id)` | write | Runs the non-deterministic validator consensus step. Callable again after a dispute; settles any pending bond. |
| `dispute_answer(question_id)` | write, payable | Challenge a `RESOLVED` verdict by staking a bond ≥ `min_dispute_bond`, only while the dispute window is still open. |
| `update_source_hint(question_id, new_source_hint)` | write | Asker-only fix for a dead/unreachable source on an unresolved question. |
| `finalize(question_id)` | write | Locks in the current verdict. Only allowed from `RESOLVED`, with no open dispute, **and** only after `dispute_window_seconds` has elapsed since `resolved_at`. |
| `withdraw_forfeited_bonds(to, amount)` | write | Owner-only payout from the forfeited-bond pool. |
| `get_question(question_id)` | view | Full question record (status, answer, confidence, reasoning, round count, `resolved_at`, dispute bond/disputer, etc). |
| `get_final_answer(question_id)` | view | Reverts unless finalized — safe read for integrators. |
| `can_finalize(question_id)` | view | Whether `finalize()` would succeed right now. |
| `get_dispute_window_seconds()` / `get_forfeited_bonds()` | view | Deploy-time window length / current forfeited-bond pool balance. |
| `question_count()` | view | Total questions proposed. |

## Integrating from another contract

Any contract that needs "resolve this real-world fact, then act on it" can
deploy or reference a `RealWorldOracle` instance and call it the same way a
user would — propose a question relevant to its own logic (e.g. "did
event E, which this escrow depends on, occur by date D"), wait for
finalization, then read `get_final_answer` before releasing funds or
settling a market. Because the oracle is a separate contract with its own
dispute window, dependent contracts inherit that safety margin without
implementing it themselves.

## Example questions this primitive is suited for

- "Did [sports event] end with [team] winning?" — with `source_hint`
  pointing at an official results page.
- "Was [shipment/order ID] confirmed delivered by [date]?"
- "Did [named event] occur in [region] by [date]?" for parametric
  insurance-style payouts.
- "Does the delivered work in [linked document/repo] satisfy [criteria]?"
  for freelance/bounty escrow.

## What this is *not*

This is a primitive, not a product. It doesn't include UI or an incentive
model for validator honesty beyond GenLayer's base consensus. It's
deliberately generic rather than tied to one use case, which is what makes
it reusable as a dependency for other Intelligent Contracts.

## Version note (v0.3.0)

An earlier revision (`real_world_oracle_v2.py`, kept for reference) had two
gaps flagged in review: `finalize()` had no real timing/state guard — it
was callable the instant a question hit `RESOLVED`, so the "dispute
window" was nominal only — and a posted dispute bond had no refund/award/
withdrawal path; it was silently zeroed on re-resolution regardless of
outcome. `real_world_oracle_v3.py` fixes both: a real `dispute_window_seconds`
elapsed-time guard on `finalize()` and `dispute_answer()`, and full bond
settlement (refund on an upheld dispute, forfeiture to an owner-withdrawable
pool on a rejected one) before the bond fields are cleared. See the
constructor signature change (`min_dispute_bond, dispute_window_seconds`)
and the "Dispute bond settlement" section above.

## Files

```
contract/real_world_oracle_v2.py — prior revision, kept for reference only
contract/real_world_oracle_v3.py — the current Intelligent Contract
tests/test_real_world_oracle.py  — gltest-style test suite (targets v3)
docs/README.md                   — this file
```
