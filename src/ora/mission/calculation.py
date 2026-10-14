"""Bounded, deterministic arithmetic over explicitly supplied research values.

This module does not retrieve data, infer statistical quantities, or validate
that a supplied value matches a source. The mission host owns source lineage.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext


class CalculationError(ValueError):
    """The requested calculation is outside the supported arithmetic language."""


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_LITERAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_MAX_INPUTS = 20
_MAX_EXPRESSION = 160
_MAX_NODES = 64
_MAX_DEPTH = 12
_MAX_ADJUSTED_EXPONENT = 18
_PRECISION = 28

CALCULATION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "expression": {"type": "string", "minLength": 1, "maxLength": _MAX_EXPRESSION},
        "inputs": {
            "type": "array",
            "maxItems": _MAX_INPUTS,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 32},
                    "value": {"type": ["number", "string"]},
                },
                "required": ["name", "value"],
                "additionalProperties": False,
            },
        },
        "units": {"type": ["string", "null"], "maxLength": 40},
    },
    "required": ["expression", "inputs"],
    "additionalProperties": False,
}

CALCULATION_TOOL_DESCRIPTION = (
    "Calculate bounded arithmetic from values you explicitly supply. Use +, -, *, /, "
    "parentheses, sum(...), or mean(...); expression names refer to named inputs. "
    "Returns the numeric result and supplied units without retrieving data. This does not "
    "verify source values or infer confidence intervals or p-values. Supply units "
    "only when known."
)


def _checked(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise CalculationError("Values and results must be finite")
    if value and abs(value.adjusted()) > _MAX_ADJUSTED_EXPONENT:
        raise CalculationError("Value or result is outside the supported magnitude")
    significant = len(value.as_tuple().digits)
    for digit in reversed(value.as_tuple().digits):
        if digit:
            break
        significant -= 1
    if significant > _PRECISION:
        raise CalculationError("Input or literal exceeds 28 significant digits")
    return value


def _number(value: object) -> Decimal:
    if type(value) not in (int, float, str):
        raise CalculationError("Each input must be a number or plain decimal string")
    raw = str(value)
    if len(raw) > 48 or (type(value) is str and not _DECIMAL.fullmatch(raw)):
        raise CalculationError("Each input must be a bounded plain decimal value")
    try:
        return _checked(Decimal(raw))
    except InvalidOperation as exc:
        raise CalculationError("Each input must be a finite decimal value") from exc


def _render(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered == "-0" else rendered


def _eval(node: ast.AST, expression: str, values: dict[str, Decimal], depth: int) -> Decimal:
    if depth > _MAX_DEPTH:
        raise CalculationError("Expression nesting exceeds the limit")
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        literal = ast.get_source_segment(expression, node)
        if literal is None or not _LITERAL.fullmatch(literal):
            raise CalculationError("Numeric literals must use decimal notation")
        try:
            return _checked(Decimal(literal))
        except InvalidOperation as exc:
            raise CalculationError("Numeric literal is invalid") from exc
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise CalculationError(f"Unknown input: {node.id}")
        return values[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval(node.operand, expression, values, depth + 1)
        return _checked(value if isinstance(node.op, ast.UAdd) else -value)
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
    ):
        left = _eval(node.left, expression, values, depth + 1)
        right = _eval(node.right, expression, values, depth + 1)
        if isinstance(node.op, ast.Add):
            return _checked(left + right)
        if isinstance(node.op, ast.Sub):
            return _checked(left - right)
        if isinstance(node.op, ast.Mult):
            return _checked(left * right)
        if right == 0:
            raise CalculationError("Division by zero")
        return _checked(left / right)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("sum", "mean")
        and not node.keywords
        and 1 <= len(node.args) <= _MAX_INPUTS
    ):
        members = [_eval(arg, expression, values, depth + 1) for arg in node.args]
        total = _checked(sum(members, Decimal(0)))
        return _checked(total / len(members)) if node.func.id == "mean" else total
    raise CalculationError("Unsupported expression; use basic arithmetic, sum, or mean")


def calculate(
    expression: str, inputs: list[dict], units: str | None = None
) -> dict:
    """Evaluate a small expression and return a reproducible decimal receipt.

    Inputs and formula literals are asserted by the caller. A source reference
    in an outer mission record must never be inferred from these values alone.
    """
    if type(expression) is not str or not expression.strip() or len(expression) > _MAX_EXPRESSION:
        raise CalculationError("Expression must be 1 to 160 characters")
    if type(inputs) is not list or len(inputs) > _MAX_INPUTS:
        raise CalculationError("At most 20 named inputs are allowed")
    if units is not None and (
        type(units) is not str
        or not units.strip()
        or len(units) > 40
        or any(ord(char) < 32 for char in units)
    ):
        raise CalculationError("Units must be a short, printable label")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, OverflowError) as exc:
        raise CalculationError("Expression is not valid arithmetic") from exc
    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise CalculationError("Expression exceeds the node limit")
    values: dict[str, Decimal] = {}
    for item in inputs:
        if type(item) is not dict or set(item) != {"name", "value"}:
            raise CalculationError("Inputs require only name and value")
        name = item["name"]
        if (
            type(name) is not str
            or len(name) > 32
            or not _NAME.fullmatch(name)
            or name in ("sum", "mean")
            or name in values
        ):
            raise CalculationError("Input names must be unique identifiers")
        values[name] = _number(item["value"])
    try:
        with localcontext() as context:
            context.prec = _PRECISION
            result = _checked(_eval(tree.body, expression, values, 0))
            rendered_inputs = [
                {"name": name, "value": _render(value)} for name, value in values.items()
            ]
            rendered_result = _render(result)
    except (DivisionByZero, InvalidOperation, OverflowError) as exc:
        raise CalculationError("Calculation could not produce a finite result") from exc
    return {
        "expression": expression,
        "inputs": rendered_inputs,
        "result": rendered_result,
        "rounding_policy": "28 significant decimal digits; round half even",
        "units": units.strip() if units is not None else None,
        "validation_scope": "calculated_from_supplied_values_not_source_validation",
    }
