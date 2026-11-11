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


# 用户规则新增的日期／金额类规则以这些前缀命名，随“保留金额、日期”一并保留；
# 否则新增的签署日、付款日规则会绕过用户选择的策略被默认打码。
TRANSACTION_TYPE_PREFIXES = ("AMOUNT_", "DATE_", "MONEY_")


def excluded_types(policy_id: str) -> frozenset[str]:
    if policy_id == PRESERVE_TRANSACTION:
        return frozenset({"AMOUNT", "DATE", "MONEY"})
    return frozenset()


def is_transaction_type(type_id: str) -> bool:
    type_id = str(type_id or "")
    return (type_id in {"AMOUNT", "DATE", "MONEY"} or
            type_id.startswith(TRANSACTION_TYPE_PREFIXES))


def is_excluded(type_id: str, policy_id: str) -> bool:
    """内置类型及按前缀声明的自定义交易要素类型，是否被当前策略默认保留。"""
    return policy_id == PRESERVE_TRANSACTION and is_transaction_type(type_id)


def label(policy_id: str) -> str:
    return _POLICY_LABELS.get(policy_id, _POLICY_LABELS[PRIVACY_FIRST])


def description(policy_id: str) -> str:
    name = label(policy_id)
    if policy_id == PRESERVE_TRANSACTION:
        return f"策略：{name}；金额、日期默认保留"
    return f"策略：{name}；金额、日期默认脱敏"
