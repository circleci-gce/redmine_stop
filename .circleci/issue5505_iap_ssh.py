#!/usr/bin/env python3
"""One approved IAP SSH trial; only the target VM's metadata may be written."""

import base64
from collections import Counter
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

PROJECT = "buoyant-world-263612"
ZONE = "us-east1-d"
VM = "redmica2-south-carolina"
INSTANCE_ID = "7213092591087619377"
ACCOUNT = "circleci-circleci-gce-23011-16@buoyant-world-263612.iam.gserviceaccount.com"
BRANCH = "test/issue5505-iap-r01"
HOST_FP = "SHA256:giY6WNa9vKQr7EY/IG79NMqfqa4KRACl9mv6KzJe5XI"
PROJECT_PATH = "/projects/" + PROJECT
ZONE_PATH = PROJECT_PATH + "/zones/" + ZONE
VM_PATH = ZONE_PATH + "/instances/" + VM
BASE = "https://compute.googleapis.com/compute/v1"


class Stop(RuntimeError):
    pass


class ApiError(Stop):
    def __init__(self, status):
        self.status = status
        super().__init__("Compute API HTTP status " + str(status))


def require(condition, message):
    if not condition:
        raise Stop(message)


def emit(message):
    print(message, flush=True)


def run(args, *, input=None, timeout=60, check=True):
    result = subprocess.run(args, input=input, text=True, capture_output=True,
                            timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL)
    if check and result.returncode:
        # Do not print credential commands, metadata or arbitrary subprocess output.
        raise Stop("Command failed: " + Path(args[0]).name + " exit=" + str(result.returncode))
    return result


def metadata(data, field="metadata"):
    result = {}
    for item in (data.get(field) or {}).get("items") or []:
        key, value = item["key"], item["value"]
        require(isinstance(key, str) and isinstance(value, str), "Invalid metadata format")
        require(key not in result, "Duplicate metadata key")
        result[key] = value
    return result


def lines(value):
    return Counter(line for line in value.splitlines() if line.strip())


def fingerprint(blob):
    raw = base64.b64decode(blob, validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def has_key(line, blob):
    parts = line.split()
    return len(parts) >= 2 and parts[1] == blob


def without_key(value, blob):
    return "".join(line for line in value.splitlines(keepends=True) if not has_key(line, blob))


def check_auth_mode(vm, project):
    im, pm = metadata(vm), metadata(project, "commonInstanceMetadata")
    for values in (im, pm):
        for name in ("enable-oslogin", "enable-oslogin-2fa", "block-project-ssh-keys"):
            require(values.get(name, "FALSE").upper() in ("TRUE", "FALSE"),
                    "Unexpected SSH authentication metadata")
    require(im.get("enable-oslogin", pm.get("enable-oslogin", "FALSE")).upper() != "TRUE",
            "OS Login is enabled; this trial requires metadata SSH keys")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stop("Unexpected Compute API redirect")


class Trial:
    def __init__(self, work):
        self.work = Path(work)
        self.token = None
        self.pending = None
        self.tunnel = None
        self.tunnel_log = None
        self.blob = None
        self.key_line = None
        self.original = None
        self.registered_value = None
        self.attempted = False
        self.project_before = None

    def api(self, method, path, body=None):
        if method == "POST":
            require(path.startswith(VM_PATH + "/setMetadata?requestId="), "Forbidden write target")
        else:
            require(method == "GET" and (path in (VM_PATH, PROJECT_PATH) or
                    path.startswith(ZONE_PATH + "/operations/")), "Unexpected read target")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(BASE + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise ApiError(status) from None
        except (urllib.error.URLError, TimeoutError):
            raise Stop("Compute API transport error; response not confirmed") from None

    def get_vm(self, running=False):
        vm = self.api("GET", VM_PATH)
        require(str(vm.get("id")) == INSTANCE_ID, "Instance ID changed")
        if running:
            require(vm.get("status") == "RUNNING", "VM is not RUNNING")
        metadata(vm)
        return vm

    def finish_pending(self):
        if self.pending is None:
            return
        pending = self.pending
        if pending["operation"] is None:
            try:
                pending["operation"] = self.api("POST", pending["path"], pending["body"])
            except ApiError as error:
                if 400 <= error.status < 500 and error.status not in (408, 429):
                    self.pending = None  # A definitive rejected request made no change.
                raise
        deadline = time.monotonic() + 120
        while True:
            operation = pending["operation"]
            name = operation.get("name", "")
            require(re.fullmatch(r"[A-Za-z0-9_-]+", name), "Invalid operation name")
            require(str(operation.get("targetId")) == INSTANCE_ID, "Unexpected operation target")
            if operation.get("status") == "DONE":
                self.pending = None
                require(not operation.get("error"), "Metadata operation failed")
                return
            require(time.monotonic() < deadline, "Metadata operation completion not confirmed")
            time.sleep(2)
            pending["operation"] = self.api("GET", ZONE_PATH + "/operations/" + name)

    def write_metadata(self, vm, values):
        require(self.pending is None, "An earlier metadata write remains unresolved")
        fp = (vm.get("metadata") or {}).get("fingerprint")
        require(isinstance(fp, str) and fp, "Metadata fingerprint missing")
        self.pending = {
            "path": VM_PATH + "/setMetadata?requestId=" + str(uuid.uuid4()),
            "body": {"fingerprint": fp, "items": [
                {"key": key, "value": value} for key, value in values.items()]},
            "operation": None,
        }
        # A transport failure leaves this exact request ID/payload pending. Cleanup
        # resolves it before removing the key, rather than claiming premature absence.
        self.finish_pending()

    def initialize(self):
        require(os.environ.get("CIRCLECI") == "true" and os.environ.get("CIRCLE_BRANCH") == BRANCH,
                "Run only in the approved CircleCI trial branch")
        require("CLOUDSDK_AUTH_ACCESS_TOKEN" not in os.environ, "Access token override present")
        for prop in ("auth/impersonate_service_account", "auth/access_token_file",
                     "auth/credential_file_override"):
            value = run(["gcloud", "config", "get", prop]).stdout.strip()
            require(value in ("", "(unset)"), "Authentication override present")
        account = run(["gcloud", "auth", "list", "--filter=status:ACTIVE",
                       "--format=value(account)"]).stdout.strip()
        require(account == ACCOUNT, "Unexpected CI service account")
        self.token = run(["gcloud", "auth", "print-access-token", "--account=" + ACCOUNT]).stdout.strip()
        require(bool(self.token), "No access token returned")
        self.project_before = self.api("GET", PROJECT_PATH)
        vm = self.get_vm(running=True)
        check_auth_mode(vm, self.project_before)
        emit("CI_GCP_IDENTITY_CHECK=PASS")
        emit("TARGET_INSTANCE_CHECK=PASS")

    def start_tunnel(self):
        self.tunnel_log = (self.work / "tunnel.log").open("w")
        self.tunnel = subprocess.Popen([
            "gcloud", "compute", "start-iap-tunnel", VM, "22", "--account=" + ACCOUNT,
            "--project=" + PROJECT, "--zone=" + ZONE, "--local-host-port=127.0.0.1:22022",
        ], stdin=subprocess.DEVNULL, stdout=self.tunnel_log, stderr=subprocess.STDOUT,
            start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
        for _ in range(60):
            require(self.tunnel.poll() is None, "IAP tunnel exited; no SSH key was registered")
            if "Listening on port [22022]." in (self.work / "tunnel.log").read_text():
                break
            time.sleep(1)
        else:
            raise Stop("IAP tunnel readiness timed out")
        scan = run(["ssh-keyscan", "-4", "-T", "15", "-p", "22022", "-t", "ed25519", "127.0.0.1"])
        keys = {tuple(line.split()[1:3]) for line in scan.stdout.splitlines()
                if line.strip() and not line.startswith("#")}
        require(len(keys) == 1, "Expected one SSH host key")
        kind, blob = next(iter(keys))
        require(kind == "ssh-ed25519" and fingerprint(blob) == HOST_FP, "SSH host key mismatch")
        (self.work / "known_hosts").write_text("issue5505-production " + kind + " " + blob + "\n")
        emit("IAP_SSH_HOST_KEY_CHECK=PASS")

    def register_key(self):
        key = self.work / "id_rsa"
        run(["ssh-keygen", "-q", "-t", "rsa", "-b", "3072", "-N", "", "-f", str(key),
             "-C", "issue5505-ci-trial"])
        kind, self.blob = Path(str(key) + ".pub").read_text().split()[:2]
        require(kind == "ssh-rsa", "Unexpected generated key type")
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=20)
        expires_text = expires.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.key_line = "centos:" + kind + " " + self.blob + " google-ssh " + json.dumps(
            {"userName": "centos", "expireOn": expires_text}, separators=(",", ":"))
        emit("TRIAL_KEY_FINGERPRINT=" + fingerprint(self.blob))
        emit("TRIAL_KEY_EXPIRES_UTC=" + expires_text)
        for _ in range(3):
            vm = self.get_vm(running=True)
            check_auth_mode(vm, self.api("GET", PROJECT_PATH))
            before = metadata(vm)
            old = before.get("ssh-keys", "")
            require(not any(has_key(line, self.blob) for line in old.splitlines()), "Trial key already present")
            value = old + ("\n" if old and not old.endswith("\n") else "") + self.key_line + "\n"
            desired = dict(before, **{"ssh-keys": value})
            self.original, self.registered_value = before, value
            self.attempted = True
            try:
                self.write_metadata(vm, desired)
            except ApiError as error:
                if error.status == 412:
                    continue
                raise
            require(metadata(self.get_vm()) == desired, "Metadata readback differs after registration")
            emit("INSTANCE_EXISTING_KEYS_PRESERVED=PASS")
            emit("TEMPORARY_SSH_KEY_REGISTERED=PASS")
            return
        raise Stop("Metadata changed repeatedly; registration aborted")

    def ssh_check(self):
        args = ["ssh", "-F", "/dev/null", "-T", "-a", "-x", "-S", "none", "-p", "22022",
                "-i", str(self.work / "id_rsa")]
        for option in (
            "HostKeyAlias=issue5505-production", "StrictHostKeyChecking=yes", "CheckHostIP=no",
            "UpdateHostKeys=no", "HostKeyAlgorithms=ssh-ed25519",
            "UserKnownHostsFile=" + str(self.work / "known_hosts"), "GlobalKnownHostsFile=/dev/null",
            "IdentityAgent=none", "IdentitiesOnly=yes", "BatchMode=yes", "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no", "PreferredAuthentications=publickey",
            "ClearAllForwardings=yes", "ProxyCommand=none", "ProxyJump=none",
            "ConnectTimeout=10", "ConnectionAttempts=1", "ServerAliveInterval=10", "ServerAliveCountMax=2",
        ):
            args.extend(["-o", option])
        args.extend(["centos@127.0.0.1", "bash -s"])
        for attempt in range(1, 7):
            require(self.tunnel.poll() is None, "IAP tunnel stopped")
            result = run(args, input=REMOTE_CHECK, timeout=60, check=False)
            if result.returncode != 255:
                break
            emit("SSH_WAIT_FOR_KEY_ATTEMPT=" + str(attempt))
            if attempt < 6:
                time.sleep(5)
        for line in result.stdout.splitlines():
            if line.startswith("REMOTE_CHECK_STAGE="):
                emit(line)
        if result.returncode == 255 and "Permission denied" in result.stderr:
            emit("SSH_FAILURE_HINT=Public-key authentication was not accepted")
        require(result.returncode == 0, "SSH command failed, exit=" + str(result.returncode))
        matches = re.findall(r"^SSH_CONNECTION=(.+)$", result.stdout, re.MULTILINE)
        require(len(matches) == 1 and len(matches[0].split()) == 4, "Invalid SSH_CONNECTION result")
        source, _, destination, port = matches[0].split()
        require(ipaddress.ip_address(source) in ipaddress.ip_network("35.235.240.0/20"),
                "SSH connection did not originate from the IAP range")
        require(destination == "10.142.0.9" and port == "22", "Unexpected SSH destination")
        require("ip protocol tcp ip hdrlength > 5 counter drop" in result.stdout,
                "Expected mitigation rule missing")
        emit(result.stdout.rstrip())
        emit("IAP_SSH_SUDO_CHECK=PASS")
        emit("MITIGATION_READ_CHECK=PASS")

    def remove_key(self):
        self.finish_pending()
        for _ in range(3):
            vm = self.get_vm()
            before = metadata(vm)
            old = before.get("ssh-keys", "")
            if not any(has_key(line, self.blob) for line in old.splitlines()):
                emit("TEMPORARY_SSH_KEY_REMOVED=PASS")
                return
            desired = dict(before)
            # Restore the exact original string only if nobody changed this value.
            if old == self.registered_value:
                if "ssh-keys" in self.original:
                    desired["ssh-keys"] = self.original["ssh-keys"]
                else:
                    desired.pop("ssh-keys", None)
            else:
                desired["ssh-keys"] = without_key(old, self.blob)
            try:
                self.write_metadata(vm, desired)
            except ApiError as error:
                if error.status == 412:
                    continue
                raise
            require(metadata(self.get_vm()) == desired, "Metadata readback differs after cleanup")
            emit("TEMPORARY_SSH_KEY_REMOVED=PASS")
            emit("OTHER_INSTANCE_METADATA_PRESERVED=PASS")
            return
        raise Stop("Metadata changed repeatedly during cleanup")

    def verify_project_keys(self):
        before = metadata(self.project_before, "commonInstanceMetadata")
        after = metadata(self.api("GET", PROJECT_PATH), "commonInstanceMetadata")
        require(not (lines(before.get("ssh-keys", "")) - lines(after.get("ssh-keys", ""))),
                "Project SSH key entries changed; manual review required")
        emit("PROJECT_EXISTING_KEYS_PRESERVED=PASS")

    def verify_instance_keys(self):
        after = metadata(self.get_vm())
        require(not (lines(self.original.get("ssh-keys", "")) - lines(after.get("ssh-keys", ""))),
                "Pre-existing VM SSH key entries changed; manual review required")
        require(not any(has_key(line, self.blob) for line in after.get("ssh-keys", "").splitlines()),
                "Trial key is still registered")
        emit("INSTANCE_EXISTING_KEYS_PRESERVED_AFTER_CLEANUP=PASS")

    def close_tunnel(self):
        if self.tunnel is not None and self.tunnel.poll() is None:
            try:
                os.killpg(self.tunnel.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.tunnel.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.tunnel.wait(timeout=5)
        if self.tunnel_log is not None:
            self.tunnel_log.close()


REMOTE_CHECK = r'''set -euo pipefail
export LC_ALL=C
export TZ=UTC
echo 'REMOTE_CHECK_STAGE=identity'
test "$(hostname -s)" = redmica2-south-carolina
test "$(id -un)" = centos
cve_id="$(curl -q -fsS --noproxy '*' --connect-timeout 3 --max-time 10 \
  -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/id)"
test "$cve_id" = 7213092591087619377
printf 'HOSTNAME=%s\nUSER=%s\nINSTANCE_ID=%s\n' "$(hostname -s)" "$(id -un)" "$cve_id"
printf 'SSH_CONNECTION=%s\n' "${SSH_CONNECTION:?}"
echo 'REMOTE_CHECK_STAGE=sudo'
cve_uid="$(sudo -k -n id -u)"
test "$cve_uid" = 0
printf 'SUDO_UID=%s\n' "$cve_uid"
echo 'REMOTE_CHECK_STAGE=mitigation'
sudo -n systemctl is-active --quiet cve-2026-98432-mitigation.service
sudo -n systemctl is-active --quiet firewalld.service
test "$(sudo -n systemctl show cve-2026-98432-mitigation.service -p UnitFileState --value)" = enabled
sudo -n nft --stateless list table inet cve_2026_98432
date -u '+%Y-%m-%dT%H:%M:%SZ'
'''


def interrupted(signum, frame):
    raise Stop("Trial interrupted by signal " + str(signum))


def main():
    require(sys.argv[1:] == ["--run"], "Usage: issue5505_iap_ssh.py --run (CircleCI only)")
    os.environ.update(LC_ALL="C", TZ="UTC", CLOUDSDK_CORE_DISABLE_PROMPTS="1")
    os.umask(0o077)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    success = False
    with tempfile.TemporaryDirectory(prefix="issue5505-iap-ssh-") as work:
        trial = Trial(work)
        try:
            trial.initialize()
            trial.start_tunnel()
            trial.register_key()
            trial.ssh_check()
            success = True
        except Exception as error:
            emit("TRIAL_RESULT=FAIL: " + (str(error) if isinstance(error, Stop) else type(error).__name__))
        finally:
            # Give a normal cancellation one bounded cleanup opportunity.
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, signal.SIG_IGN)
            if trial.attempted:
                cleaned = False
                for attempt in range(1, 4):
                    emit("CLEANUP_ATTEMPT=" + str(attempt))
                    try:
                        trial.remove_key()
                        trial.verify_instance_keys()
                        cleaned = True
                        break
                    except Exception:
                        time.sleep(2)
                if not cleaned:
                    success = False
                    emit("TEMPORARY_SSH_KEY_CLEANUP=UNCONFIRMED")
                    emit("MANUAL_CLEANUP_KEY_FINGERPRINT=" + fingerprint(trial.blob))
            if trial.project_before is not None:
                try:
                    trial.verify_project_keys()
                except Exception:
                    success = False
                    emit("PROJECT_EXISTING_KEYS_PRESERVED=UNCONFIRMED")
            try:
                trial.close_tunnel()
            except Exception:
                success = False
                emit("IAP_TUNNEL_CLEANUP=UNCONFIRMED")
            trial.token = None
    emit("CI_TEMPORARY_FILES_REMOVED=PASS")
    if success:
        emit("IAP_AUTHENTICATED_TRIAL=PASS")
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as error:
        emit("STOP: " + str(error))
        sys.exit(1)
