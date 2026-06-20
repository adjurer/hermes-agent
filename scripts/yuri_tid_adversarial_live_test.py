#!/Users/tbd/.hermes/hermes-agent/venv/bin/python
"""Run a live Telegram adversarial TID regression test against Yuri.

The script reuses the latest proven 50-case corpus by default. Each outbound
message gets a fresh run id while preserving the case TID, then replies are
graded by TID, required strings, and forbidden strings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


EXTRA_SITE_PACKAGES = os.environ.get("TELEGRAM_WATCH_SITE_PACKAGES")
if not EXTRA_SITE_PACKAGES:
    _fallback_site = Path("/Users/tbd/.hermes/hermes-agent/venv/lib/python3.11/site-packages")
    if _fallback_site.exists():
        EXTRA_SITE_PACKAGES = str(_fallback_site)
if EXTRA_SITE_PACKAGES and EXTRA_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, EXTRA_SITE_PACKAGES)

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/Users/tbd/.hermes"))
DEFAULT_CASES = HERMES_HOME / "state" / "yuri_live_tid_adversarial_50_latest.json"
DEFAULT_OUTPUT = HERMES_HOME / "state" / "yuri_live_tid_adversarial_50_latest.json"
SESSION_PATH = Path(os.environ.get("YURI_TELEGRAM_SESSION", "/Users/tbd/.local/share/telegram-user-mcp/yuri_telegram"))


def load_env(path: Path = HERMES_HOME / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def load_cases(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("sent_cases") or data.get("cases") or []
    clean: list[dict[str, Any]] = []
    for case in cases:
        tid = str(case.get("tid") or "").strip()
        text = str(case.get("text") or "").strip()
        if not tid or not text:
            continue
        clean.append(
            {
                "tid": tid,
                "text": text,
                "must": list(case.get("must") or []),
                "forbid": list(case.get("forbid") or []),
            }
        )
    if limit is not None:
        clean = clean[:limit]
    if not clean:
        raise SystemExit(f"No test cases found in {path}")
    return clean


def target_value(cli_target: str | None) -> str | int:
    raw = cli_target or os.environ.get("YURI_LIVE_TEST_TARGET") or "@beanslab_bot"
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def grade_reply(case: dict[str, Any], text: str | None) -> list[str]:
    failures: list[str] = []
    tid = case["tid"]
    body = text or ""
    if not has_tid(body, tid):
        failures.append("missing_tid")
    for must in case.get("must") or []:
        if str(must) not in body:
            failures.append(f"missing:{must}")
    for forbid in case.get("forbid") or []:
        if str(forbid) in body:
            failures.append(f"forbidden:{forbid}")
    return failures


def has_tid(text: str, tid: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_-])TID={re.escape(tid)}(?![A-Za-z0-9_-])", text or ""))


async def run(args: argparse.Namespace) -> int:
    load_env()
    try:
        from telethon import TelegramClient
    except Exception as exc:  # pragma: no cover - environment guard
        print(f"Telethon import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print("TELEGRAM_API_ID/API_HASH is missing", file=sys.stderr)
        return 2

    cases = load_cases(Path(args.cases), args.limit)
    run_id = args.run_id or f"TID50R-{int(time.time())}"
    target = target_value(args.target)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(SESSION_PATH), int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        print("Telegram user session is not authorized", file=sys.stderr)
        return 2

    started = time.monotonic()
    entity = await client.get_entity(target)
    latest = await client.get_messages(entity, limit=1)
    start_id = int(latest[0].id) if latest else 0

    sent_cases: list[dict[str, Any]] = []
    for idx, case in enumerate(cases, start=1):
        payload = f"[{run_id}] [TID={case['tid']}] {case['text']}"
        msg = await client.send_message(entity, payload)
        sent_cases.append(
            {
                **case,
                "message_id": int(msg.id),
                "payload": payload,
                "at": round(time.monotonic() - started, 3),
                "index": idx,
            }
        )
        await asyncio.sleep(args.send_delay)

    expected = {case["tid"]: case for case in sent_cases}
    replies: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and len(replies) < len(expected):
        async for msg in client.iter_messages(entity, min_id=start_id, reverse=True, limit=args.scan_limit):
            text = msg.message or ""
            if getattr(msg, "out", False):
                continue
            for tid in expected:
                if has_tid(text, tid):
                    replies[tid] = {
                        "id": int(msg.id),
                        "text": text,
                        "date": msg.date.isoformat() if msg.date else None,
                    }
        if len(replies) >= len(expected):
            break
        await asyncio.sleep(args.poll_delay)

    failures: list[dict[str, Any]] = []
    for tid, case in expected.items():
        reply = replies.get(tid)
        problems = grade_reply(case, reply.get("text") if reply else None)
        if not reply:
            problems.append("no_reply")
        if problems:
            failures.append({"tid": tid, "problems": problems, "reply": reply, "case": case})

    result = {
        "run_id": run_id,
        "target": str(target),
        "sent": len(sent_cases),
        "received_messages": len(replies),
        "tid_replies": len(replies),
        "failures": failures,
        "pass": not failures and len(replies) == len(sent_cases),
        "elapsed_sec": round(time.monotonic() - started, 3),
        "sent_cases": sent_cases,
        "replies": [replies[k] for k in sorted(replies)],
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    await client.disconnect()
    print(json.dumps({k: result[k] for k in ("run_id", "sent", "received_messages", "tid_replies", "failures", "pass", "elapsed_sec")}, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="JSON artifact containing sent_cases/cases")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON output path")
    parser.add_argument("--target", default=None, help="Telegram target, default YURI_LIVE_TEST_TARGET or @beanslab_bot")
    parser.add_argument("--run-id", default=None, help="Override run id")
    parser.add_argument("--limit", type=int, default=None, help="Optional number of cases to run")
    parser.add_argument("--timeout", type=float, default=180.0, help="Seconds to wait for replies")
    parser.add_argument("--send-delay", type=float, default=0.2, help="Seconds between outbound messages")
    parser.add_argument("--poll-delay", type=float, default=2.0, help="Seconds between reply scans")
    parser.add_argument("--scan-limit", type=int, default=300, help="Maximum recent messages to scan")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
