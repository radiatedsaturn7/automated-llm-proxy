"""Bounded, unambiguous UTF-8 JSON for inference request bodies."""
import json
import math

from fastapi import HTTPException


def parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('Non-finite JSON number')
        return result

    def invalid(value):
        raise ValueError('Invalid JSON constant')

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_float=number, parse_constant=invalid)
        # Cap nesting before downstream deepcopy/translation/serialization. Also
        # reject escaped lone surrogates, which JSON parsing itself accepts but
        # the provider's UTF-8 encoder cannot represent.
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 64:
                raise ValueError('JSON nesting too deep')
            if isinstance(item, str):
                item.encode('utf-8')
            elif isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
                for key in item:
                    key.encode('utf-8')
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise HTTPException(400, 'Invalid inference JSON') from None
