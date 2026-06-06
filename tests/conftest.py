"""Test fixtures."""
import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir():
    d = Path(tempfile.mkdtemp(prefix="agentchat-test-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)
