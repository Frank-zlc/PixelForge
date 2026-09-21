"""Exclusive device leases with fencing.

Two people driving one phone at the same time is not a race to be smoothed over
-- it produces taps interleaved from two intents and a script that fails for
reasons nobody can reconstruct. So control of a device is exclusive, and the
exclusivity is enforced with a *fencing token* rather than a plain mutex.

Why fencing matters here: a lease can be preempted or can expire while a slow
operation is already in flight (a 30s ``screencap`` on a bad USB link, say). A
lock released by then would let the stale operation land on a device someone else
now owns. Each lease therefore carries a monotonically increasing ``epoch``, and
every device-touching operation re-checks it **before and after** the work. If a
newer lease appeared in between, the result is discarded instead of applied.

Clocks are ``time.monotonic``: wall-clock jumps (NTP, laptop sleep) must not
silently extend or expire a lease.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock

__all__ = [
    "DeviceBusyError",
    "Lease",
    "LeaseError",
    "LeaseExpiredError",
    "LeaseManager",
    "LeaseSupersededError",
]

DEFAULT_TTL_S = 30.0
MAX_TTL_S = 3600.0

# How many retired tokens to remember. A heartbeat arrives seconds after its
# lease died, never hours, so a small ring is enough to answer "what happened to
# my lease" accurately while staying bounded.
_RETIRED_MEMORY = 256


class _Retirement(str, Enum):
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class LeaseError(RuntimeError):
    """Base class for lease failures."""


class DeviceBusyError(LeaseError):
    """Another owner holds a live lease on this device."""

    def __init__(self, device_id: str, owner: str) -> None:
        super().__init__(f"device {device_id} is in use by {owner}")
        self.device_id = device_id
        self.owner = owner


class LeaseExpiredError(LeaseError):
    """The lease's TTL elapsed without a renewal."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"lease on {device_id} has expired")
        self.device_id = device_id


class LeaseSupersededError(LeaseError):
    """A newer lease was issued for this device; this one is no longer valid."""

    def __init__(self, device_id: str, epoch: int, current_epoch: int) -> None:
        super().__init__(
            f"lease on {device_id} was superseded (epoch {epoch} -> {current_epoch})"
        )
        self.device_id = device_id
        self.epoch = epoch
        self.current_epoch = current_epoch


@dataclass(frozen=True, slots=True)
class Lease:
    """An exclusive claim on one device.

    ``token`` is the secret the holder presents to renew or release.
    ``epoch`` is the public fencing value: it only ever increases per device, so
    comparing it is enough to detect that a claim has been taken over.
    """

    device_id: str
    owner: str
    token: str
    epoch: int
    ttl_s: float
    expires_at: float
    mode: str = "control"

    def remaining(self, *, now: float | None = None) -> float:
        return max(0.0, self.expires_at - (now if now is not None else time.monotonic()))

    def is_expired(self, *, now: float | None = None) -> bool:
        return self.remaining(now=now) <= 0.0

    def public(self) -> dict[str, object]:
        """Serialisable view with the token withheld."""
        return {
            "device_id": self.device_id,
            "owner": self.owner,
            "epoch": self.epoch,
            "mode": self.mode,
            "ttl_s": self.ttl_s,
            "remaining_s": round(self.remaining(), 3),
        }


class LeaseManager:
    """In-process registry of device leases.

    Single-process by design. The whole backend runs in one process because
    device sessions (scrcpy sockets, forwards) are process-local state -- see
    ARCHITECTURE.md ``工程约束``. If that ever changes, this class is the seam to
    move behind Redis, and ``epoch`` is already the right primitive for it.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = RLock()
        self._leases: dict[str, Lease] = {}
        self._by_token: dict[str, str] = {}
        # Epochs outlive their leases: a device's counter must never go
        # backwards, or a stale operation could look current again.
        self._epochs: dict[str, int] = {}
        # Why each recently-dead token died. Without this, the answer to a
        # heartbeat on a dead lease depends on whether some unrelated request
        # happened to reap it first -- the client would see "expired" or
        # "unknown token" for the same situation, and could not tell a timeout
        # from a client bug.
        self._retired: OrderedDict[str, tuple[str, _Retirement]] = OrderedDict()

    # ------------------------------------------------------------- acquiring

    def acquire(
        self,
        device_id: str,
        *,
        owner: str,
        ttl_s: float = DEFAULT_TTL_S,
        mode: str = "control",
        force: bool = False,
    ) -> Lease:
        """Claim a device.

        Raises :class:`DeviceBusyError` if someone else holds a live lease,
        unless ``force`` is set. The same owner re-acquiring is always allowed
        and issues a fresh epoch (so their own earlier in-flight work is fenced
        off too -- reconnecting after a browser reload should not let a
        half-finished operation from the previous page land).
        """
        if not device_id:
            raise ValueError("device_id is required")
        if not owner:
            raise ValueError("owner is required")
        if not 0 < ttl_s <= MAX_TTL_S:
            raise ValueError(f"ttl_s must be in (0, {MAX_TTL_S}]")

        now = self._clock()
        with self._lock:
            current = self._live(device_id, now=now)
            if current is not None and current.owner != owner and not force:
                raise DeviceBusyError(device_id, current.owner)
            if current is not None:
                self._by_token.pop(current.token, None)
                self._retire(current, _Retirement.SUPERSEDED)

            epoch = self._epochs.get(device_id, 0) + 1
            self._epochs[device_id] = epoch
            lease = Lease(
                device_id=device_id,
                owner=owner,
                token=uuid.uuid4().hex,
                epoch=epoch,
                ttl_s=ttl_s,
                expires_at=now + ttl_s,
                mode=mode,
            )
            self._leases[device_id] = lease
            self._by_token[lease.token] = device_id
            return lease

    def renew(self, token: str, *, ttl_s: float | None = None) -> Lease:
        """Extend a lease. This is the heartbeat the frontend calls."""
        now = self._clock()
        with self._lock:
            lease = self._resolve_token(token, now=now)
            extended = replace(
                lease,
                ttl_s=ttl_s if ttl_s is not None else lease.ttl_s,
                expires_at=now + (ttl_s if ttl_s is not None else lease.ttl_s),
            )
            self._leases[lease.device_id] = extended
            return extended

    def release(self, token: str) -> None:
        """Release a lease. Idempotent: releasing twice is not an error."""
        with self._lock:
            device_id = self._by_token.pop(token, None)
            if device_id is None:
                return
            held = self._leases.get(device_id)
            if held is not None and held.token == token:
                del self._leases[device_id]

    # -------------------------------------------------------------- checking

    def current(self, device_id: str) -> Lease | None:
        """The live lease for a device, or None. Expired leases are reaped."""
        with self._lock:
            return self._live(device_id, now=self._clock())

    def assert_current(self, lease: Lease) -> None:
        """Verify a lease is still the authoritative claim on its device.

        Call this before and after every device-touching operation.
        """
        with self._lock:
            held = self._live(lease.device_id, now=self._clock())
            if held is None or held.epoch > lease.epoch:
                raise LeaseSupersededError(
                    lease.device_id,
                    lease.epoch,
                    held.epoch if held else self._epochs.get(lease.device_id, 0),
                )
            if held.token != lease.token:
                raise LeaseSupersededError(lease.device_id, lease.epoch, held.epoch)

    @asynccontextmanager
    async def guard(self, lease: Lease) -> AsyncIterator[Lease]:
        """Fence a device operation on both sides.

        >>> async with leases.guard(lease):
        ...     await adb.screencap_png(lease.device_id)

        The trailing check is the point: if the lease was taken over while the
        screenshot was in flight, the caller gets
        :class:`LeaseSupersededError` instead of acting on a device that now
        belongs to someone else.
        """
        self.assert_current(lease)
        yield lease
        self.assert_current(lease)

    def sweep(self) -> list[Lease]:
        """Drop expired leases and return them, so callers can notify the UI."""
        now = self._clock()
        with self._lock:
            expired = [ls for ls in self._leases.values() if ls.is_expired(now=now)]
            for lease in expired:
                self._leases.pop(lease.device_id, None)
                self._by_token.pop(lease.token, None)
                self._retire(lease, _Retirement.EXPIRED)
            return expired

    def snapshot(self) -> dict[str, Lease]:
        with self._lock:
            now = self._clock()
            return {
                device_id: lease
                for device_id, lease in self._leases.items()
                if not lease.is_expired(now=now)
            }

    # --------------------------------------------------------------- private

    def _retire(self, lease: Lease, reason: _Retirement) -> None:
        """Record why a token stopped being valid, in a bounded ring."""
        self._retired[lease.token] = (lease.device_id, reason)
        self._retired.move_to_end(lease.token)
        while len(self._retired) > _RETIRED_MEMORY:
            self._retired.popitem(last=False)

    def _live(self, device_id: str, *, now: float) -> Lease | None:
        lease = self._leases.get(device_id)
        if lease is None:
            return None
        if lease.is_expired(now=now):
            del self._leases[device_id]
            self._by_token.pop(lease.token, None)
            self._retire(lease, _Retirement.EXPIRED)
            return None
        return lease

    def _resolve_token(self, token: str, *, now: float) -> Lease:
        device_id = self._by_token.get(token)
        if device_id is None:
            self._raise_for_retired(token)
            raise LeaseError("unknown lease token")
        lease = self._leases.get(device_id)
        if lease is None or lease.token != token:
            self._raise_for_retired(token)
            raise LeaseError("unknown lease token")
        if lease.is_expired(now=now):
            del self._leases[device_id]
            self._by_token.pop(token, None)
            self._retire(lease, _Retirement.EXPIRED)
            raise LeaseExpiredError(device_id)
        return lease

    def _raise_for_retired(self, token: str) -> None:
        """Report the real reason a dead token is dead, if we still know it.

        This is what makes the answer deterministic: whether or not some
        unrelated request already reaped the lease, the client gets the same
        error for the same situation.
        """
        record = self._retired.get(token)
        if record is None:
            return
        device_id, reason = record
        if reason is _Retirement.EXPIRED:
            raise LeaseExpiredError(device_id)
        raise LeaseSupersededError(device_id, 0, self._epochs.get(device_id, 0))
