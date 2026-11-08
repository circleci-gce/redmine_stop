import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issue5505_ansible_diagnostics as d


class AnsibleDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def simulate(self, failing=None, malformed=False, missing_owner=False, combined_failure=False):
        calls = []

        def command(args, **kwargs):
            calls.append((args, kwargs))
            if args[0] != "ansible-doc":
                return subprocess.CompletedProcess(args, 0, "synthetic version\n", "")
            if len(args) == 5 and args[-1] == failing:
                return subprocess.CompletedProcess(args, 1, "", "Unable to load " + failing +
                                                   "; synthetic-secret-719\n")
            if len(args) == 6 and combined_failure:
                return subprocess.CompletedProcess(args, 1, "", "combined query failed\n")
            docs = {name: {"doc": {"options": {"path": {}, "expected_bucket_owner": {}}}}
                    for name in args[4:]}
            if missing_owner and "amazon.aws.s3_object" in docs:
                del docs["amazon.aws.s3_object"]["doc"]["options"]["expected_bucket_owner"]
            return subprocess.CompletedProcess(args, 0, "not json" if malformed else json.dumps(docs), "")

        with patch.dict(os.environ, {"AWS_SESSION_TOKEN": "synthetic-secret-719",
                                     "GCLOUD_SERVICE_KEY": "synthetic-gcp-key",
                                     "ANSIBLE_DEBUG": "true", "ANSIBLE_LOG_PATH": "/tmp/unsafe"}), \
                patch.object(d.subprocess, "run", side_effect=command):
            result = d.check_modules()
        self.assertEqual(len(calls), 6)
        self.assertEqual({args[0] for args, _ in calls}, {"ansible", "ansible-doc", "ansible-galaxy"})
        for args, kwargs in calls:
            self.assertNotIn("AWS_SESSION_TOKEN", kwargs["env"])
            self.assertNotIn("GCLOUD_SERVICE_KEY", kwargs["env"])
            self.assertNotIn("ANSIBLE_LOG_PATH", kwargs["env"])
            self.assertEqual(kwargs["env"]["ANSIBLE_DEBUG"], "false")
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(Path(kwargs["cwd"]).exists())  # Temporary directory cleaned.
        return result

    def test_success_checks_combined_query_both_modules_and_owner_support(self):
        self.assertTrue(self.simulate())
        self.assertIn("ANSIBLE_DOC_COMBINED=PASS", self.output.getvalue())
        self.assertIn("S3_EXPECTED_BUCKET_OWNER_SUPPORTED=YES", self.output.getvalue())
        self.assertIn("ANSIBLE_DEPENDENCY_CHECK=PASS", self.output.getvalue())

    def test_failed_module_prints_diagnostic_and_redacts_credentials(self):
        self.assertFalse(self.simulate(failing="amazon.aws.s3_object"))
        value = self.output.getvalue()
        self.assertIn("Unable to load amazon.aws.s3_object", value)
        self.assertIn("ANSIBLE_MODULE=amazon.aws.s3_object RESULT=FAIL", value)
        self.assertIn("S3_EXPECTED_BUCKET_OWNER_SUPPORTED=UNKNOWN", value)
        self.assertNotIn("synthetic-secret-719", value)
        self.assertNotIn("ANSIBLE_DEPENDENCY_CHECK=PASS", value)

    def test_combined_failure_is_not_bypassed_by_individual_success(self):
        self.assertFalse(self.simulate(combined_failure=True))
        self.assertIn("combined query failed", self.output.getvalue())

    def test_invalid_json_or_missing_owner_check_fails_closed(self):
        for options in ({"malformed": True}, {"missing_owner": True}):
            with self.subTest(options=options):
                self.output.seek(0)
                self.output.truncate(0)
                self.assertFalse(self.simulate(**options))
                expected = "UNKNOWN" if options.get("malformed") else "NO"
                self.assertIn("S3_EXPECTED_BUCKET_OWNER_SUPPORTED=" + expected, self.output.getvalue())

    def test_probe_executes_with_clean_environment_and_retains_error_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "ansible.cfg").write_text("[defaults]\n")
            script = work / "fake_doc.py"
            script.write_text("import os, sys\n"
                              "assert 'AWS_SESSION_TOKEN' not in os.environ\n"
                              "assert 'ANSIBLE_LOG_PATH' not in os.environ\n"
                              "sys.stderr.write('module documentation unavailable\\n')\n"
                              "sys.exit(1)\n")
            with patch.dict(os.environ, {"AWS_SESSION_TOKEN": "fake-token", "ANSIBLE_LOG_PATH": "unsafe"}):
                result = d.probe([sys.executable, str(script)], work)
            self.assertEqual(result.returncode, 1)
        self.assertIn("module documentation unavailable", self.output.getvalue())

    def test_timeout_does_not_dump_captured_output(self):
        with patch.object(d.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                "ansible-doc", 60, output="never-display-this", stderr="nor-this")):
            self.assertIsNone(d.probe(["ansible-doc", "--version"], Path("/unused")))
        self.assertIn("TimeoutExpired", self.output.getvalue())
        self.assertNotIn("never-display-this", self.output.getvalue())

    def test_json_service_account_private_key_is_redacted(self):
        key = "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----"
        with patch.dict(os.environ, {"GCLOUD_SERVICE_KEY": json.dumps({"private_key": key})}):
            for text in (key, json.dumps(key), "Authorization: Bearer synthetic"):
                self.assertNotIn("synthetic", d.safe_output(text))


if __name__ == "__main__":
    unittest.main()
