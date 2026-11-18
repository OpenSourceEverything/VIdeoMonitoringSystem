from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import video_monitoring_system.doctor as doctor_module
from video_monitoring_system.doctor import (
    POLICY_ENVIRONMENT_VARIABLE,
    DoctorReport,
    run_doctor,
    sha256_file,
)


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "public-project"
        self.repo.mkdir()
        try:
            _git(self.repo, "init", "--quiet")
        except (OSError, subprocess.CalledProcessError):
            self.temporary.cleanup()
            self.skipTest("Git is required for doctor integration tests")
        _git(self.repo, "config", "user.name", "Open Source Developer")
        _git(self.repo, "config", "user.email", "developer@example.invalid")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def track_all(self) -> None:
        _git(self.repo, "add", ".")

    def finding_codes(self, report: DoctorReport) -> set[str]:
        return {finding.code for finding in report.findings}

    def valid_manifest(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "release_version": "1.2.3",
            "role": "camera",
            "supported_boards": ["zero-w"],
            "image": "https://downloads.example.invalid/camera.img.xz",
            "sha256": "a" * 64,
            "signature": {
                "algorithm": "ed25519",
                "key_id": "release-key-1",
                "signature": base64.b64encode(bytes(64)).decode("ascii"),
            },
            "channel": "product",
            "architecture": "armhf",
            "bits": 32,
            "released": "2026-06-18",
        }

    def test_clean_repository_passes_and_serializes(self) -> None:
        self.write("src/tool.py", "print('safe')\n")
        self.write("release-manifest.json", json.dumps(self.valid_manifest()))
        self.track_all()

        report = run_doctor(self.repo)

        self.assertTrue(report.passed, report.findings)
        serialized = report.to_dict()
        self.assertTrue(serialized["passed"])
        self.assertEqual(serialized["repository"], str(self.repo.resolve()))
        self.assertIsInstance(serialized["findings"], list)

    def test_not_a_repository_fails(self) -> None:
        outside = self.base / "not-a-repository"
        outside.mkdir()

        report = run_doctor(outside)

        self.assertFalse(report.passed)
        self.assertIn("git.not_repository", self.finding_codes(report))

    def test_external_policy_checks_path_remote_identity_and_tracked_data(self) -> None:
        marker = "restricted-marker"
        self.write("docs/note.txt", f"content has {marker}\n")
        self.track_all()
        _git(self.repo, "remote", "add", "origin", f"https://{marker}.invalid/repo.git")
        _git(self.repo, "config", "user.email", f"person@{marker}.invalid")
        policy_path = self.base / "doctor-policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "forbidden_patterns": {
                        "repository_path": [r"public-project$"],
                        "git_remote": [marker],
                        "git_identity": [marker],
                        "tracked_path": [r"docs/note"],
                        "tracked_content": [marker],
                    }
                }
            ),
            encoding="utf-8",
        )

        report = run_doctor(self.repo, policy_path=policy_path)

        codes = self.finding_codes(report)
        self.assertFalse(report.passed)
        for expected in (
            "policy.repository_path",
            "policy.git_remote",
            "policy.git_identity",
            "policy.tracked_path",
            "policy.tracked_content",
        ):
            self.assertIn(expected, codes)

    def test_policy_can_be_selected_through_environment(self) -> None:
        self.write("safe.txt", "safe\n")
        self.track_all()
        policy = self.base / "policy.json"
        policy.write_text(
            json.dumps(
                {"forbidden_patterns": {"git_identity": ["Open Source Developer"]}}
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {POLICY_ENVIRONMENT_VARIABLE: str(policy)}):
            report = run_doctor(self.repo)

        self.assertIn("policy.git_identity", self.finding_codes(report))

    def test_invalid_or_in_repository_policy_fails(self) -> None:
        invalid = self.base / "invalid.json"
        invalid.write_text("[", encoding="utf-8")
        invalid_report = run_doctor(self.repo, policy_path=invalid)
        self.assertIn("policy.invalid", self.finding_codes(invalid_report))

        tracked = self.write("local-policy.json", "{}")
        self.track_all()
        tracked_report = run_doctor(self.repo, policy_path=tracked)
        self.assertIn("policy.inside_repository", self.finding_codes(tracked_report))

    def test_likely_secret_file_assignment_and_private_key_fail(self) -> None:
        sensitive_name = "." + "env"
        secret_key = "pass" + "word"
        self.write(sensitive_name, f'{secret_key}="a-real-value-123"\n')
        private_material = "-----BEGIN " + "PRIVATE KEY-----\nmaterial\n"
        self.write("config/device.txt", private_material)
        self.track_all()

        report = run_doctor(self.repo)

        codes = self.finding_codes(report)
        self.assertIn("secret.sensitive_filename", codes)
        self.assertIn("secret.assignment", codes)
        self.assertIn("secret.private_key", codes)

    def test_untracked_files_are_scanned_for_secrets_and_policy_content(self) -> None:
        marker = "blocked-content-marker"
        key = "access_" + "token"
        self.write("scratch/local-settings.txt", f'{key}="real-value-12345"\n{marker}\n')
        policy_path = self.base / "doctor-policy.json"
        policy_path.write_text(
            json.dumps({"forbidden_patterns": {"tracked_content": [marker]}}),
            encoding="utf-8",
        )

        report = run_doctor(self.repo, policy_path=policy_path)

        codes = self.finding_codes(report)
        self.assertIn("secret.assignment", codes)
        self.assertIn("policy.tracked_content", codes)

    def test_git_ignored_outputs_are_not_scanned(self) -> None:
        self.write(".gitignore", ".venv/\nbuild/\n")
        key = "pass" + "word"
        self.write(".venv/local.env", f'{key}="real-value-12345"\n')
        self.write("build/secrets.txt", f'{key}="another-real-value"\n')

        report = run_doctor(self.repo)

        self.assertNotIn("secret.assignment", self.finding_codes(report))
        self.assertNotIn("secret.sensitive_filename", self.finding_codes(report))

    def test_untracked_symlink_escape_fails_without_reading_target(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_text("safe\n", encoding="utf-8")
        link = self.repo / "linked.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links are unavailable in this environment")

        report = run_doctor(self.repo)

        self.assertIn("repository.symlink_escape", self.finding_codes(report))

    def test_file_inventory_failure_fails_closed(self) -> None:
        original_run_git = doctor_module._run_git

        def fail_inventory(path: Path, *arguments: str):
            if arguments and arguments[0] == "ls-files":
                return subprocess.CompletedProcess(
                    ["git", *arguments], 128, "", "inventory unavailable"
                )
            return original_run_git(path, *arguments)

        with mock.patch.object(doctor_module, "_run_git", side_effect=fail_inventory):
            report = run_doctor(self.repo)

        self.assertFalse(report.passed)
        self.assertIn("git.file_inventory_unavailable", self.finding_codes(report))

    def test_empty_git_root_response_fails_closed(self) -> None:
        with mock.patch.object(
            doctor_module,
            "_run_git",
            return_value=subprocess.CompletedProcess(["git"], 0, "", ""),
        ):
            report = run_doctor(self.repo)

        self.assertFalse(report.passed)
        self.assertIn("git.repository_state_unavailable", self.finding_codes(report))

    def test_secret_placeholders_do_not_fail(self) -> None:
        key = "api_" + "key"
        self.write("templates/settings.example", f'{key}="replace-me"\n')
        self.track_all()

        report = run_doctor(self.repo)

        self.assertNotIn("secret.assignment", self.finding_codes(report))

    def test_external_provisioning_tool_is_rejected(self) -> None:
        outside_tool = self.base / "factory-tool.py"
        outside_tool.write_text("print('tool')\n", encoding="utf-8")

        report = run_doctor(self.repo, provisioning_tool_paths=[outside_tool])

        self.assertIn("provisioning.outside_repository", self.finding_codes(report))

    def test_missing_internal_provisioning_tool_is_rejected(self) -> None:
        report = run_doctor(
            self.repo, provisioning_tool_paths=["tools/missing-provisioner.py"]
        )

        self.assertIn("provisioning.not_found", self.finding_codes(report))

    def test_release_manifest_requires_version_hash_and_signature(self) -> None:
        manifest = self.write(
            "candidate.json",
            json.dumps(
                {
                    "release_version": "",
                    "sha256": "short",
                    "signature": None,
                    "channel": "product",
                }
            ),
        )

        report = run_doctor(self.repo, release_manifest_path=manifest)

        codes = self.finding_codes(report)
        self.assertIn("release.version_missing", codes)
        self.assertIn("release.hash_invalid", codes)
        self.assertIn("release.signature_missing", codes)

    def test_release_inputs_are_individually_checked(self) -> None:
        manifest = self.valid_manifest()
        manifest["inputs"] = [
            {
                "sha256": "b" * 64,
                "signature": self.valid_manifest()["signature"],
            },
            {"sha256": "bad", "signature": "not-structured"},
        ]
        path = self.write("candidate.json", json.dumps(manifest))

        report = run_doctor(self.repo, release_manifest_path=path)

        input_findings = [
            finding for finding in report.findings if finding.subject == "inputs[1]"
        ]
        self.assertEqual(
            {finding.code for finding in input_findings},
            {"release.hash_invalid", "release.signature_invalid"},
        )

    def test_product_signature_must_be_a_strict_metadata_object(self) -> None:
        cases = (
            "legacy-signature",
            {"algorithm": "ed25519", "key_id": "release-key-1"},
            {
                "algorithm": "unsupported",
                "key_id": "release-key-1",
                "signature": base64.b64encode(bytes(64)).decode("ascii"),
            },
            {
                "algorithm": "ed25519",
                "key_id": "bad key id",
                "signature": base64.b64encode(bytes(64)).decode("ascii"),
            },
            {
                "algorithm": "ed25519",
                "key_id": "release-key-1",
                "signature": "not-base64",
            },
        )
        for index, signature in enumerate(cases):
            with self.subTest(index=index):
                manifest = self.valid_manifest()
                manifest["signature"] = signature
                path = self.write(f"candidate-{index}.json", json.dumps(manifest))

                report = run_doctor(self.repo, release_manifest_path=path)

                self.assertFalse(report.passed)
                self.assertIn("release.signature_invalid", self.finding_codes(report))

    def test_explicit_development_manifest_is_non_product_but_passes(self) -> None:
        manifest = self.valid_manifest()
        manifest["channel"] = "development"
        manifest["signature"] = None
        path = self.write("pilot.json", json.dumps(manifest))

        report = run_doctor(self.repo, release_manifest_path=path)

        self.assertTrue(report.passed, report.findings)
        finding = next(
            item
            for item in report.findings
            if item.code == "release.development_non_product"
        )
        self.assertEqual(finding.severity, "info")
        self.assertNotIn("release.signature_missing", self.finding_codes(report))

    def test_development_manifest_must_declare_explicit_null_signature(self) -> None:
        manifest = self.valid_manifest()
        manifest["channel"] = "development"
        del manifest["signature"]
        path = self.write("pilot-missing-signature.json", json.dumps(manifest))

        report = run_doctor(self.repo, release_manifest_path=path)

        self.assertFalse(report.passed)
        self.assertIn("release.signature_missing", self.finding_codes(report))

    def test_automatically_discovered_development_manifest_fails(self) -> None:
        manifest = self.valid_manifest()
        manifest["channel"] = "development"
        manifest["signature"] = None
        self.write("release-manifest.json", json.dumps(manifest))

        report = run_doctor(self.repo)

        codes = self.finding_codes(report)
        self.assertFalse(report.passed)
        self.assertIn("release.development_non_product", codes)
        self.assertIn("release.development_not_explicit", codes)

    def test_missing_configured_manifest_fails_but_no_release_is_informational(self) -> None:
        missing = run_doctor(self.repo, release_manifest_path="missing.json")
        self.assertIn("release.manifest_missing", self.finding_codes(missing))

        ordinary = run_doctor(self.repo)
        self.assertTrue(ordinary.passed)
        self.assertIn("release.not_configured", self.finding_codes(ordinary))

    def test_sha256_file_returns_expected_digest(self) -> None:
        artifact = self.base / "artifact.bin"
        artifact.write_bytes(b"abc")
        self.assertEqual(
            sha256_file(artifact),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


if __name__ == "__main__":
    unittest.main()
