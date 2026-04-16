# API Tester — LLM (Ollama + Mistral) → Excel

An automated API testing tool that:

1. **Reads** a Postman Collection v2.1 JSON file
2. **Generates** test case variants (happy path, edge cases, invalid inputs) using Mistral via Ollama
3. **Executes** all HTTP requests and captures status codes, response times, and bodies
4. **Analyses** each response with Mistral (PASS / FAIL / WARN + plain-English reason)
5. **Writes** a formatted `.xlsx` Excel report with a Summary sheet and a Details sheet

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.9+ | |
| [Ollama](https://ollama.com/) installed and running | `ollama serve` |
| Mistral model pulled | `ollama pull mistral` |

---

## Installation

```bash
# 1. Clone / navigate to the project folder
cd api_tester

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Usage

### Basic (no auth)

```bash
python main.py --collection my_api.postman_collection.json
```

### Basic Auth

```bash
python main.py --collection my_api.postman_collection.json \
               --auth-type basic \
               --username admin \
               --password secret
```

### Custom output path + more variants

```bash
python main.py --collection api.json \
               --output reports/run1.xlsx \
               --variants 5
```

### Skip LLM (fast mode — status-code only verdicts)

```bash
python main.py --collection api.json \
               --no-llm-generate \
               --no-llm-analyze
```

---

## CLI Reference

| Flag | Default | Description |
|---|---|---|
| `--collection`, `-c` | *(required)* | Path to Postman Collection JSON |
| `--output`, `-o` | `test_results.xlsx` | Output `.xlsx` file path |
| `--variants`, `-n` | `3` | LLM-generated test variants per endpoint |
| `--auth-type` | `none` | Global auth: `none` or `basic` |
| `--username` | — | Username for Basic Auth |
| `--password` | — | Password for Basic Auth |
| `--no-llm-generate` | off | Skip LLM test generation; use original request only |
| `--no-llm-analyze` | off | Skip LLM analysis; verdict based on status code only |
| `--ollama-url` | `http://localhost:11434` | Ollama base URL |
| `--model` | `mistral` | Ollama model name |

---

## Output — Excel Report

### Sheet 1: Summary

| Section | Contents |
|---|---|
| Run metadata | Collection name, start/finish timestamps |
| Aggregate counts | Total, Passed, Failed, Warned, Errors |
| Per-endpoint table | Breakdown of pass/fail/warn per endpoint |

### Sheet 2: Details

One row per test case variant. Columns:

`Endpoint Name` · `Folder` · `Method` · `URL` · `Test Variant` · `Variant Description` · `Expected Status` · `Actual Status` · `Response Time (ms)` · `LLM Verdict` · `LLM Analysis` · `Request Headers` · `Request Body` · `Response Body` · `Error`

Verdict cells are colour-coded: **green** (PASS) · **red** (FAIL) · **yellow** (WARN).

---

## Project Structure

```
api_tester/
├── main.py                   # CLI entry point — orchestrates the full pipeline
├── parser/
│   └── postman_parser.py     # Parses Postman Collection v2.1 JSON
├── llm/
│   └── ollama_client.py      # Ollama/Mistral: test generation + response analysis
├── runner/
│   └── test_runner.py        # Fires HTTP requests, captures results
├── report/
│   └── excel_writer.py       # Writes formatted .xlsx report
├── requirements.txt
└── README.md
```

---

## Troubleshooting

**`Cannot connect to Ollama at http://localhost:11434`**
→ Make sure Ollama is running: `ollama serve`

**`LLM timed out`**
→ Mistral may still be loading. Wait ~30 s and retry, or use `--no-llm-generate --no-llm-analyze` for a fast run.

**`Unsupported Postman collection schema`**
→ Export your collection from Postman as **Collection v2.1** (File → Export → Collection v2.1).
