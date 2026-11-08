# #5515: 通常の Redmine バックアップと VM 停止を IAP 経由へ移行

## この版の状態

`circleci-gce/redmine_stop` の `5515` ブランチ、
`9ebd32141975243b98a6a75539bc33ee1fe09abe` を適用基点とする準備版。
同コミットには #5505 の試験用設定が入っていたため、通常運用用の設定へ置き換える。
通常運用版の実機実行・main へのマージ・スケジュール切り替えは未実施。

この文書はコードと一緒に Git 管理する。
ZIP に付属する `docs/issue5515-package/` は配布時の説明・検証記録であり、コミット不要。
既存の試験手順は `issue5505-iap-backup.md` に履歴として残す。

## 実行条件と API 互換性

| 項目 | 値・動作 |
| --- | --- |
| workflow | `stop-redmine` |
| オフライン検査 job | `check-code`。AWS/GCP context を指定しない |
| 実処理 job | `build`。`check-code` 成功後、`main` のみ |
| `instance-name` | `redmica2-south-carolina` と完全一致が必要 |
| `zone` | `us-east1-d` と完全一致が必要 |
| `pip-executable`, `pip-package` | 旧 API の受付互換用。通常運用版では使用しない |
| 全 parameter の型・既定値 | string / 空文字列 |
| contexts | `AWS_OPENID_CONNECT_TOKENS`, `GOOGLE_CLOUD_IAM` |
| 直列実行 group | `buoyant-world-263612/us-east1-d/redmica2-south-carolina/backup-stop` |

`5515` に push しても、実処理の `build` は branch filter により実行しない。
`main` へ push しただけで対象 parameter が空の場合は、`build` の事前検査で失敗する。
AWS/GCP セットアップより前に検査し、誤った対象を処理しない。
`main` 上で対象 parameter が一致する実行は、正常終了時に VM 停止まで行う。

旧 `run-iap-*` の boolean parameter はこの通常運用設定から削除した。
試験 branch の workflow を再利用する場合は、その branch の設定・手順を参照する。
API / schedule の既存登録内容と実際の呼出元は、この ZIP 作成では確認・変更していない。
切り替え前に branch・parameter 名・値・context・起動時刻を照合する。
`pip-*` で VM 上へのパッケージ追加を期待していた呼出元は、同じ引数でも動作が変わる。

## 対象と保存先

| 項目 | 固定値 |
| --- | --- |
| GCP project | `buoyant-world-263612` |
| zone / VM | `us-east1-d` / `redmica2-south-carolina` |
| instance ID | `7213092591087619377` |
| CI GCP service account | `circleci-circleci-gce-23011-16@buoyant-world-263612.iam.gserviceaccount.com` |
| SSH user / 接続 | `centos` / IAP の `127.0.0.1:22022` |
| ED25519 host fingerprint | `SHA256:giY6WNa9vKQr7EY/IG79NMqfqa4KRACl9mv6KzJe5XI` |
| AWS role | `arn:aws:iam::808683561341:role/CircleCI_OIDC_circleci-gce` |
| S3 bucket / owner | `mamono210` / `808683561341` |
| 通常保存先 | `s3://mamono210/circleci-gce/redmine/stop/` |

通常保存先の `files.tar.gz`, `git.tar.gz`, `redmine.sql` の 3 個を毎回上書きする。
VM 上でも `/tmp/redmine-backup/` の同名ファイルを作り直す。
IAP は SSH / Ansible の制御接続に使い、ファイル本体は VM から S3 へ直接転送する。

## 処理と停止条件

1. `redmine_stop.py` が branch / parameter を確認し、今回のランダムな run ID を生成する。
2. 別プロセスの `redmine_iap_backup.py` が AWS の実行主体とバケット所有者を確認する。
3. `issue5505_backup_sources.json` の 6 リポジトリを記載コミットで取得する。
   Ansible のモジュール・構文を確認し、子プロセスに `C.UTF-8` を設定する。
4. GCP の実行主体、VM ID、IAP の SSH ホスト鍵を確認する。
5. 60 分の期限付き SSH 鍵を対象 VM に一時登録する。更新には metadata fingerprint を使い、
   同時変更と既存鍵を保持する。プロジェクト metadata は読み取りだけ。
6. SSH / sudo / 暫定緩和策、既存の Python 3.9・boto3・botocore、10 GiB 以上の空きを確認する。
7. バックアップを作成し、S3 に private ACL / AES256 / expected bucket owner を指定して上書きする。
   各オブジェクトの user metadata に `backup-run-id` を設定する。
8. 3 オブジェクトの HEAD でサイズ・AES256・今回の run ID を確認する。
   サイズが同じでも run ID が前回のものなら失敗にする。
   VM 側のサイズ・mtime・inode、boot ID、緩和策の開始時刻とルール、firewalld のルールも照合する。
9. 今回の SSH 鍵だけを削除し、既存の VM / project 鍵の保持を確認する。
   IAP トンネルと CI の一時ファイルを片付ける。
10. 全処理が成功して子プロセスが終了コード 0 を返し、親プロセスがキャンセルを受けていない場合だけ、
    実行主体・VM ID・RUNNING 状態を改めて確認して停止を一度要求する。
11. 同じ VM ID が `TERMINATED` になったことを読み直して全体の成功とする。

バックアップ、S3 検証、鍵削除、既存鍵保持、トンネル削除のいずれかが失敗すれば停止へ進まない。
親プロセスは子プロセスとは別の signal handler を持ち、後片付け中のキャンセルも記録する。

停止要求を送る直前までのキャンセルでは、新しい停止要求を行わない。
`VM_STOP_REQUESTING` 以降は停止 API が受理済みの可能性があり、キャンセルによる取り消しは保証できない。
停止要求失敗・タイムアウト・状態不明時は自動で再要求せず、現状確認を必要とする。

## 時間制限と障害時の扱い

- バックアップ本体は準備を含め 45 分上限。親は 50 分で中断し、最大 180 秒の後片付け猶予を設ける。
- CircleCI の強制終了・基盤障害・SIGKILL では、鍵削除や VM 上の処理停止を保証できない。
  60 分の鍵期限は metadata 行の削除や既存 SSH 接続の切断を保証しない。
- 途中失敗では、VM 側のバックアップ処理、今回の鍵、S3 の各 run ID を確認してから再実行する。
  `TRIAL_KEY_FINGERPRINT` / `MANUAL_CLEANUP_KEY_FINGERPRINT` は対象鍵の特定に使う。
  鍵を期限だけで判断して一括削除しない。
- 固定キー 3 個の上書きはトランザクションではない。失敗すると新旧のファイルが混在しうる。
  一部成功のファイルを自動削除・自動ロールバックしない。
- 同時実行禁止は、この serial group を共有する job に対して有効。
  旧設定・別リポジトリ・手動コマンドのバックアップや VM 停止は排他対象に入らないため、切り替え時に重複を防ぐ。
- 検証はサイズ・暗号化・run ID の確認であり、全ファイルのハッシュ一致や復元試験ではない。
- executor は既存の `ansible:latest` を継承している。playbook / role のコミットは固定しているが、
  Python / Ansible / collection の実際のバージョンは実行時に診断して記録する。

## 段階的な導入

### 1. 5515 branch で確認

ZIP を基点の作業ツリーに展開し、以下を実行する。

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .circleci/tests -p 'test_*.py'
circleci config validate .circleci/config.yml
git diff --stat
git status --short --branch
```

オフライン検証は Python 3.11 以上を使う。Linux の locale テストは Mac では 1 件スキップされる。
`.circleci/` のコード・テスト・この運用文書をレビューしてコミットし、通常の Git サーバー経由で push する。
中継の GitHub 転送ログも確認し、CircleCI のコミット SHA が今回のものか照合する。
`5515` の CI では `check-code` のみが実行されることを確認する。
ここまででは通常 S3 保存先の更新も VM 停止も行わない。

### 2. main へ切り替える準備

既存スケジュール・API 呼出元・旧 job の実行中 / 待機中の有無を確認する。
変更作業は VM が停止してよい時間帯を選び、旧処理との重複をなくしてから行う。
必要な contexts と実行主体の権限、IAP 接続、VM 側の依存パッケージが維持されていることも確認する。
main へのマージと実処理の起動は別の工程としてレビューする。

最初の通常運用実行では、対象コミットと次の parameter を確認する。

```json
{
  "instance-name": "redmica2-south-carolina",
  "zone": "us-east1-d",
  "pip-executable": "",
  "pip-package": ""
}
```

この文書は pipeline を自動起動しない。CircleCI の最新 UI / API と既存の呼出元に合わせて設定する。

### 3. 初回実行の受入確認

実際の CI ログから、少なくとも次の結果を確認する。

```text
PRODUCTION_CONTEXT_CHECK=PASS
AWS_ROLE_CHECK=PASS
S3_BUCKET_OWNER_CHECK=PASS
IAP_SSH_HOST_KEY_CHECK=PASS
BACKUP_CREATE=PASS
S3_UPLOAD=PASS
S3_CURRENT_RUN_CHECK=PASS
S3_SIZE_AND_ENCRYPTION_CHECK=PASS
MITIGATION_AND_FIREWALL_UNCHANGED=PASS
TEMPORARY_SSH_KEY_REMOVED=PASS
INSTANCE_EXISTING_KEYS_PRESERVED_AFTER_CLEANUP=PASS
PROJECT_EXISTING_KEYS_PRESERVED=PASS
CI_TEMPORARY_FILES_REMOVED=PASS
PRODUCTION_IAP_BACKUP_AND_CLEANUP=PASS
VM_STOP_REQUESTING
VM_TERMINATED_CHECK=PASS
PRODUCTION_BACKUP_AND_STOP=PASS
```

pipeline URL、コミット SHA、`BACKUP_RUN_ID`、3 ファイルのサイズ、鍵削除結果、VM 最終状態を記録する。
試験版の #724 成功は、通常保存先への上書きとこの停止処理の成功を証明するものではない。

## 今回の変更範囲外

公開 SSH / HTTP の firewall、IAM の追加変更、カーネル更新、暫定緩和策の解除、Git 接続方式の変更は含まない。
特に公開 SSH を閉じる前には Git の push 経路と全 CI の移行確認が別途必要。
不具合時に旧公開 SSH 設定へ自動で戻す機能は設けない。失敗時は VM 状態とバックアップを確認して次の操作を決める。

## 仕様参照

- [CircleCI configuration reference](https://circleci.com/docs/reference/configuration-reference/)
- [Ansible amazon.aws.s3_object](https://docs.ansible.com/projects/ansible/13/collections/amazon/aws/s3_object_module.html)
- [gcloud compute instances stop](https://docs.cloud.google.com/sdk/gcloud/reference/compute/instances/stop)
