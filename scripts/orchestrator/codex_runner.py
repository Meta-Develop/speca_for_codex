"""Codex CLI orchestrator runner.

Wraps authenticated ``codex exec --json`` as an audit-pipeline driver.

Authentication is owned by the Codex CLI login state (``codex login`` or
``codex login --with-api-key``). The orchestrator does not require
``OPENAI_API_KEY`` for this runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import aiofiles

from .config import PhaseConfig
from .paths import get_output_root
from .runner import CircuitBreaker, CircuitBreakerTripped, MaxTurnsExhausted
from .watchdog import BudgetExceeded, CostTracker


_WIN_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _resolve_codex_bin() -> str | None:
    """Locate the Codex CLI on PATH."""

    found = shutil.which("codex")
    if found is None and sys.platform == "win32":
        found = shutil.which("codex.cmd") or shutil.which("codex.exe")
    return found


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class CodexRunner:
    """Drives ``codex exec --json`` as an audit batch worker.

    Constructor signature matches ClaudeRunner / APIRunner so base.py can
    select it through the runtime registry.
    """

    RUNTIME_LABEL = "codex"

    def __init__(
        self,
        config: PhaseConfig,
        semaphore: asyncio.Semaphore,
        max_retries: int = 2,
        circuit_breaker: CircuitBreaker | None = None,
        cost_tracker: CostTracker | None = None,
        *,
        model: str | None = None,
        sandbox: str | None = None,
    ):
        self.config = config
        self.semaphore = semaphore
        self.max_retries = max_retries
        self.circuit_breaker = circuit_breaker or CircuitBreaker(config)
        self.cost_tracker = cost_tracker
        self.model = model if model is not None else os.environ.get("CODEX_MODEL")
        self.sandbox = (
            sandbox
            if sandbox is not None
            else os.environ.get("CODEX_SANDBOX", "workspace-write")
        )

        self.output_dir = get_output_root()
        self.log_dir = self.output_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    async def run_batch(
        self,
        batch: list[dict[str, Any]],
        worker_id: int,
        batch_index: int,
    ) -> list[dict[str, Any]] | None:
        async with self.semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    result = await self._execute_batch(batch, worker_id, batch_index)
                    if result is not None:
                        if len(result) == 0:
                            await self.circuit_breaker.record_empty_result()
                            print(
                                f"[W{worker_id}] Batch {batch_index}: empty result set",
                                file=sys.stderr,
                            )
                        else:
                            await self.circuit_breaker.record_success()
                        return result
                except (CircuitBreakerTripped, BudgetExceeded):
                    raise
                except MaxTurnsExhausted as e:
                    print(f"[W{worker_id}] {e}", file=sys.stderr)
                    return []
                except Exception as e:
                    print(
                        f"[W{worker_id}] Batch {batch_index} attempt {attempt + 1} failed: {e}",
                        file=sys.stderr,
                    )

                if attempt < self.max_retries:
                    await self.circuit_breaker.record_retry()
                    await asyncio.sleep(2 ** attempt)

            await self.circuit_breaker.record_failure()
            return None

    async def _execute_batch(
        self,
        batch: list[dict[str, Any]],
        worker_id: int,
        batch_index: int,
    ) -> list[dict[str, Any]] | None:
        bin_ = _resolve_codex_bin()
        if bin_ is None:
            raise RuntimeError(
                "codex CLI not found on PATH. Install Codex CLI and run `codex login`."
            )

        timestamp = int(time.time())
        phase_id = self.config.phase_id
        directory_mode = self.config.output_mode == "directory"
        workdir = self.config.workdir or str(Path.cwd())

        queue_path = self.output_dir / f"{phase_id}_ASYNC_QUEUE_W{worker_id}B{batch_index}_{timestamp}.json"
        context_path = self.output_dir / f"{phase_id}_CONTEXT_W{worker_id}B{batch_index}_{timestamp}.json"
        log_file = self.log_dir / f"{phase_id}_w{worker_id}b{batch_index}_{timestamp}.log.jsonl"
        last_message_path = self.log_dir / f"{phase_id}_w{worker_id}b{batch_index}_{timestamp}.last.txt"

        if directory_mode:
            batch_output_dir = self.output_dir / "graphs" / f"batch_w{worker_id}b{batch_index}_{timestamp}"
            batch_output_dir.mkdir(parents=True, exist_ok=True)
            result_parse_path = batch_output_dir / ".no_result_file"
            output_kwargs: dict[str, str] = {"output_dir": str(batch_output_dir)}
        else:
            result_parse_path = self.output_dir / f"{phase_id}_PARTIAL_W{worker_id}B{batch_index}_{timestamp}.json"
            output_kwargs = {"output_file": str(result_parse_path)}

        id_field = self.config.item_id_field
        item_ids = [str(item.get(id_field, f"item-{i}")) for i, item in enumerate(batch)]
        queue_payload = {
            "worker_id": worker_id,
            "phase": phase_id,
            "item_ids": item_ids,
            "total_items": len(batch),
            "context_file": str(context_path),
        }

        fields = self.config.context_fields
        context_payload: dict[str, Any] = {}
        for i, item in enumerate(batch):
            key = str(item.get(id_field, f"item-{i}"))
            if fields:
                context_payload[key] = {k: item[k] for k in fields if k in item}
            else:
                context_payload[key] = item

        self._save_json(queue_path, queue_payload)
        self._save_json(context_path, context_payload)

        prompt_content = self._build_prompt(
            worker_id=worker_id,
            queue_file=str(queue_path),
            context_file=str(context_path),
            batch_size=len(batch),
            iteration=batch_index,
            timestamp=timestamp,
            **output_kwargs,
        )
        prompt_bytes = prompt_content.encode("utf-8")
        cmd = self._build_cmd(bin_, last_message_path, workdir)
        state = self._fresh_state()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            creationflags=_WIN_NO_WINDOW,
        )

        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt_bytes)
                await proc.stdin.drain()
            finally:
                proc.stdin.close()

        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            if proc.stderr:
                while True:
                    chunk = await proc.stderr.read(65536)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())

        try:
            async with aiofiles.open(log_file, mode="wb") as f:
                if proc.stdout:
                    while True:
                        raw = await proc.stdout.readline()
                        if not raw:
                            break
                        await f.write(raw)
                        self._consume_event(
                            raw.decode("utf-8", errors="replace"),
                            state,
                        )

            await asyncio.wait_for(proc.wait(), timeout=self.config.timeout_seconds)
            await stderr_task
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stderr_task.cancel()
            print(
                f"[W{worker_id}] Batch {batch_index} timed out",
                file=sys.stderr,
            )
            return None
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await asyncio.wait_for(stderr_task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

        stderr_bytes = b"".join(stderr_chunks)
        if stderr_bytes:
            try:
                with open(log_file, "ab") as f:
                    line = json.dumps(
                        {
                            "type": "stderr",
                            "text": stderr_bytes.decode("utf-8", errors="replace"),
                        }
                    )
                    f.write((line + "\n").encode("utf-8"))
            except OSError:
                pass

        if not state["assistant_text"]:
            state["assistant_text"] = self._read_text(last_message_path)

        await self._record_usage(state, worker_id, batch_index)

        if state["error_message"]:
            print(
                f"[W{worker_id}] Batch {batch_index}: codex reported error: "
                f"{state['error_message'][:200]}",
                file=sys.stderr,
            )
            recovered = self._parse_results(result_parse_path)
            if recovered is not None:
                return recovered
            return None

        if proc.returncode != 0:
            print(
                f"[W{worker_id}] Batch {batch_index}: codex exited code {proc.returncode}",
                file=sys.stderr,
            )
            recovered = self._parse_results(result_parse_path)
            if recovered is not None:
                return recovered
            recovered = self._extract_results_from_text(state["assistant_text"])
            if recovered is not None:
                return recovered
            return [] if directory_mode else None

        results = self._parse_results(result_parse_path)
        if results is None:
            results = self._extract_results_from_text(state["assistant_text"])

        if results is None and directory_mode:
            return []

        if results is None:
            print(
                f"[W{worker_id}] Batch {batch_index}: no results parsed",
                file=sys.stderr,
            )
            return None

        return results

    # ------------------------------------------------------------------
    # Event consumption
    # ------------------------------------------------------------------

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "assistant_text": "",
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "error_message": None,
            "thread_id": None,
            "turn_count": 0,
            "tool_count": 0,
            "saw_complete": False,
        }

    def _consume_event(self, raw: str, state: dict[str, Any]) -> None:
        """Update accumulator state from one Codex JSONL line."""

        line = raw.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return

        event_type = str(payload.get("type") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

        if event_type == "thread.started":
            thread_id = (
                payload.get("thread_id")
                or payload.get("threadId")
                or data.get("thread_id")
                or data.get("threadId")
            )
            if isinstance(thread_id, str) and thread_id:
                state["thread_id"] = thread_id
            return

        if event_type == "turn.started":
            state["turn_count"] += 1
            return

        if event_type == "item.completed":
            item = payload.get("item")
            if not isinstance(item, dict):
                return
            item_type = str(item.get("type") or "")
            if item_type == "agent_message":
                text = self._text_from_item(item)
                if text:
                    state["assistant_text"] += text
            elif self._is_tool_item_type(item_type):
                state["tool_count"] += 1
            return

        if event_type in ("agent_message", "assistant.message", "message"):
            text = self._text_from_item(payload)
            if text:
                state["assistant_text"] += text
            return

        if self._is_tool_event_type(event_type):
            state["tool_count"] += 1
            return

        if event_type in ("error", "turn.failed", "thread.failed"):
            message = (
                data.get("message")
                or payload.get("message")
                or data.get("error")
                or payload.get("error")
                or "codex reported error"
            )
            state["error_message"] = str(message)
            return

        if event_type in ("turn.completed", "thread.completed", "complete", "done"):
            usage = payload.get("usage") or data.get("usage") or {}
            if isinstance(usage, dict):
                input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
                cached_input = usage.get(
                    "cached_input_tokens",
                    usage.get("cache_read_input_tokens"),
                )
                output_tokens = usage.get(
                    "output_tokens",
                    usage.get("completion_tokens"),
                )
                reasoning_tokens = usage.get("reasoning_output_tokens")

                state["input_tokens"] = _as_int(
                    input_tokens,
                    state["input_tokens"],
                )
                state["cache_read_tokens"] = _as_int(
                    cached_input,
                    state["cache_read_tokens"],
                )
                previous_reasoning = state["reasoning_output_tokens"]
                previous_visible_output = max(
                    0,
                    state["output_tokens"] - previous_reasoning,
                )
                state["reasoning_output_tokens"] = _as_int(
                    reasoning_tokens,
                    previous_reasoning,
                )
                visible_output = _as_int(output_tokens, previous_visible_output)
                state["output_tokens"] = visible_output + state["reasoning_output_tokens"]
            state["saw_complete"] = True
            return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_cmd(
        self,
        bin_: str,
        output_last_message: str | Path,
        workdir: str | Path,
    ) -> list[str]:
        cmd: list[str] = [
            bin_,
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            self.sandbox,
            "--cd",
            str(workdir),
            "--output-last-message",
            str(output_last_message),
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append("-")
        return cmd

    def _build_prompt(self, **kwargs: Any) -> str:
        with open(self.config.prompt_path, encoding="utf-8") as f:
            prompt_content = f.read()

        adapter_note = (
            "Codex runtime adapter note: If this phase prompt refers to "
            "Claude-style tools named Read, Grep, Glob, or Write, use the "
            "equivalent Codex CLI filesystem operations to inspect, search, "
            "and create files. You must materialize the requested OUTPUT_FILE "
            "or OUTPUT_DIR exactly; do not only describe the result in the "
            "final answer. If the prompt references /spec-discovery or "
            "/subgraph-extractor and that slash skill is unavailable, read "
            "the corresponding project skill file at "
            ".claude/skills/spec-discovery/SKILL.md or "
            ".claude/skills/subgraph-extractor/SKILL.md and execute those "
            "instructions inline, preserving the requested output "
            "file/directory and JSON envelope."
        )

        def _quote(v: Any) -> str:
            s = str(v)
            if " " in s or '"' in s:
                return f'"{s}"'
            return s

        args = " ".join(f"{k.upper()}={_quote(v)}" for k, v in kwargs.items())
        return f"{prompt_content}\n\n{adapter_note}\n\n{args}"

    def _save_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    async def _record_usage(
        self,
        state: dict[str, Any],
        worker_id: int,
        batch_index: int,
    ) -> None:
        if (
            self.config.max_cache_read_tokens > 0
            and state["cache_read_tokens"] > self.config.max_cache_read_tokens
        ):
            raise CircuitBreakerTripped(
                f"cache_read_tokens {state['cache_read_tokens']:,} "
                f"exceeds limit {self.config.max_cache_read_tokens:,} "
                f"(batch {batch_index}, worker {worker_id})",
                self.circuit_breaker._get_stats_unlocked(),
            )

        if not self.cost_tracker:
            return

        if (
            state["input_tokens"] <= 0
            and state["output_tokens"] <= 0
            and state["cache_read_tokens"] <= 0
        ):
            return

        num_turns = state["turn_count"] or state["tool_count"]
        batch_cost = await self.cost_tracker.record_usage(
            input_tokens=state["input_tokens"],
            output_tokens=state["output_tokens"],
            cache_read_tokens=state["cache_read_tokens"],
            num_turns=num_turns,
            worker_id=worker_id,
            batch_index=batch_index,
        )
        cost_stats = self.cost_tracker.get_stats()
        total_tokens = (
            state["input_tokens"]
            + state["cache_read_tokens"]
            + state["output_tokens"]
        )
        turns_str = f", turns={num_turns}" if num_turns else ""
        print(
            f"[W{worker_id}] Batch {batch_index}: "
            f"tokens={total_tokens:,} "
            f"(in={state['input_tokens']:,}, "
            f"cache_read={state['cache_read_tokens']:,}, "
            f"out={state['output_tokens']:,}{turns_str}); "
            f"+${batch_cost:.4f}, total=${cost_stats['total_cost_usd']:.2f}/"
            f"${cost_stats['max_budget_usd']:.2f}",
        )

    def _parse_results(self, result_path: Path) -> list[dict[str, Any]] | None:
        if not result_path.exists():
            return None
        try:
            with open(result_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        return self._normalize_result_data(data)

    def _normalize_result_data(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            result_key = self.config.result_key
            for key in [
                result_key,
                "items",
                "results",
                "audit_items",
                "reviewed_items",
                "graphs",
                "specs",
            ]:
                if key in data and isinstance(data[key], list):
                    return [item for item in data[key] if isinstance(item, dict)]
            return [data]
        return []

    def _extract_results_from_text(self, text: str) -> list[dict[str, Any]] | None:
        if not text:
            return None
        json_match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                return self._normalize_result_data(data)
            except json.JSONDecodeError:
                pass
        for start_char in ["{", "["]:
            idx = text.find(start_char)
            if idx >= 0:
                try:
                    data = json.loads(text[idx:])
                    return self._normalize_result_data(data)
                except json.JSONDecodeError:
                    pass
        return None

    def _text_from_item(self, item: dict[str, Any]) -> str:
        for key in ("text", "message", "content", "result"):
            value = item.get(key)
            text = self._text_from_value(value)
            if text:
                return text
        return ""

    def _text_from_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [self._text_from_value(v) for v in value]
            return "".join(p for p in parts if p)
        if isinstance(value, dict):
            for key in ("text", "content", "message"):
                text = self._text_from_value(value.get(key))
                if text:
                    return text
        return ""

    def _is_tool_item_type(self, item_type: str) -> bool:
        lower = item_type.lower()
        if lower in {"agent_message", "assistant_message", "message", "reasoning"}:
            return False
        return any(
            marker in lower
            for marker in ("tool", "call", "command", "exec", "shell", "patch")
        )

    def _is_tool_event_type(self, event_type: str) -> bool:
        lower = event_type.lower()
        if not any(
            marker in lower
            for marker in ("tool", "command", "exec", "shell", "patch")
        ):
            return False
        return any(marker in lower for marker in ("started", "completed", "call"))
