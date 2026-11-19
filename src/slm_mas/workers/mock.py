from ..schemas import WorkerResponse

BAD_CODE = "def add(a, b):\n    return a - b\n"
GOOD_CODE = "def add(a, b):\n    return a + b\n"


class MockWorker:
    is_mock = True

    def __init__(self, responses=None):
        self.responses = iter(responses or [f"```python\n{BAD_CODE}```", f"```python\n{GOOD_CODE}```"])
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        value = next(self.responses)
        return value if isinstance(value, WorkerResponse) else WorkerResponse(value, "stop", metadata={"mock": True})
