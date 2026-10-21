# Third-party components

The browser assets are bundled locally; opening the application does not fetch scripts or fonts from a CDN.

| Component | Version | Source | License |
| --- | --- | --- | --- |
| PDF.js (`pdfjs-dist`) | 6.4.299 | https://github.com/mozilla/pdf.js | Apache-2.0; see `dist/vendor/LICENSE-pdfjs.txt` |
| jsPDF | 4.2.1 | https://github.com/parallax/jsPDF | MIT; see `dist/vendor/LICENSE-jspdf.txt` |
| JSZip | 3.10.1 | https://github.com/Stuk/jszip | MIT (dual licensed MIT/GPLv3); see `dist/vendor/LICENSE-jszip.txt` |

PDF.js CMaps, standard fonts and WebAssembly codecs are included under `dist/vendor/`; their distribution licenses are retained in those directories.

### QuickJS sandbox notices

The `pdfjs-dist` 6.4.299 package includes `quickjs-eval.js` and `quickjs-eval.wasm` but omits their MIT license notices. This distribution adds the two notices from the [official Mozilla fix](https://github.com/mozilla/pdf.js/commit/a3beacc1a35f2d285f0505c5489d8cb4a9ebcc89), without changing the sandbox assets:

- `dist/vendor/wasm/LICENSE_QUICKJS`: [QuickJS notice at the pinned upstream commit](https://raw.githubusercontent.com/mozilla/pdf.js/a3beacc1a35f2d285f0505c5489d8cb4a9ebcc89/external/quickjs/LICENSE_QUICKJS).
- `dist/vendor/wasm/LICENSE_PDFJS_QUICKJS`: [pdf.js.quickjs notice at the pinned upstream commit](https://raw.githubusercontent.com/mozilla/pdf.js/a3beacc1a35f2d285f0505c5489d8cb4a9ebcc89/external/quickjs/LICENSE_PDFJS_QUICKJS).

## Implementation references

- PDF coordinates and rendering: https://mozilla.github.io/pdf.js/api/draft/api.js.html
- OpenAI Chat Completions request fields: https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- OpenAI Responses request and output format: https://developers.openai.com/api/docs/guides/migrate-to-responses
- Reasoning models and incomplete output: https://developers.openai.com/api/docs/guides/reasoning
- Model parameter differences: https://developers.openai.com/api/docs/guides/latest-model
- Node.js forward proxy handling: https://nodejs.org/docs/latest-v24.x/api/http.html#built-in-proxy-support
- Additional CA certificates: https://nodejs.org/api/cli.html#node_extra_ca_certsfile
- Word page and section geometry: https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.pagesize
- Word maximum page size: https://learn.microsoft.com/en-us/previous-versions/troubleshoot/microsoft-365/microsoft-365-apps/word/operating-parameter-limitation
- PowerPoint common slide size and limits: https://support.microsoft.com/en-us/powerpoint/change-the-size-of-your-powerpoint-slides

Model API selection is an application policy and can be overridden. Supported models and account availability are determined by the configured provider.
