-- pqb SQLite スキーマ（企画書 付録C を骨子に、UI／版管理用の列を追加）
-- すべての行に created_at と actor（human／llm／policy／system）を持たせる。
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, name TEXT, purpose TEXT, input_text TEXT, countries TEXT,
  date_from TEXT, date_to TEXT, seeds_json TEXT, status TEXT, iteration INTEGER DEFAULT 1,
  dialect TEXT, csv_dialect TEXT, settings_json TEXT,
  created_at TEXT, updated_at TEXT, actor TEXT);

CREATE TABLE IF NOT EXISTS axes (
  case_id TEXT, axis_id TEXT, name TEXT, kind TEXT,      -- kind: required | auxiliary
  definition TEXT, origin TEXT, evidence TEXT, terms_json TEXT, category TEXT, fixed INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0,
  created_at TEXT, actor TEXT,
  PRIMARY KEY (case_id, axis_id));

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY, case_id TEXT, iteration INTEGER, kind TEXT,  -- term | code
  axis_id TEXT, value TEXT, scheme TEXT, level TEXT, title TEXT, variant_kind TEXT,
  origin TEXT, origin_doc TEXT, confidence REAL,
  r INTEGER, n INTEGER, r_total INTEGER, n_total INTEGER, rsj_w REAL, offer_w REAL, w_sample REAL,
  flip_rate REAL, needs_review INTEGER DEFAULT 0, dict_known INTEGER,
  status TEXT, reason_code TEXT, note TEXT, decided_by TEXT, decided_at TEXT,
  created_at TEXT, actor TEXT);
CREATE INDEX IF NOT EXISTS idx_candidates_case ON candidates(case_id, kind, axis_id);

CREATE TABLE IF NOT EXISTS queries (
  query_id TEXT PRIMARY KEY, case_id TEXT, iteration INTEGER, variant TEXT,
  dsl_json TEXT, parent_query_id TEXT, created_at TEXT, actor TEXT);
CREATE INDEX IF NOT EXISTS idx_queries_case ON queries(case_id, iteration);

CREATE TABLE IF NOT EXISTS renderings (
  query_id TEXT, dialect TEXT, part_no INTEGER, text TEXT, chars INTEGER, roundtrip_ok INTEGER,
  PRIMARY KEY (query_id, dialect, part_no));

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, case_id TEXT, query_id TEXT, iteration INTEGER, variant TEXT,
  dialect TEXT, source TEXT,  -- source: csv | api | local_index
  hit_count INTEGER, csv_path TEXT, info_json TEXT, executed_at TEXT, actor TEXT);
CREATE INDEX IF NOT EXISTS idx_runs_case ON runs(case_id, iteration);

CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, title TEXT, abstract TEXT, claims TEXT,
  pub_date TEXT, applicant TEXT, citations_json TEXT, raw_json TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS doc_codes (doc_id TEXT, scheme TEXT, code TEXT, PRIMARY KEY (doc_id, scheme, code));

CREATE TABLE IF NOT EXISTS run_docs (run_id TEXT, doc_id TEXT, rank INTEGER, PRIMARY KEY (run_id, doc_id));

-- 引用エッジ: doc_id が cited_id を引用する（被引用は逆向きに見る）
CREATE TABLE IF NOT EXISTS doc_citations (doc_id TEXT, cited_id TEXT, PRIMARY KEY (doc_id, cited_id));
CREATE INDEX IF NOT EXISTS idx_doc_citations_cited ON doc_citations(cited_id);

-- 商用DB API へのアクセス記録（利用規約・上限の遵守）
CREATE TABLE IF NOT EXISTS db_access (
  access_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, kind TEXT, endpoint TEXT, hit_count INTEGER, created_at TEXT);

CREATE TABLE IF NOT EXISTS local_index (doc_id TEXT PRIMARY KEY, added_at TEXT);

CREATE TABLE IF NOT EXISTS judgments (
  judgment_id TEXT PRIMARY KEY, case_id TEXT, doc_id TEXT, iteration INTEGER,
  selection TEXT,            -- topk | sample | citation | cal | seed
  judge TEXT,                -- human | llm | classifier | seed
  overall INTEGER, per_axis_json TEXT, flip_rate REAL, needs_review INTEGER DEFAULT 0,
  rationale TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_judgments_case ON judgments(case_id, doc_id);

CREATE TABLE IF NOT EXISTS pool (case_id TEXT, doc_id TEXT, source TEXT, added_iteration INTEGER,
  PRIMARY KEY (case_id, doc_id));

CREATE TABLE IF NOT EXISTS samples (
  sample_id TEXT PRIMARY KEY, case_id TEXT, iteration INTEGER, population_query_id TEXT,
  target_query_id TEXT, seed INTEGER, m INTEGER, s INTEGER, t INTEGER,
  recall_hat REAL, ci_low REAL, ci_high REAL, doc_ids_json TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS transforms (
  transform_id TEXT PRIMARY KEY, case_id TEXT, iteration INTEGER, op TEXT, target_json TEXT,
  source TEXT,               -- rsj | tree | llm | human
  direction TEXT,            -- narrow | widen
  pred_hits INTEGER, pred_recall_pool REAL, pred_p_at_k REAL, local_eval INTEGER DEFAULT 0,
  regression INTEGER DEFAULT 0, reason TEXT,
  status TEXT, decided_by TEXT, decided_at TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS iterations (
  case_id TEXT, iteration INTEGER, metrics_json TEXT, created_at TEXT,
  PRIMARY KEY (case_id, iteration));

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, case_id TEXT, iteration INTEGER, gate TEXT, actor TEXT,
  action TEXT, payload_json TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS llm_calls (
  call_id TEXT PRIMARY KEY, case_id TEXT, prompt_id TEXT, mode TEXT, model TEXT,
  input_hash TEXT, n_samples INTEGER, masked INTEGER, response_json TEXT, flip_rate REAL,
  status TEXT, duration_ms INTEGER, created_at TEXT);

CREATE TABLE IF NOT EXISTS manual_prompts (
  call_key TEXT PRIMARY KEY, case_id TEXT, prompt_id TEXT, sample_no INTEGER, prompt_text TEXT,
  response_text TEXT, status TEXT, created_at TEXT, answered_at TEXT);

CREATE TABLE IF NOT EXISTS masking (
  case_id TEXT, token TEXT, original TEXT, kind TEXT, PRIMARY KEY (case_id, token));

CREATE TABLE IF NOT EXISTS dictionary_terms (
  term TEXT, synonym TEXT, kind TEXT, origin TEXT, adopted INTEGER DEFAULT 0, rejected INTEGER DEFAULT 0,
  PRIMARY KEY (term, synonym));

CREATE TABLE IF NOT EXISTS dictionary_codes (
  scheme TEXT, code TEXT, title TEXT, parent TEXT, level TEXT, origin TEXT, PRIMARY KEY (scheme, code));

CREATE TABLE IF NOT EXISTS dictionary_axis_codes (
  axis_name TEXT, scheme TEXT, code TEXT, adopted INTEGER DEFAULT 0, rejected INTEGER DEFAULT 0,
  PRIMARY KEY (axis_name, scheme, code));

CREATE TABLE IF NOT EXISTS logs (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, iteration INTEGER, gate TEXT, actor TEXT,
  action TEXT, message TEXT, created_at TEXT);
