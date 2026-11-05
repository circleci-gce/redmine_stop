"""Offline tests of production metadata boundaries and cleanup failure paths."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "issue5505_iap_ssh.py"
SPEC = importlib.util.spec_from_file_location("iap_trial", MODULE)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class FakeCompute:
    def __init__(self, newline=""):
        self.values = {"ssh-keys": "tomonori:ssh-ed25519 T0xE original" + newline,
                       "startup-script": "keep this exact value\n", "other": "preserve"}
        self.version = 1
        self.posts = []
        self.operations = {}
        self.conflict_once = False
        self.drop_response_once = False
        self.reject = False
        self.id = m.INSTANCE_ID

    def api(self, method, path, body=None):
        if method == "GET" and path == m.PROJECT_PATH:
            return {"commonInstanceMetadata": {"items": [{"key": "ssh-keys", "value":
                    "centos:ssh-rsa UFJPSkVDVA== existing project key"}]}}
        if method == "GET" and path == m.VM_PATH:
            return {"id": self.id, "status": "RUNNING", "metadata": {
                "fingerprint": str(self.version), "items": [
                    {"key": k, "value": v} for k, v in self.values.items()]}}
        if method == "GET" and path.startswith(m.ZONE_PATH + "/operations/"):
            return next(op for op in self.operations.values() if op["name"] == path.rsplit("/", 1)[1])
        if method != "POST" or not path.startswith(m.VM_PATH + "/setMetadata?requestId="):
            raise AssertionError("An out-of-scope API operation was attempted")
        self.posts.append((path, copy.deepcopy(body)))
        request_id = path.split("requestId=", 1)[1]
        if request_id in self.operations:
            return copy.deepcopy(self.operations[request_id])
        if self.reject:
            raise m.ApiError(403)
        if self.conflict_once:
            self.conflict_once = False
            self.values["ssh-keys"] += "\nother:ssh-ed25519 Q09OQ1VSUkVOVA== concurrent\n"
            self.values["other"] = "changed concurrently"
            self.version += 1
            raise m.ApiError(412)
        if body["fingerprint"] != str(self.version):
            raise m.ApiError(412)
        self.values = {item["key"]: item["value"] for item in body["items"]}
        self.version += 1
        operation = {"name": "operation-" + request_id, "targetId": m.INSTANCE_ID, "status": "DONE"}
        self.operations[request_id] = operation
        if self.drop_response_once:
            self.drop_response_once = False
            raise m.Stop("Response lost after the server accepted the request")
        return copy.deepcopy(operation)


class MetadataSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.trial = m.Trial(self.directory.name)
        # Synthetic public material; no private key or cloud credentials are used.
        Path(self.directory.name, "id_rsa.pub").write_text("ssh-rsa VFJJQUw= synthetic-test-key\n")
        self.cloud = FakeCompute()
        self.trial.api = self.cloud.api
        self.addCleanup(patch.stopall)
        patch.object(m, "run", return_value=subprocess.CompletedProcess([], 0, "", "")).start()
        patch.object(m.time, "sleep").start()
        patch.object(m.urllib.request, "build_opener", side_effect=AssertionError("Network forbidden in tests")).start()
        self.sink = io.StringIO()
        self.output = redirect_stdout(self.sink)
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_round_trip_preserves_exact_original_metadata_and_20_minute_expiry(self):
        for suffix in ("", "\n", "\r\n"):
            with self.subTest(newline=repr(suffix)):
                self.cloud = FakeCompute(suffix)
                self.trial = m.Trial(self.directory.name)
                self.trial.api = self.cloud.api
                before = copy.deepcopy(self.cloud.values)
                start = m.dt.datetime.now(m.dt.timezone.utc)
                self.trial.register_key()
                added = self.cloud.values["ssh-keys"].splitlines()[-1]
                detail = json.loads(added.split(" google-ssh ", 1)[1])
                delta = (m.dt.datetime.strptime(detail["expireOn"], "%Y-%m-%dT%H:%M:%S%z") - start).total_seconds()
                self.assertTrue(1198 <= delta <= 1201)
                self.assertEqual(detail["userName"], "centos")
                self.trial.remove_key()
                self.trial.verify_instance_keys()
                self.assertEqual(self.cloud.values, before)

    def test_addition_retries_conflict_and_preserves_new_foreign_key(self):
        self.cloud.conflict_once = True
        self.trial.register_key()
        self.trial.remove_key()
        self.trial.verify_instance_keys()
        self.assertIn("concurrent", self.cloud.values["ssh-keys"])
        self.assertEqual(self.cloud.values["other"], "changed concurrently")
        self.assertEqual(len(self.cloud.posts), 3)
        self.assertNotEqual(self.cloud.posts[0][0], self.cloud.posts[1][0])

    def test_cleanup_preserves_keys_and_metadata_added_during_trial(self):
        self.trial.register_key()
        self.cloud.values["ssh-keys"] += "new:ssh-rsa TkVX new unrelated key\n"
        self.cloud.values["other"] = "new unrelated setting"
        self.cloud.version += 1
        self.trial.remove_key()
        self.assertIn("tomonori:", self.cloud.values["ssh-keys"])
        self.assertIn("new:ssh-rsa", self.cloud.values["ssh-keys"])
        self.assertNotIn(self.trial.blob, self.cloud.values["ssh-keys"])
        self.assertEqual(self.cloud.values["other"], "new unrelated setting")

    def test_cleanup_conflict_reads_fresh_snapshot(self):
        self.trial.register_key()
        self.cloud.conflict_once = True
        self.trial.remove_key()
        self.trial.verify_instance_keys()
        self.assertIn("concurrent", self.cloud.values["ssh-keys"])
        self.assertEqual(self.cloud.values["other"], "changed concurrently")

    def test_lost_registration_response_is_resolved_before_removal(self):
        before = copy.deepcopy(self.cloud.values)
        self.cloud.drop_response_once = True
        with self.assertRaises(m.Stop):
            self.trial.register_key()
        self.assertIsNotNone(self.trial.pending)
        self.trial.remove_key()
        self.assertEqual(self.cloud.posts[0], self.cloud.posts[1])  # Same idempotency request.
        self.assertEqual(self.cloud.values, before)
        self.assertIsNone(self.trial.pending)

    def test_lost_removal_response_is_resolved_without_touching_other_keys(self):
        before = copy.deepcopy(self.cloud.values)
        self.trial.register_key()
        self.cloud.drop_response_once = True
        with self.assertRaises(m.Stop):
            self.trial.remove_key()
        self.trial.remove_key()
        self.assertEqual(self.cloud.posts[1], self.cloud.posts[2])
        self.assertEqual(self.cloud.values, before)

    def test_unknown_pending_request_never_reports_cleanup_pass(self):
        self.cloud.drop_response_once = True
        with self.assertRaises(m.Stop):
            self.trial.register_key()
        self.trial.api = lambda *args: (_ for _ in ()).throw(m.Stop("Network unavailable"))
        with self.assertRaises(m.Stop):
            self.trial.remove_key()
        self.assertNotIn("TEMPORARY_SSH_KEY_REMOVED=PASS", self.sink.getvalue())

    def test_wrong_instance_id_prevents_all_writes(self):
        self.cloud.id = "different-instance"
        with self.assertRaises(m.Stop):
            self.trial.register_key()
        self.assertEqual(self.cloud.posts, [])

    def test_oslogin_enabled_prevents_all_writes(self):
        self.cloud.values["enable-oslogin"] = "TRUE"
        with self.assertRaises(m.Stop):
            self.trial.register_key()
        self.assertEqual(self.cloud.posts, [])

    def test_rejected_write_does_not_change_existing_keys(self):
        before = copy.deepcopy(self.cloud.values)
        self.cloud.reject = True
        with self.assertRaises(m.ApiError):
            self.trial.register_key()
        self.trial.remove_key()
        self.assertEqual(self.cloud.values, before)

    def test_cleanup_detects_loss_of_a_preexisting_key(self):
        self.trial.register_key()
        self.cloud.values["ssh-keys"] = self.trial.key_line + "\n"
        self.trial.remove_key()
        with self.assertRaises(m.Stop):
            self.trial.verify_instance_keys()

    def test_ssh_failure_still_removes_temporary_key_and_local_files(self):
        before = copy.deepcopy(self.cloud.values)
        used = []

        def initialize(trial):
            trial.api = self.cloud.api
            trial.project_before = self.cloud.api("GET", m.PROJECT_PATH)
            used.append(trial.work)
            (trial.work / "id_rsa").write_text("synthetic private file for deletion test")
            (trial.work / "id_rsa.pub").write_text("ssh-rsa VFJJQUw= test\n")

        with patch.object(m.Trial, "initialize", initialize), \
             patch.object(m.Trial, "start_tunnel"), \
             patch.object(m.Trial, "ssh_check", side_effect=m.Stop("Simulated SSH failure")), \
             patch.object(m.signal, "signal"), \
             patch.object(m.os, "umask"), \
             patch.object(m.sys, "argv", [str(MODULE), "--run"]):
            self.assertEqual(m.main(), 1)
        self.assertEqual(self.cloud.values, before)
        self.assertTrue(all(not path.exists() for path in used))
        self.assertNotIn("IAP_AUTHENTICATED_TRIAL=PASS", self.sink.getvalue())

    def test_cleanup_failure_fails_trial_and_reports_public_fingerprint(self):
        used = []

        def initialize(trial):
            trial.api = self.cloud.api
            trial.project_before = self.cloud.api("GET", m.PROJECT_PATH)
            used.append(trial.work)
            (trial.work / "id_rsa").write_text("synthetic private file")
            (trial.work / "id_rsa.pub").write_text("ssh-rsa VFJJQUw= test\n")

        def deny_cleanup(trial):
            self.cloud.reject = True

        with patch.object(m.Trial, "initialize", initialize), \
             patch.object(m.Trial, "start_tunnel"), \
             patch.object(m.Trial, "ssh_check", deny_cleanup), \
             patch.object(m.signal, "signal"), \
             patch.object(m.os, "umask"), \
             patch.object(m.sys, "argv", [str(MODULE), "--run"]):
            self.assertEqual(m.main(), 1)
        self.assertTrue(all(not path.exists() for path in used))
        self.assertIn("MANUAL_CLEANUP_KEY_FINGERPRINT=SHA256:", self.sink.getvalue())
        self.assertNotIn("IAP_AUTHENTICATED_TRIAL=PASS", self.sink.getvalue())
        self.assertNotIn("synthetic private file", self.sink.getvalue())

    def test_wrong_ci_identity_stops_before_token_or_metadata_access(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[1:3] == ["config", "get"]:
                return subprocess.CompletedProcess(args, 0, "(unset)\n", "")
            if args[1:3] == ["auth", "list"]:
                return subprocess.CompletedProcess(args, 0, "unexpected@example.com\n", "")
            raise AssertionError("Credential command should not be reached")

        with patch.dict(m.os.environ, {"CIRCLECI": "true", "CIRCLE_BRANCH": m.BRANCH}, clear=True), \
             patch.object(m, "run", fake_run):
            with self.assertRaises(m.Stop):
                self.trial.initialize()
        self.assertIsNone(self.trial.token)
        self.assertEqual(self.cloud.posts, [])

    def test_host_key_mismatch_stops_before_registration(self):
        class Process:
            def poll(self):
                return None

        def popen(args, **kwargs):
            kwargs["stdout"].write("Listening on port [22022].\n")
            kwargs["stdout"].flush()
            return Process()

        with patch.object(m.subprocess, "Popen", popen), \
             patch.object(m, "run", return_value=subprocess.CompletedProcess(
                 [], 0, "[127.0.0.1]:22022 ssh-ed25519 V1JPTkc=\n", "")):
            with self.assertRaises(m.Stop):
                self.trial.start_tunnel()
        self.trial.tunnel_log.close()
        self.assertFalse(self.trial.attempted)
        self.assertEqual(self.cloud.posts, [])

    def test_non_iap_source_address_cannot_pass(self):
        class Process:
            def poll(self):
                return None
        self.trial.tunnel = Process()
        output = "SSH_CONNECTION=192.0.2.1 1234 10.142.0.9 22\n"
        output += "ip protocol tcp ip hdrlength > 5 counter drop\n"
        with patch.object(m, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaises(m.Stop):
                self.trial.ssh_check()
        self.assertNotIn("IAP_SSH_SUDO_CHECK=PASS", self.sink.getvalue())


if __name__ == "__main__":
    unittest.main()
