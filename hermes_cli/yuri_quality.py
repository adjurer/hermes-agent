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
import sqlite3
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
    r"(이관하겠습니다|배정하겠습니다|넘기겠습니다|라우팅하겠습니다|맡기겠습니다|"
    r"일로\s*보겠습니다|업무로\s*보겠습니다|"
    r"(운영|기획|조사|문서|검증|작성)팀에\s*(?:이관|배정|넘기|맡기))"
)

WORKER_COMPLETE_RE = re.compile(
    r"(완료(?:했습니다|했습니다,|함)|전송 완료|마무리했습니다|처리 완료|보고드립니다)"
)

STOCK_ORCHESTRATION_RE = re.compile(
    r"(진행과\s*검증은\s*나눠\s*진행하고|결과는\s*제가\s*모아서\s*짧게\s*보고|"
    r"확인해야\s*할\s*상태,\s*실제\s*원인,\s*바로\s*할\s*조치)"
)

FALSE_BLOCKED_STATUS_RE = re.compile(
    r"(막혀\s*있|막힘|blocked\s*\d+\s*건|계속\s*막|작업.*막)",
    re.IGNORECASE,
)

HELD_COUNT_STATUS_RE = re.compile(
    r"(검수\s*/?\s*판단\s*보류|검수\s*보류)\s*\d+\s*건",
    re.IGNORECASE,
)

BLOCKED_STATUS_CONTEXT_RE = re.compile(
    r"(현재\s*진행\s*장애는\s*아닙니다|과거\s*검수\s*보류|검수\s*보류|진행\s*중\s*worker\s*장애가\s*아니라)",
    re.IGNORECASE,
)

SPECIFIC_INTERPRETATION_RE = re.compile(
    r"(제가 이해한|이건|이 일은|말씀하신|요청은|목표는|핵심은|"
    r"상태|원인|파일|문서|로그|게이트웨이|텔레그램|지도|사이트|검토|조사|수정|설정|접속)"
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
                    recommendation="건강 확인, OK-only, 가능 여부 질문은 직접 짧게 답하고 확인/분석/수정/생성/검증 같은 업무성 요청은 Kanban/planner-first로 보냅니다.",
                ))
            elif not SPECIFIC_INTERPRETATION_RE.search(text) and len(text.strip()) < 80:
                issues.append(QualityIssue(
                    code="thin_orchestration_ack",
                    severity="medium",
                    message_id=mid,
                    summary="구체적 업무 해석 없이 팀 이관만 말해 로봇처럼 보입니다.",
                    recommendation="첫 응답에는 이번 일을 어떻게 이해했는지 한 문장으로 적고, 팀 배정은 짧게 붙입니다.",
                ))

        if _is_yuri(sender) and STOCK_ORCHESTRATION_RE.search(text):
            issues.append(QualityIssue(
                code="stock_orchestration_phrase",
                severity="medium",
                message_id=mid,
                summary="반복되는 오케스트레이션 멘트가 사용자 대화에 노출되었습니다.",
                recommendation="고정 문구 대신 이번 업무의 구체적 해석, 맡길 팀, 완료 후 보고 방식만 짧게 말합니다.",
            ))

        if _is_yuri(sender) and FALSE_BLOCKED_STATUS_RE.search(text) and not BLOCKED_STATUS_CONTEXT_RE.search(text):
            issues.append(QualityIssue(
                code="blocked_status_overstated",
                severity="high",
                message_id=mid,
                summary="보류/검수 상태를 현재 진행 장애처럼 말했습니다.",
                recommendation="blocked 카드는 실제 실행 장애, 검수 보류, 과거 실패 잔여물을 구분해 말하고 '막힘' 단정 표현을 피합니다.",
            ))

        if _is_yuri(sender) and HELD_COUNT_STATUS_RE.search(text):
            recent = " ".join(_text(prev) for prev in rows[max(0, idx - 3):idx])
            if re.search(r"(진행|하고\s*있는|하고있는|돌고|뭐가\s*있|무슨\s*일)", recent) and not re.search(r"(전체|남은|대기|보류|막힘|막혀|백로그|큐|밀린)", recent):
                issues.append(QualityIssue(
                    code="held_count_leaked_into_active_status",
                    severity="high",
                    message_id=mid,
                    summary="진행 중 작업 질문에 보류 카운트를 섞어 답했습니다.",
                    recommendation="현재 진행 상태 질문에는 running/review만 답하고, 대기/보류/전체 큐는 사용자가 명시적으로 물을 때만 분리해서 보여줍니다.",
                ))

    return issues


def _parse_skill_frontmatter(skill_md: Path) -> dict[str, str]:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    front = text[3:end]
    out: dict[str, str] = {}
    for line in front.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in {"name", "description"}:
            continue
        out[key] = value.strip().strip("'\"")
    return out


def _skill_records(root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return records
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            rel = skill_md.relative_to(root).parent
        except ValueError:
            rel = skill_md.parent
        front = _parse_skill_frontmatter(skill_md)
        declared = front.get("name") or skill_md.parent.name
        aliases = sorted({declared, skill_md.parent.name, str(rel)})
        key = declared
        records[key] = {
            "name": declared,
            "path": str(rel),
            "dir_name": skill_md.parent.name,
            "description": front.get("description", ""),
            "aliases": aliases,
        }
    return records


def _merge_skill_record(
    merged: dict[str, dict[str, Any]],
    *,
    source: str,
    name: str,
    record: Mapping[str, Any],
) -> None:
    item = merged.setdefault(
        name,
        {
            "name": name,
            "description": record.get("description", ""),
            "aliases": sorted(set(record.get("aliases") or [])),
            "sources": [],
            "paths": {},
        },
    )
    if source not in item["sources"]:
        item["sources"].append(source)
    item["paths"][source] = record.get("path", "")
    item["aliases"] = sorted(set(item.get("aliases") or []) | set(record.get("aliases") or []))
    if not item.get("description") and record.get("description"):
        item["description"] = record.get("description", "")


def _skill_available(records: Mapping[str, Mapping[str, Any]], skill_name: str) -> bool:
    wanted = str(skill_name or "").strip()
    if not wanted:
        return False
    for name, record in records.items():
        aliases = set(record.get("aliases") or [])
        if wanted == name or wanted in aliases:
            return True
    return False


def _profile_dirs(home: Path) -> list[Path]:
    profiles_dir = home / "profiles"
    if not profiles_dir.exists():
        return []
    out: list[Path] = []
    for profile in sorted(profiles_dir.iterdir()):
        if profile.is_dir() and ((profile / "profile.yaml").exists() or (profile / "config.yaml").exists()):
            out.append(profile)
    return out


def _recent_skill_usage(home: Path, *, limit: int = 500) -> dict[str, dict[str, Any]]:
    """Return explicit Kanban skill-use evidence from recent task rows.

    This intentionally counts only task.skills JSON, not passive skill-index
    visibility. It answers "was this skill actually force-loaded recently?"
    without pretending that every visible skill was used.
    """
    dbs = [home / "kanban" / "kanban.db"]
    boards = home / "kanban" / "boards"
    if boards.exists():
        dbs.extend(sorted(boards.glob("*/kanban.db")))
    usage: dict[str, dict[str, Any]] = {}
    seen_dbs: set[Path] = set()
    for db in dbs:
        if not db.exists() or db in seen_dbs:
            continue
        seen_dbs.add(db)
        conn = None
        try:
            conn = sqlite3.connect(
                f"{db.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=0.2,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                "SELECT id, title, skills, created_at, completed_at FROM tasks "
                "WHERE skills IS NOT NULL AND skills != '' "
                "ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        except Exception:
            continue
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        board = db.parent.name if db.parent.name != "kanban" else "default"
        for row in rows:
            try:
                skills = json.loads(row["skills"] or "[]")
            except Exception:
                continue
            if not isinstance(skills, list):
                continue
            for raw in skills:
                skill = str(raw or "").strip()
                if not skill:
                    continue
                item = usage.setdefault(
                    skill,
                    {
                        "count": 0,
                        "latest_task_id": "",
                        "latest_title": "",
                        "latest_created_at": 0,
                        "latest_completed_at": None,
                        "latest_board": "",
                    },
                )
                item["count"] += 1
                created_at = int(row["created_at"] or 0)
                if created_at >= int(item.get("latest_created_at") or 0):
                    item["latest_task_id"] = row["id"]
                    item["latest_title"] = row["title"]
                    item["latest_created_at"] = created_at
                    item["latest_completed_at"] = row["completed_at"]
                    item["latest_board"] = board
    return usage


def build_inventory(*, hermes_home: str | Path | None = None, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Build a lightweight inventory of skills, profiles, MCP servers, and tools."""
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    repo = Path(repo_root or home / "hermes-agent").expanduser()
    root_skill_records = _skill_records(home / "skills")
    repo_skill_records = _skill_records(repo / "skills")
    merged_skill_records: dict[str, dict[str, Any]] = {}
    for source, records in (("home", root_skill_records), ("repo", repo_skill_records)):
        for name, record in records.items():
            _merge_skill_record(merged_skill_records, source=source, name=name, record=record)

    inventory: dict[str, Any] = {
        "hermes_home": str(home),
        "repo_root": str(repo),
        "skills": [],
        "profiles": [],
        "skill_visibility": [],
        "skill_visibility_summary": {},
        "skill_reference_gaps": [],
        "recent_skill_usage": {},
        "mcp_servers": {},
        "tools": [],
    }

    profile_records: dict[str, dict[str, dict[str, Any]]] = {}
    for profile in _profile_dirs(home):
        inventory["profiles"].append(profile.name)
        records = _skill_records(profile / "skills")
        profile_records[profile.name] = records
        for name, record in records.items():
            _merge_skill_record(
                merged_skill_records,
                source=f"profile:{profile.name}",
                name=name,
                record=record,
            )

    inventory["skills"] = sorted(
        {
            str(path)
            for record in merged_skill_records.values()
            for path in (record.get("paths") or {}).values()
            if path
        }
    )

    cfg = _load_yaml(home / "config.yaml")
    skills_cfg = cfg.get("skills") if isinstance(cfg, dict) else {}
    disabled = set()
    platform_disabled: dict[str, set[str]] = {}
    auto_inject: dict[str, list[str]] = {}
    if isinstance(skills_cfg, dict):
        raw_disabled = skills_cfg.get("disabled", [])
        if isinstance(raw_disabled, list):
            disabled = {str(item) for item in raw_disabled if item}
        raw_platform_disabled = skills_cfg.get("platform_disabled", {})
        if isinstance(raw_platform_disabled, dict):
            for platform, names in raw_platform_disabled.items():
                if isinstance(names, list):
                    platform_disabled[str(platform)] = {str(item) for item in names if item}
        raw_auto = skills_cfg.get("auto_inject", {})
        if isinstance(raw_auto, dict):
            for group, data in raw_auto.items():
                names: list[str] = []
                enabled = False
                if isinstance(data, list):
                    enabled = True
                    names = [str(item) for item in data if item]
                elif isinstance(data, dict):
                    enabled = bool(data.get("enabled", False))
                    raw_names = data.get("skills", [])
                    if isinstance(raw_names, list):
                        names = [str(item) for item in raw_names if item]
                if enabled:
                    auto_inject[str(group)] = names

    all_profile_names = sorted(profile_records)
    root_names = set(root_skill_records) | set(repo_skill_records)
    recent_usage = _recent_skill_usage(home)
    inventory["recent_skill_usage"] = recent_usage
    visibility_rows: list[dict[str, Any]] = []
    missing_from_any_profile = 0
    known_skill_refs: set[str] = set()
    for name in sorted(merged_skill_records):
        record = merged_skill_records[name]
        known_skill_refs.add(name)
        known_skill_refs.update(str(alias) for alias in (record.get("aliases") or []) if alias)
        present_profiles = [
            profile
            for profile, records in sorted(profile_records.items())
            if _skill_available(records, name)
        ]
        missing_profiles = [profile for profile in all_profile_names if profile not in present_profiles]
        if missing_profiles:
            missing_from_any_profile += 1
        disabled_platforms = sorted(
            platform
            for platform, names in platform_disabled.items()
            if name in names or any(alias in names for alias in record.get("aliases") or [])
        )
        auto_groups = sorted(
            group
            for group, names in auto_inject.items()
            if name in names or any(alias in names for alias in record.get("aliases") or [])
        )
        usage = next(
            (
                recent_usage[alias]
                for alias in [name, *(record.get("aliases") or [])]
                if alias in recent_usage
            ),
            {},
        )
        visibility_rows.append(
            {
                "name": name,
                "paths": record.get("paths", {}),
                "sources": sorted(record.get("sources", [])),
                "profiles": present_profiles,
                "missing_profiles": missing_profiles,
                "profile_count": len(present_profiles),
                "root_available": name in root_names,
                "globally_disabled": name in disabled or any(alias in disabled for alias in record.get("aliases") or []),
                "platform_disabled": disabled_platforms,
                "auto_inject_groups": auto_groups,
                "recent_use": usage,
                "description": record.get("description", ""),
            }
        )
    inventory["skill_visibility"] = visibility_rows
    inventory["skill_reference_gaps"] = [
        {
            "name": skill,
            "reason": "recent_task_skill_not_installed",
            "recent_use": recent_usage[skill],
        }
        for skill in sorted(recent_usage)
        if skill not in known_skill_refs
    ]
    inventory["skill_visibility_summary"] = {
        "root_skill_count": len(root_names),
        "profile_count": len(all_profile_names),
        "skills_visible_to_all_profiles": sum(1 for row in visibility_rows if not row["missing_profiles"]),
        "skills_missing_from_any_profile": missing_from_any_profile,
        "recent_skill_reference_gaps": len(inventory["skill_reference_gaps"]),
        "auto_inject": auto_inject,
        "recent_skill_usage_count": len(recent_usage),
        "platform_disabled_counts": {
            platform: len(names)
            for platform, names in sorted(platform_disabled.items())
        },
    }

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
        {
            "name": "orchestration acks need concrete interpretation",
            "messages": [{"id": "m7", "sender": "YURI", "text": "네. 운영팀에 이관하겠습니다."}],
            "expect": {"thin_orchestration_ack"},
        },
        {
            "name": "stock orchestration phrases should be avoided",
            "messages": [{"id": "m8", "sender": "YURI", "text": "진행과 검증은 나눠 진행하고, 결과는 제가 모아서 짧게 보고드리겠습니다."}],
            "expect": {"stock_orchestration_phrase"},
        },
        {
            "name": "blocked status must not be overstated",
            "messages": [{"id": "m9", "sender": "YURI", "text": "지금 작업은 막힘 5건입니다."}],
            "expect": {"blocked_status_overstated"},
        },
        {
            "name": "held count must not leak into active status",
            "messages": [
                {"id": "m10a", "sender": "대표님", "text": "우리가 진행하고있는 작업은 뭐에요?"},
                {"id": "m10b", "sender": "YURI", "text": "지금 실제로 돌고 있는 작업은 없습니다. 다만 준비 대기 2건, 검수 보류 5건이 남아 있습니다."},
            ],
            "expect": {"held_count_leaked_into_active_status"},
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
