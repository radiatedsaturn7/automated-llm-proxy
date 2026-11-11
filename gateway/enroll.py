"""Explicit operator CLI: enroll a private device journal using browser SSO approval."""
import argparse
import asyncio
import json
import time
from uuid import UUID

import httpx

from .relay import RelaySettings
from .relay_journal import CredentialJournal


class EnrollmentError(RuntimeError):
    pass


async def request(client, url, payload, root=None):
    headers = {"Authorization": "Bearer " + root} if root else {}
    try:
        async with asyncio.timeout(30):
            async with client.stream("POST", url, json=payload, headers=headers, follow_redirects=False) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise EnrollmentError("Enrollment response too large")
                if response.status_code == 410:
                    return {"state": "expired"}
                if response.status_code == 429:
                    return {"state": "slow_down"}
                if response.status_code != 200:
                    raise EnrollmentError(f"Enrollment rejected ({response.status_code}); no credential change")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise EnrollmentError("Invalid enrollment response")
                return result
    except Exception:
        raise EnrollmentError("Enrollment request unavailable; rerun with the same private journal") from None


async def enroll(journal, client, notify, *, wait_seconds=600, sleep=asyncio.sleep):
    pending = journal.state.get("enrollment")
    if pending is None:
        if journal.state.get("key_id"):
            return {"state": "already_enrolled", "key_id": journal.state["key_id"]}
        raise EnrollmentError("Existing manually provisioned journal cannot be enrolled")
    root = journal.state["current"]
    base = journal.central_url + "/auth/agent/enrollment"
    result = await request(client, base + "/start", {**pending, "candidate_credential": root})
    if result.get("state") == "expired":
        result = await request(client, base + "/recover", {"request_id": pending["request_id"]}, root)
        return complete(journal, pending, result)
    try:
        code = str(UUID(result["approval_code"]))
        if UUID(code).version != 4:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise EnrollmentError("Enrollment initiation unavailable; rerun with the same journal") from None
    # A public approval identifier, never a root secret or bearer token.
    notify({"approval_code": code, "approval_url": journal.central_url + "/relay-enroll",
            "device_name": pending["device_name"], "policy": result.get("policy")})
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        result = await request(client, base + "/poll", {"request_id": pending["request_id"]}, root)
        state = result.get("state")
        if state == "approved":
            return complete(journal, pending, result)
        if state == "expired":
            result = await request(client, base + "/recover", {"request_id": pending["request_id"]}, root)
            return complete(journal, pending, result)
        if state not in {"pending", "slow_down"}:
            raise EnrollmentError("Enrollment denied or unavailable; journal retained")
        await sleep(5)
    raise EnrollmentError("Approval wait expired; journal retained for recovery")


def complete(journal, pending, result):
    if (result.get("state") != "approved" or result.get("request_id") != pending["request_id"]
            or not isinstance(result.get("key_id"), str) or not result["key_id"]
            or not isinstance(result.get("team_id"), str) or not isinstance(result.get("user_id"), str)):
        raise EnrollmentError("Invalid enrollment acknowledgement")
    journal._write({**journal.state, "enrollment": None, "key_id": result["key_id"], "last_rotated": time.time()})
    return {k: result[k] for k in ("state", "key_id", "team_id", "user_id")}


async def run(args):
    settings = RelaySettings(central_url=args.gateway.rstrip("/"), credential="", local_token="x" * 32,
                             journal_directory=args.journal)
    journal = None
    try:
        journal = CredentialJournal(args.journal, settings.central_url, "", enrollment_name=args.name)
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False,
                                    limits=httpx.Limits(max_connections=1)) as client:
            result = await enroll(journal, client, lambda info: print(json.dumps(info), flush=True))
            print(json.dumps(result))
    finally:
        if journal is not None:
            journal.close()


def main():
    parser = argparse.ArgumentParser(description="Enroll one private relay device with explicit SSO browser approval")
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--journal", required=True, help="Existing private mode0700 directory")
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception:
        print("Enrollment incomplete. Keep the private journal and retry; inspect browser/operator policy.")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
