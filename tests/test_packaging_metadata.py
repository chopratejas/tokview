from __future__ import annotations

import tomllib
from pathlib import Path

from tokview import __version__


def test_python_requires_excludes_unsupported_litellm_proxy_stack():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    classifiers = set(pyproject["project"]["classifiers"])

    assert pyproject["project"]["requires-python"] == ">=3.11,<3.14"
    assert "Programming Language :: Python :: 3.13" in classifiers
    assert "Programming Language :: Python :: 3.14" not in classifiers


def test_package_metadata_version_matches_runtime_version():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert __version__ == pyproject["project"]["version"]
