"""Yuri quality backtests and lightweight self-audit helpers.

This module is intentionally deterministic.  It does not mutate memories,
delete chats, or call an LLM.  The goal is to give Yuri a repeatable
pre-flight check for the behaviors that have caused friction in Telegram:

* claiming a file was sent before an attachment exists,
* exposing local paths or raw internal URLs,
* worker/profile bots reporting directly instead of through Yuri,
* losing obvious recent context,
* surfacing raw cron/gateway failures without secretary-style synthesis.
* routing exact-response/simple health checks into subagents.

The functions accept plain message dictionaries so tests and future Telegram
readers can feed the same scanner without coupling this module to Telethon.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


LOCAL_NOISE_RE = re.compile(
    r"file:///[^\s`<>)\]}]+"
    r"|https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?/[^\s`<>)\]}]*"
    r"|(?<!MEDIA:)(?<![\w./-])(?:/Users/tbd|/private/tmp|/tmp|/var/folders)/[^\s`<>)\]}]+"
)

FILE_CLAIM_RE = re.compile(
    r"(전송(?:했| 완료)|보냈(?:습|어|다)|첨부(?:했| 완료)|파일\s*전달(?:했| 완료))"
)

RAW_FAILURE_RE = re.compile(
    r"(Cron job '.+' failed|Traceback \(most recent call last\)|Connection closed by UNKNOWN port|"
    r"Script exited with code \d+|telegram\.error\.NetworkError|httpx\.[A-Za-z]+Error)"
)

CONTEXT_LOST_RE = re.compile(
    r"(맥락을 먼저 확인해야 합니다|연결할 진행 업무를 찾지 못했습니다|어떤 파일이나 이미지를 기준으로)"
)

CONTEXT_HINT_RE = re.compile(
    r"(scrapling|스크래핑|크롤링|조합|방금|아까|그 방식|그 조합|커뮤니티|파일|산출물)",
    re.IGNORECASE,
)

SIMPLE_CHECK_RE = re.compile(
    r"(OK라고만\s*답|오케이만\s*답|가능\s*여부만\s*답|상태\s*확인|정신차렸어|"
    r"진단\s*응답\s*테스트|테스트.*답해줘)",
    re.IGNORECASE,
)

ORCHESTRATION_CLAIM_RE = re.compile(
    r"(이관하겠습니다|배정하겠습니다|넘기겠습니다|라우팅하겠습니다|"
    r"(운영|기획|조사|문서|검증|작성)팀에\s*(?:이관|배정|넘기))"
)

WORKER_COMPLETE_RE = re.compile(
    r"(완료(?:했습니다|했습니다,|함)|전송 완료|마무리했습니다|처리 완료|보고드립니다)"
)

YURI_NAMES = {"yuri", "유리", "beanslab_bot", "@beanslab_bot"}
INTERNAL_WORKER_HINTS = {
    "planner", "researcher", "ops", "reviewer", "docslead", "analyst", "writer",
    "기획", "조사", "운영", "검증", "문서", "분석", "작성",
}


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message_id: str
    summary: str
    recommendation: str


def _text(msg: Mapping[str, Any]) -> str:
    return str(msg.get("text") or msg.get("message") or "")


def _sender(msg: Mapping[str, Any]) -> str:
    raw = str(msg.get("sender") or msg.get("sender_name") or msg.get("username") or "")
    return raw.strip()


def _message_id(msg: Mapping[str, Any], idx: int) -> str:
    value = msg.get("id") or msg.get("message_id") or idx
    return str(value)


def _has_media(msg: Mapping[str, Any]) -> bool:
    return bool(
        msg.get("has_media")
        or msg.get("media")
        or msg.get("file")
        or msg.get("document")
        or msg.get("photo")
    )


def _is_yuri(sender: str) -> bool:
    folded = sender.casefold().strip()
    return folded in YURI_NAMES or folded == "beanslab_bot"


def _looks_like_internal_worker(sender: str) -> bool:
    folded = sender.casefold()
    if "beanslab_" in folded and "beanslab_bot" not in folded:
        return True
    return any(hint in folded for hint in INTERNAL_WORKER_HINTS)


def scan_messages(messages: Iterable[Mapping[str, Any]], *, context_window: int = 8) -> list[QualityIssue]:
    """Return deterministic quality issues detected in recent chat messages."""
    rows = list(messages)
    issues: list[QualityIssue] = []

    for idx, msg in enumerate(rows):
        text = _text(msg)
        sender = _sender(msg)
        mid = _message_id(msg, idx)

        if text and LOCAL_NOISE_RE.search(text):
            issues.append(QualityIssue(
                code="local_delivery_noise",
                severity="high",
                message_id=mid,
                summary="사용자-facing 메시지에 로컬 경로 또는 내부 URL이 노출되었습니다.",
                recommendation="Telegram 보고 전 local path/localhost/file:// 문자열을 제거하고, 필요한 경우 파일 자체만 첨부합니다.",
            ))

        if FILE_CLAIM_RE.search(text) and not _has_media(msg):
            issues.append(QualityIssue(
                code="file_claim_without_media",
                severity="critical",
                message_id=mid,
                summary="파일 전송 완료처럼 말했지만 같은 메시지에 첨부 증거가 없습니다.",
                recommendation="전송 API의 message_id 또는 실제 media/document evidence 확인 전에는 전송 완료 표현을 금지합니다.",
            ))

        if RAW_FAILURE_RE.search(text):
            issues.append(QualityIssue(
                code="raw_failure_leaked",
                severity="medium",
                message_id=mid,
                summary="cron/gateway 원시 오류가 그대로 사용자 대화에 노출되었습니다.",
                recommendation="원시 로그는 내부 로그에 남기고, 사용자에게는 원인/영향/다음 조치만 요약합니다.",
            ))

        if _looks_like_internal_worker(sender) and not _is_yuri(sender) and WORKER_COMPLETE_RE.search(text):
            issues.append(QualityIssue(
                code="worker_direct_report",
                severity="high",
                message_id=mid,
                summary="서브에이전트/부서 봇이 유리를 거치지 않고 직접 완료 보고를 한 것으로 보입니다.",
                recommendation="worker는 kanban_complete/kanban_block으로만 결과를 남기고, 최종 Telegram 보고는 유리만 담당합니다.",
            ))

        if CONTEXT_LOST_RE.search(text):
            recent = " ".join(_text(prev) for prev in rows[max(0, idx - context_window):idx])
            if CONTEXT_HINT_RE.search(recent):
                issues.append(QualityIssue(
                    code="context_lost_despite_recent_hint",
                    severity="high",
                    message_id=mid,
                    summary="직전 대화에 연결 단서가 있는데도 맥락을 찾지 못했다고 답했습니다.",
                    recommendation="최근 대화/활성 카드/직전 완료 카드 순서로 referent를 복구한 뒤, 그래도 모호할 때만 짧게 되묻습니다.",
                ))

        if _is_yuri(sender) and ORCHESTRATION_CLAIM_RE.search(text):
            recent = " ".join(_text(prev) for prev in rows[max(0, idx - 3):idx])
            if SIMPLE_CHECK_RE.search(recent):
                issues.append(QualityIssue(
                    code="simple_check_overrouted",
                    severity="medium",
                    message_id=mid,
                    summary="간단 진단/정확 응답 요청을 서브에이전트 업무처럼 라우팅했습니다.",
                    recommendation="건강 확인, OK-only, 가능 여부 질문은 직접 짧게 답하고 실제 다단계 업무만 Kanban/subagent로 보냅니다.",
                ))

    return issues


def build_inventory(*, hermes_home: str | Path | None = None, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Build a lightweight inventory of skills, profiles, MCP servers, and tools."""
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    repo = Path(repo_root or home / "hermes-agent").expanduser()
    inventory: dict[str, Any] = {
        "hermes_home": str(home),
        "repo_root": str(repo),
        "skills": [],
        "profiles": [],
        "mcp_servers": {},
        "tools": [],
    }

    for root in (home / "skills", repo / "skills"):
        if not root.exists():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            try:
                rel = skill_md.relative_to(root)
            except ValueError:
                rel = skill_md
            inventory["skills"].append(str(rel.parent))

    profiles_dir = home / "profiles"
    if profiles_dir.exists():
        for profile in sorted(profiles_dir.iterdir()):
            if profile.is_dir() and (profile / "profile.yaml").exists():
                inventory["profiles"].append(profile.name)

    cfg = _load_yaml(home / "config.yaml")
    mcp = cfg.get("mcp_servers") if isinstance(cfg, dict) else None
    if isinstance(mcp, dict):
        for name, data in sorted(mcp.items()):
            if isinstance(data, dict):
                inventory["mcp_servers"][name] = {
                    "enabled": bool(data.get("enabled", False)),
                    "command": str(data.get("command") or ""),
                }

    try:
        from tools import registry
        registry.discover_builtin_tools(repo / "tools")
        inventory["tools"] = sorted(entry.name for entry in registry.registry._snapshot_entries())
    except Exception as exc:
        inventory["tools_error"] = str(exc)

    return inventory


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except Exception:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def run_backtests() -> dict[str, Any]:
    """Run regression-style quality checks against observed Yuri failure modes."""
    cases = [
        {
            "name": "file claim requires attachment evidence",
            "messages": [{"id": "m1", "sender": "YURI", "text": "파일 전송 완료했습니다.", "has_media": False}],
            "expect": {"file_claim_without_media"},
        },
        {
            "name": "local paths must not leak to Telegram",
            "messages": [{"id": "m2", "sender": "YURI", "text": "결과는 /Users/tbd/tmp/report.md 입니다."}],
            "expect": {"local_delivery_noise"},
        },
        {
            "name": "workers should not directly report completion",
            "messages": [{"id": "m3", "sender": "문서제작실", "text": "전송 완료했습니다, 대표님."}],
            "expect": {"worker_direct_report", "file_claim_without_media"},
        },
        {
            "name": "recent context hints should prevent false no-context replies",
            "messages": [
                {"id": "m4a", "sender": "대표님", "text": "Scrapling도 합친 조합으로 할수있어?"},
                {"id": "m4b", "sender": "YURI", "text": "네, Scrapling 조합으로 가능합니다."},
                {"id": "m4c", "sender": "대표님", "text": "네 조합으로 진행해주세요."},
                {"id": "m4d", "sender": "YURI", "text": "맥락을 먼저 확인해야 합니다. 연결할 진행 업무를 찾지 못했습니다."},
            ],
            "expect": {"context_lost_despite_recent_hint"},
        },
        {
            "name": "raw cron failures should be summarized",
            "messages": [{"id": "m5", "sender": "YURI", "text": "⚠️ Cron job 'poll' failed: Script exited with code 255"}],
            "expect": {"raw_failure_leaked"},
        },
        {
            "name": "simple checks should not be routed to subagents",
            "messages": [
                {"id": "m6a", "sender": "대표님", "text": "유리야 진단 응답 테스트입니다. OK라고만 답해줘."},
                {"id": "m6b", "sender": "YURI", "text": "네. 운영팀에 이관하겠습니다."},
            ],
            "expect": {"simple_check_overrouted"},
        },
    ]
    results = []
    ok = True
    for case in cases:
        found = {issue.code for issue in scan_messages(case["messages"])}
        passed = set(case["expect"]).issubset(found)
        ok = ok and passed
        results.append({
            "name": case["name"],
            "passed": passed,
            "expected": sorted(case["expect"]),
            "found": sorted(found),
        })
    return {"ok": ok, "results": results}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Yuri quality backtests and inventory")
    parser.add_argument("--backtest", action="store_true", help="run built-in quality backtests")
    parser.add_argument("--inventory", action="store_true", help="print skill/tool/profile inventory")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    payload: dict[str, Any] = {}
    if args.backtest or not args.inventory:
        payload["backtests"] = run_backtests()
    if args.inventory:
        payload["inventory"] = build_inventory()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if "backtests" in payload:
            status = "PASS" if payload["backtests"]["ok"] else "FAIL"
            print(f"yuri quality backtests: {status}")
            for row in payload["backtests"]["results"]:
                mark = "PASS" if row["passed"] else "FAIL"
                print(f"  {mark} {row['name']}")
        if "inventory" in payload:
            inv = payload["inventory"]
            print(f"skills={len(inv.get('skills', []))} profiles={len(inv.get('profiles', []))} "
                  f"tools={len(inv.get('tools', []))} mcp={len(inv.get('mcp_servers', {}))}")
    return 0 if payload.get("backtests", {}).get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
