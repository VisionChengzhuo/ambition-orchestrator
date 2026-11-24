"""Helpers for separating a final agent report from its surrounding prose."""

import json
import re


def split_summary_message(text):
    """Return the prose and a trailing summary object, accepting Markdown fences."""
    text = text.rstrip()
    end = text.removesuffix("\n```").rstrip()
    fenced = end != text
    if not end.endswith("}"):
        return text, None
    for match in re.finditer(r"(?m)^[ \t]*(?P<fence>```(?:json)?[ \t]*\n[ \t]*)?(?P<object>\{)", end):
        if bool(match["fence"]) != fenced:
            continue
        try:
            value = json.loads(end[match.start("object"):])
        except ValueError:
            continue
        if isinstance(value, dict) and "summary" in value:
            return end[:match.start()].rstrip(), value
    return text, None
