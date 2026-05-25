"""Tests for ``CodexRunner`` - the authenticated Codex CLI driver."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from orchestrator.codex_runner import CodexRunner, _resolve_codex_bin
from orchestrator.config import get_phase_config
from orchestrator.runner import CircuitBreaker


class TestRunnerInterface:
    def test_constructor_signature(self) -> None:
        config = get_phase_config("03")
        sem = asyncio.Semaphore(1)
        cb = CircuitBreaker(config)
        runner = CodexRunner(config, sem, circuit_breaker=cb)
        assert runner.config is config
        assert runner.semaphore is sem
        assert runner.circuit_breaker is cb

    def test_has_run_batch(self) -> None:
        config = get_phase_config("03")
        runner = CodexRunner(config, asyncio.Semaphore(1))
        assert hasattr(runner, "run_batch")
        assert asyncio.iscoroutinefunction(runner.run_batch)

    def test_default_model_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CODEX_MODEL", raising=False)
        config = get_phase_config("03")
        runner = CodexRunner(config, asyncio.Semaphore(1))
        assert runner.model is None

    def test_model_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_MODEL", "gpt-5.1-codex")
        config = get_phase_config("03")
        runner = CodexRunner(config, asyncio.Semaphore(1))
        assert runner.model == "gpt-5.1-codex"

    def test_model_kwarg_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_MODEL", "from-env")
        config = get_phase_config("03")
        runner = CodexRunner(config, asyncio.Semaphore(1), model="from-kwarg")
        assert runner.model == "from-kwarg"

    def test_sandbox_default_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = get_phase_config("03")
        monkeypatch.delenv("CODEX_SANDBOX", raising=False)
        assert CodexRunner(config, asyncio.Semaphore(1)).sandbox == "workspace-write"
        monkeypatch.setenv("CODEX_SANDBOX", "read-only")
        assert CodexRunner(config, asyncio.Semaphore(1)).sandbox == "read-only"


class TestBuildCmd:
    def _runner(self, model: str | None = None) -> CodexRunner:
        config = get_phase_config("03")
        return CodexRunner(config, asyncio.Semaphore(1), model=model)

    def test_base_args_use_stdin_prompt(self) -> None:
        runner = self._runner()
        cmd = runner._build_cmd("/usr/local/bin/codex", "/tmp/last.txt", "/repo")
        assert cmd[0] == "/usr/local/bin/codex"
        assert cmd[1] == "--ask-for-approval"
        assert cmd[2] == "never"
        assert cmd[3] == "exec"
        assert "--json" in cmd
        assert "--ephemeral" in cmd
        assert "--sandbox" in cmd
        assert "workspace-write" in cmd
        assert "--cd" in cmd
        assert "/repo" in cmd
        assert "--output-last-message" in cmd
        assert "/tmp/last.txt" in cmd
        assert cmd[-1] == "-"

    def test_no_model_flag_when_unset(self) -> None:
        cmd = self._runner()._build_cmd("codex", "/tmp/last.txt", "/repo")
        assert "--model" not in cmd

    def test_model_flag_when_set(self) -> None:
        cmd = self._runner(model="gpt-5.1-codex")._build_cmd(
            "codex",
            "/tmp/last.txt",
            "/repo",
        )
        assert "--model" in cmd
        assert "gpt-5.1-codex" in cmd


class TestPrompt:
    def test_prompt_includes_adapter_note_and_args(self) -> None:
        config = get_phase_config("03")
        runner = CodexRunner(config, asyncio.Semaphore(1))
        prompt = runner._build_prompt(
            worker_id=2,
            queue_file="outputs/q.json",
            context_file="outputs/c.json",
            batch_size=3,
            iteration=4,
            timestamp=123,
            output_file="outputs/result.json",
        )
        assert "Codex runtime adapter note" in prompt
        assert "Read, Grep, Glob, or Write" in prompt
        assert "/spec-discovery" in prompt
        assert "/subgraph-extractor" in prompt
        assert "slash skill is unavailable" in prompt
        assert ".claude/skills/spec-discovery/SKILL.md" in prompt
        assert "OUTPUT_FILE" in prompt
        assert "WORKER_ID=2" in prompt
        assert "OUTPUT_FILE=outputs/result.json" in prompt


class TestConsumeEvent:
    def _runner(self) -> CodexRunner:
        config = get_phase_config("03")
        return CodexRunner(config, asyncio.Semaphore(1))

    def _fresh_state(self) -> dict:
        return self._runner()._fresh_state()

    def test_ignores_blank_line(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event("", state)
        runner._consume_event("   \n", state)
        assert state == runner._fresh_state()

    def test_ignores_invalid_json(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event("not json\n", state)
        assert state["assistant_text"] == ""

    def test_thread_started_captures_id(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event(
            json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
            state,
        )
        assert state["thread_id"] == "thread-123"

    def test_turn_started_increments_count(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event(json.dumps({"type": "turn.started"}), state)
        assert state["turn_count"] == 1

    def test_agent_message_accumulates_text(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Hello "},
                }
            ),
            state,
        )
        runner._consume_event(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "content": [{"text": "world."}],
                    },
                }
            ),
            state,
        )
        assert state["assistant_text"] == "Hello world."

    def test_tool_items_increment_count(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "local_shell_call", "command": "rg foo"},
                }
            ),
            state,
        )
        runner._consume_event(json.dumps({"type": "tool.started"}), state)
        assert state["tool_count"] == 2

    def test_error_captures_message(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event(
            json.dumps({"type": "turn.failed", "message": "auth failed"}),
            state,
        )
        assert state["error_message"] == "auth failed"

    def test_turn_completed_extracts_usage(self) -> None:
        runner = self._runner()
        state = runner._fresh_state()
        runner._consume_event(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 123,
                        "cached_input_tokens": 40,
                        "output_tokens": 56,
                        "reasoning_output_tokens": 7,
                    },
                }
            ),
            state,
        )
        assert state["input_tokens"] == 123
        assert state["cache_read_tokens"] == 40
        assert state["reasoning_output_tokens"] == 7
        assert state["output_tokens"] == 63
        assert state["saw_complete"] is True


class TestExtractResultsFromText:
    def _runner(self) -> CodexRunner:
        config = get_phase_config("03")
        return CodexRunner(config, asyncio.Semaphore(1))

    def test_fenced_json_array(self) -> None:
        runner = self._runner()
        text = (
            "Here is the audit:\n\n"
            "```json\n"
            '[{"property_id": "PROP-1"}, {"property_id": "PROP-2"}]\n'
            "```\n"
        )
        result = runner._extract_results_from_text(text)
        assert result is not None
        assert len(result) == 2
        assert result[0]["property_id"] == "PROP-1"

    def test_raw_array(self) -> None:
        runner = self._runner()
        assert runner._extract_results_from_text('[{"check_id": "C-1"}]') == [
            {"check_id": "C-1"}
        ]

    def test_returns_none_on_empty(self) -> None:
        runner = self._runner()
        assert runner._extract_results_from_text("") is None
        assert runner._extract_results_from_text("no json here") is None


class TestNormalizeResultData:
    def _runner(self) -> CodexRunner:
        config = get_phase_config("03")
        return CodexRunner(config, asyncio.Semaphore(1))

    def test_list_passthrough(self) -> None:
        runner = self._runner()
        data = [{"a": 1}, {"b": 2}, "not a dict"]
        assert runner._normalize_result_data(data) == [{"a": 1}, {"b": 2}]

    def test_dict_with_result_key(self) -> None:
        runner = self._runner()
        data = {"audit_items": [{"id": "x"}]}
        assert runner._normalize_result_data(data) == [{"id": "x"}]

    def test_dict_wrap_when_no_known_key(self) -> None:
        runner = self._runner()
        assert runner._normalize_result_data({"foo": "bar"}) == [{"foo": "bar"}]


class TestResolveBin:
    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "orchestrator.codex_runner.shutil.which",
            lambda _name: None,
        )
        assert _resolve_codex_bin() is None

    def test_returns_path_when_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "orchestrator.codex_runner.shutil.which",
            lambda _name: "/usr/local/bin/codex",
        )
        assert _resolve_codex_bin() == "/usr/local/bin/codex"

    def test_windows_cmd_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_which(name: str) -> str | None:
            if name == "codex.cmd":
                return "C:/npm/codex.cmd"
            return None

        monkeypatch.setattr("orchestrator.codex_runner.sys.platform", "win32")
        monkeypatch.setattr("orchestrator.codex_runner.shutil.which", fake_which)
        assert _resolve_codex_bin() == "C:/npm/codex.cmd"
