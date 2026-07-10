# FermiScope アーキテクチャ

## 全体像

```
ブラウザ (HTML / CSS / Vanilla JS ─ 自作SVGチャート・自作数式レンダラ)
   │  fetch / Server-Sent Events
   ▼
FastAPI (api/) ── RunManager(asyncioタスク+イベントキュー)
   │
   ▼
ResearchOrchestrator (research/orchestrator.py)
   │ 1 問い正規化      question/parser.py        (ルール優先・LLM補助)
   │ 2 モデル生成      models/generator.py       (類型テンプレート+採点)
   │ 3 式グラフ        formula/                  (安全AST・Pint単位検査)
   │ 4 検索計画        research/planner.py       (目的別クエリ・日英)
   │ 5 検索実行        research/search/          (Mock / Brave、上限・キャッシュ)
   │ 6 文書取得        research/fetcher.py       (SSRFガード・robots・多形式)
   │ 7 値抽出          evidence/extractor.py     (構造化→パターン→LLM検証付き)
   │ 8 採点/クラスタ   evidence/ranker.py, clustering.py
   │ 9 矛盾検出        evidence/contradiction.py
   │10 証拠統合        estimation/fusion.py      (重み付き分位点)
   │11 敵対的検証      adversarial/verifier.py   (決定論チェック+反証検索)
   │12 再分解          decomposition/engine.py   (ルール優先・LLM検証付き)
   │13 シミュレーション estimation/engine.py      (MC・シナリオ・シード保存)
   │14 感度分析        sensitivity/engine.py     (OAT・弾力性・Spearman)
   │15 検算            validation/engine.py      (3倍警告・区間重なり)
   │16 レポート        reporting/                (JSON/CSV/HTML/Markdown)
   ▼
persistence/repository.py (SQLite: 状態JSON + 監査/証拠/検索の正規化テーブル)
```

## 設計原則(要件 §2 対応)

1. **数値はPythonが計算する** — LLMは `llm/` の7つの補助メソッドのみ。
   数式評価は `formula/parser.py`(ASTホワイトリスト、eval不使用)、
   確率計算は NumPy/SciPy。
2. **証拠は必ず原典に紐づく** — `EvidenceItem` は URL・取得日・根拠抜粋・
   content_hash を必須で持つ。LLM抽出は `validate_llm_extraction` が
   「抜粋が原文に実在するか」を検査してから保存する。
3. **由来のない値は存在しない** — `ParameterEstimate.value_basis` は
   evidence / user_input / assumption / derived / unresolved の5値。
   unresolved は計算に使えず、GUIで入力を促す。
4. **単位は機械検査** — Pint に person / household / piano 等のエンティティ
   次元を定義し、式全体の次元を目標単位と照合する。
5. **外部Webは不信データ** — `security/` にURLガード(SSRF)、HTML
   サニタイズ、LLMデータ境界(`wrap_untrusted`)。命令文は実行されない
   ことを `tests/adversarial/` が担保する。

## 交換可能な境界(インターフェース)

| 境界 | 抽象 | 実装 |
|------|------|------|
| Web検索 | `research/search/base.py: SearchProvider` | `MockSearchProvider`(フィクスチャ)、`BraveSearchProvider`(実API) |
| 生成AI | `llm/base.py: LLMProvider` | `NoOpLLMProvider`、`MockLLMProvider`、`OpenAICompatProvider` |
| 証拠評価 | `evidence/ranker.py`(重み・クラス基準は `config/*.yaml` で全て外出し) | ルールベース実装 |
| 文書取得 | `DocumentFetcher(transport=...)` | httpx実トランスポート / モックトランスポート。JS描画ページ対応はtransport差し替えで拡張 |
| 永続化 | `ProjectRepository(database_url)` | SQLite(既定)/ PostgreSQL(URL変更のみ) |

## データフローの要点

- **転載排除**: `cluster_evidence` が content_hash・引用関係・タイトル類似で
  クラスタ化し、統合時はクラスタ代表1件のみが票を持つ。
- **矛盾の非隠蔽**: 互換な証拠間で比が閾値(既定2倍)を超えると
  `Contradiction` を生成し、定義/時点/地域/方法の差を分析して表示する。
  定義非互換は片方を統合から除外する(平均しない)。
- **再分解ループ**: 感度×不確実性×批判重大度 = importance。閾値超のみ
  分解し、分解後は下位パラメータを再調査・再検証してMCをやり直す
  (最大2周・深度/末端数上限つき)。
- **再現性**: シード・反復回数・アプリ版・設定ファイルハッシュ・全検索・
  全取得(content_hash付き)・全値変更が `audit_events` に残る。
  同じシードで同じ結果が得られる(テストで担保)。

## 長時間処理

調査は `RunManager` が asyncio タスクとして起動し、HTTPは即応答する。
進捗は実カウンタ(検索数・取得数・検証数)のみをSSEで配信し、
架空のパーセンテージは存在しない。キャンセルは `cancel_requested`
フラグをステージ境界・パラメータ境界で検査して行う。

## セキュリティ層

- URLガード: スキーム制限、localhost/プライベート/リンクローカル/
  メタデータIP拒否、DNS解決後の全IP検査、リダイレクト毎の再検査
- 取得: サイズ上限(ストリーミング検査)、Content-Type許可リスト、
  タイムアウト、robots.txt 尊重、UA明示
- 表示: サーバ側サニタイズ+フロントは全て textContent 挿入+CSP
- LLM: データ境界トークン(偽装は無害化)、出力はPydantic検証、
  引用実在検査、APIキー非ログ
