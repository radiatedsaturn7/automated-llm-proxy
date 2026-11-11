"""Deterministic inbound heuristics. No network calls, dynamic plugins or content logging."""
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Protocol


class Detector(Protocol):
    """Trusted source extension: return bounded spans; never emit matched text."""
    id: str

    def spans(self, text: str) -> list[tuple[int, int]]: ...


@dataclass(frozen=True)
class PatternDetector:
    id: str
    pattern: re.Pattern

    def spans(self, text):
        return [match.span() for match in self.pattern.finditer(text)]


@dataclass(frozen=True)
class CardDetector:
    id: str = "payment_card"

    def spans(self, text):
        spans = []
        for match in re.finditer(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", text):
            digits = [int(x) for x in match[0] if x.isdigit()]
            if len(set(digits)) <= 1:
                continue
            transformed = [x * 2 if i % 2 else x for i, x in enumerate(reversed(digits))]
            if sum(x - 9 if x > 9 else x for x in transformed) % 10 == 0:
                spans.append(match.span())
        return spans


BUILTINS = MappingProxyType({d.id: d for d in (
    PatternDetector("secrets", re.compile(r"(?<![A-Za-z0-9])(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,256}|gh[pousr]_[A-Za-z0-9]{20,255}|AKIA[A-Z0-9]{16})(?![A-Za-z0-9_-])")),
    PatternDetector("email", re.compile(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,5}(?![\w.-])")),
    PatternDetector("us_ssn", re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)")),
    # Consume through the matching END header or the rest of the text when truncated.
    PatternDetector("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\Z)")),
    CardDetector(),
)})


class GuardrailRejected(ValueError):
    def __init__(self, reason, counts=None):
        self.reason = reason
        self.counts = dict(counts or {})
        super().__init__("Inbound content rejected by gateway policy")


@dataclass(frozen=True)
class GuardrailResult:
    payload: dict
    counts: dict[str, int]


def preflight(payload, endpoint, settings):
    """Apply before reservation/dispatch. Exceptions contain metadata only.

    endpoint is chat or embeddings. Caller owns audit persistence and sanitized HTTP errors.
    """
    if not settings.guardrails_enabled:
        return GuardrailResult(payload, {})
    ids = json.loads(settings.guardrails_detectors)
    detectors = tuple(BUILTINS[x] for x in ids)
    counts = {}
    total = 0
    nodes = 0

    def scan(text):
        nonlocal total
        total += len(text)
        if total > settings.guardrails_max_characters:
            raise GuardrailRejected("scan_limit")
        spans = []
        for detector in detectors:
            matches = detector.spans(text)
            if matches:
                counts[detector.id] = min(10000, counts.get(detector.id, 0) + len(matches))
                spans.extend(matches)
        if spans and settings.guardrails_action == "block":
            raise GuardrailRejected("detected", counts)
        # Merge overlapping detections so replacement cannot retain parts of a secret.
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        parts, cursor = [], 0
        for start, end in merged:
            parts.extend((text[cursor:start], "[REDACTED]"))
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)

    def walk(value, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 10000 or depth > 24:
            raise GuardrailRejected("structure_limit")
        if isinstance(value, str):
            return scan(value)
        if isinstance(value, list):
            return [walk(x, depth + 1) for x in value]
        if isinstance(value, dict):
            # Keys may hold data too: block rather than rename keys and change structure.
            for key in value:
                if scan(key) != key:
                    raise GuardrailRejected("sensitive_object_key", counts)
            return {key: walk(item, depth + 1) for key, item in value.items()}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if scan(str(value)) != str(value):
                raise GuardrailRejected("sensitive_numeric_value", counts)
            return value
        if value is None or isinstance(value, bool):
            return value
        raise GuardrailRejected("unsupported_content")

    try:
        result = deepcopy(payload)
    except RecursionError:
        raise GuardrailRejected("structure_limit") from None
    if endpoint == "embeddings":
        # Token IDs can encode private data and cannot be scanned without the tokenizer.
        value = result.get("input")
        if not isinstance(value, str) and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
            raise GuardrailRejected("unsupported_embedding_input")
        result["input"] = walk(value)
    elif endpoint == "chat":
        for message in result.get("messages", []):
            if "content" in message:
                # Binary/image/audio content is not inspected. Fail closed rather than claim coverage.
                content = message["content"]
                if isinstance(content, list) and any(not isinstance(x, dict) or x.get("type") not in {"text", "input_text"} for x in content):
                    raise GuardrailRejected("unsupported_multimodal_content")
                message["content"] = walk(content)
            calls = message.get("tool_calls", [])
            if "function_call" in message:
                calls = [*calls, {"function": message["function_call"]}]
            for call in calls:
                function = call.get("function", {})
                arguments = function.get("arguments")
                if arguments is not None:
                    if not isinstance(arguments, str) or len(arguments) > settings.guardrails_max_characters:
                        raise GuardrailRejected("scan_limit")
                    try:
                        decoded = json.loads(arguments, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, TypeError, RecursionError):
                        raise GuardrailRejected("invalid_tool_arguments") from None
                    function["arguments"] = json.dumps(walk(decoded), ensure_ascii=False, allow_nan=False)
    else:
        raise GuardrailRejected("unsupported_endpoint")
    # Non-content parameters (tool schemas, names, IDs, stop strings) can carry data.
    # Inspect but never rewrite routing/structural metadata: reject matches even in redact mode.
    metadata = {k: v for k, v in result.items() if k not in {"messages", "input"}}
    for message in result.get("messages", []):
        metadata.setdefault("message_metadata", []).append({k: v for k, v in message.items() if k not in {"content", "tool_calls", "function_call"}})
        if "function_call" in message:
            metadata.setdefault("function_metadata", []).append({k: v for k, v in message["function_call"].items() if k != "arguments"})
        for call in message.get("tool_calls", []):
            metadata.setdefault("call_metadata", []).append({k: v for k, v in call.items() if k != "function"})
            metadata.setdefault("function_metadata", []).append({k: v for k, v in call.get("function", {}).items() if k != "arguments"})
    if walk(metadata) != metadata:
        raise GuardrailRejected("sensitive_metadata", counts)
    return GuardrailResult(result, counts)
