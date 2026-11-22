from typing import Protocol
from ..schemas import PublicTask, TaskManifest


class BenchmarkAdapter(Protocol):
    def load_public_tasks(self, manifest_path) -> tuple[TaskManifest, dict[str, PublicTask], dict[str, str]]: ...
