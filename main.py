"""
API Tester — entry point.

Pipeline:
  1. Parse Postman collection → flat list of endpoints
  2. For each endpoint, ask Mistral to generate test case variants
  3. Fire all HTTP requests via the test runner
  4. For each result, ask Mistral to analyse the response
  5. Write everything to a formatted Excel report
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from parser.postman_parser import parse_collection
from llm.ollama_client import generate_test_cases, analyze_response
from runner.test_runner import run_test_case
from report.excel_writer import write_report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="api_tester",
        description="LLM-powered API tester: Postman collection → Excel report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # No auth
  python main.py --collection my_api.postman_collection.json

  # Basic auth
  python main.py --collection my_api.postman_collection.json \\
                 --auth-type basic --username admin --password secret

  # Custom output path and 5 LLM variants per endpoint
  python main.py --collection api.json --output reports/run1.xlsx --variants 5
""",
    )
    p.add_argument(
        "--collection", "-c",
        required=True,
        metavar="FILE",
        help="Path to the Postman Collection v2.1 JSON file.",
    )
    p.add_argument(
        "--output", "-o",
        default="test_results.xlsx",
        metavar="FILE",
        help="Output Excel file path (default: test_results.xlsx).",
    )
    p.add_argument(
        "--variants", "-n",
        type=int,
        default=3,
        metavar="N",
        help="Number of LLM-generated test variants per endpoint (default: 3).",
    )
    p.add_argument(
        "--auth-type",
        choices=["none", "basic"],
        default="none",
        help="Global auth type applied to all requests (default: none).",
    )
    p.add_argument(
        "--username",
        default=None,
        help="Username for Basic Auth.",
    )
    p.add_argument(
        "--password",
        default=None,
        help="Password for Basic Auth.",
    )
    p.add_argument(
        "--no-llm-generate",
        action="store_true",
        help="Skip LLM test generation; use only the original request as-is.",
    )
    p.add_argument(
        "--no-llm-analyze",
        action="store_true",
        help="Skip LLM response analysis; verdict is based on status code only.",
    )
    p.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        metavar="URL",
        help="Ollama base URL (default: http://localhost:11434).",
    )
    p.add_argument(
        "--model",
        default="mistral",
        help="Ollama model name to use (default: mistral).",
    )
    return p


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def _print_step(step: str, msg: str) -> None:
    print(f"  [{step}] {msg}", flush=True)


def _progress(current: int, total: int, label: str) -> None:
    bar_len = 30
    filled = int(bar_len * current / total) if total else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  [{bar}] {current}/{total}  {label[:60]:<60}", end="", flush=True)


# ---------------------------------------------------------------------------
# Fallback verdict (no LLM)
# ---------------------------------------------------------------------------

def _simple_verdict(result: dict) -> dict:
    if result.get("error"):
        return {"verdict": "FAIL", "reason": f"Request error: {result['error']}"}
    expected = result.get("expected_status", 200)
    actual = result.get("status_code")
    if actual == expected:
        return {"verdict": "PASS", "reason": f"Status {actual} matches expected {expected}."}
    return {
        "verdict": "FAIL",
        "reason": f"Status {actual} does not match expected {expected}.",
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_parser().parse_args()

    # Patch ollama_client constants if user supplied custom values
    import llm.ollama_client as _oc
    _oc.OLLAMA_BASE_URL = args.ollama_url
    _oc.MODEL = args.model

    print("\n╔══════════════════════════════════════╗")
    print("║   API Tester — LLM + Excel Report    ║")
    print("╚══════════════════════════════════════╝\n")

    # ------------------------------------------------------------------
    # Step 1: Parse Postman collection
    # ------------------------------------------------------------------
    print("Step 1/4  Parsing Postman collection …")
    try:
        endpoints = parse_collection(args.collection)
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    collection_name = Path(args.collection).stem
    print(f"          Found {len(endpoints)} endpoint(s) in '{collection_name}'.\n")

    if not endpoints:
        print("No endpoints found in the collection. Exiting.")
        return 0

    # ------------------------------------------------------------------
    # Step 2: LLM — generate test case variants
    # ------------------------------------------------------------------
    print("Step 2/4  Generating test cases with Mistral …")
    variants_map: dict[int, list[dict]] = {}

    for idx, ep in enumerate(endpoints):
        label = ep.get("name", ep.get("url", ""))
        _print_step("GEN", label)

        if args.no_llm_generate:
            # Single variant: use the original request unchanged
            variants_map[idx] = [
                {
                    "variant_name": "Original",
                    "description": "Original request from Postman collection.",
                    "headers_override": {},
                    "body_override": "",
                    "expected_status": 200,
                }
            ]
        else:
            try:
                variants_map[idx] = generate_test_cases(ep, num_variants=args.variants)
            except (ConnectionError, TimeoutError) as exc:
                print(f"\n  WARNING: LLM unavailable — {exc}")
                print("  Falling back to original request only.\n")
                variants_map[idx] = [
                    {
                        "variant_name": "Original (LLM unavailable)",
                        "description": "Original request; LLM generation failed.",
                        "headers_override": {},
                        "body_override": "",
                        "expected_status": 200,
                    }
                ]

    total_variants = sum(len(v) for v in variants_map.values())
    print(f"\n          Generated {total_variants} test case(s) across {len(endpoints)} endpoint(s).\n")

    # ------------------------------------------------------------------
    # Step 3: Execute HTTP requests
    # ------------------------------------------------------------------
    print("Step 3/4  Running HTTP requests …")
    global_auth = args.auth_type if args.auth_type != "none" else None

    raw_results: list[dict] = []
    current = 0
    total = total_variants

    for idx, ep in enumerate(endpoints):
        for variant in variants_map.get(idx, []):
            current += 1
            _progress(current, total, f"{ep.get('name', '')} [{variant.get('variant_name', '')}]")

            result = run_test_case(
                endpoint=ep,
                variant=variant,
                global_auth_type=global_auth,
                global_username=args.username,
                global_password=args.password,
            )
            raw_results.append(result)

    print()  # newline after progress bar
    print(f"\n          Completed {len(raw_results)} request(s).\n")

    # ------------------------------------------------------------------
    # Step 4: LLM — analyse responses
    # ------------------------------------------------------------------
    started_at = datetime.datetime.now()
    print("Step 4/4  Analysing responses with Mistral …")

    final_results: list[dict] = []
    for i, res in enumerate(raw_results):
        ep_idx = next(
            (
                idx
                for idx, ep in enumerate(endpoints)
                if ep.get("name") == res.get("endpoint_name")
                   and ep.get("url") == res.get("url")
            ),
            0,
        )
        ep = endpoints[ep_idx]
        variant = {
            "variant_name": res.get("variant_name", ""),
            "description": res.get("variant_description", ""),
            "expected_status": res.get("expected_status", 200),
        }

        _print_step(
            "ANALYSE",
            f"{res.get('endpoint_name', '')} [{res.get('variant_name', '')}]",
        )

        if args.no_llm_analyze:
            analysis = _simple_verdict(res)
        else:
            try:
                analysis = analyze_response(
                    endpoint=ep,
                    variant=variant,
                    status_code=res.get("status_code") or 0,
                    response_body=res.get("response_body", ""),
                    response_time_ms=res.get("response_time_ms", 0.0),
                    error=res.get("error"),
                )
            except (ConnectionError, TimeoutError) as exc:
                print(f"\n  WARNING: LLM analysis failed — {exc}")
                analysis = _simple_verdict(res)

        final_results.append({**res, "analysis": analysis})

    finished_at = datetime.datetime.now()

    # ------------------------------------------------------------------
    # Write Excel report
    # ------------------------------------------------------------------
    print(f"\nWriting Excel report → {args.output} …")
    out_path = write_report(
        results=final_results,
        output_path=args.output,
        collection_name=collection_name,
        started_at=started_at,
        finished_at=finished_at,
    )

    # Print summary
    passed = sum(1 for r in final_results if (r.get("analysis") or {}).get("verdict", "").upper() == "PASS")
    failed = sum(1 for r in final_results if (r.get("analysis") or {}).get("verdict", "").upper() == "FAIL")
    warned = sum(1 for r in final_results if (r.get("analysis") or {}).get("verdict", "").upper() == "WARN")

    print(f"""
┌─────────────────────────────────────┐
│  Run complete                        │
│  Total   : {len(final_results):<5}                       │
│  Passed  : {passed:<5}                       │
│  Failed  : {failed:<5}                       │
│  Warned  : {warned:<5}                       │
│  Report  : {str(out_path)[:36]:<36} │
└─────────────────────────────────────┘
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
