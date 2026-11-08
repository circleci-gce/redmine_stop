"""Offline failure-path tests: no cloud calls, SSH connections or real secrets."""

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issue5505_iap_backup as b
import issue5505_iap_ssh as iap
import issue5505_ansible_diagnostics as diagnostics
import test_issue5505_iap_ssh as existing


SNAPSHOT = {"BOOT_ID": "d3dbf0e2-4ffe-4c4e-a7d0-0ea4e9a5a0de", "SERVICE_START": "12345",
            "MITIGATION_SHA256": "a" * 64, "FIREWALLD_SHA256": "b" * 64}
SIZES = {"files.tar.gz": 245030158, "git.tar.gz": 7862871949, "redmine.sql": 33386382}
STATS = {name: {"size": size, "mtime": 1001, "inode": index}
         for index, (name, size) in enumerate(SIZES.items())}
OBJECTS = {name: {"Size": size, "LastModified": "2026-11-05T15:00:00Z", "Encryption": "AES256"}
           for name, size in SIZES.items()}
SSH_OUTPUT = "SSH_CONNECTION=35.235.243.210 42297 10.142.0.9 22\n" + \
             "ip protocol tcp ip hdrlength > 5 counter drop\n"


class BackupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.object(iap.urllib.request, "build_opener", side_effect=AssertionError("Network forbidden")).start()
        self.output = io.StringIO()
        context = redirect_stdout(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def test_s3_size_mismatch_fails(self):
        values = copy.deepcopy(OBJECTS)
        values["git.tar.gz"]["Size"] -= 1
        with self.assertRaises(iap.Stop):
            b.validate_objects(SIZES, values)

    def test_wrong_encryption_or_missing_object_fails(self):
        for change in ("encryption", "missing"):
            values = copy.deepcopy(OBJECTS)
            if change == "encryption":
                values["redmine.sql"]["Encryption"] = "aws:kms"
            else:
                del values["redmine.sql"]
            with self.assertRaises(iap.Stop):
                b.validate_objects(SIZES, values)

    def test_three_matching_objects_pass(self):
        b.validate_objects(SIZES, OBJECTS)

    def test_missing_or_duplicate_snapshot_field_fails(self):
        good = "\n".join(key + "=" + value for key, value in SNAPSHOT.items())
        self.assertEqual(b.snapshot_values(good), SNAPSHOT)
        for bad in (good + "\nSERVICE_START=4", good.replace("SERVICE_START=12345", "SERVICE_START=0")):
            with self.assertRaises(iap.Stop):
                b.snapshot_values(bad)

    def test_ansible_has_one_iap_target_and_pinned_host_auth(self):
        hosts = b.inventory(self.work)["all"]["hosts"]
        self.assertEqual(list(hosts), ["issue5505_target"])
        target = hosts["issue5505_target"]
        self.assertEqual((target["ansible_host"], target["ansible_port"], target["ansible_connection"]),
                         ("127.0.0.1", 22022, "ssh"))
        self.assertIn("StrictHostKeyChecking=yes", target["ansible_ssh_args"])
        self.assertIn("IdentityAgent=none", target["ansible_ssh_args"])
        self.assertIn("ControlPath=none", target["ansible_ssh_args"])
        self.assertEqual(target["ansible_ssh_private_key_file"], str(self.work / "id_rsa"))

    def test_upload_targets_and_credentials_are_not_logged(self):
        play = b.upload_play()[0]
        self.assertTrue(play["no_log"])
        safeguards = play["module_defaults"]["amazon.aws.s3_object"]
        self.assertEqual(safeguards["expected_bucket_owner"], "808683561341")
        self.assertEqual(safeguards["overwrite"], "never")
        self.assertEqual(safeguards["permission"], ["private"])
        self.assertEqual(len(play["tasks"]), 3)
        for name, task in zip(b.FILES, play["tasks"]):
            self.assertTrue(task["ansible.builtin.include_role"]["apply"]["no_log"])
            self.assertEqual(task["vars"]["upload_file_path"], "/tmp/redmine-backup/" + name)

    def test_inherited_ansible_debug_and_key_settings_are_removed(self):
        trial = b.BackupTrial(self.work)
        trial.deps = self.work / "deps"
        with patch.dict(os.environ, {"ANSIBLE_DEBUG": "true", "ANSIBLE_PRIVATE_KEY": "synthetic",
                                     "ANSIBLE_LOG_PATH": "/tmp/unsafe-log"}):
            env = trial.ansible_environment()
        self.assertEqual(env["ANSIBLE_NO_LOG"], "true")
        self.assertEqual(env["ANSIBLE_DEBUG"], "false")
        self.assertNotIn("ANSIBLE_PRIVATE_KEY", env)
        self.assertNotIn("ANSIBLE_LOG_PATH", env)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI executor locale check")
    def test_ansible_children_initialize_utf8_with_c_locale_parent(self):
        trial = b.BackupTrial(self.work)
        trial.deps = self.work / "deps"
        # Match Ansible's locale initialization, without relying on Python's
        # automatic UTF-8 mode or locale coercion to hide a non-UTF-8 setting.
        command = [sys.executable, "-c", "import json, locale, sys; "
                   "locale.setlocale(locale.LC_ALL, ''); "
                   "print(json.dumps([locale.getlocale()[1], sys.getfilesystemencoding()]))"]
        with patch.dict(os.environ, {"LC_ALL": "C", "LANG": "C"}):
            environments = {
                "original_c_locale": dict(os.environ),
                "diagnostics": diagnostics.diagnostic_environment(self.work / "ansible.cfg"),
                "playbooks": trial.ansible_environment(),
            }
        for name, env in environments.items():
            with self.subTest(child=name):
                env.update(PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
                result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                encoding, filesystem = json.loads(result.stdout)
                if name == "original_c_locale":
                    self.assertIsNone(encoding)
                else:
                    self.assertEqual(encoding.lower(), "utf-8")
                    self.assertEqual(filesystem.lower(), "utf-8")

    def test_wrong_branch_fails_before_aws_or_metadata(self):
        trial = b.BackupTrial(self.work)
        with patch.dict(os.environ, {"CIRCLECI": "true", "CIRCLE_BRANCH": "main"}), \
                patch.object(trial, "aws") as aws:
            with self.assertRaises(iap.Stop):
                trial.initialize()
            aws.assert_not_called()

    def test_failed_doc_check_stops_before_gcp_initialization_or_backup(self):
        trial = b.BackupTrial(self.work)
        caller = {"Account": b.OWNER, "Arn": "arn:aws:sts::808683561341:assumed-role/"
                  "CircleCI_OIDC_circleci-gce/offline-test"}
        source = Mock()
        source.read_text.return_value = json.dumps({"version": 1, "sources": []})
        with patch.dict(os.environ, {"CIRCLECI": "true", "CIRCLE_BRANCH": b.BRANCH,
                                     "CIRCLE_BUILD_NUM": "733", "AWS_ROLE_ARN": b.AWS_ROLE,
                                     "AWS_ACCESS_KEY_ID": "synthetic", "AWS_SECRET_ACCESS_KEY": "synthetic",
                                     "AWS_SESSION_TOKEN": "synthetic"}, clear=True), \
                patch.object(b, "SOURCE_FILE", source), \
                patch.object(trial, "aws", side_effect=[json.dumps(caller), '{"KeyCount": 0}']), \
                patch.object(b, "check_modules", return_value=False), \
                patch.object(iap.Trial, "initialize") as cloud, \
                patch.object(trial, "run_playbook") as play:
            with self.assertRaisesRegex(iap.Stop, "Ansible dependency check failed"):
                trial.initialize()
            cloud.assert_not_called()
            play.assert_not_called()
            self.assertFalse(trial.attempted)

    def test_syntax_check_uses_same_private_config_and_fails_closed(self):
        trial = b.BackupTrial(self.work)
        trial.deps = self.work / "deps"
        trial.deadline = time.monotonic() + 60
        trial.prepare_ansible_files()
        with patch.object(b.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as run:
            with self.assertRaises(iap.Stop):
                trial.syntax_check()
            args, options = run.call_args
            self.assertIn("--syntax-check", args[0])
            self.assertEqual(options["env"]["ANSIBLE_CONFIG"], str(self.work / "ansible.cfg"))
            self.assertEqual(options["env"]["ANSIBLE_NO_LOG"], "true")
            self.assertEqual(options["cwd"], self.work)
        self.assertNotIn("ANSIBLE_SYNTAX_CHECK=PASS", self.output.getvalue())

    def test_work_deadline_stops_child_before_cleanup(self):
        trial = b.BackupTrial(self.work)
        trial.deadline = time.monotonic() - 1
        trial.deps = self.work / "deps"
        process = Mock(returncode=None, pid=123456)
        process.poll.return_value = None
        with patch.object(b.subprocess, "Popen", return_value=process), patch.object(b.os, "killpg") as kill:
            with self.assertRaises(iap.Stop):
                trial.run_playbook(self.work / "unused.yml", {}, "TEST")
            kill.assert_called_once_with(123456, b.signal.SIGTERM)
        self.assertIn("REMOTE_TASK_COMPLETION=UNCONFIRMED", self.output.getvalue())

    def lifecycle(self, failure=None):
        cloud = existing.FakeCompute()
        original = copy.deepcopy(cloud.values)
        executed = []
        created = []

        class OfflineTrial(b.BackupTrial):
            def initialize(inner):
                inner.api = cloud.api
                inner.project_before = cloud.api("GET", iap.PROJECT_PATH)
                inner.deadline = time.monotonic() + b.WORK_SECONDS
                inner.deps = inner.work / "deps"
                inner.prefix = b.PREFIX_ROOT + "offline-test"
                (inner.work / "id_rsa.pub").write_text("ssh-rsa VFJJQUw= synthetic-test-key\n")
                created.append(inner.work)

            def start_tunnel(inner):
                inner.tunnel = Mock()
                inner.tunnel.poll.return_value = None

            def close_tunnel(inner):
                pass

            def snapshot(inner, label):
                result = dict(SNAPSHOT)
                if failure == "mitigation" and label == "AFTER":
                    result["SERVICE_START"] = "99999"
                return result

            def remote(inner, script):
                if script == b.PRECHECK:
                    return "BACKUP_PRECHECK=PASS\n"
                if script == b.FILE_STATS:
                    return json.dumps(STATS)
                return "1000\n"

            def run_playbook(inner, playbook, variables, stage):
                executed.append(stage)
                if failure == stage:
                    raise iap.Stop("Synthetic stage failure")

            def aws(inner, args):
                name = args[args.index("--key") + 1].rsplit("/", 1)[1]
                value = dict(OBJECTS[name])
                if failure == "s3_size" and name == "git.tar.gz":
                    value["Size"] -= 1
                return json.dumps(value)

        with patch.object(existing, "m", iap), \
                patch.object(iap, "run", return_value=subprocess.CompletedProcess([], 0, SSH_OUTPUT, "")), \
                patch.object(iap.signal, "signal"), patch.object(sys, "argv", ["trial", "--run"]):
            rc = iap.main(OfflineTrial)
        self.assertEqual(cloud.values, original)
        self.assertTrue(created and all(not p.exists() for p in created))
        added = cloud.posts[0][1]["items"]
        line = next(x["value"] for x in added if x["key"] == "ssh-keys").splitlines()[-1]
        expiry = json.loads(line.split(" google-ssh ")[1])["expireOn"]
        seconds = (iap.dt.datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%S%z") -
                   iap.dt.datetime.now(iap.dt.timezone.utc)).total_seconds()
        self.assertTrue(3595 <= seconds <= 3601)
        self.assertIn("TEMPORARY_SSH_KEY_REMOVED=PASS", self.output.getvalue())
        return rc, executed

    def test_backup_failure_cleans_key_and_never_uploads(self):
        rc, executed = self.lifecycle("BACKUP_CREATE")
        self.assertEqual(rc, 1)
        self.assertEqual(executed, ["BACKUP_CREATE"])
        self.assertNotIn("IAP_BACKUP_TRIAL=PASS", self.output.getvalue())

    def test_upload_failure_cleans_key_and_fails_trial(self):
        rc, _ = self.lifecycle("S3_UPLOAD")
        self.assertEqual(rc, 1)
        self.assertNotIn("IAP_BACKUP_TRIAL=PASS", self.output.getvalue())

    def test_s3_mismatch_cleans_key_and_fails_trial(self):
        rc, _ = self.lifecycle("s3_size")
        self.assertEqual(rc, 1)
        self.assertNotIn("IAP_BACKUP_TRIAL=PASS", self.output.getvalue())

    def test_mitigation_restart_cleans_key_and_fails_trial(self):
        rc, _ = self.lifecycle("mitigation")
        self.assertEqual(rc, 1)
        self.assertNotIn("IAP_BACKUP_TRIAL=PASS", self.output.getvalue())

    def test_success_requires_backup_upload_checks_and_cleanup(self):
        rc, executed = self.lifecycle()
        self.assertEqual(rc, 0)
        self.assertEqual(executed, ["BACKUP_CREATE", "S3_UPLOAD"])
        self.assertIn("IAP_BACKUP_TRIAL=PASS", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
