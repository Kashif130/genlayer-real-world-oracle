# { "Depends": "py-genlayer:test" }
"""
RealWorldOracle — a reusable Intelligent Contract primitive for resolving
ambiguous, real-world questions on-chain via GenLayer validator consensus.

Any other contract (prediction market, insurance payout, DAO conditional,
escrow release, etc.) can compose against this oracle by calling
`request_resolution()` and later reading `get_question()` for the
finalized answer.

Design summary
---------------
Lifecycle:  PROPOSED -> RESOLVING -> RESOLVED -> (DISPUTED -> RESOLVING) -> FINALIZED

1. Anyone proposes a question with a resolution criteria (what counts as a
   valid source / how to decide) and an optional source hint (a URL).
2. Anyone can trigger resolution. GenLayer validators independently
   research the question (web access where a source is given, general
   knowledge/reasoning otherwise) and return a structured verdict. This is
   the non-deterministic block, gated by a *non-comparative* equivalence
   principle: validators are not compared for byte-identical output, they
   are compared for whether their independently-produced verdict satisfies
   the same criteria (semantic agreement), which is what real-world
   ambiguous questions require.
3. The result is stored and a dispute window opens. Anyone can dispute by
   staking a bond; a dispute re-triggers resolution and is marked as an
   escalation round.
4. After the dispute window closes with no challenge (or after a dispute
   round resolves), the question can be finalized. Finalized answers are
   immutable and are what dependent contracts should read.

This separates "get an answer" (cheap, can be wrong, has a challenge
window) from "trust an answer" (finalized, used for real money movement),
which is the standard oracle safety pattern applied to LLM consensus.
"""

from genlayer import *
import typing
import json


# ---------------------------------------------------------------------------
# Storage types
# ---------------------------------------------------------------------------

class Status:
    PROPOSED = 0
    RESOLVING = 1
    RESOLVED = 2
    DISPUTED = 3
    FINALIZED = 4


@allow_storage
class Question:
    asker: Address
    text: str
    criteria: str          # what counts as a valid/complete answer
    source_hint: str        # optional URL; empty string if none
    status: u256
    answer: str              # the current verdict's short answer
    confidence: str          # "high" | "medium" | "low" as reported by validators
    reasoning: str            # short justification, capped at storage layer by caller
    resolved_round: u256      # how many resolution rounds have run (0, 1, 2...)
    dispute_bond: u256        # total bond staked against the current answer
    disputer: Address         # last disputer (zero address if none)
    created_at: u256
    finalized: bool


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

class RealWorldOracle(gl.Contract):
    questions: TreeMap[u256, Question]
    next_id: u256
    dispute_window_blocks: u256   # how many "ticks" (caller-driven, see note below)
    min_dispute_bond: u256
    owner: Address

    def __init__(self, min_dispute_bond: int = 0, dispute_window_blocks: int = 1):
        self.next_id = u256(0)
        self.min_dispute_bond = u256(min_dispute_bond)
        self.dispute_window_blocks = u256(dispute_window_blocks)
        self.owner = gl.message.sender_address
        self.questions = TreeMap()

    # -----------------------------------------------------------------
    # 1. Propose a question
    # -----------------------------------------------------------------
    @gl.public.write
    def propose_question(self, text: str, criteria: str, source_hint: str = "") -> u256:
        """
        Register a new question to be resolved by validator consensus.

        text:         the question in plain language, e.g.
                       "Did the Lahore Qalandars win the 2026 PSL final?"
        criteria:     how a validator should decide, e.g.
                       "Answer YES or NO based on the official PSL result.
                        Cite the match date and final score if available."
        source_hint:  optional URL validators should check first (e.g. an
                       official results page). Leave empty to let each
                       validator find its own sources / use general
                       knowledge, which is appropriate for less
                       time-sensitive questions.
        """
        if len(text.strip()) == 0:
            raise Exception("question text cannot be empty")
        if len(criteria.strip()) == 0:
            raise Exception("resolution criteria cannot be empty")

        qid = self.next_id
        self.next_id = u256(int(self.next_id) + 1)

        q = Question(
            asker=gl.message.sender_address,
            text=text,
            criteria=criteria,
            source_hint=source_hint,
            status=u256(Status.PROPOSED),
            answer="",
            confidence="",
            reasoning="",
            resolved_round=u256(0),
            dispute_bond=u256(0),
            disputer=Address("0x0000000000000000000000000000000000000000"),
            created_at=u256(0),
            finalized=False,
        )
        self.questions[qid] = q
        return qid

    # -----------------------------------------------------------------
    # 2. Resolve — the non-deterministic consensus step
    # -----------------------------------------------------------------
    @gl.public.write
    def request_resolution(self, question_id: int) -> None:
        """
        Trigger validator consensus to produce (or re-produce, after a
        dispute) a verdict for a question.

        This is the core Intelligent Contract primitive: each validator
        independently researches the question and returns a structured
        verdict. Validators are reconciled with a *non-comparative*
        equivalence principle — they don't need identical text, they need
        to independently satisfy the same resolution criteria. This is
        what makes the oracle usable for genuinely ambiguous real-world
        questions (sports results, news events, weather, "did X happen"),
        as opposed to `strict_eq`, which only works when the underlying
        fact is already byte-deterministic (e.g. hashing a fixed webpage).
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status == u256(Status.FINALIZED):
            raise Exception("question already finalized")

        text = q.text
        criteria = q.criteria
        source_hint = q.source_hint

        def get_verdict() -> str:
            # Non-deterministic block. Must contain any web/LLM calls and
            # must take no external arguments (closure-captured values only)
            # per GenVM's isolation rules for non-deterministic execution.
            context = ""
            if source_hint:
                try:
                    context = gl.nondet.web.get(source_hint)
                except Exception:
                    context = "(source_hint unreachable; use general knowledge)"

            prompt = f"""
You are a neutral fact-checking validator for an on-chain oracle.

Question: {text}

Resolution criteria: {criteria}

{"Reference material fetched from the suggested source:\n" + context[:4000] if context else "No source was provided; use your best available knowledge and reasoning."}

Research and answer the question. Respond ONLY with JSON in this exact
shape, nothing else:
{{
  "answer": "<short, specific answer, e.g. YES, NO, a name, a number, or UNRESOLVABLE if genuinely unanswerable>",
  "confidence": "high" | "medium" | "low",
  "reasoning": "<one or two sentences justifying the answer against the criteria>"
}}
""".strip()
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
                "different named outcomes), the verdicts are NOT equivalent."
            ),
        )

        try:
            parsed = json.loads(raw)
            answer = str(parsed.get("answer", "")).strip()
            confidence = str(parsed.get("confidence", "medium")).strip().lower()
            reasoning = str(parsed.get("reasoning", "")).strip()
        except Exception:
            answer = raw.strip()[:200]
            confidence = "low"
            reasoning = "Validator response was not valid JSON; raw text stored as answer."

        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        q.answer = answer
        q.confidence = confidence
        q.reasoning = reasoning[:500]
        q.status = u256(Status.RESOLVED)
        q.resolved_round = u256(int(q.resolved_round) + 1)
        q.dispute_bond = u256(0)
        q.disputer = Address("0x0000000000000000000000000000000000000000")
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 3. Dispute — challenge a resolved answer, triggers re-resolution
    # -----------------------------------------------------------------
    @gl.public.write.payable
    def dispute_answer(self, question_id: int) -> None:
        """
        Challenge the current answer by staking a bond. This does not
        overturn the answer directly — it marks the question DISPUTED and
        the next call to `request_resolution` will run a fresh, independent
        validator round. Comparing the two independent rounds (old
        `reasoning`/`answer` vs the new one) lets downstream contracts or
        a human reviewer judge whether the dispute was warranted.

        A minimum bond (set at deployment) discourages spam disputes.
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status != u256(Status.RESOLVED):
            raise Exception("only a RESOLVED question can be disputed")
        if gl.message.value < int(self.min_dispute_bond):
            raise Exception(f"dispute bond must be at least {int(self.min_dispute_bond)}")

        q.status = u256(Status.DISPUTED)
        q.dispute_bond = u256(int(q.dispute_bond) + int(gl.message.value))
        q.disputer = gl.message.sender_address
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 4. Finalize — lock in the answer for dependent contracts
    # -----------------------------------------------------------------
    @gl.public.write
    def finalize(self, question_id: int) -> None:
        """
        Lock in the current answer as immutable. Allowed once a question is
        RESOLVED (i.e. not currently under an open dispute). In production,
        a real deployment should gate this on an actual block-height/time
        based dispute window rather than caller discretion alone; this
        primitive exposes `dispute_window_blocks` for integrators to check
        off-chain or extend with a scheduled trigger.
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status != u256(Status.RESOLVED):
            raise Exception("question must be in RESOLVED status (no open dispute) to finalize")

        q.status = u256(Status.FINALIZED)
        q.finalized = True
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # Views — for humans and for dependent contracts
    # -----------------------------------------------------------------
    @gl.public.view
    def get_question(self, question_id: int) -> TreeMap[str, typing.Any]:
        q = self.questions.get(u256(question_id), None)
        if q is None:
            raise Exception("unknown question_id")
        return q

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
    def question_count(self) -> int:
        return int(self.next_id)
