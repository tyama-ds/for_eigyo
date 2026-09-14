/* Discovery is a reviewable workspace: importing evidence never selects a class. */
let discoveryDraft = null, discoveryRequestPending = false, discoveryVisibleCount = 24;
const discoveryPicked = new Set();
let discoveryResultsSignature = '', discoveryHistorySignature = '';
let discoveryPlanRequestState = null;
let discoveryFocusKey = null, discoveryLassoMode = false, discoveryMapCache = null, discoveryMapObserver = null, discoveryAdopting = false;
const discoveryCompare = {mode: 'map', facet: 'all', source: 'all', search: '', selectedOnly: false, language: 'ja', zoom: 'normal'};

function discoveryKey(item) { return item.key || `${item.kind}:${item.code}`; }
function discoveryItems(discovery = state.discovery || {}) {
 const recommendations = discovery.analysis?.recommendations || [];
 const known = new Set(recommendations.map(discoveryKey));
 return [...recommendations, ...(discovery.plan?.candidate_hypotheses || []).filter(item => !known.has(discoveryKey(item)))];
}
function captureDiscoveryDraft() {
 if (!$('#discovery-keywords')) return;
 discoveryDraft = {
  keywords: $('#discovery-keywords').value,
  english_terms: $('#discovery-english').value,
  use_llm: $('#discovery-use-llm').checked,
  max_rounds: $('#discovery-round-limit').value,
  max_queries: $('#discovery-query-limit').value,
  per_query: $('#discovery-result-limit').value,
 };
}
function resetDiscoveryDraft() {
 discoveryDraft = null; discoveryPicked.clear(); discoveryVisibleCount = 24;
 discoveryPlanRequestState = null;
 discoveryFocusKey = null; discoveryLassoMode = false; discoveryMapCache = null;
 discoveryMapObserver?.disconnect(); discoveryMapObserver = null;
 Object.assign(discoveryCompare, {mode: 'map', facet: 'all', source: 'all', search: '', selectedOnly: false, language: 'ja', zoom: 'normal'});
 discoveryResultsSignature = ''; discoveryHistorySignature = '';
 const root = $('#discovery-content'); if (root) root.innerHTML = '';
}
function discoveryTextList(values) {
 return (values || []).map(value => typeof value === 'string' ? value : value.term || value.label || '').filter(Boolean);
}
function discoverySource(source) { return typeof source === 'string' ? source : JSON.stringify(source || 'CSV'); }
function discoveryPlanChanged() {
 const plan = state.discovery?.plan;
 return Boolean(plan && $('#discovery-keywords') && (
  $('#discovery-keywords').value.trim() !== (plan.keywords || '').trim() ||
  ($('#discovery-english')?.value || '').trim() !== (plan.user_english_terms || '').trim()
 ));
}
function mergeDiscoveryResponse(result, adopt = false) {
 const fields = adopt ? ['candidates', 'classification_view', 'discovery', 'job'] : ['discovery', 'job'];
 state = {...state, ...Object.fromEntries(fields.filter(field => Object.prototype.hasOwnProperty.call(result, field)).map(field => [field, result[field]]))};
}
function discoveryWarnings(values) {
 return values?.length ? `<div class="discovery-warnings" role="note">${values.map(value => `<p>${esc(value)}</p>`).join('')}</div>` : '';
}
function discoveryLlmProposalHtml(plan) {
 if (!plan) return '';
 const proposal = plan.llm_proposal;
 if (!proposal) return `<div class="panel discovery-llm-report"><span class="eyebrow">LLM RESPONSE</span><h3>この計画にはLLMの反映記録がありません</h3><p class="caption">以前の版で作成した計画のため、どの語をLLMが返したかは特定できません。次回の計画作成から、受け取った提案と反映先をここに表示します。</p></div>`;
 if (!proposal.used) return `<div class="panel discovery-llm-report"><span class="eyebrow">PLAN SOURCE</span><h3>今回はLLMを使わず計画を作成しました</h3><p class="caption">入力語・分類辞書・既存候補を使っています。「LLMで観点・検索語を補う」にチェックして計画を作ると、追加された語と反映先を確認できます。</p></div>`;
 const english = discoveryTextList(proposal.english_terms), facets = (proposal.facets || []).filter(f=>f.terms?.length||f.english_terms?.length), codes = discoveryTextList(proposal.candidate_keys);
 const tags = values => values.length ? `<div class="discovery-llm-tags">${values.map(value=>`<span>${esc(value)}</span>`).join('')}</div>` : '<span class="caption">追加なし（重複・検証結果は下の記録を参照）</span>';
 return `<section class="panel discovery-llm-report" aria-label="探索ラボのLLM提案と反映先"><div class="panel-heading"><div><span class="eyebrow">LLM RESPONSE → EXPLORATION PLAN</span><h3>${english.length||facets.length||codes.length?"LLMの提案を、ここへ反映しました":"LLMの提案を確認しました · 新規追加なし"}</h3></div><span class="pill">主題語 ${english.length} · 観点 ${facets.length}</span></div><p class="caption">ラボのLLMには英語の主題語と技術観点を依頼します。分類候補は、その語を分類辞書に照合して探します。候補の追加・選択と検索の実行は別の操作です。</p><div class="discovery-llm-targets"><article><b>主題の英語検索語</b><small>反映先：探索計画の主題語（検索案は先頭6語まで）</small>${tags(english)}</article><article><b>補った技術観点</b><small>反映先：探索の観点・分類候補の語句照合</small>${facets.length?facets.map(f=>`<div class="discovery-llm-facet"><strong>${esc(f.label||f.id)}</strong>${tags([...discoveryTextList(f.terms),...discoveryTextList(f.english_terms)])}</div>`).join(''):'<span class="caption">追加なし（既存の観点を維持）</span>'}</article>${codes.length?`<article><b>返された分類コード</b><small>反映先：存在を確認できた探索仮説</small>${tags(codes)}</article>`:''}</div>${discoveryWarnings(proposal.notices)}<p class="caption">対話で探索へは、下の比較画面で分類を選び「選んだ分類を手動探索へ」で引き継ぎます。統括ワークフローでは、同じテーマの計画を処理に利用します。</p></section>`;
}
function discoveryFormHtml(draft) {
 return `<div class="panel discovery-start"><div class="discovery-intro"><span class="eyebrow">EXPAND THROUGH EVIDENCE</span><h2>見つかった特許から、<br>次の探索範囲へ。</h2><p class="muted">製品だけでなく、材料・製法・界面などの観点で探索。CSVを追加するたびに、実際の公報が持つ分類と次の検索案を見直します。</p><div class="discovery-flow" aria-label="探索の流れ"><span>観点を分ける</span><b>→</b><span>検索・CSV</span><b>→</b><span>公報の分類</span><b>→</b><span>次の検索へ</span></div></div>
 <div class="discovery-form"><label for="discovery-keywords">調べたい技術・キーワード</label><textarea id="discovery-keywords" rows="2" placeholder="例：全固体電池、界面の安定化、量産プロセス">${esc(draft.keywords)}</textarea><div class="discovery-plan-actions"><button id="discovery-plan" class="button primary discovery-action">探索計画を作る ↗</button><label class="check"><input type="checkbox" id="discovery-use-llm" ${draft.use_llm ? 'checked' : ''}>LLMで観点・検索語を補う</label></div><p class="caption" id="discovery-llm-note">LLMの返り値は英語検索語と技術観点です。計画作成後に、追加された語と反映先を表示します。チェックした場合だけキーワードを設定中のLLMへ送ります。</p><p class="caption" id="discovery-plan-note" aria-live="polite"></p></div></div>
 <div class="discovery-workflows"><div class="panel discovery-csv"><div class="discovery-section-title"><span class="discovery-step-number">01</span><div><h3>まずはCSVで育てる</h3><p class="caption">検索結果を追加し、分類の広がりを確認します。</p></div><span class="pill">API設定不要</span></div><div class="discovery-csv-actions"><button id="discovery-analyze" class="button discovery-action">取込済みCSVで検証</button><span class="caption" id="discovery-manual-count"></span></div><div class="discovery-upload"><label for="discovery-csv-file">次の検索結果CSV</label><input type="file" id="discovery-csv-file" accept=".csv,.tsv,text/csv,text/tab-separated-values"><button id="discovery-import" class="button primary discovery-action">次のCSVを追加して分析 ↗</button></div><p class="caption">CSV / TSV・20 MBまで。発明の名称・要約・IPC・公報番号などを含めてください。公報番号が同じ文献は重複をまとめ、手動探索の特許と判断はそのまま保持します。</p></div>
 <details class="panel discovery-remote"><summary><span class="discovery-section-title"><span class="discovery-step-number">02</span><span><b>外部検索を自動化する</b><small>EPO OPS · 任意の拡張</small></span><span class="discovery-summary-arrow">⌄</span></span></summary><p class="caption">公開特許のメタデータを取得し、見つかったIPCを次の検索へつなげます。</p><label for="discovery-english">英語のトピック語（任意）</label><textarea id="discovery-english" rows="2" placeholder='例：solid state battery, solid electrolyte'>${esc(draft.english_terms)}</textarea><p class="caption">英語検索に使う語です。変更後は「探索計画を作る」で反映します。</p><div class="discovery-limits"><label>最大ラウンド<input id="discovery-round-limit" type="number" min="1" max="3" step="1" value="${esc(draft.max_rounds)}"></label><label>検索回数の上限<input id="discovery-query-limit" type="number" min="1" max="12" step="1" value="${esc(draft.max_queries)}"></label><label>1検索の取得件数<input id="discovery-result-limit" type="number" min="5" max="25" step="1" value="${esc(draft.per_query)}"></label></div><p class="caption">キーワード・英語検索語・IPCをEPOへ送信します。上限や候補の収束で停止します。</p><p class="notice" id="discovery-ops-status"></p><div class="button-row"><button id="discovery-run" class="button primary discovery-action">EPO OPSで再帰検索 ↗</button><button id="discovery-settings" class="button subtle small">接続・Proxy設定 →</button></div></details></div>
 <div id="discovery-llm-request-status" role="status" aria-live="polite"></div><div id="discovery-progress" aria-live="polite"></div><div id="discovery-results"></div><div id="discovery-history"></div>
 <details class="panel discovery-architecture"><summary>このシステムの設計と、改善の意味</summary><div class="discovery-architecture-grid"><div><h3>いま動く仕組み</h3><p>観点ごとの検索案を作り、CSVまたはEPO OPSで得た公報の分類を集計します。公報・ファミリー・要否判断を根拠として、分類候補と次の検索語を更新します。自動検索では重複を除き、回数の上限まで繰り返します。</p></div><div><h3>手動探索へつなぐ</h3><p>採用したい分類だけをコンステレーションへ送ります。検索に使う分類の選択は、手動探索で確認して決められます。検索式の出力形式や、既存のTransformer学習の操作も続けて使えます。</p></div><div><h3>次の拡張案</h3><p>引用・被引用の追跡、分野ごとの探索漏れの評価、少数の関連特許からの能動学習へ拡張できます。現在の改善は探索条件の更新であり、このタブでTransformerを再学習するものではありません。</p></div></div><p class="caption">得られた候補は、検索語と取り込んだ特許の範囲に依存します。公報の同時出現は技術的な関連性の手掛かりです。IPC候補を自動採用せず、出典と内容を確認できる構成にしています。</p></details>`;
}
function discoveryHasEvidence(item){return ['patent_metadata','derived_candidate'].includes(item?.evidence_status);}
function discoveryOriginHtml(item){
 const origins=(item.classification_origins||item.origins||[]).filter(o=>['fi_to_ipc','fterm_theme_to_ipc'].includes(o.type));
 return origins.length?`<details class="discovery-origin"><summary>IPC候補の対応元を確認 (${origins.length})</summary>${origins.slice(0,12).map(o=>`<p><b>${o.type==='fi_to_ipc'?'FIからの候補':'Fタームのテーマ範囲からの候補'}</b><br>${esc(o.raw||'')} · ${esc(o.patent_id||'')}<br>${esc(o.warning||'公報に付与されたIPCと同一とは限りません。')}${o.source_version?`<br>対応表：${esc(o.source_version)}`:''}${o.ipc_version?` · IPC：${esc(o.ipc_version)}`:''}</p>`).join('')}</details>`:'';
}
function discoveryCandidateHtml(item) {
 const key = discoveryKey(item), evidence = item.evidence || [], metadata = discoveryHasEvidence(item);
 const syntheticCount = Number(item.synthetic_count ?? evidence.filter(row => row.synthetic).length);
 const supportCount = Number(item.support_count ?? evidence.length);
 const sourceLabel = (item.evidence_status==='derived_candidate'?'FI・Fターム対応からのIPC候補（付与IPCとは別）':metadata ? (syntheticCount && syntheticCount === supportCount ? '架空サンプル由来' : '公報由来候補') : '辞書候補・探索仮説') + (item.verified===false?' · 分類定義未確認':'');
 const reason = item.reason || (metadata ? '取り込んだ特許の分類に含まれます。出典と要否判断を確認してください。' : '探索の観点から挙げた仮説です。公報での検証はまだありません。');
 const preview = [item.title, reason, ...evidence.slice(0, 4).map(row => `${row.patent_id || ''} ${row.title || ''}`)].filter(Boolean).join('\n');
 const facets = discoveryTextList(item.facets).map(value => state.discovery?.plan?.facets?.find(facet => facet.id === value)?.label || value);
 return `<article class="discovery-candidate ${metadata ? 'with-evidence' : 'hypothesis'} ${discoveryPicked.has(key) ? 'chosen' : ''}"><label class="discovery-candidate-label" title="${esc(preview)}"><input type="checkbox" data-discovery-key="${esc(key)}" ${discoveryPicked.has(key) ? 'checked' : ''} ${item.selectable === false ? 'disabled' : ''}><span><small class="discovery-source ${syntheticCount ? 'synthetic' : ''}">${esc(sourceLabel)}</small><b>${esc(item.kind)} ${esc(item.code)}</b><span class="discovery-candidate-title">${esc(discoveryTitle(item))}</span></span></label>${facets.length ? `<div class="discovery-tags">${facets.map(value => `<span>${esc(value)}</span>`).join('')}</div>` : ''}<p class="discovery-reason">${esc(reason)}</p>${discoveryOriginHtml(item)}${metadata ? `<div class="discovery-evidence-count"><span><b>${Number(item.support_count || 0)}</b> 公報</span><span><b>${Number(item.family_count || 0)}</b> ファミリー等</span><span>人の必要 ${Number(item.positive_count || 0)} / AIの必要 ${Number(item.ai_positive_count || 0)}</span><span>不要 ${Number(item.negative_count || 0)}（人 ${Number(item.human_negative_count || 0)} / AI ${Number(item.ai_negative_count || 0)}）</span></div>` : ''}<details class="discovery-evidence" data-discovery-detail="${esc(key)}"><summary>${metadata ? '根拠の公報を見る' : '候補の位置づけ'} <span>↗</span></summary>${metadata ? `<ul>${evidence.map(row => `<li><b>${esc(row.patent_id || '公報番号なし')}${row.synthetic ? '<em>架空サンプル</em>' : ''}</b><span>${esc(row.title || '名称なし')}</span><small>${esc(discoverySource(row.source))} · ${row.label === 'keep' ? '必要' : row.label === 'exclude' ? '不要' : '未判定'}${row.family_id ? ` · ファミリー ${esc(row.family_id)}` : ''}</small></li>`).join('') || '<li>表示できる公報情報がありません。</li>'}</ul>${item.support_count > evidence.length ? `<p class="caption">支持する公報 ${Number(item.support_count)} 件から、ファミリー等の代表公報 ${evidence.length} 件を表示。</p>` : ''}` : '<p class="caption">分類辞書に基づく探索候補です。公報中の出現件数を示すものではありません。CSV検証または外部検索を行うと、実際の公報から得た候補を比較できます。</p>'}</details></article>`;
}
function discoveryLabels(item) {
 if (typeof PatentClassLabels !== 'undefined' && PatentClassLabels.bilingual) return PatentClassLabels.bilingual(item);
 const title = String(item.title || item.label || '');
 return {ja: item.title_ja || (/[\u3040-\u30ff\u3400-\u9fff]/u.test(title) ? title : ''), en: item.title_en || item.title_official || (!/[\u3040-\u30ff\u3400-\u9fff]/u.test(title) ? title : ''), jaStatus: item.title_ja_status || '', enStatus: ''};
}
function discoveryTitle(item, language = discoveryCompare.language) {
 if (typeof PatentClassLabels !== 'undefined' && PatentClassLabels.label) return String(PatentClassLabels.label(item, language) || item.code);
 const labels = discoveryLabels(item);
 return (language === 'en' ? labels.en || labels.ja : labels.ja || labels.en) || item.title || item.code;
}
function discoveryBilingualHtml(item) {
 const labels = discoveryLabels(item);
 const status = code => typeof PatentClassLabels !== 'undefined' ? PatentClassLabels.status(code) : ({official: '公式名称', app_caption: 'アプリ内の説明名', app_translation: 'アプリ内の参考訳', unavailable: '未収録'}[code] || '参考名称');
 return `<div class="discovery-definition"><span class="discovery-definition-language">日本語${labels.ja ? ` · ${esc(status(labels.jaStatus))}` : ''}${item.title_ja_version ? ` · ${esc(item.title_ja_version)}` : ''}</span><p lang="ja">${esc(labels.ja || '日本語名称は未収録です。英語の定義を確認してください。')}${item.title_ja_parent ? `<span class="discovery-definition-parent">上位：${esc(item.parent || item.title_ja_parent_code || '')} ${esc(item.title_ja_parent)}</span>` : ''}</p><span class="discovery-definition-language">English${labels.en ? ` · ${esc(status(labels.enStatus))}` : ''}${item.title_en_version ? ` · ${esc(item.title_en_version)}` : ''}</span><p lang="en">${esc(labels.en || '英語名称は未収録です。')}${item.title_en_parent ? `<span class="discovery-definition-parent">Parent: ${esc(item.parent || item.title_en_parent_code || '')} ${esc(item.title_en_parent)}</span>` : ''}</p></div>`;
}
function discoveryEvidenceType(item) {
 if(item.evidence_status==='derived_candidate')return 'derived';
 if (!discoveryHasEvidence(item)) return 'hypothesis';
 const count = Number(item.support_count ?? item.evidence?.length ?? 0);
 const synthetic = Number(item.synthetic_count ?? (item.evidence || []).filter(row => row.synthetic).length);
 return synthetic > 0 && synthetic >= count ? 'synthetic' : 'patent';
}
function discoveryEvidenceLabel(item) {
 const type = discoveryEvidenceType(item);
 return type === 'derived' ? 'FI・Fターム対応のIPC候補 · 付与IPCとは別' : type === 'synthetic' ? '架空サンプル由来' : type === 'patent' ? '公報由来候補' : '辞書候補・探索仮説';
}
function discoveryFilteredItems(items = discoveryItems()) {
 const search = discoveryCompare.search.trim().toLocaleLowerCase();
 return items.filter(item => {
  const labels = discoveryLabels(item);
  return (discoveryCompare.source === 'all' || discoveryEvidenceType(item) === discoveryCompare.source) &&
   (discoveryCompare.facet === 'all' || (item.facets || []).includes(discoveryCompare.facet)) &&
   (!discoveryCompare.selectedOnly || discoveryPicked.has(discoveryKey(item))) &&
   (!search || [item.kind, item.code, item.title, labels.ja, labels.en].filter(Boolean).join(' ').toLocaleLowerCase().includes(search));
 });
}
function discoveryComparisonHtml(items) {
 const counts = {patent: 0, derived:0, hypothesis: 0, synthetic: 0}; items.forEach(item => counts[discoveryEvidenceType(item)]++);
 return `<section class="panel discovery-compare-panel" aria-labelledby="discovery-compare-heading"><div class="discovery-compare-heading"><div><span class="eyebrow">A MAP OF POSSIBILITIES</span><h3 id="discovery-compare-heading">分類候補を見比べる</h3><p>近い技術を見渡して、根拠を確かめる。</p></div><div class="discovery-view-tabs" role="group" aria-label="分類候補の表示"><button data-discovery-mode="map" class="${discoveryCompare.mode === 'map' ? 'active' : ''}" aria-pressed="${discoveryCompare.mode === 'map'}">◌ 地図</button><button data-discovery-mode="list" class="${discoveryCompare.mode === 'list' ? 'active' : ''}" aria-pressed="${discoveryCompare.mode === 'list'}">☷ 一覧</button></div></div>
 <div class="discovery-source-filters" role="group" aria-label="候補の出典で絞り込み">${[['all', 'すべて', items.length], ['patent', '公報由来', counts.patent], ['derived','FI・Fターム対応',counts.derived], ['hypothesis', '探索仮説', counts.hypothesis], ['synthetic', '架空サンプル', counts.synthetic]].map(([value, label, count]) => `<button data-discovery-source="${value}" class="${value === discoveryCompare.source ? 'active' : ''}" aria-pressed="${value === discoveryCompare.source}"><i class="${value}"></i>${label}<b>${count}</b></button>`).join('')}</div>
 <div class="discovery-compare-filters"><select id="discovery-facet-filter" aria-label="技術の観点"><option value="all">すべての観点</option>${(state.discovery?.plan?.facets || []).map(facet => `<option value="${esc(facet.id)}" ${facet.id === discoveryCompare.facet ? 'selected' : ''}>${esc(facet.label)}</option>`).join('')}</select><input id="discovery-class-search" aria-label="分類コード・名称で絞り込む" placeholder="分類コード・名称を探す" value="${esc(discoveryCompare.search)}"><select id="discovery-label-language" aria-label="地図・一覧の名称表示"><option value="ja" ${discoveryCompare.language === 'ja' ? 'selected' : ''}>日本語を優先</option><option value="en" ${discoveryCompare.language === 'en' ? 'selected' : ''}>English</option></select><label class="check"><input id="discovery-selected-only" type="checkbox" ${discoveryCompare.selectedOnly ? 'checked' : ''}>選択済み</label></div>
 <div class="discovery-compare-status"><span id="discovery-visible-status"></span><button id="discovery-clear" class="button subtle small">選択を解除</button></div><div id="discovery-comparison-body">${discoveryComparisonBodyHtml()}</div>
 <div class="discovery-adoption"><div><strong id="discovery-picked-count">${[...discoveryPicked].filter(key => items.some(item => discoveryKey(item) === key && item.selectable !== false)).length}</strong><span> 件を手動探索へ</span><p>選んだ分類を候補に追加し、検索条件は移動先で確認できます。</p></div><button id="discovery-adopt" class="button primary discovery-action">選んだ分類を手動探索へ送る →</button></div></section>`;
}
function discoveryComparisonBodyHtml() {
 const items = discoveryFilteredItems();
 if (!items.some(item => discoveryKey(item) === discoveryFocusKey)) discoveryFocusKey = items[0] ? discoveryKey(items[0]) : null;
 const focus = items.find(item => discoveryKey(item) === discoveryFocusKey);
 const view = discoveryCompare.mode === 'map' ? `<div class="discovery-map-tools"><span>クリックで選択 · 重ねて根拠を表示</span><button id="discovery-lasso-toggle" class="button subtle small ${discoveryLassoMode ? 'active' : ''}" aria-pressed="${discoveryLassoMode}">◌ 囲んで選択</button><select id="discovery-map-zoom" aria-label="地図の表示倍率"><option value="normal" ${discoveryCompare.zoom === 'normal' ? 'selected' : ''}>標準</option><option value="fit" ${discoveryCompare.zoom === 'fit' ? 'selected' : ''}>全体を見る</option><option value="large" ${discoveryCompare.zoom === 'large' ? 'selected' : ''}>拡大</option></select></div><div class="discovery-map-viewport" id="discovery-map-viewport" tabindex="0" aria-label="分類候補の地図。地図内をスクロールできます"><svg id="discovery-class-map" class="discovery-star-map ${discoveryLassoMode ? 'lasso-on' : ''}" role="group" aria-label="意味の近さから配置した分類候補"></svg>${!items.length ? '<div class="discovery-filter-empty">該当する候補はありません。<br>出典・観点・検索語を見直してください。</div>' : ''}</div><div class="discovery-map-caption">距離と見出しは技術の近さの目安です。色は出典、円内の数字は支持公報数、? は仮説です。<br>未収録の名称はもう一方の言語を表示します。地図内をスクロールできます。「全体を見る」で配置全体を確認できます。</div>` : `<div class="discovery-list-viewport"><table class="discovery-comparison-table"><thead><tr><th>選択</th><th>分類・名称</th><th>根拠</th></tr></thead><tbody>${items.map(item => `<tr class="${discoveryPicked.has(discoveryKey(item)) ? 'chosen' : ''}" data-discovery-row="${esc(discoveryKey(item))}"><td><input type="checkbox" data-discovery-key="${esc(discoveryKey(item))}" aria-label="${esc(item.code)} を選択" ${discoveryPicked.has(discoveryKey(item)) ? 'checked' : ''} ${item.selectable === false ? 'disabled' : ''}></td><td><button class="discovery-list-focus" data-discovery-focus="${esc(discoveryKey(item))}"><b>${esc(item.kind)} ${esc(item.code)}</b><span>${esc(discoveryTitle(item))}</span></button></td><td><small class="discovery-list-source ${discoveryEvidenceType(item)}">${esc(discoveryEvidenceLabel(item))}</small>${discoveryHasEvidence(item) ? `<span>${Number(item.support_count || 0)} 公報<br>${Number(item.family_count || 0)} ファミリー等</span>` : '<span>公報による検証待ち</span>'}</td></tr>`).join('') || '<tr><td colspan="3">該当する候補はありません。絞り込み条件を見直してください。</td></tr>'}</tbody></table></div>`;
 return `<div class="discovery-comparison-layout"><div class="discovery-map-side">${view}</div><aside class="discovery-inspector" id="discovery-inspector" aria-label="確認中の分類の詳細"><div class="discovery-inspector-heading"><span>INSPECT THE EVIDENCE</span><h4>分類と根拠</h4></div><div id="discovery-inspector-content">${focus ? discoveryInspectorHtml(focus) : '<p class="caption">分類を選ぶと、名称・出典・根拠の公報を確認できます。</p>'}</div></aside></div>`;
}
function discoveryInspectorHtml(item) {
 const card = discoveryCandidateHtml(item).replace('<details class="discovery-evidence"', '<details open class="discovery-evidence"');
 return card.replace('<p class="discovery-reason">', `${discoveryBilingualHtml(item)}<p class="discovery-reason">`);
}
function renderDiscoveryInspector(item) {
 if (!item || !$('#discovery-inspector-content')) return;
 discoveryFocusKey = discoveryKey(item);
 $('#discovery-inspector-content').innerHTML = discoveryInspectorHtml(item);
 $$('.discovery-star').forEach(node => node.classList.toggle('focused', node.dataset.discoveryFocus === discoveryFocusKey));
 bindDiscoveryCheckboxes($('#discovery-inspector-content'));
}
function discoveryShortTitle(item) {
 const title = discoveryTitle(item); return title.length > 20 ? title.slice(0, 18) + '…' : title;
}
function discoveryMapHtml(items, layout) {
 const {positions, groups = [], links = []} = layout;
 const groupTitle = label => typeof PatentClassLabels !== 'undefined' && PatentClassLabels.group ? PatentClassLabels.group(label, discoveryCompare.language) : label;
 return `<g class="discovery-star-regions" aria-hidden="true">${groups.map(group => `<g><rect x="${group.bounds.x}" y="${group.bounds.y}" width="${group.bounds.width}" height="${group.bounds.height}" rx="24"/><text x="${group.bounds.x + 13}" y="${group.bounds.y + 22}" data-width="${Math.max(1, group.bounds.width - 26)}">${esc(groupTitle(group.label))}<tspan> · ${group.memberIndices.length}</tspan></text></g>`).join('')}</g><g class="discovery-star-links" aria-hidden="true">${links.map(link => positions[link.from] && positions[link.to] ? `<line x1="${positions[link.from][0]}" y1="${positions[link.from][1]}" x2="${positions[link.to][0]}" y2="${positions[link.to][1]}"/>` : '').join('')}</g><g>${items.map((item, index) => { const position = positions[index], key = discoveryKey(item), selected = discoveryPicked.has(key); return `<g class="discovery-star ${discoveryEvidenceType(item)} ${selected ? 'chosen' : ''} ${key === discoveryFocusKey ? 'focused' : ''}" data-discovery-focus="${esc(key)}" data-discovery-index="${index}" data-discovery-selectable="${item.selectable !== false}" transform="translate(${position[0]} ${position[1]})" role="button" tabindex="0" aria-label="${esc(item.kind + ' ' + item.code + ' ' + discoveryTitle(item) + ' · ' + discoveryEvidenceLabel(item))}" aria-pressed="${selected}"><title>${esc(item.kind + ' ' + item.code + '\n' + discoveryTitle(item) + '\n' + discoveryEvidenceLabel(item))}</title><circle r="32"/><text class="discovery-star-kind" y="-5">${esc(item.kind)}</text><text class="discovery-star-mark" y="15">${selected ? '✓' : discoveryHasEvidence(item) ? Number(item.support_count || 0) : '?'}</text><text class="discovery-star-code" y="49">${esc(item.code)}</text><text class="discovery-star-title" y="69">${esc(discoveryShortTitle(item))}</text></g>`; }).join('')}</g><path id="discovery-lasso-path" class="discovery-lasso-path"/>`;
}
function renderDiscoveryMap() {
 const svg = $('#discovery-class-map'), viewport = $('#discovery-map-viewport'); if (!svg || !viewport) return;
 discoveryMapObserver?.disconnect();
 const items = discoveryFilteredItems(), width = viewport.clientWidth > 720 ? 900 : 620;
 const semanticItems = items.map(item => ({...item, title: discoveryLabels(item).ja || item.title, title_en: discoveryLabels(item).en}));
 const cacheKey = JSON.stringify([width, semanticItems]);
 if (discoveryMapCache?.key !== cacheKey) {
  const layout = typeof PatentClassLayout !== 'undefined' ? PatentClassLayout.compute(semanticItems, {mode: 'semantic', width}) : {width, height: Math.max(380, Math.ceil(items.length / 3) * 142 + 85), positions: items.map((_, i) => [width / 3 * (i % 3 + .5), 85 + Math.floor(i / 3) * 142])};
  discoveryMapCache = {key: cacheKey, layout};
 }
 const layout = discoveryMapCache.layout;
 svg.setAttribute('viewBox', `0 0 ${layout.width} ${layout.height}`); svg.setAttribute('width', layout.width); svg.setAttribute('height', layout.height);
 svg.innerHTML = discoveryMapHtml(items, layout); updateDiscoveryMapZoom();
 $$('.discovery-star-title, .discovery-star-code', svg).forEach(label => {
  if (label.getComputedTextLength() > 164) { label.setAttribute('textLength', '164'); label.setAttribute('lengthAdjust', 'spacingAndGlyphs'); }
 });
 $$('.discovery-star-regions text', svg).forEach(label => {
  const width = Number(label.dataset.width);
  if (width > 0 && label.getComputedTextLength() > width) { label.setAttribute('textLength', String(width)); label.setAttribute('lengthAdjust', 'spacingAndGlyphs'); }
 });
 $$('.discovery-star', svg).forEach(node => {
  const item = items[Number(node.dataset.discoveryIndex)];
  node.addEventListener('mouseenter', () => { if (!svg.dataset.drawing) renderDiscoveryInspector(item); });
  node.addEventListener('focus', () => renderDiscoveryInspector(item));
  node.addEventListener('click', () => { if (svg.dataset.dragged) return; renderDiscoveryInspector(item); toggleDiscoveryPicked(discoveryKey(item)); });
  node.addEventListener('keydown', event => { if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); renderDiscoveryInspector(item); toggleDiscoveryPicked(discoveryKey(item)); } });
 });
 bindDiscoveryLasso(svg, items, layout.positions);
 if (typeof ResizeObserver !== 'undefined') {
  discoveryMapObserver = new ResizeObserver(() => { const wanted = viewport.clientWidth > 720 ? 900 : 620; if (wanted !== discoveryMapCache?.layout.width && viewport.clientWidth) renderDiscoveryMap(); });
  discoveryMapObserver.observe(viewport);
 }
}
function updateDiscoveryMapZoom() {
 const svg = $('#discovery-class-map'); if (!svg) return;
 svg.style.width = discoveryCompare.zoom === 'large' ? '150%' : '100%';
 svg.style.height = discoveryCompare.zoom === 'fit' ? '100%' : 'auto';
}
function discoveryPointInPolygon(point, polygon) {
 let inside = false;
 for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
  const a = polygon[i], b = polygon[j];
  if ((a.y > point.y) !== (b.y > point.y) && point.x < (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x) inside = !inside;
 }
 return inside;
}
function applyDiscoveryLasso(keys) {
 if (discoveryAdopting) return;
 const allowed = new Set(discoveryFilteredItems().filter(item => item.selectable !== false).map(discoveryKey));
 for (const key of keys) if (allowed.has(key)) discoveryPicked.add(key);
 updateDiscoveryPickedDisplay();
}
function bindDiscoveryLasso(svg, items, positions) {
 const path = $('#discovery-lasso-path', svg); let polygon = [], pointer = null, dragged = false;
 const coordinate = event => { const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY; return point.matrixTransform(svg.getScreenCTM().inverse()); };
 svg.addEventListener('pointerdown', event => {
  if (!discoveryLassoMode || event.button !== 0) return;
  pointer = event.pointerId; polygon = [coordinate(event)]; dragged = false; svg.dataset.drawing = '1';
 });
 svg.addEventListener('pointermove', event => {
  if (event.pointerId !== pointer) return;
  const point = coordinate(event); if (Math.hypot(point.x - polygon[0].x, point.y - polygon[0].y) < 5 && !dragged) return;
  dragged = true; if (!svg.hasPointerCapture(pointer)) svg.setPointerCapture(pointer);
  polygon.push(point); path.setAttribute('d', 'M' + polygon.map(point => `${point.x},${point.y}`).join(' L') + ' Z');
 });
 const finish = (event, cancelled = false) => {
  if (event.pointerId !== pointer) return;
  if (svg.hasPointerCapture(pointer)) svg.releasePointerCapture(pointer);
  pointer = null; delete svg.dataset.drawing; path.setAttribute('d', '');
  if (dragged && !cancelled && polygon.length > 3) {
   svg.dataset.dragged = '1';
   applyDiscoveryLasso(items.filter((item, index) => discoveryPointInPolygon({x: positions[index][0], y: positions[index][1]}, polygon)).map(discoveryKey));
   setTimeout(() => { delete svg.dataset.dragged; }, 0);
  }
 };
 svg.addEventListener('pointerup', event => finish(event)); svg.addEventListener('pointercancel', event => finish(event, true));
}
function toggleDiscoveryPicked(key, checked) {
 if (discoveryAdopting) return;
 const item = discoveryItems().find(row => discoveryKey(row) === key); if (!item || item.selectable === false) return;
 if (checked === undefined ? !discoveryPicked.has(key) : checked) discoveryPicked.add(key); else discoveryPicked.delete(key);
 updateDiscoveryPickedDisplay();
 if (discoveryCompare.selectedOnly) renderDiscoveryComparisonBody();
}
function updateDiscoveryPickedDisplay() {
 $$('[data-discovery-key]').forEach(node => { node.checked = discoveryPicked.has(node.dataset.discoveryKey); node.closest('.discovery-candidate')?.classList.toggle('chosen', node.checked); });
 $$('.discovery-star').forEach(node => {
  const selected = discoveryPicked.has(node.dataset.discoveryFocus), item = discoveryItems().find(row => discoveryKey(row) === node.dataset.discoveryFocus);
  node.classList.toggle('chosen', selected); node.setAttribute('aria-pressed', String(selected));
  const mark = $('.discovery-star-mark', node); if (mark) mark.textContent = selected ? '✓' : discoveryHasEvidence(item) ? Number(item.support_count || 0) : '?';
 });
 $$('[data-discovery-row]').forEach(node => node.classList.toggle('chosen', discoveryPicked.has(node.dataset.discoveryRow)));
 updateDiscoveryVisibleStatus(); updateDiscoveryControls();
}
function updateDiscoveryVisibleStatus() {
 const node = $('#discovery-visible-status'); if (!node) return;
 const visible = discoveryFilteredItems(), all = discoveryItems(), hiddenPicked = [...discoveryPicked].filter(key => !visible.some(item => discoveryKey(item) === key)).length;
 node.textContent = `${visible.length} / ${all.length} 件を表示${hiddenPicked ? ` · 絞り込み外に選択 ${hiddenPicked} 件` : ''}`;
}
function bindDiscoveryCheckboxes(root = $('#discovery-results')) {
 if (!root) return;
 $$('[data-discovery-key]', root).forEach(node => node.addEventListener('change', () => toggleDiscoveryPicked(node.dataset.discoveryKey, node.checked)));
}
function renderDiscoveryComparisonBody() {
 const body = $('#discovery-comparison-body'); if (!body) return;
 discoveryMapObserver?.disconnect();
 body.innerHTML = discoveryComparisonBodyHtml(); bindDiscoveryComparisonBody(); updateDiscoveryVisibleStatus(); updateDiscoveryControls();
}
function bindDiscoveryComparisonBody() {
 renderDiscoveryMap(); bindDiscoveryCheckboxes($('#discovery-comparison-body'));
 $$('[data-discovery-focus].discovery-list-focus').forEach(node => {
  const show = () => renderDiscoveryInspector(discoveryItems().find(item => discoveryKey(item) === node.dataset.discoveryFocus));
  node.addEventListener('mouseenter', show); node.addEventListener('focus', show); node.addEventListener('click', show);
 });
 on('#discovery-lasso-toggle', 'click', () => { discoveryLassoMode = !discoveryLassoMode; const button = $('#discovery-lasso-toggle'); button.classList.toggle('active', discoveryLassoMode); button.setAttribute('aria-pressed', String(discoveryLassoMode)); $('#discovery-class-map')?.classList.toggle('lasso-on', discoveryLassoMode); });
 on('#discovery-map-zoom', 'change', event => { discoveryCompare.zoom = event.target.value; updateDiscoveryMapZoom(); });
}
function bindDiscoveryComparison() {
 $$('[data-discovery-mode]').forEach(node => node.addEventListener('click', () => {
  discoveryCompare.mode = node.dataset.discoveryMode;
  $$('[data-discovery-mode]').forEach(button => { const active = button.dataset.discoveryMode === discoveryCompare.mode; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
  renderDiscoveryComparisonBody();
 }));
 $$('[data-discovery-source]').forEach(node => node.addEventListener('click', () => {
  discoveryCompare.source = node.dataset.discoverySource;
  $$('[data-discovery-source]').forEach(button => { const active = button.dataset.discoverySource === discoveryCompare.source; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
  renderDiscoveryComparisonBody();
 }));
 on('#discovery-facet-filter', 'change', event => { discoveryCompare.facet = event.target.value; renderDiscoveryComparisonBody(); });
 on('#discovery-class-search', 'input', event => { discoveryCompare.search = event.target.value; renderDiscoveryComparisonBody(); });
 on('#discovery-selected-only', 'change', event => { discoveryCompare.selectedOnly = event.target.checked; renderDiscoveryComparisonBody(); });
 on('#discovery-label-language', 'change', event => { discoveryCompare.language = event.target.value; renderDiscoveryComparisonBody(); });
 bindDiscoveryComparisonBody(); updateDiscoveryVisibleStatus();
}
function discoveryQueryHtml(query) {
 return `<div class="discovery-query"><div><b>${esc(query.label || query.facet_id || '追加探索')}</b><span>${esc(query.reason || '')}</span>${query.query ? `<button class="button subtle small discovery-copy-query" data-discovery-query="${esc(query.query)}">式をコピー</button>` : ''}</div><pre>${esc(query.query || '')}</pre></div>`;
}
function discoveryResultsHtml(discovery) {
 const plan = discovery.plan, analysis = discovery.analysis, items = discoveryItems(discovery);
 if (!plan && !analysis) return '<div class="panel discovery-empty"><span class="empty-orbit">◎</span><h3>製品の周辺にある技術も、探索の入口に。</h3><p>まず探索計画を作り、CSVで検証してください。分類候補は根拠のあるものと探索仮説を分けて表示します。</p></div>';
 const recommendations = analysis?.recommendations || [], hypotheses = items.filter(item => !discoveryHasEvidence(item));
 const nextQueries = analysis?.next_queries || [], plannedQueries = plan?.queued_queries || [];
 const newTerms = discoveryTextList(analysis?.new_terms);
 return `${discoveryLlmProposalHtml(plan)}${discoveryComparisonHtml(items)}${plan ? `<div class="panel discovery-plan-panel"><div class="panel-heading"><div><span class="eyebrow">SEARCH FROM MULTIPLE ANGLES</span><h3>探索の観点</h3></div><span class="pill">${(plan.facets || []).length} 観点</span></div><p class="caption">探索テーマ：${esc(plan.keywords || '')}</p><div class="discovery-facets">${(plan.facets || []).map((facet, index) => `<div class="discovery-facet"><span class="discovery-facet-number">${String(index + 1).padStart(2, '0')}</span><h3>${esc(facet.label)}</h3><p>${esc(discoveryTextList(facet.terms).join(' / '))}</p>${facet.english_terms?.length ? `<small>${esc(discoveryTextList(facet.english_terms).join(' · '))}</small>` : ''}</div>`).join('')}</div>${discoveryWarnings(plan.warnings)}<details class="discovery-query-details" data-discovery-detail="plan-queries"><summary>最初の検索案を確認 <span>${plannedQueries.length} 件 · EPO OPS形式</span></summary>${plannedQueries.map(discoveryQueryHtml).join('') || '<p class="caption">英語検索語を指定し、計画を作り直すと検索案が表示されます。</p>'}</details></div>` : ''}
 ${analysis ? `<div class="panel discovery-analysis"><div class="panel-heading"><div><span class="eyebrow">FOLLOW THE EVIDENCE</span><h3>公報を読んで、範囲を見直す</h3></div><a class="button subtle small" href="/api/discovery/export" download>探索記録を保存 ↓</a></div><p class="caption">ファミリーIDがない公報は、公報ごとに1集計単位として数えます。</p><div class="discovery-metrics">${[['対象公報', analysis.counts?.documents], ['ファミリー等の集計単位', analysis.counts?.families], ['公報・対応づけの分類候補', recommendations.length], ['架空サンプル', analysis.counts?.synthetic]].map(([label, value]) => `<div><strong>${Number(value || 0).toLocaleString()}</strong><small>${label}</small></div>`).join('')}</div>${Number(analysis.counts?.excluded) ? `<p class="caption">不要と判断された特許 ${Number(analysis.counts.excluded)} 件を区別して評価しています。</p>` : ''}${discoveryWarnings(analysis.warnings)}${newTerms.length ? `<div class="discovery-new-terms"><b>次の探索に使える語</b><div class="discovery-tags">${newTerms.map(value => `<span>${esc(value)}</span>`).join('')}</div></div>` : ''}${nextQueries.length ? `<details class="discovery-query-details" data-discovery-detail="next-queries" open><summary>次の検索案 <span>${nextQueries.length} 件 · EPO OPS形式</span></summary>${nextQueries.map(discoveryQueryHtml).join('')}</details>` : ''}</div>` : ''}
 `;
}
function discoveryHistoryHtml(discovery) {
 const rounds = discovery.rounds || [], logs = discovery.logs || [], archives = discovery.archives || [];
 if (!rounds.length && !logs.length && !archives.length) return '';
 const archiveHtml = archives.length ? `<div class="discovery-query-details"><h3>以前の計画</h3><p class="caption">計画を作り直す前の公報・分析・検索記録をJSONで保存できます。</p>${archives.map(archive => `<div class="history-item"><span>${esc(archive.keywords || '探索計画')}</span><small>${esc(archive.time || '')}</small><a class="button subtle small" href="/api/discovery/export?archive_id=${esc(encodeURIComponent(String(archive.id)))}" download>記録を保存 ↓</a></div>`).join('')}</div>` : '';
 return `<details class="panel discovery-history" data-discovery-detail="history" ${discovery.status === 'running' ? 'open' : ''}><summary>探索の記録 <span>${rounds.length} ラウンド${archives.length ? ` · 以前の計画 ${archives.length} 件` : ''}</span></summary>${rounds.length ? `<div class="table-scroll"><table><thead><tr><th>ラウンド</th><th>検索数</th><th>新規公報</th><th>新規分類</th><th>累積公報</th></tr></thead><tbody>${rounds.map(round => `<tr><td>${Number(round.round || 0)}</td><td>${(round.queries || []).length}</td><td>${Number(round.new_documents || 0)}</td><td>${Number(round.new_classifications || 0)}</td><td>${Number(round.total_documents || 0)}</td></tr>`).join('')}</tbody></table></div>${rounds.map(round => `<details class="discovery-query-details" data-discovery-detail="round-${Number(round.round || 0)}"><summary>ラウンド ${Number(round.round || 0)} の検索と結果</summary>${(round.queries || []).map(query => `<div class="discovery-query"><div><b>${esc(query.label || query.id || '')}</b><span>取得 ${Number(query.result_count || 0)} 件 · 新規 ${Number(query.new_documents || 0)} 件${query.total != null ? ` · ヒット総数 ${Number(query.total).toLocaleString()} 件${query.total_is_lower_bound ? '以上' : ''}` : ''}</span></div><pre>${esc(query.query || '')}</pre>${query.truncated ? `<p class="caption">検索結果の一部（先頭から取得できた ${Number(query.result_count || 0)} 件）を分析しています。ヒット全件を取得した結果ではありません。</p>` : ''}</div>`).join('') || '<p class="caption">CSVに基づく分析ラウンドです。</p>'}</details>`).join('')}` : ''}${logs.length ? `<ol class="discovery-log">${logs.slice(-40).map(log => `<li><time>${esc(log.time || '')}</time><span>${esc(log.message || '')}</span></li>`).join('')}</ol>` : ''}${archiveHtml}</details>`;
}
function discoveryReplaceHtml(selector, html) {
 const target = $(selector); if (!target) return;
 const open = new Map($$('details[data-discovery-detail]', target).map(node => [node.dataset.discoveryDetail, node.open]));
 const scroll = ['#discovery-map-viewport', '#discovery-inspector', '.discovery-list-viewport'].map(selector => { const node = $(selector, target); return {selector, top: node?.scrollTop || 0, left: node?.scrollLeft || 0}; });
 const active = typeof document !== 'undefined' && target.contains?.(document.activeElement) ? document.activeElement : null;
 const focus = active?.id ? {id: active.id, start: active.selectionStart, end: active.selectionEnd} : active?.dataset.discoveryFocus ? {key: active.dataset.discoveryFocus} : null;
 target.innerHTML = html;
 $$('details[data-discovery-detail]', target).forEach(node => { if (open.has(node.dataset.discoveryDetail)) node.open = open.get(node.dataset.discoveryDetail); });
 const restore = () => {
  scroll.forEach(position => { const node = $(position.selector, target); if (node) { node.scrollTop = position.top; node.scrollLeft = position.left; } });
  if (focus) { const node = focus.id ? $('#' + focus.id, target) : $$('[data-discovery-focus]', target).find(node => node.dataset.discoveryFocus === focus.key); node?.focus({preventScroll: true}); if (Number.isInteger(focus.start) && node?.setSelectionRange) node.setSelectionRange(focus.start, focus.end); }
 };
 restore(); return restore;
}
function renderDiscovery() {
 const root = $('#discovery-content'); if (!root || !state) return;
 const discovery = state.discovery || {};
 if (!$('#discovery-keywords')) {
  discoveryDraft ||= {keywords: discovery.plan?.keywords || state.keywords || '', english_terms: discovery.plan?.user_english_terms || '', use_llm: false, max_rounds: 2, max_queries: 6, per_query: 10};
  root.innerHTML = discoveryFormHtml(discoveryDraft); discoveryResultsSignature = ''; discoveryHistorySignature = '';
  bindDiscoveryForm();
 }
 const resultSignature = JSON.stringify([discovery.plan, discovery.analysis, discoveryVisibleCount]);
 if (resultSignature !== discoveryResultsSignature) {
  discoveryResultsSignature = resultSignature;
  const allowed = new Set(discoveryItems(discovery).filter(item => item.selectable !== false).map(discoveryKey));
  for (const key of discoveryPicked) if (!allowed.has(key)) discoveryPicked.delete(key);
  const restore = discoveryReplaceHtml('#discovery-results', discoveryResultsHtml(discovery)); bindDiscoveryResults(); restore?.();
 }
 const historySignature = JSON.stringify([discovery.rounds, discovery.logs, discovery.archives]);
 if (historySignature !== discoveryHistorySignature) { discoveryHistorySignature = historySignature; discoveryReplaceHtml('#discovery-history', discoveryHistoryHtml(discovery)); }
 const statusLabels = {idle: '開始前', planned: '探索計画を作成済み', running: '探索しています', done: '分析が完了しました', error: '探索を確認してください', stopped: '探索を停止しました'};
 $('#discovery-progress').innerHTML = discovery.status && discovery.status !== 'idle' ? `<div class="discovery-status ${discovery.status === 'error' ? 'error' : ''}"><span class="${discovery.status === 'running' ? 'discovery-pulse' : ''}"></span><b>${esc(statusLabels[discovery.status] || discovery.status)}</b>${discovery.last_error ? `<p>${esc(discovery.last_error)}</p>` : '<p>候補を確認し、必要な範囲を手動探索へ引き継げます。</p>'}${discovery.status === 'running' && state.job?.status === 'running' ? '<button id="discovery-stop" class="button subtle small">停止する</button>' : ''}</div>` : '';
 on('#discovery-stop', 'click', async () => { await api('/job/stop', {}); toast('停止を依頼しました。実行中の通信が終わると停止します。'); });
 $('#discovery-manual-count').textContent = `手動探索に ${Number(state.patents?.length || 0)} 件${state.demo ? ' · 架空サンプル' : ''}`;
 const opsReady = Boolean(state.settings?.ops_key_set && state.settings?.ops_secret_set);
 $('#discovery-ops-status').textContent = opsReady ? 'EPO OPSの認証情報は設定済みです。Proxyが必要な環境では接続設定も確認してください。' : 'EPO OPSのキーとシークレットは未設定です。CSVの分析はこのまま利用できます。自動検索を使う際に接続設定を行ってください。';
 updateDiscoveryControls();
 renderDiscoveryPlanRequest();
}
function renderDiscoveryPlanRequest() {
 const host=$('#discovery-llm-request-status');if(!host)return;
 const request=discoveryPlanRequestState;
 host.innerHTML=!request?'':request.status==='running'?'<p class="discovery-plan-request pending">探索計画を作成中です。提案の検証と保存が終わるまで、現在の計画を表示しています。</p>':request.status==='error'?`<p class="discovery-plan-request error">今回の計画は作成できませんでした：${esc(request.message)}<br>表示中の結果は変更前の計画です。</p>`:'';
}
function updateDiscoveryControls() {
 const unavailable = discoveryRequestPending || state.job?.status === 'running';
 const needsPlan = !state.discovery?.plan || discoveryPlanChanged();
 $$('#discovery-content .discovery-action').forEach(button => { button.disabled = Boolean(unavailable); });
 if ($('#discovery-run')) $('#discovery-run').disabled = Boolean(unavailable || !state.settings?.ops_key_set || !state.settings?.ops_secret_set || needsPlan);
 if ($('#discovery-analyze')) $('#discovery-analyze').disabled = Boolean(unavailable || !state.patents?.length || needsPlan);
 if ($('#discovery-import')) $('#discovery-import').disabled = Boolean(unavailable || !$('#discovery-csv-file')?.files?.length || needsPlan);
 if ($('#discovery-plan-note')) $('#discovery-plan-note').textContent = !state.discovery?.plan ? '① 探索計画を作成 → ② CSVで検証 → ③ 次のCSVを追加、の順に進めます。' : `${discoveryPlanChanged() ? 'テーマ・英語検索語が変更されています。「探索計画を作る」で反映してからCSVを分析してください。' : '現在の探索計画でCSVを分析します。'} 計画を作り直すと新しい蓄積を開始し、以前の計画は記録に保存します。`;
 const allowed = new Set(discoveryItems().filter(item => item.selectable !== false).map(discoveryKey));
 const selected = [...discoveryPicked].filter(key => allowed.has(key));
 if ($('#discovery-adopt')) $('#discovery-adopt').disabled = Boolean(unavailable || !selected.length);
 if ($('#discovery-picked-count')) $('#discovery-picked-count').textContent = selected.length;
 $$('[data-discovery-key]').forEach(node => { const item = discoveryItems().find(item => discoveryKey(item) === node.dataset.discoveryKey); node.disabled = Boolean(discoveryAdopting || item?.selectable === false); });
 $$('.discovery-star').forEach(node => node.setAttribute('aria-disabled', String(discoveryAdopting || node.dataset.discoverySelectable === 'false')));
}
async function requestDiscovery(path, body, options) {
 if (discoveryRequestPending || state.job?.status === 'running') return false;
 captureDiscoveryDraft(); discoveryRequestPending = true; updateDiscoveryControls();
 if(path==='/discovery/plan'){discoveryPlanRequestState={status:'running'};renderDiscoveryPlanRequest();}
 try { const result = await api(path, body, options); mergeDiscoveryResponse(result.job_id ? await api('/state') : result); if(path==='/discovery/plan')discoveryPlanRequestState=null; updateChrome(); return true; }
 catch(error){if(path==='/discovery/plan'){discoveryPlanRequestState={status:'error',message:error.message||String(error)};renderDiscoveryPlanRequest();}throw error;}
 finally { discoveryRequestPending = false; if (activeTab === 'discovery') renderDiscovery(); }
}
function discoveryLimits(draft) {
 const limits = {max_rounds: [1, 3], max_queries: [1, 12], per_query: [5, 25]}, result = {};
 for (const [name, [min, max]] of Object.entries(limits)) {
  const value = Number(draft[name]);
  if (!Number.isInteger(value) || value < min || value > max) throw new Error('検索の上限は、表示されている範囲の整数で指定してください。');
  result[name] = value;
 }
 return result;
}
function bindDiscoveryForm() {
 $$('#discovery-content input:not([type=file]), #discovery-content textarea').forEach(node => node.addEventListener('input', () => { captureDiscoveryDraft(); updateDiscoveryControls(); }));
 on('#discovery-plan', 'click', async () => {
  captureDiscoveryDraft();
  if (!discoveryDraft.keywords.trim()) throw new Error('調べたい技術・キーワードを入力してください。');
  if (await requestDiscovery('/discovery/plan', {keywords: discoveryDraft.keywords, english_terms: discoveryDraft.english_terms, use_llm: discoveryDraft.use_llm})) toast('探索計画を作成しました。CSVで候補を検証できます。');
 });
 on('#discovery-analyze', 'click', async () => { if (await requestDiscovery('/discovery/analyze', {})) toast('取込済みの特許から、分類候補を見直しました。'); });
 on('#discovery-csv-file', 'change', updateDiscoveryControls);
 on('#discovery-import', 'click', async () => {
  const file = $('#discovery-csv-file')?.files?.[0]; if (!file) throw new Error('追加するCSVを選んでください。');
  if (file.size > 20 * 1024 * 1024) throw new Error('CSVは20 MB以下にしてください。');
  if (await requestDiscovery('/discovery/import', undefined, {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file})) {
   $('#discovery-csv-file').value = ''; updateDiscoveryControls(); toast('CSVを追加して、分類候補と次の検索案を見直しました。');
  }
 });
 on('#discovery-run', 'click', async () => { captureDiscoveryDraft(); if (await requestDiscovery('/discovery/run', discoveryLimits(discoveryDraft))) toast('自動検索を開始しました。上限内で探索を繰り返します。'); });
 on('#discovery-settings', 'click', () => { captureDiscoveryDraft(); setTab('settings'); $('#ops-key')?.focus(); });
}
function bindDiscoveryResults() {
 $$('.discovery-copy-query').forEach(node => node.addEventListener('click', run(async () => { await navigator.clipboard.writeText(node.dataset.discoveryQuery); toast('EPO OPS形式の検索式をコピーしました。'); })));
 bindDiscoveryComparison();
 on('#discovery-clear', 'click', () => { discoveryPicked.clear(); updateDiscoveryPickedDisplay(); if (discoveryCompare.selectedOnly) renderDiscoveryComparisonBody(); });
 on('#discovery-adopt', 'click', adoptDiscoveryClasses);
}
async function adoptDiscoveryClasses() {
 if (discoveryRequestPending || state.job?.status === 'running') return;
 const allowed = new Set(discoveryItems().filter(item => item.selectable !== false).map(discoveryKey));
 const keys = [...discoveryPicked].filter(key => allowed.has(key));
 if (!keys.length) throw new Error('送る分類にチェックを入れてください。');
 captureDiscoveryDraft(); captureClassDraft(); discoveryRequestPending = true; discoveryAdopting = true; updateDiscoveryControls();
 try {
  await selectionSave;
  mergeDiscoveryResponse(await api('/discovery/adopt', {keys}), true); updateChrome();
  discoveryPicked.clear(); discoveryResultsSignature = '';
  setTab('explore'); if (step !== 0) setStep(0);
  toast(`${keys.length} 件を候補へ追加しました。検索に使う分類を選んでください。`);
 } finally { discoveryRequestPending = false; discoveryAdopting = false; if (activeTab === 'discovery') renderDiscovery(); }
}
