import { replaceHtml, $, on, toast } from './core.js';
import { initMotion } from './atlas-motion.js';
import { init } from './app.js';

initMotion();
init().catch((error) => {
  const host = $('#research-workbench');
  replaceHtml(
    host,
    '<div class="panel empty-state"><h2>サーバーに接続できません</h2><p>Pythonサーバーを起動してから再読み込みしてください。保存済みのデータは削除していません。</p><button id="research-retry-connection" class="button primary" type="button">再読み込みする</button></div>'
  );
  on('#research-retry-connection', 'click', () => location.reload());
  toast(error.message, true);
});
