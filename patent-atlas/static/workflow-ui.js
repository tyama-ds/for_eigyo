import { captureLearningDraft } from './learning-ui.js';
import { hidePatentRowPreview } from './patent-inspector.js';
import { appState } from './ui-session.js';
import { $, getState } from './core.js';
import * as PatentAtlasMotion from './atlas-motion.js';
import { render as renderClassifications } from './classification-ui.js';
import { render as renderQuery } from './query-output.js';
import { render as renderMap } from './map-ui.js';
import { updateChrome } from './workspace-chrome.js';
import { render as renderRefinementWorkspace } from './refinement-ui.js';

function renderStep() {
  captureLearningDraft();
  hidePatentRowPreview();
  cancelAnimationFrame(appState.animation);
  const root = $('#step-content');
  PatentAtlasMotion.cancel(root);
  [renderClassifications, renderQuery, renderMap, renderRefinementWorkspace][appState.step](root, getState());
  updateChrome();
}

export { renderStep };
