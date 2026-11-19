import ast
import re
from .schemas import Artifact, PilotError, WorkflowState, digest

ORDER = ["analyst", "implementer", "repairer", "stop"]


def legal_actions(state: WorkflowState) -> list[str]:
    return ORDER.copy() if state.current_code_id else ORDER[:2]


def extract_code(response: str) -> str:
    # Fenced syntax-invalid code must reach the execution feedback path.
    if "```" in response:
        matches = list(re.finditer(r"```(?:python|py)\s*\n(.*?)```", response, re.S))
        if len(matches) != 1 or response.count("```") != 2:
            raise PilotError("output_parse_error", "expected exactly one Python fence")
        return matches[0].group(1).strip() + "\n"
    try:
        tree = ast.parse(response)
        if not tree.body or not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in tree.body):
            raise ValueError("no implementation")
    except (SyntaxError, ValueError):
        raise PilotError("output_parse_error", "unfenced response is not an identifiable Python implementation") from None
    return response.strip() + "\n"


class ArtifactStore:
    def __init__(self, log):
        self.log = log
        self.items: dict[str, Artifact] = {}

    def add(self, kind, content, role, step, parent_id, based_on_code_id):
        aid = f"{kind}-{step:03d}-{digest(content)[:12]}"
        artifact = Artifact(aid, kind, content, digest(content), role, step, parent_id, based_on_code_id)
        self.items[aid] = artifact
        self.log.write(f"artifacts/{aid}.json", artifact)
        self.log.write_text(f"artifacts/{aid}.{'py' if kind == 'code' else 'txt'}", content)
        return artifact

    def get(self, artifact_id):
        return self.items.get(artifact_id)
