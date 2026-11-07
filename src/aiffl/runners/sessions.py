"""Multi-turn agent sessions: one conversation that survives many send() calls.

All three harnesses get the same system prompt and the same league MCP tools, with each
harness's built-in tools (shell, file editing, native web search) turned off.

    session = make_session(team, system_prompt, phase="draft")
    result = session.send("You're on the clock...", timeout_s=600)
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aiffl.config import LOGS_DIR, Team, runtime_env
from aiffl.runners.openrouter import REASONING_EFFORT, chat, make_client

PYTHON = sys.executable
DoneCheck = Callable[[], str | None]  # returns a reminder if the turn's required action is missing


@dataclass
class SendResult:
    ok: bool
    text: str = ""
    error: str | None = None
    seconds: float = 0.0
    cost_usd: float = 0.0
    turns: int = 0


class Session:
    harness = ""

    def __init__(self, team: Team, system_prompt: str, phase: str, session_id: str | None = None,
                 log_name: str | None = None):
        self.team, self.system_prompt, self.phase = team, system_prompt, phase
        self.session_id = session_id
        self.budgets: dict[str, int] = {}  # per-turn web budgets, e.g. {"web_search": 10, "fetch_page": 5}
        self.workspace = team.workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.log_path = LOGS_DIR / (log_name or f"{phase}_{team.id}.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, text: str, timeout_s: float, check_done: DoneCheck | None = None) -> SendResult:
        raise NotImplementedError

    def _log(self, kind: str, payload) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "data": payload},
                               default=str) + "\n")

    def _mcp_env(self) -> dict[str, str]:
        env = runtime_env()
        if "web_search" in self.budgets:
            env["AIFFL_SEARCH_BUDGET"] = str(self.budgets["web_search"])
        if "fetch_page" in self.budgets:
            env["AIFFL_FETCH_BUDGET"] = str(self.budgets["fetch_page"])
        return env


# OpenRouter: our own loop; tools are called in-process on the same MCP server object ------

class OpenRouterSession(Session):
    harness = "openrouter"
    # High on purpose: some models make one tool call per turn, and the time limit is the real bound.
    # (Claude Code and Codex have no turn cap at all.)
    MAX_TURNS_PER_SEND = 200
    KEEP_FULL_TOOL_RESULTS_SENDS = 2  # older tool results get trimmed to keep context small

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = make_client()
        # The conversation is saved after every send so a crashed draft can resume it.
        self.state_file = self.workspace / f".session-{self.phase}.json"
        if self.session_id and self.state_file.exists():
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.messages, self.send_starts = saved["messages"], saved["send_starts"]
        else:
            self.messages = [{"role": "system", "content": self.system_prompt}]
            self.send_starts = []
        self.session_id = str(self.state_file)

    def send(self, text, timeout_s, check_done=None):
        result = asyncio.run(self._send(text, timeout_s, check_done))
        self.state_file.write_text(json.dumps({"messages": self.messages, "send_starts": self.send_starts},
                                              default=str), encoding="utf-8")
        return result

    def _trim_old_tool_results(self):
        if len(self.send_starts) <= self.KEEP_FULL_TOOL_RESULTS_SENDS:
            return
        cutoff = self.send_starts[-self.KEEP_FULL_TOOL_RESULTS_SENDS]
        for m in self.messages[:cutoff]:
            if m.get("role") == "tool" and len(m.get("content", "")) > 400:
                m["content"] = m["content"][:400] + " …[trimmed from an earlier turn]"

    async def _send(self, text, timeout_s, check_done):
        from aiffl.tools import build_server
        # Fresh server = fresh per-turn web budget. Budgets are passed explicitly, not via
        # os.environ, because prep turns run in parallel threads.
        server = build_server(self.team.id, budgets=self.budgets)
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                   "parameters": t.input_schema}}
                 for t in await server.list_tools()]
        self._trim_old_tool_results()
        self.send_starts.append(len(self.messages))
        self.messages.append({"role": "user", "content": text})
        self._log("user", text)
        start, result, reminded, warned = time.monotonic(), SendResult(ok=False), False, False

        while result.turns < self.MAX_TURNS_PER_SEND:
            elapsed = time.monotonic() - start
            if elapsed > timeout_s:
                result.error = "timeout"
                break
            if not warned and timeout_s >= 300 and elapsed > 0.8 * timeout_s:
                warned = True
                note = (f"About {int((timeout_s - elapsed) / 60) + 1} minutes are left in this turn. Wrap up: "
                        "make your final moves, then save your notes.")
                self.messages.append({"role": "user", "content": note})
                self._log("user", note)
            try:
                resp = await asyncio.to_thread(chat, self.client, self.team.model, self.messages, tools)
            except Exception as e:
                result.error = f"{type(e).__name__}: {e}"
                break
            result.turns += 1
            if resp.usage is not None:
                result.cost_usd += float(getattr(resp.usage, "cost", 0) or 0)
                u = resp.usage.model_dump()
                self._log("usage", {"provider": (resp.model_extra or {}).get("provider"),
                                    "prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens"),
                                    "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                                    "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                                    "cost": u.get("cost")})
            msg = resp.choices[0].message
            self.messages.append(msg.model_dump(exclude_none=True))
            self._log("assistant", self.messages[-1])
            if not msg.tool_calls:
                reminder = None if (reminded or check_done is None) else check_done()
                if reminder:
                    reminded = True
                    self.messages.append({"role": "user", "content": reminder})
                    self._log("user", reminder)
                    continue
                result.ok, result.text = True, msg.content or ""
                break
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    out = (await server.call_tool(call.function.name, args)).content[0].text
                except json.JSONDecodeError as e:
                    out = json.dumps({"error": f"Arguments were not valid JSON: {e}. Retry with valid JSON."})
                except Exception as e:
                    out = json.dumps({"error": f"{type(e).__name__}: {e}"})
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": out})
                self._log("tool", {"name": call.function.name, "args": call.function.arguments, "result": out[:2000]})
        else:
            result.error = "max_turns"
        result.seconds = time.monotonic() - start
        return result


# CLI harnesses: shared subprocess handling ----------------------------------------------

def _run(cmd: list[str], stdin_text: str, cwd: Path, timeout_s: float) -> tuple[int | None, str, str]:
    """Run a CLI agent; on timeout kill its whole process tree (it spawns our MCP server)."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=flags)
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout_s)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        out, err = proc.communicate()
        return None, out, err


class ClaudeCodeSession(Session):
    harness = "claude_code"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
        self.prompt_file = self.workspace / f".system-prompt-{self.phase}.md"
        self.prompt_file.write_text(self.system_prompt, encoding="utf-8")
        self.mcp_file = self.workspace / ".mcp-league.json"

    def _write_mcp_config(self):
        # Rewritten every send so the current per-turn budgets reach the MCP server.
        self.mcp_file.write_text(json.dumps({"mcpServers": {"league": {
            "command": PYTHON, "args": ["-m", "aiffl.mcp_server", "--team", self.team.id],
            "env": self._mcp_env()}}}, indent=2), encoding="utf-8")

    def send(self, text, timeout_s, check_done=None):
        result = self._send_once(text, timeout_s)
        reminder = check_done() if (result.ok and check_done) else None
        if reminder and result.seconds < timeout_s:
            again = self._send_once(reminder, timeout_s - result.seconds)
            again.seconds += result.seconds
            again.cost_usd += result.cost_usd
            again.turns += result.turns
            return again
        return result

    def _send_once(self, text, timeout_s):
        self._write_mcp_config()
        first = self.session_id is None
        if first:
            self.session_id = str(uuid.uuid4())
        cmd = [self.exe, "-p", "--model", self.team.model, "--effort", REASONING_EFFORT, "--output-format", "json",
               "--system-prompt-file", str(self.prompt_file),
               "--tools", "",                              # no built-in tools: no shell, files, or native web
               "--mcp-config", str(self.mcp_file), "--strict-mcp-config",
               "--allowedTools", "mcp__league",
               "--setting-sources", "project"]             # ignore the user's own settings, hooks, plugins
        cmd += ["--session-id", self.session_id] if first else ["--resume", self.session_id]
        self._log("user", text)
        start = time.monotonic()
        code, out, err = _run(cmd, text, self.workspace, timeout_s)
        result = SendResult(ok=False, seconds=time.monotonic() - start)
        if code is None:
            result.error = "timeout"
        try:
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except json.JSONDecodeError:
            data = {}
        self._log("result", data or {"stdout": out[-2000:], "stderr": err[-2000:]})
        if data:
            result.text = data.get("result") or ""
            result.turns = data.get("num_turns") or 0
            result.cost_usd = float(data.get("total_cost_usd") or 0)  # API-equivalent; subscription isn't billed
            if code is not None:
                result.ok = not data.get("is_error")
                result.error = None if result.ok else (data.get("subtype") or "error")
        elif code is not None:
            result.error = f"exit {code}: {(err or out)[-500:]}"
        return result


class CodexSession(Session):
    harness = "codex"
    # Not disabled: code_mode_host. This Codex version routes MCP tool calls through it.
    DISABLED_FEATURES = ["shell_tool", "unified_exec", "browser_use", "browser_use_external",
                         "computer_use", "image_generation", "multi_agent", "apps", "plugins", "view_image",
                         "in_app_browser", "tool_suggest", "sleep_tool", "goals"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.codex_js = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        self.node = shutil.which("node") or "node"

    def _config_args(self) -> list[str]:
        env_table = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in self._mcp_env().items()) + "}"
        # Codex keeps subagent tools that cannot be switched off; the other harnesses have none.
        instructions = self.system_prompt + "\n\nWork alone: do not spawn, message, or wait on other agents."
        settings = {
            "developer_instructions": json.dumps(instructions),
            "approval_policy": '"never"',
            "model_reasoning_effort": json.dumps(REASONING_EFFORT),  # else the user's own config.toml applies
            "web_search": '"disabled"',
            "mcp_servers.league.command": json.dumps(PYTHON),
            "mcp_servers.league.args": json.dumps(["-m", "aiffl.mcp_server", "--team", self.team.id]),
            "mcp_servers.league.env": env_table,
            "mcp_servers.league.default_tools_approval_mode": '"approve"',  # league tools run without prompts
            "mcp_servers.league.startup_timeout_sec": "60",
            "mcp_servers.league.tool_timeout_sec": "180",
        }
        args = []
        for k, v in settings.items():
            args += ["-c", f"{k}={v}"]
        for f in self.DISABLED_FEATURES:
            args += ["--disable", f]
        return args

    def send(self, text, timeout_s, check_done=None):
        result = self._send_once(text, timeout_s)
        reminder = check_done() if (result.ok and check_done) else None
        if reminder and result.seconds < timeout_s:
            again = self._send_once(reminder, timeout_s - result.seconds)
            again.seconds += result.seconds
            again.turns += result.turns
            return again
        return result

    def _send_once(self, text, timeout_s):
        base = [self.node, str(self.codex_js), "exec"]
        common = ["--json", "-m", self.team.model, "--skip-git-repo-check", *self._config_args()]
        if self.session_id is None:
            cmd = base + common + ["-C", str(self.workspace), "--sandbox", "read-only", "-"]
        else:
            cmd = base + ["resume", self.session_id] + common + ["-"]
        self._log("user", text)
        start = time.monotonic()
        code, out, err = _run(cmd, text, self.workspace, timeout_s)
        result = SendResult(ok=False, seconds=time.monotonic() - start)
        final, failure = "", None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._log("event", ev)
            kind = ev.get("type", "")
            if kind == "thread.started" and ev.get("thread_id"):
                self.session_id = ev["thread_id"]
            elif kind == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                final = ev["item"].get("text") or final
            elif kind == "turn.completed":
                result.turns += 1
            elif kind in ("turn.failed", "error"):
                failure = json.dumps(ev)[:500]
        result.text = final
        if code is None:
            result.error = "timeout"
        elif code != 0 or failure:
            result.error = failure or f"exit {code}: {err[-500:]}"
        else:
            result.ok = True
        if err.strip():
            self._log("stderr", err[-3000:])
        return result


HARNESSES = {"openrouter": OpenRouterSession, "claude_code": ClaudeCodeSession, "codex": CodexSession}


def make_session(team: Team, system_prompt: str, phase: str, session_id: str | None = None,
                 log_name: str | None = None) -> Session:
    return HARNESSES[team.harness](team, system_prompt, phase, session_id, log_name)
