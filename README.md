# ACP Proxy

Bridges OpenCode to GitHub Copilot by exposing the `copilot-language-server` ACP interface as an OpenAI-compatible HTTP endpoint.

```
OpenCode  →  ACP Proxy (localhost:8765)  →  copilot-language-server  →  Copilot backend
```

## Prerequisites

- Python 3.11+
- `copilot-language-server` binary (bundled with the JetBrains GitHub Copilot plugin)
- GitHub Copilot signed in (cached token at `~/.config/github-copilot/`)

## Install

```bash
git clone https://github.com/jonathanmiddleton/acp_test.git
cd acp_test
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

### 1. Start the proxy

```bash
python -m acp_proxy --port 8765
```

The proxy auto-discovers the `copilot-language-server` binary from running processes or JetBrains plugin directories. To specify the path explicitly:

```bash
python -m acp_proxy --port 8765 --binary /path/to/copilot-language-server
```

### 2. Configure OpenCode

Copy the provided `opencode.json` to your project root, or to `~/.config/opencode/opencode.json` for global use. It configures a `copilot` provider pointing at the proxy.

### 3. Start OpenCode

```bash
opencode
```

Available models will appear as `copilot/gpt-4.1`, `copilot/gpt-4o`, `copilot/auto`.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Integration tests require the `copilot-language-server` binary to be available and are skipped automatically if it cannot be found.

## Options

| Flag          | Default           | Description                        |
|---------------|-------------------|------------------------------------|
| `--binary`    | auto-discovered   | Path to `copilot-language-server`  |
| `--port`      | 8765              | Port for the HTTP server           |
| `--cwd`       | current directory | Working directory for ACP sessions |
| `--log-level` | INFO              | DEBUG, INFO, WARNING, ERROR        |
