"""Production failure boundaries. All cloud calls and VM actions are simulated."""

import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issue5505_iap_backup as backup
import issue5505_iap_ssh as iap
import redmine_iap_backup as production
import redmine_stop as stop
import test_issue5505_iap_backup as fixtures
import test_issue5505_iap_ssh as metadata_fixtures

CONTEXT = {"CIRCLECI": "true", "CIRCLE_BRANCH": "main", "CIRCLE_BUILD_NUM": "800",
           "REDMINE_INSTANCE": iap.VM, "REDMINE_ZONE": iap.ZONE}
RUN_ID = "a" * 32


class ProductionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(patch.dict(os.environ, CONTEXT, clear=True))
        self.enterContext(patch.object(iap.urllib.request, "build_opener",
                                      side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch.object(iap, "run", side_effect=AssertionError("Cloud/SSH forbidden")))
        self.enterContext(patch.object(subprocess, "Popen",
                                      side_effect=AssertionError("External child forbidden")))

    def objects(self):
        objects = copy.deepcopy(fixtures.OBJECTS)
        for item in objects.values():
            item["RunId"] = RUN_ID
        return objects

    def trial(self):
        trial = production.ProductionBackup(self.work)
        trial.run_id = RUN_ID
        trial.prefix = production.PREFIX
        return trial

    def test_wrong_context_stops_before_backup_or_stop(self):
        for key, value in (("CIRCLECI", "false"), ("CIRCLE_BRANCH", "5515"),
                           ("REDMINE_INSTANCE", "other"), ("REDMINE_ZONE", ""),
                           ("CIRCLE_BUILD_NUM", "")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                workflow = stop.StopWorkflow()
                with patch.object(workflow, "run_backup") as child, \
                        patch.object(workflow, "stop_vm") as vm_stop:
                    with self.assertRaises(iap.Stop):
                        workflow.execute()
                    child.assert_not_called()
                    vm_stop.assert_not_called()

    def test_check_command_never_starts_backup(self):
        with patch.object(stop.StopWorkflow, "execute") as execute:
            self.assertEqual(stop.main(["--check"]), 0)
            execute.assert_not_called()

    def test_current_run_and_all_three_objects_are_required(self):
        trial = self.trial()
        trial.validate_saved_objects(fixtures.SIZES, self.objects())
        for name in backup.FILES:
            for mutation in ("stale", "missing_run", "size", "encryption", "missing_object"):
                with self.subTest(name=name, mutation=mutation):
                    values = self.objects()
                    if mutation == "stale":
                        values[name]["RunId"] = "b" * 32
                    elif mutation == "missing_run":
                        del values[name]["RunId"]
                    elif mutation == "size":
                        values[name]["Size"] -= 1
                    elif mutation == "encryption":
                        values[name]["Encryption"] = "aws:kms"
                    else:
                        del values[name]
                    with self.assertRaises(iap.Stop):
                        trial.validate_saved_objects(fixtures.SIZES, values)

    def test_production_upload_keeps_guards_without_changing_trial_overwrite(self):
        play = self.trial().build_upload_play()[0]
        settings = play["module_defaults"]["amazon.aws.s3_object"]
        self.assertEqual(settings["overwrite"], "always")
        self.assertEqual(settings["metadata"], {"backup-run-id": RUN_ID})
        self.assertEqual(settings["expected_bucket_owner"], backup.OWNER)
        self.assertEqual(settings["permission"], ["private"])
        self.assertEqual(settings["encryption_mode"], "AES256")
        self.assertTrue(play["no_log"] and settings["encrypt"])
        self.assertEqual(backup.upload_play()[0]["module_defaults"]
                         ["amazon.aws.s3_object"]["overwrite"], "never")
        self.assertEqual(len(play["tasks"]), 3)

    def test_destination_and_head_use_expected_owner_and_same_fixed_key(self):
        trial = self.trial()
        with patch.object(trial, "aws", return_value="{}") as aws:
            trial.prepare_destination()
            self.assertEqual(trial.prefix, "circleci-gce/redmine/stop")
            self.assertEqual(aws.call_args.args[0][:2], ["s3api", "head-bucket"])
            for name in backup.FILES:
                trial.read_saved_object(name)
                args = aws.call_args.args[0]
                self.assertEqual(args[args.index("--expected-bucket-owner") + 1], backup.OWNER)
                self.assertEqual(args[args.index("--key") + 1], production.PREFIX + "/" + name)
                self.assertIn('RunId:Metadata."backup-run-id"', args[args.index("--query") + 1])
            count = aws.call_count
            with self.assertRaises(iap.Stop):
                trial.read_saved_object("unrelated-file")
            self.assertEqual(aws.call_count, count)

    def lifecycle(self, failure=None):
        """Exercise production upload verification and the real shared cleanup."""
        cloud = metadata_fixtures.FakeCompute()
        before = copy.deepcopy(cloud.values)
        stages = []
        workdirs = []

        class OfflineProduction(production.ProductionBackup):
            def initialize(inner):
                inner.run_id = RUN_ID
                inner.api = cloud.api
                inner.project_before = cloud.api("GET", iap.PROJECT_PATH)
                inner.deadline = time.monotonic() + backup.WORK_SECONDS
                inner.deps = inner.work / "deps"
                inner.prefix = production.PREFIX
                (inner.work / "id_rsa.pub").write_text("ssh-rsa VFJJQUw= synthetic-test\n")
                workdirs.append(inner.work)

            def start_tunnel(inner):
                inner.tunnel = Mock()
                inner.tunnel.poll.return_value = None

            def close_tunnel(inner):
                if failure == "tunnel_cleanup":
                    raise iap.Stop("Synthetic tunnel cleanup failure")

            def snapshot(inner, label):
                return dict(fixtures.SNAPSHOT)

            def remote(inner, script):
                if script == backup.PRECHECK:
                    return "BACKUP_PRECHECK=PASS\n"
                if script == backup.FILE_STATS:
                    return json.dumps(fixtures.STATS)
                return "1000\n"

            def run_playbook(inner, playbook, variables, stage):
                stages.append(stage)
                if failure == stage:
                    raise iap.Stop("Synthetic backup/upload failure")

            def aws(inner, args):
                name = args[args.index("--key") + 1].rsplit("/", 1)[1]
                value = dict(fixtures.OBJECTS[name], RunId=RUN_ID)
                if failure == "old_s3_object" and name == "redmine.sql":
                    value["RunId"] = "b" * 32
                return json.dumps(value)

            def remove_key(inner):
                if failure == "key_cleanup":
                    raise iap.Stop("Synthetic metadata service failure")
                return super().remove_key()

            def verify_project_keys(inner):
                if failure == "project_keys":
                    raise iap.Stop("Synthetic project preservation failure")
                return super().verify_project_keys()

        with patch.object(metadata_fixtures, "m", iap), \
                patch.object(iap, "run", return_value=subprocess.CompletedProcess(
                    [], 0, fixtures.SSH_OUTPUT, "")), \
                patch.object(iap.time, "sleep"), patch.object(iap.signal, "signal"), \
                patch.object(iap.os, "umask"), patch.object(sys, "argv", ["production", "--run"]):
            rc = iap.main(OfflineProduction)
        self.assertTrue(workdirs and all(not path.exists() for path in workdirs))
        if failure != "key_cleanup":
            self.assertEqual(cloud.values, before)
        return rc, stages

    def test_backup_upload_verification_and_cleanup_failures_all_block_stop(self):
        for failure in ("BACKUP_CREATE", "S3_UPLOAD", "old_s3_object", "key_cleanup",
                        "project_keys", "tunnel_cleanup"):
            with self.subTest(failure=failure):
                rc, stages = self.lifecycle(failure)
                self.assertEqual(rc, 1)
                if failure == "BACKUP_CREATE":
                    self.assertEqual(stages, ["BACKUP_CREATE"])
                workflow = stop.StopWorkflow()
                process = Mock(returncode=rc)
                process.poll.return_value = rc
                with patch.object(subprocess, "Popen", return_value=process), \
                        patch.object(workflow, "stop_vm") as vm_stop:
                    with self.assertRaises(iap.Stop):
                        workflow.execute()
                    vm_stop.assert_not_called()

    def test_full_success_cleans_key_before_stop_and_uses_fresh_run_id(self):
        rc, stages = self.lifecycle()
        self.assertEqual((rc, stages), (0, ["BACKUP_CREATE", "S3_UPLOAD"]))
        self.assertIn("PRODUCTION_IAP_BACKUP_AND_CLEANUP=PASS", self.output.getvalue())
        workflow = stop.StopWorkflow()
        process = Mock(returncode=0)
        process.poll.return_value = 0
        with patch.dict(os.environ, {production.RUN_ID_ENV: "untrusted-inherited-value"}), \
                patch.object(subprocess, "Popen", return_value=process) as popen, \
                patch.object(workflow, "stop_vm") as vm_stop:
            workflow.execute()
        vm_stop.assert_called_once()
        run_id = popen.call_args.kwargs["env"][production.RUN_ID_ENV]
        self.assertRegex(run_id, "^[0-9a-f]{32}$")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_cancel_during_cleanup_blocks_stop_even_when_child_exits_zero(self):
        workflow = stop.StopWorkflow()
        process = Mock(returncode=0)
        polls = iter((None, None, 0, 0))

        def poll():
            value = next(polls, 0)
            if value is None:
                workflow.cancel(signal.SIGTERM, None)
            return value

        process.poll.side_effect = poll
        with patch.object(subprocess, "Popen", return_value=process), \
                patch.object(stop.time, "sleep"), patch.object(workflow, "stop_vm") as vm_stop:
            with self.assertRaises(iap.Stop):
                workflow.execute()
        process.send_signal.assert_called_once_with(signal.SIGTERM)
        vm_stop.assert_not_called()

    def test_cancel_after_child_success_still_blocks_stop(self):
        workflow = stop.StopWorkflow()
        with patch.object(workflow, "run_backup", side_effect=lambda: workflow.cancel(15, None)), \
                patch.object(workflow, "stop_vm") as vm_stop:
            with self.assertRaises(iap.Stop):
                workflow.execute()
            vm_stop.assert_not_called()

    def test_work_timeout_allows_cleanup_then_blocks_stop(self):
        workflow = stop.StopWorkflow()
        process = Mock(returncode=0)
        process.poll.side_effect = [None, None, 0, 0]
        with patch.object(stop, "BACKUP_LIMIT", -1), patch.object(stop.time, "sleep"), \
                patch.object(subprocess, "Popen", return_value=process), \
                patch.object(workflow, "stop_vm") as vm_stop:
            with self.assertRaises(iap.Stop):
                workflow.execute()
        process.send_signal.assert_called_once_with(signal.SIGTERM)
        vm_stop.assert_not_called()

    def test_stuck_cleanup_is_bounded_and_never_stops_vm(self):
        workflow = stop.StopWorkflow()
        process = Mock(returncode=None, pid=123456)
        process.poll.return_value = None
        with patch.object(stop, "BACKUP_LIMIT", -1), patch.object(stop, "CLEANUP_GRACE", 0), \
                patch.object(subprocess, "Popen", return_value=process), \
                patch.object(stop.os, "killpg") as kill, patch.object(workflow, "stop_vm") as vm_stop:
            with self.assertRaises(iap.Stop):
                workflow.execute()
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        vm_stop.assert_not_called()
        self.assertIn("BACKUP_CLEANUP=UNCONFIRMED", self.output.getvalue())

    def cloud_commands(self, states, *, fail_stop=False, cancel_on_precheck=False, wrong_account=False):
        """Small gcloud stand-in; reject all unexpected cloud operations."""
        commands = []
        iterator = iter(states)
        workflow = stop.StopWorkflow()

        def run(args, **kwargs):
            commands.append(args)
            if args[:3] == ["gcloud", "config", "get"]:
                value = "(unset)"
            elif args[:3] == ["gcloud", "auth", "list"]:
                value = "unexpected-account" if wrong_account else iap.ACCOUNT
            elif args[:4] == ["gcloud", "compute", "instances", "describe"]:
                if cancel_on_precheck:
                    workflow.cancel(signal.SIGTERM, None)
                value = json.dumps(next(iterator))
            elif args[:4] == ["gcloud", "compute", "instances", "stop"]:
                if fail_stop:
                    raise iap.Stop("Synthetic stop response lost")
                value = ""
            else:
                raise AssertionError("Unexpected cloud command")
            return subprocess.CompletedProcess(args, 0, value, "")

        return workflow, commands, run

    def test_stop_uses_fixed_scope_and_waits_for_terminated(self):
        workflow, commands, run = self.cloud_commands([
            {"id": iap.INSTANCE_ID, "status": status} for status in ("RUNNING", "STOPPING", "TERMINATED")])
        with patch.object(iap, "run", side_effect=run), patch.object(stop.time, "sleep"):
            workflow.stop_vm()
        requests = [a for a in commands if a[:4] == ["gcloud", "compute", "instances", "stop"]]
        self.assertEqual(requests, [["gcloud", "compute", "instances", "stop", iap.VM,
                                   "--account=" + iap.ACCOUNT, "--project=" + iap.PROJECT,
                                   "--zone=" + iap.ZONE, "--quiet"]])
        self.assertIn("VM_TERMINATED_CHECK=PASS", self.output.getvalue())

    def test_wrong_vm_state_identity_or_cancel_prevents_stop_request(self):
        for case in ("id", "state", "account", "cancel"):
            with self.subTest(case=case):
                state = {"id": "different" if case == "id" else iap.INSTANCE_ID,
                         "status": "TERMINATED" if case == "state" else "RUNNING"}
                workflow, commands, run = self.cloud_commands(
                    [state], wrong_account=case == "account", cancel_on_precheck=case == "cancel")
                with patch.object(iap, "run", side_effect=run):
                    with self.assertRaises(iap.Stop):
                        workflow.stop_vm()
                self.assertFalse(workflow.stop_requested)
                self.assertFalse(any(a[:4] == ["gcloud", "compute", "instances", "stop"] for a in commands))

    def test_unknown_stop_result_is_not_retried_or_reported_success(self):
        for fail_request in (True, False):
            with self.subTest(fail_request=fail_request):
                self.output.truncate(0)
                self.output.seek(0)
                states = [{"id": iap.INSTANCE_ID, "status": "RUNNING"},
                          {"id": "replaced-instance", "status": "TERMINATED"}]
                workflow, commands, run = self.cloud_commands(states, fail_stop=fail_request)
                with patch.object(iap, "run", side_effect=run):
                    with self.assertRaises(iap.Stop):
                        workflow.stop_vm()
                self.assertEqual(sum(a[:4] == ["gcloud", "compute", "instances", "stop"] for a in commands), 1)
                self.assertIn("VM_STOP_RESULT=UNCONFIRMED", self.output.getvalue())
                self.assertNotIn("VM_TERMINATED_CHECK=PASS", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
