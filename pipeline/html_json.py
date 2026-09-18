"""JSON serialization for data embedded in an HTML script element."""
import json


def dumps_for_script(value: object) -> str:
    """Preserve JSON values without exposing HTML delimiters to the parser.

    HTML parses script end tags even inside JavaScript strings. Unicode escapes
    also prevent comment/script parser state changes and retain the original
    characters when the JSON is evaluated. Escape line/paragraph separators for
    compatibility with JavaScript engines that treat them as line terminators.
    """
    return (json.dumps(value, ensure_ascii=False)
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))
