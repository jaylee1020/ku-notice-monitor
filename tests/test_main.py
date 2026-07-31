"""스마트 알림 스케줄 테스트."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from main import _digest_is_due, _flush_digest_if_due
from state import enqueue_digest


def _config(hour=21):
    return {"notifications": {"digest_hour_kst": hour}}


def test_digest_is_due_at_configured_kst_hour():
    now = datetime(2026, 8, 1, 21, 15, tzinfo=ZoneInfo("Asia/Seoul"))
    assert _digest_is_due(_config(), now) is True


def test_digest_is_not_due_at_other_hour():
    now = datetime(2026, 8, 1, 20, 59, tzinfo=ZoneInfo("Asia/Seoul"))
    assert _digest_is_due(_config(), now) is False


def test_flush_digest_sends_and_clears(make_classified):
    state = {"pending_digest": []}
    enqueue_digest([make_classified()], state)
    with (
        patch("main._digest_is_due", return_value=True),
        patch("main.notify_digest", new_callable=AsyncMock) as notify,
    ):
        count = asyncio.run(_flush_digest_if_due(state, _config()))
    assert count == 1
    notify.assert_awaited_once()
    assert state["pending_digest"] == []
