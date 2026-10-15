"""Реферальные начисления и только связанные с ними возвратные операции."""

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from .db import Ledger


def referral_operation_condition():
    original = aliased(Ledger)
    reversal_has_reward = (
        select(original.id)
        .where(
            original.account_id == Ledger.account_id,
            original.operation == "referral_payment:" + func.split_part(Ledger.operation, ":", 2),
        )
        .exists()
    )
    return or_(
        Ledger.operation.like("referral_payment:%"), Ledger.operation.like("reversal:%") & reversal_has_reward
    )


def earned_total(session, account_id):
    return session.scalar(
        select(func.coalesce(func.sum(Ledger.amount), 0)).where(
            Ledger.account_id == account_id, referral_operation_condition()
        )
    )
