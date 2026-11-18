"""Platform-neutral safety checks for developer and release workflows.

Customer- or organization-specific rules are deliberately supplied at run time
through an external JSON policy.  This module contains only generic checks.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence


POLICY_ENVIRONMENT_VARIABLE = "VMS_DOCTOR_POLICY"


@dataclass(frozen=True, slots=True)
class Finding:
    """One machine-readable doctor result."""

    code: str
    message: str
    severity: str = "error"
    subject: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.subject is not None:
            result["subject"] = self.subject
        return result


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Complete result returned to either a GUI or CLI adapter."""

    repository: str
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "repository": self.repository,
            "findings": [finding.to_dict() for finding in self.findings],
        }


_POLICY_SCOPES = (
    "repository_path",
    "git_remote",
    "git_identity",
    "tracked_path",
    "tracked_content",
)
_PRIVATE_KEY_MARKER = "-----BEGIN " + "PRIVATE KEY-----"
_PRIVATE_KEY_VARIANT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?im)(?:^|[,{\s])['\"]?"
    r"(?:password|passwd|secret|access[_-]?token|api[_-]?key|private[_-]?key)"
    r"['\"]?\s*[:=]\s*['\"]([^'\"\r\n]{8,})['\"]"
)
_SENSITIVE_FILE_NAMES = (
    re.compile(r"^\.env$", re.IGNORECASE),
    re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)$", re.IGNORECASE),
    re.compile(r"^(?:credentials|secrets?)(?:\.[^.]+)?$", re.IGNORECASE),
    re.compile(r".+\.(?:p12|pfx)$", re.IGNORECASE),
)
_PROVISIONING_NAME = re.compile(
    r"(?:provision|factory|flash(?:er|ing)?|image[_-]?builder)", re.IGNORECASE
)
_RELEASE_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PLACEHOLDERS = (
    "change-me",
    "changeme",
    "example",
    "placeholder",
    "prompt",
    "redacted",
    "replace-me",
    "unset",
)
_MAX_TEXT_FILE_BYTES = 8 * 1024 * 1024
_RELEASE_MANIFEST_CANDIDATES = (
    "release-manifest.json",
    "release_manifest.json",
    "metadata/release-manifest.json",
)


def run_doctor(
    repository: str | os.PathLike[str] = ".",
    *,
    policy_path: str | os.PathLike[str] | None = None,
    release_manifest_path: str | os.PathLike[str] | None = None,
    provisioning_tool_paths: Iterable[str | os.PathLike[str]] = (),
) -> DoctorReport:
    """Run non-mutating safety checks and return a structured report.

    ``policy_path`` defaults to the path in ``VMS_DOCTOR_POLICY``.  A policy may
    contain ``forbidden_patterns`` (a mapping keyed by the documented scopes),
    ``provisioning_tool_paths`` (a list), and ``release_manifest_path``.
    Organization-specific names, domains, and repository fragments belong only
    in that external file.
    """

    requested_path = Path(repository).expanduser().resolve()
    findings: list[Finding] = []
    root, discovery_finding = _discover_git_repository(requested_path)
    if root is None:
        return DoctorReport(
            repository=str(requested_path),
            findings=(discovery_finding,),
        )

    selected_policy_path = policy_path or os.environ.get(POLICY_ENVIRONMENT_VARIABLE)
    policy: Mapping[str, Any] = {}
    if selected_policy_path:
        policy, policy_findings = _load_policy(Path(selected_policy_path), root)
        findings.extend(policy_findings)

    compiled_policy, pattern_findings = _compile_forbidden_patterns(policy)
    findings.extend(pattern_findings)
    _check_forbidden(
        compiled_policy.get("repository_path", ()),
        str(root),
        "policy.repository_path",
        findings,
    )

    remotes_result = _run_git(root, "remote", "-v")
    if remotes_result.returncode != 0:
        findings.append(
            Finding(
                "git.remotes_unavailable",
                "Git remote configuration could not be inspected.",
                subject=str(root),
            )
        )
    remotes = _completed_lines(remotes_result)
    for remote in remotes:
        _check_forbidden(
            compiled_policy.get("git_remote", ()),
            remote,
            "policy.git_remote",
            findings,
        )

    identity_parts = []
    for key in ("user.name", "user.email"):
        identity_result = _run_git(root, "config", "--get", key)
        if identity_result.returncode not in (0, 1):
            findings.append(
                Finding(
                    "git.identity_unavailable",
                    "Git identity configuration could not be inspected.",
                    subject=key,
                )
            )
        value = _completed_value(identity_result)
        if value:
            identity_parts.append(value)
    if not identity_parts:
        findings.append(
            Finding(
                "git.identity_missing",
                "Git user.name and user.email are not configured.",
                severity="warning",
            )
        )
    identity = " ".join(identity_parts)
    _check_forbidden(
        compiled_policy.get("git_identity", ()),
        identity,
        "policy.git_identity",
        findings,
    )

    repository_paths = _repository_paths(root, findings)
    for relative_path in repository_paths:
        normalized_path = relative_path.as_posix()
        _check_forbidden(
            compiled_policy.get("tracked_path", ()),
            normalized_path,
            "policy.tracked_path",
            findings,
        )
        absolute_path = root / relative_path
        safe_to_read = _check_repository_path(
            root, absolute_path, normalized_path, findings
        )
        if not safe_to_read:
            continue
        text = _read_repository_text_file(absolute_path, normalized_path, findings)
        if text is None:
            continue
        _check_forbidden(
            compiled_policy.get("tracked_content", ()),
            text,
            "policy.tracked_content",
            findings,
            subject=normalized_path,
        )
        _check_secret_content(text, normalized_path, findings)

    declared_tools: list[str | os.PathLike[str]] = list(provisioning_tool_paths)
    declared_tools.extend(_policy_string_list(policy, "provisioning_tool_paths", findings))
    _check_provisioning_tools(root, declared_tools, findings)

    configured_manifest = release_manifest_path
    if configured_manifest is None:
        configured_manifest = _policy_optional_string(
            policy, "release_manifest_path", findings
        )
    _check_release_manifest(root, configured_manifest, findings)

    return DoctorReport(repository=str(root), findings=tuple(_deduplicate(findings)))


def _discover_git_repository(path: Path) -> tuple[Path | None, Finding]:
    completed = _run_git(path, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        if completed.returncode == 127:
            return None, Finding(
                "git.unavailable",
                "Git is unavailable, so repository safety checks cannot run.",
                subject=str(path),
            )
        return None, Finding(
            "git.not_repository",
            "The selected directory is not inside an accessible Git repository.",
            subject=str(path),
        )
    raw_root = completed.stdout.strip()
    if not raw_root:
        return None, Finding(
            "git.repository_state_unavailable",
            "Git returned no repository root, so safety checks cannot run.",
            subject=str(path),
        )
    try:
        root = Path(raw_root).resolve(strict=True)
    except OSError:
        return None, Finding(
            "git.repository_state_unavailable",
            "The Git repository root cannot be resolved.",
            subject=raw_root,
        )
    if not root.is_dir():
        return None, Finding(
            "git.repository_state_unavailable",
            "The Git repository root is not an accessible directory.",
            subject=str(root),
        )
    return root, Finding("git.ok", "Git repository is accessible.", severity="info")


def _run_git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(
            ["git", *arguments], returncode=127, stdout="", stderr=str(error)
        )


def _completed_lines(completed: subprocess.CompletedProcess[str]) -> list[str]:
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _completed_value(completed: subprocess.CompletedProcess[str]) -> str | None:
    lines = _completed_lines(completed)
    return lines[0] if lines else None


def _repository_paths(root: Path, findings: list[Finding]) -> tuple[Path, ...]:
    """Return tracked and untracked files after Git's standard ignores."""

    completed = _run_git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    if completed.returncode != 0:
        findings.append(
            Finding(
                "git.file_inventory_unavailable",
                "Repository files could not be enumerated; content checks did not run.",
                subject=str(root),
            )
        )
        return ()
    result: list[Path] = []
    for item in completed.stdout.split("\0"):
        if not item:
            continue
        relative = Path(item)
        if relative.is_absolute() or ".." in relative.parts:
            findings.append(
                Finding(
                    "git.file_inventory_invalid",
                    "Git returned a repository path that cannot be inspected safely.",
                    subject=item,
                )
            )
            continue
        result.append(relative)
    return tuple(result)


def _load_policy(path: Path, root: Path) -> tuple[Mapping[str, Any], list[Finding]]:
    findings: list[Finding] = []
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        return {}, [
            Finding(
                "policy.not_found",
                "The external doctor policy does not exist.",
                subject=str(resolved_path),
            )
        ]
    try:
        value = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {}, [
            Finding(
                "policy.invalid",
                f"The external doctor policy is not valid JSON: {error}",
                subject=str(resolved_path),
            )
        ]
    if not isinstance(value, dict):
        return {}, [
            Finding(
                "policy.invalid",
                "The external doctor policy must contain a JSON object.",
                subject=str(resolved_path),
            )
        ]
    if _is_within(resolved_path, root):
        findings.append(
            Finding(
                "policy.inside_repository",
                "The environment-specific doctor policy must be stored outside the repository.",
                subject=resolved_path.relative_to(root).as_posix(),
            )
        )
    return value, findings


def _compile_forbidden_patterns(
    policy: Mapping[str, Any],
) -> tuple[dict[str, tuple[re.Pattern[str], ...]], list[Finding]]:
    findings: list[Finding] = []
    raw = policy.get("forbidden_patterns", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return {}, [
            Finding(
                "policy.invalid_forbidden_patterns",
                "forbidden_patterns must be an object of scope-to-pattern lists.",
            )
        ]
    unknown_scopes = sorted(set(raw) - set(_POLICY_SCOPES))
    for scope in unknown_scopes:
        findings.append(
            Finding(
                "policy.unknown_scope",
                "The policy contains an unsupported forbidden-pattern scope.",
                subject=str(scope),
            )
        )
    compiled: dict[str, tuple[re.Pattern[str], ...]] = {}
    for scope in _POLICY_SCOPES:
        values = raw.get(scope, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            findings.append(
                Finding(
                    "policy.invalid_scope",
                    "A forbidden-pattern scope must contain a list of strings.",
                    subject=scope,
                )
            )
            continue
        patterns: list[re.Pattern[str]] = []
        for value in values:
            try:
                patterns.append(re.compile(value, re.IGNORECASE))
            except re.error as error:
                findings.append(
                    Finding(
                        "policy.invalid_pattern",
                        f"A forbidden pattern is not a valid regular expression: {error}",
                        subject=scope,
                    )
                )
        compiled[scope] = tuple(patterns)
    return compiled, findings


def _check_forbidden(
    patterns: Sequence[re.Pattern[str]],
    value: str,
    code: str,
    findings: list[Finding],
    *,
    subject: str | None = None,
) -> None:
    if value and any(pattern.search(value) for pattern in patterns):
        findings.append(
            Finding(
                code,
                "An external policy forbids this value in the public project.",
                subject=subject or value,
            )
        )


def _check_repository_path(
    root: Path, absolute_path: Path, relative_path: str, findings: list[Finding]
) -> bool:
    name = absolute_path.name
    if any(pattern.fullmatch(name) for pattern in _SENSITIVE_FILE_NAMES):
        findings.append(
            Finding(
                "secret.sensitive_filename",
                "A likely credential file is present in the repository work tree.",
                subject=relative_path,
            )
        )
    try:
        resolved_path = absolute_path.resolve(strict=False)
    except OSError:
        findings.append(
            Finding(
                "repository.path_unreadable",
                "A repository path cannot be resolved safely.",
                subject=relative_path,
            )
        )
        return False
    if not _is_within(resolved_path, root):
        findings.append(
            Finding(
                "repository.symlink_escape",
                "A repository path resolves outside the repository.",
                subject=relative_path,
            )
        )
        if _PROVISIONING_NAME.search(relative_path):
            findings.append(
                Finding(
                    "provisioning.outside_repository",
                    "A provisioning tool resolves outside the repository.",
                    subject=relative_path,
                )
            )
        return False
    return True


def _read_repository_text_file(
    path: Path, relative_path: str, findings: list[Finding]
) -> str | None:
    try:
        if path.is_symlink():
            return None
        if not path.is_file():
            findings.append(
                Finding(
                    "repository.file_unavailable",
                    "A repository file disappeared or is not readable as a file.",
                    subject=relative_path,
                )
            )
            return None
        size = path.stat().st_size
        if size > _MAX_TEXT_FILE_BYTES:
            findings.append(
                Finding(
                    "repository.file_too_large",
                    "A repository file is too large for a complete content safety scan.",
                    subject=relative_path,
                )
            )
            return None
        data = path.read_bytes()
    except OSError:
        findings.append(
            Finding(
                "repository.file_unreadable",
                "A repository file could not be read for content safety checks.",
                subject=relative_path,
            )
        )
        return None
    # Replacement decoding preserves ASCII policy and secret markers even in a
    # mixed or binary file.  Files above the bound fail closed instead of being
    # silently skipped.
    return data.decode("utf-8", errors="replace")


def _check_secret_content(text: str, path: str, findings: list[Finding]) -> None:
    if _PRIVATE_KEY_MARKER in text or _PRIVATE_KEY_VARIANT.search(text):
        findings.append(
            Finding(
                "secret.private_key",
                "Private-key material is present in the repository work tree.",
                subject=path,
            )
        )
    for match in _SENSITIVE_ASSIGNMENT.finditer(text):
        candidate = match.group(1).strip().lower()
        if candidate.startswith(("${", "{{", "<")):
            continue
        if any(placeholder in candidate for placeholder in _PLACEHOLDERS):
            continue
        findings.append(
            Finding(
                "secret.assignment",
                "A tracked file contains a likely embedded credential.",
                subject=path,
            )
        )
        break


def _check_provisioning_tools(
    root: Path,
    declared_tools: Iterable[str | os.PathLike[str]],
    findings: list[Finding],
) -> None:
    for declared in declared_tools:
        raw_path = Path(declared).expanduser()
        path = raw_path if raw_path.is_absolute() else root / raw_path
        resolved = path.resolve()
        if not _is_within(resolved, root):
            findings.append(
                Finding(
                    "provisioning.outside_repository",
                    "A declared provisioning tool is outside the repository.",
                    subject=str(resolved),
                )
            )
        elif not resolved.exists():
            findings.append(
                Finding(
                    "provisioning.not_found",
                    "A declared provisioning tool does not exist.",
                    subject=str(raw_path),
                )
            )


def _check_release_manifest(
    root: Path,
    configured_path: str | os.PathLike[str] | None,
    findings: list[Finding],
) -> None:
    manifest_path: Path | None = None
    explicitly_selected = configured_path is not None
    if configured_path is not None:
        candidate = Path(configured_path).expanduser()
        manifest_path = candidate if candidate.is_absolute() else root / candidate
    else:
        for relative in _RELEASE_MANIFEST_CANDIDATES:
            candidate = root / relative
            if candidate.is_file():
                manifest_path = candidate
                break
    if manifest_path is None:
        findings.append(
            Finding(
                "release.not_configured",
                "No release manifest was selected; release checks were skipped.",
                severity="info",
            )
        )
        return
    if not manifest_path.is_file():
        if explicitly_selected:
            findings.append(
                Finding(
                    "release.manifest_missing",
                    "The configured release manifest does not exist.",
                    subject=str(manifest_path),
                )
            )
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        findings.append(
            Finding(
                "release.manifest_invalid",
                f"The release manifest is not valid JSON: {error}",
                subject=str(manifest_path),
            )
        )
        return
    if not isinstance(manifest, dict):
        findings.append(
            Finding(
                "release.manifest_invalid",
                "The release manifest must contain a JSON object.",
                subject=str(manifest_path),
            )
        )
        return
    release_channel = _validate_release_record(
        manifest,
        "release",
        findings,
        require_version=True,
        explicitly_selected=explicitly_selected,
    )
    inputs = manifest.get("inputs")
    if inputs is not None:
        if not isinstance(inputs, list) or not inputs:
            findings.append(
                Finding(
                    "release.inputs_invalid",
                    "Release inputs must be a non-empty list when present.",
                    subject=str(manifest_path),
                )
            )
        else:
            for index, item in enumerate(inputs):
                if not isinstance(item, dict):
                    findings.append(
                        Finding(
                            "release.input_invalid",
                            "Each release input must be an object.",
                            subject=f"inputs[{index}]",
                        )
                    )
                    continue
                _validate_release_record(
                    item,
                    f"inputs[{index}]",
                    findings,
                    require_version=False,
                    explicitly_selected=explicitly_selected,
                    inherited_channel=release_channel,
                )


def _validate_release_record(
    record: Mapping[str, Any],
    subject: str,
    findings: list[Finding],
    *,
    require_version: bool,
    explicitly_selected: bool,
    inherited_channel: str | None = None,
) -> str | None:
    if require_version and not _meaningful_string(record.get("release_version")):
        findings.append(
            Finding(
                "release.version_missing",
                "The release manifest needs a non-placeholder version.",
                subject=subject,
            )
        )
    digest = record.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        findings.append(
            Finding(
                "release.hash_invalid",
                "The release record needs a lowercase 64-character SHA-256 value.",
                subject=subject,
            )
        )

    channel_value = record.get("channel", inherited_channel)
    channel = channel_value if isinstance(channel_value, str) else None
    if channel not in ("development", "product"):
        findings.append(
            Finding(
                "release.channel_invalid",
                "The release channel must be development or product.",
                subject=subject,
            )
        )
        channel = None

    signature_declared = "signature" in record
    signature = record.get("signature")
    if not signature_declared:
        findings.append(
            Finding(
                "release.signature_missing",
                "The release record must declare its signature metadata or an explicit null.",
                subject=subject,
            )
        )
    elif signature is None:
        if channel != "development":
            findings.append(
                Finding(
                    "release.signature_missing",
                    "Only a development release may have a null signature.",
                    subject=subject,
                )
            )
    elif not _valid_release_signature(signature):
        findings.append(
            Finding(
                "release.signature_invalid",
                "A release signature must contain valid Ed25519 algorithm, key_id, and signature fields.",
                subject=subject,
            )
        )

    if require_version and channel == "development":
        findings.append(
            Finding(
                "release.development_non_product",
                "The selected development image is for testing only and is not a product release.",
                severity="info",
                subject=subject,
            )
        )
        if not explicitly_selected:
            findings.append(
                Finding(
                    "release.development_not_explicit",
                    "A development image must be selected explicitly by external configuration.",
                    subject=subject,
                )
            )
    return channel


def _valid_release_signature(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) != {"algorithm", "key_id", "signature"}:
        return False
    if value.get("algorithm") != "ed25519":
        return False
    key_id = value.get("key_id")
    if not isinstance(key_id, str) or _RELEASE_KEY_ID.fullmatch(key_id) is None:
        return False
    encoded = value.get("signature")
    if not isinstance(encoded, str):
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == 64 and base64.b64encode(decoded).decode("ascii") == encoded


def _meaningful_string(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    lowered = value.strip().lower()
    return not any(placeholder in lowered for placeholder in _PLACEHOLDERS)


def _policy_string_list(
    policy: Mapping[str, Any], key: str, findings: list[Finding]
) -> list[str]:
    value = policy.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        findings.append(
            Finding(
                "policy.invalid_value",
                "This policy value must be a list of strings.",
                subject=key,
            )
        )
        return []
    return value


def _policy_optional_string(
    policy: Mapping[str, Any], key: str, findings: list[Finding]
) -> str | None:
    value = policy.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        findings.append(
            Finding(
                "policy.invalid_value",
                "This policy value must be a string.",
                subject=key,
            )
        )
        return None
    return value


def _matches_any_literal_path(path: str, ignored_paths: Sequence[str]) -> bool:
    return any(path == ignored or path.startswith(ignored.rstrip("/") + "/") for ignored in ignored_paths)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _deduplicate(findings: Iterable[Finding]) -> list[Finding]:
    seen: set[tuple[str, str | None, str]] = set()
    result: list[Finding] = []
    for finding in findings:
        key = (finding.code, finding.subject, finding.message)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Return a release-input digest without modifying the input file."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
