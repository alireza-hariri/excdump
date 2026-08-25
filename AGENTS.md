# Agent Instructions

## Python environment

Prefer running project code as a module with `uv`

```bash
uv run -m <package>.<module>
```

Use the managed environment for all other Python commands too:

```bash
uv run <script.py>
uv run pytest
uv run pytest tests/<test_file>.py -q
uv run ruff check .
```

If `uv` is unavailable, use the venv:

```bash 
.venv/bin/activate/python -m <package>.<module>
.venv/bin/activate/python <script.py>
.venv/bin/activate/pytest
```

To publish a release to PyPI, bump the version, run `uv build`, and publish only the artifacts for that version with `uv publish dist/excdump-<version>.tar.gz dist/excdump-<version>-py3-none-any.whl`. `UV_PUBLISH_TOKEN` is provided by the environment; do not use `uv publish dist/*` because old artifacts in `dist/` may already exist on PyPI.

