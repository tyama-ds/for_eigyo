/** In-memory checkpoints retain completed work without mixing documents or edits. */
const jobState = new WeakMap();

function invalid(detail) {
  const error = new Error(`翻訳の続きから再開できません（${detail}）。翻訳対象を選び直してください。`);
  error.code = 'TRANSLATION_JOB_CHANGED';
  return error;
}

function blockId(block) {
  if (!block || !['string', 'number'].includes(typeof block.id)
      || (typeof block.id === 'number' && !Number.isFinite(block.id)) || !String(block.id)) {
    throw invalid('領域 ID が不正です');
  }
  return String(block.id);
}

function stateFor(job) {
  const state = jobState.get(job);
  if (!state) throw invalid('翻訳処理が見つかりません');
  return state;
}

function assertUnchanged(entry) {
  const { block, id, original, translation, enabled, supported } = entry;
  if (blockId(block) !== id || block.original !== original) throw invalid('原文が変更されています');
  if (block.translation !== translation) throw invalid('訳文が編集されています');
  if (block.enabled !== enabled || block.supported !== supported || block.enabled === false || block.supported === false) {
    throw invalid('領域の翻訳設定が変更されています');
  }
}

/**
 * The caller binds contextKey to the document, selection and translation settings,
 * and discards the job when that context changes. Block references remain live;
 * original text, initial translations and eligibility are captured independently.
 */
export function createTranslationJob(blocks, contextKey) {
  if (!Array.isArray(blocks) || typeof contextKey !== 'string') throw invalid('翻訳対象の設定が不正です');
  const entries = new Map();
  for (const block of blocks) {
    const id = blockId(block);
    if (entries.has(id)) throw invalid('領域 ID が重複しています');
    if (typeof block.original !== 'string' || !block.original.trim()) throw invalid('原文が空です');
    if (block.enabled === false || block.supported === false) throw invalid('翻訳できない領域があります');
    entries.set(id, {
      block, id, original: block.original, translation: block.translation,
      enabled: block.enabled, supported: block.supported,
    });
  }
  const completed = new Set();
  const job = Object.freeze({ contextKey, total: entries.size, completed });
  jobState.set(job, { entries, completed });
  return job;
}

/** Return unfinished live block references in their original order. */
export function getPendingJobBlocks(job) {
  const { entries, completed } = stateFor(job);
  const pending = [];
  for (const [id, entry] of entries) {
    if (completed.has(id)) continue;
    assertUnchanged(entry);
    pending.push(entry.block);
  }
  return pending;
}

/** Validate the entire checkpoint before writing any translation. */
export function applyTranslationBatch(job, { blocks, translations } = {}) {
  const { entries, completed } = stateFor(job);
  if (!Array.isArray(blocks) || !blocks.length || !translations || typeof translations !== 'object' || Array.isArray(translations)) {
    throw invalid('翻訳結果の形式が不正です');
  }
  const ids = new Set();
  const updates = [];
  for (const input of blocks) {
    const id = blockId(input);
    const entry = entries.get(id);
    if (!entry || ids.has(id)) throw invalid('翻訳結果の領域 ID が不一致です');
    if (completed.has(id)) throw invalid('完了済みの領域が含まれています');
    if (input.text !== entry.original) throw invalid('翻訳対象の原文が不一致です');
    assertUnchanged(entry);
    if (!Object.hasOwn(translations, id) || typeof translations[id] !== 'string' || !translations[id].trim()) {
      throw invalid('翻訳文が不足しています');
    }
    ids.add(id);
    updates.push({ entry, text: translations[id] });
  }
  const keys = Reflect.ownKeys(translations);
  if (keys.length !== ids.size || keys.some(id => !ids.has(id))) throw invalid('翻訳結果の領域 ID が不一致です');
  for (const { entry, text } of updates) {
    entry.block.translation = text;
    completed.add(entry.id);
  }
  return updates.length;
}
