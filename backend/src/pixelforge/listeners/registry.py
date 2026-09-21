"""Listener registry.

Only logcat ships built in. HTTP interception and raw packet capture are declared
here as unbuilt rather than omitted, because which one a project needs is a
property of the protocol it speaks -- and picking wrong is expensive:

* **mitmproxy** sees TCP HTTP(S) only. It cannot see UDP at all, so a game on a
  UDP transport (Photon, QUIC, most realtime protocols) is invisible to it
  regardless of configuration. On Android it also faces three obstacles worth
  verifying *before* writing any code: API 24+ apps do not trust user-installed
  CA certificates unless they opt in, certificate pinning defeats interception
  even when they do, and many apps ignore the system HTTP proxy entirely.
* **pcap** sees everything on the wire but understands nothing, so it needs a
  protocol parser supplied per project.

Neither is a substitute for the other, and neither is an upgrade of logcat.
"""

from __future__ import annotations

from pixelforge.adb.client import AdbClient
from pixelforge.listeners.base import Listener
from pixelforge.listeners.logcat import LogcatListener

__all__ = ["PLANNED_LISTENERS", "available_listeners", "build_listener"]

PLANNED_LISTENERS = {
    "mitmproxy": (
        "HTTP(S) interception. TCP only -- cannot see UDP. Verify the target app "
        "trusts a user CA, does not pin, and honours the system proxy first."
    ),
    "pcap": (
        "Raw packet capture. Needs a per-project protocol parser to mean anything."
    ),
}


def available_listeners() -> list[dict[str, str]]:
    return [
        {"name": LogcatListener.name, "description": LogcatListener.description,
         "status": "built"},
        *(
            {"name": name, "description": description, "status": "planned"}
            for name, description in PLANNED_LISTENERS.items()
        ),
    ]


def build_listener(name: str, adb: AdbClient, **options: object) -> Listener:
    if name == LogcatListener.name:
        return LogcatListener(adb, **options)  # type: ignore[arg-type]
    if name in PLANNED_LISTENERS:
        raise NotImplementedError(
            f"listener {name!r} is not built yet: {PLANNED_LISTENERS[name]}"
        )
    raise KeyError(f"unknown listener {name!r}")
