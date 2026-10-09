"""Small recursive JSON shapes for saved agent settings and tool arguments.

This is an explicit subset of JSON Schema, not a second general schema engine.
Unsupported keywords fail at registration instead of being silently ignored.
"""

from __future__ import annotations

import math
from typing import Any


class JsonShapeError(ValueError):
    """A JSON value or declared tool shape is invalid."""


TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items",
    "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
    "enum", "const", "anyOf",
})


def finite_json(value: Any) -> None:
    """Reject Python-only values, non-finite numbers, non-string keys and cycles."""
    visiting: set[int] = set()

    def visit(item: Any) -> None:
        if item is None or type(item) in {str, int, bool}:
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise JsonShapeError("JSON numbers must be finite")
            return
        if type(item) not in {dict, list}:
            raise JsonShapeError("Value must contain only JSON objects, arrays and scalars")
        identity = id(item)
        if identity in visiting:
            raise JsonShapeError("JSON values cannot contain a cycle")
        visiting.add(identity)
        try:
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise JsonShapeError("JSON object keys must be strings")
                for child in item.values():
                    visit(child)
            else:
                for child in item:
                    visit(child)
        finally:
            visiting.remove(identity)

    try:
        visit(value)
    except RecursionError as exc:
        raise JsonShapeError("JSON value nesting exceeds the interpreter limit") from exc


def _types(schema: dict[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        options = [declared]
    elif isinstance(declared, list) and declared:
        options = declared
    elif declared is None and set(schema) & {"anyOf", "enum", "const"}:
        return set()
    else:
        raise JsonShapeError("Schema needs a type or nonempty type union")
    if any(type(item) is not str or item not in TYPES for item in options):
        raise JsonShapeError("Schema declares an unsupported JSON type")
    if len(options) != len(set(options)):
        raise JsonShapeError("Schema repeats a JSON type")
    return set(options)


def validate_schema(schema: dict[str, Any]) -> None:
    """Check the recursive subset used by this host before exposing a tool."""
    visiting: set[int] = set()

    def visit(part: Any) -> None:
        if type(part) is not dict or not set(part) <= KEYWORDS:
            raise JsonShapeError("Tool schema must use supported JSON-shape keywords")
        identity = id(part)
        if identity in visiting:
            raise JsonShapeError("Tool schema cannot contain a cycle")
        visiting.add(identity)
        try:
            choices = _types(part)
            if "anyOf" in part:
                branches = part["anyOf"]
                if type(branches) is not list or not branches:
                    raise JsonShapeError("anyOf needs nonempty schema branches")
                for branch in branches:
                    visit(branch)
            if "enum" in part:
                if type(part["enum"]) is not list or not part["enum"]:
                    raise JsonShapeError("enum needs nonempty JSON choices")
                finite_json(part["enum"])
            if "const" in part:
                finite_json(part["const"])
            if set(part) & {"properties", "required", "additionalProperties"}:
                if "object" not in choices:
                    raise JsonShapeError("Object keywords require object type")
                properties = part.get("properties", {})
                required = part.get("required", [])
                if type(properties) is not dict or any(type(key) is not str for key in properties):
                    raise JsonShapeError("Properties need string names and schemas")
                if type(required) is not list or any(type(key) is not str for key in required):
                    raise JsonShapeError("Required properties must be string names")
                if len(required) != len(set(required)):
                    raise JsonShapeError("Required property names must be unique")
                for child in properties.values():
                    visit(child)
                extras = part.get("additionalProperties", True)
                if type(extras) is dict:
                    visit(extras)
                elif type(extras) is not bool:
                    raise JsonShapeError("Additional properties need a boolean or schema")
            if set(part) & {"items", "minItems", "maxItems"}:
                if "array" not in choices:
                    raise JsonShapeError("Array keywords require array type")
                if "items" in part:
                    visit(part["items"])
            if set(part) & {"minLength", "maxLength"} and "string" not in choices:
                raise JsonShapeError("String length keywords require string type")
            if set(part) & {"minimum", "maximum"} and not choices & {"integer", "number"}:
                raise JsonShapeError("Numeric bounds require integer or number type")
            for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems")):
                for name in (lower, upper):
                    if name in part and (type(part[name]) is not int or part[name] < 0):
                        raise JsonShapeError(f"{name} must be a nonnegative integer")
                if lower in part and upper in part and part[lower] > part[upper]:
                    raise JsonShapeError(f"{lower} exceeds {upper}")
            for name in ("minimum", "maximum"):
                if name in part and (
                    type(part[name]) not in {int, float}
                    or not math.isfinite(part[name])
                ):
                    raise JsonShapeError(f"{name} must be a finite number")
            if "minimum" in part and "maximum" in part and part["minimum"] > part["maximum"]:
                raise JsonShapeError("minimum exceeds maximum")
        finally:
            visiting.remove(identity)

    try:
        visit(schema)
    except RecursionError as exc:
        raise JsonShapeError("Tool schema nesting exceeds the interpreter limit") from exc


def validate_value(schema: dict[str, Any], value: Any) -> None:
    """Validate a finite JSON value without coercing numbers, nulls or objects."""
    finite_json(value)

    def visit(part: dict[str, Any], item: Any, path: str) -> None:
        if "anyOf" in part:
            for branch in part["anyOf"]:
                try:
                    visit(branch, item, path)
                    break
                except JsonShapeError:
                    continue
            else:
                raise JsonShapeError(f"{path} matches no declared alternative")
        if "enum" in part and not any(_json_equal(item, choice) for choice in part["enum"]):
            raise JsonShapeError(f"{path} is outside the declared choices")
        if "const" in part and not _json_equal(item, part["const"]):
            raise JsonShapeError(f"{path} does not match the declared value")
        choices = _types(part)
        if not choices:
            return
        kind = (
            "null" if item is None else
            "object" if type(item) is dict else
            "array" if type(item) is list else
            "string" if type(item) is str else
            "boolean" if type(item) is bool else
            "integer" if type(item) is int else "number"
        )
        if kind not in choices and not (kind == "integer" and "number" in choices):
            raise JsonShapeError(f"{path} has the wrong JSON type")
        if kind == "object":
            properties = part.get("properties", {})
            missing = set(part.get("required", [])) - item.keys()
            if missing:
                raise JsonShapeError(f"{path} is missing required properties {sorted(missing)}")
            extras = part.get("additionalProperties", True)
            for name, child in item.items():
                if name in properties:
                    visit(properties[name], child, f"{path}.{name}")
                elif extras is False:
                    raise JsonShapeError(f"{path} contains undeclared property {name}")
                elif type(extras) is dict:
                    visit(extras, child, f"{path}.{name}")
        elif kind == "array":
            if "minItems" in part and len(item) < part["minItems"]:
                raise JsonShapeError(f"{path} has too few items")
            if "maxItems" in part and len(item) > part["maxItems"]:
                raise JsonShapeError(f"{path} has too many items")
            if "items" in part:
                for index, child in enumerate(item):
                    visit(part["items"], child, f"{path}[{index}]")
        elif kind == "string":
            if "minLength" in part and len(item) < part["minLength"]:
                raise JsonShapeError(f"{path} is shorter than declared")
            if "maxLength" in part and len(item) > part["maxLength"]:
                raise JsonShapeError(f"{path} is longer than declared")
        elif kind in {"integer", "number"}:
            if "minimum" in part and item < part["minimum"]:
                raise JsonShapeError(f"{path} is below the declared minimum")
            if "maximum" in part and item > part["maximum"]:
                raise JsonShapeError(f"{path} exceeds the declared maximum")

    try:
        visit(schema, value, "$arguments")
    except RecursionError as exc:
        raise JsonShapeError("Argument nesting exceeds the interpreter limit") from exc


def _json_equal(left: Any, right: Any) -> bool:
    """Keep booleans distinct from numbers when matching enum and const."""
    if type(left) in {int, float} and type(right) in {int, float}:
        return left == right
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right
