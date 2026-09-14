/* Display-only names. Never rewrite canonical classification/query data. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.PatentClassLabels = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const japanese = value => /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u.test(value || '');
  const clean = value => typeof value === 'string' ? value.trim() : '';
  const GROUPS = {
    '界面・表面':'Interfaces & surfaces', '電解質':'Electrolytes', '電極・活物質':'Electrodes & active materials',
    '製造・加工':'Manufacturing', '材料・組成':'Materials & compositions', '蓄電・電池':'Batteries & storage',
    '測定・評価':'Measurement & testing', '熱・温度':'Heat & temperature', '光学・撮像':'Optics & imaging',
    '音響・振動':'Acoustics & vibration', '農業・栽培':'Agriculture', '船舶・航行':'Marine & navigation',
    '電力・発電':'Electric power', '情報・通信':'Computing & communication', '医療・診断':'Medicine & diagnostics',
    '機構・駆動':'Mechanisms & drives', '構造・装置':'Structures & devices', '性能・評価':'Performance & testing',
    '用途・システム':'Applications & systems', '名称・階層が近い分類':'Related classifications',
  };
  function bilingual(item = {}) {
    const title = clean(item.title), official = clean(item.title_official);
    const en = clean(item.title_en) || ([official, title].find(value => value && !japanese(value) && value !== item.code) || '');
    const ja = clean(item.title_ja) || ([title, official].find(value => value && japanese(value)) || '');
    return {ja, en,
      jaStatus: clean(item.title_ja) ? clean(item.title_ja_status) || 'provided' : ja ? item.kind === 'F-term' ? 'official' : 'app_caption' : 'unavailable',
      enStatus: clean(item.title_en) ? clean(item.title_en_status) || 'provided' : en ? item.kind === 'IPC' && official === en ? 'official' : 'provided' : 'unavailable',
      jaSource: item.title_ja_source || '', enSource: item.title_en_source || ''};
  }
  function label(item, language = 'ja') {
    const names = bilingual(item);
    return names[language === 'en' ? 'en' : 'ja'] || names[language === 'en' ? 'ja' : 'en'] || clean(item?.title) || clean(item?.code);
  }
  function status(value) {
    return {official:'公式名称', official_translation:'公式日本語訳', app_translation:'アプリ内の参考訳',
      app_caption:'アプリ内の説明名', provided:'登録された名称', unavailable:'未収録'}[value] || '参考名称';
  }
  function fallback(item, language) {
    const names = bilingual(item);
    return language === 'en' ? !names.en && names.ja ? '英訳未収録・日本語を表示' : '' : !names.ja && names.en ? '日本語未収録・英語を表示' : '';
  }
  function tooltip(item) {
    const names = bilingual(item);
    return `${item.kind || ''} ${item.code || ''}\n日本語: ${names.ja || '未収録'}\nEnglish: ${names.en || 'Not available'}`;
  }
  function group(label, language = 'ja') {
    if (language !== 'en') return label;
    return String(label || '').split(' · ').map(part => GROUPS[part] || part.replace(/ 周辺$/, ' neighborhood')).join(' · ');
  }
  return Object.freeze({bilingual, label, status, fallback, tooltip, group});
});
