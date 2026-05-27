from __future__ import annotations


class CouncilAgentError(RuntimeError):
    """Raised when a council node fails; session must fail all-or-none."""

    def __init__(self, agent: str, cause: Exception):
        self.agent = agent
        self.cause = cause
        super().__init__(f"{agent} failed: {type(cause).__name__}: {cause}")


class CouncilStop(RuntimeError):
    """Expected terminal condition where no proposal should be produced."""

    def __init__(self, *, reason: str, user_message: str, detail: str):
        self.reason = reason
        self.user_message = user_message
        self.detail = detail
        super().__init__(detail)


class CouncilNoEvidence(CouncilStop):
    """Retrieval could not provide evidence for a council draft."""

    def __init__(self, *, user_message: str, detail: str):
        super().__init__(reason="no_evidence", user_message=user_message, detail=detail)


class CouncilIncompleteSkill(CouncilStop):
    """A generated skill could not be repaired into the required structure."""

    def __init__(self, *, user_message: str, detail: str):
        super().__init__(
            reason="incomplete_skill",
            user_message=user_message,
            detail=detail,
        )
