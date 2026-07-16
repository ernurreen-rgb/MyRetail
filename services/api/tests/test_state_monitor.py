from decimal import Decimal

import pytest

from myretail_api.state.monitor import (
    RecoveryAgeExceededError,
    RecoveryHealth,
    evaluate_recovery_health,
)


def test_recovery_monitor_accepts_empty_and_fresh_queues() -> None:
    assert evaluate_recovery_health(
        pending_count=0,
        oldest_age_seconds=None,
        maximum_age=900,
    ) == RecoveryHealth(pending_count=0, oldest_age_seconds=0)
    assert evaluate_recovery_health(
        pending_count=2,
        oldest_age_seconds=Decimal("899.9"),
        maximum_age=900,
    ) == RecoveryHealth(pending_count=2, oldest_age_seconds=899)


def test_recovery_monitor_fails_closed_for_stale_work() -> None:
    with pytest.raises(RecoveryAgeExceededError, match="recovery age threshold"):
        evaluate_recovery_health(
            pending_count=1,
            oldest_age_seconds=901,
            maximum_age=900,
        )


def test_recovery_monitor_rejects_invalid_counts() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        evaluate_recovery_health(
            pending_count=-1,
            oldest_age_seconds=0,
            maximum_age=900,
        )
