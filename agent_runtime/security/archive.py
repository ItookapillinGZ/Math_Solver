from __future__ import annotations

import gzip
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ArchiveSecurityError(ValueError):
    """Raised when an archive is valid enough to inspect but violates policy."""


@dataclass(frozen=True)
class SafeArchiveConfig:
    """Resource and path limits for downloaded research archives."""

    max_members: int = 2_000
    max_total_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    max_path_chars: int = 1_024
    copy_chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_members < 1:
            raise ValueError("max_members must be positive")
        if self.max_total_bytes < 1:
            raise ValueError("max_total_bytes must be positive")
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")
        if self.max_path_chars < 16:
            raise ValueError("max_path_chars is too small")
        if self.copy_chunk_bytes < 1:
            raise ValueError("copy_chunk_bytes must be positive")


class SafeArchiveExtractor:
    """Extract regular tar/gzip content without trusting archive paths.

    Tar extraction is intentionally manual rather than calling ``extractall``.
    Only directories and regular files are accepted. Links, devices, FIFOs,
    absolute paths, traversal components, Windows drive paths, oversized member
    sets, and oversized payloads are rejected before archive data is written.
    """

    def __init__(self, config: SafeArchiveConfig | None = None) -> None:
        self.config = config or SafeArchiveConfig()

    def extract_tar_gzip(self, archive_path: Path, destination: Path) -> list[Path]:
        """Safely extract a gzip-compressed tar archive.

        ``tarfile.ReadError`` is allowed to propagate so callers can distinguish
        "not a tar archive" from ``ArchiveSecurityError`` (a valid-but-unsafe
        archive that must fail closed).
        """

        archive = Path(archive_path)
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)

        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            validated = self._validate_members(members, destination)

            extracted: list[Path] = []
            written_total = 0
            for member, target in validated:
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                self._assert_target_inside(destination, target)
                source = tar.extractfile(member)
                if source is None:
                    raise ArchiveSecurityError(
                        f"archive member cannot be read as a regular file: {member.name!r}"
                    )

                member_written = 0
                with source, target.open("wb") as output:
                    while True:
                        chunk = source.read(self.config.copy_chunk_bytes)
                        if not chunk:
                            break
                        member_written += len(chunk)
                        written_total += len(chunk)
                        if member_written > self.config.max_file_bytes:
                            raise ArchiveSecurityError(
                                f"archive member exceeds per-file limit: {member.name!r}"
                            )
                        if written_total > self.config.max_total_bytes:
                            raise ArchiveSecurityError(
                                "archive exceeds total extracted-size limit"
                            )
                        output.write(chunk)

                if member.size >= 0 and member_written != member.size:
                    raise ArchiveSecurityError(
                        f"archive member size mismatch: {member.name!r}"
                    )
                extracted.append(target)

            return extracted

    def inflate_single_gzip(self, archive_path: Path, destination_file: Path) -> Path:
        """Inflate a non-tar gzip stream with the same per-file/total limit."""

        archive = Path(archive_path)
        target = Path(destination_file).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with gzip.open(archive, "rb") as source, target.open("wb") as output:
                while True:
                    chunk = source.read(self.config.copy_chunk_bytes)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.config.max_file_bytes:
                        raise ArchiveSecurityError(
                            "single gzip payload exceeds per-file limit"
                        )
                    if written > self.config.max_total_bytes:
                        raise ArchiveSecurityError(
                            "single gzip payload exceeds total extracted-size limit"
                        )
                    output.write(chunk)
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        return target

    def _validate_members(
        self,
        members: list[tarfile.TarInfo],
        destination: Path,
    ) -> list[tuple[tarfile.TarInfo, Path]]:
        if len(members) > self.config.max_members:
            raise ArchiveSecurityError(
                f"archive contains {len(members)} members; limit is {self.config.max_members}"
            )

        declared_total = 0
        validated: list[tuple[tarfile.TarInfo, Path]] = []
        seen_targets: set[str] = set()
        for member in members:
            target = self._validated_target(destination, member.name)
            target_key = os.path.normcase(str(target))
            if target_key in seen_targets:
                raise ArchiveSecurityError(
                    f"duplicate archive target is not allowed: {member.name!r}"
                )
            seen_targets.add(target_key)
            if member.isdir():
                validated.append((member, target))
                continue
            if not member.isreg():
                raise ArchiveSecurityError(
                    f"unsupported archive member type for {member.name!r}; "
                    "links, devices, and FIFOs are not allowed"
                )
            if member.size < 0:
                raise ArchiveSecurityError(
                    f"archive member has invalid size: {member.name!r}"
                )
            if member.size > self.config.max_file_bytes:
                raise ArchiveSecurityError(
                    f"archive member exceeds per-file limit: {member.name!r}"
                )
            declared_total += member.size
            if declared_total > self.config.max_total_bytes:
                raise ArchiveSecurityError("archive exceeds total extracted-size limit")
            validated.append((member, target))
        return validated

    def _validated_target(self, destination: Path, member_name: str) -> Path:
        if not member_name or "\x00" in member_name:
            raise ArchiveSecurityError("archive member has an empty or NUL-containing path")
        if len(member_name) > self.config.max_path_chars:
            raise ArchiveSecurityError("archive member path is too long")

        # Tar names are POSIX-like, but malicious archives targeting Windows may
        # use backslashes or drive prefixes. Normalize both before validation.
        normalized = member_name.replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("//"):
            raise ArchiveSecurityError(f"absolute archive path is not allowed: {member_name!r}")
        if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
            raise ArchiveSecurityError(
                f"Windows drive archive path is not allowed: {member_name!r}"
            )

        pure = PurePosixPath(normalized)
        parts = pure.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ArchiveSecurityError(
                f"archive path traversal or ambiguous component is not allowed: {member_name!r}"
            )
        for part in parts:
            if ":" in part:
                raise ArchiveSecurityError(
                    f"colon/alternate-stream archive component is not allowed: {member_name!r}"
                )
            base = part.rstrip(" .").split(".", 1)[0].casefold()
            reserved = {"con", "prn", "aux", "nul"}
            reserved.update(f"com{i}" for i in range(1, 10))
            reserved.update(f"lpt{i}" for i in range(1, 10))
            if base in reserved:
                raise ArchiveSecurityError(
                    f"reserved device archive component is not allowed: {member_name!r}"
                )

        target = destination.joinpath(*parts)
        self._assert_target_inside(destination, target)
        return target

    @staticmethod
    def _assert_target_inside(destination: Path, target: Path) -> None:
        root = destination.resolve()
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ArchiveSecurityError(
                f"archive member escapes destination: {target}"
            )
        # Existing symlinks/junction-like path redirections are caught by the
        # resolve() containment test above. Reject a direct symlink target too.
        if target.exists() and target.is_symlink():
            raise ArchiveSecurityError(f"archive target is a symlink: {target}")
