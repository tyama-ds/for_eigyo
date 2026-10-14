/* Scopus dates are collected by the user's logged-in browser, then imported as evidence. */
(() => {
  'use strict';
  const MAX_RESULT_BYTES = 1024 * 1024;
  const PAGE_SIZE = 20;
  const statuses = {
    pending: '未取得', found: '日付あり', requires_access: 'ログイン・アクセスの確認が必要',
    not_found: '出版日が見つかりません', selector_changed: '画面構造の確認が必要',
    identity_mismatch: '論文IDが一致しません', timeout: 'ページの読み込み待ちを中断',
    error: '取得エラー', outside_target: '今回の対象外', no_eid: 'Scopus EIDなし',
    identity_conflict: '論文IDに不一致があります', ready: '取得待ち', completed: '確認済み',
    partial: '一部取得済み', pending_import: '結果の取り込み待ち',
  };
  const reasons = {
    selected: '補完版に採用します', year_conflict: '元の出版年と異なるため保持します',
    future_date: '未来の日付のため採用しません', no_precise_date: '月・日を補える情報がありません',
    existing_date_kept: '既存の月日を保持します', existing_date_conflict: '既存の月日と異なるため保持します',
    already_present: '既存の日付と同じです', outside_target: '今回の対象外です',
    no_candidate: '採用できる日付がありません', no_observation: 'まだ日付を取得していません',
    no_eid: 'Scopus EIDを特定できません', identity_conflict: '論文IDの不一致を確認してください',
    not_found: '日付が取得できませんでした', invalid_date: '日付として解釈できません',
    year_only: '年のみの情報から月日を推定しません',
    missing_match_method: '出版日フィールドを特定した根拠がありません',
    invalid_scopus_url: 'Scopusの文献URLを確認してください', identity_mismatch: '論文IDが一致しません',
  };
  const matches = {testid: '出版日フィールド', label: 'Publication date の項目名'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = value => value != null && Number.isFinite(Number(value)) ? Number(value).toLocaleString('ja-JP') : '—';
  const validId = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
  const state = {version: 0, request: 0, context: null, library: null, job: null, records: null, snapshot: '', offset: 0, busy: false, recordsBusy: false, error: '', notice: ''};
  let dialog;
  const field = id => dialog?.querySelector(`[data-sd-field="${id}"]`);
  const current = version => version === state.version && Boolean(dialog?.open);
  const jobId = () => state.job?.id || state.job?.job_id;
  const message = error => error instanceof Error || typeof error?.message === 'string' ? error.message : '処理に失敗しました。もう一度お試しください。';
  const policy = () => ({allow_year_change: !!field('allow-year')?.checked, overwrite_existing: !!field('overwrite')?.checked});
  const policyKey = () => JSON.stringify(policy());
  const post = (context, url, body) => context.api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

  function ensureDialog() {
    if (dialog) return;
    dialog = document.createElement('dialog');
    dialog.id = 'scopus-dates-dialog';
    dialog.className = 'dialog dialog-wide scopus-dates-dialog';
    dialog.setAttribute('aria-labelledby', 'scopus-dates-title');
    document.body.appendChild(dialog);
    dialog.addEventListener('close', () => {++state.version; ++state.request; state.busy = false; state.recordsBusy = false;});
    dialog.addEventListener('click', handleClick);
    dialog.addEventListener('change', handleChange);
  }

  function shell() {
    dialog.innerHTML = `<div class="dialog-header"><div><div class="eyebrow">SCOPUS · BROWSER DATE LIBRARY</div><h2 id="scopus-dates-title">Scopusの画面から、出版日を補完。</h2><p class="muted">${esc(state.context.dataset.name)} · ${num(state.context.dataset.paper_count)}論文</p></div><button class="icon-button" data-sd-action="close" aria-label="Scopus出版日補完を閉じる">×</button></div>
      <div class="dialog-body sd-body">
        <p class="sd-intro">ログイン済みのChrome / Edgeで詳細ページの <strong>Publication date</strong> を読み、日付だけを独立した台帳に取り込みます。Scopus APIは不要です。</p>
        <ol class="sd-steps">
          <li><span class="sd-step-number">01</span><div><h3>拡張機能を準備</h3><p>ZIPを展開し、Chrome / Edgeの拡張機能画面で「デベロッパーモード」→「パッケージ化されていない拡張機能を読み込む」から展開したフォルダーを選びます。</p><a class="text-link" href="/api/scopus-browser-extension" download>Scopus日付補完の拡張機能を保存 ↗</a></div></li>
          <li><span class="sd-step-number">02</span><div><h3>取得リストを渡す</h3><p>下のボタンでJSONを保存し、拡張機能で読み込んで開始します。1つの専用タブで順番に取得し、一時停止・再開できます。</p></div></li>
          <li><span class="sd-step-number">03</span><div><h3>結果を取り込み、確認</h3><p>拡張機能から結果JSONを保存して、この画面へ取り込みます。日付を確認後、補完版データセットを保存して再分析します。</p></div></li>
        </ol>
        <div id="sd-library"></div>
        <section class="sd-section"><h3>取得リスト</h3><div class="sd-controls"><label>取得対象<select data-sd-field="target"><option value="missing_month">月が不明の論文</option><option value="missing_day">日まで分からない論文</option><option value="all">Scopus EIDのある全論文</option></select></label><label>1回の件数<input data-sd-field="batch" type="number" min="1" max="500" step="1" value="100"></label><label class="sd-check"><input data-sd-field="retry" type="checkbox">未取得・エラーを再試行</label></div><p class="field-help">初期値100件、最大500件。2万件以上のデータも、結果を取り込みながら続きのリストを作れます。取得済みのEIDは除外します。</p><div class="sd-actions"><button class="button button-primary" data-sd-action="queue">取得リストを作成・保存</button><button class="text-link" data-sd-action="fresh">別の条件で新しい台帳</button></div></section>
        <div id="sd-status" aria-live="polite"></div>
        <section class="sd-section"><h3>ブラウザで取得した結果を取り込む</h3><label class="sd-file-label">結果JSONファイル<input data-sd-field="file" type="file" accept=".json,application/json"></label><p class="field-help">この台帳から発行した取得リストの結果だけを受け付けます。1ファイル最大1 MB、500件。同じ結果を再取り込みしても重複登録しません。</p></section>
        <div id="sd-notice" role="status"></div><div id="sd-error" role="alert"></div><div id="sd-results"></div>
        <section class="sd-section sd-apply"><h3>補完版データセットを保存</h3><div class="sd-controls"><label class="sd-check"><input data-sd-field="allow-year" type="checkbox">元の出版年と異なる日付も採用する</label><label class="sd-check"><input data-sd-field="overwrite" type="checkbox">既存の月日も更新する</label></div><p id="sd-policy-note" class="field-help">元の出版年を保持し、月日が不明の部分を補います。年のみ・未来の日付は採用しません。</p><button class="button button-primary" data-sd-action="apply">確認した日付で補完版を保存</button><p class="field-help">元データと過去の分析結果は保持します。補完版を再分析すると、月別・3か月別の時層に反映されます。Scopusに記載されたPublication dateの定義はオンライン公開日と異なる場合があります。</p></section>
        <p class="sd-limitations">ログインやアクセス権の確認が必要なページでは、拡張機能が停止します。ブラウザでアクセスを確認してから再開してください。ログイン情報・Cookie・抄録・本文は結果ファイルや日付台帳に含めません。</p>
      </div>`;
  }

  function renderLibrary() {
    const area = dialog.querySelector('#sd-library');
    if (!state.library) {area.innerHTML = '<p role="status">日付の収録状況を確認しています…</p>'; return;}
    const c = state.library.coverage || {}, jobs = state.library.jobs || [];
    area.innerHTML = `<div class="sd-stats"><div><span>月まで利用可能</span><strong>${num(c.month_or_day)}</strong></div><div><span>月が不明</span><strong>${num(c.missing_month)}</strong></div><div><span>Scopus EIDあり</span><strong>${num(c.eligible_identity)}</strong></div></div><p class="field-help">EIDなし ${num(c.no_eid)}件 · IDの不一致 ${num(c.identity_conflicts)}件は、Scopusの取得リストから除外します。</p>${jobs.length ? `<label class="sd-history">保存済みのScopus日付台帳<select data-sd-field="history"><option value="">台帳を選択</option>${jobs.map(j => `<option value="${esc(j.id || j.job_id)}" ${(j.id || j.job_id) === jobId() ? 'selected' : ''}>${esc(String(j.created_at || '').slice(0, 19).replace('T', ' '))} · 日付あり ${num(j.found_eids)} / ${num(j.unique_eids)} EID</option>`).join('')}</select></label>` : ''}`;
  }

  function exportQuery(format) {
    const query = new URLSearchParams({format, ...Object.fromEntries(Object.entries(policy()).map(([key, value]) => [key, String(value)]))});
    if (validId(state.snapshot)) query.set('snapshot_id', state.snapshot);
    return query.toString();
  }

  function renderStatus() {
    if (!dialog?.open) return;
    const id = jobId(), locked = state.busy || state.recordsBusy;
    for (const key of ['target', 'batch', 'retry', 'history', 'file', 'allow-year', 'overwrite']) {
      const el = field(key);
      if (el) el.disabled = locked || (key === 'target' && !!state.job) || (key === 'file' && !validId(id));
    }
    for (const action of ['queue', 'fresh', 'apply', 'previous', 'next']) {
      const el = dialog.querySelector(`[data-sd-action="${action}"]`);
      if (!el) continue;
      el.disabled = locked || (action === 'apply' && (!validId(id) || !validId(state.snapshot))) || (action === 'previous' && state.offset === 0) || (action === 'next' && (!state.records || state.offset + (state.records.items?.length || 0) >= Number(state.records.total || 0)));
      if (action === 'fresh') el.hidden = !state.job;
      if (action === 'queue') el.textContent = state.job ? '続きの取得リストを保存' : '取得リストを作成・保存';
    }
    dialog.querySelector('#sd-error').textContent = state.error;
    dialog.querySelector('#sd-notice').textContent = state.notice;
    const area = dialog.querySelector('#sd-status');
    if (!state.job) {area.innerHTML = ''; return;}
    const j = state.job, total = Number(j.unique_eids) || 0, done = Number(j.processed_eids) || 0;
    area.innerHTML = `<section class="sd-job"><div class="sd-job-heading"><strong>日付台帳 · ${esc(statuses[j.status] || '取得状況')}</strong><span>${num(done)} / ${num(total)} EID 確認済み</span></div><progress max="${Math.max(1, total)}" value="${Math.min(total, done)}" aria-label="Scopus出版日の確認進捗"></progress><div class="sd-counts"><span>日付あり ${num(j.found_eids)}</span><span>未取得 ${num(j.pending_eids)}</span><span>取得エラー等 ${num(j.error_eids)}</span></div><p class="field-help">重複を除いたEIDの件数です。対象論文は${num(j.eligible_papers)}件。結果を取り込むと更新されます。</p>${validId(id) && validId(state.snapshot) ? `<div class="sd-actions"><a class="text-link" href="/api/scopus-date-jobs/${id}/export?${esc(exportQuery('csv'))}" download>日付台帳をCSVで保存 ↗</a><a class="text-link" href="/api/scopus-date-jobs/${id}/export?${esc(exportQuery('json'))}" download>日付台帳をJSONで保存 ↗</a></div>` : ''}</section>`;
  }

  function dateLabel(value) {
    if (!value) return '—';
    return typeof value === 'string' ? value : value.publication_date || value.date || String(value.year || '—');
  }

  function recordRow(row) {
    const p = row.proposed || {}, observation = row.observation || {}, candidate = p.candidate;
    const adopted = p.apply ? dateLabel(candidate) : dateLabel(row.original);
    const reason = reasons[p.reason] || statuses[p.reason] || p.reason || '';
    return `<tr><td><strong>${esc(row.title || row.paper_id)}</strong><small>${esc(row.eid || 'Scopus EIDなし')}</small></td><td>${esc(dateLabel(row.original))}<small>${esc(row.original?.date_precision || '')}</small></td><td><strong>${esc(candidate ? dateLabel(candidate) : statuses[row.status] || row.status || '未取得')}</strong>${observation.date_text ? `<small>原文：${esc(observation.date_text)}</small>` : ''}${observation.observed_at ? `<small>確認日時：${esc(observation.observed_at)}</small>` : ''}${observation.matched_by ? `<small>${esc(matches[observation.matched_by] || observation.matched_by)}</small>` : ''}</td><td><strong>${esc(adopted)}</strong><small>${esc(reason)}</small>${p.conflict ? '<span class="sd-warning">⚠ 元の年・月日と異なります</span>' : ''}${p.reason === 'future_date' ? '<span class="sd-warning">⚠ 未来の日付は補完版への採用対象外です</span>' : ''}</td></tr>`;
  }

  function renderRecords() {
    const area = dialog.querySelector('#sd-results');
    if (!state.records) {area.innerHTML = state.recordsBusy ? '<p role="status">採用する日付を確認しています…</p>' : ''; return;}
    const rows = state.records.items || [], offset = Number(state.records.offset) || 0, total = Number(state.records.total) || 0;
    area.innerHTML = `<section class="sd-preview"><h3>採用する日付のプレビュー</h3><div class="sd-table-wrap"><table><thead><tr><th>論文 / Scopus EID</th><th>元の日付</th><th>取得したPublication date</th><th>保存後の日付 / 確認事項</th></tr></thead><tbody>${rows.map(recordRow).join('') || '<tr><td colspan="4">表示できる対象論文がありません。</td></tr>'}</tbody></table></div><div class="sd-pagination"><button class="button button-quiet" data-sd-action="previous">前の20件</button><span>${total ? num(offset + 1) : 0}–${num(Math.min(total, offset + rows.length))} / ${num(total)}</span><button class="button button-quiet" data-sd-action="next">次の20件</button></div></section>`;
  }

  function resetPreview() {
    ++state.request; state.records = null; state.snapshot = ''; state.offset = 0; state.recordsBusy = false;
    renderRecords(); renderStatus();
  }

  async function records(version = state.version) {
    const id = jobId();
    if (!validId(id) || !current(version)) return;
    const request = ++state.request, offset = state.offset, key = policyKey(), context = state.context;
    const query = new URLSearchParams({offset: String(offset), limit: String(PAGE_SIZE), ...Object.fromEntries(Object.entries(policy()).map(([k, v]) => [k, String(v)]))});
    if (validId(state.snapshot)) query.set('snapshot_id', state.snapshot);
    const stillCurrent = () => current(version) && request === state.request && id === jobId() && offset === state.offset && key === policyKey();
    state.recordsBusy = true; renderRecords(); renderStatus();
    try {
      const value = await context.api(`/api/scopus-date-jobs/${id}/records?${query}`);
      if (!stillCurrent()) return;
      if (!validId(value.snapshot_id)) throw new Error('日付プレビューを固定できませんでした。台帳を開き直してください。');
      state.records = value; state.snapshot = value.snapshot_id; renderRecords();
    } catch (error) {
      if (stillCurrent()) {state.error = message(error); state.records = null; state.snapshot = ''; renderRecords();}
    } finally {
      if (stillCurrent()) {state.recordsBusy = false; renderRecords(); renderStatus();}
    }
  }

  function checkedJob(value, datasetId) {
    if (!validId(value?.id || value?.job_id) || value.dataset_id !== datasetId) throw new Error('台帳と対象データセットが一致しません。');
    return value;
  }

  async function refreshLibrary(version) {
    const context = state.context, library = await context.api(`/api/datasets/${context.dataset.id}/scopus-dates`);
    if (!current(version)) return;
    state.library = library; renderLibrary(); renderStatus();
  }

  async function loadJob(id) {
    if (!validId(id) || state.busy || state.recordsBusy) return;
    const version = ++state.version, context = state.context;
    state.busy = true; state.job = null; state.error = ''; state.notice = ''; resetPreview();
    try {
      const value = await context.api(`/api/scopus-date-jobs/${id}`);
      if (!current(version)) return;
      state.job = checkedJob(value, context.dataset.id); field('target').value = value.target || 'missing_month';
      renderLibrary(); await records(version);
    } catch (error) {if (current(version)) state.error = message(error);}
    finally {if (current(version)) {state.busy = false; renderStatus();}}
  }

  async function open(context) {
    if (!validId(context.dataset?.id)) throw new Error('出版日を補完するデータセットを選択してください。');
    ensureDialog(); const version = ++state.version;
    Object.assign(state, {context, library: null, job: null, records: null, snapshot: '', offset: 0, busy: true, recordsBusy: false, error: '', notice: ''});
    ++state.request; shell(); if (!dialog.open) dialog.showModal(); renderLibrary(); renderStatus();
    try {
      await refreshLibrary(version);
      if (!current(version)) return;
      state.busy = false;
      if (state.library?.jobs?.length) await loadJob(state.library.jobs[0].id || state.library.jobs[0].job_id);
      else renderStatus();
    } catch (error) {if (current(version)) {state.error = message(error); state.busy = false; renderStatus();}}
  }

  function queueOptions() {
    const batch = Number(field('batch')?.value);
    if (!Number.isInteger(batch) || batch < 1 || batch > 500) throw new Error('1回の件数は1〜500の整数で指定してください。');
    return {batch_size: batch, retry_errors: !!field('retry')?.checked};
  }

  function downloadJSON(value, filename) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = filename;
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function queue() {
    if (state.busy || state.recordsBusy) return;
    const version = state.version, context = state.context;
    state.error = ''; state.notice = '';
    try {
      const options = queueOptions(); state.busy = true; renderStatus();
      if (!validId(jobId())) {
        const target = field('target').value;
        if (!['missing_month', 'missing_day', 'all'].includes(target)) throw new Error('取得対象を確認してください。');
        const created = await post(context, '/api/scopus-date-jobs', {dataset_id: context.dataset.id, target});
        if (!current(version)) return;
        state.job = checkedJob(created, context.dataset.id);
      }
      const id = jobId(), value = await post(context, `/api/scopus-date-jobs/${id}/queue`, options);
      if (!current(version) || id !== jobId()) return;
      if (value.schema !== 'research-atlas.scopus-date-queue.v1' || value.job_id !== id || !validId(value.batch_id) || !Array.isArray(value.entries) || value.entries.length > 500) throw new Error('取得リストの形式を確認できませんでした。');
      if (value.entries.length) {
        downloadJSON(value, `scopus-date-queue-${value.batch_id.slice(0, 8)}.json`);
        state.notice = `${num(value.entries.length)}件の取得リストを保存しました。拡張機能で読み込み、取得結果をこの台帳へ取り込んでください。`;
      } else state.notice = '今回の条件で追加取得するEIDはありません。未取得・エラーを再試行する場合はチェックを入れてください。';
      await refreshLibrary(version);
      if (current(version)) await records(version);
    } catch (error) {if (current(version)) state.error = message(error);}
    finally {if (current(version)) {state.busy = false; renderStatus();}}
  }

  async function readResult(file, id) {
    if (!file || !Number.isFinite(file.size) || file.size < 1 || file.size > MAX_RESULT_BYTES) throw new Error('結果JSONは1ファイル1 MB以内で指定してください。');
    if (!/\.json$/i.test(file.name || '')) throw new Error('拡張機能が保存した結果JSONファイルを選択してください。');
    let value;
    try {value = JSON.parse(await file.text());} catch (_) {throw new Error('結果JSONを読み取れませんでした。拡張機能から保存し直してください。');}
    if (!value || value.schema !== 'research-atlas.scopus-date-results.v1' || value.job_id !== id || !validId(value.batch_id) || !Array.isArray(value.observations) || value.observations.length > 500) throw new Error('この台帳の取得リストに対応する結果JSONではありません。保存済みの台帳を確認してください。');
    return value;
  }

  async function importResult(file) {
    if (!file || state.busy || state.recordsBusy || !validId(jobId())) return;
    const version = state.version, id = jobId(), context = state.context;
    state.busy = true; state.error = ''; state.notice = ''; renderStatus();
    try {
      const value = await readResult(file, id);
      if (!current(version) || id !== jobId()) return;
      await post(context, `/api/scopus-date-jobs/${id}/import`, value);
      if (!current(version) || id !== jobId()) return;
      resetPreview();
      state.notice = '取得結果を日付台帳へ保存しました。原文と保存後の日付を確認してください。';
      const job = await context.api(`/api/scopus-date-jobs/${id}`);
      if (!current(version) || id !== jobId()) return;
      state.job = checkedJob(job, context.dataset.id);
      await refreshLibrary(version);
      if (current(version)) await records(version);
    } catch (error) {if (current(version)) state.error = message(error);}
    finally {if (current(version)) {field('file').value = ''; state.busy = false; renderStatus();}}
  }

  async function apply() {
    if (state.busy || state.recordsBusy || !validId(jobId()) || !validId(state.snapshot)) return;
    const version = state.version, id = jobId(), context = state.context;
    state.busy = true; state.error = ''; state.notice = ''; renderStatus();
    try {
      const value = await post(context, `/api/scopus-date-jobs/${id}/apply`, {...policy(), snapshot_id: state.snapshot});
      if (!current(version) || id !== jobId()) return;
      if (!validId(value.dataset?.id)) throw new Error('補完版データセットを確認できませんでした。');
      dialog.close(); await context.onApplied?.(value);
    } catch (error) {if (current(version)) state.error = message(error);}
    finally {if (current(version)) {state.busy = false; renderStatus();}}
  }

  function handleClick(event) {
    const name = event.target.closest('[data-sd-action]')?.dataset.sdAction;
    if (!name) return;
    event.preventDefault();
    if (name === 'close') {dialog.close(); return;}
    if (state.busy || state.recordsBusy) return;
    if (name === 'queue') {queue(); return;}
    if (name === 'apply') {apply(); return;}
    if (name === 'fresh') {
      ++state.version; state.job = null; state.error = ''; state.notice = ''; resetPreview(); renderLibrary(); renderStatus(); return;
    }
    if (name === 'previous' || name === 'next') {
      if (name === 'next' && (!state.records || state.offset + state.records.items.length >= state.records.total)) return;
      state.offset = Math.max(0, state.offset + (name === 'next' ? PAGE_SIZE : -PAGE_SIZE)); records();
    }
  }

  function handleChange(event) {
    const key = event.target.dataset.sdField;
    if (state.busy || state.recordsBusy) return;
    if (key === 'history') {loadJob(event.target.value); return;}
    if (key === 'file') {importResult(event.target.files?.[0]); return;}
    if (key === 'allow-year' || key === 'overwrite') {
      state.error = ''; resetPreview();
      dialog.querySelector('#sd-policy-note').textContent = field('allow-year').checked
        ? '⚠ 出版年・年度別の論文数が変わる場合があります。保存後の年と、再分析する期間を確認してください。年のみ・未来の日付は採用しません。'
        : '元の出版年を保持し、月日が不明の部分を補います。年のみ・未来の日付は採用しません。';
      records();
    }
  }

  window.AtlasScopusDates = Object.freeze({open});
  if (window.__ATLAS_UI_TEST__) window.__scopusDatesTest = {state, open, loadJob, records, queue, apply, importResult, readResult, queueOptions, recordRow, renderRecords, renderStatus, exportQuery, resetPreview, esc};
})();
