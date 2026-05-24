from __future__ import annotations


class CouncilAgentError(RuntimeError):
    """Raised when a council node fails; session must fail all-or-none."""

    def __init__(self, agent: str, cause: Exception):
        self.agent = agent
        self.cause = cause
        super().__init__(f"{agent} failed: {type(cause).__name__}: {cause}")
