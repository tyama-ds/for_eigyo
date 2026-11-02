'use strict';
/* Tab-local recovery of edits, never of a reviewed preview or server state. */
const RESEARCH_DRAFT_PREFIX = 'patent-atlas:research-draft:v1:';
const RESEARCH_DRAFT_MAX_BYTES = 1024 * 1024;
let researchDraftTimer = null, researchDraftStoreInitialized = false;
let researchDraftStoreStatus = 'ready', researchDraftWasRestored = false;
let researchDraftRestoreAttempt = null;
let researchDraftOwner = null;

function researchDraftContext() {
  const workbench = typeof state === 'undefined' ? null : state?.research_workbench;
  if (!workbench || typeof workbench.id !== 'string' || !workbench.id ||
      !Number.isInteger(workbench.revision) || workbench.revision < 0) return null;
  return {
    workspace_id: workbench.id,
    revision: workbench.revision,
    research_plan_id: workbench.plan?.id || null,
    invention_plan_id: workbench.invention?.plan?.id || null,
  };
}

function researchDraftStorageKey(context) {
  return RESEARCH_DRAFT_PREFIX + encodeURIComponent(context.workspace_id);
}

function researchDraftOwnsContext(context) {
  if (researchDraftOwner === null) researchDraftOwner = context;
  return JSON.stringify(researchDraftOwner) === JSON.stringify(context);
}

// Call only after a successful API mutation and after rebasing the draft
// variables onto its response. A different workspace must initialize anew.
function researchDraftRebase() {
  const context = researchDraftContext();
  if (!context || (researchDraftOwner && researchDraftOwner.workspace_id !== context.workspace_id)) {
    researchDraftSetStatus('stale');
    return false;
  }
  if (JSON.stringify(researchDraftOwner) === JSON.stringify(context)) return true;
  if (researchDraftTimer !== null) clearTimeout(researchDraftTimer);
  researchDraftTimer = null;
  researchDraftOwner = context;
  researchDraftRestoreAttempt = JSON.stringify(context);
  researchDraftSetStatus('ready');
  return true;
}

function researchDraftStatusMessage() {
  if (researchDraftStoreStatus === 'unavailable') return '下書きを保存・復元できません。このタブを閉じる前に入力を控えてください。';
  if (researchDraftStoreStatus === 'stale') return '保存済みの調査状態が変わったため、古い下書きは自動復元していません。';
  if (researchDraftStoreStatus === 'pending') return researchDraftWasRestored ? 'このタブの下書きを復元しました · 変更を保存中' : 'このタブの下書きを保存中';
  if (researchDraftWasRestored) return 'このタブの下書きを復元しました · 検索条件は再プレビューしてください。';
  if (researchDraftStoreStatus === 'saved') return '下書きをこのタブに保存済み · 再読み込みしても続けられます。';
  return '入力はこのタブに自動保存します。';
}

function researchDraftStatusHtml() {
  return `<span id="research-draft-status" class="caption research-draft-status" role="status" aria-live="polite" data-draft-state="${researchDraftStoreStatus}">${researchDraftStatusMessage()}</span>`;
}

function researchDraftSetStatus(status) {
  researchDraftStoreStatus = status;
  if (typeof document !== 'undefined') {
    const element = document.getElementById('research-draft-status');
    if (element) {
      element.textContent = researchDraftStatusMessage();
      element.setAttribute('data-draft-state', status);
    }
  }
}

function researchDraftObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid draft object');
  return value;
}

function researchDraftString(value) {
  if (typeof value !== 'string') throw new Error('Invalid draft text');
  return value;
}

function researchDraftStrings(value) {
  if (!Array.isArray(value) || value.length > 12000) throw new Error('Invalid draft text list');
  return value.map(researchDraftString);
}

function researchDraftBoolean(value, fallback = false) {
  if (value === undefined) return fallback;
  if (typeof value !== 'boolean') throw new Error('Invalid draft selection');
  return value;
}

function researchDraftChoice(value, choices, fallback) {
  if (value === undefined && fallback !== undefined) return fallback;
  if (!choices.includes(value)) throw new Error('Invalid draft choice');
  return value;
}

function researchDraftBriefData(value) {
  if (value == null) return null;
  researchDraftObject(value);
  return {
    entry_mode: researchDraftChoice(value.entry_mode, ['target', 'discover', 'examples', 'tools']),
    purpose: researchDraftString(value.purpose), goal: researchDraftString(value.goal),
    keywords: researchDraftString(value.keywords),
    user_aspects: researchDraftStrings(value.user_aspects),
    target_ids: researchDraftStrings(value.target_ids),
  };
}

function researchDraftInputData(value) {
  if (value == null) return null;
  researchDraftObject(value);
  return {invention_text: researchDraftString(value.invention_text), manual_elements: researchDraftStrings(value.manual_elements)};
}

function researchDraftKnownRows(rows, definitions) {
  if (!Array.isArray(rows) || !Array.isArray(definitions) || rows.length !== definitions.length) throw new Error('Draft plan has changed');
  const byId = new Map();
  for (const row of rows) {
    researchDraftObject(row);
    if (typeof row.id !== 'string' || byId.has(row.id)) throw new Error('Invalid draft reference');
    byId.set(row.id, row);
  }
  if (definitions.some(row => !byId.has(row.id))) throw new Error('Unknown draft reference');
  return definitions.map(definition => [definition, byId.get(definition.id)]);
}

function researchDraftPlanData(value, definition, restoring = false) {
  if (value == null) return null;
  researchDraftObject(value);
  if (!definition || value.id !== definition.id) {
    if (restoring) throw new Error('Draft plan has changed');
    return null;
  }
  return {
    id: definition.id,
    concepts: researchDraftKnownRows(value.concepts, definition.concepts).map(([source, row]) => ({
      id: source.id, selected: researchDraftBoolean(row.selected),
      role: researchDraftChoice(row.role, ['required', 'optional', 'exclude']),
      or_group: researchDraftString(row.or_group || ''), terms: researchDraftStrings(row.terms),
    })),
    classification_keys: researchDraftStrings(value.classification_keys),
    acknowledge_unmapped: researchDraftBoolean(value.acknowledge_unmapped),
  };
}

function researchDraftInventionData(value, definition, restoring = false) {
  if (value == null) return null;
  researchDraftObject(value);
  if (!definition || value.id !== definition.id) {
    if (restoring) throw new Error('Draft plan has changed');
    return null;
  }
  const ids = definition.elements.map(row => row.id), selected = researchDraftStrings(value.element_ids);
  if (new Set(selected).size !== selected.length || selected.some(id => !ids.includes(id))) throw new Error('Unknown selected element');
  const joins = researchDraftObject(value.perspective_joins), exclusions = researchDraftObject(value.exclusions);
  return {
    id: definition.id,
    strategy: researchDraftChoice(value.strategy, ['element', 'combination', 'alternatives']),
    element_ids: selected,
    elements: researchDraftKnownRows(value.elements, definition.elements).map(([element, row]) => ({
      id: element.id,
      perspectives: researchDraftKnownRows(row.perspectives, element.perspectives).map(([source, perspective]) => ({
        id: source.id, selected: researchDraftBoolean(perspective.selected),
        terms: researchDraftStrings(perspective.terms), classification_keys: researchDraftStrings(perspective.classification_keys),
        class_mode: researchDraftChoice(perspective.class_mode, ['text', 'class', 'or', 'and']),
      })),
    })),
    perspective_joins: Object.fromEntries(ids.map(id => [id, researchDraftChoice(joins[id], ['or', 'and'], 'or')])),
    exclusions: Object.fromEntries(['all', ...ids].map(id => [id, researchDraftStrings(exclusions[id] || [])])),
    confirm_exclusions: researchDraftBoolean(value.confirm_exclusions),
    confirm_class_intersection: researchDraftBoolean(value.confirm_class_intersection),
  };
}

function researchDraftSnapshot() {
  const context = researchDraftContext();
  if (!context) return null;
  const workbench = state.research_workbench;
  return {
    version: 1, context,
    drafts: {
      brief: researchDraftBriefData(typeof researchDraft === 'undefined' ? null : researchDraft),
      research_plan: researchDraftPlanData(typeof researchPlanDraft === 'undefined' ? null : researchPlanDraft, workbench.plan),
      invention_input: researchDraftInputData(typeof inventionInputDraft === 'undefined' ? null : inventionInputDraft),
      invention_plan: researchDraftInventionData(typeof inventionPlanDraft === 'undefined' ? null : inventionPlanDraft, workbench.invention?.plan),
    },
  };
}

function researchDraftFlush() {
  if (researchDraftTimer !== null) clearTimeout(researchDraftTimer);
  researchDraftTimer = null;
  try {
    const context = researchDraftContext();
    if (!context) return false;
    if (!researchDraftOwnsContext(context)) {
      researchDraftSetStatus('stale');
      return false;
    }
    const record = researchDraftSnapshot();
    if (!record || Object.values(record.drafts).every(value => value === null)) return false;
    const encoded = JSON.stringify(record);
    if (encoded.length > RESEARCH_DRAFT_MAX_BYTES) throw new Error('Draft too large');
    globalThis.sessionStorage.setItem(researchDraftStorageKey(record.context), encoded);
    researchDraftSetStatus('saved');
    return true;
  } catch {
    researchDraftSetStatus('unavailable');
    return false;
  }
}

function researchDraftScheduleSave() {
  const context = researchDraftContext();
  if (!context) return;
  if (!researchDraftOwnsContext(context)) {
    researchDraftSetStatus('stale');
    return;
  }
  if (researchDraftTimer !== null) clearTimeout(researchDraftTimer);
  researchDraftSetStatus('pending');
  researchDraftTimer = setTimeout(researchDraftFlush, 300);
}

function researchDraftRestore() {
  const context = researchDraftContext();
  if (!context) return false;
  const signature = JSON.stringify(context);
  if (researchDraftRestoreAttempt === signature) return false;
  if (researchDraftOwner && researchDraftOwner.workspace_id !== context.workspace_id) {
    // A new project must not inherit live form caches from the previous one.
    if (typeof researchDraft !== 'undefined') researchDraft = null;
    if (typeof researchPlanDraft !== 'undefined') researchPlanDraft = null;
    if (typeof inventionInputDraft !== 'undefined') inventionInputDraft = null;
    if (typeof inventionPlanDraft !== 'undefined') inventionPlanDraft = null;
  }
  researchDraftRestoreAttempt = signature;
  researchDraftOwner = context;
  try {
    const text = globalThis.sessionStorage.getItem(researchDraftStorageKey(context));
    if (text === null) return false;
    if (text.length > RESEARCH_DRAFT_MAX_BYTES) throw new Error('Draft too large');
    const record = JSON.parse(text);
    researchDraftObject(record);
    if (record.version !== 1 || JSON.stringify(record.context) !== signature) {
      researchDraftSetStatus('stale');
      return false;
    }
    const source = researchDraftObject(record.drafts), workbench = state.research_workbench;
    // Validate every component before assigning any global, so recovery is atomic.
    const restored = {
      brief: researchDraftBriefData(source.brief),
      research_plan: researchDraftPlanData(source.research_plan, workbench.plan, true),
      invention_input: researchDraftInputData(source.invention_input),
      invention_plan: researchDraftInventionData(source.invention_plan, workbench.invention?.plan, true),
    };
    if (restored.research_plan) {
      const plan = restored.research_plan;
      plan.concepts = plan.concepts.map(row => ({...structuredClone(workbench.plan.concepts.find(c => c.id === row.id)), ...row}));
      plan.preview = null;
    }
    if (restored.invention_plan) Object.assign(restored.invention_plan, {preview: null, previewPayload: null});
    if (restored.brief && typeof researchDraft !== 'undefined') researchDraft = restored.brief;
    if (restored.research_plan && typeof researchPlanDraft !== 'undefined') researchPlanDraft = restored.research_plan;
    if (restored.invention_input && typeof inventionInputDraft !== 'undefined') inventionInputDraft = restored.invention_input;
    if (restored.invention_plan && typeof inventionPlanDraft !== 'undefined') inventionPlanDraft = restored.invention_plan;
    researchDraftWasRestored = true;
    researchDraftSetStatus('restored');
    return true;
  } catch {
    researchDraftSetStatus('unavailable');
    return false;
  }
}

function researchDraftReset() {
  if (researchDraftTimer !== null) clearTimeout(researchDraftTimer);
  researchDraftTimer = null;
  try {
    const context = researchDraftContext();
    if (context) globalThis.sessionStorage.removeItem(researchDraftStorageKey(context));
    researchDraftWasRestored = false;
    researchDraftRestoreAttempt = null;
    researchDraftSetStatus('ready');
    return true;
  } catch {
    researchDraftSetStatus('unavailable');
    return false;
  }
}

function initResearchDraftStore() {
  const restored = researchDraftRestore();
  if (!researchDraftStoreInitialized && typeof window !== 'undefined') {
    const flushLatest = () => {
      try {
        if (typeof captureResearchDraft === 'function') captureResearchDraft();
        else if (typeof captureInventionDraft === 'function') captureInventionDraft();
        researchDraftFlush();
      } catch { researchDraftSetStatus('unavailable'); }
    };
    window.addEventListener('pagehide', flushLatest);
    window.addEventListener('beforeunload', flushLatest);
    researchDraftStoreInitialized = true;
  }
  return restored;
}
