#!/usr/bin/env python3
"""Inspect installed Ansible documentation locally; no cloud or SSH operations."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

MODULES = ("community.general.archive", "amazon.aws.s3_object")


def safe_output(value):
    # Defence in depth: the diagnostic children do not inherit credentials.
    secrets = set()
    for name, item in os.environ.items():
        if item and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", name, re.I):
            secrets.add(item)
            try:
                data = json.loads(item)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                secrets.update(v for v in data.values() if isinstance(v, str) and v)
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
        value = value.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                   "[PRIVATE KEY REDACTED]", value, flags=re.S)
    value = re.sub(r"(?im)^.*authorization\s*[:=].*$", "[AUTHORIZATION REDACTED]", value)
    value = "".join(c for c in value if c in "\n\t" or c.isprintable())
    return value[:16000] + ("\n[output truncated]" if len(value) > 16000 else "")


def diagnostic_environment(config):
    # Preserve HOME/PATH to find the image's user-installed Ansible collections.
    # Use a private config and empty cwd, without inventory, playbooks or cloud env.
    env = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR") if k in os.environ}
    # Ansible checks the locale encoding at startup; the C locale is not UTF-8.
    env.update(LC_ALL="C.UTF-8", LANG="C.UTF-8", TZ="UTC", PYTHONDONTWRITEBYTECODE="1",
               ANSIBLE_CONFIG=str(config), ANSIBLE_DEBUG="false",
               ANSIBLE_VERBOSITY="0", ANSIBLE_FORCE_COLOR="false",
               ANSIBLE_NOCOLOR="true")
    return env


def probe(args, work, *, show_stdout=False, timeout=60):
    label = " ".join(args)
    print("ANSIBLE_PROBE=" + label, flush=True)
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, cwd=work,
                                env=diagnostic_environment(work / "ansible.cfg"))
    except (OSError, subprocess.TimeoutExpired) as error:
        # Exceptions can contain captured output. Print only the exception type.
        print("ANSIBLE_PROBE_ERROR=" + type(error).__name__, flush=True)
        return None
    print("ANSIBLE_PROBE_EXIT_CODE=" + str(result.returncode), flush=True)
    if result.stderr.strip():
        print("[stderr]\n" + safe_output(result.stderr.rstrip()), flush=True)
    if show_stdout and result.stdout.strip():
        print("[stdout]\n" + safe_output(result.stdout.rstrip()), flush=True)
    return result


def documentation(result):
    if result is None or result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except ValueError:
        print("ANSIBLE_DOC_JSON=INVALID", flush=True)
        return None
    return value if isinstance(value, dict) else None


def check_modules(timeout=60):
    with tempfile.TemporaryDirectory(prefix="issue5505-ansible-doc-") as directory:
        work = Path(directory)
        (work / "ansible.cfg").write_text("[defaults]\nno_log = true\n")
        print("PYTHON_VERSION=" + sys.version.split()[0], flush=True)
        probe(["ansible", "--version"], work, show_stdout=True, timeout=timeout)
        for collection in ("community.general", "amazon.aws"):
            probe(["ansible-galaxy", "collection", "list", collection], work,
                  show_stdout=True, timeout=timeout)

        # Reproduce the original combined query, then isolate each module.
        combined = documentation(probe(["ansible-doc", "-t", "module", "--json", *MODULES],
                                       work, timeout=timeout))
        ready = combined is not None and all(name in combined for name in MODULES)
        print("ANSIBLE_DOC_COMBINED=" + ("PASS" if ready else "FAIL"), flush=True)
        for module in MODULES:
            data = documentation(probe(["ansible-doc", "-t", "module", "--json", module],
                                       work, timeout=timeout))
            entry = data.get(module) if data is not None else None
            doc = entry.get("doc") if isinstance(entry, dict) else None
            options = doc.get("options") if isinstance(doc, dict) else None
            valid = isinstance(options, dict)
            if isinstance(entry, dict) and entry.get("error"):
                print("ANSIBLE_DOC_ERROR=" + safe_output(str(entry["error"])), flush=True)
            print("ANSIBLE_MODULE=" + module + " RESULT=" + ("PASS" if valid else "FAIL"), flush=True)
            ready = ready and valid
            if module == "amazon.aws.s3_object":
                owner_check = valid and "expected_bucket_owner" in options
                owner_status = ("YES" if owner_check else "NO") if valid else "UNKNOWN"
                print("S3_EXPECTED_BUCKET_OWNER_SUPPORTED=" + owner_status, flush=True)
                ready = ready and owner_check
        print("ANSIBLE_DEPENDENCY_CHECK=" + ("PASS" if ready else "FAIL"), flush=True)
        return ready


if __name__ == "__main__":
    sys.exit(0 if check_modules() else 1)
