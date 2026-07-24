/**
 * Lightweight i18n module. Japanese is the default locale; additional locales
 * can be added by providing another dictionary with the same keys.
 *
 * All UI strings MUST go through t() — no hardcoded strings in components.
 */

export const ja = {
  // App chrome
  "app.title": "Deep Research Orchestrator",
  "app.nav.console": "リサーチコンソール",
  "app.nav.settings": "設定",

  // Common
  "common.unknown": "不明",
  "common.unknownReason": "不明（{reason}）",
  "common.notMeasured": "計測対象外",
  "common.loading": "読み込み中…",
  "common.reload": "再読み込み",
  "common.refresh": "更新",
  "common.save": "保存",
  "common.cancel": "キャンセル",
  "common.close": "閉じる",
  "common.delete": "削除",
  "common.edit": "編集",
  "common.create": "新規作成",
  "common.confirm": "実行する",
  "common.back": "戻る",
  "common.none": "なし",
  "common.notSet": "未設定",
  "common.set": "設定済み",
  "common.enabled": "有効",
  "common.disabled": "無効",
  "common.error": "エラー",
  "common.errorPrefix": "エラー: {message}",
  "common.empty": "データがありません",
  "common.yes": "はい",
  "common.no": "いいえ",
  "common.estimateTag": "推定",
  "common.measuredTag": "実測",
  "common.selfHostedZeroCost": "¥0（セルフホスト）",
  "common.seconds": "秒",
  "common.openLink": "リンクを開く",
  "common.download": "ダウンロード",
  "common.warnings": "警告",
  "common.saved": "保存しました",
  "common.deleted": "削除しました",
  "common.required": "必須",

  // Job / run statuses
  "status.queued": "待機中",
  "status.starting": "起動中",
  "status.running": "実行中",
  "status.researching": "調査中",
  "status.normalizing": "正規化中",
  "status.succeeded": "成功",
  "status.failed": "失敗",
  "status.timed_out": "タイムアウト",
  "status.cancelled": "キャンセル済み",
  "status.partial": "一部成功",
  "status.pending": "待機中",
  "status.unavailable": "利用不可",
  "status.skipped": "スキップ",

  // Engine taglines
  "engine.tagline.gpt-researcher": "自律探索型ディープリサーチ",
  "engine.tagline.open-deep-research": "ノードグラフ型オープンリサーチ",
  "engine.tagline.mock-fast": "高速応答のモックエンジン",
  "engine.tagline.mock-slow": "低速動作のモックエンジン",
  "engine.tagline.mock-fail": "失敗系テスト用モック",
  "engine.tagline.mock-partial": "部分成功テスト用モック",
  "engine.tagline.mock-timeout": "タイムアウト検証用モック",
  "engine.tagline.mock-cancellable": "キャンセル検証用モック",
  "engine.tagline.unknown": "リサーチエンジン",

  // Engine availability
  "engine.availability.available": "利用可能",
  "engine.availability.experimental": "実験的",
  "engine.availability.unsupported": "未対応",
  "engine.availability.disabled": "無効化",
  "engine.availability.unhealthy": "異常あり",
  "engine.unhealthy": "ヘルスチェック異常",
  "engine.noEngines": "利用可能なエンジンがありません",

  // Run form
  "form.title": "新規リサーチ",
  "form.topic": "テーマ",
  "form.objective": "目的",
  "form.instructions": "追加指示",
  "form.language": "言語",
  "form.language.ja": "日本語",
  "form.language.en": "英語",
  "form.inputUrls": "入力URL",
  "form.inputUrlsHelp": "1行につき1URL（最大20件）",
  "form.engines": "エンジン",
  "form.maxTimeSeconds": "最大時間（秒）",
  "form.maxSearches": "最大検索数",
  "form.maxCostUsd": "最大コスト（USD)",
  "form.autoSynthesize": "自動統合",
  "form.autoSynthesizeHelp": "全エンジン完了後に統合レポートを自動生成します",
  "form.submit": "リサーチ開始",
  "form.submitting": "送信中…",
  "form.topicRequired": "テーマを入力してください",
  "form.enginesRequired": "エンジンを1つ以上選択してください",
  "form.tooManyUrls": "入力URLは最大20件までです",
  "form.optional": "任意",
  "form.optionalSuffix": "（任意）",

  // Egress preview
  "egress.title": "この実行で通信する外部先",
  "egress.kind": "種別",
  "egress.name": "名称",
  "egress.host": "ホスト",
  "egress.purpose": "用途",
  "egress.empty": "外部への通信はありません",
  "egress.selectEngines": "エンジンを選択すると通信先が表示されます",
  "egress.loadFailed": "通信先一覧の取得に失敗しました",

  // Job list
  "jobs.recentTitle": "最近のジョブ",
  "jobs.topic": "テーマ",
  "jobs.status": "状態",
  "jobs.createdAt": "作成日時",
  "jobs.engines": "エンジン",
  "jobs.open": "開く",
  "jobs.empty": "ジョブはまだありません",
  "jobs.loadFailed": "ジョブ一覧の取得に失敗しました",

  // Job view
  "job.title": "ジョブ",
  "job.statusBanner.partial": "一部のエンジンのみ成功しました",
  "job.statusBanner.cancelRequested": "キャンセルを要求しました",
  "job.cancelJob": "ジョブ全体をキャンセル",
  "job.exportMarkdown": "Markdownエクスポート",
  "job.exportJson": "JSONエクスポート",
  "job.exportFailed": "エクスポートに失敗しました",
  "job.backToList": "コンソールへ戻る",
  "job.loadFailed": "ジョブの取得に失敗しました",
  "job.cancelFailed": "キャンセル要求に失敗しました",
  "job.finishedAt": "完了日時",
  "job.language": "言語",
  "job.objective": "目的",
  "job.instructions": "追加指示",
  "job.error": "エラー",
  "job.connection.connecting": "接続中…",
  "job.connection.open": "ライブ更新中",
  "job.connection.reconnecting": "再接続中…",
  "job.connection.closed": "ストリーム終了",

  // Run card
  "run.stage": "ステージ",
  "run.elapsed": "経過時間",
  "run.searches": "検索数",
  "run.sources": "ソース数",
  "run.tokens": "トークン",
  "run.tokensIn": "入力",
  "run.tokensOut": "出力",
  "run.llmCost": "LLMコスト",
  "run.searchApiCost": "検索APIコスト",
  "run.infraCost": "インフラコスト",
  "run.attempt": "試行",
  "run.cancelRun": "このランをキャンセル",
  "run.cancelRequested": "キャンセル要求済み",
  "run.eventLog": "イベントログ（直近{count}件）",
  "run.noEvents": "イベントはまだありません",
  "run.error": "エラー",

  // Results tabs
  "tabs.overview": "概要",
  "tabs.compare": "比較",
  "tabs.reports": "レポート",
  "tabs.sources": "ソース",
  "tabs.conflicts": "不一致",
  "tabs.synthesis": "統合",
  "tabs.raw": "Raw",
  "tabs.ariaLabel": "結果タブ",

  // Overview tab
  "overview.jobSummary": "ジョブ概要",
  "overview.engineOutcomes": "エンジン別結果",
  "overview.engine": "エンジン",
  "overview.result": "結果",

  // Compare tab
  "compare.agreements": "一致した知見",
  "compare.partialFindings": "部分的に一致した知見",
  "compare.conflicts": "不一致",
  "compare.unsupportedClaims": "根拠が不足している主張",
  "compare.coverage": "カバレッジ",
  "compare.openQuestions": "未解決の論点",
  "compare.enginesColumn": "エンジン",
  "compare.findingColumn": "内容",
  "compare.notReady": "比較結果はまだ利用できません",
  "compare.loadFailed": "比較結果の取得に失敗しました",
  "compare.emptySection": "該当なし",

  // Conflicts tab
  "conflicts.title": "エンジン間の不一致",
  "conflicts.description": "各エンジンの主張と値を並べて表示します",
  "conflicts.engine": "エンジン",
  "conflicts.claim": "主張",
  "conflicts.value": "値",
  "conflicts.empty": "不一致は検出されませんでした",

  // Reports tab
  "reports.summary": "サマリー",
  "reports.noReport": "レポートがありません",
  "reports.loadFailed": "レポートの取得に失敗しました",

  // Sources tab
  "sources.url": "URL",
  "sources.title": "タイトル",
  "sources.engine": "エンジン",
  "sources.fetchedAt": "取得時刻",
  "sources.duplicate": "重複 ×{count}",
  "sources.duplicateHint": "正規化URLが同一のソース",
  "sources.loadFailed": "ソース一覧の取得に失敗しました",
  "sources.empty": "ソースはありません",

  // Synthesis tab
  "synthesis.status": "統合ステータス",
  "synthesis.unavailable": "統合レポートは利用できません",
  "synthesis.failed": "統合レポートの生成に失敗しました",
  "synthesis.errorReason": "理由: {reason}",
  "synthesis.citations": "引用一覧",
  "synthesis.citationDetail": "引用の詳細",
  "synthesis.citationUrl": "URL",
  "synthesis.citationTitle": "タイトル",
  "synthesis.citationFetchedAt": "取得時刻",
  "synthesis.citationExcerpt": "抜粋",
  "synthesis.citationEngines": "使用したランナー",
  "synthesis.retry": "統合を再実行",
  "synthesis.retryProfile": "使用するLLMプロファイル",
  "synthesis.retryProfileDefault": "デフォルト（現在の割り当て）",
  "synthesis.retryRequested": "再実行を要求しました",
  "synthesis.retryFailed": "再実行の要求に失敗しました",
  "synthesis.loadFailed": "統合レポートの取得に失敗しました",
  "synthesis.attempt": "試行回数",
  "synthesis.openCitation": "引用 {sid} の詳細を開く",

  // Raw tab
  "raw.artifacts": "Rawアーティファクト",
  "raw.artifactDownload": "アーティファクトをダウンロード",
  "raw.noArtifact": "アーティファクトなし",
  "raw.normalizedJson": "正規化結果（JSON）",
  "raw.engine": "エンジン",
  "raw.run": "ラン",
  "raw.loadFailed": "結果の取得に失敗しました",

  // Settings page
  "settings.title": "設定",

  // LLM profiles
  "settings.profiles.title": "LLMプロファイル",
  "settings.profiles.name": "名前",
  "settings.profiles.provider": "プロバイダ",
  "settings.profiles.api": "APIタイプ",
  "settings.profiles.endpoint": "エンドポイント",
  "settings.profiles.apiKey": "APIキー",
  "settings.profiles.apiKeyHelp": "書き込み専用。空欄のままにすると変更されません。",
  "settings.profiles.apiKeyNotSet": "未設定",
  "settings.profiles.model": "モデル",
  "settings.profiles.timeout": "タイムアウト（秒）",
  "settings.profiles.maxConcurrency": "最大同時実行数",
  "settings.profiles.enabled": "有効",
  "settings.profiles.test": "接続試験",
  "settings.profiles.testTitle": "接続試験の実行",
  "settings.profiles.testBillingWarning":
    "このプロバイダ（{provider}）は有料APIです。接続試験では最小限のリクエストを送信するため、少額の実課金が発生する可能性があります。",
  "settings.profiles.testLocalNote": "接続試験を実行します。よろしいですか？",
  "settings.profiles.testRunning": "試験中…",
  "settings.profiles.testReachable": "到達性",
  "settings.profiles.testAuthenticated": "認証",
  "settings.profiles.testModelAvailable": "モデル利用可否",
  "settings.profiles.testGenerationOk": "最小生成",
  "settings.profiles.testOk": "OK",
  "settings.profiles.testNg": "NG",
  "settings.profiles.testNotRun": "未実施",
  "settings.profiles.testBillingNote": "課金に関する注記",
  "settings.profiles.testFailed": "接続試験に失敗しました",
  "settings.profiles.deleteConfirm": "プロファイル「{name}」を削除しますか？",
  "settings.profiles.empty": "プロファイルはまだありません",
  "settings.profiles.loadFailed": "プロファイル一覧の取得に失敗しました",
  "settings.profiles.saveFailed": "プロファイルの保存に失敗しました",
  "settings.profiles.editTitle": "プロファイルの編集",
  "settings.profiles.createTitle": "プロファイルの新規作成",
  "settings.profiles.provider.local": "ローカル",
  "settings.profiles.provider.openai": "OpenAI",
  "settings.profiles.provider.anthropic": "Anthropic",

  // Roles
  "settings.roles.title": "ロール割り当て",
  "settings.roles.help": "各処理ロールに使用するLLMプロファイルを割り当てます",
  "settings.roles.research": "リサーチ (research)",
  "settings.roles.summarization": "要約 (summarization)",
  "settings.roles.normalization": "正規化 (normalization)",
  "settings.roles.synthesis": "統合 (synthesis)",
  "settings.roles.unassigned": "未割り当て",
  "settings.roles.loadFailed": "ロール割り当ての取得に失敗しました",
  "settings.roles.saveFailed": "ロール割り当ての保存に失敗しました",

  // Proxy
  "settings.proxy.title": "プロキシ設定",
  "settings.proxy.scopeGlobal": "スコープ: global",
  "settings.proxy.mode": "モード",
  "settings.proxy.mode.off": "オフ",
  "settings.proxy.mode.inherit": "環境変数を継承",
  "settings.proxy.mode.explicit": "明示的に指定",
  "settings.proxy.httpProxy": "HTTPプロキシ",
  "settings.proxy.httpsProxy": "HTTPSプロキシ",
  "settings.proxy.allProxy": "ALLプロキシ",
  "settings.proxy.writeOnlyHelp": "書き込み専用。空欄のままにすると変更されません。",
  "settings.proxy.noProxy": "NO_PROXYリスト",
  "settings.proxy.noProxyHelp": "1行につき1エントリ",
  "settings.proxy.caBundlePath": "CAバンドルパス",
  "settings.proxy.test": "プロキシ試験",
  "settings.proxy.testRunning": "試験中…",
  "settings.proxy.testExternal": "外部URL",
  "settings.proxy.testInternal": "内部URL",
  "settings.proxy.testViaProxy": "プロキシ経由",
  "settings.proxy.testBypassed": "バイパス",
  "settings.proxy.testNotViaProxy": "プロキシ非経由",
  "settings.proxy.testNotBypassed": "バイパスされていません",
  "settings.proxy.testRawResult": "試験結果の詳細（JSON）",
  "settings.proxy.testFailed": "プロキシ試験に失敗しました",
  "settings.proxy.loadFailed": "プロキシ設定の取得に失敗しました",
  "settings.proxy.saveFailed": "プロキシ設定の保存に失敗しました",

  // Search settings
  "settings.search.title": "検索設定",
  "settings.search.note":
    "検索設定はサーバー側で管理されています（読み取り専用）。変更はサーバー設定ファイルで行ってください。",
  "settings.search.loadFailed": "検索設定の取得に失敗しました",

  // Allowlist
  "settings.allowlist.title": "LLMエンドポイント許可リスト",
  "settings.allowlist.empty": "許可リストのエントリはありません",
  "settings.allowlist.deleteConfirm": "このエントリを削除しますか？",
  "settings.allowlist.loadFailed": "許可リストの取得に失敗しました",
  "settings.allowlist.deleteFailed": "エントリの削除に失敗しました",
} as const;

export type MessageKey = keyof typeof ja;

type Dictionary = Record<MessageKey, string>;

const dictionaries: Record<string, Dictionary> = { ja };

let currentLocale = "ja";

/** Switch UI locale (dictionaries must be registered first). */
export function setLocale(locale: string): void {
  if (dictionaries[locale]) currentLocale = locale;
}

export function getLocale(): string {
  return currentLocale;
}

/**
 * Translate a message key, with optional {param} interpolation.
 * Unknown params are left as-is; missing keys fall back to the key itself.
 */
export function t(
  key: MessageKey,
  params?: Record<string, string | number>,
): string {
  const dict = dictionaries[currentLocale] ?? ja;
  let msg: string = dict[key] ?? ja[key] ?? key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      msg = msg.split(`{${name}}`).join(String(value));
    }
  }
  return msg;
}
