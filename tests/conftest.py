"""
Suite-wide pytest configuration.

The ``gpu`` marker and the ``--run-gpu`` option (Batch 11 Stage 5, ruling Q8).
A test marked ``gpu`` loads a real model onto the GPU. It is skipped unless
``--run-gpu`` is given, so the default ``python -m pytest`` stays GPU-free and
fast in every environment, including one where CUDA is available.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-gpu",
        action="store_true",
        default=False,
        help="run the tests marked `gpu` (they load a real model onto the GPU)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "gpu: loads a real model onto the GPU; skipped unless --run-gpu is given"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-gpu"):
        return
    skip = pytest.mark.skip(reason="GPU test: pass --run-gpu to run it")
    for item in items:
        if "gpu" in item.keywords:
            item.add_marker(skip)
