from __future__ import annotations

import gzip
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from agent_runtime.security import (
    ArchiveSecurityError,
    SafeArchiveConfig,
    SafeArchiveExtractor,
)


def _write_tar_gz(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with tarfile.open(path, mode="w:gz") as tar:
        for info, data in members:
            if data is None:
                tar.addfile(info)
                continue
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _regular(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), data


class SafeArchiveExtractorTests(unittest.TestCase):
    def test_extracts_nested_regular_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "source.tar.gz"
            _write_tar_gz(
                archive,
                [
                    _regular("main.tex", b"\\documentclass{article}\n"),
                    _regular("figures/data.txt", b"safe"),
                ],
            )
            destination = root / "out"
            extracted = SafeArchiveExtractor().extract_tar_gzip(archive, destination)
            self.assertEqual((destination / "main.tex").read_bytes(), b"\\documentclass{article}\n")
            self.assertEqual((destination / "figures" / "data.txt").read_bytes(), b"safe")
            self.assertEqual(len(extracted), 2)

    def test_parent_traversal_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "bad.tar.gz"
            _write_tar_gz(archive, [_regular("../escape.txt", b"owned")])
            with self.assertRaises(ArchiveSecurityError):
                SafeArchiveExtractor().extract_tar_gzip(archive, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_backslash_parent_traversal_is_rejected_for_windows_archives(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "bad.tar.gz"
            _write_tar_gz(archive, [_regular(r"..\escape.txt", b"owned")])
            with self.assertRaises(ArchiveSecurityError):
                SafeArchiveExtractor().extract_tar_gzip(archive, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_absolute_and_drive_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("/absolute.txt", "C:/outside.txt"):
                archive = root / ("bad_" + str(abs(hash(name))) + ".tar.gz")
                _write_tar_gz(archive, [_regular(name, b"x")])
                with self.assertRaises(ArchiveSecurityError):
                    SafeArchiveExtractor().extract_tar_gzip(archive, root / "out")

    def test_symlink_and_hardlink_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info = tarfile.TarInfo("link")
                info.type = member_type
                info.linkname = "../outside"
                archive = root / ("bad_" + str(member_type[0]) + ".tar.gz")
                _write_tar_gz(archive, [(info, None)])
                with self.assertRaises(ArchiveSecurityError):
                    SafeArchiveExtractor().extract_tar_gzip(archive, root / "out")

    def test_fifo_or_device_like_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            info = tarfile.TarInfo("pipe")
            info.type = tarfile.FIFOTYPE
            archive = root / "fifo.tar.gz"
            _write_tar_gz(archive, [(info, None)])
            with self.assertRaises(ArchiveSecurityError):
                SafeArchiveExtractor().extract_tar_gzip(archive, root / "out")

    def test_member_count_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "many.tar.gz"
            _write_tar_gz(archive, [_regular("a", b"1"), _regular("b", b"2")])
            extractor = SafeArchiveExtractor(
                SafeArchiveConfig(max_members=1, max_total_bytes=32, max_file_bytes=16)
            )
            with self.assertRaises(ArchiveSecurityError):
                extractor.extract_tar_gzip(archive, root / "out")

    def test_total_and_per_file_size_limits_are_enforced_before_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "large.tar.gz"
            _write_tar_gz(archive, [_regular("large.tex", b"x" * 20)])
            extractor = SafeArchiveExtractor(
                SafeArchiveConfig(max_total_bytes=16, max_file_bytes=16)
            )
            with self.assertRaises(ArchiveSecurityError):
                extractor.extract_tar_gzip(archive, root / "out")
            self.assertFalse((root / "out" / "large.tex").exists())

    def test_single_gzip_is_streamed_with_size_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "single.gz"
            archive.write_bytes(gzip.compress(b"x" * 100))
            target = root / "main.tex"
            extractor = SafeArchiveExtractor(
                SafeArchiveConfig(max_total_bytes=32, max_file_bytes=32)
            )
            with self.assertRaises(ArchiveSecurityError):
                extractor.inflate_single_gzip(archive, target)
            self.assertFalse(target.exists())

    def test_windows_reserved_device_and_ads_components_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("NUL.tex", "paper.tex:secret"):
                archive = root / ("bad_" + str(abs(hash(name))) + ".tar.gz")
                _write_tar_gz(archive, [_regular(name, b"x")])
                with self.assertRaises(ArchiveSecurityError):
                    SafeArchiveExtractor().extract_tar_gzip(archive, root / "out")


if __name__ == "__main__":
    unittest.main()
