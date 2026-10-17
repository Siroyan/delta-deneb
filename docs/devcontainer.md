# Dev Containerでの開発

## 開き方

ホスト側に必要なのは、起動済みのDocker環境（Docker Desktopなど）とVS Code・Dev Containers拡張。Python、Node.js、npm、uv、AWS CLIをホストへ追加する必要はない。

1. VS Codeで`delta-deneb`のリポジトリフォルダを開く。
2. コマンドパレットで「Dev Containers: Reopen in Container」を実行する。
3. 初回はイメージのビルドと`uv sync --locked`・`npm ci`が完了するまで待つ。
4. VS Codeの新しいターミナルを開く。作業フォルダは`/workspaces/delta-deneb`。

コンテナにはPython 3.13、Node.js 24（npmを含む）、uv 0.12.24、AWS CLI v2、Gitが入る。Apple Silicon（ARM64）とIntel / AMD（x86_64）の両方に対応する構成。

## 普段の操作

次のコマンドは、すべてコンテナ内のターミナルで実行する。

```sh
npm test
npm run test:infra
npm run synth:offline
npm run preview
```

`npm run preview`を起動したら、VS Codeの「ポート」から8080番を開く。通常はホストの <http://127.0.0.1:8080/> で表示できる。ホスト側の8080番が使用中なら、「ポート」に表示された転送先の番号を使う。停止はターミナルで`Ctrl+C`。

テストと`synth:offline`はAWSへ接続せず、AWSの認証設定なしで使える。生成した`cdk.out/`はリポジトリ配下に保存される。

依存関係の定義・ロックファイルを更新した場合は、次で同期する。

```sh
uv sync --locked
npm ci
```

コンテナ設定やDockerfileを変更した場合は「Dev Containers: Rebuild Container」を実行する。

## ファイルと依存環境

ソースコードはホストのリポジトリを共有する。コンテナ内で編集した内容はホストのGitにも反映される。

- Pythonの仮想環境はコンテナ内の`/home/vscode/.venvs/delta-deneb`に作成し、既存のホスト側`.venv/`と分離する。
- `node_modules/`はコンテナ専用のDockerボリュームを重ねてマウントし、ホストのパッケージと分離する。初回作成時に開発ユーザーへ所有権を設定する。
- 依存関係は`uv.lock`と`package-lock.json`から復元する。新しくコンテナを作った場合も初期化処理で揃う。
- ビルドのコンテキストは`.devcontainer/`だけとし、アプリのソースやホストの認証ファイルをイメージへコピーしない。

## AWSの認証情報

このプロジェクトではIAMユーザーのアクセスキーで認証し、Profile名は`delta-deneb-dev`を使う。ホストの`~/.aws`、AWS認証用の環境変数、Dockerソケットをこの設定から自動で渡すことはない。初期化処理でもAWSの認証設定やデプロイは行わない。

ユーザー自身がDev Container内のターミナルで実行する。

```sh
aws configure --profile delta-deneb-dev
```

質問に従って、アクセスキーID・シークレットアクセスキー・リージョン`ap-northeast-1`・出力形式`json`を入力する。キーはチャットやリポジトリへ送らず、対話形式の入力欄に入力する。

接続先を確認する。

```sh
aws sts get-caller-identity --profile delta-deneb-dev --query Account --output text
```

表示されたアカウントIDがデプロイ先と一致することを確認する。キーの取得方法と各入力項目は[Step 1のProfile設定手順](step1-deployment.md#2-iamユーザーのアクセスキーでprofileを設定する)を参照。

認証情報はコンテナ内の`~/.aws/credentials`、リージョン・出力形式は`~/.aws/config`に保存される。コンテナを停止・再開した場合は維持されるが、作り直した場合は`aws configure --profile delta-deneb-dev`で再設定する。

認証後のbootstrap・diff・deployは[Step 1のデプロイ手順](step1-deployment.md)を参照。

## 確認状況

2026-10-17にARM64のDocker環境で、イメージのビルド、開発ユーザーによる初期化、JavaScriptの16件・Pythonの25件のテストを確認した。`npm run synth:offline`でTokyo向けの18リソースを生成でき、`npm run preview`からHTML・JavaScript・コースJSONを取得できることも確認済み。

確認にはリポジトリの共有マウントと専用の`node_modules/`ボリュームを使い、ホストのPython環境・Nodeパッケージと分離した。AWSへの接続・認証操作は行っていない。VS Codeでの接続操作とポート転送そのものは、この検証には含めていない。

参考: [Dev ContainerのDockerfile設定](https://containers.dev/guide/dockerfile)、[uvとDocker](https://docs.astral.sh/uv/guides/integration/docker/)、[AWS CLI v2のインストール](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)。
