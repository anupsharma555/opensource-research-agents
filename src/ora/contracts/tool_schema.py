"""Project canonical inputs into the host's supported tool-schema subset.

Inline local definitions and omit non-executable defaults and regex annotations.
The canonical Pydantic input still performs complete validation at execution;
this projection preserves the existing mission catalog's schema representation.
"""


def tool_schema(model) -> dict:
    schema = model.model_json_schema(by_alias=False)
    definitions = schema.get("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(definitions[value["$ref"].removeprefix("#/$defs/")])
        return {key: inline(item) for key, item in value.items()
                if key not in {"$defs", "default", "pattern"}}

    return inline(schema)
