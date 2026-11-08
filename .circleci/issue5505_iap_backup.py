#!/usr/bin/env python3
"""Opt-in IAP backup trial. No VM stop, firewall change or project metadata write."""

import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid

import issue5505_iap_ssh as iap
from issue5505_ansible_diagnostics import check_modules

BRANCH = "test/issue5505-iap-backup-r01"
BUCKET = "mamono210"
OWNER = "808683561341"
AWS_ROLE = "arn:aws:iam::808683561341:role/CircleCI_OIDC_circleci-gce"
PREFIX_ROOT = "circleci-gce/redmine/issue5505-test/iap-backup/"
FILES = ("files.tar.gz", "git.tar.gz", "redmine.sql")
BACKUP_DIR = "/tmp/redmine-backup"
WORK_SECONDS = 45 * 60
SOURCE_FILE = Path(__file__).with_name("issue5505_backup_sources.json")


def validate_objects(sizes, objects):
    iap.require(set(sizes) == set(FILES) == set(objects), "Expected exactly three backup files")
    for name in FILES:
        iap.require(type(sizes[name]) is int and sizes[name] > 0, "Invalid VM backup size")
        item = objects[name]
        iap.require(item.get("Size") == sizes[name], "S3 size differs: " + name)
        iap.require(item.get("Encryption") == "AES256", "Unexpected S3 encryption: " + name)
        iap.require(bool(item.get("LastModified")), "S3 modification time missing")


def snapshot_values(output):
    values = {}
    for key in ("BOOT_ID", "SERVICE_START", "MITIGATION_SHA256", "FIREWALLD_SHA256"):
        matches = re.findall(r"^" + key + r"=(.+)$", output, re.MULTILINE)
        iap.require(len(matches) == 1, "Missing or duplicate snapshot field: " + key)
        values[key] = matches[0]
    iap.require(re.fullmatch(r"[a-f0-9-]{36}", values["BOOT_ID"]), "Invalid boot ID")
    iap.require(values["SERVICE_START"].isdigit() and int(values["SERVICE_START"]) > 0,
                "Invalid service start timestamp")
    for key in ("MITIGATION_SHA256", "FIREWALLD_SHA256"):
        iap.require(re.fullmatch(r"[a-f0-9]{64}", values[key]), "Invalid ruleset digest")
    return values


def inventory(work):
    # These options apply to SSH and SFTP. Disable connection sharing so each
    # operation authenticates using this trial's key and the pinned host key.
    options = ["-F", "/dev/null"]
    for value in (
        "HostKeyAlias=issue5505-production", "StrictHostKeyChecking=yes",
        "CheckHostIP=no", "UpdateHostKeys=no", "HostKeyAlgorithms=ssh-ed25519",
        "UserKnownHostsFile=" + str(work / "known_hosts"), "GlobalKnownHostsFile=/dev/null",
        "IdentityAgent=none", "IdentitiesOnly=yes", "BatchMode=yes",
        "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
        "PreferredAuthentications=publickey", "ControlMaster=no", "ControlPath=none",
        "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes",
        "ProxyCommand=none", "ProxyJump=none", "ConnectTimeout=10",
        "ConnectionAttempts=1", "ServerAliveInterval=10", "ServerAliveCountMax=2",
    ):
        options.extend(["-o", value])
    return {"all": {"hosts": {"issue5505_target": {
        "ansible_connection": "ssh", "ansible_host": "127.0.0.1", "ansible_port": 22022,
        "ansible_user": "centos", "ansible_ssh_private_key_file": str(work / "id_rsa"),
        "ansible_python_interpreter": "/usr/bin/python3.9", "ansible_host_key_checking": True,
        "ansible_ssh_args": shlex.join(options), "ansible_ssh_common_args": "",
        "ansible_ssh_extra_args": "", "ansible_sftp_extra_args": "",
        "ansible_ssh_transfer_method": "sftp", "ansible_ssh_use_tty": False,
        "ansible_become_method": "sudo", "ansible_become_user": "root",
        "ansible_become_flags": "-H -S -n",
    }}}}


def upload_play():
    # Reuse the existing upload role with explicit destination safeguards. All
    # tasks are no_log because the role resolves AWS session credentials.
    return [{"name": "Upload three trial backups through the IAP-managed host",
             "hosts": "all", "become": True, "gather_facts": False, "no_log": True,
             "module_defaults": {"amazon.aws.s3_object": {
                 "expected_bucket_owner": OWNER, "encrypt": True,
                 "encryption_mode": "AES256", "permission": ["private"], "overwrite": "never"}},
             "tasks": [{"name": "Upload " + name,
                        "ansible.builtin.include_role": {"name": "mamono210.aws_s3_cp",
                                                          "apply": {"no_log": True}},
                        "vars": {"s3_bucket": BUCKET,
                                 "s3_object_name": "{{ trial_s3_prefix }}/" + name,
                                 "upload_file_path": BACKUP_DIR + "/" + name}}
                       for name in FILES]}]


class BackupTrial(iap.Trial):
    branch = BRANCH
    key_minutes = 60
    success_marker = "IAP_BACKUP_TRIAL=PASS"

    def remaining(self, maximum=60):
        seconds = self.deadline - time.monotonic()
        iap.require(seconds > 0, "45-minute trial work deadline reached; cleanup follows")
        return min(maximum, seconds)

    def aws(self, args):
        return iap.run(["aws", *args, "--no-cli-pager", "--cli-connect-timeout", "10",
                        "--cli-read-timeout", "30"], timeout=self.remaining()).stdout

    def initialize(self):
        iap.require(os.environ.get("CIRCLECI") == "true" and os.environ.get("CIRCLE_BRANCH") == self.branch,
                    "Run only in the approved backup trial branch")
        self.deadline = time.monotonic() + WORK_SECONDS
        iap.require(os.environ.get("AWS_ROLE_ARN") == AWS_ROLE, "Unexpected AWS_ROLE_ARN")
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            iap.require(bool(os.environ.get(name)), "Required AWS session environment is missing: " + name)
        iap.require(not any(value for name, value in os.environ.items() if name.startswith("AWS_ENDPOINT_URL")),
                    "AWS endpoint override present")
        caller = json.loads(self.aws(["sts", "get-caller-identity", "--output", "json"]))
        iap.require(caller.get("Account") == OWNER and caller.get("Arn", "").startswith(
            "arn:aws:sts::808683561341:assumed-role/CircleCI_OIDC_circleci-gce/"), "Unexpected AWS caller")
        iap.emit("AWS_ROLE_CHECK=PASS")
        self.prepare_destination()
        self.prepare_dependencies()
        super().initialize()

    def prepare_destination(self):
        build = os.environ.get("CIRCLE_BUILD_NUM", "")
        iap.require(build.isdigit(), "Missing CircleCI build number")
        self.prefix = PREFIX_ROOT + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.prefix += "-" + build + "-" + uuid.uuid4().hex
        iap.emit("S3_TRIAL_PREFIX=s3://" + BUCKET + "/" + self.prefix + "/")
        listing = json.loads(self.aws([
            "s3api", "list-objects-v2", "--bucket", BUCKET, "--expected-bucket-owner", OWNER,
            "--prefix", self.prefix + "/", "--max-keys", "1", "--no-paginate", "--output", "json"]))
        iap.require(listing.get("KeyCount") == 0 and not listing.get("Contents"), "S3 trial prefix is not empty")
        iap.emit("S3_NEW_PREFIX_CHECK=PASS")

    def build_upload_play(self):
        return upload_play()

    def read_saved_object(self, name):
        return json.loads(self.aws([
            "s3api", "head-object", "--bucket", BUCKET, "--expected-bucket-owner", OWNER,
            "--key", self.prefix + "/" + name, "--query",
            "{Size:ContentLength,LastModified:LastModified,Encryption:ServerSideEncryption,VersionId:VersionId}",
            "--output", "json"]))

    def validate_saved_objects(self, sizes, objects):
        validate_objects(sizes, objects)

    def prepare_dependencies(self):
        lock = json.loads(SOURCE_FILE.read_text())
        iap.require(lock.get("version") == 1, "Unsupported dependency manifest")
        self.deps = self.work / "deps"
        self.deps.mkdir()
        prepare_deadline = time.monotonic() + 300
        for source in lock["sources"]:
            relative = Path(source["path"])
            iap.require(not relative.is_absolute() and ".." not in relative.parts, "Invalid source path")
            iap.require(re.fullmatch(r"[a-f0-9]{40}", source["commit"]), "Source must be pinned to a commit")
            iap.require(re.fullmatch(r"https://github\.com/ansible-(?:roles|playbooks)-mamono210/[a-z0-9_-]+\.git",
                                    source["url"]), "Unexpected dependency repository")
            destination = self.deps / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            iap.emit("FETCH_SOURCE=" + source["path"] + " COMMIT=" + source["commit"])
            for args in (
                ["git", "clone", "--quiet", "--no-checkout", "--", source["url"], str(destination)],
                ["git", "-C", str(destination), "checkout", "--quiet", "--detach", source["commit"]],
            ):
                left = prepare_deadline - time.monotonic()
                iap.require(left > 0, "Dependency preparation exceeded five minutes")
                iap.run(args, timeout=min(self.remaining(120), left))
            actual = iap.run(["git", "-C", str(destination), "rev-parse", "HEAD"]).stdout.strip()
            iap.require(actual == source["commit"], "Dependency commit mismatch")
        # No remote package installation. Check that the executor has both modules.
        iap.require(check_modules(timeout=self.remaining()),
                    "Ansible dependency check failed; see ANSIBLE_PROBE diagnostics above")
        self.prepare_ansible_files()
        self.syntax_check()
        iap.emit("PINNED_DEPENDENCIES_READY=PASS")

    def syntax_check(self):
        for playbook in (self.deps / "playbooks/backup/create-backup.yml", self.work / "upload.json"):
            result = subprocess.run([
                "ansible-playbook", "--syntax-check", "-i", str(self.work / "inventory.json"), str(playbook)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=self.remaining(),
                cwd=self.work, env=self.ansible_environment())
            iap.require(result.returncode == 0, "Ansible syntax check failed: " + playbook.name)
        iap.emit("ANSIBLE_SYNTAX_CHECK=PASS")

    def prepare_ansible_files(self):
        (self.work / "inventory.json").write_text(json.dumps(inventory(self.work)))
        (self.work / "upload.json").write_text(json.dumps(self.build_upload_play()))
        (self.work / "ansible.cfg").write_text(
            "[defaults]\nno_log = true\nhost_key_checking = true\nstdout_callback = default\n"
            "display_args_to_stdout = false\nretry_files_enabled = false\n"
            "roles_path = " + str(self.deps / "roles") + "\n"
            "[ssh_connection]\nretries = 0\n")

    def remote(self, script):
        iap.require(self.tunnel.poll() is None, "IAP tunnel stopped")
        return iap.run(self.ssh_arguments(), input=script, timeout=self.remaining()).stdout

    def snapshot(self, label):
        output = self.remote(SNAPSHOT)
        values = snapshot_values(output)
        for key, value in values.items():
            iap.emit(label + "_" + key + "=" + value)
        return values

    def ansible_environment(self):
        # Remove inherited Ansible overrides, then explicitly configure no_log.
        env = {key: value for key, value in os.environ.items() if not key.startswith("ANSIBLE_")}
        # The shared trial uses LC_ALL=C. Override it for every Ansible child,
        # including syntax checks and both backup/upload playbooks.
        env.update(LC_ALL="C.UTF-8", LANG="C.UTF-8",
                   ANSIBLE_CONFIG=str(self.work / "ansible.cfg"), ANSIBLE_NO_LOG="true",
                   ANSIBLE_DEBUG="false", ANSIBLE_VERBOSITY="0", ANSIBLE_DISPLAY_ARGS_TO_STDOUT="false",
                   ANSIBLE_ROLES_PATH=str(self.deps / "roles"), ANSIBLE_HOST_KEY_CHECKING="true",
                   ANSIBLE_NO_TARGET_SYSLOG="true", ANSIBLE_FORCE_COLOR="false")
        return env

    def run_playbook(self, playbook, variables, stage):
        values_file = self.work / "variables.json"
        values_file.write_text(json.dumps(variables))  # Only paths and non-secret settings.
        args = ["ansible-playbook", "-i", str(self.work / "inventory.json"),
                "--extra-vars", "@" + str(values_file), str(playbook)]
        iap.emit(stage + "=START")
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, env=self.ansible_environment(),
                                   cwd=self.work, start_new_session=True)
        next_heartbeat = time.monotonic() + 30
        try:
            while process.poll() is None:
                self.remaining()
                iap.require(self.tunnel.poll() is None, "IAP tunnel stopped during " + stage)
                if time.monotonic() >= next_heartbeat:
                    iap.emit(stage + "=RUNNING")
                    next_heartbeat = time.monotonic() + 30
                time.sleep(1)
            iap.require(process.returncode == 0, stage + " failed; exit=" + str(process.returncode))
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                iap.emit("REMOTE_TASK_COMPLETION=UNCONFIRMED; inspect VM before any retry")
        iap.emit(stage + "=PASS")

    def ssh_check(self):
        super().ssh_check()
        before = self.snapshot("BEFORE")
        iap.emit(self.remote(PRECHECK).rstrip())
        self.remote_started = int(self.remote("set -eu\ndate -u +%s\n").strip())
        # Override only the already-used output path; source selection stays in the pinned playbook.
        self.run_playbook(self.deps / "playbooks/backup/create-backup.yml",
                          {"redmine_backup_data_dir": BACKUP_DIR}, "BACKUP_CREATE")
        files = json.loads(self.remote(FILE_STATS))
        iap.require(set(files) == set(FILES), "Unexpected backup file list")
        for name in FILES:
            iap.require(files[name]["size"] > 0 and files[name]["mtime"] >= self.remote_started - 1,
                        "Backup is empty or predates this run: " + name)
            iap.emit("VM_BACKUP_FILE=" + name + " SIZE=" + str(files[name]["size"]))
        sizes = {name: files[name]["size"] for name in FILES}
        self.run_playbook(self.work / "upload.json", {"trial_s3_prefix": self.prefix}, "S3_UPLOAD")
        objects = {}
        for name in FILES:
            objects[name] = self.read_saved_object(name)
        self.validate_saved_objects(sizes, objects)
        iap.emit("S3_VERIFIED_OBJECTS=" + json.dumps(objects, sort_keys=True))
        iap.emit("S3_SIZE_AND_ENCRYPTION_CHECK=PASS")
        iap.require(json.loads(self.remote(FILE_STATS)) == files, "VM backup files changed during upload")
        iap.require(self.snapshot("AFTER") == before, "Boot, mitigation service or firewall rules changed")
        iap.emit("MITIGATION_AND_FIREWALL_UNCHANGED=PASS")


SNAPSHOT = iap.REMOTE_CHECK + r'''
cve_boot="$(cat /proc/sys/kernel/random/boot_id)"
cve_start="$(sudo -n systemctl show cve-2026-98432-mitigation.service -p ExecMainStartTimestampMonotonic --value)"
cve_mitigation_sha="$(sudo -n nft --stateless list table inet cve_2026_98432 | sha256sum | awk '{print $1}')"
cve_firewalld_sha="$(sudo -n nft --stateless list table inet firewalld | sha256sum | awk '{print $1}')"
printf 'BOOT_ID=%s\nSERVICE_START=%s\nMITIGATION_SHA256=%s\nFIREWALLD_SHA256=%s\n' \
  "$cve_boot" "$cve_start" "$cve_mitigation_sha" "$cve_firewalld_sha"
'''

PRECHECK = r'''set -euo pipefail
sudo -n /usr/bin/python3.9 - <<'PY'
import json, os, pathlib, shutil
import boto3, botocore
p = pathlib.Path('/tmp/redmine-backup')
assert not p.is_symlink(), 'Backup directory must not be a symlink'
if p.exists():
    assert p.is_dir(), 'Backup path is not a directory'
for name in ('files.tar.gz', 'git.tar.gz', 'redmine.sql'):
    target = p / name
    assert not target.is_symlink(), 'Backup file must not be a symlink'
    if target.exists():
        assert target.is_file(), 'Backup path is not a regular file'
free = shutil.disk_usage(p if p.exists() else p.parent).free
print('BACKUP_FREE_BYTES=' + str(free))
assert free >= 10 * 1024**3, 'Less than 10 GiB free; review storage before the trial'
print('REMOTE_BOTO3_VERSION=' + boto3.__version__)
print('REMOTE_BOTOCORE_VERSION=' + botocore.__version__)
print('BACKUP_PRECHECK=PASS')
PY
'''

FILE_STATS = r'''set -euo pipefail
sudo -n /usr/bin/python3.9 - <<'PY'
import json, pathlib
root = pathlib.Path('/tmp/redmine-backup')
assert not root.is_symlink()
result = {}
for name in ('files.tar.gz', 'git.tar.gz', 'redmine.sql'):
    path = root / name
    assert path.is_file() and not path.is_symlink()
    st = path.stat()
    result[name] = {'size': st.st_size, 'mtime': st.st_mtime, 'inode': st.st_ino}
print(json.dumps(result, sort_keys=True))
PY
'''


if __name__ == "__main__":
    try:
        sys.exit(iap.main(BackupTrial))
    except iap.Stop as error:
        iap.emit("STOP: " + str(error))
        sys.exit(1)
