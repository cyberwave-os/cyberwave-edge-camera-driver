"""The camera driver must keep the SDK's shared TimeReference fresh.

client.video_stream defaults camera tracks to the client's shared
TimeReference, whose read() returns the last update() snapshot. Robot drivers
refresh it from their control loop; this camera-only driver has no control
loop, so without a keep-alive thread every frame (video sync anchors AND MQTT
depth frames) is stamped with the frozen client-init timestamp — collapsing
backend pointcloud recordings to a single frame (2026-07-16 field incident).
"""

import threading
import time

from main import _time_reference_update_thread


class _FakeTimeReference:
    def __init__(self) -> None:
        self.updates = 0

    def update(self) -> None:
        self.updates += 1


def test_updates_run_at_high_rate_until_stopped() -> None:
    ref = _FakeTimeReference()
    stop = threading.Event()
    t = threading.Thread(
        target=_time_reference_update_thread, args=(ref, stop), daemon=True
    )
    t.start()
    time.sleep(0.15)
    stop.set()
    t.join(timeout=2.0)

    assert not t.is_alive()
    # ~100 Hz for 150ms; generous lower bound for slow CI.
    assert ref.updates >= 5

    settled = ref.updates
    time.sleep(0.05)
    assert ref.updates == settled  # no updates after stop
