"""
Run Budget & Loop Guard endpoint.

POST / (or whatever path you mount this at) with:
{
  "budget_tokens": <int>,
  "steps": [ { "step_number": int, "tool": str, "args": {...}, "tokens_used": int }, ... ]
}

Returns:
{ "decision": "continue" | "halt", "reason": "..." }
"""

import json
import re
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# Allow the grader to call this from anywhere.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Step(BaseModel):
    step_number: int
    tool: str
    args: dict[str, Any] = {}
    tokens_used: int


class RunState(BaseModel):
    budget_tokens: int
    steps: list[Step] = []


# ---------------------------------------------------------------------------
# Canonicalization: turn "args" into a string that is identical for two
# calls that are functionally the same, even if they look different on
# the surface (different key order, extra whitespace, a changing tracing id).
# ---------------------------------------------------------------------------
def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize(val)
            for key, val in value.items()
            if key != "client_ts"  # tracing id — never part of the "real" call
        }
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        # collapse all whitespace runs to a single space, strip the ends
        return re.sub(r"\s+", " ", value).strip()
    return value


def canonical_key(tool: str, args: dict) -> str:
    """A string that's equal for two (tool, args) pairs iff they're the
    'same call' under the assignment's rules."""
    normalized = _normalize(args)
    # sort_keys=True makes key order irrelevant
    return tool + "::" + json.dumps(normalized, sort_keys=True)


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------
def decide(state: RunState) -> dict:
    steps = state.steps

    # --- Budget check -------------------------------------------------
    total_tokens = sum(s.tokens_used for s in steps)
    if total_tokens >= state.budget_tokens:
        return {
            "decision": "halt",
            "reason": f"Cumulative tokens_used ({total_tokens}) has reached "
                      f"the budget ({state.budget_tokens}).",
        }

    # --- Loop check 1: same call 3+ times in a row ---------------------
    if len(steps) >= 3:
        last_three_keys = [canonical_key(s.tool, s.args) for s in steps[-3:]]
        if last_three_keys[0] == last_three_keys[1] == last_three_keys[2]:
            return {
                "decision": "halt",
                "reason": (
                    f"Tool '{steps[-1].tool}' was called 3+ times in a row "
                    f"with functionally identical arguments — looks like a stuck loop."
                ),
            }

    # --- Loop check 2: A,B,A,B,A,B cycle over the last 6 steps ---------
    if len(steps) >= 6:
        last_six = steps[-6:]
        keys = [canonical_key(s.tool, s.args) for s in last_six]
        a, b = keys[0], keys[1]
        is_cycle = (
            a != b  # must be two genuinely different calls, not one repeated call
            and keys[0] == keys[2] == keys[4]
            and keys[1] == keys[3] == keys[5]
        )
        if is_cycle:
            return {
                "decision": "halt",
                "reason": (
                    f"Detected a 2-step repeating cycle between '{last_six[0].tool}' "
                    f"and '{last_six[1].tool}' over the last 6 steps."
                ),
            }

    # --- Nothing tripped: safe to continue -----------------------------
    return {
        "decision": "continue",
        "reason": f"Under budget ({total_tokens}/{state.budget_tokens} tokens used) "
                  f"and no repeated-call or cycle pattern detected.",
    }


@app.post("/")
def run_guard(state: RunState):
    return decide(state)


# Some graders probe a specific path instead of "/" — expose the same
# logic there too, just in case.
@app.post("/guard")
def run_guard_alias(state: RunState):
    return decide(state)


@app.get("/")
def health():
    return {"status": "ok"}
