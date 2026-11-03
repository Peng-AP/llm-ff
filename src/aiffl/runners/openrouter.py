"""Agent loop for any tool-calling model on OpenRouter.

The loop is deliberately harness-neutral: the caller supplies the system prompt, the tool
schemas, and a function that executes tool calls. Later the tools come from the league MCP
server; for now the smoke test passes fake ones.
"""

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from aiffl.config import LOGS_DIR

ToolHandler = Callable[[str, dict], object]

# Applied to every OpenRouter team alike. Only route to hosts that support tool calling and serve
# the model at 8-bit precision or better (4-bit copies are measurably weaker; "unknown" covers the
# first-party hosts, which don't publish it). Among those, the cheapest first, with hosts slower
# than 20 tokens/s moved to the back. Sorting by speed instead picked premium "fast" tiers that
# cost up to ~20x more per token for the same model.
PROVIDER_ROUTING = {"require_parameters": True,
                    "quantizations": ["int8", "fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown"],
                    "sort": "price", "preferred_min_throughput": {"p50": 20}}
# Called when the model tries to finish. Returns a reminder to send back (the run continues),
# or None to let it finish. Each run gets at most one reminder.
FinishCheck = Callable[[], str | None]


@dataclass
class RunResult:
    model: str
    final_text: str | None
    turns: int
    tool_calls: int
    reminders: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stopped_reason: str = "completed"
    messages: list[dict] = field(default_factory=list)


def make_client() -> OpenAI:
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key.")
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
        default_headers={"X-Title": "AI Fantasy Football League"},
        # Upstream providers rate-limit (429) now and then; back off and retry rather than lose a run.
        max_retries=6,
    )


def run_agent(
    model: str,
    system: str,
    user: str,
    tools: list[dict],
    handle_tool: ToolHandler,
    max_turns: int = 20,
    client: OpenAI | None = None,
    check_finish: FinishCheck | None = None,
) -> RunResult:
    client = client or make_client()
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    result = RunResult(model=model, final_text=None, turns=0, tool_calls=0, messages=messages)
    reminded = False

    for _ in range(max_turns):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            extra_body={"provider": PROVIDER_ROUTING, "usage": {"include": True}},
        )
        result.turns += 1
        _add_usage(result, resp.usage)

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            reminder = None if reminded or check_finish is None else check_finish()
            if reminder:
                reminded = True
                result.reminders += 1
                messages.append({"role": "user", "content": reminder})
                continue
            result.final_text = msg.content
            return result

        for call in msg.tool_calls:
            result.tool_calls += 1
            try:
                args = json.loads(call.function.arguments or "{}")
                output = handle_tool(call.function.name, args)
            except json.JSONDecodeError as e:
                output = {"error": f"Arguments were not valid JSON: {e}. Retry with valid JSON."}
            except Exception as e:  # tool errors go back to the model so it can correct itself
                output = {"error": str(e)}
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(output, default=str),
            })

    result.stopped_reason = "max_turns"
    return result


def _add_usage(result: RunResult, usage) -> None:
    if usage is None:
        return
    result.prompt_tokens += usage.prompt_tokens or 0
    result.completion_tokens += usage.completion_tokens or 0
    # OpenRouter adds `cost` (USD) to usage when usage accounting is requested.
    result.cost_usd += float(getattr(usage, "cost", 0) or 0)


def save_transcript(result: RunResult, label: str) -> Path:
    LOGS_DIR.mkdir(exist_ok=True)
    safe_model = result.model.replace("/", "_").replace(":", "_")
    path = LOGS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}_{safe_model}.json"
    path.write_text(json.dumps(result.__dict__, indent=2, default=str), encoding="utf-8")
    return path
