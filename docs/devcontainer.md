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

ホストの`~/.aws`、AWS認証用の環境変数、Dockerソケットをこの設定から自動で渡すことはない。初期化処理でもAWSへのログインやデプロイは行わない。

デプロイするときは、ユーザー自身がコンテナ内で普段の認証方式を設定する。IAM Identity Center（SSO）を利用している場合の例:

```sh
aws configure sso --use-device-code --no-browser
aws sso login --profile YOUR_PROFILE --use-device-code --no-browser
```

案内されたURLをホスト側のブラウザで開き、ユーザー自身が認証する。アクセスキーやトークンをチャットへ送る必要はない。認証設定はコンテナのホームに置かれ、コンテナを作り直すと失われるため再設定する。

認証後のbootstrap・diff・deployは[Step 1のデプロイ手順](step1-deployment.md)を参照。

## 確認状況

2026-10-17にARM64のDocker環境で、イメージのビルド、開発ユーザーによる初期化、JavaScriptの16件・Pythonの25件のテストを確認した。`npm run synth:offline`でTokyo向けの18リソースを生成でき、`npm run preview`からHTML・JavaScript・コースJSONを取得できることも確認済み。

確認にはリポジトリの共有マウントと専用の`node_modules/`ボリュームを使い、ホストのPython環境・Nodeパッケージと分離した。AWSへの接続・認証操作は行っていない。VS Codeでの接続操作とポート転送そのものは、この検証には含めていない。

参考: [Dev ContainerのDockerfile設定](https://containers.dev/guide/dockerfile)、[uvとDocker](https://docs.astral.sh/uv/guides/integration/docker/)、[AWS CLI v2のインストール](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)。
