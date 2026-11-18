import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_monitoring_system.camera_endpoints import (
    AuthenticatedCameraObservation,
    CameraEndpointConflictError,
    CameraEndpointRegistry,
    ObservedCameraEndpoint,
)


class CameraEndpointRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.state_path = Path(self.temporary_directory.name) / "camera-endpoints.json"
        self.registry = CameraEndpointRegistry(self.state_path)

    @staticmethod
    def observation(
        camera_id: str,
        ip_address: str,
        mac_address: str,
        *,
        dhcp_client_id: str = "vms-card-cam-0002",
    ) -> AuthenticatedCameraObservation:
        return AuthenticatedCameraObservation(
            camera_id=camera_id,
            endpoint=ObservedCameraEndpoint(
                ip_address=ip_address,
                mac_address=mac_address,
                dhcp_client_id=dhcp_client_id,
            ),
        )

    def test_board_swap_keeps_card_identity_while_mac_changes(self) -> None:
        original = self.observation(
            "CAM-0002", "10.42.0.26", "b8:27:eb:00:00:01"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[original]
        )

        replacement_board = self.observation(
            "CAM-0002", "10.42.0.26", "dc:a6:32:00:00:99"
        )
        result = self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[replacement_board]
        )

        self.assertEqual(result.updated, ("CAM-0002",))
        self.assertEqual(
            self.registry.endpoint_for("CAM-0002"), replacement_board.endpoint
        )
        self.assertEqual(tuple(self.registry.endpoints), ("CAM-0002",))
        self.assertEqual(
            self.registry.endpoint_for("CAM-0002").dhcp_client_id,
            original.endpoint.dhcp_client_id,
        )
        self.assertEqual(
            CameraEndpointRegistry(self.state_path).endpoint_for("CAM-0002"),
            replacement_board.endpoint,
        )

    def test_authenticated_identity_follows_address_change_automatically(self) -> None:
        original = self.observation(
            "CAM-0002", "10.42.0.26", "b8:27:eb:00:00:01"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[original]
        )
        moved = self.observation(
            "CAM-0002", "10.42.0.12", "dc:a6:32:00:00:99"
        )

        result = self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[moved]
        )

        self.assertEqual(result.updated, ("CAM-0002",))
        self.assertEqual(
            self.registry.endpoint_for("CAM-0002").ip_address, "10.42.0.12"
        )

    def test_camera_heartbeat_stages_only_that_camera_until_promoted(self) -> None:
        first = self.observation(
            "CAM-0001", "192.168.0.81", "b8:27:eb:00:00:01"
        )
        second = self.observation(
            "CAM-0002", "192.168.0.82", "b8:27:eb:00:00:02"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0001", "CAM-0002"},
            observations=[first, second],
        )

        moved = self.observation(
            "CAM-0002", "192.168.0.184", "dc:a6:32:00:00:99"
        )
        result = self.registry.observe(
            authorized_camera_ids={"CAM-0001", "CAM-0002"},
            observation=moved,
        )

        self.assertEqual(result.updated, ("CAM-0002",))
        self.assertEqual(self.registry.endpoint_for("CAM-0001"), first.endpoint)
        self.assertEqual(self.registry.endpoint_for("CAM-0002"), second.endpoint)
        self.assertEqual(self.registry.candidate_for("CAM-0002"), moved.endpoint)

        promoted = self.registry.promote(
            camera_id="CAM-0002", endpoint=moved.endpoint
        )

        self.assertEqual(promoted.updated, ("CAM-0002",))
        self.assertEqual(self.registry.endpoint_for("CAM-0001"), first.endpoint)
        self.assertEqual(self.registry.endpoint_for("CAM-0002"), moved.endpoint)
        self.assertIsNone(self.registry.candidate_for("CAM-0002"))

    def test_unauthorized_heartbeat_does_not_change_routes(self) -> None:
        known = self.observation(
            "CAM-0001", "192.168.0.81", "b8:27:eb:00:00:01"
        )
        self.registry.observe(
            authorized_camera_ids={"CAM-0001"}, observation=known
        )
        self.registry.promote(camera_id="CAM-0001", endpoint=known.endpoint)

        result = self.registry.observe(
            authorized_camera_ids={"CAM-0001"},
            observation=self.observation(
                "CAM-ROGUE", "192.168.0.82", "b8:27:eb:00:00:02"
            ),
        )

        self.assertEqual(result.ignored_unauthorized, ("CAM-ROGUE",))
        self.assertEqual(self.registry.endpoint_for("CAM-0001"), known.endpoint)
        self.assertIsNone(self.registry.endpoint_for("CAM-ROGUE"))

    def test_network_metadata_cannot_authorize_an_unknown_camera(self) -> None:
        known = self.observation(
            "CAM-0002", "10.42.0.26", "b8:27:eb:00:00:01"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[known]
        )
        same_network_facts_wrong_identity = self.observation(
            "CAM-ROGUE",
            "10.42.0.26",
            "b8:27:eb:00:00:01",
            dhcp_client_id=known.endpoint.dhcp_client_id,
        )

        result = self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"},
            observations=[same_network_facts_wrong_identity],
        )

        self.assertEqual(result.ignored_unauthorized, ("CAM-ROGUE",))
        self.assertEqual(result.removed, ("CAM-0002",))
        self.assertIsNone(self.registry.endpoint_for("CAM-0002"))
        self.assertIsNone(self.registry.endpoint_for("CAM-ROGUE"))

    def test_conflicting_observations_leave_saved_route_unchanged(self) -> None:
        original = self.observation(
            "CAM-0002", "10.42.0.26", "b8:27:eb:00:00:01"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[original]
        )

        with self.assertRaisesRegex(CameraEndpointConflictError, "multiple endpoints"):
            self.registry.reconcile(
                authorized_camera_ids={"CAM-0002"},
                observations=[
                    self.observation(
                        "CAM-0002", "10.42.0.12", "dc:a6:32:00:00:99"
                    ),
                    self.observation(
                        "CAM-0002", "10.42.0.13", "dc:a6:32:00:00:99"
                    ),
                ],
            )

        self.assertEqual(self.registry.endpoint_for("CAM-0002"), original.endpoint)
        self.assertEqual(
            CameraEndpointRegistry(self.state_path).endpoint_for("CAM-0002"),
            original.endpoint,
        )

    def test_failed_persistence_does_not_publish_new_route(self) -> None:
        original = self.observation(
            "CAM-0002", "10.42.0.26", "b8:27:eb:00:00:01"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[original]
        )
        moved = self.observation(
            "CAM-0002", "10.42.0.12", "dc:a6:32:00:00:99"
        )

        with patch(
            "video_monitoring_system.camera_endpoints.os.replace",
            side_effect=OSError("write failed"),
        ), self.assertRaisesRegex(OSError, "write failed"):
            self.registry.reconcile(
                authorized_camera_ids={"CAM-0002"}, observations=[moved]
            )

        self.assertEqual(self.registry.endpoint_for("CAM-0002"), original.endpoint)
        self.assertEqual(
            CameraEndpointRegistry(self.state_path).endpoint_for("CAM-0002"),
            original.endpoint,
        )

    def test_rejected_candidate_preserves_working_route_across_restart(self) -> None:
        original = self.observation(
            "CAM-0002", "10.42.0.26", "b8:27:eb:00:00:01"
        )
        moved = self.observation(
            "CAM-0002", "192.168.0.184", "dc:a6:32:00:00:99"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[original]
        )

        self.registry.observe(
            authorized_camera_ids={"CAM-0002"}, observation=moved
        )
        staged = CameraEndpointRegistry(self.state_path)
        self.assertEqual(staged.endpoint_for("CAM-0002"), original.endpoint)
        self.assertEqual(staged.candidate_for("CAM-0002"), moved.endpoint)

        self.assertTrue(
            staged.reject(camera_id="CAM-0002", endpoint=moved.endpoint)
        )
        reloaded = CameraEndpointRegistry(self.state_path)
        self.assertEqual(reloaded.endpoint_for("CAM-0002"), original.endpoint)
        self.assertIsNone(reloaded.candidate_for("CAM-0002"))

    def test_duplicate_route_is_quarantined_without_replacing_working_route(self) -> None:
        original = self.observation(
            "CAM-0002", "192.168.0.82", "b8:27:eb:00:00:01"
        )
        duplicate = self.observation(
            "CAM-0002", "192.168.0.184", "dc:a6:32:00:00:99"
        )
        self.registry.reconcile(
            authorized_camera_ids={"CAM-0002"}, observations=[original]
        )

        self.registry.quarantine(
            camera_id="CAM-0002", endpoint=duplicate.endpoint
        )

        reloaded = CameraEndpointRegistry(self.state_path)
        self.assertEqual(reloaded.endpoint_for("CAM-0002"), original.endpoint)
        self.assertEqual(reloaded.conflict_for("CAM-0002"), duplicate.endpoint)
        self.assertIsNone(reloaded.candidate_for("CAM-0002"))
        self.assertTrue(reloaded.clear_conflict("CAM-0002"))
        self.assertIsNone(
            CameraEndpointRegistry(self.state_path).conflict_for("CAM-0002")
        )


if __name__ == "__main__":
    unittest.main()
