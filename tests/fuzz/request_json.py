"""Deterministic mutation/property checks of the inbound inference JSON boundary."""
import argparse
import json
import math
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi import HTTPException  # noqa: E402
from gateway.request_json import parse  # noqa: E402


class ObjectPairs(list):
    """Preserve duplicate keys for independent post-parse inspection."""


def oracle(raw):
    """Model acceptance independently using preserved pairs and recursive walking."""
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=ObjectPairs)

        def walk(node, depth=0):
            if depth > 64:
                raise ValueError('depth')
            if isinstance(node, ObjectPairs):
                result = {}
                for key, item in node:
                    key.encode('utf-8')
                    if key in result:
                        raise ValueError('duplicate')
                    result[key] = walk(item, depth + 1)
                return result
            if isinstance(node, list):
                return [walk(item, depth + 1) for item in node]
            if isinstance(node, str):
                node.encode('utf-8')
            if isinstance(node, float) and not math.isfinite(node):
                raise ValueError('nonfinite')
            return node

        return True, walk(value)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return False, None


def check(raw):
    accepted, expected = oracle(raw)
    try:
        actual = parse(raw)
    except HTTPException as exc:
        assert not accepted, 'rejected valid bounded JSON'
        assert exc.status_code == 400 and exc.detail == 'Invalid inference JSON', 'unsanitized failure'
    else:
        assert accepted, 'accepted ambiguous or unsafe JSON'
        assert actual == expected, 'changed JSON semantics'
        # Accepted values must survive provider-side UTF-8 serialization.
        encoded = json.dumps(actual, ensure_ascii=False, allow_nan=False).encode('utf-8')
        assert parse(encoded) == actual, 'round-trip instability'


def generated(rng, depth=0):
    atoms = [None, True, False, 0, -1, 2**53 + 1, 1.25, '', '雪', '\U0001f600', '\\', '\x00']
    if depth >= 5 or rng.randrange(3) == 0:
        return rng.choice(atoms)
    if rng.randrange(2):
        return [generated(rng, depth + 1) for _ in range(rng.randrange(5))]
    return {f'{rng.choice(["key", "雪", "quote\""])}{i}': generated(rng, depth + 1)
            for i in range(rng.randrange(5))}


def run(seed, cases):
    rng = random.Random(seed)
    corpus = [s.encode('utf-8') for s in json.loads(Path(__file__).with_name('request-json-seeds.json').read_text())]
    # Exact nesting boundaries, decoder recursion, invalid UTF-8 and BOMs.
    corpus += [b'[' * n + b'0' + b']' * n for n in (63, 64, 65, 66, 1100)]
    corpus += [b'{"x":"\xff"}', b'\xff\xfe{\x00}\x00', b'\xef\xbb\xbf{}', b'{"x":' + b'9'*5000 + b'}']
    for index, raw in enumerate(corpus):
        try:
            check(raw)
        except Exception as exc:
            raise AssertionError(f'corpus regression index={index}; contents omitted') from exc
    for index in range(cases):
        value = generated(rng)
        raw = json.dumps(value, ensure_ascii=bool(rng.randrange(2)), allow_nan=False).encode('utf-8')
        try:
            check(raw)  # Valid generated values must retain exact semantics.
            source = bytearray(rng.choice(corpus) if rng.randrange(2) else raw)
            for _ in range(1 + rng.randrange(4)):
                pos = rng.randrange(len(source) + 1)
                action = rng.randrange(3)
                if action == 0:
                    source[pos:pos] = bytes([rng.randrange(256)])
                elif action == 1:
                    del source[pos:pos+1]
                elif pos < len(source):
                    source[pos] = rng.randrange(256)
            check(bytes(source))
            # Escaped keys must collide with their decoded equivalent, at any nesting.
            key = f'k{rng.randrange(10000)}'
            escaped = ''.join('\\u%04x' % ord(c) for c in key)
            duplicate = ('{"%s":0,"%s":1}' % (key, escaped)).encode()
            duplicate = b'[' * (index % 20) + duplicate + b']' * (index % 20)
            check(duplicate)
            assert not oracle(duplicate)[0]
        except Exception as exc:
            raise AssertionError(f'fuzz regression seed={seed} case={index}; contents omitted') from exc
    print(f'Request JSON properties passed: seed={seed}, cases={cases}, curated={len(corpus)}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=20261112)
    parser.add_argument('--cases', type=int, default=2000)
    args = parser.parse_args()
    if not 1 <= args.cases <= 20000:
        parser.error('--cases must be between 1 and 20000')
    run(args.seed, args.cases)
