import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
