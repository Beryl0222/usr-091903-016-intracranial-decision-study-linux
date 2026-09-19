"""领域错误类型。"""


class DomainError(Exception):
    """领域规则被违反。"""


class ValidationError(DomainError):
    """输入不合法。"""


class NotFound(DomainError):
    """对象不存在。"""


class AccessDenied(DomainError):
    """角色无权访问。"""


class ClinicalConflict(DomainError):
    """与临床安排冲突（临床优先）。"""


class ConsentViolation(DomainError):
    """超出同意范围。"""


class ImmutableViolation(DomainError):
    """试图改动不可变对象。"""


class StateError(DomainError):
    """状态机不允许的操作。"""


class GovernanceError(DomainError):
    """分析治理规则被违反。"""
