#!/usr/bin/env python3
"""Stop the fixed VM only after a successful backup child and verified cleanup."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import issue5505_iap_ssh as iap
from redmine_iap_backup import RUN_ID_ENV, check_context

BACKUP_SCRIPT = Path(__file__).with_name("redmine_iap_backup.py")
BACKUP_LIMIT = 50 * 60
CLEANUP_GRACE = 180
STOP_READBACK_LIMIT = 180


class StopWorkflow:
    def __init__(self):
        self.cancelled = False
        self.stop_requested = False

    def cancel(self, signum, frame):
        # The child changes its handlers during cleanup; keep this independent.
        self.cancelled = True

    def check_not_cancelled(self):
        iap.require(not self.cancelled, "Cancelled; no new VM stop request")

    @staticmethod
    def terminate_child(process):
        if process.poll() is not None:
            return
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

    def wait_for_backup(self, process):
        deadline = time.monotonic() + BACKUP_LIMIT
        cancelled_at = None
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                self.cancelled = True
            if self.cancelled and cancelled_at is None:
                cancelled_at = now
                iap.emit("BACKUP_CANCEL_REQUESTED; allowing bounded key cleanup")
                self.terminate_child(process)
            if cancelled_at is not None and now - cancelled_at >= CLEANUP_GRACE:
                iap.emit("BACKUP_CLEANUP=UNCONFIRMED; inspect temporary key and remote tasks")
                raise iap.Stop("Backup cleanup exceeded the cancellation grace period")
            time.sleep(0.2)
        iap.require(process.returncode == 0, "Backup or cleanup failed; VM stop is blocked")
        self.check_not_cancelled()

    def run_backup(self):
        self.check_not_cancelled()
        env = dict(os.environ)
        env[RUN_ID_ENV] = uuid.uuid4().hex
        process = subprocess.Popen([sys.executable, str(BACKUP_SCRIPT), "--run"],
                                   stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        try:
            self.wait_for_backup(process)
        finally:
            if process.poll() is None:
                # No retry or VM stop follows a forcibly ended child.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)

    def check_identity(self):
        iap.require("CLOUDSDK_AUTH_ACCESS_TOKEN" not in os.environ, "Access token override present")
        for prop in ("auth/impersonate_service_account", "auth/access_token_file",
                     "auth/credential_file_override"):
            value = iap.run(["gcloud", "config", "get", prop]).stdout.strip()
            iap.require(value in ("", "(unset)"), "Authentication override present")
        account = iap.run(["gcloud", "auth", "list", "--filter=status:ACTIVE",
                           "--format=value(account)"]).stdout.strip()
        iap.require(account == iap.ACCOUNT, "Unexpected CI service account before stop")

    @staticmethod
    def scope():
        return ["--account=" + iap.ACCOUNT, "--project=" + iap.PROJECT,
                "--zone=" + iap.ZONE, "--quiet"]

    def describe(self):
        result = iap.run(["gcloud", "compute", "instances", "describe", iap.VM,
                          *self.scope(), "--format=json(id,status)"], timeout=45)
        vm = json.loads(result.stdout)
        iap.require(str(vm.get("id")) == iap.INSTANCE_ID, "Target VM ID changed before/after stop")
        return vm

    def stop_vm(self):
        self.check_not_cancelled()
        self.check_identity()
        iap.require(self.describe().get("status") == "RUNNING", "VM is not RUNNING before stop")
        self.check_not_cancelled()
        # Once this boundary is crossed, cancellation cannot undo a submitted stop.
        self.stop_requested = True
        iap.emit("VM_STOP_REQUESTING")
        try:
            iap.run(["gcloud", "compute", "instances", "stop", iap.VM, *self.scope()], timeout=300)
            deadline = time.monotonic() + STOP_READBACK_LIMIT
            while True:
                status = self.describe().get("status")
                if status == "TERMINATED":
                    iap.emit("VM_TERMINATED_CHECK=PASS")
                    return
                iap.require(status in ("RUNNING", "STOPPING"), "Unexpected VM state after stop")
                iap.require(time.monotonic() < deadline, "VM stop readback timed out")
                time.sleep(3)
        except Exception:
            iap.emit("VM_STOP_RESULT=UNCONFIRMED; read current VM state before any manual retry")
            raise

    def execute(self):
        check_context()
        self.check_not_cancelled()
        self.run_backup()
        self.check_not_cancelled()
        self.stop_vm()
        iap.require(not self.cancelled, "Cancelled after VM stop request; inspect the recorded final state")
        iap.emit("PRODUCTION_BACKUP_AND_STOP=PASS")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    iap.require(argv in (["--check"], ["--run"]), "Usage: redmine_stop.py --check | --run")
    check_context()
    if argv == ["--check"]:
        iap.emit("PRODUCTION_CONTEXT_CHECK=PASS")
        return 0
    os.environ.update(LC_ALL="C", TZ="UTC", CLOUDSDK_CORE_DISABLE_PROMPTS="1")
    os.umask(0o077)
    workflow = StopWorkflow()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in handlers:
            signal.signal(sig, workflow.cancel)
        workflow.execute()
        return 0
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        iap.emit("PRODUCTION_RESULT=FAIL: " + (str(error) if isinstance(error, iap.Stop)
                                              else type(error).__name__))
        sys.exit(1)
