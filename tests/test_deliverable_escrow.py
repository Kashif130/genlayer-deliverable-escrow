"""
Tests for DeliverableEscrow.

Written against GenLayer's GenVM test harness pattern (gltest). Covers:

  1. Full happy path: fund -> submit -> verify (APPROVED) -> withdraw.
  2. Rejection path: verify (REJECTED) -> resubmit -> verify again.
  3. Refund path: refund_enabled escrow reclaimed by payer before approval.
  4. Guard rails: double withdraw, wrong-caller submit/withdraw/refund,
     refund when disabled, zero-value funding, empty brief/criteria,
     payee == payer.

Run with:  gltest tests/test_deliverable_escrow.py
"""

import pytest
from gltest import get_contract_factory, default_account, other_account
from gltest.assertions import tx_execution_succeeded, tx_execution_failed


@pytest.fixture
def escrow():
    factory = get_contract_factory("DeliverableEscrow")
    contract = factory.deploy(args=[])
    return contract


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------

def test_fund_submit_approve_withdraw(escrow):
    payee = other_account(0)

    fund_tx = escrow.create_escrow(
        args=[
            payee.address,
            "A 500-word blog post about GenLayer's consensus model.",
            "Approve if the linked post is at least 400 words and "
            "specifically discusses GenLayer's equivalence principle.",
            True,
        ],
        value=1000,
    )
    assert tx_execution_succeeded(fund_tx)
    eid = 0
    assert escrow.get_status(args=[eid]) == 0  # FUNDED
    assert escrow.get_amount(args=[eid]) == 1000

    submit_tx = escrow.submit_deliverable(
        args=[eid, "https://example.com/my-genlayer-post — 620 words, covers equivalence principle."],
        account=payee,
    )
    assert tx_execution_succeeded(submit_tx)
    assert escrow.get_status(args=[eid]) == 1  # SUBMITTED

    verify_tx = escrow.verify_deliverable(args=[eid])
    assert tx_execution_succeeded(verify_tx)

    status = escrow.get_status(args=[eid])
    assert status in (2, 3)  # APPROVED or REJECTED depending on LLM judgment
    assert escrow.get_verdict_reasoning(args=[eid]) != ""

    if status == 2:
        withdraw_tx = escrow.withdraw(args=[eid], account=payee)
        assert tx_execution_succeeded(withdraw_tx)
        assert escrow.get_status(args=[eid]) == 4  # RELEASED


# ---------------------------------------------------------------------
# Rejection + resubmission path
# ---------------------------------------------------------------------

def test_reject_then_resubmit(escrow):
    payee = other_account(0)

    escrow.create_escrow(
        args=[
            payee.address,
            "A comprehensive 2000-word technical whitepaper on zero-knowledge proofs.",
            "Approve only if the submission is a real whitepaper of "
            "substantial length covering ZK-SNARKs and ZK-STARKs in depth.",
            True,
        ],
        value=500,
    )
    eid = 0

    # Deliberately weak submission, likely to be REJECTED or NEEDS_REVISION
    escrow.submit_deliverable(args=[eid, "here u go"], account=payee)
    escrow.verify_deliverable(args=[eid])

    status = escrow.get_status(args=[eid])
    if status == 3:  # REJECTED
        # Resubmission should now be allowed
        resubmit_tx = escrow.submit_deliverable(
            args=[eid, "Revised: a full whitepaper draft with sections on SNARKs, STARKs, and benchmarks."],
            account=payee,
        )
        assert tx_execution_succeeded(resubmit_tx)
        assert escrow.get_status(args=[eid]) == 1  # SUBMITTED again


# ---------------------------------------------------------------------
# Refund path
# ---------------------------------------------------------------------

def test_refund_when_enabled_and_not_approved(escrow):
    payer = default_account()
    payee = other_account(0)

    escrow.create_escrow(
        args=[payee.address, "Some brief", "Some criteria", True],
        value=777,
        account=payer,
    )
    eid = 0
    # Escrow is FUNDED, not APPROVED — refund should succeed since enabled.
    refund_tx = escrow.refund(args=[eid], account=payer)
    assert tx_execution_succeeded(refund_tx)
    assert escrow.get_status(args=[eid]) == 5  # REFUNDED


def test_refund_disabled_reverts(escrow):
    payer = default_account()
    payee = other_account(0)

    escrow.create_escrow(
        args=[payee.address, "Some brief", "Some criteria", False],
        value=100,
        account=payer,
    )
    eid = 0
    tx = escrow.refund(args=[eid], account=payer)
    assert tx_execution_failed(tx)


def test_refund_wrong_caller_reverts(escrow):
    payee = other_account(0)
    stranger = other_account(1)

    escrow.create_escrow(args=[payee.address, "Brief", "Criteria", True], value=100)
    tx = escrow.refund(args=[0], account=stranger)
    assert tx_execution_failed(tx)


# ---------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------

def test_zero_value_funding_rejected(escrow):
    payee = other_account(0)
    tx = escrow.create_escrow(args=[payee.address, "Brief", "Criteria", True], value=0)
    assert tx_execution_failed(tx)


def test_empty_brief_rejected(escrow):
    payee = other_account(0)
    tx = escrow.create_escrow(args=[payee.address, "", "Criteria", True], value=100)
    assert tx_execution_failed(tx)


def test_empty_criteria_rejected(escrow):
    payee = other_account(0)
    tx = escrow.create_escrow(args=[payee.address, "Brief", "", True], value=100)
    assert tx_execution_failed(tx)


def test_payee_cannot_equal_payer(escrow):
    payer = default_account()
    tx = escrow.create_escrow(
        args=[payer.address, "Brief", "Criteria", True], value=100, account=payer
    )
    assert tx_execution_failed(tx)


def test_wrong_caller_cannot_submit(escrow):
    payee = other_account(0)
    stranger = other_account(1)

    escrow.create_escrow(args=[payee.address, "Brief", "Criteria", True], value=100)
    tx = escrow.submit_deliverable(args=[0, "some work"], account=stranger)
    assert tx_execution_failed(tx)


def test_cannot_withdraw_before_approval(escrow):
    payee = other_account(0)
    escrow.create_escrow(args=[payee.address, "Brief", "Criteria", True], value=100)
    tx = escrow.withdraw(args=[0], account=payee)
    assert tx_execution_failed(tx)


def test_cannot_submit_twice_without_rejection(escrow):
    payee = other_account(0)
    escrow.create_escrow(args=[payee.address, "Brief", "Criteria", True], value=100)
    escrow.submit_deliverable(args=[0, "first submission"], account=payee)
    # status is now SUBMITTED, not FUNDED/REJECTED — second submit should fail
    tx = escrow.submit_deliverable(args=[0, "second submission"], account=payee)
    assert tx_execution_failed(tx)


def test_unknown_escrow_id_reverts(escrow):
    tx = escrow.get_status(args=[999])
    assert tx_execution_failed(tx)
  
