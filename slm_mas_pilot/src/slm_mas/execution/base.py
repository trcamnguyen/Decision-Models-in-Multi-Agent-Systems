from typing import Protocol
from ..schemas import Artifact, PublicFeedback, PublicTask


class PublicExecutor(Protocol):
    def check(self, task: PublicTask, code: Artifact, timeout_seconds: float, log,
              supervisor_timeout_seconds: float | None = None) -> PublicFeedback: ...
