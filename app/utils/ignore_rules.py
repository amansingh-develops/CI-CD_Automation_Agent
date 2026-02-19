"""
Ignore Rules
============
Rules for ignoring generated files, dependencies, and non-source artifacts.

Ignored patterns:
    - node_modules/
    - __pycache__/
    - .git/
    - .venv/ / venv/
    - dist/ / build/
    - *.pyc, *.pyo
    - coverage reports
    - lock files (package-lock.json, poetry.lock, etc.)

These rules prevent the parser and fix agent from operating
on generated or third-party code that should never be modified.
"""
