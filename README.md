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
                                       |
                                  dispute_answer()
                                       |
                                       v
                                   DISPUTED --request_resolution()--> RESOLVED (round 2...)
```

- **PROPOSED**: question registered, no verdict yet.
- **RESOLVED**: validators reached consensus on a verdict this round.
  Not yet safe for downstream contracts to act on — it's inside the
  dispute window.
- **DISPUTED**: someone staked a bond challenging the current verdict.
  `finalize()` is blocked until the question is re-resolved.
- **FINALIZED**: immutable. `get_final_answer()` only returns a value in
  this state — this is the read dependent contracts should use.

Separating "resolved" from "finalized" is the standard oracle safety
pattern (a challenge window before funds move), applied here to LLM-driven
verdicts instead of numeric price feeds.

## Public interface

| Method | Type | Purpose |
|---|---|---|
| `propose_question(text, criteria, source_hint)` | write | Register a new question. Returns `question_id`. |
| `request_resolution(question_id)` | write | Runs the non-deterministic validator consensus step. Callable again after a dispute. |
| `dispute_answer(question_id)` | write, payable | Challenge a `RESOLVED` verdict by staking a bond ≥ `min_dispute_bond`. |
| `finalize(question_id)` | write | Locks in the current verdict. Only allowed from `RESOLVED` (not `DISPUTED`). |
| `get_question(question_id)` | view | Full question record (status, answer, confidence, reasoning, round count, etc). |
| `get_final_answer(question_id)` | view | Reverts unless finalized — safe read for integrators. |
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

This is a primitive, not a product. It doesn't include UI, an incentive
model for validator honesty beyond GenLayer's base consensus, or a bonded
challenge-resolution auction (a real "disputer wins/loses bond" mechanism
would be a natural extension, kept out here to keep the primitive focused
and auditable). It's deliberately generic rather than tied to one use case,
which is what makes it reusable as a dependency for other Intelligent
Contracts.

## Files

```
contract/real_world_oracle.py   — the Intelligent Contract
tests/test_real_world_oracle.py — gltest-style test suite
docs/README.md                  — this file
```
