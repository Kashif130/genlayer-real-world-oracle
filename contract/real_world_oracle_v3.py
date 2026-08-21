# v0.3.0
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
RealWorldOracle — a reusable Intelligent Contract primitive for resolving
ambiguous, real-world questions on-chain via GenLayer validator consensus.

Any other contract (prediction market, insurance payout, DAO conditional,
escrow release, etc.) can compose against this oracle by proposing a
question and later reading get_final_answer() for the finalized verdict.

Lifecycle:  PROPOSED -> RESOLVED -> (DISPUTED -> RESOLVED again) -> FINALIZED

1. Anyone proposes a question with a resolution criteria (what counts as a
   valid answer) and an optional source hint (a URL).
2. Anyone can trigger resolution. GenLayer validators independently
   research the question and return a structured verdict. This is the
   non-deterministic block, reconciled with a *non-comparative* equivalence
   principle: validators are not compared for byte-identical output, they
   are compared for whether their independently-produced verdict agrees on
   the same underlying answer given the criteria.
3. The result is stored and a REAL dispute window opens (wall-clock time,
   `dispute_window_seconds`, set at deploy time). Anyone can dispute during
   that window by staking a bond >= min_dispute_bond; a dispute re-triggers
   resolution as a new round. finalize() is rejected until the window has
   elapsed, and rejected outright while a dispute is open.
4. Once resolved, the window has elapsed, and there's no open dispute, the
   question can be finalized. Finalized answers are immutable and are what
   dependent contracts read.

v0.3.0 changes (fixes to the challenge lifecycle flagged in review):
  - finalize() is no longer callable the instant a question is RESOLVED.
    A real timing guard (`dispute_window_seconds`, wall-clock via
    datetime.now(), which is deterministic across GenVM validators) must
    elapse first, giving disputers a genuine window to act in.
  - A posted dispute bond is no longer silently zeroed out on
    re-resolution. It is now settled:
      * dispute upheld (the re-resolved answer differs from the answer
        that was disputed) -> bond is refunded in full to the disputer
        via gl.ContractAt(...).emit_transfer(...).
      * dispute rejected (re-resolved answer matches the disputed one)
        -> bond is forfeited into `forfeited_bonds`, withdrawable by the
        contract owner via withdraw_forfeited_bonds(). This is the
        deterrent against frivolous/spam disputes.
    Either way the bond amount is accounted for before it's cleared from
    the question record — nothing just disappears.
  - Added get_question() view (full record) and a few new views
    (get_dispute_window_seconds, get_forfeited_bonds, get_resolved_at)
    needed to inspect/verify the above from outside the contract.
"""

from genlayer import *
from dataclasses import dataclass
import datetime
import json


# ---------------------------------------------------------------------------
# Status constants (stored as u256 on the Question record)
# ---------------------------------------------------------------------------
STATUS_PROPOSED = 0
STATUS_RESOLVED = 2
STATUS_DISPUTED = 3
STATUS_FINALIZED = 4

ZERO_ADDRESS = Address("0x0000000000000000000000000000000000000000")

# Sentinel for "never resolved yet" — a question in PROPOSED status has no
# real resolved_at, so finalize() (which requires RESOLVED anyway) never
# reads this meaningfully. Kept timezone-naive to match datetime.now().
_NEVER_RESOLVED = datetime.datetime.min


@allow_storage
@dataclass
class Question:
    asker: Address
    text: str
    criteria: str
    source_hint: str
    requires_evidence: bool
    status: u256
    answer: str
    confidence: str
    reasoning: str
    resolved_round: u256
    resolved_at: datetime.datetime
    dispute_bond: u256
    disputer: Address
    finalized: bool


class RealWorldOracle(gl.Contract):
    questions: TreeMap[u256, Question]
    next_id: u256
    min_dispute_bond: u256
    dispute_window_seconds: u256
    forfeited_bonds: u256
    owner: Address

    # -----------------------------------------------------------------
    # Constructor
    # -----------------------------------------------------------------
    def __init__(self, min_dispute_bond: int, dispute_window_seconds: int):
        if dispute_window_seconds < 0:
            raise Exception("dispute_window_seconds cannot be negative")
        self.next_id = u256(0)
        self.min_dispute_bond = u256(min_dispute_bond)
        self.dispute_window_seconds = u256(dispute_window_seconds)
        self.forfeited_bonds = u256(0)
        self.owner = gl.message.sender_address

    # -----------------------------------------------------------------
    # 1. Propose a question
    # -----------------------------------------------------------------
    @gl.public.write
    def propose_question(
        self,
        text: str,
        criteria: str,
        source_hint: str,
        requires_evidence: bool = True,
    ) -> None:
        """
        Register a new question to be resolved by validator consensus.

        text:               the question in plain language, e.g.
                             "Did the Lahore Qalandars win the 2026 PSL final?"
        criteria:           how a validator should decide, e.g.
                             "Answer YES or NO based on the official PSL result."
        source_hint:        URL validators should fetch and check. Required
                             (non-empty) unless requires_evidence is set to
                             False.
        requires_evidence:  if True (default), this question can only be
                             resolved by grounding on fetched source content
                             — source_hint must be non-empty, and resolution
                             fails closed (reverts, question stays
                             PROPOSED/DISPUTED) if the source cannot be
                             fetched, rather than silently falling back to
                             the model's own knowledge. Set to False only
                             for questions that are genuinely not externally
                             verifiable (pure logic/math, or subjective
                             judgment calls with no source of truth) — in
                             that case source_hint may be left empty.
        """
        if len(text.strip()) == 0:
            raise Exception("question text cannot be empty")
        if len(criteria.strip()) == 0:
            raise Exception("resolution criteria cannot be empty")
        if requires_evidence and len(source_hint.strip()) == 0:
            raise Exception(
                "requires_evidence is True but no source_hint was given; "
                "either provide a URL validators can fetch, or explicitly "
                "set requires_evidence=False for a non-externally-verifiable "
                "question"
            )

        qid = self.next_id
        self.next_id = u256(int(self.next_id) + 1)

        q = Question(
            asker=gl.message.sender_address,
            text=text,
            criteria=criteria,
            source_hint=source_hint,
            requires_evidence=requires_evidence,
            status=u256(STATUS_PROPOSED),
            answer="",
            confidence="",
            reasoning="",
            resolved_round=u256(0),
            resolved_at=_NEVER_RESOLVED,
            dispute_bond=u256(0),
            disputer=ZERO_ADDRESS,
            finalized=False,
        )
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 2. Resolve — the non-deterministic consensus step
    # -----------------------------------------------------------------
    @gl.public.write
    def request_resolution(self, question_id: int) -> None:
        """
        Trigger validator consensus to produce (or re-produce, after a
        dispute) a verdict for a question.

        Each validator independently researches the question and returns
        a structured verdict. Validators are reconciled with a
        non-comparative equivalence principle: they must independently
        satisfy the same resolution criteria, not produce identical text.

        If this round is re-resolving a DISPUTED question, the posted
        bond is settled here (see module docstring) before being cleared.
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status == u256(STATUS_FINALIZED):
            raise Exception("question already finalized")

        text = q.text
        criteria = q.criteria
        source_hint = q.source_hint
        requires_evidence = q.requires_evidence

        FETCH_FAILED_SENTINEL = "\u0000FETCH_FAILED\u0000"

        def get_verdict() -> str:
            # Non-deterministic block: any web/LLM calls happen here, in a
            # closure that takes no external arguments (GenVM isolation
            # rule for non-deterministic execution). Must return a plain
            # string — eq_principle.prompt_non_comparative asserts on that.
            context = ""
            fetch_failed = False
            if source_hint:
                try:
                    # render(mode='text') returns a plain string, unlike
                    # web.get() which returns a Response object — using
                    # the wrong one here silently breaks the empty/failure
                    # check below (a Response is always truthy).
                    context = gl.nondet.web.render(source_hint, mode="text")
                    if not context or not context.strip():
                        fetch_failed = True
                except Exception:
                    fetch_failed = True

            if requires_evidence and fetch_failed:
                # Fail closed: this question was marked as requiring
                # contract-side evidence, and the source could not be
                # acquired. Do NOT fall back to the model's own knowledge —
                # signal failure back to the deterministic caller instead
                # of guessing.
                return FETCH_FAILED_SENTINEL

            reference = (
                ("Reference material fetched from the required source:\n" + context[:4000])
                if context
                else "No source was provided; use your best available knowledge and reasoning."
            )

            prompt = (
                "You are a neutral fact-checking validator for an on-chain oracle.\n\n"
                f"Question: {text}\n\n"
                f"Resolution criteria: {criteria}\n\n"
                f"{reference}\n\n"
                "Research and answer the question. Respond ONLY with JSON in this "
                "exact shape, nothing else:\n"
                '{"answer": "<short specific answer, e.g. YES, NO, a name, a number, '
                'or UNRESOLVABLE if genuinely unanswerable>", '
                '"confidence": "high|medium|low", '
                '"reasoning": "<one or two sentences justifying the answer against the criteria>"}'
            )
            return gl.nondet.exec_prompt(prompt)

        raw = gl.eq_principle.prompt_non_comparative(
            get_verdict,
            task="Resolve a real-world factual question for an on-chain oracle.",
            criteria=(
                "Two verdicts are equivalent if they reach the same 'answer' "
                "given the stated resolution criteria, even if the wording of "
                "'reasoning' differs. Different confidence levels are still "
                "equivalent as long as the 'answer' field matches. If the "
                "answer differs in substance (e.g. YES vs NO, or two "
                "different named outcomes), the verdicts are NOT equivalent. "
                "A verdict that is exactly the fetch-failure sentinel is only "
                "equivalent to another instance of that same sentinel."
            ),
        )

        if requires_evidence and FETCH_FAILED_SENTINEL in raw:
            # Evidence was required and validators could not acquire it in
            # consensus. Leave the question exactly as it was (PROPOSED or
            # DISPUTED) rather than resolving on a guess — the caller must
            # retry once the source is reachable, or the question can be
            # amended. Nothing is written to answer/confidence/reasoning,
            # and (important for a DISPUTED question) the bond is left
            # exactly as posted rather than being touched or cleared.
            raise Exception(
                "could not fetch required source evidence; resolution "
                "aborted rather than falling back to model knowledge — "
                "retry once the source URL is reachable"
            )

        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()
            parsed = json.loads(cleaned)
            answer = str(parsed.get("answer", "")).strip()
            confidence = str(parsed.get("confidence", "medium")).strip().lower()
            reasoning = str(parsed.get("reasoning", "")).strip()
        except Exception:
            answer = raw.strip()[:200]
            confidence = "low"
            reasoning = "Validator response was not valid JSON; raw text stored as answer."

        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        # --- capture pre-overwrite state needed for bond settlement -----
        was_disputed = q.status == u256(STATUS_DISPUTED)
        previous_answer = q.answer
        bond_amount = int(q.dispute_bond)
        disputer = q.disputer

        q.answer = answer
        q.confidence = confidence
        q.reasoning = reasoning[:500]
        q.status = u256(STATUS_RESOLVED)
        q.resolved_round = u256(int(q.resolved_round) + 1)
        q.resolved_at = datetime.datetime.now()

        # --- settle the dispute bond, if any, BEFORE clearing it --------
        # Normalized (case/whitespace-insensitive) comparison: the answer
        # field is specified as a short token (YES/NO/a name/a number), so
        # this is a safe deterministic proxy for "did the re-resolved
        # verdict actually change" without recursing into another
        # non-deterministic equivalence check.
        if was_disputed and bond_amount > 0 and disputer != ZERO_ADDRESS:
            dispute_upheld = (
                answer.strip().upper() != previous_answer.strip().upper()
            )
            if dispute_upheld:
                # Disputer was right to challenge — refund their bond.
                gl.ContractAt(disputer).emit_transfer(value=u256(bond_amount))
            else:
                # Dispute rejected — bond is forfeited as the deterrent
                # against frivolous challenges. Held by the contract,
                # withdrawable by the owner via withdraw_forfeited_bonds().
                self.forfeited_bonds = u256(int(self.forfeited_bonds) + bond_amount)

        q.dispute_bond = u256(0)
        q.disputer = ZERO_ADDRESS
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 3. Dispute — challenge a resolved answer, triggers re-resolution
    # -----------------------------------------------------------------
    @gl.public.write.payable
    def dispute_answer(self, question_id: int) -> None:
        """
        Challenge the current answer by staking a bond >= min_dispute_bond.
        Only allowed while the dispute window is still open (i.e. before
        finalize() would be allowed to run). Marks the question DISPUTED;
        the next request_resolution() call runs a fresh, independent
        validator round and settles this bond (see module docstring).
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status != u256(STATUS_RESOLVED):
            raise Exception("only a RESOLVED question can be disputed")
        if self._window_elapsed(q):
            raise Exception(
                "dispute window has closed for this question; it can only "
                "be finalized now"
            )
        if gl.message.value < int(self.min_dispute_bond):
            raise Exception("dispute bond is below the minimum required")

        q.status = u256(STATUS_DISPUTED)
        q.dispute_bond = u256(int(q.dispute_bond) + int(gl.message.value))
        q.disputer = gl.message.sender_address
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 3b. Recover a stuck evidence-required question with a dead source
    # -----------------------------------------------------------------
    @gl.public.write
    def update_source_hint(self, question_id: int, new_source_hint: str) -> None:
        """
        Let the original asker correct source_hint (e.g. a dead/moved URL)
        so an evidence-required question that keeps failing closed in
        request_resolution() can be retried against a reachable source,
        without ever letting resolution silently fall back to the model's
        own knowledge instead. Only allowed while unresolved/unfinalized —
        an already-RESOLVED or FINALIZED answer is not affected retroactively.
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if gl.message.sender_address != q.asker:
            raise Exception("only the original asker can update the source hint")
        if q.status == u256(STATUS_FINALIZED):
            raise Exception("question already finalized")
        if q.requires_evidence and len(new_source_hint.strip()) == 0:
            raise Exception(
                "this question requires evidence; new_source_hint cannot be empty"
            )

        q.source_hint = new_source_hint
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 4. Finalize — lock in the answer for dependent contracts
    # -----------------------------------------------------------------
    @gl.public.write
    def finalize(self, question_id: int) -> None:
        """
        Lock in the current answer as immutable. Only allowed from
        RESOLVED (i.e. no open dispute) AND only once the real dispute
        window (dispute_window_seconds, wall-clock from the moment this
        round resolved) has actually elapsed. This is the timing/state
        guard: RESOLVED alone is no longer sufficient to finalize.
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status != u256(STATUS_RESOLVED):
            raise Exception("question must be RESOLVED with no open dispute to finalize")
        if not self._window_elapsed(q):
            deadline = q.resolved_at + datetime.timedelta(
                seconds=int(self.dispute_window_seconds)
            )
            remaining = (deadline - datetime.datetime.now()).total_seconds()
            raise Exception(
                "dispute window still open; "
                f"{max(int(remaining), 0)} more second(s) before this "
                "question can be finalized"
            )

        q.status = u256(STATUS_FINALIZED)
        q.finalized = True
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 5. Owner — withdraw bonds forfeited by rejected disputes
    # -----------------------------------------------------------------
    @gl.public.write
    def withdraw_forfeited_bonds(self, to: str, amount: int) -> None:
        """
        Pay out accumulated forfeited-dispute-bond balance. Owner-only.
        This is the recovery path for bonds that were forfeited because
        the dispute they backed was rejected on re-resolution — previously
        that value had nowhere to go and was just cleared into nothing.
        """
        if gl.message.sender_address != self.owner:
            raise Exception("only the owner can withdraw forfeited bonds")
        if amount <= 0:
            raise Exception("amount must be positive")
        if amount > int(self.forfeited_bonds):
            raise Exception("amount exceeds available forfeited bond balance")

        self.forfeited_bonds = u256(int(self.forfeited_bonds) - amount)
        gl.ContractAt(Address(to)).emit_transfer(value=u256(amount))

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------
    def _window_elapsed(self, q: Question) -> bool:
        if q.status != u256(STATUS_RESOLVED):
            return False
        deadline = q.resolved_at + datetime.timedelta(
            seconds=int(self.dispute_window_seconds)
        )
        return datetime.datetime.now() >= deadline

    # -----------------------------------------------------------------
    # Views
    # -----------------------------------------------------------------
    @gl.public.view
    def get_answer(self, question_id: int) -> str:
        q = self.questions.get(u256(question_id), None)
        if q is None:
            raise Exception("unknown question_id")
        return q.answer

    @gl.public.view
    def get_status(self, question_id: int) -> int:
        q = self.questions.get(u256(question_id), None)
        if q is None:
            raise Exception("unknown question_id")
        return int(q.status)

    @gl.public.view
    def get_reasoning(self, question_id: int) -> str:
        q = self.questions.get(u256(question_id), None)
        if q is None:
            raise Exception("unknown question_id")
        return q.reasoning

    @gl.public.view
    def get_final_answer(self, question_id: int) -> str:
        """
        Convenience read for dependent contracts: reverts unless the
        question has been finalized, so callers can't accidentally act on
        a disputed or unresolved answer.
        """
        q = self.questions.get(u256(question_id), None)
        if q is None:
            raise Exception("unknown question_id")
        if not q.finalized:
            raise Exception("question is not finalized yet")
        return q.answer

    @gl.public.view
    def get_question(self, question_id: int) -> dict:
        """Full question record, for UIs/integrators/tests."""
        q = self.questions.get(u256(question_id), None)
        if q is None:
            raise Exception("unknown question_id")
        return {
            "asker": str(q.asker),
            "text": q.text,
            "criteria": q.criteria,
            "source_hint": q.source_hint,
            "requires_evidence": q.requires_evidence,
            "status": int(q.status),
            "answer": q.answer,
            "confidence": q.confidence,
            "reasoning": q.reasoning,
            "resolved_round": int(q.resolved_round),
            "resolved_at": q.resolved_at.isoformat(),
            "dispute_bond": int(q.dispute_bond),
            "disputer": str(q.disputer),
            "finalized": q.finalized,
        }

    @gl.public.view
    def can_finalize(self, question_id: int) -> bool:
        """True iff finalize(question_id) would succeed right now."""
        q = self.questions.get(u256(question_id), None)
        if q is None:
            raise Exception("unknown question_id")
        return self._window_elapsed(q)

    @gl.public.view
    def get_dispute_window_seconds(self) -> int:
        return int(self.dispute_window_seconds)

    @gl.public.view
    def get_forfeited_bonds(self) -> int:
        return int(self.forfeited_bonds)

    @gl.public.view
    def question_count(self) -> int:
        return int(self.next_id)
