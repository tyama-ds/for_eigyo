"""Reopen exported PDF/DOCX/PPTX and compare their actual rendered pages.

Native Word and PowerPoint conversion is intentionally separate from the app's
OOXML implementation. PDFium supplies an independent PDF renderer. Canonical
page images must come from the application's shared export rendering pipeline.
Run with the bundled Python runtime; requires Microsoft Office on Windows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from collections import Counter

import numpy as np
from PIL import Image, ImageFilter, ImageDraw
import pypdfium2 as pdfium


def bbox(mask):
    y, x = np.nonzero(mask)
    if not x.size:
        return None
    return [int(x.min()), int(y.min()), int(x.max()), int(y.max())]


def compare_images(expected, actual, registration_marks=()):
    exp = np.asarray(expected.convert('RGB'), dtype=np.int16)
    got = np.asarray(actual.convert('RGB'), dtype=np.int16)
    delta = np.abs(exp - got)
    exp_blur = np.asarray(expected.convert('RGB').filter(ImageFilter.GaussianBlur(0.6)), dtype=np.int16)
    got_blur = np.asarray(actual.convert('RGB').filter(ImageFilter.GaussianBlur(0.6)), dtype=np.int16)
    exp_foreground = np.min(exp, axis=2) < 230
    got_foreground = np.min(got, axis=2) < 230
    exp_bbox, got_bbox = bbox(exp_foreground), bbox(got_foreground)
    bounds_shift = (max(abs(a-b) for a, b in zip(exp_bbox, got_bbox))
                    if exp_bbox and got_bbox else (0 if exp_bbox == got_bbox else 1e9))
    landmarks = []
    for mark in registration_marks:
        # Evaluate a known solid registration rectangle locally. Global hue
        # thresholds on antialiased glyph edges are not position landmarks.
        left, top, right, bottom = mark['pixelRect']
        left, top = max(0, int(left)-10), max(0, int(top)-10)
        right, bottom = min(expected.width, int(right)+11), min(expected.height, int(bottom)+11)
        color = np.asarray(mark['rgb'], dtype=np.int16)
        exp_mask = np.max(np.abs(exp[top:bottom, left:right] - color), axis=2) <= 24
        got_mask = np.max(np.abs(got[top:bottom, left:right] - color), axis=2) <= 24
        a, b = bbox(exp_mask), bbox(got_mask)
        shift = max(abs(x-y) for x, y in zip(a, b)) if a and b else 1e9
        ey, ex = np.nonzero(exp_mask)
        gy, gx = np.nonzero(got_mask)
        centroid_shift = (max(abs(float(ex.mean()-gx.mean())), abs(float(ey.mean()-gy.mean())))
                          if gx.size else 1e9)
        landmarks.append({'name': mark['name'], 'roi': [left,top,right,bottom], 'rgb': mark['rgb'],
                          'expectedBounds': a, 'actualBounds': b,
                          'maxBoundsShiftPx': shift, 'centroidShiftPx': round(centroid_shift, 4),
                          'coverageRatio': round(float(got_mask.sum()/max(1,exp_mask.sum())), 6)})
    expected_ink = 255-np.min(exp, axis=2)
    actual_ink = 255-np.min(got, axis=2)
    metrics = {
        'meanAbsoluteChannelError': round(float(delta.mean()), 6),
        'blurredMeanAbsoluteChannelError': round(float(np.abs(exp_blur-got_blur).mean()), 6),
        'pixelsOver48Fraction': round(float((delta.max(axis=2) > 48).mean()), 6),
        'expectedForegroundBounds': exp_bbox,
        'actualForegroundBounds': got_bbox,
        'maxForegroundBoundsShiftPx': bounds_shift,
        'foregroundCoverageRatio': round(float(got_foreground.sum()/max(1, exp_foreground.sum())), 6),
        'weightedInkCoverageRatio': round(float(actual_ink.sum()/max(1,expected_ink.sum())),6),
        'colorLandmarks': landmarks,
    }
    errors = []
    # No translation or alignment is applied before comparison: displacement
    # remains visible to these gates. Small antialiasing differences are allowed.
    if metrics['meanAbsoluteChannelError'] > 4:
        errors.append('mean absolute channel error exceeds 4/255')
    if metrics['blurredMeanAbsoluteChannelError'] > 2:
        errors.append('blurred mean absolute channel error exceeds 2/255')
    if metrics['pixelsOver48Fraction'] > .02:
        errors.append('more than 2% of pixels differ by over 48/255')
    if bounds_shift > 2:
        errors.append('foreground boundary moved by more than 2 pixels')
    if not .95 <= metrics['weightedInkCoverageRatio'] <= 1.05:
        errors.append('weighted ink coverage changed by more than 5%')
    for item in landmarks:
        if item['maxBoundsShiftPx'] > 2:
            errors.append(f"{item['name']} landmark boundary moved by more than 2 pixels")
        if item['centroidShiftPx'] > 2:
            errors.append(f"{item['name']} landmark centroid moved by more than 2 pixels")
        if not .9 <= item['coverageRatio'] <= 1.1:
            errors.append(f"{item['name']} solid landmark area changed by more than 10%")
    return metrics, errors


def comparator_controls():
    """Check that QA accepts identical pixels and rejects known layout defects."""
    pixels = np.full((200, 160, 3), 255, dtype=np.uint8)
    for left, top, color in [(3, 3, (210, 30, 30)), (145, 3, (20, 150, 40)),
                             (3, 185, (20, 40, 210)), (145, 185, (200, 20, 180))]:
        pixels[top:top+12, left:left+12] = color
    pixels[50:150:7, 25:135] = 35
    expected = Image.fromarray(pixels)
    shifted = Image.new('RGB', expected.size, 'white')
    shifted.paste(expected, (6, 6))
    clipped = expected.copy()
    clipped.paste('white', (0, 175, 160, 200))
    stretched = expected.resize((145, 200), Image.Resampling.BILINEAR)
    stretched_canvas = Image.new('RGB', expected.size, 'white')
    stretched_canvas.paste(stretched, (0, 0))
    moved_marker = expected.copy()
    moved_marker.paste('white', (3,3,15,15))
    moved_marker.paste((210,30,30), (9,3,21,15))
    marks = [{'name': 'red-registration', 'pixelRect': [3,3,15,15], 'rgb': [210,30,30]}]
    variants = [('identical', expected, True), ('six-pixel-shift', shifted, False),
                ('blank-page', Image.new('RGB', expected.size, 'white'), False),
                ('bottom-clipping', clipped, False), ('aspect-distortion', stretched_canvas, False),
                ('single-registration-shift', moved_marker, False)]
    result = []
    for name, actual, should_pass in variants:
        _, errors = compare_images(expected, actual, marks)
        result.append({'name': name, 'expectedAccepted': should_pass,
                       'actualAccepted': not errors, 'status': 'passed' if (not errors)==should_pass else 'failed'})
    return result


def inspect_pdf(pdf_path, pages, canonical_paths, output_dir, expected_dimensions, is_slide=False):
    document = pdfium.PdfDocument(str(pdf_path))
    result = {'pdf': str(pdf_path), 'pageCount': len(document), 'expectedPageCount': len(pages), 'pages': [], 'errors': []}
    if len(document) != len(pages):
        result['errors'].append(f'page count {len(document)} differs from expected {len(pages)}')
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for i in range(min(len(document), len(pages))):
            page = document[i]
            expected = Image.open(canonical_paths[i]).convert('RGB')
            width_pt, height_pt = page.get_size()
            expected_width, expected_height = expected_dimensions[i]
            scale = expected.width / expected_width
            bitmap = page.render(scale=scale)
            actual = bitmap.to_pil().convert('RGB')
            actual_path = output_dir / f'page-{i+1}.png'
            actual.save(actual_path)
            entry = {'page': i+1, 'widthPt': width_pt, 'heightPt': height_pt,
                     'expectedWidthPt': expected_width, 'expectedHeightPt': expected_height,
                     'actualPixels': list(actual.size), 'expectedPixels': list(expected.size),
                     'renderedPng': str(actual_path), 'errors': []}
            if abs(width_pt-expected_width) > .1 or abs(height_pt-expected_height) > .1:
                entry['errors'].append('physical page dimensions differ by more than 0.1 point')
            if actual.size != expected.size:
                if max(abs(actual.width-expected.width), abs(actual.height-expected.height)) <= 1:
                    # PDF rasterizers round the final fractional pixel upward.
                    # A 1-pixel edge can be trimmed; never rescale or align content.
                    padded = Image.new('RGB', expected.size, 'white')
                    padded.paste(actual, (0, 0))
                    actual = padded
                    entry['roundingEdgeAdjusted'] = True
                else:
                    entry['errors'].append('raster dimensions differ by more than 1 pixel')
            if actual.size == expected.size:
                marks = []
                for mark in pages[i].get('registrationMarks', []):
                    x = mark['x'] + ((expected_width-pages[i]['widthPt'])/2 if is_slide else 0)
                    y = mark['y'] + ((expected_height-pages[i]['heightPt'])/2 if is_slide else 0)
                    # Use canonical scale on each axis: the fixture canvas has
                    # integer pixels while PDF dimensions can be fractional.
                    sx, sy = expected.width/expected_width, expected.height/expected_height
                    marks.append({'name': mark['name'], 'rgb': mark['rgb'],
                                  'pixelRect': [x*sx,y*sy,(x+mark['width'])*sx,(y+mark['height'])*sy]})
                metrics, errors = compare_images(expected, actual, marks)
                entry.update(metrics)
                entry['errors'].extend(errors)
                diff = np.abs(np.asarray(expected, dtype=np.int16)-np.asarray(actual, dtype=np.int16))
                Image.fromarray(np.minimum(diff*4, 255).astype(np.uint8)).save(output_dir/f'diff-{i+1}.png')
            entry['status'] = 'passed' if not entry['errors'] else 'failed'
            result['pages'].append(entry)
            bitmap.close()
            page.close()
    finally:
        document.close()
    result['status'] = 'passed' if not result['errors'] and all(x['status']=='passed' for x in result['pages']) else 'failed'
    return result


def make_contact_sheets(fixtures, output, results):
    paths = []
    for case in sorted({result['case'] for result in results}):
        metadata = json.loads((fixtures/case/'metadata.json').read_text(encoding='utf-8-sig'))
        rows = len(metadata['pages'])
        sheet = Image.new('RGB', (1320, rows*440+45), '#e9edf0')
        draw = ImageDraw.Draw(sheet)
        draw.text((12,12), f'{case} | canonical / PDF / Word 16 / PowerPoint 16', fill='black')
        for i in range(rows):
            sources = [fixtures/case/f'canonical-page-{i+1}.png']
            sources += [output/case/'export-1'/fmt/f'page-{i+1}.png' for fmt in ('pdf','docx','pptx')]
            for col, path in enumerate(sources):
                if not path.is_file():
                    continue
                preview = Image.open(path).convert('RGB')
                preview.thumbnail((310, 405), Image.Resampling.LANCZOS)
                x, y = col*330+(330-preview.width)//2, 45+i*440
                sheet.paste(preview, (x,y))
                draw.text((col*330+12, y+410), f'Page {i+1} | '+['canonical','PDF','Word','PowerPoint'][col], fill='black')
        path = output/f'contact-{case}.png'
        sheet.save(path)
        paths.append(str(path))
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=Path('tmp/office-qa/fixtures'))
    parser.add_argument('--output', type=Path, default=Path('tmp/office-qa/roundtrip'))
    parser.add_argument('--skip-convert', action='store_true')
    parser.add_argument('--powershell-executable', help='PowerShell 7 executable; defaults to pwsh on PATH')
    args = parser.parse_args()
    controls = comparator_controls()
    if any(control['status'] != 'passed' for control in controls):
        raise RuntimeError(f'Image comparison self-check failed: {controls}')
    fixtures, output = args.fixtures.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    conversion_exit_code = None
    if not args.skip_convert:
        powershell = args.powershell_executable or shutil.which('pwsh') or shutil.which('powershell')
        if not powershell:
            raise RuntimeError('PowerShell is required for native Office rendering.')
        proc = subprocess.run([powershell, '-NoProfile', '-MTA', '-ExecutionPolicy', 'Bypass', '-File',
                               str(Path(__file__).with_suffix('.ps1').resolve()),
                               '-FixtureRoot', str(fixtures), '-OutputRoot', str(output)],
                              capture_output=True, text=True)
        (output/'office-conversion.log').write_text(proc.stdout+'\n'+proc.stderr, encoding='utf-8')
        print(proc.stdout, end='')
        conversion_exit_code = proc.returncode
        (output/'office-conversion-exit.json').write_text(json.dumps({'exitCode': proc.returncode, 'powershell': powershell}), encoding='utf-8')
        if proc.returncode:
            print(proc.stderr, file=sys.stderr)
    elif (output/'office-conversion-exit.json').exists():
        conversion_exit_code = json.loads((output/'office-conversion-exit.json').read_text())['exitCode']
    records_path = output/'office-conversion.json'
    conversions = json.loads(records_path.read_text(encoding='utf-8-sig')) if records_path.exists() else []
    results = []
    expected_results = []
    completeness_errors = []
    for metadata_path in sorted(fixtures.glob('*/metadata.json')):
        case_dir = metadata_path.parent
        metadata = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
        declared_exports = metadata.get('exports', [])
        if not declared_exports:
            completeness_errors.append(f'{case_dir.name}: metadata has no declared exports')
        for declared in declared_exports:
            expected_file = case_dir / declared['file']
            expected_results.append((case_dir.name, expected_file.stem, declared['format']))
            if not expected_file.is_file():
                completeness_errors.append(f'missing fixture: {expected_file}')
        pages = metadata['pages']
        if any(not page.get('registrationMarks') for page in pages):
            completeness_errors.append(f'{case_dir.name}: missing explicit solid registration regions')
        dims = [(p['widthPt'], p['heightPt']) for p in pages]
        canonical_pages = [case_dir/f'canonical-page-{i+1}.png' for i in range(len(pages))]
        canonical_slides = [case_dir/f'canonical-slide-{i+1}.png' for i in range(len(pages))]
        slide = metadata.get('slide', metadata.get('slideSize', {}))
        slide_dims = [(slide.get('widthPt', max(p['widthPt'] for p in pages)),
                       slide.get('heightPt', max(p['heightPt'] for p in pages)))]*len(pages)
        for source_pdf in sorted(case_dir.glob('export-*.pdf')):
            target = output/case_dir.name/source_pdf.stem/'pdf'
            item = inspect_pdf(source_pdf, pages, canonical_pages, target, dims)
            item.update({'case': case_dir.name, 'run': source_pdf.stem, 'format': 'pdf', 'renderer': 'PDFium'})
            results.append(item)
        for conversion in conversions:
            if conversion['case'] != case_dir.name:
                continue
            item = {**conversion, 'run': Path(conversion['source']).stem}
            if conversion['status'] == 'passed':
                is_ppt = conversion['format'] == 'pptx'
                pdf_path = Path(conversion['pdf'])
                item.update(inspect_pdf(pdf_path, pages, canonical_slides if is_ppt else canonical_pages,
                                        pdf_path.parent, slide_dims if is_ppt else dims, is_slide=is_ppt))
            results.append(item)
    actual_results = Counter((result['case'], result['run'], result['format']) for result in results)
    expected_counts = Counter(expected_results)
    if expected_counts != actual_results:
        for key in sorted(set(expected_counts) | set(actual_results)):
            if expected_counts[key] != actual_results[key]:
                completeness_errors.append(f'{key}: expected {expected_counts[key]} result(s), found {actual_results[key]}')
    expected_office = sum(format_name in ('docx','pptx') for _, _, format_name in expected_results)
    if len(conversions) != expected_office:
        completeness_errors.append(f'expected {expected_office} native Office conversions, found {len(conversions)}')
    if conversion_exit_code not in (None, 0):
        completeness_errors.append(f'native Office conversion process exited with {conversion_exit_code}')
    page_results = [page for result in results for page in result.get('pages', [])]
    all_marks = [mark for page in page_results for mark in page.get('colorLandmarks', [])]
    repeated_renders = {}
    for result in results:
        for page in result.get('pages', []):
            key = (result['case'], result['format'], page['page'])
            repeated_renders.setdefault(key, []).append(hashlib.sha256(Path(page['renderedPng']).read_bytes()).hexdigest())
    repetition_checks = [{'case': key[0], 'format': key[1], 'page': key[2], 'repetitions': len(hashes),
                          'identicalRenderedPixels': len(set(hashes)) == 1}
                         for key, hashes in sorted(repeated_renders.items())]
    for check in repetition_checks:
        if check['repetitions'] != 3 or not check['identicalRenderedPixels']:
            completeness_errors.append(f"render repeatability failed: {check}")
    summary = {
        'status': 'passed' if results and not completeness_errors and all(x['status']=='passed' for x in results) else 'failed',
        'completenessErrors': completeness_errors,
        'expectedDocumentChecks': len(expected_results),
        'expectedOfficeConversions': expected_office,
        'conversionExitCode': conversion_exit_code,
        'documentChecks': len(results), 'pageChecks': len(page_results),
        'passedDocuments': sum(x['status']=='passed' for x in results),
        'passedPages': sum(x['status']=='passed' for x in page_results),
        'maxMeanAbsoluteChannelError': max((x.get('meanAbsoluteChannelError',0) for x in page_results), default=0),
        'maxForegroundBoundsShiftPx': max((x.get('maxForegroundBoundsShiftPx',0) for x in page_results), default=0),
        'registrationChecks': len(all_marks),
        'maxRegistrationBoundsShiftPx': max((x['maxBoundsShiftPx'] for x in all_marks), default=0),
        'maxRegistrationCentroidShiftPx': max((x['centroidShiftPx'] for x in all_marks), default=0),
        'cases': sorted({x['case'] for x in results}),
        'nativeOfficeConversions': len(conversions),
        'repeatabilityChecks': len(repetition_checks),
        'identicalThreeRunRenders': sum(item['repetitions']==3 and item['identicalRenderedPixels'] for item in repetition_checks),
        'comparison': 'Microsoft Office read-only import + native PDF export, independent PDFium rasterization, no image alignment',
    }
    report = {'summary': summary, 'comparatorControls': controls, 'repeatability': repetition_checks,
              'criteria': {'pageDimensionTolerancePt': .1, 'rasterRoundingTolerancePx': 1,
                           'meanAbsoluteChannelErrorMaximum': 4, 'blurredChannelErrorMaximum': 2,
                           'pixelsDifferingOver48MaximumFraction': .02,
                           'foregroundBoundsTolerancePx': 2, 'weightedInkCoverageToleranceFraction': .05,
                           'registrationBoundsAndCentroidTolerancePx': 2,
                           'registrationAreaToleranceFraction': .1,
                           'notes': 'Registration checks use named solid regions from fixture geometry; binary foreground coverage is diagnostic only because antialiasing changes thresholded edge-pixel counts.'},
              'contactSheets': make_contact_sheets(fixtures, output, results), 'results': results}
    (output/'comparison-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    for result in results:
        if result['status'] != 'passed':
            print('FAILED', result['case'], result.get('run'), result['format'], result.get('error', result.get('errors', [])))
            for page in result.get('pages', []):
                if page['status'] != 'passed':
                    print('  page', page['page'], page['errors'])
    return 0 if summary['status']=='passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
