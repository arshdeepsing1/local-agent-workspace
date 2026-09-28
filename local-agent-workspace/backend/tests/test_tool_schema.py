import pytest

from local_agent.feature_tools import FEATURE_TOOLS
from local_agent.tools import TOOL_DEFINITIONS
from local_agent.tool_schema import validate_arguments, validate_schema


def test_all_builtin_and_feature_schemas_are_supported():
    for definition in [*TOOL_DEFINITIONS, *FEATURE_TOOLS]:
        validate_schema(definition["function"]["parameters"])


SCHEMA = {"type": "object", "required": ["entry"], "additionalProperties": False,
    "properties": {"entry": {"$ref": "#/$defs/entry"}},
    "$defs": {"entry": {"type": "object", "required": ["count", "tags"], "additionalProperties": False,
        "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3},
            "tags": {"type": "array", "maxItems": 2, "items": {"enum": ["read", "write"]}}}}}}


def test_local_ref_nested_input_and_valid_unicode_are_accepted():
    validate_arguments(SCHEMA, {"entry": {"count": 2, "tags": ["read"]}})
    validate_arguments({"properties": {"label": {"type": "string", "pattern": "^[a-z🌍]+$"}}}, {"label": "hello🌍"})
    validate_arguments({"properties": {"content": {"type": "string"}}}, {"content": "x" * 80_000})


@pytest.mark.parametrize("arguments", [{}, {"entry": {"count": True, "tags": []}},
    {"entry": {"count": 4, "tags": []}}, {"entry": {"count": 2, "tags": ["bad-secret-value"]}},
    {"entry": {"count": 2, "tags": [], "extra": True}}, {"entry": {"count": 2, "tags": []}, "extra": True}])
def test_invalid_inputs_are_readable_without_echoing_values(arguments):
    with pytest.raises(ValueError, match="Invalid tool arguments") as error:
        validate_arguments(SCHEMA, arguments)
    assert "bad-secret-value" not in str(error.value)


@pytest.mark.parametrize("schema", [
    {"$ref": "https://example.invalid/schema"}, {"$ref": "file:///etc/passwd"},
    {"$ref": "#/$defs/missing"}, {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"},
    {"$id": "https://example.invalid/"}, {"$dynamicRef": "#loop"},
    {"patternProperties": {"(a+)+$": {"type": "string"}}},
    {"properties": {"x": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}},
    {"properties": {"x": {"pattern": "a" * 513}}},
    {"allOf": [{}] * 300}, {"type": "not-a-type"},
])
def test_unsupported_or_excessive_schemas_fail_closed_without_network(schema, monkeypatch):
    def network(*args, **kwargs):
        pytest.fail("Schema validation must never retrieve network resources")
    monkeypatch.setattr("urllib.request.urlopen", network)
    with pytest.raises(ValueError):
        validate_arguments(schema, {})


def test_regex_evaluation_is_bounded():
    with pytest.raises(ValueError, match="pattern could not be checked safely"):
        validate_arguments({"properties": {"text": {"type": "string", "pattern": "^(a+)+$"}}}, {"text": "a" * 12_000 + "!"})


def test_input_size_depth_and_non_json_values_are_bounded():
    with pytest.raises(ValueError, match="byte limit"):
        validate_arguments({}, {"x": "🌍" * 9000}, limit=32_000)
    nested = {}
    for _ in range(35):
        nested = {"child": nested}
    with pytest.raises(ValueError, match="complex"):
        validate_arguments({}, nested)
    for arguments in ([1], {"value": float("nan")}, {"value": object()}):
        with pytest.raises(ValueError):
            validate_arguments({}, arguments)


def test_validation_work_budget_bounds_repeated_ref_evaluation():
    schema = {"properties": {"values": {"type": "array", "items": {"allOf": [{"type": "integer"}] * 20}}}}
    with pytest.raises(ValueError, match="complexity limit"):
        validate_arguments(schema, {"values": [1] * 1000})


def test_unique_complex_items_use_bounded_linear_validation(monkeypatch):
    import local_agent.tool_schema as module
    calls = 0
    charge = module._charge

    def counted():
        nonlocal calls
        calls += 1
        charge()

    monkeypatch.setattr(module, "_charge", counted)
    schema = {"properties": {"values": {"uniqueItems": True}}}
    validate_arguments(schema, {"values": [{"id": n} for n in range(1500)]})
    assert calls < 4000
    validate_arguments(schema, {"values": [True, 1, None, "1"]})
    for values in ([1, 1.0], [{"a": 1, "b": 2}, {"b": 2, "a": 1.0}]):
        with pytest.raises(ValueError, match="uniqueItems"):
            validate_arguments(schema, {"values": values})


def test_local_reference_accepts_canonical_array_index():
    schema = {"allOf": [{"properties": {"value": {"type": "string"}}}],
        "properties": {"child": {"$ref": "#/allOf/0"}}}
    validate_arguments(schema, {"child": {"value": "text"}})
    with pytest.raises(ValueError, match="type constraint"):
        validate_arguments(schema, {"child": {"value": 3}})
