# v0.2.0
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
   the same underlying answer given the criteria. That is what real-world,
   ambiguous questions require — a plain strict_eq comparison only works
   when the underlying fact is already byte-deterministic.
3. The result is stored and a dispute window opens. Anyone can dispute by
   staking a bond; a dispute re-triggers resolution as a new round.
4. Once resolved with no open dispute, the question can be finalized.
   Finalized answers are immutable and are what dependent contracts read.
"""

from genlayer import *
from dataclasses import dataclass
import json


# ---------------------------------------------------------------------------
# Status constants (stored as u256 on the Question record)
# ---------------------------------------------------------------------------
STATUS_PROPOSED = 0
STATUS_RESOLVED = 2
STATUS_DISPUTED = 3
STATUS_FINALIZED = 4

ZERO_ADDRESS = Address("0x0000000000000000000000000000000000000000")


@allow_storage
@dataclass
class Question:
    asker: Address
    text: str
    criteria: str
    source_hint: str
    status: u256
    answer: str
    confidence: str
    reasoning: str
    resolved_round: u256
    dispute_bond: u256
    disputer: Address
    finalized: bool


class RealWorldOracle(gl.Contract):
    questions: TreeMap[u256, Question]
    next_id: u256
    min_dispute_bond: u256
    owner: Address

    # -----------------------------------------------------------------
    # Constructor
    # -----------------------------------------------------------------
    def __init__(self, min_dispute_bond: int):
        self.next_id = u256(0)
        self.min_dispute_bond = u256(min_dispute_bond)
        self.owner = gl.message.sender_address

    # -----------------------------------------------------------------
    # 1. Propose a question
    # -----------------------------------------------------------------
    @gl.public.write
    def propose_question(self, text: str, criteria: str, source_hint: str) -> None:
        """
        Register a new question to be resolved by validator consensus.

        text:         the question in plain language, e.g.
                       "Did the Lahore Qalandars win the 2026 PSL final?"
        criteria:     how a validator should decide, e.g.
                       "Answer YES or NO based on the official PSL result."
        source_hint:  optional URL validators should check first. Pass an
                       empty string to let each validator rely on its own
                       research / general knowledge instead.
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
            status=u256(STATUS_PROPOSED),
            answer="",
            confidence="",
            reasoning="",
            resolved_round=u256(0),
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

        def get_verdict() -> str:
            # Non-deterministic block: any web/LLM calls happen here, in a
            # closure that takes no external arguments (GenVM isolation
            # rule for non-deterministic execution). Must return a plain
            # string — eq_principle.prompt_non_comparative asserts on that.
            context = ""
            if source_hint:
                try:
                    context = gl.nondet.web.get(source_hint)
                except Exception:
                    context = ""

            reference = (
                ("Reference material fetched from the suggested source:\n" + context[:4000])
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
                "different named outcomes), the verdicts are NOT equivalent."
            ),
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

        q.answer = answer
        q.confidence = confidence
        q.reasoning = reasoning[:500]
        q.status = u256(STATUS_RESOLVED)
        q.resolved_round = u256(int(q.resolved_round) + 1)
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
        Marks the question DISPUTED; the next request_resolution() call
        runs a fresh, independent validator round.
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status != u256(STATUS_RESOLVED):
            raise Exception("only a RESOLVED question can be disputed")
        if gl.message.value < int(self.min_dispute_bond):
            raise Exception("dispute bond is below the minimum required")

        q.status = u256(STATUS_DISPUTED)
        q.dispute_bond = u256(int(q.dispute_bond) + int(gl.message.value))
        q.disputer = gl.message.sender_address
        self.questions[qid] = q

    # -----------------------------------------------------------------
    # 4. Finalize — lock in the answer for dependent contracts
    # -----------------------------------------------------------------
    @gl.public.write
    def finalize(self, question_id: int) -> None:
        """
        Lock in the current answer as immutable. Only allowed from
        RESOLVED (i.e. no open dispute).
        """
        qid = u256(question_id)
        q = self.questions.get(qid, None)
        if q is None:
            raise Exception("unknown question_id")
        if q.status != u256(STATUS_RESOLVED):
            raise Exception("question must be RESOLVED with no open dispute to finalize")

        q.status = u256(STATUS_FINALIZED)
        q.finalized = True
        self.questions[qid] = q

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
    def question_count(self) -> int:
        return int(self.next_id)
