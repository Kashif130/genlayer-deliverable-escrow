# v0.1.0
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
DeliverableEscrow — a reusable Intelligent Contract primitive that holds
funds and releases them only when GenLayer validators independently agree
a submitted deliverable satisfies stated acceptance criteria.

This turns "did the freelancer actually deliver what was agreed" from a
manual, trust-based, or off-chain-arbitrated question into an on-chain
judgment call made by consensus — useful for freelance work, bounties,
milestone-based payments, and any escrow where the deliverable can't be
checked by a simple deterministic rule (a file exists, a hash matches),
but needs actual reading/judgment (does this report cover the brief, does
this code satisfy the spec, was this design delivered as described).

Lifecycle
---------
FUNDED -> SUBMITTED -> (APPROVED -> RELEASED) | (REJECTED -> back to SUBMITTED-eligible, or REFUNDED if enabled)

1. Payer creates an escrow: deposits funds, names a payee, states the
   deliverable brief and acceptance criteria.
2. Payee submits proof of work (a description and/or a URL to the actual
   deliverable).
3. Anyone can trigger verification. Validators independently review the
   submission against the criteria and return a structured verdict
   (APPROVED / REJECTED / NEEDS_REVISION + reasoning). This is the
   non-deterministic step, reconciled with a non-comparative equivalence
   principle: validators don't need identical wording, they need to
   independently reach the same judgment given the same criteria.
4. If APPROVED, the payee can withdraw the funds — no further action
   needed. If REJECTED or NEEDS_REVISION, the payee can revise and
   resubmit, or — if the payer enabled refunds at creation time — the
   payer can reclaim funds at any point before approval.

This separates "judge the work" (LLM consensus, can be re-run) from
"move the money" (a single, guarded withdraw path), which is the standard
escrow safety pattern applied to a subjective/judged deliverable instead
of a deterministic condition.
"""

from genlayer import *
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_FUNDED = 0        # payer deposited, waiting on payee to submit
STATUS_SUBMITTED = 1     # payee submitted, waiting on verification
STATUS_APPROVED = 2      # validators approved, payee can withdraw
STATUS_REJECTED = 3      # validators rejected; payee may resubmit
STATUS_RELEASED = 4      # funds withdrawn by payee — terminal
STATUS_REFUNDED = 5      # funds returned to payer — terminal

ZERO_ADDRESS = Address("0x0000000000000000000000000000000000000000")


@allow_storage
@dataclass
class Escrow:
    payer: Address
    payee: Address
    amount: u256
    brief: str                # what the deliverable should be
    criteria: str              # how validators should judge it
    submission: str             # payee's proof-of-work description, empty until submitted
    deliverable_url: str         # optional URL to the actual artifact validators must fetch
    status: u256
    verdict_reasoning: str
    submit_count: u256          # how many times the payee has submitted
    refund_enabled: bool        # whether the payer may reclaim funds if never approved
    created_at: u256
    last_raw_response: str      # debug: last raw validator output before parsing


class DeliverableEscrow(gl.Contract):
    escrows: TreeMap[u256, Escrow]
    next_id: u256
    _require_deliverable_url: TreeMap[u256, bool]

    def __init__(self):
        self.next_id = u256(0)

    # -----------------------------------------------------------------
    # 1. Fund an escrow
    # -----------------------------------------------------------------
    @gl.public.write.payable
    def create_escrow(
        self,
        payee: str,
        brief: str,
        criteria: str,
        refund_enabled: bool,
        require_deliverable_url: bool = True,
    ) -> None:
        """
        Deposit funds into a new escrow.

        payee:            address (as a hex string) that will submit the
                           deliverable and receive funds on approval.
        brief:             what is being paid for, e.g. "A 500-word blog
                           post about GenLayer's consensus model."
        criteria:          how validators should judge the submission,
                           e.g. "Approve if the linked post is at least
                           400 words, specifically discusses GenLayer, and
                           is not plagiarized boilerplate."
        refund_enabled:    if True, the payer may call refund() at any
                           time the escrow is not APPROVED/RELEASED, to
                           reclaim funds (e.g. payee unresponsive or
                           delivering unacceptable work repeatedly). If
                           False, funds can only ever leave via an
                           APPROVED verdict + withdraw() — useful when the
                           payee wants a stronger guarantee they'll be
                           paid once they deliver acceptable work.
        require_deliverable_url: if True (default), submit_deliverable()
                           requires a fetchable URL to the actual artifact,
                           so validators judge the real deliverable rather
                           than only the payee's description of it. Set to
                           False only for briefs with no fetchable artifact
                           (e.g. purely off-chain or in-person work).
        """
        if gl.message.value <= 0:
            raise Exception("escrow must be funded with a positive amount")
        if len(brief.strip()) == 0:
            raise Exception("brief cannot be empty")
        if len(criteria.strip()) == 0:
            raise Exception("acceptance criteria cannot be empty")

        payee_addr = Address(payee)
        if payee_addr == gl.message.sender_address:
            raise Exception("payee cannot be the same as payer")

        eid = self.next_id
        self.next_id = u256(int(self.next_id) + 1)

        e = Escrow(
            payer=gl.message.sender_address,
            payee=payee_addr,
            amount=u256(int(gl.message.value)),
            brief=brief,
            criteria=criteria,
            submission="",
            deliverable_url="",
            status=u256(STATUS_FUNDED),
            verdict_reasoning="",
            submit_count=u256(0),
            refund_enabled=refund_enabled,
            created_at=u256(0),
            last_raw_response="",
        )
        self.escrows[eid] = e
        self._require_deliverable_url[eid] = require_deliverable_url

    # -----------------------------------------------------------------
    # 2. Payee submits proof of work
    # -----------------------------------------------------------------
    @gl.public.write
    def submit_deliverable(
        self, escrow_id: int, submission: str, deliverable_url: str = ""
    ) -> None:
        """
        Payee submits their proof of work: a description, plus (unless the
        escrow was created with require_deliverable_url=False) a URL
        pointing at the actual deliverable that validators will fetch and
        judge directly — the description alone is never sufficient to
        approve, since it is payee-authored and unverified.
        Callable again after a REJECTED verdict to resubmit revised work.
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if gl.message.sender_address != e.payee:
            raise Exception("only the designated payee can submit")
        if e.status not in (u256(STATUS_FUNDED), u256(STATUS_REJECTED)):
            raise Exception("escrow is not in a state that accepts submissions")
        if len(submission.strip()) == 0:
            raise Exception("submission cannot be empty")

        needs_url = self._require_deliverable_url.get(eid, True)
        if needs_url and len(deliverable_url.strip()) == 0:
            raise Exception(
                "this escrow requires a deliverable_url pointing at the "
                "actual artifact; validators will not judge on the "
                "description alone"
            )

        e.submission = submission
        e.deliverable_url = deliverable_url.strip()
        e.status = u256(STATUS_SUBMITTED)
        e.submit_count = u256(int(e.submit_count) + 1)
        self.escrows[eid] = e

    # -----------------------------------------------------------------
    # 3. Verify — the non-deterministic consensus step
    # -----------------------------------------------------------------
    @gl.public.write
    def verify_deliverable(self, escrow_id: int) -> None:
        """
        Trigger validator consensus to judge the current submission
        against the escrow's acceptance criteria.

        Each validator independently reviews the brief, the criteria, and
        the submission, and returns a structured verdict. Validators are
        reconciled with a non-comparative equivalence principle: they must
        independently reach the same verdict category, not identical
        wording — appropriate for a judgment call rather than a
        deterministic check.
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if e.status != u256(STATUS_SUBMITTED):
            raise Exception("escrow has no pending submission to verify")

        brief = e.brief
        criteria = e.criteria
        submission = e.submission
        deliverable_url = e.deliverable_url

        VALID_VERDICTS = ("APPROVED", "REJECTED", "NEEDS_REVISION")

        def get_verdict() -> str:
            # Non-deterministic block. Closure-captured values only, no
            # external args, must return a plain string.
            #
            # Fetch the actual artifact here, inside the nondet flow, so
            # each validator judges the real deliverable rather than only
            # trusting the payee's own description of it. If there's no
            # URL (require_deliverable_url was disabled at creation) we
            # fall back to judging the description alone, explicitly
            # flagged as such in the prompt.
            if deliverable_url:
                try:
                    artifact = gl.nondet.web.render(deliverable_url, mode="text")
                except Exception as fetch_err:
                    artifact = f"[FETCH FAILED: {fetch_err}]"
                artifact_block = (
                    f"Fetched deliverable content from {deliverable_url}:\n"
                    f"---\n{artifact}\n---\n\n"
                    "Base your verdict on this fetched content, not on the "
                    "payee's description below. If the fetch failed or the "
                    "content is empty/inaccessible, that alone is grounds "
                    "for REJECTED or NEEDS_REVISION — never APPROVED."
                )
            else:
                artifact_block = (
                    "No deliverable URL was provided for this escrow; you "
                    "only have the payee's description to go on. Treat an "
                    "unverifiable claim with appropriate skepticism."
                )

            prompt = (
                "You are a neutral reviewer judging whether a submitted "
                "deliverable satisfies an agreed brief, for an on-chain "
                "escrow release decision.\n\n"
                f"Brief (what was requested): {brief}\n\n"
                f"Acceptance criteria: {criteria}\n\n"
                f"Submission (payee's own description, unverified): {submission}\n\n"
                f"{artifact_block}\n\n"
                "Judge the submission against the brief and criteria.\n\n"
                "Respond in EXACTLY this format, nothing else, no extra "
                "words on line 1:\n"
                "VERDICT: <APPROVED|REJECTED|NEEDS_REVISION>\n"
                "REASON: <one short sentence citing the specific criteria "
                "that were or were not met>"
            )
            return gl.nondet.exec_prompt(prompt)

        raw = gl.eq_principle.prompt_non_comparative(
            get_verdict,
            task="Judge whether a submitted deliverable satisfies an escrow's acceptance criteria.",
            criteria=(
                "Two verdicts are equivalent if they reach the same "
                "'verdict' category (APPROVED, REJECTED, or NEEDS_REVISION) "
                "given the stated brief and acceptance criteria, even if "
                "'reasoning' wording differs. If the verdict category "
                "differs, the two are NOT equivalent."
            ),
        )

        e.last_raw_response = raw[:800]

        # Strict structured parsing: only the labeled VERDICT field on the
        # first non-empty line is authoritative. We do NOT scan the whole
        # response for a keyword — a REJECTED verdict whose reasoning text
        # contains the word "approved" (e.g. "not approved") must never be
        # misread as APPROVED. If the first line isn't an exact, well-formed
        # verdict token, we fail closed to NEEDS_REVISION rather than guess.
        lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
        verdict = "NEEDS_REVISION"
        reasoning = ""

        if lines:
            first = lines[0]
            token = first
            if ":" in first:
                token = first.split(":", 1)[1]
            token = token.strip().strip('"').strip("'").upper()
            # Exact match only — no substring containment check.
            if token in VALID_VERDICTS:
                verdict = token
            # else: malformed first line -> fail closed to NEEDS_REVISION

            if len(lines) > 1:
                reason_line = lines[1]
                if ":" in reason_line:
                    reasoning = reason_line.split(":", 1)[1].strip()
                else:
                    reasoning = reason_line
            elif verdict not in VALID_VERDICTS or token not in VALID_VERDICTS:
                # Malformed output entirely — keep raw for debugging via
                # get_last_raw_response(), but don't try to guess reasoning.
                reasoning = "Validator response did not match the required format."

        if not reasoning:
            reasoning = "No reasoning text returned by validator."

        e.verdict_reasoning = reasoning[:500]
        if verdict == "APPROVED":
            e.status = u256(STATUS_APPROVED)
        else:
            # Both REJECTED and NEEDS_REVISION route back to REJECTED,
            # which re-opens submit_deliverable for a revised attempt.
            e.status = u256(STATUS_REJECTED)
        self.escrows[eid] = e

    # -----------------------------------------------------------------
    # 4a. Payee withdraws after approval
    # -----------------------------------------------------------------
    @gl.public.write
    def withdraw(self, escrow_id: int) -> None:
        """
        Payee withdraws the escrowed funds after an APPROVED verdict.
        Terminal: moves the escrow to RELEASED so it cannot be withdrawn
        twice.
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if gl.message.sender_address != e.payee:
            raise Exception("only the payee can withdraw")
        if e.status != u256(STATUS_APPROVED):
            raise Exception("escrow is not in an APPROVED state")

        amount = int(e.amount)
        payee_addr = e.payee
        e.status = u256(STATUS_RELEASED)
        self.escrows[eid] = e
        gl.get_contract_at(payee_addr).emit_transfer(value=u256(amount))

    # -----------------------------------------------------------------
    # 4b. Payer reclaims funds after the refund window, if never approved
    # -----------------------------------------------------------------
    @gl.public.write
    def refund(self, escrow_id: int) -> None:
        """
        Payer reclaims escrowed funds if the deliverable was never
        approved. Only available if refund_enabled was set to True at
        creation, and only while the escrow is not APPROVED or already
        settled (RELEASED/REFUNDED).
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if gl.message.sender_address != e.payer:
            raise Exception("only the payer can request a refund")
        if e.status in (u256(STATUS_RELEASED), u256(STATUS_REFUNDED)):
            raise Exception("escrow has already been settled")
        if e.status == u256(STATUS_APPROVED):
            raise Exception("deliverable was approved; payer cannot reclaim funds")
        if not e.refund_enabled:
            raise Exception("refund path is disabled for this escrow")

        amount = int(e.amount)
        payer_addr = e.payer
        e.status = u256(STATUS_REFUNDED)
        self.escrows[eid] = e
        gl.get_contract_at(payer_addr).emit_transfer(value=u256(amount))

    # -----------------------------------------------------------------
    # Views
    # -----------------------------------------------------------------
    @gl.public.view
    def get_status(self, escrow_id: int) -> int:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return int(e.status)

    @gl.public.view
    def get_amount(self, escrow_id: int) -> int:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return int(e.amount)

    @gl.public.view
    def get_submission(self, escrow_id: int) -> str:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return e.submission

    @gl.public.view
    def get_verdict_reasoning(self, escrow_id: int) -> str:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return e.verdict_reasoning

    @gl.public.view
    def get_payee(self, escrow_id: int) -> str:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return str(e.payee)

    @gl.public.view
    def get_last_raw_response(self, escrow_id: int) -> str:
        """Debug helper: the raw (unparsed) validator output from the last
        verify_deliverable call, so a mis-parse can be diagnosed."""
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return e.last_raw_response

    @gl.public.view
    def escrow_count(self) -> int:
        return int(self.next_id)
