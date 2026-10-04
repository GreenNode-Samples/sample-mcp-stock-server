# Contributing

Thanks for your interest! This is a sample repo that shows the **pure MCP server** pattern on
GreenNode AgentBase (runtime → MCP Connector → MCP Gateway).

## How to contribute

1. Fork → create a branch: `git checkout -b feat/short-name`
2. Run the checks before you commit:
   ```bash
   pip install -r requirements-dev.txt
   python -m pytest -v
   ruff check --select F,E9,B,UP,SIM --target-version py312 src tests
   ```
   The tests are **hermetic** (no network calls): please keep it that way. The 24hMoney API is faked with
   `httpx.MockTransport` (see `tests/conftest.py`), and a guard fails any test that forgets to fake it.
3. Commit with a convention prefix: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`
4. Open a Pull Request with a short description

## Code conventions

- Python 3.12 (3.11 is the minimum), no separate formatter - keep the current style (ruff-clean)
- A new tool: a clear English docstring (the agent reads it to decide when to call the tool), a `Field(description=...)`
  on every parameter, `annotations=READ_ONLY`, a dict as the return value and `raise ToolError("...")` for failures
- Treat upstream data as untrusted: read numbers with `_num`, strings with `_text`, validate the shape in a `parse` function
  passed to `fetch_api` (so a bad payload is never cached) and keep missing values `null`
- Code, comments, docstrings, log messages and docs are written in English
- NEVER commit a secret (`.env` is gitignored); example keys must be placeholders the server rejects (`change-me-...`)
- Data comes only from the public 24hMoney API - do not add a source that needs an API key

## About the data

Source: the public (unofficial) API of the 24hMoney app. For demo / sample use only - not for real trading.
