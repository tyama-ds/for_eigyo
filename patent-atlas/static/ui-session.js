export const appState = {
  step: 0,
  activeTab: 'workbench',
  tool: 'lasso',
  picked: new Set(),
  pendingFile: undefined,
  animation: undefined,
  selectionSave: Promise.resolve(),
  shownJob: undefined,
  refinement: { include: [], exclude: [] },
  formatCatalog: [],
  outputFormat: localStorage.getItem('patent-output-format') || 'jplatpat',
  currentOutput: null,
  exportRequest: 0,
  viewedQueryId: null,
  pendingImportMode: 'replace',
  jobRefreshPending: false,
};
const colors = ['#7462a3', '#ae7959', '#678a82', '#a07299', '#9c9158', '#6a7b9f'];
const outputEdits = new Map();
export { colors, outputEdits };
