# 特許検索式の監査・形式別出力

## 2026-09-12：分類探索の拡張

- IPC 2026.01の全80,145有効分類を公式XML・有効記号一覧から収録。親子関係はXMLの入れ子を使用し、番号の切り詰めで推測しない。
- F-term 5H029の145ターム・9観点・1テーマを公式表から収録。他テーマとCPCの階層は未収録として明示。
- 上位・下位への表示移動と検索への選択を分離。起点1件への選択置換、個別解除、Undo、画面外選択の保持を追加。
- IPC/F-term/CPCの一括直接指定、取込済みターゲット特許からのIPC/F-term取り出し、出典と原文保持、分類だけの検索式に対応。
- タブ移動で入力が消える問題、探索通信中に選択が古い応答で戻る問題、CSV置換後の古いターゲットID、候補上限超過時の部分更新を回帰テストで確認・修正。
- 検証対象：Python 53件（任意Transformer実学習1件を除く52件）、JavaScript 14件。データはテスト用ワークスペースに分離。実ブラウザーでも階層移動、直接指定、ターゲット出典、選択置換/Undo、分類だけの式を確認。

操作と収録範囲は [分類探索ガイド](CLASSIFICATION_GUIDE.md) を参照。

## 2026-09-11：形式別出力の監査

確認日：2026-09-11。対象は検索条件の保持、改善式・判断エージェントの処理、7形式への変換です。網羅的なセキュリティ監査や実際の特許検索結果の評価ではありません。

## 修正した問題

| 問題 | 修正・検証 |
|---|---|
| 判断エージェントが前回の追加・除外語を初期化していた | 同じ探索キーワードの継続時は以前の条件を引き継ぐ。回帰テストでNOTの保持を確認 |
| 空白区切りで英語フレーズが分断され、24語超過が黙って切り捨てられた | 引用符付きフレーズを保存。Boolean式や不正な引用符、上限超過を明示的な入力エラーに変更 |
| 探索条件が変わっても前回の追加・除外語が画面でチェック済みになった | サーバーと画面で同じ文脈判定を使い、現在の条件に有効な語だけを選択済みにする |
| エージェント応答に判定一覧がなくても処理完了になった | 対象IDすべてが一度ずつ含まれることを検証し、不足・重複・応答欠落をエラーにする |
| 長い要約の後半にある必要文書の語句を除外候補にできた | 特徴語抽出用の長さ制限と必要文書の保護用照合を分離し、タイトル・要約全体で確認 |
| 同じ記号のIPCとCPCが同一選択と扱われる可能性 | 分類体系とコードを組み合わせて識別。過去の一意なコード選択は互換処理 |
| 判断エージェントの最終提案で引き継いだ追加語が再度上書きされた | 前半と後半の両方で既存の追加語・除外語を保持。未判定特許を含む最終提案まで回帰テスト |
| LLMが分類選択を返さないと全分類解除と解釈された | `selected` キーを必須にし、欠落時は分類を変更せず停止 |
| 履歴を切り替えた後に古い画面の遅延処理がコピーを無効化した | 現在の検索式IDを確認してから画面状態を更新する。遅延コールの回帰テスト |
| USPTOの演算子・フィールド指定が通常の語句として通過した | `XOR`、距離付き演算子、`battery.TI.` などを拒否し、検索範囲の変質を防ぐ |

検索結果全体の再現率は未評価です。既知の必要文書にないNOT語でも、未発見の関連特許を除外する可能性は残ります。

## 出力の設計

文字列の置き換えで方言を変換せず、保存済みのキーワード、分類、追加語、除外語から論理木を作成します。キーワード群はAND、分類群はOR、追加語群はORをANDで接続、除外語群はORをNOTに接続します。

未対応分類を含む場合は出力を保留し、明示的な省略指定がある場合だけ変換します。IPCをCPCに読み替えません。履歴の出力や語句置換で元の保存済み条件を書き換えません。条件構造を復元できない旧改善式は出力せず再作成を案内します。

以下の構文は一次資料等と照合しました。各DBのログイン後の画面で検索を実行した検証ではありません。UI・保存テキストにもこの区別と参照元を表示します。

| サービス | 主な構文・対象範囲 | 根拠 |
|---|---|---|
| J-PlatPat | `/TX`、`/IP`、`/CP`、`/FT`。ANDは`*`、ORは`+`、NOTは`-`、優先順位は角括弧。英文フレーズは単一引用符。和文・英文対象は検索画面で選択 | [論理式入力ヘルプ](https://www.j-platpat.inpit.go.jp/help/ja/p01/arithmetic.html)、[検索項目一覧](https://www.j-platpat.inpit.go.jp/help/ja/p01/p0101.html#9994) |
| Derwent Patent Search / Innovation | `CTB`はタイトル・要約・請求項。分類は`IC`、`ACP`、`FTC`。末尾セミコロン。2020年資料に基づくため、現在の契約画面のCheck Syntaxで要確認 | [Clarivate分類検索研修](https://clarivate.com/intellectual-property/wp-content/uploads/sites/5/dlm_uploads/200924_DIsearch_basic3_presentation.pdf)、[キーワード検索研修](https://clarivate.com/intellectual-property/wp-content/uploads/sites/5/dlm_uploads/200910_DIsearch_basic1_presentation.pdf) |
| Derwent Innovations Index | `TS`はタイトル・要約、IPCは`IP`。この製品の`CPC`は被引用出願人コードでありCPC分類に使えない。IPCのハイフン・主群3桁化は国立国会図書館の操作案内に基づく | [Clarivateフィールド一覧](https://webofscience.zendesk.com/hc/en-us/articles/25550079343121-Derwent-Advanced-Search-Field-Tags)、[Topic検索](https://images.webofknowledge.com/WOKRS58B4_1/help/DII/hs_topic.html)、[国立国会図書館DII案内](https://ndlsearch.ndl.go.jp/rnavi/stm/post_381) |
| Espacenet | Smart searchの`ftxt`、`ipc`、`cpc`。下位分類は`/low`。Booleanの優先順位を括弧で明示。日本語語句は出力用の言語置換が必要 | [EPO pocket guide](https://link.epo.org/web/technical/espacenet/espacenet-pocket-guide-en.pdf)、[下位分類](https://worldwide.espacenet.com/patent/help/query-syntax-low-operator)、[対応言語](https://worldwide.espacenet.com/patent/help/query-languages) |
| USPTO Patent Public Search | Advanced Search。無指定で全文、分類は`.IPC.`、`.CPC.`。複合語は順序付き`ADJ`。ADJの距離計算はストップワードを無視。分類の階層展開は未実装 | [Advanced Search guide](https://www.uspto.gov/sites/default/files/documents/Advanced-search-overview-QRG-Patent-Public-Search.pdf)、[索引一覧](https://www.uspto.gov/patents/search/patent-public-search/searchable-indexes)、[演算子](https://www.uspto.gov/patents/search/patent-public-search/operators) |
| WIPO PATENTSCOPE | `EN_ALLTXT` / `JA_ALLTXT`、`IC_EX` / `CPC_EX`。下位分類を含める場合は`IC` / `CPC`。NOTは`ANDNOT` | [構文ヘルプ](https://patentscope.wipo.int/search/en/help/querySyntaxHelp.jsf)、[フィールド一覧](https://patentscope.wipo.int/search/en/help/fieldsHelp.jsf)、[日本語ガイド](https://patentscope.wipo.int/search/help/ja/users_guide.pdf) |
| Google Patents | Advanced SearchのSearch TermsでBoolean式を使用。確認した分類構文は`CPC=`と`/low`。この実装ではIPC・F-termは未対応として扱う | [Google公式検索ヘルプ](https://support.google.com/faqs/answer/7049475?hl=en-AU) |

## 検証

- Python：33件中32件成功、任意のTransformer実学習テスト1件は今回スキップ。
- JavaScript：囲み選択4件と履歴切り替え時の遅延出力2件、計6件成功。
- 7形式の代表式、AND/OR/NOTの保持、IPC/CPC分離、語句置換、未対応分類の明示的省略、下位分類、未確認分類、履歴の非破壊出力を回帰テスト。
- 判断エージェントは模擬LLM応答で検証。外部LLM・実サービスの検索成否を示すものではありません。
- ローカルの実ブラウザーで全7形式の選択、未対応分類の保留・省略、日英語句置換、下位分類指定、履歴表示、コピー内容を確認。保存ボタン操作時を含めブラウザーエラーなし。640px・1280px幅で横方向のはみ出しなし。

## 残る制限

- 検索DBの収録・言語・分類付与・語形処理が異なり、同一の論理構造でも同じヒット集合にはなりません。
- 任意のBoolean式のインポート、ワイルドカード、近接演算子の自由編集、同義語の概念ブロック編集は未実装です。扱えない記号付き語句は無断で変更せず入力エラーにします。
- 自動翻訳は行いません。出力用の翻訳は利用者が入力し、その同等性を確認する必要があります。
- Google PatentsのIPC、DIIのCPC・F-term、Espacenet/USPTO/PATENTSCOPEのF-termは、この実装では出力しません。
- Derwent等の契約サービスの現行構文確認と、検索サイトへの自動投入・CSV自動取得は未接続です。
