/**
 * Appearance-preserving Office export. The PNG for each page is the same
 * renderer used by PDF export. Text intentionally remains inside the image:
 * Office font substitution and paragraph reflow cannot move the composition.
 *
 * Word: one section per page, page-relative floating image, no text wrapping.
 * PowerPoint: one fixed slide size, original-size pages centered without crop.
 * Page units: PDF points; DrawingML units: 12,700 EMU/pt; Word: 20 twips/pt.
 *
 * Primary format references:
 * https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.sectionproperties
 * https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.drawing.wordprocessing.anchor
 * https://learn.microsoft.com/en-us/previous-versions/troubleshoot/microsoft-365/microsoft-365-apps/word/operating-parameter-limitation
 * https://support.microsoft.com/en-us/powerpoint/change-the-size-of-your-powerpoint-slides
 */
import { renderTranslatedCanvas } from './pdf-engine.js';

const XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
const NS = {
  a: 'http://schemas.openxmlformats.org/drawingml/2006/main',
  p: 'http://schemas.openxmlformats.org/presentationml/2006/main',
  w: 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
  wp: 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
  pic: 'http://schemas.openxmlformats.org/drawingml/2006/picture',
  r: 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
};
export const OFFICE_MIME = Object.freeze({
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
});
const emu = points => Math.round(points * 12700);
const twips = points => Math.round(points * 20);
const xmlEscape = value => String(value).replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;',
})[c]);
const namespaces = names => names.map(name => `xmlns:${name}="${NS[name]}"`).join(' ');

/** Validate all geometry before allocating canvases or partially building a ZIP. */
export function validateOfficePages(pages, format) {
  if (!Object.hasOwn(OFFICE_MIME, format)) throw new Error('出力形式は docx または pptx を指定してください。');
  if (!Array.isArray(pages) || !pages.length) throw new Error('出力するページがありません。');
  const max = (format === 'docx' ? 22 : 56) * 72;
  for (let i = 0; i < pages.length; i++) {
    const { width, height } = pages[i] || {};
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
      throw new Error(`${i + 1} ページのサイズが正しくありません。`);
    }
    if (width > max || height > max || (format === 'docx' && (width < 18 || height < 18))) {
      const bounds = format === 'docx' ? '0.25 ～ 22 インチ' : '56 インチ以下';
      throw new Error(`${i + 1} ページのサイズは ${format.toUpperCase()} の対応範囲（各辺 ${bounds}）を超えます。縮小せずに保存するには PDF を選択してください。`);
    }
  }
  if (format === 'pptx' && (Math.max(...pages.map(p => p.width)) < 72 || Math.max(...pages.map(p => p.height)) < 72)) {
    throw new Error('PPTX のスライドは各辺 1 インチ以上が必要です。このページサイズのまま保存するには PDF を選択してください。');
  }
}

export function getPptxGeometry(pages) {
  validateOfficePages(pages, 'pptx');
  const width = Math.max(...pages.map(p => p.width));
  const height = Math.max(...pages.map(p => p.height));
  const cx = emu(width), cy = emu(height);
  return { width, height, cx, cy, pages: pages.map(page => ({
    x: Math.round((cx - emu(page.width)) / 2), y: Math.round((cy - emu(page.height)) / 2),
    cx: emu(page.width), cy: emu(page.height),
  })) };
}

function newZip(Zip) {
  if (typeof Zip !== 'function') throw new Error('Office 出力ライブラリを読み込めません。ページを再読み込みしてください。');
  return new Zip();
}
function relationships(items) {
  return XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    + items.map(([id, type, target]) => `<Relationship Id="${id}" Type="${type.startsWith('http') ? type : NS.r + '/' + type}" Target="${xmlEscape(target)}"/>`).join('')
    + '</Relationships>';
}
function contentTypes(overrides) {
  return XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
    + overrides.map(([path, mime]) => `<Override PartName="/${path}" ContentType="${mime}"/>`).join('')
    + '</Types>';
}
function addProperties(zip, format, count) {
  zip.file('docProps/core.xml', XML + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
    + '<dc:title>Translated document</dc:title><dc:creator>PDF Translator Studio</dc:creator>'
    + '<dc:description>Layout preserved as page images. Text is not editable.</dc:description></cp:coreProperties>');
  zip.file('docProps/app.xml', XML + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
    + `<Application>PDF Translator Studio</Application><${format === 'docx' ? 'Pages' : 'Slides'}>${count}</${format === 'docx' ? 'Pages' : 'Slides'}>`
    + '<AppVersion>1.0</AppVersion></Properties>');
}
function propertiesTypes() {
  return [['docProps/core.xml', 'application/vnd.openxmlformats-package.core-properties+xml'],
    ['docProps/app.xml', 'application/vnd.openxmlformats-officedocument.extended-properties+xml']];
}
function rootRelationships(mainPath) {
  return relationships([
    ['rId1', 'officeDocument', mainPath],
    ['rId2', 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties', 'docProps/core.xml'],
    ['rId3', 'extended-properties', 'docProps/app.xml'],
  ]);
}
async function addPng(zip, path, data) {
  let bytes;
  if (typeof data === 'string' && /^data:image\/png;base64,/i.test(data)) {
    try {
      const binary = atob(data.slice(data.indexOf(',') + 1));
      bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    } catch {
      throw new Error('ページ画像が PNG 形式ではありません。');
    }
  } else {
    bytes = data instanceof Blob ? new Uint8Array(await data.arrayBuffer())
      : data instanceof ArrayBuffer ? new Uint8Array(data) : data;
  }
  if (!(bytes instanceof Uint8Array) || bytes.length < 8 || ![137, 80, 78, 71, 13, 10, 26, 10].every((v, i) => bytes[i] === v)) {
    throw new Error('ページ画像が PNG 形式ではありません。');
  }
  zip.file(path, bytes, { compression: 'STORE' });
}
function generate(zip, format, type) {
  return zip.generateAsync({ type, mimeType: OFFICE_MIME[format], compression: 'DEFLATE', compressionOptions: { level: 6 } });
}

function section(page) {
  return '<w:sectPr><w:type w:val="nextPage"/>'
    + `<w:pgSz w:w="${twips(page.width)}" w:h="${twips(page.height)}" w:orient="${page.width > page.height ? 'landscape' : 'portrait'}"/>`
    + '<w:pgMar w:top="0" w:right="0" w:bottom="0" w:left="0" w:header="0" w:footer="0" w:gutter="0"/>'
    + '<w:cols w:space="0"/></w:sectPr>';
}
function wordPage(page, index, last) {
  const id = index + 1, cx = emu(page.width), cy = emu(page.height);
  // A 0.05pt paragraph anchors the image but occupies no meaningful page space.
  // Section breaks alone paginate; explicit page breaks would create blank pages.
  return '<w:p><w:pPr><w:keepNext w:val="0"/><w:keepLines w:val="0"/><w:widowControl w:val="0"/>'
    + '<w:spacing w:before="0" w:after="0" w:line="1" w:lineRule="exact"/>'
    + '<w:rPr><w:sz w:val="2"/></w:rPr>' + (last ? '' : section(page)) + '</w:pPr>'
    + '<w:r><w:rPr><w:noProof/><w:sz w:val="2"/></w:rPr><w:drawing>'
    + '<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="0" behindDoc="0" locked="1" layoutInCell="1" allowOverlap="1">'
    + '<wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionH>'
    + '<wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV>'
    + `<wp:extent cx="${cx}" cy="${cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>`
    + `<wp:docPr id="${id}" name="Page ${id}" descr="Translated page ${id}; appearance preserved as image"/>`
    + '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
    + `<a:graphic><a:graphicData uri="${NS.pic}"><pic:pic>`
    + `<pic:nvPicPr><pic:cNvPr id="${id}" name="page-${id}.png"/><pic:cNvPicPr><a:picLocks noChangeAspect="1"/></pic:cNvPicPr></pic:nvPicPr>`
    + `<pic:blipFill><a:blip r:embed="rIdImage${id}" cstate="none"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>`
    + `<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:ln><a:noFill/></a:ln></pic:spPr>`
    + '</pic:pic></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r></w:p>';
}

/** Package already-rendered {width,height,png} pages. Useful independently of PDF.js. */
export async function buildDocx(pages, { Zip = globalThis.JSZip, type = 'blob' } = {}) {
  validateOfficePages(pages, 'docx');
  const zip = newZip(Zip);
  zip.file('_rels/.rels', rootRelationships('word/document.xml'));
  addProperties(zip, 'docx', pages.length);
  zip.file('[Content_Types].xml', contentTypes([
    ['word/document.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'],
    ['word/settings.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml'],
    ['word/styles.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml'], ...propertiesTypes(),
  ]));
  zip.file('word/document.xml', XML + `<w:document ${namespaces(['w', 'r', 'wp', 'a', 'pic'])}><w:body>`
    + pages.map((page, i) => wordPage(page, i, i === pages.length - 1)).join('') + section(pages.at(-1))
    + '</w:body></w:document>');
  zip.file('word/settings.xml', XML + `<w:settings ${namespaces(['w'])}><w:zoom w:percent="100"/>`
    + '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat><w:doNotAutoCompressPictures/></w:settings>');
  zip.file('word/styles.xml', XML + `<w:styles ${namespaces(['w'])}><w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="2"/></w:rPr></w:rPrDefault>`
    + '<w:pPrDefault><w:pPr><w:spacing w:before="0" w:after="0" w:line="1" w:lineRule="exact"/></w:pPr></w:pPrDefault></w:docDefaults></w:styles>');
  zip.file('word/_rels/document.xml.rels', relationships([
    ['rIdSettings', 'settings', 'settings.xml'], ['rIdStyles', 'styles', 'styles.xml'],
    ...pages.map((_, i) => [`rIdImage${i + 1}`, 'image', `media/page-${i + 1}.png`]),
  ]));
  for (let i = 0; i < pages.length; i++) await addPng(zip, `word/media/page-${i + 1}.png`, pages[i].png);
  return generate(zip, 'docx', type);
}

const groupTree = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
  + '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>';
const colorMap = '<p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/>';
const whiteBackground = '<p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>';

function theme() {
  const colors = [['dk1', '000000'], ['lt1', 'FFFFFF'], ['dk2', '1F2937'], ['lt2', 'F4F5F6'],
    ['accent1', '1E5C51'], ['accent2', 'E9D7AD'], ['accent3', '96B49B'], ['accent4', '667085'],
    ['accent5', '567A92'], ['accent6', 'B87A58'], ['hlink', '0563C1'], ['folHlink', '954F72']];
  const fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>';
  return XML + `<a:theme ${namespaces(['a'])} name="PDF Translator Studio"><a:themeElements><a:clrScheme name="Studio">`
    + colors.map(([name, value]) => `<a:${name}><a:srgbClr val="${value}"/></a:${name}>`).join('')
    + '</a:clrScheme><a:fontScheme name="Studio"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    + '<a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
    + `<a:fmtScheme name="Studio"><a:fillStyleLst>${fill.repeat(3)}</a:fillStyleLst><a:lnStyleLst>`
    + [6350, 12700, 19050].map(w => `<a:ln w="${w}" cap="flat" cmpd="sng" algn="ctr">${fill}<a:prstDash val="solid"/><a:miter lim="800000"/></a:ln>`).join('')
    + `</a:lnStyleLst><a:effectStyleLst>${'<a:effectStyle><a:effectLst/></a:effectStyle>'.repeat(3)}</a:effectStyleLst>`
    + `<a:bgFillStyleLst>${fill.repeat(3)}</a:bgFillStyleLst></a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>`;
}
function slide(geometry, index) {
  return XML + `<p:sld ${namespaces(['a', 'r', 'p'])}><p:cSld name="Page ${index + 1}">${whiteBackground}<p:spTree>${groupTree}`
    + `<p:pic><p:nvPicPr><p:cNvPr id="2" name="Page ${index + 1}" descr="Translated page ${index + 1}; original physical size"/>`
    + '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
    + '<p:blipFill><a:blip r:embed="rIdImage" cstate="none"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
    + `<p:spPr><a:xfrm><a:off x="${geometry.x}" y="${geometry.y}"/><a:ext cx="${geometry.cx}" cy="${geometry.cy}"/></a:xfrm>`
    + '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:ln><a:noFill/></a:ln></p:spPr></p:pic>'
    + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>';
}

export async function buildPptx(pages, { Zip = globalThis.JSZip, type = 'blob' } = {}) {
  const geometry = getPptxGeometry(pages);
  const zip = newZip(Zip);
  addProperties(zip, 'pptx', pages.length);
  zip.file('_rels/.rels', rootRelationships('ppt/presentation.xml'));
  zip.file('[Content_Types].xml', contentTypes([
    ['ppt/presentation.xml', 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml'],
    ['ppt/slideMasters/slideMaster1.xml', 'application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml'],
    ['ppt/slideLayouts/slideLayout1.xml', 'application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml'],
    ['ppt/theme/theme1.xml', 'application/vnd.openxmlformats-officedocument.theme+xml'],
    ...pages.map((_, i) => [`ppt/slides/slide${i + 1}.xml`, 'application/vnd.openxmlformats-officedocument.presentationml.slide+xml']), ...propertiesTypes(),
  ]));
  zip.file('ppt/presentation.xml', XML + `<p:presentation ${namespaces(['a', 'r', 'p'])} autoCompressPictures="0">`
    + '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rIdMaster"/></p:sldMasterIdLst><p:sldIdLst>'
    + pages.map((_, i) => `<p:sldId id="${256 + i}" r:id="rIdSlide${i + 1}"/>`).join('')
    + `</p:sldIdLst><p:sldSz cx="${geometry.cx}" cy="${geometry.cy}" type="custom"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>`);
  zip.file('ppt/_rels/presentation.xml.rels', relationships([
    ['rIdMaster', 'slideMaster', 'slideMasters/slideMaster1.xml'],
    ...pages.map((_, i) => [`rIdSlide${i + 1}`, 'slide', `slides/slide${i + 1}.xml`]),
  ]));
  zip.file('ppt/slideMasters/slideMaster1.xml', XML + `<p:sldMaster ${namespaces(['a', 'r', 'p'])}><p:cSld>${whiteBackground}<p:spTree>${groupTree}</p:spTree></p:cSld>`
    + colorMap + '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rIdLayout"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>');
  zip.file('ppt/slideMasters/_rels/slideMaster1.xml.rels', relationships([
    ['rIdLayout', 'slideLayout', '../slideLayouts/slideLayout1.xml'], ['rIdTheme', 'theme', '../theme/theme1.xml'],
  ]));
  zip.file('ppt/slideLayouts/slideLayout1.xml', XML + `<p:sldLayout ${namespaces(['a', 'r', 'p'])} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>${groupTree}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>`);
  zip.file('ppt/slideLayouts/_rels/slideLayout1.xml.rels', relationships([['rIdMaster', 'slideMaster', '../slideMasters/slideMaster1.xml']]));
  zip.file('ppt/theme/theme1.xml', theme());
  for (let i = 0; i < pages.length; i++) {
    zip.file(`ppt/slides/slide${i + 1}.xml`, slide(geometry.pages[i], i));
    zip.file(`ppt/slides/_rels/slide${i + 1}.xml.rels`, relationships([
      ['rIdLayout', 'slideLayout', '../slideLayouts/slideLayout1.xml'], ['rIdImage', 'image', `../media/page-${i + 1}.png`],
    ]));
    await addPng(zip, `ppt/media/page-${i + 1}.png`, pages[i].png);
  }
  return generate(zip, 'pptx', type);
}

function snapshotBlocks(page) {
  return (page.blocks || []).map(block => ({
    ...block, sourceRect: block.sourceRect ? { ...block.sourceRect } : undefined, fit: null,
  }));
}
function checkOverflow(page) {
  const overflowing = page.blocks.filter(block => block.enabled !== false && block.supported !== false
    && String(block.translation || '').trim() && block.fit?.overflow);
  if (!overflowing.length) return;
  const error = new Error(`${page.pageNumber} ページに、枠に収まらない訳文が ${overflowing.length} 件あります。訳文または枠のサイズを調整してください。`);
  error.code = 'TEXT_OVERFLOW';
  error.blockIds = overflowing.map(block => block.id);
  throw error;
}
async function canvasPng(canvas) {
  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob(result => result ? resolve(result) : reject(new Error('ページ画像の保存に失敗しました。')), 'image/png');
  });
  return new Uint8Array(await blob.arrayBuffer());
}

/** Browser export; renderCanvas/Zip can be supplied by offline integration tests. */
export async function exportOffice(pages, {
  format, onProgress = () => {}, Zip = globalThis.JSZip, renderCanvas = renderTranslatedCanvas,
  preparePage, releasePage,
} = {}) {
  validateOfficePages(pages, format);
  // Validate library availability before costly page rendering.
  if (typeof Zip !== 'function') newZip(Zip);
  // Freeze edits already in memory before yielding. Unread pages can only be
  // snapshotted after preparation; do not retain their canvases or PDF proxies.
  const edits = pages.map(page => page.loaded === false ? null : snapshotBlocks(page));
  const images = [];
  try {
    for (let i = 0; i < pages.length; i++) {
      let prepared = pages[i], canvas, failed = false;
      try {
        if (preparePage) prepared = await preparePage(prepared) || prepared;
        const page = { ...prepared, blocks: edits[i] || snapshotBlocks(prepared) };
        canvas = await renderCanvas(page, { scale: 2.25 });
        checkOverflow(page);
        images.push({ width: page.width, height: page.height, png: await canvasPng(canvas) });
      } catch (error) {
        failed = true;
        throw error;
      } finally {
        if (canvas) { canvas.width = 1; canvas.height = 1; }
        edits[i] = null;
        try { if (releasePage) await releasePage(prepared); } catch (error) { if (!failed) throw error; }
      }
      onProgress(i + 1, pages.length);
      await new Promise(resolve => setTimeout(resolve, 0));
    }
    return await (format === 'docx' ? buildDocx : buildPptx)(images, { Zip });
  } finally {
    images.length = 0;
  }
}
