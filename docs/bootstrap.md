# 専用CDK配布基盤の初回準備

Step 1の手順2-3で`delta-deneb-login`による本人確認が完了した後に実行する。対象はTokyo（`ap-northeast-1`）の`dev`環境。コマンドはすべてDev Container内のリポジトリルート`/workspaces/delta-deneb`で実行する。

既存の`CDKToolkit`はS3バケットのみの構成なので保持し、`DeltaDenebDevToolkit`という専用スタックを新しく作る。配布バケット・ロールなどの識別子（qualifier）は`denebdev1`を使う。アプリも同じ識別子を参照する設定になっている。

## 1. 更新したコードと依存関係を取得する

```sh
git pull --ff-only
uv sync --locked
npm ci
npm run prepare:bootstrap
npm run synth:offline
```

`bootstrap.out/dev.template.json`に初回準備用のCloudFormationテンプレートを生成する。生成処理と`synth:offline`はAWSに接続しない。

専用ユーザー名を`delta-deneb-deployer`以外にした場合だけ、代わりに次で生成する。

```sh
npm run prepare:bootstrap -- --deployer-user-name 自分で作成したIAMユーザー名
```

## 2. 初回準備用の管理者Profileを作成する

ここだけは、CloudFormation、IAM、S3、ECR、SSMのリソースを作成できる既存の管理者を使う。普段使う`delta-deneb-deployer`へ管理者権限を追加する必要はない。

```sh
aws configure set region ap-northeast-1 --profile delta-deneb-bootstrap
aws configure set output json --profile delta-deneb-bootstrap
aws login --profile delta-deneb-bootstrap --remote
aws sts get-caller-identity --profile delta-deneb-bootstrap
```

表示されたURLをホスト側のブラウザで開き、既存の管理者で認証する。`Account`が`delta-deneb-login`で確認したアカウントと同じこと、`Arn`が初回準備に使う管理者であることを自分で確認する。認証コードは自分のターミナルへ入力し、チャットには送らない。

## 3. テンプレートを確認して専用スタックを作成する

`bootstrap.out/dev.template.json`をVS Codeで開き、以下の「作成されるものと権限の範囲」を確認する。特にCloudFrontの共有設定APIの範囲を確認してから実行する。

```sh
aws cloudformation deploy \
  --template-file bootstrap.out/dev.template.json \
  --stack-name DeltaDenebDevToolkit \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ap-northeast-1 \
  --profile delta-deneb-bootstrap \
  --tags Project=delta-deneb Environment=dev
```

このコマンドはCloudFormationの変更セットを作成して実行し、完了を待つ。既存の`CDKToolkit`を指定したり、引数なしの`cdk bootstrap`を実行したりしない。

作成結果を確認する。

```sh
aws cloudformation describe-stacks \
  --stack-name DeltaDenebDevToolkit \
  --region ap-northeast-1 \
  --profile delta-deneb-bootstrap \
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}'
```

初回は`CREATE_COMPLETE`になっていることを確認する。初回準備用の認証はここで終了できる。

```sh
aws logout --profile delta-deneb-bootstrap
```

## 4. 専用ユーザーでアプリをデプロイする

ここからは`delta-deneb-login`を使う。セッションの有効期限が切れていれば、専用ユーザーで再ログインする。

```sh
aws login --profile delta-deneb-login --remote
aws sts get-caller-identity --profile delta-deneb-login
npm run cdk -- diff DeltaDenebDev --profile delta-deneb-login
npm run cdk -- deploy DeltaDenebDev --profile delta-deneb-login --outputs-file cdk-outputs.dev.json
uv run --locked python scripts/check_deployment.py --outputs cdk-outputs.dev.json
```

ブラウザ認証で`delta-deneb-deployer`を選び、`Arn`でも確認する。デプロイ後は`SiteUrl`をホスト側のブラウザで開いて、画面の動作を確認する。

## 作成されるものと権限の範囲

- 配布用S3バケット、ECRリポジトリ、CDK用IAMロール、bootstrapバージョンを記録するSSMパラメーター、2種類の権限上限ポリシー、専用ユーザーのデプロイ用ポリシーを作成する。ECRはCDK標準テンプレートとの互換性のために作成するが、専用ユーザーにはイメージ配布ロールの利用を許可しない。
- 新しいユーザーやアクセスキーは作成しない。手順2-1で作成済みの`delta-deneb-deployer`へ、専用ロールを引き受ける権限と対象スタック・バージョン情報の読み取り権限を付与する。
- デプロイ操作は`delta-deneb-dev`スタックに限定する。専用ユーザーにbootstrap自体の更新権限は付与しない。配布ロールの信頼も、このユーザーに限定する。
- CloudFormation実行ロールには`AdministratorAccess`を付けず、S3、Lambda、ログ、CloudFront、アプリ用IAMロールの管理に必要な許可を定義する。実行ロール自体にも同じ上限を付ける。
- アプリ用のIAMロールは`delta-deneb-dev-`で始まるものに限定し、必ずランタイム用の権限上限を付ける。アプリのコードからIAM管理・別ロールへの切り替えを許可せず、デプロイ経由で上限を削除・変更する操作も制限する。
- S3、Lambda、ログは名前・アカウント・リージョンで対象を絞る。CloudFrontディストリビューションの更新・削除は`Project=delta-deneb`と`Environment=dev`のタグが付いたものに限定する。これらのタグの除去も禁止する。
- 新規ディストリビューションの作成時にもタグ付け権限が必要なため、識別タグが未設定の既存ディストリビューションへ同じタグを付ける余地は残る。別のProjectやEnvironmentが設定済みのリソースを付け替える操作は許可しない。タグだけによる厳密な所有権分離を保証するものではない。
- **CloudFrontのキャッシュポリシー、オリジンリクエストポリシー、OACの一部APIは、AWSがリソース単位のIAM制限を提供していないため、該当する設定の作成・取得・更新・削除権限がアカウント全体に及ぶ。** 他のシステムが共有するこれらの設定へ影響する余地は残る。アカウント全体からの完全な分離を保証する構成ではない。ロググループ一覧とCloudFormationのテンプレート概要も、メタデータの読み取り権限が残る。

この構成はStep 1のファイル配布とヘルスチェック向け。DynamoDBやIoTへのアクセス権限は含めない。Step 2以降で必要な権限を確認して追加する。

## 検証状況と更新

コンテナ内で、認証なしのテンプレート生成、権限と信頼関係のテスト、アプリのsynthを実行済み。CloudFormationテンプレートは`cfn-lint 1.57.2`でも静的検証した。アプリのテンプレートにはCDKが出力する重複した依存関係の警告（W3005）があるため、その警告のみを除外して確認した。AWSへの作成・デプロイとIAMの実際の許可判定はユーザーの実行後に確認する。

生成元は固定済みのAWS CDK CLI `2.1145.0`に同梱されたbootstrap version 32。CLIまたはbootstrapのバージョンが変わった場合は生成処理を止め、権限の差分を確認してから更新する。専用基盤の更新も管理者Profileで同じ手順を使う。

`prod`では`--stage prod`で`bootstrap.out/prod.template.json`を生成し、スタック名`DeltaDenebProdToolkit`、qualifier `denebprd1`を使う。アプリ側も`-c stage=prod --toolkit-stack-name DeltaDenebProdToolkit`を指定する。この手順の`dev`向けコマンドと混在させない。

参考: [CDK bootstrapのカスタマイズ](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-customizing.html)、[CDKのセキュリティ](https://docs.aws.amazon.com/cdk/v2/guide/best-practices-security.html)、[CloudFrontのIAM対応範囲](https://docs.aws.amazon.com/service-authorization/latest/reference/list_cloudfront.html)、[ブラウザによるAWS CLI認証](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)。
