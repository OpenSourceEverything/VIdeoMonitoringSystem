"""Reproducible generic Raspberry Pi recorder image build.

The image contains public recorder runtime code only. Factory provisioning
adds each kit's identity, camera authorization, and private network settings
after the image has been written to removable media.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence

from .image_build import (
    CameraImageBuildError,
    RawImageInspection,
    _CLOUD_INIT_JOURNAL_ONLY_DROP_IN,
    _CLOUD_INIT_JOURNAL_ONLY_DROP_IN_NAME,
    _CLOUD_INIT_STAGE_UNITS,
    _COMMIT,
    _VMS_PROVISION_CLEANUP_SCRIPT,
    _VMS_PROVISION_CLEANUP_SERVICE,
    _VMS_PROVISION_SCRIPT,
    _VMS_PROVISION_SERVICE,
    _checked_output,
    _confined_rootfs_file,
    _effective_scalar,
    _effective_unit_assignments,
    _prepare_pi_gen,
    _require_clean_versioned_source,
    _sha256,
    _verify_enabled_unit,
    _verify_exported_image_rootfs,
    check_build_host,
    inspect_xz_image,
)
from .provisioning import PREINSTALLED_RECORDER_IMAGE_REFERENCE


RecorderImageBuildError = CameraImageBuildError
_RECORDER_IMAGE = Path("platforms/raspberry-pi/recorder-image/pilot")
_RECORDER_SERVICE = Path("platforms/linux/recorder")
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_WHEEL_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REQUIRED_PACKAGES = frozenset(
    {
        "avahi-daemon",
        "cloud-init",
        "iputils-ping",
        "network-manager",
        "nftables",
        "openssh-server",
        "python3",
        "python3-pip",
        "python3-venv",
        "sudo",
        "wpasupplicant",
    }
)


@dataclass(frozen=True, slots=True)
class PythonWheel:
    name: str
    version: str
    filename: str
    sha256: str

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PythonWheel:
        if set(value) != {"name", "version", "filename", "sha256"}:
            raise ValueError("python wheel fields are invalid")
        wheel = cls(
            name=value.get("name"),
            version=value.get("version"),
            filename=value.get("filename"),
            sha256=value.get("sha256"),
        )
        if _PACKAGE_NAME.fullmatch(wheel.name) is None:
            raise ValueError("python wheel name is invalid")
        if not wheel.version or any(character.isspace() for character in wheel.version):
            raise ValueError("python wheel version is invalid")
        if _WHEEL_FILE.fullmatch(wheel.filename) is None:
            raise ValueError("python wheel filename is invalid")
        if _SHA256.fullmatch(wheel.sha256) is None:
            raise ValueError("python wheel sha256 is invalid")
        return wheel


@dataclass(frozen=True, slots=True)
class RecorderImageBuildManifest:
    schema_version: int
    profile: str
    image_reference: str
    release_version: str
    distribution_release: str
    architecture: str
    bits: int
    supported_boards: tuple[str, ...]
    builder_repository: str
    builder_commit: str
    packages: tuple[str, ...]
    python_wheels: tuple[PythonWheel, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RecorderImageBuildManifest:
        expected = {
            "schema_version",
            "profile",
            "image_reference",
            "release_version",
            "distribution_release",
            "architecture",
            "bits",
            "supported_boards",
            "builder",
            "packages",
            "python_wheels",
        }
        if set(value) != expected:
            raise ValueError("recorder image build manifest fields are invalid")
        builder = value.get("builder")
        boards = value.get("supported_boards")
        packages = value.get("packages")
        wheels = value.get("python_wheels")
        if not isinstance(builder, Mapping) or set(builder) != {"repository", "commit"}:
            raise ValueError("builder must contain exactly repository and commit")
        if not isinstance(boards, list) or not all(isinstance(item, str) for item in boards):
            raise ValueError("supported_boards must be an array of strings")
        if not isinstance(packages, list) or not all(isinstance(item, str) for item in packages):
            raise ValueError("packages must be an array of strings")
        if not isinstance(wheels, list) or not all(isinstance(item, Mapping) for item in wheels):
            raise ValueError("python_wheels must be an array of objects")
        manifest = cls(
            schema_version=value.get("schema_version"),
            profile=value.get("profile"),
            image_reference=value.get("image_reference"),
            release_version=value.get("release_version"),
            distribution_release=value.get("distribution_release"),
            architecture=value.get("architecture"),
            bits=value.get("bits"),
            supported_boards=tuple(boards),
            builder_repository=builder.get("repository"),
            builder_commit=builder.get("commit"),
            packages=tuple(packages),
            python_wheels=tuple(PythonWheel.from_dict(item) for item in wheels),
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.schema_version != 1 or self.profile != "pilot-debug":
            raise ValueError("unsupported recorder image build profile")
        if self.image_reference != PREINSTALLED_RECORDER_IMAGE_REFERENCE:
            raise ValueError("preinstalled recorder image reference is inconsistent")
        if self.distribution_release != "trixie":
            raise ValueError("pilot recorder image must use trixie")
        if self.architecture != "arm64" or self.bits != 64:
            raise ValueError("Pi 4 recorder image must use 64-bit arm64")
        if self.supported_boards != ("pi4",):
            raise ValueError("recorder image must declare the Pi 4 board")
        if not isinstance(self.release_version, str) or not self.release_version.strip():
            raise ValueError("release_version must be non-empty text")
        if not isinstance(self.builder_repository, str) or not self.builder_repository.strip():
            raise ValueError("builder repository must be non-empty text")
        if _COMMIT.fullmatch(self.builder_commit) is None:
            raise ValueError("pi-gen must be pinned to a full commit hash")
        if len(set(self.packages)) != len(self.packages):
            raise ValueError("packages must not contain duplicates")
        missing = _REQUIRED_PACKAGES - set(self.packages)
        if missing:
            raise ValueError("recorder image is missing packages: " + ", ".join(sorted(missing)))
        names = tuple(wheel.name.lower() for wheel in self.python_wheels)
        filenames = tuple(wheel.filename for wheel in self.python_wheels)
        if len(set(names)) != len(names) or len(set(filenames)) != len(filenames):
            raise ValueError("python wheels must be unique")
        if set(names) != {"cryptography", "pyyaml", "segno", "cffi", "pycparser", "setuptools"}:
            raise ValueError("python wheel set is incomplete")


def load_build_manifest(path: str | os.PathLike[str]) -> RecorderImageBuildManifest:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RecorderImageBuildError("recorder image build manifest is unreadable") from error
    if not isinstance(value, Mapping):
        raise RecorderImageBuildError("recorder image build manifest must be an object")
    try:
        return RecorderImageBuildManifest.from_dict(value)
    except (TypeError, ValueError) as error:
        raise RecorderImageBuildError(str(error)) from error


def prepare_wheelhouse(
    manifest: RecorderImageBuildManifest, directory: Path
) -> Path:
    """Download the exact pinned ARM64 wheels and verify every byte."""

    expected = {wheel.filename: wheel for wheel in manifest.python_wheels}
    if directory.is_dir() and set(path.name for path in directory.iterdir()) == set(expected):
        if all(_sha256(directory / name) == wheel.sha256 for name, wheel in expected.items()):
            return directory
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise RecorderImageBuildError("recorder wheel cache is unsafe")
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    requirements = [f"{wheel.name}=={wheel.version}" for wheel in manifest.python_wheels]
    arguments = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-deps",
        "--only-binary=:all:",
        "--platform=manylinux2014_aarch64",
        "--python-version=313",
        "--implementation=cp",
        "--abi=cp313",
        f"--dest={directory}",
        *requirements,
    ]
    try:
        subprocess.run(arguments, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RecorderImageBuildError("pinned recorder wheels could not be downloaded") from error
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != set(expected):
        raise RecorderImageBuildError("downloaded recorder wheel set is inconsistent")
    for name, wheel in expected.items():
        if _sha256(directory / name) != wheel.sha256:
            raise RecorderImageBuildError(f"recorder wheel checksum failed: {name}")
    return directory


def _copy_versioned_source(repository: Path, destination: Path) -> dict[str, str]:
    prefixes = ("pyproject.toml", "src/video_monitoring_system", os.fspath(_RECORDER_SERVICE))
    tracked = _checked_output(["git", "ls-files", "--", *prefixes], cwd=repository).splitlines()
    if "pyproject.toml" not in tracked or not tracked:
        raise RecorderImageBuildError("recorder package source is incomplete")
    hashes: dict[str, str] = {}
    for relative in tracked:
        source = repository / relative
        if source.is_symlink() or not source.is_file():
            raise RecorderImageBuildError(f"recorder source is unsafe: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes[relative] = _sha256(source)
    return hashes


def assemble_pi_gen_stage(
    repository: Path,
    stage_directory: Path,
    manifest: RecorderImageBuildManifest,
    wheelhouse: Path,
    *,
    source_revision: str,
) -> None:
    """Assemble the generic recorder stage from committed public inputs."""

    if _COMMIT.fullmatch(source_revision) is None:
        raise RecorderImageBuildError("VMS source must use a full commit hash")
    stage_directory = stage_directory.resolve()
    if stage_directory.name != "stage-vms-recorder" or stage_directory.is_symlink():
        raise RecorderImageBuildError("recorder pi-gen stage path is unsafe")
    if stage_directory.exists():
        shutil.rmtree(stage_directory)
    previous_stage = stage_directory.parent / "stage2"
    if not previous_stage.is_dir():
        raise RecorderImageBuildError("pi-gen stage2 is unavailable")
    (previous_stage / "SKIP_IMAGES").write_text("", encoding="utf-8")
    (stage_directory / "EXPORT_IMAGE").parent.mkdir(parents=True)
    (stage_directory / "EXPORT_IMAGE").write_text("", encoding="utf-8")
    prerun = stage_directory / "prerun.sh"
    prerun.write_text(
        '#!/bin/bash -e\n\nif [ ! -d "${ROOTFS_DIR}" ]; then\n    copy_previous\nfi\n',
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(prerun, 0o755)
    substage = stage_directory / "00-vms-recorder"
    files_directory = substage / "files"
    files_directory.mkdir(parents=True)
    (substage / "00-packages-nr").write_text(
        "\n".join(manifest.packages) + "\n", encoding="utf-8", newline="\n"
    )
    shutil.copyfile(repository / _RECORDER_IMAGE / "install-rootfs.sh", substage / "00-run.sh")
    os.chmod(substage / "00-run.sh", 0o755)

    source_hashes = _copy_versioned_source(
        repository, files_directory / "usr/src/video-monitoring-system"
    )
    wheel_destination = files_directory / "usr/share/vms-recorder-wheels"
    wheel_destination.mkdir(parents=True)
    for wheel in manifest.python_wheels:
        source = wheelhouse / wheel.filename
        if not source.is_file() or source.is_symlink() or _sha256(source) != wheel.sha256:
            raise RecorderImageBuildError(f"recorder wheel is missing or invalid: {wheel.filename}")
        shutil.copyfile(source, wheel_destination / wheel.filename)

    payload: dict[str, tuple[str, int]] = {
        "usr/local/libexec/vms-provision": (_VMS_PROVISION_SCRIPT, 0o755),
        "etc/systemd/system/vms-provision.service": (_VMS_PROVISION_SERVICE, 0o644),
        "usr/local/libexec/vms-provision-cleanup": (_VMS_PROVISION_CLEANUP_SCRIPT, 0o700),
        "etc/systemd/system/vms-provision-cleanup.service": (_VMS_PROVISION_CLEANUP_SERVICE, 0o644),
    }
    for unit in _CLOUD_INIT_STAGE_UNITS:
        payload[f"etc/systemd/system/{unit}.d/{_CLOUD_INIT_JOURNAL_ONLY_DROP_IN_NAME}"] = (
            _CLOUD_INIT_JOURNAL_ONLY_DROP_IN,
            0o644,
        )
    for relative, (content, mode) in payload.items():
        destination = files_directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(destination, mode)

    wheels_value = [
        {
            "name": wheel.name,
            "version": wheel.version,
            "filename": wheel.filename,
            "sha256": wheel.sha256,
        }
        for wheel in manifest.python_wheels
    ]
    contract = {
        "schema_version": 1,
        "profile": manifest.profile,
        "role": "recorder",
        "image_reference": manifest.image_reference,
        "release_version": manifest.release_version,
        "first_boot": "configuration-only",
        "contains_kit_secrets": False,
        "vms_source_commit": source_revision,
        "vms_source_sha256": source_hashes,
        "pi_gen_commit": manifest.builder_commit,
        "python_wheels": wheels_value,
    }
    metadata = files_directory / "usr/share/vms-recorder-image"
    metadata.mkdir(parents=True)
    (metadata / "build-contract.json").write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (metadata / "required-packages.txt").write_text(
        "\n".join(manifest.packages) + "\n", encoding="utf-8", newline="\n"
    )
    (metadata / "python-wheels.json").write_text(
        json.dumps(wheels_value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def verify_rootfs_runtime_contract(rootfs: Path) -> None:
    """Verify generic recorder runtime and absence of per-kit state."""

    rootfs = rootfs.resolve()
    if not rootfs.is_dir() or rootfs.is_symlink():
        raise RecorderImageBuildError("recorder image root filesystem is unsafe")
    owned = {
        "usr/local/libexec/vms-provision": (_VMS_PROVISION_SCRIPT, 0o755),
        "etc/systemd/system/vms-provision.service": (_VMS_PROVISION_SERVICE, 0o644),
        "usr/local/libexec/vms-provision-cleanup": (_VMS_PROVISION_CLEANUP_SCRIPT, 0o700),
        "etc/systemd/system/vms-provision-cleanup.service": (_VMS_PROVISION_CLEANUP_SERVICE, 0o644),
    }
    for unit in _CLOUD_INIT_STAGE_UNITS:
        owned[f"etc/systemd/system/{unit}.d/{_CLOUD_INIT_JOURNAL_ONLY_DROP_IN_NAME}"] = (
            _CLOUD_INIT_JOURNAL_ONLY_DROP_IN,
            0o644,
        )
    for relative, (expected, mode) in owned.items():
        path = _confined_rootfs_file(rootfs, relative, label=f"image-owned /{relative}")
        if path.read_text(encoding="utf-8") != expected:
            raise RecorderImageBuildError(f"image-owned /{relative} is inconsistent")
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != mode:
            raise RecorderImageBuildError(f"image-owned /{relative} has an unsafe mode")
    repository = _repository_root()
    for name in (
        "vms-recorder.service",
        "vms-recorder-capture.service",
        "vms-recorder-capture.timer",
        "vms-recorder-alerts.service",
        "vms-recorder-monitor.service",
        "vms-recorder-maintenance.service",
        "vms-recorder-maintenance.timer",
        "vms-recording-ready.path",
        "vms-recording-ready.service",
        "vms-remote-gateway.path",
        "vms-remote-gateway.service",
    ):
        installed = _confined_rootfs_file(
            rootfs,
            f"etc/systemd/system/{name}",
            label=f"recorder service {name}",
        )
        expected = (repository / _RECORDER_SERVICE / name).read_text(encoding="utf-8")
        if installed.read_text(encoding="utf-8") != expected:
            raise RecorderImageBuildError(f"recorder service {name} is inconsistent")
    for relative in (
        "etc/vms/device.json",
        "usr/local/libexec/vms-first-boot",
        "etc/systemd/system/vms-first-boot.service",
    ):
        path = rootfs.joinpath(*relative.split("/"))
        if os.path.lexists(path):
            raise RecorderImageBuildError("generic recorder image contains per-kit state")
    executable = _confined_rootfs_file(
        rootfs,
        "opt/video-monitoring-system/bin/vms",
        label="recorder application command",
    )
    if not os.access(executable, os.X_OK):
        raise RecorderImageBuildError("recorder application command is not executable")
    contract_path = _confined_rootfs_file(
        rootfs,
        "usr/share/vms-recorder-image/build-contract.json",
        label="recorder build contract",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _confined_rootfs_file(
        rootfs,
        "usr/share/vms-recorder-image/package-versions.tsv",
        label="recorder package inventory",
    )
    if (
        contract.get("role") != "recorder"
        or contract.get("image_reference") != PREINSTALLED_RECORDER_IMAGE_REFERENCE
        or contract.get("contains_kit_secrets") is not False
        or _COMMIT.fullmatch(str(contract.get("vms_source_commit"))) is None
    ):
        raise RecorderImageBuildError("recorder build contract is invalid")
    provision = _effective_unit_assignments(rootfs, "vms-provision.service")
    if _effective_scalar(provision, "Service", "Restart") != "on-failure":
        raise RecorderImageBuildError("recorder provisioning retry policy is missing")
    _verify_enabled_unit(rootfs, "multi-user.target", "vms-provision.service")
    _verify_enabled_unit(rootfs, "cloud-init.target", "vms-provision-cleanup.service")
    for target, unit in (
        ("multi-user.target", "vms-recorder.service"),
        ("timers.target", "vms-recorder-capture.timer"),
        ("multi-user.target", "vms-recorder-alerts.service"),
        ("multi-user.target", "vms-recorder-monitor.service"),
        ("timers.target", "vms-recorder-maintenance.timer"),
    ):
        if (rootfs / "etc/systemd/system" / f"{target}.wants" / unit).exists():
            raise RecorderImageBuildError("recorder runtime starts before kit provisioning")


def _pi_gen_config(manifest: RecorderImageBuildManifest) -> str:
    image_name = re.sub(r"[^A-Za-z0-9_.-]", "-", f"vms-recorder-{manifest.release_version}")
    return "\n".join(
        (
            f"IMG_NAME='{image_name}'",
            "PI_GEN_RELEASE='Video Monitoring System pilot recorder'",
            f"RELEASE='{manifest.distribution_release}'",
            f"ARCH='{manifest.architecture}'",
            "DEPLOY_COMPRESSION='xz'",
            "COMPRESSION_LEVEL='6'",
            "ENABLE_CLOUD_INIT='1'",
            "ENABLE_SSH='0'",
            "FIRST_USER_NAME='vms-build'",
            "STAGE_LIST='stage0 stage1 stage2 stage-vms-recorder'",
            "",
        )
    )


def build_recorder_image(
    repository: Path,
    manifest_path: Path,
    work_directory: Path,
    output_directory: Path,
) -> tuple[Path, Path, Path]:
    source_revision = _require_clean_versioned_source(repository)
    check_build_host()
    manifest = load_build_manifest(manifest_path)
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RecorderImageBuildError("Docker is unavailable") from error
    wheelhouse = prepare_wheelhouse(manifest, work_directory / "wheels")
    pi_gen = _prepare_pi_gen(repository, work_directory, manifest)
    assemble_pi_gen_stage(
        repository,
        pi_gen / "stage-vms-recorder",
        manifest,
        wheelhouse,
        source_revision=source_revision,
    )
    config = pi_gen / "config-vms-recorder"
    config.write_text(_pi_gen_config(manifest), encoding="utf-8", newline="\n")
    deploy = pi_gen / "deploy"
    if deploy.is_symlink():
        raise RecorderImageBuildError("pi-gen deploy directory is unsafe")
    if deploy.exists():
        shutil.rmtree(deploy)
    try:
        subprocess.run(
            ["./build-docker.sh", "-c", config.name], cwd=pi_gen, check=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RecorderImageBuildError("pi-gen recorder image build failed") from error
    artifacts = tuple(deploy.glob("*.img.xz"))
    if len(artifacts) != 1:
        raise RecorderImageBuildError("pi-gen did not produce one recorder image")

    output_directory.mkdir(parents=True, exist_ok=True)
    stem = f"vms-recorder-{manifest.release_version}"
    artifact = output_directory / f"{stem}.img.xz"
    release_path = output_directory / f"{stem}.json"
    verification_path = output_directory / f"{stem}.verification.json"
    if any(path.exists() for path in (artifact, release_path, verification_path)):
        raise RecorderImageBuildError("output already exists")
    with tempfile.TemporaryDirectory(
        prefix=".vms-recorder-verify-", dir=output_directory
    ) as directory:
        staged = Path(directory) / artifact.name
        shutil.copy2(artifacts[0], staged)
        inspection = inspect_xz_image(staged)
        _verify_exported_image_rootfs(
            staged,
            inspection,
            runtime_verifier=verify_rootfs_runtime_contract,
            role="recorder",
        )
        compressed_sha256 = _sha256(staged)
        os.link(staged, artifact)
        staged.unlink()
    release = {
        "schema_version": 1,
        "release_version": manifest.release_version,
        "role": "recorder",
        "supported_boards": list(manifest.supported_boards),
        "image": str(artifact.resolve()),
        "sha256": compressed_sha256,
        "extract_sha256": inspection.sha256,
        "signature": None,
        "channel": "development",
        "architecture": manifest.architecture,
        "bits": manifest.bits,
        "released": date.today().isoformat(),
        "image_reference": manifest.image_reference,
    }
    release_path.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verification = {
        "schema_version": 1,
        "release_version": manifest.release_version,
        "compressed_image": {"bytes": artifact.stat().st_size, "sha256": compressed_sha256},
        "raw_image": {
            "bytes": inspection.bytes,
            "sha256": inspection.sha256,
            "sector_size": 512,
            "partitions": [partition.to_dict() for partition in inspection.partitions],
        },
        "rootfs_contract": {"verified": True, "method": "debugfs-rdump-read-only"},
    }
    verification_path.write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return artifact, release_path, verification_path


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    repository = _repository_root()
    parser = argparse.ArgumentParser(
        prog="build-recorder-image",
        description="Build the generic preinstalled Pi 4 recorder image.",
    )
    parser.add_argument(
        "--check", action="store_true", help="validate without building"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository / _RECORDER_IMAGE / "build.json",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=repository / "build/recorder-product-image",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=repository / "dist/releases"
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.check:
            manifest = load_build_manifest(arguments.manifest)
            revision = _require_clean_versioned_source(repository)
            check_build_host()
            print(f"recorder image inputs ready: {manifest.release_version} from {revision}")
            return 0
        artifact, release, verification = build_recorder_image(
            repository, arguments.manifest, arguments.work_dir, arguments.output_dir
        )
    except RecorderImageBuildError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    print(f"Image: {artifact}")
    print(f"Development release manifest: {release}")
    print(f"Image verification: {verification}")
    print("The image is unsigned and must not be promoted as a product release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
