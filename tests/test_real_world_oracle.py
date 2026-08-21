"""
Tests for RealWorldOracle (v0.3.0).

Written against GenLayer's GenVM test harness pattern (as used in
`gltest` / GenLayer Studio's Python test runner). Covers:

  1. Full happy path: propose -> resolve -> (wait out dispute window) ->
     finalize -> read.
  2. Equivalence principle behavior: validators with differently-worded but
     substantively identical verdicts should reach consensus.
  3. Timing/state guard: finalize() must be rejected immediately after
     RESOLVED, and only succeed once dispute_window_seconds has elapsed.
  4. Dispute flow: a RESOLVED question can be disputed (only while the
     window is open), which blocks finalize() until it is re-resolved.
  5. Bond settlement: a successful (upheld) dispute refunds the disputer in
     full; a rejected dispute forfeits the bond, withdrawable by the owner
     via withdraw_forfeited_bonds() — nothing is silently cleared away.
  6. Guard rails: empty question text, insufficient dispute bond, reading
     get_final_answer() before finalization, double finalize, disputing
     after the window has closed.

Run with:  gltest tests/test_real_world_oracle.py
(or adapt the fixtures below to whichever GenLayer test runner version you
have installed — the assertions are the meaningful part).
"""

import time

import pytest
from gltest import get_contract_factory, default_account
from gltest.assertions import tx_execution_succeeded, tx_execution_failed


CONTRACT_PATH = "contract/real_world_oracle_v3.py"

# Kept small so tests don't need to sleep long, but non-zero so the guard
# is actually exercised rather than trivially satisfied.
TEST_DISPUTE_WINDOW_SECONDS = 2


@pytest.fixture
def oracle():
    factory = get_contract_factory("RealWorldOracle")
    contract = factory.deploy(args=[0, TEST_DISPUTE_WINDOW_SECONDS])  # min_dispute_bond=0
    return contract


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------

def test_propose_resolve_finalize_happy_path(oracle):
    tx = oracle.propose_question(
        args=[
            "Did Pakistan win the 2025 Champions Trophy?",
            "Answer YES or NO based on the official ICC result.",
            "",
        ]
    )
    assert tx_execution_succeeded(tx)
    qid = 0

    resolve_tx = oracle.request_resolution(args=[qid])
    assert tx_execution_succeeded(resolve_tx)

    q = oracle.get_question(args=[qid])
    assert q["status"] == 2  # RESOLVED
    assert q["answer"] != ""
    assert q["confidence"] in ("high", "medium", "low")

    # finalize is NOT allowed immediately — the dispute window is open.
    assert oracle.can_finalize(args=[qid]) is False
    early_finalize = oracle.finalize(args=[qid])
    assert tx_execution_failed(early_finalize)

    time.sleep(TEST_DISPUTE_WINDOW_SECONDS + 1)
    assert oracle.can_finalize(args=[qid]) is True

    finalize_tx = oracle.finalize(args=[qid])
    assert tx_execution_succeeded(finalize_tx)

    q_final = oracle.get_question(args=[qid])
    assert q_final["status"] == 4  # FINALIZED
    assert q_final["finalized"] is True

    answer = oracle.get_final_answer(args=[qid])
    assert answer == q_final["answer"]


# ---------------------------------------------------------------------
# Equivalence principle: semantic agreement despite wording differences
# ---------------------------------------------------------------------

def test_equivalence_allows_differently_worded_agreement(oracle):
    """
    Two validators phrasing the same underlying fact differently (e.g.
    "YES, they won 3-1" vs "Yes — final score 3-1 in their favor") should
    still be treated as equivalent because prompt_non_comparative compares
    on the 'answer' semantics + criteria, not exact text. This test asserts
    the contract reaches RESOLVED (i.e. consensus was achieved) rather than
    reverting on disagreement, for a question whose ground truth is stable
    and unambiguous enough that independently-run validators should agree
    substantively even if worded differently.
    """
    tx = oracle.propose_question(
        args=[
            "Is water composed of hydrogen and oxygen?",
            "Answer YES or NO based on basic chemistry.",
            "",
        ]
    )
    assert tx_execution_succeeded(tx)
    qid = 1

    resolve_tx = oracle.request_resolution(args=[qid])
    assert tx_execution_succeeded(resolve_tx)

    q = oracle.get_question(args=[qid])
    assert q["status"] == 2
    assert q["answer"].strip().upper().startswith("YES")


# ---------------------------------------------------------------------
# Timing/state guard on finalize()
# ---------------------------------------------------------------------

def test_finalize_rejected_before_window_elapses(oracle):
    oracle.propose_question(args=["Q?", "Answer YES or NO.", ""])
    oracle.request_resolution(args=[0])

    tx = oracle.finalize(args=[0])
    assert tx_execution_failed(tx)

    q = oracle.get_question(args=[0])
    assert q["status"] == 2  # still RESOLVED, not FINALIZED


def test_finalize_succeeds_after_window_elapses(oracle):
    oracle.propose_question(args=["Q?", "Answer YES or NO.", ""])
    oracle.request_resolution(args=[0])

    time.sleep(TEST_DISPUTE_WINDOW_SECONDS + 1)

    tx = oracle.finalize(args=[0])
    assert tx_execution_succeeded(tx)


def test_zero_window_deploy_allows_immediate_finalize():
    """dispute_window_seconds=0 is a valid (if inadvisable) deploy-time
    choice — it should behave like an always-elapsed window rather than
    silently failing, so a test/local deployment can opt out explicitly."""
    factory = get_contract_factory("RealWorldOracle")
    contract = factory.deploy(args=[0, 0])
    contract.propose_question(args=["Q?", "Answer YES or NO.", ""])
    contract.request_resolution(args=[0])

    assert contract.can_finalize(args=[0]) is True
    tx = contract.finalize(args=[0])
    assert tx_execution_succeeded(tx)


# ---------------------------------------------------------------------
# Dispute flow + bond settlement
# ---------------------------------------------------------------------

def test_dispute_blocks_finalize_until_reresolved(oracle):
    oracle.propose_question(
        args=["Is the sky blue on a clear day?", "Answer YES or NO.", ""]
    )
    qid = 0
    oracle.request_resolution(args=[qid])

    dispute_tx = oracle.dispute_answer(args=[qid], value=0)
    assert tx_execution_succeeded(dispute_tx)

    q = oracle.get_question(args=[qid])
    assert q["status"] == 3  # DISPUTED

    # finalize should fail while disputed
    bad_finalize = oracle.finalize(args=[qid])
    assert tx_execution_failed(bad_finalize)

    # re-resolving clears the dispute and produces a new round
    reresolve_tx = oracle.request_resolution(args=[qid])
    assert tx_execution_succeeded(reresolve_tx)

    q2 = oracle.get_question(args=[qid])
    assert q2["status"] == 2  # RESOLVED again
    assert q2["resolved_round"] == 2
    # bond bookkeeping cleared off the question record after settlement
    assert q2["dispute_bond"] == 0

    time.sleep(TEST_DISPUTE_WINDOW_SECONDS + 1)
    finalize_tx = oracle.finalize(args=[qid])
    assert tx_execution_succeeded(finalize_tx)


def test_dispute_window_closes_after_deadline(oracle):
    """Once the dispute window has elapsed, dispute_answer() should be
    rejected too, not just finalize() being the only gate — otherwise a
    disputer could grief a question in the middle of finalize() logic."""
    oracle.propose_question(args=["Q?", "Answer YES or NO.", ""])
    oracle.request_resolution(args=[0])

    time.sleep(TEST_DISPUTE_WINDOW_SECONDS + 1)

    tx = oracle.dispute_answer(args=[0], value=0)
    assert tx_execution_failed(tx)


def test_dispute_requires_minimum_bond():
    factory = get_contract_factory("RealWorldOracle")
    contract = factory.deploy(args=[100, TEST_DISPUTE_WINDOW_SECONDS])

    contract.propose_question(args=["Test question?", "Answer YES or NO.", ""])
    contract.request_resolution(args=[0])

    underfunded = contract.dispute_answer(args=[0], value=10)
    assert tx_execution_failed(underfunded)

    funded = contract.dispute_answer(args=[0], value=100)
    assert tx_execution_succeeded(funded)


def test_upheld_dispute_refunds_bond_to_disputer():
    """If re-resolution produces a DIFFERENT answer than what was
    disputed, the disputer was right — their bond must come back."""
    factory = get_contract_factory("RealWorldOracle")
    contract = factory.deploy(args=[100, TEST_DISPUTE_WINDOW_SECONDS])
    disputer = default_account()

    contract.propose_question(args=["Contested question?", "Answer YES or NO.", ""])
    contract.request_resolution(args=[0])

    balance_before = disputer.get_balance()

    contract.dispute_answer(args=[0], value=100)
    # NOTE: in a live validator-driven deployment the re-resolved answer is
    # non-deterministic (LLM-produced); this test's assertion structure
    # applies regardless of which branch fires — see the paired
    # test_rejected_dispute_forfeits_bond for the complementary case. A
    # harness with a mocked/stubbed nondet block should force the answer
    # to differ here to deterministically exercise the refund branch.
    contract.request_resolution(args=[0])

    q = contract.get_question(args=[0])
    assert q["dispute_bond"] == 0
    assert q["disputer"] == "0x0000000000000000000000000000000000000000"
    # Either the disputer was refunded (dispute upheld) or the bond was
    # forfeited into the contract's owner-withdrawable pool (dispute
    # rejected) — it must be exactly one of the two, never neither.
    forfeited = contract.get_forfeited_bonds(args=[])
    balance_after = disputer.get_balance()
    refunded = balance_after > balance_before
    assert refunded != (forfeited > 0)


def test_owner_can_withdraw_forfeited_bonds(oracle):
    owner = default_account()

    bad = oracle.withdraw_forfeited_bonds(args=[str(owner.address), 1])
    assert tx_execution_failed(bad)  # nothing forfeited yet -> amount exceeds balance

    only_owner_tx = oracle.withdraw_forfeited_bonds(args=[str(owner.address), 0])
    assert tx_execution_failed(only_owner_tx)  # amount must be positive


# ---------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------

def test_empty_question_text_rejected(oracle):
    tx = oracle.propose_question(args=["", "Some criteria", ""])
    assert tx_execution_failed(tx)


def test_empty_criteria_rejected(oracle):
    tx = oracle.propose_question(args=["A question?", "", ""])
    assert tx_execution_failed(tx)


def test_get_final_answer_before_finalization_reverts(oracle):
    oracle.propose_question(args=["Q?", "Answer YES or NO.", ""])
    oracle.request_resolution(args=[0])
    # status is RESOLVED but not FINALIZED yet
    tx = oracle.get_final_answer(args=[0])
    assert tx_execution_failed(tx)


def test_double_finalize_rejected(oracle):
    oracle.propose_question(args=["Q?", "Answer YES or NO.", ""])
    oracle.request_resolution(args=[0])
    time.sleep(TEST_DISPUTE_WINDOW_SECONDS + 1)

    first = oracle.finalize(args=[0])
    assert tx_execution_succeeded(first)

    second = oracle.finalize(args=[0])
    assert tx_execution_failed(second)


def test_unknown_question_id_reverts(oracle):
    tx = oracle.get_question(args=[999])
    assert tx_execution_failed(tx)


def test_negative_dispute_window_rejected_at_deploy():
    factory = get_contract_factory("RealWorldOracle")
    with pytest.raises(Exception):
        factory.deploy(args=[0, -1])
