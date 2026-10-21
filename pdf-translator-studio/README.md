# PDF Lingua

PDF のページ画像を残し、抽出した文字領域に訳文を重ねる JavaScript / HTML アプリです。ブラウザーで PDF を読み込み、元の配置を参照しながら訳文を編集して PDF・Word (.docx)・PowerPoint (.pptx) に書き出せます。翻訳先は社内の OpenAI 互換 LLM または OpenAI API を選べます。

文字数・フォント・言語の組版規則が変わるため、外見の完全一致は保証できません。特に長い訳文、複雑な背景、表、縦書きには制約があります。

## 起動

Node.js **24.5 以上**と、現行の Chrome / Edge などを使用してください。ブラウザー用ライブラリーは `dist/vendor/` に同梱しています。アプリの起動に npm install や外部 CDN は必要ありません。

APIキーやLLMの接続URLを設定する場合は、このフォルダーで設定ファイルを作成します。既存の `.env` がある場合は上書きせず編集してください。プロキシ・APIの応答待ち時間・ローカルLLMへ一度に送る量は、起動後の「接続設定」画面から保存できます。

```powershell
Copy-Item .env.example .env
```

次のいずれかで起動します。既定のローカル接続先を使う場合、`.env` は不要です。

```powershell
node launch.mjs
```

```powershell
powershell -File .\start.ps1
```

`start.ps1` は PATH の Node.js を使用し、見つからない場合はこの環境の Codex 同梱ランタイムを探します。スクリプト実行が組織のポリシーで禁止されている場合は、上の `node` コマンドを使用してください。

ブラウザーで [http://127.0.0.1:4173](http://127.0.0.1:4173) を開きます。終了はターミナルで `Ctrl+C`。ポート番号は `.env` の `PORT` で変更できます。`.env` の変更後はサーバーを再起動してください。画面から保存した接続・翻訳設定は再起動不要です。

アプリの使用中はサーバーを起動したままにしてください。ブラウザーの画面が残っていても、サーバーが停止すると PDF ライブラリーや翻訳機能を読み込めません。`Failed to fetch dynamically imported module` と表示された場合はサーバーを起動し直し、PDF をもう一度開いてください。古い版の画面でエラーが続く場合は、編集データを保存できる状態なら先に保存し、ページを再読み込みしてください。

`launch.mjs` は `.env` を読み込んだ後、設定を継承した Node.js 子プロセスでサーバーを起動します。社内 CA も子プロセスの起動時に反映します。HTML ファイルをダブルクリックする `file://` 起動は使用できません。

## 社内・ローカル LLM

既に稼働している OpenAI 互換 API のベース URL と、そこで利用可能なモデル名を設定します。例のホスト名・モデル名は実際の環境に置き換えてください。

```dotenv
LOCAL_LLM_BASE_URL=http://llm.internal:8000/v1
LOCAL_LLM_MODEL=your-intranet-model
LOCAL_LLM_API_KEY=
LOCAL_LLM_USE_PROXY=false
```

同じ PC 上の Ollama の OpenAI 互換エンドポイントを使う場合のベース URL の例は `http://127.0.0.1:11434/v1` です。モデルは別途用意し、利用可能なモデル名を指定してください。このアプリは LLM サーバーのインストールやモデルのダウンロードを行いません。

ローカル・社内接続は既定でプロキシを通しません。社内 LLM にもプロキシが必要な環境では、画面のプロキシ設定でローカルLLMにも使う設定を有効にします。従来の環境変数方式では `LOCAL_LLM_USE_PROXY=true` にします。選択したモデルには指定された JSON 形式で翻訳を返す能力が必要です。

ローカルLLMには既定で4領域・1000文字以内にまとめて送信します（長い単一領域は分割しません）。「接続設定」で領域数は1〜24、文字数は100〜6000の整数に変更できます。`LOCAL_LLM_JSON_MODE=auto` では、JSON Schemaで返却件数と使用可能なIDを指定します。接続先がこの形式を明示的に拒否した場合だけ、プロンプトによるJSON指定へ切り替えます。必要に応じて `json_schema`、`json_object`、`prompt` を明示できます。[LM Studioの構造化出力](https://lmstudio.ai/docs/developer/openai-compat/structured-output)に対応しています。

ローカルモデルが領域をまとめたりIDを欠落させたりした場合、元の位置との対応を推測せず、小分けにして再翻訳します（最大3段階）。各バッチ内の全件の対応を検証してから、そのバッチの訳文を画面へ反映します。処理中は領域数・バッチ数・経過秒を表示し、失敗理由は画面下部にも残ります。途中で失敗・中止しても、それ以前に検証済みのバッチは保持します。認証失敗・通信切断・タイムアウトによる小分け再送は行いません。OpenAIの既定バッチは24領域・6000文字で、このローカル専用の小分け再翻訳は行いません。

APIの応答待ち時間は、ローカルLLMが既定600秒、OpenAIが既定180秒です。「接続設定」で接続先ごとに30〜1800秒の整数を保存できます。HTTP 504が出た場合は、LLMサーバーの稼働状況を確認し、応答待ち時間を延ばすか、ローカルLLMへ送る領域数・文字数を減らしてください。504になった要求は自動で再送しません。タイムアウト後も接続先で推論が続いている場合があるため、再開前に接続先の状態を確認してください。

## OpenAI と社内プロキシ

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
OPENAI_API_MODE=auto
```

プロキシは、画面右上の「接続設定」で入力して保存できます。

1. アプリに保存する方式を選び、プロキシURL（例: `http://proxy.company.example:8080`）を入力します。同じURLをHTTP・HTTPS接続に使います。
2. 必要ならBasic認証のユーザー名・パスワードと、プロキシを使わない接続先（NO_PROXY）を入力します。認証情報はURLに含めず専用欄に入力してください。
3. 社内・ローカルLLMにも使う場合だけ、そのチェックを入れます。
4. 「プロキシ設定を保存」を押します。次の翻訳から適用され、アプリを再起動しても保持されます。翻訳・出力などの処理中は保存できません。

保存済みパスワードは画面へ返しません。パスワード欄を空のまま保存すると既存値を維持し、削除する場合は削除用チェックを使います。「直接接続」を保存すると、環境変数にプロキシがあっても使いません。「.env・環境変数」を選ぶと従来の設定を使用します。初期状態は従来方式なので、既存の接続設定は自動変更しません。

従来方式では `HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY`、`PROXY_USERNAME`、`PROXY_PASSWORD` を使用します。大文字と小文字の両方がある場合は小文字の設定を優先します。NTLM、Kerberos、PAC の解釈は実装していません。それらが必須の場合は、組織が提供する認証ブリッジなどが必要です。

社内 TLS 検査用 CA が必要なら、組織が配布する PEM ファイルを指定して、起動コマンドから再起動します。

```dotenv
NODE_EXTRA_CA_CERTS=C:/certificates/company-ca.pem
```

追加 CA は Node.js の起動時に読み込まれるため、通常は `node launch.mjs` を使用してください。`node server.mjs` を直接実行する場合は、`.env` だけに頼らず PowerShell で先に環境変数を設定します。

```powershell
$env:NODE_EXTRA_CA_CERTS = 'C:/certificates/company-ca.pem'
node server.mjs
```

`--env-file` だけでは、この環境の Node.js 24.19 で追加 CA が反映されないことを確認しています。[Node.js の追加 CA の仕様](https://nodejs.org/api/cli.html#node_extra_ca_certsfile)では、実行中に環境変数を変更しても現在のプロセスの信頼ストアは変わりません。

TLS の証明書検証は有効のままです。プロキシ通信には Node.js のネイティブ `Agent({ proxyEnv })` を使います。設定形式は [Node.js の公式プロキシ仕様](https://nodejs.org/docs/latest-v24.x/api/http.html#built-in-proxy-support)を参照してください。

## GPT-4o と GPT-5 系のリクエスト

このアプリの `auto` 選択は次のとおりです。モデルの利用可否は、接続先と API アカウントによって異なります。

| 接続先・モデル名 | 既定の API | 主な送信形式 |
| --- | --- | --- |
| 社内・ローカル LLM | Chat Completions | `messages`、`max_tokens` |
| OpenAI GPT-4 系（GPT-4o を含む） | Chat Completions | `messages`、`max_completion_tokens`、JSON 出力指定 |
| OpenAI GPT-5 以降・o 系 | Responses | `instructions`、`input`、`max_output_tokens`、JSON Schema |

画面または `OPENAI_API_MODE` で `chat` / `responses` を明示できます。`auto` はモデル名による判定です。独自のモデル名・ゲートウェイでは API 形式を明示してください。GPT-5 系には `temperature` を送信せず、推論設定は指定された場合にだけ送ります。利用可能な推論設定はモデルごとに異なります。

`OPENAI_MAX_OUTPUT_TOKENS`、`LOCAL_LLM_MAX_TOKENS` は `.env.example` を参照してください。従来の `REQUEST_TIMEOUT_MS` も、アプリに翻訳設定が未保存の場合の既定値として使えます（1000〜1800000ミリ秒。画面では秒へ丸めて30〜1800秒に収めます）。画面から保存した待ち時間を優先します。タイムアウトはAPIへの各リクエスト単位で適用されるため、小分け再翻訳を含む処理全体の時間はこの値を超える場合があります。本文が途中で切れた結果や ID が一致しない結果は、誤った位置へ置かずエラーとして扱います。

実装の参照先は OpenAI の [Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Responses API 移行ガイド](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[推論モデルの仕様](https://developers.openai.com/api/docs/guides/reasoning)です。4o が Responses を使えないという意味ではなく、このアプリの既定値を分けています。

## 操作とデータの流れ

1. PDF を読み込み、原文と文字領域を確認します。
2. 翻訳先・モデル・言語を選び、対象のページや領域を翻訳します。
3. 訳文と領域の見え方を確認し、必要に応じて編集します。
4. 「全ページの出力形式」で PDF・Word・PowerPoint を選び、保存します。

「サンプルを試す」は組み込みの見本と日本語訳を開き、API に通信しません。編集データは JSON として保存・再読込できます。復元時は同じ元 PDF が必要です。PDF は最大 30 MB です。ページ数の固定上限はありませんが、1 ページの寸法には上限があります。翻訳が枠に収まらない場合は、どの形式でも書き出しを止めて修正を促します。

長い文書は最初にページ数・寸法を確認し、表示するページの文字と画像だけを読み込みます。「ページへ移動」で任意のページ番号を指定できます。別のページに移動しても訳文・文字枠の編集は保持されます。「すべてのページ」の翻訳と全形式の出力は、まだ表示していないページも順番に読み込み、処理済みのページ画像を解放します。出力ファイル全体と抽出した文字はメモリを使用するため、処理できる文書の規模・所要時間は端末と内容に依存します。

編集データには読み込み済みのページを保存します。未読込ページは省略されますが、元PDFを開いて復元すれば引き続き読み込めます。従来の全ページ形式の編集データも読み込めます。編集データの読込上限は100MBです。

翻訳が途中で止まった場合は、検証済みバッチの訳文を残して未完了の領域だけ再開できます。完了した領域は再送しません。途中結果と再開情報は現在の画面のメモリに保持され、文書本文の自動保存は行いません。再読み込みや画面を閉じる前には「編集データを保存」を使ってください。同じ元PDFと保存した編集データから訳文を復元できますが、処理中ジョブそのものは引き継ぎません。

## 出力形式とレイアウト

外見を保つため、3形式とも翻訳済みページを PNG 画像として固定配置します。Word や PowerPoint で文字が再配置されたり、別のフォントに置き換わったりしない方式です。文章・表・図形は画像の一部となり、Office 上で個別に編集できません。文字修正はこのアプリで行い、もう一度書き出してください。元 PDF の検索テキスト・リンク・フォーム・電子署名は継承しません。

| 形式 | ページ・スライドの配置 |
| --- | --- |
| PDF | 元のページごとの寸法・縦横向きを保持します。 |
| Word (.docx) | 各ページを別セクションとして、元の用紙寸法・縦横向きで画像をページ原点に配置します。Word の寸法単位による丸めは最大 0.025 pt です。このアプリの対応範囲は各辺 0.25～22 インチです。 |
| PowerPoint (.pptx) | 元の1ページを1スライドにします。全スライドの幅は元ページの最大幅、高さは最大高さとし、各画像を原寸のまま中央に置きます。サイズ混在時は余白が生まれますが、引き伸ばし・切り抜きは行いません。スライドの各辺が 1～56 インチの範囲で出力できます。 |

Office の範囲外の用紙寸法は明示的なエラーにして、意図しない縮小を避けます。画像の基本解像度は 162 dpi（PDFポイントの2.25倍）です。大きいページはメモリー上限に合わせて解像度を制限するため、拡大時の鮮明さには限界があります。3形式の画像生成はブラウザー内で完結し、Office ソフトや追加サーバーへの送信は不要です。

PowerPoint の共通スライド寸法とサイズ範囲は [Microsoft の仕様](https://support.microsoft.com/en-us/powerpoint/change-the-size-of-your-powerpoint-slides)、Word の寸法上限は [Microsoft の制限一覧](https://learn.microsoft.com/en-us/previous-versions/troubleshoot/microsoft-365/microsoft-365-apps/word/operating-parameter-limitation)を参照しています。

PDF の解析・元ページの描画・書き出しはブラウザーで行います。翻訳時には抽出したテキストをローカル Node.js サーバーへ送り、そこから選択した LLM / OpenAI API に送ります。元の PDF ファイル全体は API に送信しません。読み込みと手動編集には翻訳 API への接続は不要です。

APIキーはサーバー側の `.env` または環境変数に設定します。画面から保存するプロキシ設定はアプリフォルダー内の `data/proxy-settings.json`、応答待ち時間とローカルの送信量は `data/translation-settings.json` に保存します。初回保存まで設定ファイルを作成しません。いずれも静的配信フォルダーの外にあり、Git・配布ZIPの対象外です。保存済みのAPIキーやプロキシパスワードをブラウザーへ返すAPIはありません。設定ファイルは平文で、暗号化していません。認証情報を含むため `.env` と `data/` を共有しないでください。アプリを別フォルダーへ移す場合、保存設定を引き継ぐには `data/` も移します。

ブラウザーは同じローカルサーバーの `/api/` を呼び出すため、翻訳先でのブラウザー CORS 設定は不要です。サーバーは `127.0.0.1` のみに待ち受け、外部の Host / Origin を拒否します。この構成は単一 PC 用です。静的サイトとして `dist/` だけを公開しても翻訳 API は動作しません。

## 外見保持の範囲

- 元ページの画像・図形・余白を背景に使い、元の文字の近くへ訳文を配置します。訳文の長さに応じてフォントサイズを調整します。
- 文字領域を塗りつぶす方式のため、写真・グラデーション・罫線に重なった文字は背景も消える場合があります。プレビューで確認し、領域や背景色を補正してください。
- 元のフォントや文字幅、改行を完全には再現できません。小さくしすぎた文字や重なりは確認が必要です。
- スキャン画像だけの PDF に対する OCR と、縦書きの翻訳レイアウトは未対応です。
- 3形式ともページを画像化します。元の検索可能なテキスト、リンク、フォーム、電子署名、アクセシビリティ構造は引き継ぎません。
- 大きな PDF はブラウザーのメモリーを多く使います。ページ数・解像度を抑えて確認してください。

## 開発・テスト

```powershell
node --test tests/*.test.mjs
```

`launch.mjs` が設定を読み込んでサーバーを起動し、`server.mjs` が静的ファイルとアプリ API、`providers.mjs` がモデルごとの送信形式、`proxy-fetch.mjs` が外向き通信を担当します。ブラウザー側の実装は `dist/` にあります。テストはローカルの模擬サービスを用い、実際の OpenAI の課金 API や社内認証情報は使用しません。実際の社内ネットワーク・モデルとの接続確認は、それぞれの環境で行ってください。

ブラウザー検証用の `tests/browser-engine.mjs` と `tests/browser-app.mjs` は、別途 Playwright と Edge を使います。必要に応じて `PLAYWRIGHT_MODULE` に Playwright の `index.mjs` の絶対パス、`BROWSER_CHANNEL` にブラウザーの種類を設定してください。これらはアプリ利用には不要です。同梱ライブラリーのバージョン・ライセンスは `THIRD_PARTY.md` に記載しています。

3形式の繰り返し出力は次で検証できます。`tests/browser-exports.mjs` は検証用サーバーを自分で起動・終了します。Playwright に加え、ZIP の内容検査に使う Node.js 用 JSZip が必要です。`JSZIP_MODULE` で `jszip/lib/index.js` の絶対パスを指定できます。

```powershell
node tests/browser-exports.mjs
```

長い文書の画面操作は `node tests/browser-long-pdf.mjs` で検証できます。41ページの検証文書を作成し、ページ移動・編集保持・全ページ翻訳・3形式の出力を確認します。翻訳は模擬サービスを使います。実資料を追加する場合は `LONG_PDF_SOURCE` に元PDFの絶対パスを指定してください。Playwright、JSZipに加え、出力PDFを独立したプロセスで検査するPythonと`pypdf`が必要です。`PYTHON_BINARY`でPythonの実行ファイルを指定できます。検証用サーバーは別のポートで動作します。

Word / PowerPoint がインストール済みの Windows では、生成ファイルを読み取り専用で実際に開き、Office が出力した PDF を独立した PDFium レンダラーで比較できます。Python の `pypdfium2`、`Pillow`、`numpy` を使用します。

```powershell
python tests/office-roundtrip.py --fixtures tmp/office-qa/fixtures --output tmp/office-qa/roundtrip
```

検証用の Office 自動操作には PowerShell 7 (`pwsh`) を優先します。必要なら `--powershell-executable "C:/path/to/pwsh.exe"` で明示できます。この検証環境では Windows PowerShell 5 経由の連続処理が停止したため、PowerShell 7 で実行しました。

文書構造の追加検査には `tests/validate-office-schema.ps1` を使えます。別途用意した Microsoft Open XML SDK 2.20.0 の net46 DLL を `-SDKDLLPath`、検証対象フォルダーを `-FixtureRoot` に指定します。SDK はアプリの依存ライブラリーではありません。

検証内容・実行結果・精度の範囲は [VALIDATION.md](VALIDATION.md) を参照してください。Office および Python はこの追加検証のためだけに必要で、アプリでの3形式の出力には不要です。
