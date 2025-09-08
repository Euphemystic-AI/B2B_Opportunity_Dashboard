# B2B Opportunity Dashboard

A small, focused pipeline that enriches company directory records with AI-generated insights and indexes the merged data into OpenSearch.

The main script, `5company_lookup.py`, reads a directory JSON, renders a structured prompt (from `Prompt01.txt`), calls the OpenAI Chat Completions API for a strict JSON response, merges it with the original company facts, enforces AFI metrics normalization, and streams results to an OpenSearch index via the bulk API.

## Features
- Prompt-driven enrichment with strict JSON output
- Schema normalization for multiple directory sources
- AFI score rounding/banding guarantees
- OpenSearch bulk indexing (NDJSON)
- Lightweight logging to a local `logs/` directory

## Requirements
- Python 3.9+
- Network access to OpenAI and your OpenSearch cluster

## Installation
- Create and activate a virtual environment, then install dependencies:

```
python -m venv .venv
# Windows PowerShell
. .venv/Scripts/Activate.ps1
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Configuration
Set the following environment variables (copy `.env.example` and update values as needed):

- `OPENAI_API_KEY`: OpenAI API key (required)
- `OPENAI_MODEL`: Optional model name (default: `gpt-4o`)
- `OS_URL`: OpenSearch bulk endpoint, e.g. `https://host:9200/index/_bulk` (required)
- `OS_USERNAME`: OpenSearch username (required)
- `OS_PASSWORD`: OpenSearch password (required)
- `OS_CA_CERT`: Path to CA certificate file (optional but recommended)
- `MEMBER_JSON_PATH`: Input JSON path (default: `/apps/chamber/member_index.json`)
- `PROMPT01_PATH`: Prompt template path (default: `./Prompt01.txt`)
- `LOG_DIR`: Directory for logs (default: `./logs` if the configured path is not writable)

See code for exact behavior and defaults:
- `5company_lookup.py:23`

## Input Data
This repo includes two example inputs:
- `member_index.json`: Flat structure with keys like `company_name`, `primary_address`, `website_url`, `social_links`, `about_html`.
- `boulder_chamber_directory_compiled.json`: Uses different keys and a nested `address` object.

The script normalizes either schema automatically. Key mappings include:
- `name` → `company_name`
- `address{street,city,region,postal_code}` → `primary_address` (flattened string)
- `detail_url` → `source_url`
- Fallbacks for `website_url`, `social_links`, and `about_html`

## Running
- With the default `member_index.json`:

```
# PowerShell
$env:OPENAI_API_KEY="sk-..."
$env:OS_URL="https://host:9200/my-index/_bulk"
$env:OS_USERNAME="opensearch-user"
$env:OS_PASSWORD="opensearch-pass"
python 5company_lookup.py
```

- With the Boulder directory file:

```
# PowerShell
$env:MEMBER_JSON_PATH="./boulder_chamber_directory_compiled.json"
python 5company_lookup.py
```

The script writes logs to `./logs` by default and flushes to OpenSearch in modest bulk batches.

## OpenSearch Notes
- Provide the full `_bulk` endpoint in `OS_URL` (including index name).
- The payload is NDJSON: alternating action and document lines with a trailing newline.
- TLS verification is enabled by default; provide `OS_CA_CERT` to trust a custom CA.

## Troubleshooting
- Missing env vars: the script exits with a clear error (required variables are enforced).
- JSON decoding errors from the model: the document is still indexed with a `validation_warning`.
- AFI fields: scores are rounded to one decimal and bands are coerced to `High|Mid|Low`.

## Development
- Main script: `5company_lookup.py`
- Prompt template: `Prompt01.txt`
- Example inputs: `member_index.json`, `boulder_chamber_directory_compiled.json`

Contributions welcome. See `CONTRIBUTING.md`.

## License
This repository currently has no license specified. If you plan to open source it, add a `LICENSE` file (e.g., MIT/Apache-2.0). For private/internal use, coordinate with your org’s policy.

