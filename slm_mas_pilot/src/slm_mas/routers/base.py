import math
from typing import Protocol
from ..schemas import PilotError, RoutingRequest, RoutingDecision


class Router(Protocol):
    def decide(self, request: RoutingRequest) -> RoutingDecision: ...


def validate_decision(decision, legal, tolerance=0.0003):
    if decision.validation_status != "valid":
        raise PilotError("router_output_error", "adapter rejected response")
    if decision.model_choice_raw not in legal:
        raise PilotError("router_output_error", "SDK choice outside legal actions")
    probs = decision.probabilities
    if probs is not None:
        if not isinstance(probs, dict) or set(probs) != set(legal) or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0 for v in probs.values()):
            raise PilotError("router_output_error", "invalid probability keys or values")
        if abs(sum(probs.values()) - 1) > tolerance:
            raise PilotError("router_output_error", "probability sum outside tolerance")
        decision.action = max(legal, key=lambda action: probs[action])
        ordered = sorted(probs.values(), reverse=True)
        decision.probability_summary = {"top1": ordered[0], "top2": ordered[1] if len(ordered) > 1 else None,
                                        "margin": ordered[0] - ordered[1] if len(ordered) > 1 else None}
    elif decision.action not in legal or decision.action != decision.model_choice_raw:
        raise PilotError("router_output_error", "invalid choice-only response")
    return decision
