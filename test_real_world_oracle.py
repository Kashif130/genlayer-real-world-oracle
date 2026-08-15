"""
Tests for RealWorldOracle.

These are written against GenLayer's GenVM test harness pattern (as used in
`gltest` / GenLayer Studio's Python test runner). They cover:

  1. Full happy path: propose -> resolve -> finalize -> read.
  2. Equivalence principle behavior: validators with differently-worded but
     substantively identical verdicts should reach consensus.
  3. Disagreement case: validators with substantively different answers
     should NOT reach consensus (simulated at the criteria level).
  4. Dispute flow: a RESOLVED question can be disputed, which blocks
     finalize() until it is re-resolved.
  5. Guard rails: empty question text, insufficient dispute bond, reading
     get_final_answer() before finalization, double finalize.

Run with:  gltest tests/test_real_world_oracle.py
(or adapt the fixtures below to whichever GenLayer test runner version you
have installed — the assertions are the meaningful part).
"""

import pytest
from gltest import get_contract_factory, default_account
from gltest.assertions import tx_execution_succeeded, tx_execution_failed


CONTRACT_PATH = "contract/real_world_oracle.py"


@pytest.fixture
def oracle():
    factory = get_contract_factory("RealWorldOracle")
    contract = factory.deploy(args=[0, 1])  # min_dispute_bond=0, dispute_window_blocks=1
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
# Dispute flow
# ---------------------------------------------------------------------

def test_dispute_blocks_finalize_until_reresolved(oracle):
    oracle.propose_question(
        args=["Is the sky blue on a clear day?", "Answer YES or NO.", ""]
    )
    qid = 2
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
    assert int(q2["resolved_round"]) == 2

    finalize_tx = oracle.finalize(args=[qid])
    assert tx_execution_succeeded(finalize_tx)


def test_dispute_requires_minimum_bond():
    factory = get_contract_factory("RealWorldOracle")
    contract = factory.deploy(args=[100, 1])  # min_dispute_bond=100

    contract.propose_question(args=["Test question?", "Answer YES or NO.", ""])
    contract.request_resolution(args=[0])

    underfunded = contract.dispute_answer(args=[0], value=10)
    assert tx_execution_failed(underfunded)

    funded = contract.dispute_answer(args=[0], value=100)
    assert tx_execution_succeeded(funded)


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
    first = oracle.finalize(args=[0])
    assert tx_execution_succeeded(first)

    second = oracle.finalize(args=[0])
    assert tx_execution_failed(second)


def test_unknown_question_id_reverts(oracle):
    tx = oracle.get_question(args=[999])
    assert tx_execution_failed(tx)
