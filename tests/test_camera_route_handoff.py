import unittest

from video_monitoring_system.camera_endpoints import ObservedCameraEndpoint
from video_monitoring_system.camera_route_handoff import (
    CameraRouteHandoffError,
    FrigateCameraRouteAdapter,
    FrigateRouteStage,
    TransactionalCameraRouteHandoff,
)


CAMERA_ID = "CAM-0123456789ABCDEF"


class Adapter:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def _call(self, name, **values):
        self.calls.append((name, values))
        if self.failure == name:
            raise RuntimeError(name)

    def stage(self, **values):
        self._call("stage", **values)
        return "private-stage-handle"

    def verify_staged(self, **values):
        self._call("verify_staged", **values)

    def switch(self, **values):
        self._call("switch", **values)

    def verify_active(self, **values):
        self._call("verify_active", **values)

    def restore(self, **values):
        self._call("restore", **values)

    def commit(self, **values):
        self._call("commit", **values)

    def discard(self, **values):
        self._call("discard", **values)


class TransactionalCameraRouteHandoffTests(unittest.TestCase):
    def setUp(self):
        self.previous = ObservedCameraEndpoint("10.42.0.82", 9080)
        self.candidate = ObservedCameraEndpoint("192.168.1.82", 9080)

    def test_prepares_downstream_before_registry_commit(self):
        adapter = Adapter()

        transaction = TransactionalCameraRouteHandoff(adapter).prepare(
            camera_id=CAMERA_ID,
            previous=self.previous,
            candidate=self.candidate,
        )

        self.assertEqual(
            [name for name, _ in adapter.calls],
            ["stage", "verify_staged", "switch", "verify_active"],
        )
        transaction.commit()
        self.assertEqual(
            [name for name, _ in adapter.calls][-2:], ["commit", "discard"]
        )

    def test_staging_failure_never_touches_the_working_route(self):
        adapter = Adapter(failure="verify_staged")

        with self.assertRaisesRegex(CameraRouteHandoffError, "downstream"):
            TransactionalCameraRouteHandoff(adapter).prepare(
                camera_id=CAMERA_ID,
                previous=self.previous,
                candidate=self.candidate,
            )

        self.assertEqual(
            [name for name, _ in adapter.calls],
            ["stage", "verify_staged", "discard"],
        )

    def test_post_switch_failure_restores_and_proves_previous_route(self):
        class FailsFirstActiveProof(Adapter):
            def __init__(self):
                super().__init__()
                self.proofs = 0

            def verify_active(self, **values):
                self._call("verify_active", **values)
                self.proofs += 1
                if self.proofs == 1:
                    raise RuntimeError("candidate downstream failed")

        adapter = FailsFirstActiveProof()

        with self.assertRaisesRegex(CameraRouteHandoffError, "downstream"):
            TransactionalCameraRouteHandoff(adapter).prepare(
                camera_id=CAMERA_ID,
                previous=self.previous,
                candidate=self.candidate,
            )

        self.assertEqual(
            [name for name, _ in adapter.calls],
            [
                "stage",
                "verify_staged",
                "switch",
                "verify_active",
                "restore",
                "verify_active",
                "discard",
            ],
        )

    def test_registry_failure_can_roll_back_a_prepared_switch(self):
        adapter = Adapter()
        transaction = TransactionalCameraRouteHandoff(adapter).prepare(
            camera_id=CAMERA_ID,
            previous=self.previous,
            candidate=self.candidate,
        )

        transaction.rollback()

        self.assertEqual(
            [name for name, _ in adapter.calls][-3:],
            ["restore", "verify_active", "discard"],
        )

    def test_failed_restore_is_reported_as_the_safety_failure(self):
        class FailsActiveAndRestore(Adapter):
            def verify_active(self, **values):
                self._call("verify_active", **values)
                raise RuntimeError("no frames")

        adapter = FailsActiveAndRestore(failure="restore")

        with self.assertRaisesRegex(CameraRouteHandoffError, "could not be restored"):
            TransactionalCameraRouteHandoff(adapter).prepare(
                camera_id=CAMERA_ID,
                previous=self.previous,
                candidate=self.candidate,
            )


class FakeRelay:
    def __init__(self):
        self.calls = []

    def activate_candidate(self, **values):
        self.calls.append(("activate", values))

    def restore_committed(self, **values):
        self.calls.append(("restore", values))


class FakeEngine:
    camera_bindings = {CAMERA_ID: "driveway"}

    def __init__(self):
        self.calls = []
        # Static scenes can produce identical JPEGs while the H.264 transport
        # keeps advancing. Verification checks valid decoded frames, not motion.
        self.private_frames = [b"same-frame"] * 4
        self.snapshots = [b"same-snapshot"] * 2

    @staticmethod
    def private_route_source(**values):
        return f"relay:{values['camera_id']}:{values['selection']}"

    def replace_private_route_source(self, **values):
        self.calls.append(("replace", values))

    def delete_private_route_source(self, **values):
        self.calls.append(("delete", values))

    def read_private_route_frame(self, **values):
        self.calls.append(("relay_frame", values))
        return self.private_frames.pop(0)

    def read_snapshot(self, **values):
        self.calls.append(("frigate_frame", values))
        return self.snapshots.pop(0)


class FrigateCameraRouteAdapterTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = ObservedCameraEndpoint("192.168.1.82", 9080)
        self.engine = FakeEngine()
        self.relay = FakeRelay()
        self.adapter = FrigateCameraRouteAdapter(
            self.engine, self.relay, sleeper=lambda _seconds: None
        )

    def test_stage_switch_verify_commit_uses_only_stable_relay_sources(self):
        stage = self.adapter.stage(camera_id=CAMERA_ID, endpoint=self.endpoint)
        self.adapter.verify_staged(camera_id=CAMERA_ID, handle=stage)
        self.adapter.switch(camera_id=CAMERA_ID, handle=stage)
        self.adapter.verify_active(camera_id=CAMERA_ID)
        self.adapter.commit(camera_id=CAMERA_ID, handle=stage)
        self.adapter.discard(camera_id=CAMERA_ID, handle=stage)

        replacements = [values for name, values in self.engine.calls if name == "replace"]
        self.assertEqual(replacements[0]["stream_name"], stage.stream_name)
        self.assertEqual(replacements[0]["source"], f"relay:{CAMERA_ID}:candidate")
        self.assertEqual(replacements[1]["stream_name"], "driveway")
        self.assertEqual(replacements[1]["source"], f"relay:{CAMERA_ID}:active")
        self.assertEqual(
            [name for name, _ in self.relay.calls], ["activate", "restore"]
        )

    def test_restore_reselects_committed_route_before_resetting_working_stream(self):
        stage = FrigateRouteStage(CAMERA_ID, self.endpoint, "ose_stage_test")

        self.adapter.restore(
            camera_id=CAMERA_ID,
            previous=ObservedCameraEndpoint("10.42.0.82", 9080),
            handle=stage,
        )

        self.assertEqual(self.relay.calls[0][0], "restore")
        self.assertEqual(self.engine.calls[0][0], "replace")
        self.assertEqual(self.engine.calls[0][1]["stream_name"], "driveway")


if __name__ == "__main__":
    unittest.main()
