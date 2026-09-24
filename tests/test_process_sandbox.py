from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from agent_runtime.security import ProcessSandbox, ProcessSandboxConfig


class ProcessSandboxTests(unittest.TestCase):
    def test_config_validation(self):
        with self.assertRaises(ValueError):
            ProcessSandboxConfig(memory_limit_mb=32)
        with self.assertRaises(ValueError):
            ProcessSandboxConfig(cpu_time_seconds=0)
        with self.assertRaises(ValueError):
            ProcessSandboxConfig(max_processes=0)

    def test_capability_report_is_explicit(self):
        sandbox = ProcessSandbox()
        caps = sandbox.capabilities
        payload = caps.to_dict()
        self.assertIn(caps.backend, {"windows-job-object", "posix-prlimit", "posix-basic"})
        self.assertTrue(caps.process_tree_termination)
        self.assertFalse(caps.network_isolation)
        self.assertFalse(caps.filesystem_isolation)
        self.assertEqual(payload["backend"], caps.backend)

    def test_strict_network_requirement_fails_closed(self):
        sandbox = ProcessSandbox(
            ProcessSandboxConfig(require_network_isolation=True)
        )
        with self.assertRaisesRegex(RuntimeError, "network isolation is required"):
            sandbox.validate_requirements()

    def test_strict_filesystem_requirement_fails_closed(self):
        sandbox = ProcessSandbox(
            ProcessSandboxConfig(require_filesystem_isolation=True)
        )
        with self.assertRaisesRegex(RuntimeError, "filesystem isolation is required"):
            sandbox.validate_requirements()

    def test_handle_terminates_process_tree_root(self):
        sandbox = ProcessSandbox(
            ProcessSandboxConfig(memory_limit_mb=256, cpu_time_seconds=5, max_processes=4)
        )
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )
        handle = sandbox.attach(proc)
        try:
            handle.terminate()
            proc.communicate(timeout=3)
        finally:
            handle.close()
        self.assertIsNotNone(proc.returncode)

    @unittest.skipIf(os.name == "nt", "POSIX prlimit readback test")
    def test_posix_memory_limit_is_applied_to_child(self):
        sandbox = ProcessSandbox(
            ProcessSandboxConfig(memory_limit_mb=128, cpu_time_seconds=5, max_processes=4)
        )
        if not sandbox.capabilities.hard_memory_limit:
            self.skipTest("hard memory limit unavailable on this backend")
        import resource

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        handle = sandbox.attach(proc)
        try:
            soft, hard = resource.prlimit(proc.pid, resource.RLIMIT_AS)
            expected = 128 * 1024 * 1024
            self.assertEqual((soft, hard), (expected, expected))
        finally:
            handle.terminate()
            proc.wait(timeout=3)
            handle.close()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object memory test")
    def test_windows_job_memory_limit_blocks_large_allocation(self):
        sandbox = ProcessSandbox(
            ProcessSandboxConfig(memory_limit_mb=128, cpu_time_seconds=5, max_processes=4)
        )
        code = (
            "import time\n"
            "time.sleep(0.25)\n"
            "try:\n"
            "    x = bytearray(512 * 1024 * 1024)\n"
            "    print('ALLOCATED')\n"
            "except MemoryError:\n"
            "    print('MEMORY_LIMITED')\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        handle = sandbox.attach(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        finally:
            handle.close()
        self.assertNotIn("ALLOCATED", output or "")
        self.assertTrue(
            "MEMORY_LIMITED" in (output or "") or proc.returncode not in (0, None)
        )

    @unittest.skipUnless(os.name == "nt", "Windows Job Object capability")
    def test_windows_job_object_reports_process_count_limit(self):
        self.assertTrue(ProcessSandbox().capabilities.hard_process_count_limit)

    @unittest.skipIf(os.name == "nt", "POSIX backend capability")
    def test_posix_backend_does_not_overclaim_process_count_limit(self):
        self.assertFalse(ProcessSandbox().capabilities.hard_process_count_limit)


if __name__ == "__main__":
    unittest.main()
