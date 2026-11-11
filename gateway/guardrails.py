"""Deterministic content heuristics. No network calls, dynamic plugins or content logging."""
from copy import deepcopy
from dataclasses import dataclass
import json
import math
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
        super().__init__("Content rejected by gateway policy")


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
                        json.dumps(decoded, allow_nan=False)
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


def embedding_postflight(payload, settings):
    """Only finite numeric vectors are excluded; every metadata field is scanned."""
    from types import SimpleNamespace
    if (not isinstance(payload, dict) or set(payload) - {"object", "model", "data", "usage"}
            or payload.get("object") != "list" or not isinstance(payload.get("model"), str)):
        raise GuardrailRejected("unsupported_embedding_output")
    items = payload.get("data")
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise GuardrailRejected("unsupported_embedding_output")
    metadata = {k: v for k, v in payload.items() if k != "data"}
    metadata["data"] = []
    dimensions, total = None, 0
    for index, item in enumerate(items):
        if (not isinstance(item, dict) or set(item) != {"object", "index", "embedding"}
                or item["object"] != "embedding" or type(item["index"]) is not int or item["index"] != index):
            raise GuardrailRejected("unsupported_embedding_output")
        vector = item["embedding"]
        if not isinstance(vector, list) or not 1 <= len(vector) <= 65536:
            raise GuardrailRejected("unsupported_embedding_vector")
        total += len(vector)
        if total > 1_000_000 or (dimensions is not None and dimensions != len(vector)):
            raise GuardrailRejected("embedding_output_limit")
        dimensions = len(vector)
        try:
            if any(type(value) not in {int, float} or not math.isfinite(value) for value in vector):
                raise GuardrailRejected("unsupported_embedding_vector")
        except OverflowError:
            raise GuardrailRejected("unsupported_embedding_vector") from None
        metadata["data"].append({k: v for k, v in item.items() if k != "embedding"})
    if "usage" in payload:
        usage = payload["usage"]
        if (not isinstance(usage, dict) or set(usage) - {"prompt_tokens", "completion_tokens", "total_tokens"}
                or any(type(value) is not int or value < 0 for value in usage.values())):
            raise GuardrailRejected("unsupported_embedding_usage")
    cfg = SimpleNamespace(guardrails_enabled=True, guardrails_action="block",
                          guardrails_detectors=settings.guardrails_detectors,
                          guardrails_max_characters=settings.guardrails_max_characters)
    preflight({"messages": [], "output_metadata": metadata}, "chat", cfg)
    return GuardrailResult(payload, {})


def postflight(payload, settings, endpoint="chat"):

    """Inspect normalized, non-streaming text output; never alter accounting metadata."""
    if not settings.guardrails_output_enabled:
        return GuardrailResult(payload, {})
    if endpoint == "embeddings":
        return embedding_postflight(payload, settings)
    from types import SimpleNamespace
    if not isinstance(payload, dict) or set(payload) - {
        "id", "object", "created", "model", "choices", "usage", "system_fingerprint", "service_tier"
    }:
        raise GuardrailRejected("unsupported_output")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not 1 <= len(choices) <= 128:
        raise GuardrailRejected("unsupported_output")
    messages, metadata = [], {k: v for k, v in payload.items() if k != "choices"}
    metadata["choices"] = []
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) - {"index", "message", "finish_reason", "logprobs"}:
            raise GuardrailRejected("unsupported_output")
        if choice.get("logprobs") is not None:
            raise GuardrailRejected("unsupported_output_logprobs")
        message = choice.get("message")
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or set(message) - {"role", "content", "refusal", "tool_calls", "function_call", "annotations"}
                or ("annotations" in message and message["annotations"] != [])):
            raise GuardrailRejected("unsupported_output_message")
        for field in ("content", "refusal"):
            if message.get(field) is not None and not isinstance(message[field], str):
                raise GuardrailRejected("unsupported_output_multimodal")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or len(calls) > 100:
            raise GuardrailRejected("unsupported_output_tools")
        for call in calls:
            if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                    or call.get("type") != "function" or not isinstance(call.get("id"), str)):
                raise GuardrailRejected("unsupported_output_tools")
        functions = [call["function"] for call in calls]
        if "function_call" in message:
            functions.append(message["function_call"])
        for function in functions:
            if (not isinstance(function, dict) or set(function) != {"name", "arguments"}
                    or not isinstance(function["name"], str) or not isinstance(function["arguments"], str)):
                raise GuardrailRejected("unsupported_output_tools")
        # Refusals are generated content too. Scan separately so they can be
        # redacted without modifying structural/routing metadata.
        messages.extend([{k: v for k, v in message.items() if k != "refusal"},
                         {"role": "assistant", "content": message.get("refusal")}])
        metadata["choices"].append({k: v for k, v in choice.items() if k != "message"})
    cfg = SimpleNamespace(guardrails_enabled=True, guardrails_action=settings.guardrails_output_action,
                          guardrails_detectors=settings.guardrails_detectors,
                          guardrails_max_characters=settings.guardrails_max_characters)
    inspected = preflight({"messages": messages, "output_metadata": metadata}, "chat", cfg)
    result = deepcopy(payload)
    for index, choice in enumerate(result["choices"]):
        original = choice["message"]
        choice["message"] = inspected.payload["messages"][index * 2]
        if "refusal" in original:
            choice["message"]["refusal"] = inspected.payload["messages"][index * 2 + 1]["content"]
    return GuardrailResult(result, inspected.counts)
