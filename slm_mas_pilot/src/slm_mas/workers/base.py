from typing import Protocol
from ..schemas import WorkerRequest, WorkerResponse


class WorkerBackend(Protocol):
    def generate(self, request: WorkerRequest) -> WorkerResponse: ...
