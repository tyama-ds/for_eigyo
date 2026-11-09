// HTML snapshots of the top-level templates. The modules are imported without mocks, the
// shared state is a trimmed demo workspace, and only whitespace differences are ignored.
// Regenerate with UPDATE_TEMPLATE_SNAPSHOTS=1 node --test tests/template-snapshot.test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createDom } = require('./module-harness.cjs');

const dir = path.resolve(__dirname, 'fixtures/templates');
const update = Boolean(process.env.UPDATE_TEMPLATE_SNAPSHOTS);
const demo = () => JSON.parse(fs.readFileSync(path.join(dir, 'demo-state.json'), 'utf8'));
const researchPlan = () =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, 'fixtures/research-plan-gemma-synthetic.json'), 'utf8'));
const normalize = (html) =>
  String(html ?? '')
    .replace(/\s+/g, ' ')
    .replace(/>\s+</g, '><')
    .trim();

function check(name, html) {
  const file = path.join(dir, name + '.html');
  const actual = normalize(html);
  assert.ok(actual.length > 0, `${name}: empty render`);
  if (update) {
    fs.writeFileSync(file, actual + '\n');
    return;
  }
  assert.ok(fs.existsSync(file), `${name}: snapshot missing; run with UPDATE_TEMPLATE_SNAPSHOTS=1`);
  assert.equal(actual, fs.readFileSync(file, 'utf8').trim(), name);
}

let modules;
async function load() {
  if (modules) return modules;
  const names = [
    'core',
    'ui-session',
    'classification-ui',
    'discovery-ui',
    'research-workbench',
    'research-guidance',
    'orchestration-ui',
    'query-output',
    'invention-workbench',
    'map-ui',
    'refinement-ui',
    'agent-ui',
    'convergence-ui',
    'query-library-ui',
    'projection-ui',
    'learning-ui',
    'patent-inspector',
    'research-progress',
    'research-draft-store',
  ];
  modules = {};
  for (const name of names) modules[name] = await import('../static/' + name + '.js');
  return modules;
}

async function scene(state, { tab = 'explore', step = 0, stage = 'input', editor = 'invention' } = {}) {
  const dom = createDom();
  dom.window.SVGElement.prototype.getComputedTextLength = () => 0;
  const m = await load();
  globalThis.fetch = () => new Promise(() => {});
  m.core.setState(state);
  m['ui-session'].appState.activeTab = tab;
  m['ui-session'].appState.step = step;
  Object.assign(m['research-guidance'].researchGuidanceState, {
    researchFlowStage: stage,
    researchFlowEditor: editor,
    researchFlowInputMethod: 'llm',
  });
  Object.assign(m['discovery-ui'].discoveryUiState, {
    discoveryDraft: null,
    discoveryResultsSignature: '',
    discoveryHistorySignature: '',
  });
  m['orchestration-ui'].orchestrationUiState.orchestrationDraft = null;
  m['research-workbench'].researchWorkbenchState.researchDraft = null;
  m['research-workbench'].researchWorkbenchState.researchPlanDraft = null;
  m['invention-workbench'].inventionWorkbenchState.inventionDraft = null;
  m['invention-workbench'].inventionWorkbenchState.inventionPlanDraft = null;
  const $ = (s) => dom.window.document.querySelector(s);
  return { dom, m, $, document: dom.window.document };
}

const candidateProposal = () => ({
  id: 'prop-1',
  keywords: '全固体電池 固体電解質 界面抵抗',
  llm_used: true,
  decomposition_method: 'llm',
  returned_count: 3,
  accepted_count: 2,
  new_count: 1,
  existing_count: 1,
  rejected_count: 1,
  elements: [
    { id: 'e1', name: '固体電解質', terms: ['固体電解質', 'solid electrolyte'], candidate_keys: ['IPC:H01M10/0562'] },
    { id: 'e2', name: '界面<抵抗>', terms: ['界面抵抗'], candidate_keys: [] },
  ],
  unresolved_elements: [{ id: 'e2' }],
  items: [
    { kind: 'IPC', code: 'H01M10/0562', reason: '固体電解質の定義に一致', is_new: false },
    { kind: 'F-term', code: '5H029AJ06', reason: '電池性能の観点', is_new: true },
  ],
  rejected: [
    {
      kind: 'IPC',
      code: 'H01M10/0999',
      reason: '辞書に存在しない',
      broader_candidate: { kind: 'IPC', code: 'H01M10/05', verified: true, selectable: true, title: '固体電解質電池' },
      revision: {
        status: 'retired',
        explanation: '旧版の分類',
        last_verified_version: '2019.01',
        sources: [{ url: 'https://ipcpub.wipo.int/', label: 'IPC' }],
      },
    },
  ],
  notices: ['辞書照合は 2026.01 版です。'],
});

const discoveryState = (state) => {
  const facet = (id, label, terms) => ({ id, label, terms, english_terms: ['solid electrolyte'] });
  const candidate = {
    kind: 'IPC',
    code: 'H01M10/0562',
    key: 'IPC:H01M10/0562',
    title: 'Solid electrolytes',
    title_ja: '固体電解質',
    title_en: 'Solid electrolytes',
    selectable: true,
    evidence_status: 'hypothesis',
    facets: ['material'],
    reason: '主題語との一致',
  };
  state.discovery = {
    ...state.discovery,
    status: 'done',
    plan: {
      id: 'plan-1',
      keywords: '全固体電池 界面',
      facets: [facet('material', '材料', ['固体電解質', '硫化物']), facet('process', '製法', ['加圧', '積層'])],
      candidate_hypotheses: [
        candidate,
        {
          ...candidate,
          code: 'H01M4/04',
          key: 'IPC:H01M4/04',
          title_ja: '電極の製造方法',
          title_en: 'Manufacturing electrodes',
          evidence_status: 'patent_metadata',
          support_count: 4,
          synthetic_count: 0,
          family_count: 3,
          facets: ['process'],
          evidence: [{ patent_id: 'EP1', title: 'Coating an electrode', source: 'EPO OPS' }],
        },
        {
          ...candidate,
          code: 'G01R31/36',
          key: 'IPC:G01R31/36',
          title_ja: '電池の試験',
          title_en: 'Battery testing',
          evidence_status: 'patent_metadata',
          support_count: 3,
          synthetic_count: 3,
          family_count: 3,
          facets: ['performance'],
          evidence: [{ patent_id: 'DEMO-1', title: 'Sample', synthetic: true }],
        },
      ],
      queued_queries: [{ label: '材料', facet_id: 'material', reason: '主題語', query: 'ta="solid electrolyte"' }],
      llm_proposal: {
        used: true,
        english_terms: ['solid electrolyte', 'interface resistance'],
        facets: [{ label: '材料', terms: ['硫化物'], english_terms: ['sulfide'] }],
        candidate_keys: ['IPC:H01M10/0562'],
        rejected_terms: ['<script>'],
      },
    },
    analysis: {
      counts: { documents: 12, families: 9, synthetic: 12, excluded: 2 },
      recommendations: [{ ...candidate, evidence_status: 'patent_metadata', support_count: 2 }],
      warnings: ['架空サンプルを含みます。'],
      new_terms: ['界面層', 'buffer layer'],
      next_queries: [{ label: '次の材料検索', reason: '新語', query: 'ta="buffer layer"' }],
    },
    rounds: [{ round: 1, queries: [{}, {}], new_documents: 12, new_classifications: 3, total_documents: 12 }],
    logs: [{ time: '21:05', message: 'CSVを分析しました' }],
    archives: [{ id: 'a1', keywords: '以前の計画', time: '2026-11-04' }],
  };
  return state;
};

const orchestrationState = (state, checkpoint) => {
  state.orchestration = {
    ...state.orchestration,
    status: 'review',
    stage: 'classify',
    progress: 40,
    message: '確認待ち',
    cycle: 2,
    keywords: '全固体電池',
    checkpoint,
    steps: [
      { id: 'discover', label: '分類探索', status: 'done' },
      { id: 'classify', label: '判定', status: 'active' },
      { id: 'learn', label: '学習', status: 'pending' },
    ],
    logs: [{ time: '21:06', message: '分類候補を確認してください', stage: 'classify' }],
    summary: { judged: 4, keep: 2, exclude: 1 },
    completed_stages: ['discover'],
  };
  return state;
};

const inventionState = (state) => {
  const brief = {
    entry_mode: 'target',
    purpose: '先行技術を探す',
    goal: '冷却を制御する装置',
    keywords: '冷却',
    target_ids: ['DEMO-01-001'],
    user_aspects: ['必須: 温度検出'],
  };
  const input = { invention_text: 'センサと制御部を備える装置。', manual_elements: ['温度センサ', '冷却制御部'] };
  const plan = {
    id: 'inv1',
    brief: structuredClone(brief),
    input: { ...structuredClone(input), purpose: brief.purpose, goal: brief.goal, keywords: brief.keywords },
    method: 'llm',
    summary: 'センサと制御部の案',
    elements: [
      {
        id: 'e1',
        name: '温度センサ',
        description: '温度を取得する。',
        relation: '検出値を制御部へ送る',
        evidence: [{ source_id: 'input', quote: 'センサと制御部を備える装置。' }],
        perspectives: [
          {
            id: 'e1p1',
            name: '温度検出',
            terms: ['温度検出', '温度センサ'],
            classification_keys: ['IPC:G01K'],
            reason: '温度を測る手段',
          },
        ],
      },
      {
        id: 'e2',
        name: '冷却制御部',
        description: '取得温度に応じて冷却する。',
        relation: 'センサの測定結果を使う',
        evidence: [],
        perspectives: [
          { id: 'e2p1', name: '冷却手段', terms: ['冷却'], classification_keys: [], reason: '冷却の機構' },
          { id: 'e2p2', name: '制御方式', terms: ['フィードバック'], classification_keys: [], reason: '制御の働き' },
        ],
      },
    ],
    classification_candidates: [
      { key: 'IPC:G01K', kind: 'IPC', code: 'G01K', title: '温度測定', selectable: true, verified: true, origins: [{ type: 'patent' }] },
      { key: 'IPC:F25B', kind: 'IPC', code: 'F25B', title: '冷凍機', selectable: false, verified: false, origins: [{ type: 'llm' }] },
    ],
    questions: ['要素間の関係を確認してください。'],
    classification_notes: ['F25Bは存在未確認です。'],
  };
  state.research_workbench = {
    ...state.research_workbench,
    brief,
    invention: { input, plan },
    target_patents: [state.patents[0]],
    adoptions: [{ created_at: '2026-11-05 21:00', note: '境界を確認', evaluation: { human_reviewed: 2, human_keep: 1, human_exclude: 1 } }],
  };
  return state;
};

const researchPlanState = (state) => {
  inventionState(state);
  const base = researchPlan();
  state.research_workbench.plan = {
    id: 'rp1',
    method: 'llm',
    brief: structuredClone(state.research_workbench.brief),
    ...base,
    unmapped_keywords: ['量産'],
    classification_candidates: [
      {
        key: 'IPC:H01M10/0562',
        kind: 'IPC',
        code: 'H01M10/0562',
        title: '無機固体電解質',
        selectable: true,
        verified: true,
        origins: [{ type: 'patent', patent_id: 'DEMO-01-001' }],
      },
      {
        key: 'IPC:H01M4/13',
        kind: 'IPC',
        code: 'H01M4/13',
        title: '電極',
        selectable: true,
        verified: true,
        origins: [{ type: 'concept', concept_id: 'c1' }],
      },
      {
        key: 'CPC:H01M2300/0068',
        kind: 'CPC',
        code: 'H01M2300/0068',
        title: 'Solid electrolytes',
        selectable: false,
        verified: false,
        origins: [{ type: 'llm' }],
      },
    ],
    classification_notes: ['CPC候補は存在未確認です。'],
  };
  return state;
};

const libraryEntry = () => ({
  id: 'e1',
  name: '車両調査',
  purpose: '先行技術調査',
  facets: ['用途'],
  source_format: 'jplatpat',
  original_text: '[[車両/TX+自動車/TX]*[B60/IP]]-[玩具/TX]',
  status: 'editable',
  issues: [],
  terms: ['車両', '自動車', '玩具'],
  classification_terms: ['IPC:B60'],
  query: {
    boolean_tree: {
      op: 'not',
      children: [
        {
          op: 'and',
          children: [
            { op: 'or', children: [{ op: 'text', value: '車両' }, { op: 'text', value: '自動車' }] },
            { op: 'class', system: 'IPC', value: 'B60' },
          ],
        },
        { op: 'text', value: '玩具' },
      ],
    },
  },
});

test('classification explorer', async () => {
  const s = await scene(demo());
  s.m['classification-ui'].render(s.$('#step-content'), s.m.core.getState());
  check('classification-default', s.$('#step-content').innerHTML);
});

test('classification explorer with an LLM proposal and a focused class', async () => {
  const state = demo();
  state.candidate_proposal = candidateProposal();
  state.classification_view = { ...state.classification_view, direction: 'children', trail: [{ key: 'IPC:H01M', title: '電池' }] };
  const s = await scene(state);
  s.m['classification-ui'].render(s.$('#step-content'), state);
  check('classification-proposal', s.$('#step-content').innerHTML);
  s.m['classification-ui'].classDetail(state.candidates[0]);
  check('classification-detail', s.$('#class-detail')?.innerHTML || s.$('#step-content').innerHTML);
});

test('discovery lab without and with a plan', async () => {
  let s = await scene(demo(), { tab: 'discovery' });
  s.m['discovery-ui'].render(s.$('#discovery-content'), s.m.core.getState());
  check('discovery-empty', s.$('#discovery-content').innerHTML);
  const state = discoveryState(demo());
  s = await scene(state, { tab: 'discovery' });
  s.m['discovery-ui'].render(s.$('#discovery-content'), state);
  check('discovery-plan', s.$('#discovery-content').innerHTML);
  const items = s.m['discovery-ui'].discoveryItems(state.discovery);
  const layout = (await import('../static/classification-layout.js')).default.compute(items, { mode: 'semantic', width: 900 });
  check('discovery-map', s.m['discovery-ui'].discoveryMapHtml(items, layout));
  check('discovery-inspector', s.m['discovery-ui'].discoveryInspectorHtml(state.discovery.plan.candidate_hypotheses[1]));
});

test('research workbench entry modes', async () => {
  for (const mode of ['target', 'discover', 'examples', 'tools']) {
    const state = inventionState(demo());
    state.research_workbench.brief.entry_mode = mode;
    if (mode === 'examples') state.research_workbench.library = [libraryEntry()];
    const s = await scene(state, { tab: 'workbench', stage: 'input', editor: mode === 'examples' ? 'library' : 'invention' });
    s.m['research-workbench'].render(s.$('#research-workbench'), state, 'input');
    check('research-input-' + mode, s.$('#research-workbench').innerHTML);
  }
});

test('research workbench concept plan and invention elements', async () => {
  const state = researchPlanState(demo());
  let s = await scene(state, { tab: 'workbench', stage: 'elements', editor: 'concepts' });
  s.m['research-workbench'].render(s.$('#research-workbench'), state, 'elements');
  check('research-elements-concepts', s.$('#research-workbench').innerHTML);
  s = await scene(state, { tab: 'workbench', stage: 'elements', editor: 'invention' });
  s.m['research-workbench'].render(s.$('#research-workbench'), state, 'elements');
  check('research-elements-invention', s.$('#research-workbench').innerHTML);
  s.m['research-guidance'].researchGuidanceState.researchFlowInputMethod = 'manual';
  s.m['invention-workbench'].render(s.$('#invention-workbench'));
  check('invention-manual', s.$('#invention-workbench').innerHTML);
});

test('orchestration idle and review checkpoints', async () => {
  let s = await scene(demo(), { tab: 'orchestration' });
  s.m['orchestration-ui'].render(s.$('#orchestration-content'), s.m.core.getState());
  check('orchestration-idle', s.$('#orchestration-content').innerHTML);
  const candidates = [
    { kind: 'IPC', code: 'H01M', title: '電池' },
    { kind: 'IPC', code: 'G05D', title: '車両制御', title_en: 'Vehicle control', evidence_status: 'derived_candidate' },
    { kind: 'IPC', code: 'B60W', title: '制御', selectable: false },
  ];
  const classState = orchestrationState(demo(), { type: 'classifications', selected_keys: ['IPC:G05D'], candidates });
  s = await scene(classState, { tab: 'orchestration' });
  s.m['orchestration-ui'].render(s.$('#orchestration-content'), classState);
  check('orchestration-review-classifications', s.$('#orchestration-content').innerHTML);
  const queryState = orchestrationState(demo(), {
    type: 'query',
    selected_keys: [],
    candidates: [],
    term_options: { include: ['界面', { term: '硫化物' }], exclude: [] },
    include_terms: ['界面'],
    expression: 'TEXT="界面"',
  });
  s = await scene(queryState, { tab: 'orchestration' });
  s.m['orchestration-ui'].render(s.$('#orchestration-content'), queryState);
  check('orchestration-review-query', s.$('#orchestration-content').innerHTML);
});

test('query output with and without a saved query', async () => {
  let s = await scene(demo(), { step: 1 });
  s.m['query-output'].render(s.$('#step-content'), s.m.core.getState());
  check('query-output-default', s.$('#step-content').innerHTML);
  const state = demo();
  state.queries = [];
  s = await scene(state, { step: 1 });
  s.m['query-output'].render(s.$('#step-content'), state);
  check('query-output-empty', s.$('#step-content').innerHTML);
});

test('patent map, learning and patent list', async () => {
  const state = demo();
  state.training = {
    method: '軽量学習 · TF-IDF',
    train_count: 3,
    includes_agent_labels: true,
    metrics: { precision: 0.8, recall: 0.6, f1: 0.69, n: 5 },
    evaluation_note: '独立評価の件数は少数です。',
  };
  let s = await scene(state, { step: 2 });
  s.m['map-ui'].render(s.$('#step-content'), state);
  check('map-default', s.$('#step-content').innerHTML);
  const empty = demo();
  empty.patents = [];
  s = await scene(empty, { step: 2 });
  s.m['map-ui'].render(s.$('#step-content'), empty);
  check('map-empty', s.$('#step-content').innerHTML);
});

test('refinement workspace', async () => {
  const state = demo();
  state.refinement = {
    include: ['自動運転', '電池'],
    exclude: ['冷却'],
    active_include: ['自動運転'],
    active_exclude: [],
    classifications: {
      active_keys: ['IPC:H01M10/00'],
      recommended_keys: ['IPC:B60W60/00'],
      candidates: [
        { kind: 'IPC', code: 'H01M10/00', title: '電池', recommended: false, evidence: [], keep_ids: [], exclude_ids: [] },
        { kind: 'IPC', code: 'B60W60/00', title: '自動運転', recommended: true, evidence: [], keep_count: 4, exclude_count: 0, keep_ids: ['JP1'] },
        { kind: 'IPC', code: 'G06V20/58', title: '道路の画像解析', recommended: false, evidence: [], predicted_keep_count: 8, predicted_keep_ids: ['JP-predicted'] },
      ],
    },
  };
  const s = await scene(state, { step: 3 });
  s.m['refinement-ui'].refinementUiState.refinementNeedsRefresh = false;
  s.m['refinement-ui'].render(s.$('#step-content'), state);
  check('refinement-default', s.$('#step-content').innerHTML);
  check('refinement-ipc', s.m['refinement-ui'].refinementIpcHtml(state.refinement.classifications));
});

test('agent log, convergence and query library', async () => {
  const state = demo();
  state.agent_log = [{ time: '21:07', message: 'DEMO-01-001 を必要と判定 <b>' }];
  state.convergence = {
    ...state.convergence,
    records: [
      { id: 'r1', created_at: '2026-11-05', query_id: state.queries[0].id, total_hits: 120, new_keep: 3, new_keep_rate: 0.02, topic_shift_rate: 0.01, hit_change_rate: 0.03, stable: true, note: '安定' },
    ],
    status: 'stable',
    stable_streak: 1,
  };
  state.research_workbench.library = [libraryEntry()];
  const s = await scene(state, { tab: 'agent' });
  s.m['agent-ui'].render(s.$('#agent-tab'), state);
  check('agent-log', s.$('#agent-log').innerHTML);
  const host = s.document.createElement('div');
  host.id = 'convergence-content';
  s.document.body.append(host);
  s.m['convergence-ui'].render(host, state);
  check('convergence-default', host.innerHTML);
  const held = { ...state.patents[4], agent_confidence: 0.62, llm_decision: 'keep', publication_number: 'JP-HOLD' };
  check('convergence-review', s.m['convergence-ui'].convergenceReviewHtml(held, 3));
  check('convergence-review-empty', s.m['convergence-ui'].convergenceReviewHtml(null, 0));
  check('query-library', s.m['query-library-ui'].queryLibraryHtml(state.research_workbench));
  check('query-library-tree', s.m['query-library-ui'].queryLibraryTreeHtml(libraryEntry().query.boolean_tree));
});

test('projection, learning controls, patent preview and progress', async () => {
  const state = demo();
  const s = await scene(state, { step: 2 });
  const evaluation = {
    human_keep: 2,
    human_exclude: 1,
    human_reviewed: 3,
    machine_keep: 2,
    machine_exclude: 0,
    unreviewed_count: 6,
    hold_count: 1,
    signal_ratio: 0.667,
    signal_noise_ratio: 2,
    sn_status: 'ok',
    scope_note: '人が確認した部分集合です。',
    boundary_note: '元の空間での近傍。',
    boundary_candidates: [{ id: 'DEMO-01-004', reason: '関連度0.5付近', keep_neighbor: 'DEMO-01-001', exclude_neighbor: 'DEMO-01-003' }],
    boundary_candidate_count: 1,
    target_count: 1,
    target_found: 1,
    unknown_source_count: 1,
  };
  check('projection-evaluation', s.m['projection-ui'].projectionEvaluationHtml(evaluation));
  check('projection-evaluation-loading', s.m['projection-ui'].projectionEvaluationHtml(null));
  check('projection-adoption', s.m['projection-ui'].projectionAdoptionHtml());
  check('projection-tooltip', s.m['projection-ui'].projectionTooltipHtml(state.patents[0]));
  check('learning-controls', s.m['learning-ui'].learningControlsHtml());
  check('patent-preview', s.m['patent-inspector'].previewHtml(state.patents[1]));
  check('patent-preview-empty', s.m['patent-inspector'].previewHtml());
  s.m['research-progress'].researchProgressState.researchProgressCurrent = {
    token: 'p1',
    status: 'running',
    provider: 'local',
    label: '観点を起案中',
    startedAt: 0,
    finishedAt: 65000,
  };
  check('research-progress', s.m['research-progress'].researchProgressHtml());
  s.m['research-progress'].researchProgressState.researchProgressCurrent = {
    token: 'p2',
    status: 'error',
    provider: 'external',
    label: '観点を起案中',
    message: '接続できません <b>',
    startedAt: 0,
    finishedAt: 5000,
  };
  check('research-progress-error', s.m['research-progress'].researchProgressHtml());
  s.m['research-progress'].researchProgressState.researchProgressCurrent = null;
  check('research-draft-status', s.m['research-draft-store'].researchDraftStatusHtml());
  s.m['research-guidance'].renderResearchFlowHeader();
  check('research-flow-header', s.$('#research-flow-header').innerHTML);
});
