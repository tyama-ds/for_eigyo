import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import {
  CompletionHandler,
  ICompletionProviderManager,
  IInlineCompletionContext,
  IInlineCompletionItem,
  IInlineCompletionList,
  IInlineCompletionProvider
} from '@jupyterlab/completer';
import { KernelMessage } from '@jupyterlab/services';

/** カーネルへ安全に文字列を渡すための UTF-8 対応 base64。 */
function toBase64Utf8(s: string): string {
  return btoa(unescape(encodeURIComponent(s)));
}

const MARKER = '<<LLMLAB>>';
const TIMEOUT_MS = 8000;

/**
 * インライン補完プロバイダ。
 * 補完要求のたびにアクティブなカーネルへ silent execute を投げ、
 * カーネル内で設定済みの `llmlab.inline_complete(prefix, suffix)` を呼ぶ。
 * これによりノートブックで `llmlab.configure(...)` した接続設定をそのまま使える。
 */
class LLMLabInlineProvider implements IInlineCompletionProvider {
  readonly identifier = 'llmlab-completer';
  readonly name = 'llmlab (local LLM)';

  // 直近の実行 future。新しい補完要求が来たら前のものを破棄し、
  // カーネルのキューに補完リクエストが滞留しないようにする。
  private _pending: { dispose(): void; isDisposed: boolean } | null = null;

  async fetch(
    request: CompletionHandler.IRequest,
    context: IInlineCompletionContext
  ): Promise<IInlineCompletionList<IInlineCompletionItem>> {
    const empty: IInlineCompletionList<IInlineCompletionItem> = { items: [] };

    const text = request.text || '';
    const offset = request.offset ?? text.length;
    const prefix = text.slice(0, offset);
    const suffix = text.slice(offset);

    if (!prefix.trim()) {
      return empty;
    }
    const kernel = context.session?.kernel;
    if (!kernel) {
      return empty;
    }

    const completion = await this._callKernel(kernel, prefix, suffix);
    if (!completion) {
      return empty;
    }
    return { items: [{ insertText: completion }] };
  }

  private _callKernel(
    kernel: NonNullable<IInlineCompletionContext['session']>['kernel'],
    prefix: string,
    suffix: string
  ): Promise<string> {
    const encP = toBase64Utf8(prefix);
    const encS = toBase64Utf8(suffix);
    // 1 行に収め、インデント由来のエラーを避ける。失敗時も llmlab.inline_complete が
    // 空文字を返すため、ここでは例外を意識しなくてよい。
    const code =
      'print("' +
      MARKER +
      '"+__import__("json").dumps({"c":__import__("llmlab").inline_complete(' +
      '__import__("base64").b64decode("' +
      encP +
      '").decode("utf-8"),__import__("base64").b64decode("' +
      encS +
      '").decode("utf-8"))}))';

    return new Promise<string>(resolve => {
      let out = '';
      let settled = false;
      const finish = (value: string) => {
        if (!settled) {
          settled = true;
          resolve(value);
        }
      };

      if (!kernel) {
        return finish('');
      }

      // 前回の補完リクエストが残っていれば破棄（カーネルキューへの滞留・多重実行防止）
      if (this._pending && !this._pending.isDisposed) {
        try {
          this._pending.dispose();
        } catch {
          /* noop */
        }
      }

      let future;
      try {
        future = kernel.requestExecute({
          code,
          silent: true,
          // stop_on_error は false: 補完実行の失敗（llmlab 未 import 等）で
          // ユーザーがキューに積んだセル実行を abort させない
          stop_on_error: false,
          store_history: false,
          allow_stdin: false
        });
      } catch {
        return finish('');
      }
      this._pending = future;

      future.onIOPub = (msg: KernelMessage.IIOPubMessage) => {
        if (msg.header.msg_type === 'stream') {
          const content = msg.content as KernelMessage.IStreamMsg['content'];
          if (content.name === 'stdout') {
            out += content.text;
          }
        }
      };

      future.done
        .then(() => {
          const i = out.indexOf(MARKER);
          if (i < 0) {
            return finish('');
          }
          try {
            const obj = JSON.parse(out.slice(i + MARKER.length));
            finish(typeof obj.c === 'string' ? obj.c : '');
          } catch {
            finish('');
          }
        })
        .catch(() => finish(''));

      // 遅いカーネルでも UI を固めないよう上限を設け、future も破棄して滞留を防ぐ
      setTimeout(() => {
        if (!settled) {
          try {
            future.dispose();
          } catch {
            /* noop */
          }
        }
        finish('');
      }, TIMEOUT_MS);
    });
  }
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'jupyterlab-llmlab-completer:plugin',
  description:
    'Inline (ghost-text) completion via a local OpenAI-compatible LLM, routed through the active kernel llmlab config.',
  autoStart: true,
  requires: [ICompletionProviderManager],
  activate: (_app: JupyterFrontEnd, manager: ICompletionProviderManager) => {
    manager.registerInlineProvider(new LLMLabInlineProvider());
    // eslint-disable-next-line no-console
    console.log('jupyterlab-llmlab-completer: inline provider registered');
  }
};

export default plugin;
