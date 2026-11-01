"""续费投影的跨语言案例与坏输入隔离，不依赖数据库。"""
import datetime
import json
from pathlib import Path

import pytest

import renewals

CASES = json.loads(
    (Path(__file__).parents[1] / "testdata" / "renewal-projection.json").read_text()
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_projection(case):
    assert renewals.project_expiry(
        case["row"], datetime.date.fromisoformat(case["today"]),
    ) == case["expected"]


@pytest.mark.parametrize("row", [None, [], "bad", 42])
def test_projection_handles_non_mapping(row):
    assert renewals.project_expiry(row) is None
