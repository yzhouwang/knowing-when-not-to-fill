"""
test_llm_structured.py — Tier-1 provider-native structured-output generation.

Covers contract_drafting.llm.call_llm_structured (Anthropic forced tool call /
OpenAI response_format=json_schema strict mode) plus the OpenAI strict-mode
schema massaging helper and its cache (T6/T9).

All provider calls are mocked — NO real network access. The monkeypatch style
mirrors tests/test_contract_drafting.py::TestClauseRefinement._mock_llm_response:
we replace the client class on the llm module so .messages.create /
.chat.completions.create return canned responses and record their kwargs.
"""
from __future__ import annotations

import copy
import json

import pytest

from contract_drafting import llm as llm_mod
from contract_drafting.llm import (
    call_llm_structured,
    _massage_for_openai_strict,
    _massaged_schema_cache,
)


# ---------------------------------------------------------------------------
# Representative schema fixture (mirrors the task spec): an object with a
# governingLaw $ref into an enum-bearing definition, plus a plain integer.
# ---------------------------------------------------------------------------
@pytest.fixture
def enum_schema() -> dict:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "governingLaw": {"$ref": "#/definitions/Jurisdiction"},
            "termMonths": {"type": "integer"},
        },
        "definitions": {
            "Jurisdiction": {"enum": ["Washington", "New_York"]},
        },
    }


@pytest.fixture(autouse=True)
def _clear_massage_cache():
    """Each test starts with an empty massage cache (T9 isolation)."""
    _massaged_schema_cache.clear()
    yield
    _massaged_schema_cache.clear()


# ---------------------------------------------------------------------------
# Fake Anthropic SDK objects
# ---------------------------------------------------------------------------
class _FakeToolUseBlock:
    """Stand-in for anthropic.types.ToolUseBlock."""

    def __init__(self, name: str, input_: dict):
        self.type = "tool_use"
        self.name = name
        self.input = input_


class _FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeAnthropicMessage:
    def __init__(self, content: list):
        self.content = content


class _FakeAnthropicMessages:
    def __init__(self, parent):
        self._parent = parent

    def create(self, **kwargs):
        self._parent.create_kwargs = kwargs
        # Mimic a real response: a leading text block then the forced tool_use.
        return _FakeAnthropicMessage([
            _FakeTextBlock("here are the fields"),
            _FakeToolUseBlock(
                kwargs["tool_choice"]["name"],
                self._parent.tool_input,
            ),
        ])


class _FakeAnthropicClient:
    """Records constructor + create kwargs; returns a canned tool_use block."""

    # Set by the test before the call.
    tool_input: dict = {}

    def __init__(self, api_key=None, **_):
        self.api_key = api_key
        self.create_kwargs = None
        self.messages = _FakeAnthropicMessages(self)


# ---------------------------------------------------------------------------
# Fake OpenAI SDK objects
# ---------------------------------------------------------------------------
class _FakeOpenAIMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeOpenAIChoice:
    def __init__(self, content: str):
        self.message = _FakeOpenAIMessage(content)


class _FakeOpenAICompletion:
    def __init__(self, content: str):
        self.choices = [_FakeOpenAIChoice(content)]


class _FakeOpenAICompletions:
    def __init__(self, parent):
        self._parent = parent

    def create(self, **kwargs):
        self._parent.create_kwargs = kwargs
        return _FakeOpenAICompletion(self._parent.response_content)


class _FakeOpenAIChat:
    def __init__(self, parent):
        self.completions = _FakeOpenAICompletions(parent)


class _FakeOpenAIClient:
    """Records the response_format kwarg; returns a canned JSON string."""

    response_content: str = "{}"

    def __init__(self, api_key=None, **_):
        self.api_key = api_key
        self.create_kwargs = None
        self.chat = _FakeOpenAIChat(self)


# ---------------------------------------------------------------------------
# (a) Anthropic path
# ---------------------------------------------------------------------------
class TestAnthropicStructured:
    def test_returns_tool_input_and_wires_enum(self, monkeypatch, enum_schema):
        expected = {"governingLaw": "Washington", "termMonths": 12}

        captured = {}

        class Client(_FakeAnthropicClient):
            tool_input = expected

        # Capture the client instance so we can inspect the create kwargs.
        orig_init = Client.__init__

        def init(self, api_key=None, **kw):
            orig_init(self, api_key=api_key, **kw)
            captured["client"] = self

        monkeypatch.setattr(Client, "__init__", init)
        monkeypatch.setattr(llm_mod, "Anthropic", Client)

        result = call_llm_structured(
            "Emit the fields", "context here",
            json_schema=enum_schema,
            provider="anthropic",
            api_key="sk-test",
        )

        # The tool_use .input dict is returned verbatim.
        assert result == expected

        # The constraint is actually wired: the tool's input_schema is the
        # caller's schema and still carries the enum.
        kwargs = captured["client"].create_kwargs
        assert kwargs is not None
        tools = kwargs["tools"]
        assert tools[0]["name"] == "emit_fields"
        passed_schema = tools[0]["input_schema"]
        assert passed_schema is enum_schema  # passed through verbatim
        enum_vals = passed_schema["definitions"]["Jurisdiction"]["enum"]
        assert enum_vals == ["Washington", "New_York"]

        # tool_choice forces the emit_fields tool.
        assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_fields"}

    def test_raises_without_tool_use_block(self, monkeypatch, enum_schema):
        class _NoToolMessages:
            def create(self, **kwargs):
                return _FakeAnthropicMessage([_FakeTextBlock("nope")])

        class Client(_FakeAnthropicClient):
            def __init__(self, api_key=None, **_):
                self.api_key = api_key
                self.messages = _NoToolMessages()

        monkeypatch.setattr(llm_mod, "Anthropic", Client)
        with pytest.raises(ValueError, match="no tool_use block"):
            call_llm_structured(
                "q", "c", json_schema=enum_schema,
                provider="anthropic", api_key="sk-test",
            )


# ---------------------------------------------------------------------------
# (b) OpenAI path
# ---------------------------------------------------------------------------
class TestOpenAIStructured:
    def test_parses_json_and_wires_strict_schema(self, monkeypatch, enum_schema):
        expected = {"governingLaw": "New_York", "termMonths": 24}
        captured = {}

        class Client(_FakeOpenAIClient):
            response_content = json.dumps(expected)

        orig_init = Client.__init__

        def init(self, api_key=None, **kw):
            orig_init(self, api_key=api_key, **kw)
            captured["client"] = self

        monkeypatch.setattr(Client, "__init__", init)
        monkeypatch.setattr(llm_mod, "OpenAI", Client)

        result = call_llm_structured(
            "Emit the fields", "context",
            json_schema=enum_schema,
            provider="openai",
            api_key="sk-test",
        )
        assert result == expected

        kwargs = captured["client"].create_kwargs
        rf = kwargs["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "emit_fields"
        assert rf["json_schema"]["strict"] is True

        massaged = rf["json_schema"]["schema"]
        # Strict-mode requirements on the top-level object.
        assert massaged["additionalProperties"] is False
        assert set(massaged["required"]) == {"governingLaw", "termMonths"}
        # definitions -> $defs, and the enum survives.
        assert "definitions" not in massaged
        assert "$defs" in massaged
        assert massaged["$defs"]["Jurisdiction"]["enum"] == ["Washington", "New_York"]
        # $ref rewritten to point at $defs.
        assert massaged["properties"]["governingLaw"]["$ref"] == "#/$defs/Jurisdiction"

    def test_falls_back_on_bad_request(self, monkeypatch, enum_schema):
        """First create() raises BadRequestError (max_completion_tokens), the
        retry with max_tokens succeeds — mirrors _call_openai's pattern."""
        from openai import BadRequestError

        expected = {"governingLaw": "Washington", "termMonths": 6}
        state = {"calls": []}

        class _Completions:
            def create(self, **kwargs):
                state["calls"].append(kwargs)
                if "max_completion_tokens" in kwargs:
                    raise BadRequestError.__new__(BadRequestError)
                return _FakeOpenAICompletion(json.dumps(expected))

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        class Client(_FakeOpenAIClient):
            def __init__(self, api_key=None, **_):
                self.api_key = api_key
                self.chat = _Chat()

        monkeypatch.setattr(llm_mod, "OpenAI", Client)

        result = call_llm_structured(
            "q", "c", json_schema=enum_schema,
            provider="openai", api_key="sk-test",
        )
        assert result == expected
        # First attempt used max_completion_tokens, retry used max_tokens.
        assert "max_completion_tokens" in state["calls"][0]
        assert "max_tokens" in state["calls"][1]


# ---------------------------------------------------------------------------
# (c) _massage_for_openai_strict unit tests
# ---------------------------------------------------------------------------
class TestMassageForOpenAIStrict:
    def test_additional_properties_false_everywhere(self):
        schema = {
            "type": "object",
            "properties": {
                "outer": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {"inner": {"type": "integer"}},
                },
            },
        }
        m = _massage_for_openai_strict(schema)
        assert m["additionalProperties"] is False
        assert m["properties"]["nested"]["additionalProperties"] is False

    def test_required_is_all_properties(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],  # incomplete on purpose
        }
        m = _massage_for_openai_strict(schema)
        assert set(m["required"]) == {"a", "b"}

    def test_definitions_renamed_and_refs_rewritten(self, enum_schema):
        m = _massage_for_openai_strict(enum_schema)
        assert "definitions" not in m
        assert "$defs" in m
        assert m["properties"]["governingLaw"]["$ref"] == "#/$defs/Jurisdiction"

    def test_enum_preserved(self, enum_schema):
        m = _massage_for_openai_strict(enum_schema)
        assert m["$defs"]["Jurisdiction"]["enum"] == ["Washington", "New_York"]

    def test_meta_keys_dropped(self):
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$id": "urn:x",
            "$comment": "drop me",
            "type": "object",
            "properties": {"a": {"type": "string"}},
        }
        m = _massage_for_openai_strict(schema)
        assert "$schema" not in m
        assert "$id" not in m
        assert "$comment" not in m

    def test_does_not_mutate_input(self, enum_schema):
        before = copy.deepcopy(enum_schema)
        _massage_for_openai_strict(enum_schema)
        assert enum_schema == before  # input untouched


# ---------------------------------------------------------------------------
# (d) caching (T9)
# ---------------------------------------------------------------------------
class TestMassageCache:
    def test_second_call_returns_equal_copy_not_identity(self, enum_schema):
        first = _massage_for_openai_strict(enum_schema)
        second = _massage_for_openai_strict(enum_schema)
        # Cache returns an equal COPY, not the same object: handing out the cached
        # object by reference would let a caller that mutates it poison the cache.
        assert first == second
        assert first is not second
        # Anti-poisoning: mutating a returned result must not affect later calls.
        first["properties"]["__injected__"] = {"type": "string"}
        third = _massage_for_openai_strict(enum_schema)
        assert "__injected__" not in third["properties"]

    def test_cache_does_not_recompute(self, monkeypatch, enum_schema):
        """After the first build, a sentinel proves the walk isn't re-run."""
        calls = {"n": 0}
        real_walk = llm_mod._strict_walk

        def counting_walk(node):
            calls["n"] += 1
            return real_walk(node)

        monkeypatch.setattr(llm_mod, "_strict_walk", counting_walk)

        _massage_for_openai_strict(enum_schema)
        after_first = calls["n"]
        assert after_first > 0  # built once

        _massage_for_openai_strict(enum_schema)
        # No additional walk invocations on the cached call.
        assert calls["n"] == after_first

    def test_equal_schemas_share_cache_regardless_of_key_order(self):
        a = {"type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "integer"}}}
        # Same content, different insertion order.
        b = {"properties": {"y": {"type": "integer"}, "x": {"type": "string"}}, "type": "object"}
        m1 = _massage_for_openai_strict(a)
        m2 = _massage_for_openai_strict(b)
        assert m1 == m2  # stable sort_keys hash => one cache entry, equal copies


class TestStructuredGuards:
    """Hardening (pre-landing review F4): provider responses that violate the
    dict contract fail loud with a clear error instead of an opaque crash."""

    def _openai(self, monkeypatch, content):
        class Client(_FakeOpenAIClient):
            response_content = content
        monkeypatch.setattr(llm_mod, "OpenAI", Client)

    def test_openai_invalid_json_raises(self, monkeypatch, enum_schema):
        self._openai(monkeypatch, "{not valid json")
        with pytest.raises(ValueError, match="not valid JSON"):
            call_llm_structured("q", "c", json_schema=enum_schema, provider="openai", api_key="sk-test")

    def test_openai_non_object_raises(self, monkeypatch, enum_schema):
        self._openai(monkeypatch, "[1, 2, 3]")
        with pytest.raises(ValueError, match="not a JSON object"):
            call_llm_structured("q", "c", json_schema=enum_schema, provider="openai", api_key="sk-test")

    def test_openai_empty_raises(self, monkeypatch, enum_schema):
        self._openai(monkeypatch, "")
        with pytest.raises(ValueError, match="empty"):
            call_llm_structured("q", "c", json_schema=enum_schema, provider="openai", api_key="sk-test")

    def test_anthropic_non_dict_input_raises(self, monkeypatch, enum_schema):
        class Client(_FakeAnthropicClient):
            tool_input = ["not", "a", "dict"]
        monkeypatch.setattr(llm_mod, "Anthropic", Client)
        with pytest.raises(ValueError, match="not a JSON object"):
            call_llm_structured("q", "c", json_schema=enum_schema, provider="anthropic", api_key="sk-test")

    def test_openai_strips_concerto_keys_from_real_schema(self, monkeypatch):
        # F1: the massager itself must strip $decorators/$class so a raw Concerto
        # schema is OpenAI-strict-valid even without the demo's pre-sanitizer.
        raw = {
            "type": "object",
            "properties": {"governingLaw": {"$ref": "#/definitions/J", "$decorators": {"x": 1}}},
            "definitions": {"J": {"enum": ["Washington", "New_York"], "$class": "concerto.Enum"}},
        }
        m = _massage_for_openai_strict(raw)
        blob = json.dumps(m)
        assert "$decorators" not in blob and "$class" not in blob
        assert "$defs" in m and "Washington" in blob  # enum + $defs preserved

    def test_strips_openai_unsupported_validation_keywords(self):
        # P1 (Codex gate): OpenAI strict rejects default/minLength/pattern etc.
        # The real Concerto schema carries all three; the massager must strip them
        # while preserving the enum, or every OpenAI structured call BadRequests.
        raw = {
            "type": "object",
            "properties": {
                "disclosingParty": {"type": "string", "minLength": 1},
                "effectiveDate": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                "termMonths": {"type": "integer", "default": 24, "minimum": 1},
                "governingLaw": {"$ref": "#/definitions/J"},
            },
            "definitions": {"J": {"enum": ["Washington", "New_York"]}},
        }
        blob = json.dumps(_massage_for_openai_strict(raw))
        for k in ("default", "minLength", "pattern", "minimum"):
            assert k not in blob, f"{k} should be stripped for OpenAI strict mode"
        assert "Washington" in blob and "New_York" in blob  # enum preserved
