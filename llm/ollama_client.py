"""
Ollama + Mistral client for:
  1. Generating test case variants for an endpoint
  2. Analyzing an HTTP response and returning a structured verdict
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

import requests

OLLAMA_BASE_URL = "http://localhost:11434"
MODEL = "mistral"
REQUEST_TIMEOUT = 480  # seconds


# ---------------------------------------------------------------------------
# Low-level Ollama call
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str, temperature: float = 0.3) -> str:
    """
    Call the Ollama /api/generate endpoint and return the full response text.
    Uses non-streaming mode for simplicity.
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "")
    except requests.exceptions.ConnectionError:
        raise ConnectionError(
            f"Cannot connect to Ollama at {OLLAMA_BASE_URL}. "
            "Make sure Ollama is running (`ollama serve`)."
        )
    except requests.exceptions.Timeout:
        raise TimeoutError(
            f"Ollama request timed out after {REQUEST_TIMEOUT}s. "
            "The model may be loading — retry in a moment."
        )


def _extract_json(text: str) -> Any:
    """
    Extract the first JSON object or array from an LLM text response.
    Falls back to parsing the full text if no fenced block is found.
    """
    # Try fenced code block first
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        candidate = fence_match.group(1).strip()
    else:
        # Find the first '[' or '{'
        start = min(
            (text.find(c) for c in ("{", "[") if c in text),
            default=-1,
        )
        candidate = text[start:] if start >= 0 else text

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Last-resort: try stripping trailing garbage after last } or ]
        for end_char, open_char in (("}", "{"), ("]", "[")):
            last = candidate.rfind(end_char)
            if last >= 0:
                first = candidate.find(open_char)
                if first >= 0:
                    try:
                        return json.loads(candidate[first : last + 1])
                    except json.JSONDecodeError:
                        pass
        raise ValueError(f"Could not extract valid JSON from LLM response:\n{text[:500]}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_test_cases(endpoint: dict, num_variants: int = 3) -> list[dict]:
    """
    Ask Mistral to generate test case variants for the given endpoint.

    Parameters
    ----------
    endpoint : dict
        A single endpoint dict from postman_parser (name, method, url, headers, body).
    num_variants : int
        Number of test variants to request from the LLM.

    Returns
    -------
    list[dict]
        Each item: {
            variant_name: str,
            description: str,
            headers_override: dict,   # merged on top of original headers
            body_override: str,       # replaces original body; empty = use original
            expected_status: int,
        }
    """
    prompt = textwrap.dedent(f"""
        You are an API testing expert. Given the following API endpoint details,
        generate exactly {num_variants} distinct test case variants.

        Endpoint:
        - Name: {endpoint.get("name", "")}
        - Method: {endpoint.get("method", "GET")}
        - URL: {endpoint.get("url", "")}
        - Headers: {json.dumps(endpoint.get("headers", {}))}
        - Body: {endpoint.get("body", "") or "(empty)"}
        - Description: {endpoint.get("description", "") or "(none)"}

        Rules:
        1. Include a happy-path test, at least one edge case, and one negative/invalid test.
        2. For negative tests, use an appropriate expected HTTP status (e.g. 400, 401, 404, 422).
        3. Keep body_override as a valid JSON string when the original body is JSON, or empty to reuse original.
        4. Return ONLY a JSON array with no extra text. Each element must have these keys:
           - variant_name (string)
           - description (string, one sentence)
           - headers_override (object, can be empty {{}})
           - body_override (string, can be empty "")
           - expected_status (integer)

        JSON array:
    """).strip()

    raw = _call_ollama(prompt, temperature=0.4)
    try:
        variants = _extract_json(raw)
        if not isinstance(variants, list):
            variants = [variants]
        # Normalise keys
        normalised = []
        for v in variants:
            normalised.append(
                {
                    "variant_name": str(v.get("variant_name", "Variant")),
                    "description": str(v.get("description", "")),
                    "headers_override": v.get("headers_override") or {},
                    "body_override": str(v.get("body_override") or ""),
                    "expected_status": int(v.get("expected_status", 200)),
                }
            )
        return normalised
    except (ValueError, KeyError, TypeError) as exc:
        # Graceful fallback: single happy-path variant
        return [
            {
                "variant_name": "Happy Path",
                "description": f"Default test for {endpoint.get('name', endpoint.get('url', ''))}",
                "headers_override": {},
                "body_override": "",
                "expected_status": 200,
                "_llm_parse_error": str(exc),
            }
        ]


def analyze_response(
    endpoint: dict,
    variant: dict,
    status_code: int,
    response_body: str,
    response_time_ms: float,
    error: str | None = None,
) -> dict:
    """
    Ask Mistral to analyse an HTTP response and return a structured verdict.

    Returns
    -------
    dict
        {
            verdict: "PASS" | "FAIL" | "WARN",
            reason: str,   # plain-English explanation
        }
    """
    if error:
        return {
            "verdict": "FAIL",
            "reason": f"Request error: {error}",
        }

    expected = variant.get("expected_status", 200)
    status_match = status_code == expected

    prompt = textwrap.dedent(f"""
        You are an API testing expert. Analyse the following HTTP response and decide
        if the test PASSED, FAILED, or has a WARNING.

        Request details:
        - Endpoint name: {endpoint.get("name", "")}
        - Method: {endpoint.get("method", "GET")}
        - URL: {endpoint.get("url", "")}
        - Test variant: {variant.get("variant_name", "")} — {variant.get("description", "")}
        - Request body sent: {variant.get("body_override") or endpoint.get("body", "") or "(empty)"}
        - Expected HTTP status: {expected}

        Actual response:
        - HTTP status code: {status_code}
        - Response time: {response_time_ms:.1f} ms
        - Response body (first 1000 chars): {response_body[:1000]}

        Status match: {"YES" if status_match else "NO"}

        Rules:
        - PASS: status matches expectation and the response body looks correct for the request.
        - FAIL: status does not match expectation, or body contains clear error/unexpected content.
        - WARN: status matches but body has anomalies, unexpected fields, or performance concern
                (e.g. response time > 3000 ms).

        Return ONLY a JSON object with keys:
        - verdict (string: "PASS", "FAIL", or "WARN")
        - reason (string: one or two sentences explaining the verdict)

        JSON:
    """).strip()

    raw = _call_ollama(prompt, temperature=0.1)
    try:
        result = _extract_json(raw)
        verdict = str(result.get("verdict", "FAIL")).upper()
        if verdict not in ("PASS", "FAIL", "WARN"):
            verdict = "FAIL"
        return {
            "verdict": verdict,
            "reason": str(result.get("reason", raw[:300])),
        }
    except (ValueError, KeyError, TypeError):
        # Fallback to simple status-code check
        verdict = "PASS" if status_match else "FAIL"
        reason = (
            f"Status {status_code} {'matches' if status_match else 'does not match'} "
            f"expected {expected}. (LLM analysis unavailable.)"
        )
        return {"verdict": verdict, "reason": reason}
