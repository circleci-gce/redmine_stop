#!/usr/bin/env python3
"""Production backup adapter. VM stop is owned by the supervising process."""

import json
import os
import re
import sys

import issue5505_iap_backup as backup
import issue5505_iap_ssh as iap

PREFIX = "circleci-gce/redmine/stop"
RUN_ID_ENV = "REDMINE_BACKUP_RUN_ID"


def check_context():
    iap.require(os.environ.get("CIRCLECI") == "true", "Run only in CircleCI")
    iap.require(os.environ.get("CIRCLE_BRANCH") == "main", "Production execution requires main")
    iap.require(os.environ.get("REDMINE_INSTANCE") == iap.VM, "Unexpected instance-name parameter")
    iap.require(os.environ.get("REDMINE_ZONE") == iap.ZONE, "Unexpected zone parameter")
    iap.require(os.environ.get("CIRCLE_BUILD_NUM", "").isdigit(), "Missing CircleCI build number")


class ProductionBackup(backup.BackupTrial):
    branch = "main"
    success_marker = "PRODUCTION_IAP_BACKUP_AND_CLEANUP=PASS"

    def initialize(self):
        check_context()
        self.run_id = os.environ.get(RUN_ID_ENV, "")
        iap.require(re.fullmatch(r"[a-f0-9]{32}", self.run_id), "Missing or invalid backup run ID")
        iap.emit("BACKUP_RUN_ID=" + self.run_id)
        super().initialize()

    def prepare_destination(self):
        self.prefix = PREFIX
        self.aws(["s3api", "head-bucket", "--bucket", backup.BUCKET,
                  "--expected-bucket-owner", backup.OWNER])
        iap.emit("S3_PRODUCTION_PREFIX=s3://" + backup.BUCKET + "/" + self.prefix + "/")
        iap.emit("S3_BUCKET_OWNER_CHECK=PASS")

    def build_upload_play(self):
        play = backup.upload_play()
        play[0]["name"] = "Upload three production backups through the IAP-managed host"
        defaults = play[0]["module_defaults"]["amazon.aws.s3_object"]
        defaults.update(overwrite="always", metadata={"backup-run-id": self.run_id})
        return play

    def read_saved_object(self, name):
        # Validate size, encryption and this run's metadata from one HEAD result.
        # Do not print arbitrary object metadata in the CI log.
        iap.require(name in backup.FILES, "Unexpected backup object")
        return json.loads(self.aws([
            "s3api", "head-object", "--bucket", backup.BUCKET,
            "--expected-bucket-owner", backup.OWNER, "--key", self.prefix + "/" + name,
            "--query", '{Size:ContentLength,LastModified:LastModified,'
            'Encryption:ServerSideEncryption,VersionId:VersionId,RunId:Metadata."backup-run-id"}',
            "--output", "json"]))

    def validate_saved_objects(self, sizes, objects):
        super().validate_saved_objects(sizes, objects)
        for name in backup.FILES:
            iap.require(objects[name].get("RunId") == self.run_id,
                        "S3 object does not belong to this backup run: " + name)
        iap.emit("S3_CURRENT_RUN_CHECK=PASS")


if __name__ == "__main__":
    try:
        sys.exit(iap.main(ProductionBackup))
    except iap.Stop as error:
        iap.emit("STOP: " + str(error))
        sys.exit(1)
