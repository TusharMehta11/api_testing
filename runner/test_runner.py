"""
Execute HTTP requests for each test case variant and capture full results.
Supports: no auth and Basic Auth.
"""

from __future__ import annotations

import time
from base64 import b64encode
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

REQUEST_TIMEOUT = 30  # seconds per HTTP call


def _build_auth(
    endpoint_auth: dict,
    global_auth_type: str | None = None,
    global_username: str | None = None,
    global_password: str | None = None,
) -> HTTPBasicAuth | None:
    """
    Resolve auth to use for a request.
    Priority: endpoint-level auth → CLI global auth → none.
    """
    auth_type = endpoint_auth.get("type", "noauth")

    # CLI-supplied global basic auth overrides everything
    if global_auth_type == "basic" and global_username:
        return HTTPBasicAuth(global_username, global_password or "")

    if auth_type == "basic":
        return HTTPBasicAuth(
            endpoint_auth.get("username", ""),
            endpoint_auth.get("password", ""),
        )

    return None  # no auth / bearer handled via headers


def _inject_bearer(headers: dict, endpoint_auth: dict) -> dict:
    """Add Authorization header for bearer token auth if present."""
    if endpoint_auth.get("type") == "bearer":
        token = endpoint_auth.get("token", "")
        if token:
            headers = {**headers, "Authorization": f"Bearer {token}"}
    elif endpoint_auth.get("type") == "apikey" and endpoint_auth.get("in") == "header":
        key = endpoint_auth.get("key", "Authorization")
        value = endpoint_auth.get("value", "")
        headers = {**headers, key: value}
    return headers


def run_test_case(
    endpoint: dict,
    variant: dict,
    global_auth_type: str | None = None,
    global_username: str | None = None,
    global_password: str | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> dict:
    """
    Fire a single HTTP request for the given endpoint + variant combo.

    Parameters
    ----------
    endpoint : dict
        Parsed endpoint from postman_parser.
    variant : dict
        A test case variant from ollama_client.generate_test_cases().
    global_auth_type : str | None
        Auth type supplied at CLI level ("basic" or None).
    global_username : str | None
        Username for global basic auth.
    global_password : str | None
        Password for global basic auth.
    timeout : int
        Per-request timeout in seconds.

    Returns
    -------
    dict
        {
            endpoint_name: str,
            folder_path: str,
            method: str,
            url: str,
            variant_name: str,
            variant_description: str,
            expected_status: int,
            request_headers: dict,
            request_body: str,
            status_code: int | None,
            response_time_ms: float,
            response_headers: dict,
            response_body: str,
            error: str | None,
        }
    """
    method = endpoint.get("method", "GET").upper()
    url = endpoint.get("url", "")
    endpoint_auth = endpoint.get("auth", {"type": "noauth"})

    # Merge headers: base → variant override
    headers: dict[str, str] = {**endpoint.get("headers", {})}
    headers_override = variant.get("headers_override") or {}
    headers.update(headers_override)
    headers = _inject_bearer(headers, endpoint_auth)

    # Body: variant override takes precedence; fall back to original
    body = variant.get("body_override") or endpoint.get("body") or None

    auth = _build_auth(
        endpoint_auth,
        global_auth_type=global_auth_type,
        global_username=global_username,
        global_password=global_password,
    )

    result: dict[str, Any] = {
        "endpoint_name": endpoint.get("name", url),
        "folder_path": endpoint.get("folder_path", ""),
        "method": method,
        "url": url,
        "variant_name": variant.get("variant_name", ""),
        "variant_description": variant.get("description", ""),
        "expected_status": variant.get("expected_status", 200),
        "request_headers": headers,
        "request_body": body or "",
        "status_code": None,
        # Latency breakdown
        "request_time_ms": 0.0,    # wall-clock time from send → headers received (TTFB)
        "response_time_ms": 0.0,   # time to download response body after headers
        "total_time_ms": 0.0,      # request_time_ms + response_time_ms
        "response_headers": {},
        "response_body": "",
        "error": None,
    }

    wall_start = time.perf_counter()
    try:
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            data=body.encode() if body else None,
            auth=auth,
            timeout=timeout,
            allow_redirects=True,
            stream=True,          # stream=True lets us time body read separately
        )

        # TTFB: requests.elapsed = server processing + network to first response byte
        ttfb_ms = round(resp.elapsed.total_seconds() * 1000, 2)

        # Body download: read content and measure wall time
        body_start = time.perf_counter()
        try:
            raw_content = resp.content          # download full body
            body_ms = round((time.perf_counter() - body_start) * 1000, 2)
            response_body = raw_content.decode(resp.encoding or "utf-8", errors="replace")
        except Exception:
            body_ms = round((time.perf_counter() - body_start) * 1000, 2)
            response_body = "<binary or undecodable response>"

        result["status_code"] = resp.status_code
        result["request_time_ms"] = ttfb_ms
        result["response_time_ms"] = body_ms
        result["total_time_ms"] = round(ttfb_ms + body_ms, 2)
        result["response_headers"] = dict(resp.headers)
        result["response_body"] = response_body

    except requests.exceptions.ConnectionError as exc:
        result["error"] = f"Connection error: {exc}"
        result["total_time_ms"] = round((time.perf_counter() - wall_start) * 1000, 2)
    except requests.exceptions.Timeout:
        result["error"] = f"Request timed out after {timeout}s"
        result["total_time_ms"] = round((time.perf_counter() - wall_start) * 1000, 2)
    except requests.exceptions.RequestException as exc:
        result["error"] = str(exc)
        result["total_time_ms"] = round((time.perf_counter() - wall_start) * 1000, 2)

    return result


def run_all(
    endpoints: list[dict],
    variants_map: dict[int, list[dict]],
    global_auth_type: str | None = None,
    global_username: str | None = None,
    global_password: str | None = None,
    progress_callback=None,
) -> list[dict]:
    """
    Run all test case variants for every endpoint.

    Parameters
    ----------
    endpoints : list[dict]
        Flat list from postman_parser.parse_collection().
    variants_map : dict[int, list[dict]]
        Mapping of endpoint index → list of variants from ollama_client.
    progress_callback : callable | None
        Optional fn(current: int, total: int, label: str) called before each request.

    Returns
    -------
    list[dict]
        Flat list of raw result dicts (one per test case fired).
    """
    results: list[dict] = []
    total = sum(len(v) for v in variants_map.values())
    current = 0

    for idx, endpoint in enumerate(endpoints):
        variants = variants_map.get(idx, [])
        for variant in variants:
            current += 1
            label = f"{endpoint.get('name', endpoint.get('url', ''))} [{variant.get('variant_name', '')}]"
            if progress_callback:
                progress_callback(current, total, label)

            result = run_test_case(
                endpoint=endpoint,
                variant=variant,
                global_auth_type=global_auth_type,
                global_username=global_username,
                global_password=global_password,
            )
            results.append(result)

    return results
