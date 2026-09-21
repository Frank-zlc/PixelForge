"""Device lease exclusivity, expiry and fencing.

The fencing tests are the important ones. Exclusivity alone is easy; the real
failure mode is a *stale* operation landing on a device someone else now owns,
which is what ``epoch`` and :meth:`LeaseManager.guard` exist to prevent.
"""

from __future__ import annotations

import pytest

from pixelforge.device.lease import (
    DeviceBusyError,
    LeaseError,
    LeaseExpiredError,
    LeaseManager,
    LeaseSupersededError,
)


class FakeClock:
    """Controllable monotonic clock, so TTL tests need no sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def leases(clock: FakeClock) -> LeaseManager:
    return LeaseManager(clock=clock)


class TestAcquire:
    def test_first_acquire_succeeds(self, leases: LeaseManager) -> None:
        lease = leases.acquire("ABC", owner="alice", ttl_s=30)
        assert lease.device_id == "ABC"
        assert lease.owner == "alice"
        assert lease.epoch == 1
        assert lease.token

    def test_second_owner_is_rejected(self, leases: LeaseManager) -> None:
        leases.acquire("ABC", owner="alice")
        with pytest.raises(DeviceBusyError) as exc:
            leases.acquire("ABC", owner="bob")
        assert exc.value.owner == "alice"

    def test_different_devices_do_not_conflict(self, leases: LeaseManager) -> None:
        leases.acquire("ABC", owner="alice")
        leases.acquire("DEF", owner="bob")  # must not raise

    def test_force_takes_over(self, leases: LeaseManager) -> None:
        first = leases.acquire("ABC", owner="alice")
        second = leases.acquire("ABC", owner="bob", force=True)
        assert second.epoch > first.epoch
        assert leases.current("ABC") is not None
        assert leases.current("ABC").owner == "bob"  # type: ignore[union-attr]

    def test_same_owner_reacquire_gets_new_epoch(self, leases: LeaseManager) -> None:
        # A browser reload re-acquires. The old epoch must be fenced off so a
        # half-finished operation from the previous page cannot land.
        first = leases.acquire("ABC", owner="alice")
        second = leases.acquire("ABC", owner="alice")
        assert second.epoch > first.epoch
        assert second.token != first.token

    @pytest.mark.parametrize("ttl", [0, -1, 3601])
    def test_rejects_bad_ttl(self, leases: LeaseManager, ttl: float) -> None:
        with pytest.raises(ValueError):
            leases.acquire("ABC", owner="alice", ttl_s=ttl)

    def test_requires_owner(self, leases: LeaseManager) -> None:
        with pytest.raises(ValueError):
            leases.acquire("ABC", owner="")


class TestExpiry:
    def test_expires_after_ttl(self, leases: LeaseManager, clock: FakeClock) -> None:
        leases.acquire("ABC", owner="alice", ttl_s=30)
        clock.advance(29)
        assert leases.current("ABC") is not None
        clock.advance(2)
        assert leases.current("ABC") is None

    def test_expired_device_can_be_claimed_by_someone_else(
        self, leases: LeaseManager, clock: FakeClock
    ) -> None:
        leases.acquire("ABC", owner="alice", ttl_s=10)
        clock.advance(11)
        lease = leases.acquire("ABC", owner="bob")  # no force needed
        assert lease.owner == "bob"

    def test_renew_extends(self, leases: LeaseManager, clock: FakeClock) -> None:
        lease = leases.acquire("ABC", owner="alice", ttl_s=30)
        clock.advance(25)
        renewed = leases.renew(lease.token)
        assert renewed.epoch == lease.epoch  # renewal is not a takeover
        clock.advance(25)
        assert leases.current("ABC") is not None

    def test_renew_after_expiry_fails(self, leases: LeaseManager, clock: FakeClock) -> None:
        lease = leases.acquire("ABC", owner="alice", ttl_s=10)
        clock.advance(11)
        with pytest.raises(LeaseExpiredError):
            leases.renew(lease.token)

    def test_renew_unknown_token(self, leases: LeaseManager) -> None:
        with pytest.raises(LeaseError, match="unknown lease token"):
            leases.renew("deadbeef")

    @pytest.mark.parametrize("peek_first", [True, False])
    def test_dead_token_error_does_not_depend_on_reaping(self, peek_first: bool) -> None:
        """Regression: the failure for a dead token must be deterministic.

        ``current()`` reaps expired leases as a side effect, so the answer to a
        heartbeat used to depend on whether some *unrelated* request had touched
        the device first -- the client saw 410 or 404 for the identical
        situation and could not tell a timeout from a client bug.
        """
        clock = FakeClock()
        leases = LeaseManager(clock=clock)
        lease = leases.acquire("ABC", owner="alice", ttl_s=10)
        clock.advance(11)
        if peek_first:
            leases.current("ABC")  # unrelated request reaps it

        with pytest.raises(LeaseExpiredError):
            leases.renew(lease.token)

    def test_takeover_reports_superseded_not_expired(self, leases: LeaseManager) -> None:
        # Both end in "re-acquire", but the message the user should see differs:
        # "someone took the device" vs "your session timed out".
        stale = leases.acquire("ABC", owner="alice")
        leases.acquire("ABC", owner="bob", force=True)
        with pytest.raises(LeaseSupersededError):
            leases.renew(stale.token)

    def test_never_issued_token_is_plainly_unknown(self, leases: LeaseManager) -> None:
        with pytest.raises(LeaseError) as exc:
            leases.renew("never-existed")
        assert not isinstance(exc.value, LeaseExpiredError | LeaseSupersededError)

    def test_retired_token_memory_is_bounded(self, leases: LeaseManager) -> None:
        for index in range(600):
            leases.acquire(f"dev{index}", owner="alice")
            leases.acquire(f"dev{index}", owner="bob", force=True)
        assert len(leases._retired) <= 256

    def test_sweep_returns_expired(self, leases: LeaseManager, clock: FakeClock) -> None:
        leases.acquire("ABC", owner="alice", ttl_s=10)
        leases.acquire("DEF", owner="bob", ttl_s=100)
        clock.advance(11)
        swept = leases.sweep()
        assert [ls.device_id for ls in swept] == ["ABC"]
        assert leases.current("DEF") is not None


class TestRelease:
    def test_release_frees_device(self, leases: LeaseManager) -> None:
        lease = leases.acquire("ABC", owner="alice")
        leases.release(lease.token)
        assert leases.current("ABC") is None
        leases.acquire("ABC", owner="bob")  # must not raise

    def test_release_is_idempotent(self, leases: LeaseManager) -> None:
        lease = leases.acquire("ABC", owner="alice")
        leases.release(lease.token)
        leases.release(lease.token)  # page-unload plus explicit click

    def test_release_unknown_token_is_silent(self, leases: LeaseManager) -> None:
        leases.release("nope")

    def test_stale_token_cannot_release_a_newer_lease(self, leases: LeaseManager) -> None:
        stale = leases.acquire("ABC", owner="alice")
        leases.acquire("ABC", owner="bob", force=True)
        leases.release(stale.token)
        # Bob still holds it: alice's stale token must not be able to free it.
        assert leases.current("ABC") is not None
        assert leases.current("ABC").owner == "bob"  # type: ignore[union-attr]


class TestFencing:
    def test_assert_current_passes_for_live_lease(self, leases: LeaseManager) -> None:
        lease = leases.acquire("ABC", owner="alice")
        leases.assert_current(lease)

    def test_assert_current_detects_takeover(self, leases: LeaseManager) -> None:
        stale = leases.acquire("ABC", owner="alice")
        leases.acquire("ABC", owner="bob", force=True)
        with pytest.raises(LeaseSupersededError) as exc:
            leases.assert_current(stale)
        assert exc.value.epoch == 1
        assert exc.value.current_epoch == 2

    def test_assert_current_detects_expiry(
        self, leases: LeaseManager, clock: FakeClock
    ) -> None:
        lease = leases.acquire("ABC", owner="alice", ttl_s=10)
        clock.advance(11)
        with pytest.raises(LeaseSupersededError):
            leases.assert_current(lease)

    def test_epoch_never_goes_backwards(self, leases: LeaseManager) -> None:
        # Even across a full release, the counter must keep climbing, or a stale
        # operation from epoch 1 would look current again.
        first = leases.acquire("ABC", owner="alice")
        leases.release(first.token)
        second = leases.acquire("ABC", owner="bob")
        assert second.epoch == 2

    async def test_guard_rejects_takeover_mid_operation(self, leases: LeaseManager) -> None:
        """The whole point of the trailing check.

        Alice starts a slow screenshot; Bob takes the device over while it is in
        flight. Alice must get an error rather than acting on Bob's device.
        """
        lease = leases.acquire("ABC", owner="alice")
        with pytest.raises(LeaseSupersededError):
            async with leases.guard(lease):
                leases.acquire("ABC", owner="bob", force=True)  # takeover in flight

    async def test_guard_passes_when_uninterrupted(self, leases: LeaseManager) -> None:
        lease = leases.acquire("ABC", owner="alice")
        ran = False
        async with leases.guard(lease):
            ran = True
        assert ran


class TestPublicView:
    def test_token_is_never_exposed(self, leases: LeaseManager) -> None:
        lease = leases.acquire("ABC", owner="alice")
        public = lease.public()
        assert "token" not in public
        assert public["owner"] == "alice"
        assert public["epoch"] == 1

    def test_snapshot_omits_expired(self, leases: LeaseManager, clock: FakeClock) -> None:
        leases.acquire("ABC", owner="alice", ttl_s=10)
        clock.advance(11)
        assert leases.snapshot() == {}
