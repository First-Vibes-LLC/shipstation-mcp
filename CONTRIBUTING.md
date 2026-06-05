# Contributing

Thanks for your interest in contributing! Here's everything you need to get started.

## Getting started

1. Fork the repository and clone your fork.
2. Install dependencies:
   ```bash
   uv sync
   pre-commit install
   ```
3. Copy `env.example` to `.env` and add your ShipStation credentials.

## Running the server locally

```bash
uv run python shipstation_mcp_server.py
```

The server starts on `http://localhost:8000` by default.

## Code style

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
uv run ruff check .        # lint
uv run ruff format .       # format
uv run ruff format --check . # CI check (no writes)
```

## Submitting a pull request

1. Open an issue first for non-trivial changes so we can align on approach.
2. Create a branch from `main`: `git checkout -b your-feature`.
3. Make your changes and make sure the server starts cleanly.
4. Open a PR against `main`.

## Reporting bugs

Please include:
- Steps to reproduce
- The full traceback
- Your Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
