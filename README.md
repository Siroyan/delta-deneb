# delta-deneb

delta-vegaがMQTTでAWSに送信した走行データを、ピットのメンバーがブラウザで確認するシステム。
ログイン操作なしで共有URLから開き、走行中のデータと結果を自動更新する。

2026-10-12時点で、静的UIと模擬APIを実装。CDKの実装言語はPython。AWS構成は設計案であり、まだAWSにはデプロイしていない。

- リポジトリ: https://github.com/Siroyan/delta-deneb
- ローカル作業フォルダ: `/Users/siroyan/workspace/delta-deneb`

## UIデモの起動

```sh
cd /Users/siroyan/workspace/delta-deneb
python3 -m http.server 8080 --bind 127.0.0.1 --directory web
```

ブラウザで <http://127.0.0.1:8080/> を開く。`web/`がS3に配置する静的コンテンツのルートで、ビルド・外部CDN・追加ライブラリは不要。ES ModulesとコースJSONを読み込むため、`file://`ではなくHTTP経由で開く。

- 現在速度、平均速度、周回、経過時間、GPS由来のコース図、速度グラフ、ラップ一覧、ラップ分析を表示。
- 3秒ごとに模擬APIから取得。走行中／更新停止／APIエラー／GPS欠測／データなし／ゴールを右上で切り替えられる。
- 速度グラフは1分／5分／全体を切り替えられ、CSVに模擬データを書き出せる。
- デモ目標38:30はUI確認用の仮値。大会制限や車載機の設定値ではない。距離とゴール予測は平均速度とコース長からの参考値。
- 左右の余白は12pxで画面幅を使い切り、600px以上では数値4項目を一列に表示する。左側は大きなコース表示、右側は速度グラフ・ラップタイム・走行進捗を縦に並べる。600px未満のスマートフォン幅では1列構成へ切り替わる。
- コース図は北を上にし、輪郭に合わせて余白を詰める。現在位置・ON/OFF地点も同じ座標変換で表示する。ラップ時間はラップパネル、距離・ゴール予測は進捗パネルに表示する。

### 走行戦略の読み込み

「戦略を読み込む」からdelta-vega schema 1のJSONを選択する。「サンプルを試す」ではdelta-vega同梱の`motegi_demo.json`を読み込める。このサンプルは表示確認用で、実戦略ではない。

現在のコースIDは`motegi_oval_2025_full_v2`。全7周のON/OFF地点、コースID、経路ID、重複項目、区間順序、距離範囲、4096バイト上限などを検証する。不適合なファイルを読み込んでも、直前の有効な戦略を保持する。

- コース図の橙色はエンジン使用予定区間、青色は惰性予定区間。コース図は現在の周回を表示する。
- 「詳細」で各周の距離タイムラインとON/OFF距離を確認できる。ここで選ぶ周回は、現在位置表示とは独立したプレビュー。
- 現在のGPSを周回の経路に投影し、次のON/OFF地点までの推定距離を表示する。GPSが欠測・コースから60 m超の場合は距離を表示しない。
- 速度グラフの薄い橙色の背景は、受信GPSと計画を照合した使用予定区間。実際のエンジン稼働を示さない。
- 読み込んだJSONはこのブラウザのlocalStorageに保存し、再読み込み時に復元する。「解除」で保存と重ね合わせを解除できる。車両やAWSにファイルを送信しない。
- 戦略JSONには目標速度・目標時間は含まれないため、画面のデモ目標は変更しない。

仕様の参照元はdelta-vega-v2の`assets/strategy/README.md`と`lib/vega_core/src/domain/strategy.cpp`。同梱コースデータと戦略サンプルの由来は[静的UIの設計メモ](docs/ui-demo.md)を参照。

### データ取得の境界とテスト

`web/js/mock-api.js`の`getLatest()`が取得処理の境界。将来、ここを同一オリジンのLambda APIへの`fetch()`に置き換える。現在のブラウザはAWSを呼ばない。

Node.jsで模擬APIと戦略パーサーのテストを実行できる。追加パッケージは不要。

```sh
node --test tests/*.test.js
```

`latest`内の項目はdelta-vegaのMQTTフィールドに合わせている。ただし`timestamp_ms`は車載機の時計であり、UNIX時刻として表示しない。受信時刻の表示には別の`received_at_ms`を使用する。`phase`、ラップ履歴、表示用位置などはデモ用の補助情報で、Lambdaの実応答形式はまだ確定していない。

## 構成案

```mermaid
flowchart LR
    Vega[delta-vega] -->|MQTT| IoT[AWS IoT Core]
    IoT -->|既存ルール| DB[(既存DynamoDB)]
    Pit[ピットのブラウザ] -->|HTTPS| CF[CloudFront]
    CF -->|画面・JS・CSS / OAC| S3[非公開S3バケット]
    CF -->|GET /api/* / OAC| URL[Lambda Function URL / AWS_IAM]
    URL --> API[取得用Lambda]
    API -->|読み取り| DB
```

- S3に静的コンテンツを置き、CloudFrontからHTTPSで配信する。S3は通常のバケットオリジンを使い、パブリックアクセスをブロックする。
- CloudFrontの`/api/*`をLambda Function URLに振り分ける。画面とAPIを同一オリジンにし、ブラウザは相対URLでAPIを呼ぶ。
- S3とLambdaのオリジンにOAC（Origin Access Control）を使う。Lambda Function URLは`AWS_IAM`認証とし、指定CloudFrontディストリビューションからの呼び出しを許可する。
- ブラウザの利用者にはAWSログインを求めない。CloudFrontからLambdaへの認証と、利用者の閲覧制限は別に扱う。
- API応答は初期実装ではキャッシュしない。静的コンテンツは別のキャッシュ設定で配信する。
- まず数秒間隔のポーリングで更新する。暫定案は3秒間隔で、実際の送信頻度と必要な遅延を確認して決める。

## URL共有の扱い

認証なしのCloudFront URLは、公開エンドポイントである。URLを案内した人はログインせず閲覧でき、転送先の人も閲覧できる。
URLの非告知や検索除外だけでは、閲覧者を制限できない。

共有リンクを持つ人だけにデータを制限する必要がある場合は、推測困難な共有トークンをリンクに付け、API側で検証する方式を検討する。
その場合もリンク所有者へのアクセス許可であり、メンバー個人の認証にはならない。トークンの期限・失効・ログへの記録回避も一緒に設計する。
初期の閲覧方式は、この違いを踏まえて決定する。

## 既存DynamoDBとの接続

既存テーブルのARNを参照し、Lambdaに必要な読み取り権限を与える。最初の段階ではテーブルやIoTルールの所有権をCDKへ移さない。

最新データの取得方法は既存スキーマを確認して決める。

- 車両／走行単位のパーティションキーと時刻順のソートキーがある場合は、逆順の`Query`と`Limit=1`で最新の1件を取得できる。
- 最新状態を固定キーへ上書き保存している場合は、`GetItem`で取得する。
- 最新取得に適したキーがない場合は、GSIや最新状態用の保存先を検討する。その変更は別途設計する。
- テーブル全件の`Scan`を定期実行する方式は、通常の取得経路として採用しない。
- 時刻の型、単位、タイムゾーン、送信時刻と受信時刻の区別を確認する。GSIを使う場合は結果整合性による遅延も考慮する。

ブラウザから任意のテーブルやパーティションを指定できるAPIにはせず、対象の車両／走行と公開項目をサーバー側で制御する。

## 最初の実装範囲

1. テーブルのキー・保存データ例・表示対象を確認し、最新データの取得方法とAPI応答を決める。
2. Python CDKで、S3、CloudFront、Lambda、Function URL、OAC、IAM、CloudWatch Logsを定義する。
3. Lambdaに最新データ取得APIを実装する。データなし、取得失敗、更新停止を区別する。
4. 静的画面に必要な値、計測時刻、データの経過時間、通信状態を表示する。取得失敗時は最後のデータが古いことを示し、再試行する。
5. 最新取得・権限・オリジンの制限をテストし、`cdk synth`と`cdk diff`で構成を検証する。
6. 対象AWS環境へデプロイして実データで確認する。既存の受信・可視化構成を変更する必要がある場合は、差分を明示して扱う。

初回の受け入れ条件は、共有URLからログイン操作なしで画面を開けること、delta-vegaの新しいデータが所定の間隔で表示されること、更新が止まったときに古いデータだと判断できること。
更新間隔・停止判定の閾値・具体的な表示項目は未確定。

## 実装前に確認する情報

- AWSアカウント、リージョン、利用する認証プロファイル。
- 既存DynamoDBのテーブル名またはARN、パーティションキー／ソートキーの名前と型、必要に応じてGSI。
- 保存済みデータ1件の例と、対象車両／走行を識別する値。
- ピットで見たい項目、送信周期、必要な更新間隔、想定閲覧人数。
- URLを非告知で運用するか、共有トークンによるAPIの閲覧制限も必要か。

CDKはPythonで記述するが、CDK CLIの実行にはNode.jsも必要になる。依存関係と実行手順はCDK実装時に固定・記録する。

## AWS公式資料

- [CloudFrontからLambda Function URLへのアクセス制限](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-lambda.html)
- [CloudFrontからS3へのアクセス制限](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)
- [AWS CDKのCloudFrontオリジンとOAC](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cloudfront_origins-readme.html)
- [DynamoDB Queryのソート順と取得上限](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Query.html)
- [DynamoDB Scanの動作](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Scan.html)
