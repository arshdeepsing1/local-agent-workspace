"""Bounded, offline validation of model-supplied tool arguments."""

import json
import math
import time
from contextvars import ContextVar

import regex
from jsonschema import Draft202012Validator, ValidationError, validators
from referencing import Registry
from referencing.exceptions import NoSuchResource

MAX_ARGUMENT_BYTES = 512 * 1024
MAX_SCHEMA_BYTES = 16_000
_budget = ContextVar("tool_schema_budget")


def _bounded_json(value, limit, label):
    remaining = 10_000
    size = 0

    def visit(item, depth):
        nonlocal remaining, size
        remaining -= 1
        if depth > 32 or remaining < 0:
            raise ValueError(f"{label} is too complex to validate safely.")
        if isinstance(item, str):
            if len(item) > limit:
                raise ValueError(f"{label} exceeds its {limit}-byte limit.")
            size += len(item.encode("utf-8"))
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} must contain only JSON object keys.")
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError(f"{label} must be JSON.")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{label} must contain finite JSON numbers.")
        size += 2
        if size > limit:
            raise ValueError(f"{label} exceeds its {limit}-byte limit.")

    visit(value, 0)
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds its {limit}-byte limit.")


def validate_schema(schema):
    _bounded_json(schema, MAX_SCHEMA_BYTES, "Tool schema")
    if not isinstance(schema, (dict, bool)):
        raise ValueError("Tool schema must be a JSON Schema object or boolean.")
    visited = 0

    def visit(node, ancestors):
        nonlocal visited
        visited += 1
        if visited > 256 or len(ancestors) > 24 or id(node) in ancestors:
            raise ValueError("Tool schema has recursive references or exceeds the validation complexity limit.")
        if not isinstance(node, dict):
            return
        ancestors = (*ancestors, id(node))
        if any(key in node for key in ("$id", "$anchor", "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "patternProperties")):
            raise ValueError("Tool schema uses unsupported reference or patternProperties features; use local acyclic $refs and ordinary properties.")
        if isinstance(node.get("pattern"), str) and len(node["pattern"]) > 512:
            raise ValueError("Tool schema patterns must be at most 512 characters.")
        if "$schema" in node and (len(ancestors) != 1 or node["$schema"] not in {
                "https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"}):
            raise ValueError("Tool schemas with an explicit dialect must use JSON Schema 2020-12 at the root.")
        if "$ref" in node:
            reference = node["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise ValueError("Tool schema references must be local JSON pointers; external references are disabled.")
            target = schema
            try:
                for part in reference[2:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(target, list):
                        if not part.isascii() or not part.isdigit() or (part != "0" and part.startswith("0")):
                            raise KeyError(part)
                        target = target[int(part)]
                    else:
                        target = target[part]
            except (KeyError, TypeError, IndexError):
                raise ValueError("Tool schema contains an unresolved local reference.") from None
            visit(target, ancestors)
        for key in ("properties", "$defs", "definitions", "dependentSchemas"):
            if isinstance(node.get(key), dict):
                for child in node[key].values():
                    visit(child, ancestors)
        for key in ("items", "contains", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "propertyNames", "not", "if", "then", "else"):
            if isinstance(node.get(key), (dict, bool)):
                visit(node[key], ancestors)
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if isinstance(node.get(key), list):
                for child in node[key]:
                    visit(child, ancestors)

    visit(schema, ())
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        # Do not echo remote schema contents (which may contain credentials).
        raise ValueError("Tool schema is not valid JSON Schema 2020-12.") from error


def _charge():
    budget = _budget.get()
    budget[0] -= 1
    if budget[0] < 0 or time.monotonic() > budget[1]:
        raise ValueError("Tool arguments exceed the safe schema-validation complexity limit.")


def _bounded_keyword(validate):
    def check(validator, expected, instance, schema):
        _charge()
        yield from validate(validator, expected, instance, schema)
    return check


def _pattern(validator, pattern, instance, schema):
    if not isinstance(instance, str):
        return
    try:
        matches = regex.search(pattern, instance, timeout=0.02)
    except (TimeoutError, regex.error) as error:
        raise ValueError("Tool schema pattern could not be checked safely.") from error
    if matches is None:
        yield ValidationError("String does not match the required pattern.")


def _unique_items(validator, expected, instance, schema):
    if not expected or not isinstance(instance, list):
        return

    def identity(value):
        _charge()
        if isinstance(value, dict):
            return ("object", frozenset((key, identity(child)) for key, child in value.items()))
        if isinstance(value, list):
            return ("array", tuple(identity(child) for child in value))
        # JSON Schema compares 1 and 1.0 equally, but not true and 1.
        return ("number" if type(value) in (int, float) else type(value).__name__, value)

    seen = set()
    for value in instance:
        key = identity(value)
        if key in seen:
            yield ValidationError("Array items must be unique.")
            return
        seen.add(key)


_keywords = {**Draft202012Validator.VALIDATORS, "pattern": _pattern, "uniqueItems": _unique_items}
_Validator = validators.extend(Draft202012Validator, {key: _bounded_keyword(value) for key, value in _keywords.items()})


def _no_remote_resource(uri):
    raise NoSuchResource(ref=uri)


def validate_arguments(schema, arguments, *, limit=MAX_ARGUMENT_BYTES):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    _bounded_json(arguments, limit, "Tool arguments")
    validate_schema(schema)
    token = _budget.set([10_000, time.monotonic() + 0.25])
    try:
        error = next(_Validator(schema, registry=Registry(retrieve=_no_remote_resource)).iter_errors(arguments), None)
        if error is not None:
            # Show a bounded field path, never the supplied value or remote schema.
            path = ".".join(str(part) for part in error.absolute_path)[:160] or "arguments"
            raise ValueError(f"Invalid tool arguments at {path}: JSON Schema {error.validator} constraint failed.")
    finally:
        _budget.reset(token)
