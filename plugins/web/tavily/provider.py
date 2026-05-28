"""Tavily web search + content extraction — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Two
capabilities advertised:

- ``supports_search()``  -> True (Tavily ``/search``)
- ``supports_extract()`` -> True (Tavily ``/extract``)

Both are sync — the underlying call is ``httpx.post(...)``.

Config keys this provider responds to::

    web:
      search_backend: "tavily"     # explicit per-capability
      extract_backend: "tavily"    # explicit per-capability
      backend: "tavily"            # shared fallback for both

Env vars::

    TAVILY_API_KEY=...           # https://app.tavily.com/home (required)
    TAVILY_BASE_URL=...          # optional override of https://api.tavily.com
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read a positive integer environment setting with a safe default."""
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _tavily_budget_state_path() -> Path:
    configured = os.getenv("TAVILY_BUDGET_STATE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    return home / "state" / "tavily_budget.json"


def _read_tavily_budget_state() -> Dict[str, Any]:
    import json

    path = _tavily_budget_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - budget telemetry must not break web.
        logger.warning("Could not read Tavily budget state %s: %s", path, exc)
        return {}


def _write_tavily_budget_state(state: Dict[str, Any]) -> None:
    import json

    path = _tavily_budget_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _current_budget_keys() -> tuple[str, str]:
    return time.strftime("%Y-%m"), time.strftime("%Y-%m-%d")


def _estimate_tavily_credits(endpoint: str, payload: Dict[str, Any], response: Dict[str, Any] | None = None) -> int:
    name = endpoint.strip("/").lower()
    if name == "search":
        depth = str(payload.get("search_depth") or "basic").lower()
        return 2 if depth == "advanced" else 1
    if name == "extract":
        if response is not None:
            return max(1, len(response.get("results") or []))
        urls = payload.get("urls") or []
        return max(1, len(urls if isinstance(urls, list) else [urls]))
    if name == "crawl":
        if response is not None:
            return max(1, len(response.get("results") or []))
        return max(1, int(payload.get("limit") or 1))
    return 1


def _check_tavily_budget(endpoint: str, payload: Dict[str, Any]) -> int:
    monthly_cap = _env_int("TAVILY_MONTHLY_CREDIT_CAP", 900)
    daily_cap = _env_int("TAVILY_DAILY_CREDIT_CAP", 30)
    estimated = _estimate_tavily_credits(endpoint, payload)
    month_key, day_key = _current_budget_keys()
    state = _read_tavily_budget_state()
    month_used = int((state.get("months") or {}).get(month_key, 0))
    day_used = int((state.get("days") or {}).get(day_key, 0))
    if month_used + estimated > monthly_cap:
        raise RuntimeError(
            f"Tavily monthly budget cap would be exceeded "
            f"({month_used}+{estimated}>{monthly_cap}). Ask the user before using more."
        )
    if day_used + estimated > daily_cap:
        raise RuntimeError(
            f"Tavily daily budget cap would be exceeded "
            f"({day_used}+{estimated}>{daily_cap}). Ask the user before using more."
        )
    return estimated


def _record_tavily_budget(endpoint: str, payload: Dict[str, Any], response: Dict[str, Any]) -> None:
    credits = _estimate_tavily_credits(endpoint, payload, response)
    month_key, day_key = _current_budget_keys()
    state = _read_tavily_budget_state()
    months = state.setdefault("months", {})
    days = state.setdefault("days", {})
    endpoints = state.setdefault("endpoints", {})
    endpoint_name = endpoint.strip("/").lower()
    months[month_key] = int(months.get(month_key, 0)) + credits
    days[day_key] = int(days.get(day_key, 0)) + credits
    endpoints[endpoint_name] = int(endpoints.get(endpoint_name, 0)) + credits
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _write_tavily_budget_state(state)


def _tavily_request(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST to the Tavily API and return the parsed JSON response.

    Mirrors :func:`tools.web_tools._tavily_request`. Raises ``ValueError``
    when ``TAVILY_API_KEY`` is unset; the caller catches and surfaces as
    a typed error response.
    """
    import httpx

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise ValueError(
            "TAVILY_API_KEY environment variable not set. "
            "Get your API key at https://app.tavily.com/home"
        )

    base_url = os.getenv("TAVILY_BASE_URL", "https://api.tavily.com")
    payload = dict(payload)  # don't mutate caller's dict
    endpoint_name = endpoint.strip("/").lower()
    if endpoint_name in {"crawl", "map", "research"} and not _env_bool("TAVILY_ALLOW_EXPENSIVE", False):
        raise RuntimeError(
            f"Tavily {endpoint_name} is disabled by TAVILY_ALLOW_EXPENSIVE=0 "
            "to preserve the free monthly credit budget."
        )
    _check_tavily_budget(endpoint_name, payload)
    payload["api_key"] = api_key
    url = f"{base_url}/{endpoint.lstrip('/')}"
    logger.info("Tavily %s request to %s", endpoint, url)

    response = httpx.post(url, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    _record_tavily_budget(endpoint_name, payload, data)
    return data


def _normalize_tavily_search_results(response: Dict[str, Any]) -> Dict[str, Any]:
    """Map Tavily ``/search`` response to ``{success, data: {web: [...]}}``."""
    web_results = []
    for i, result in enumerate(response.get("results", [])):
        web_results.append(
            {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "description": result.get("content", ""),
                "position": i + 1,
            }
        )
    return {"success": True, "data": {"web": web_results}}


def _normalize_tavily_documents(
    response: Dict[str, Any], fallback_url: str = ""
) -> List[Dict[str, Any]]:
    """Map Tavily ``/extract`` response to standard documents.

    Documents follow the legacy LLM post-processing shape::

        {"url", "title", "content", "raw_content", "metadata"}

    Failures (``failed_results``, ``failed_urls``) become result entries
    with an ``error`` field rather than raising.
    """
    documents: List[Dict[str, Any]] = []
    for result in response.get("results", []):
        url = result.get("url", fallback_url)
        raw = result.get("raw_content", "") or result.get("content", "")
        documents.append(
            {
                "url": url,
                "title": result.get("title", ""),
                "content": raw,
                "raw_content": raw,
                "metadata": {"sourceURL": url, "title": result.get("title", "")},
            }
        )
    for fail in response.get("failed_results", []):
        documents.append(
            {
                "url": fail.get("url", fallback_url),
                "title": "",
                "content": "",
                "raw_content": "",
                "error": fail.get("error", "extraction failed"),
                "metadata": {"sourceURL": fail.get("url", fallback_url)},
            }
        )
    for fail_url in response.get("failed_urls", []):
        url_str = fail_url if isinstance(fail_url, str) else str(fail_url)
        documents.append(
            {
                "url": url_str,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "extraction failed",
                "metadata": {"sourceURL": url_str},
            }
        )
    return documents


class TavilyWebSearchProvider(WebSearchProvider):
    """Tavily search + extract provider."""

    @property
    def name(self) -> str:
        return "tavily"

    @property
    def display_name(self) -> str:
        return "Tavily"

    def is_available(self) -> bool:
        """Return True when ``TAVILY_API_KEY`` is set to a non-empty value."""
        return bool(os.getenv("TAVILY_API_KEY", "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a Tavily search."""
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            max_results = min(limit, _env_int("TAVILY_MAX_SEARCH_RESULTS", 3), 20)
            logger.info("Tavily search: '%s' (limit=%d, capped=%d)", query, limit, max_results)
            raw = _tavily_request(
                "search",
                {
                    "query": query,
                    "max_results": max_results,
                    "search_depth": os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            return _normalize_tavily_search_results(raw)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — including httpx errors
            logger.warning("Tavily search error: %s", exc)
            return {"success": False, "error": f"Tavily search failed: {exc}"}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via Tavily.

        Sync — the underlying call is httpx.post(...). Returns the legacy
        list-of-results shape; per-URL failures become items with ``error``.
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": u, "error": "Interrupted", "title": ""} for u in urls
                ]

            max_urls = _env_int("TAVILY_MAX_EXTRACT_URLS", 3)
            if len(urls) > max_urls:
                raise RuntimeError(
                    f"Tavily extract is capped at {max_urls} URL(s) per call "
                    "to preserve the free monthly credit budget."
                )

            logger.info("Tavily extract: %d URL(s)", len(urls))
            raw = _tavily_request(
                "extract",
                {
                    "urls": urls,
                    "include_images": False,
                },
            )
            return _normalize_tavily_documents(
                raw, fallback_url=urls[0] if urls else ""
            )
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily extract error: %s", exc)
            return [
                {"url": u, "title": "", "content": "", "error": f"Tavily extract failed: {exc}"}
                for u in urls
            ]

    def crawl(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        """Crawl a seed URL via Tavily's ``/crawl`` endpoint.

        Accepted kwargs (others ignored for forward compat):
          - ``instructions``: str — natural-language guidance for the crawl
          - ``depth``: str — ``"basic"`` (default) or ``"advanced"``
          - ``limit``: int — max pages to crawl (default 20)

        Returns ``{"results": [...]}`` shaped to match what
        :func:`tools.web_tools.web_crawl_tool` post-processes.
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"results": [{"url": url, "title": "", "content": "", "error": "Interrupted"}]}

            if not _env_bool("TAVILY_ALLOW_EXPENSIVE", False):
                return {
                    "results": [
                        {
                            "url": url,
                            "title": "",
                            "content": "",
                            "error": "Tavily crawl is disabled by TAVILY_ALLOW_EXPENSIVE=0 to preserve the free monthly credit budget.",
                        }
                    ]
                }

            instructions = kwargs.get("instructions")
            depth = kwargs.get("depth", "basic")
            limit = min(int(kwargs.get("limit", 20)), _env_int("TAVILY_MAX_CRAWL_PAGES", 5))

            logger.info("Tavily crawl: %s (depth=%s, limit=%d)", url, depth, limit)
            payload: Dict[str, Any] = {
                "url": url,
                "limit": limit,
                "extract_depth": depth,
            }
            if instructions:
                payload["instructions"] = instructions

            raw = _tavily_request("crawl", payload)
            return {
                "results": _normalize_tavily_documents(raw, fallback_url=url)
            }
        except ValueError as exc:
            return {"results": [{"url": url, "title": "", "content": "", "error": str(exc)}]}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily crawl error: %s", exc)
            return {
                "results": [
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": f"Tavily crawl failed: {exc}",
                    }
                ]
            }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Tavily",
            "badge": "paid",
            "tag": "Search + extract in one provider.",
            "env_vars": [
                {
                    "key": "TAVILY_API_KEY",
                    "prompt": "Tavily API key",
                    "url": "https://app.tavily.com/home",
                },
            ],
        }
