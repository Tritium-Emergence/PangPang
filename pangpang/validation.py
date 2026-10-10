from .store import Fault, encode

def validate(value, schema, depth=0):
    if depth > 12 or len(encode(value).encode()) > 32768:
        raise Fault("tool_argument_budget", 400)
    typ = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool, "number": type(value) in {int, float}}
    if typ and not valid.get(typ, False):
        raise Fault("invalid_tool_arguments", 400)
    if "enum" in schema and value not in schema["enum"]:
        raise Fault("invalid_tool_arguments", 400)
    if typ == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value) or (schema.get("additionalProperties") is False and set(value) - set(properties)):
            raise Fault("invalid_tool_arguments", 400)
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], depth + 1)
    elif typ == "array":
        if len(value) > 100:
            raise Fault("tool_argument_budget", 400)
        for item in value:
            validate(item, schema.get("items", {}), depth + 1)
