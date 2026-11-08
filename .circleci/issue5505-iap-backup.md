# Issue 5505: IAP 経由のバックアップ試験

> この文書は試験版 `11719dbe00f875bc1869198e6ad2de836edb4bd5` の記録です。
> #5515 の通常運用設定では、以下の試験用 pipeline parameter / workflow は使用しません。
> 現在の起動条件・停止条件・移行手順は [redmine-iap-operation.md](redmine-iap-operation.md) を参照してください。

この設定は準備用。実機試験は、人間が対象コミットを確認して手動起動する。

## Ansible 事前確認の診断

Pipeline #719 は `ansible-doc` の終了コード 1 で、IAP 接続や一時鍵登録より前に停止した。
当時のコードは標準エラーを表示していないため、ログだけでは原因を特定できない。

- 同じ branch で `run-iap-ansible-diagnostics=true`、その他のパラメーターをすべて false にする。
- workflow / job は `iap-ansible-diagnostics`。AWS/GCP context やセットアップを使用しない。
- CI の Python / Ansible バージョン、関連コレクション、元の一括問い合わせと
  モジュール個別の `ansible-doc` の終了コード・標準エラーを確認する。
- 診断用の子プロセスにはクラウド認証情報や Ansible の環境変数上書きを渡さない。
  一時ディレクトリと専用設定を使い、インベントリ・playbook を読み込まない。
- 診断は VM 接続、SSH 鍵登録、バックアップ、S3 操作、パッケージ追加を行わない。
  診断に成功しても本番試験には進まない。結果を確認してからバックアップ試験を別途起動する。
- `latest` イメージは可変なので、#719 と診断ジョブの環境が完全に同じとは限らない。
  両ジョブの起動ログにあるイメージ digest も比較する。

通常のバックアップ試験でも同じ診断を使い、必要なモジュールや S3 所有者確認オプションを
確認できなければ、一時鍵登録より前に停止する。

Pipeline #721 で `LC_ALL=C` による Ansible の起動エラーを確認した。
診断・構文確認・バックアップ作成・S3 アップロードの Ansible 子プロセスには
`LC_ALL=C.UTF-8` と `LANG=C.UTF-8` を渡す。CI イメージでの実行結果は診断ジョブで確認する。
モジュール情報を取得できない場合、S3 所有者確認オプションの対応状況は `UNKNOWN` とする。
Linux のロケール回帰テストは Mac ではスキップし、Linux CI で実行する。

## 対象と起動条件

- branch: `test/issue5505-iap-backup-r01`
- parameter: `run-iap-backup-test=true`（既定値は false）
- workflow: `iap-backup-test`
- job: `iap-backup-check`
- contexts: `AWS_OPENID_CONNECT_TOKENS`, `GOOGLE_CLOUD_IAM`
- VM: `buoyant-world-263612 / us-east1-d / redmica2-south-carolina`
- instance ID: `7213092591087619377`
- SSH: `centos`、IAP トンネルの `127.0.0.1:22022` 経由

同じ VM の通常バックアップや別の試験を重ねて実行しない。
既存の接続確認用パラメーターは false のままにする。

## 実行時の変更と確認

1. CI の AWS ロール、対象バケットの所有者、未使用の保存先を確認する。
2. `issue5505_backup_sources.json` のコミットで依存リポジトリを固定する。
   バックアップ用 playbook は前回の実行と同じコミットを使用する。
3. CI で Ansible の必要モジュールと構文を確認する。
4. GCP の実行主体、VM ID、IAP 接続先の既知の ED25519 ホスト鍵を確認する。
5. 60 分有効の RSA 3072 bit 鍵を生成し、対象 VM の `ssh-keys` に一時登録する。
   既存鍵・その他のメタデータを保持する。プロジェクトメタデータへの書き込みは行わない。
6. SSH、sudo、暫定緩和策、既存の Python 3.9 / boto3 / botocore を確認する。
   `/tmp/redmine-backup` のあるファイルシステムに 10 GiB 以上の空きが必要。
7. 既存の `/tmp/redmine-backup/files.tar.gz`, `git.tar.gz`, `redmine.sql` を作り直す。
   固定した既存ロールを使用し、バックアップ対象はメディア、`/var/git`、Redmine DB。
8. VM から S3 へ 3 ファイルをアップロードする。Ansible の制御接続が IAP 経由であり、
   S3 データ転送そのものは従来どおり VM から S3 への HTTPS 通信。
9. S3 の所有者・サイズ・AES256 暗号化を確認し、VM 上のファイルがアップロード中に
   変わっていないこと、起動 ID・緩和策の開始時刻・緩和策と firewalld のルールが
   試験前後で同じことを確認する。
10. 試験鍵だけを削除し、既存鍵の保持を確認する。CI の一時鍵・取得した依存ソース・
    一時ファイルを削除する。正常終了時には VM 上と S3 上のバックアップを残す。

保存先は次の形式で毎回生成し、今回作成するオブジェクトの ACL は private とする。

```text
s3://mamono210/circleci-gce/redmine/issue5505-test/iap-backup/<UTC時刻>-<build番号>-<ランダムID>/
```

この試験は VM 停止、再起動、ファイアウォール変更、IAM 変更、main へのマージを含まない。
既存のパッケージを使用し、VM への追加インストールは行わない。
バックアップ・S3 ロールの引数と結果は `no_log` で抑制し、認証情報をログへ出さない。

## 時間制限と失敗時

- 通常処理は準備を含め 45 分を上限とし、鍵と認証の有効期間内に後片付けする余地を確保する。
- 失敗や通常の中断でも鍵削除を試みる。削除が確認できなければ全体を失敗にする。
- CI の強制終了・基盤障害時には後片付けが実行されない場合がある。
  `TRIAL_KEY_FINGERPRINT` / `MANUAL_CLEANUP_KEY_FINGERPRINT` を使って対象鍵を特定する。
  期限切れはメタデータ行の削除や既存 SSH セッションの切断を意味しない。
- CI の子プロセスを停止しても VM 上の処理が残る可能性がある。
  タイムアウト・失敗後は、VM の処理と途中ファイル、鍵削除結果を確認してから再実行する。
- 部分的に作成された VM / S3 バックアップは自動削除しない。保存先をログで特定して判断する。
- サイズ照合は、全内容のハッシュ一致や復元成功を証明するものではない。

## 成功ログ

```text
ANSIBLE_SYNTAX_CHECK=PASS
BACKUP_PRECHECK=PASS
BACKUP_CREATE=PASS
S3_UPLOAD=PASS
S3_SIZE_AND_ENCRYPTION_CHECK=PASS
MITIGATION_AND_FIREWALL_UNCHANGED=PASS
TEMPORARY_SSH_KEY_REMOVED=PASS
INSTANCE_EXISTING_KEYS_PRESERVED_AFTER_CLEANUP=PASS
PROJECT_EXISTING_KEYS_PRESERVED=PASS
CI_TEMPORARY_FILES_REMOVED=PASS
IAP_BACKUP_TRIAL=PASS
```

パイプライン URL、コミット、保存先、3 ファイルのサイズ、最終ログを人間が確認する。
既存の公開 SSH を制限する前に、通常運用の CI と Git 操作の移行も別途検証する。

## 検証

```bash
python3 -m unittest discover -s .circleci/tests -p 'test_issue5505_*.py'
circleci config validate .circleci/config.yml
```

単体テストはクラウドや VM へ接続しない。実機接続や S3 保存の成功は実際の CI ログで確認する。
