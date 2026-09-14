# テーマからIPC候補への対応

`themes_2026_06.json` は [特許庁の全テーマコード表](https://www.jpo.go.jp/system/patent/gaiyo/bunrui/fi/themecode.html)（2026年6月版）から抽出した3,390件です。解析停止テーマも含みます。元のExcel、取得日時、SHA-256、テーマ名、FIカバー範囲、改正情報、解析期間を保存しています。`build_themes.py` で同梱Excelから再生成できます。

Fタームの最初の5文字をテーマコードとして公式表を参照し、FIカバー範囲を含む上位IPCを探索候補にします。複数メイングループにまたがる範囲はサブクラスへ広げます。範囲の端だけを取り出したり、Fタームの数字をIPCへ直接置き換えたりしません。IPC候補のコードはWIPO IPC 2026.01の同梱辞書で確認します。対応表はテーマの存在とFI範囲を確認するもので、個別Fタームの存在・意味を確認するものではありません。個別Fターム辞書は別ファイルの5H029のみです。

FI欄のIPC部分からも候補を生成しますが、FIには旧版IPCに準拠する部分があり、現行IPCとの同一性や公報への付与は保証できません。[特許庁のFI改正情報](https://www.jpo.go.jp/system/patent/gaiyo/bunrui/fi/f_i_kaisei.html)を根拠として表示します。現行辞書にないFI基本コードは、存在確認できる同じサブクラス接頭部の上位候補だけに広げ、正式な新旧対応とは扱いません。

FIの展開記号（例 `,650`）と識別記号（例 `@Z`）は原文とともに保持します。コロン付きFI索引記号は保存するだけでIPC候補に直接変換しません。FIでコロンを使うことは[特許庁のIPC説明資料](https://www.jpo.go.jp/news/shinchaku/event/seminer/document/2025_chizai-setsumeikai_jitumu/04.pdf)に説明があります。

候補には由来、元コード、対応表の版、IPC辞書の版、注意点を添付します。`patent_metadata`（取り込んだ分類欄）、`derived_candidate`（FI・テーマからのIPC候補）、`unverified_metadata`（辞書未確認）を区別し、公報の `ipc` フィールドを上書きしません。由来が混在する同じ候補では、根拠公報・件数を別に表示します。

CSVのFIだけを補完する `analysis_engine.backfill_fi` は公報ID・名称・要約・既存content_keyを照合し、FIが空欄の場合だけ新しいコピーへ追加します。既存の手動判定、LLM判定、スコア、IPC欄を変更しません。同じIDが複数ある場合や既存FIとの相違は衝突として報告します。
