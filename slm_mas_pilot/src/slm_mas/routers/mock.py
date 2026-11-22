from ..schemas import RoutingDecision


class MockRouter:
    is_mock = True

    def __init__(self, actions=None):
        self.actions = iter(actions or ["implementer", "repairer", "stop"])
        self.calls = 0

    def decide(self, request):
        self.calls += 1
        action = next(self.actions)
        return RoutingDecision(action, action, raw_response={"mock": True, "choice": action})
