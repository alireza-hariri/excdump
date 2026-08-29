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
uv run pytest tests/<test_file>.py
uv run ruff check .
```

If `uv` is unavailable, use the venv:

```bash 
.venv/bin/python -m <package>.<module>
.venv/bin/python <script.py>
.venv/bin/pytest
```

To publish a release to PyPI:
1. bump the version
2. run `uv build`
3. publish the artifacts `uv publish dist/excdump-<version>.tar.gz dist/excdump-<version>-py3-none-any.whl`.