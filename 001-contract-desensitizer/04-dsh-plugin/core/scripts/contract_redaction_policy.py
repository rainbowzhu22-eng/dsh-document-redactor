"""Shared default-selection policies for transaction fields."""
from __future__ import annotations


RECOMMEND = "recommend"
PRIVACY_FIRST = "privacy_first"
PRESERVE_TRANSACTION = "preserve_transaction"

_POLICY_LABELS = {
    PRIVACY_FIRST: "脱敏交易要素",
    PRESERVE_TRANSACTION: "保留交易要素",
}


def resolve_policy(selection: str, file_extension: str) -> str:
    """Validate an explicit strategy; file format must not choose for the user."""
    selection = str(selection or "").strip().lower()
    if selection not in {PRIVACY_FIRST, PRESERVE_TRANSACTION}:
        raise ValueError("请明确选择金额、日期的处理方式")
    return selection


def excluded_types(policy_id: str) -> frozenset[str]:
    if policy_id == PRESERVE_TRANSACTION:
        return frozenset({"AMOUNT", "DATE", "MONEY"})
    return frozenset()


def label(policy_id: str) -> str:
    return _POLICY_LABELS.get(policy_id, _POLICY_LABELS[PRIVACY_FIRST])


def description(policy_id: str) -> str:
    name = label(policy_id)
    if policy_id == PRESERVE_TRANSACTION:
        return f"策略：{name}；金额、日期默认保留"
    return f"策略：{name}；金额、日期默认脱敏"
