import http from 'node:http';
import https from 'node:https';

export function publicError(message, status = 502) {
  return Object.assign(new Error(message), { status, public: true });
}

function classifiedError(message, status, code) {
  return Object.assign(publicError(message, status), { code });
}

// Inspect known error fields only to select fixed messages. Never return upstream
// text, URLs, model names, parameter values, credentials, or arbitrary error codes.
function upstreamError(status, body) {
  let parsed;
  try { parsed = JSON.parse(body); } catch { /* Proxy HTML is not diagnostic data. */ }
  const detail = parsed?.error && typeof parsed.error === 'object' ? parsed.error : parsed;
  const field = value => typeof value === 'string' ? value.slice(0, 8192).toLowerCase() : '';
  const code = field(detail?.code || detail?.type);
  const param = field(detail?.param);
  const message = field(typeof parsed?.error === 'string' ? parsed.error : detail?.message);
  let hint; let category;
  if (status === 407) {
    category = 'PROXY_AUTHENTICATION'; hint = 'プロキシ認証情報を確認してください。';
  } else if (status === 401 || ['invalid_api_key', 'authentication_error', 'unauthorized'].includes(code)) {
    category = 'API_AUTHENTICATION'; hint = 'APIキーが未設定・無効、または期限切れです。接続先の認証設定を確認してください。';
  } else if (['insufficient_quota', 'billing_hard_limit_reached', 'billing_not_active'].includes(code)) {
    category = 'API_QUOTA'; hint = 'APIの残高または利用可能額が不足しています。アカウントの課金・利用上限設定を確認してください。';
  } else if (status === 429 || ['rate_limit_exceeded', 'rate_limit_error', 'too_many_requests'].includes(code)) {
    category = 'API_RATE_LIMIT'; hint = 'APIの同時実行数または利用頻度の上限に達しました。時間を置いて再実行してください。';
  } else if (status === 403) {
    category = 'API_FORBIDDEN'; hint = 'APIまたはプロキシのアクセス権を確認してください。';
  } else if (['context_length_exceeded', 'context_window_exceeded', 'prompt_too_long', 'input_too_long'].includes(code)
      || (/(?:context[ _-]?(?:length|window)|n_ctx)/.test(message) && /(?:exceed|too (?:many|long|large)|maximum|limit|overflow|beyond|larger|greater|insufficient)/.test(message))) {
    category = 'CONTEXT_LIMIT'; hint = 'モデルのコンテキスト上限を超えています。一度に翻訳する領域・文字数、または出力トークン上限を減らしてください。';
  } else if (['invalid_max_tokens', 'max_tokens_exceeded', 'output_token_limit_exceeded'].includes(code)
      || (/^(?:max_tokens|max_completion_tokens|max_output_tokens)$/.test(param) && /(?:exceed|too (?:many|large)|maximum|limit|at most|less than|greater than|positive|between)/.test(message))) {
    category = 'TOKEN_LIMIT'; hint = '出力トークン上限の設定がモデルの対応範囲外です。LOCAL_LLM_MAX_TOKENS または OPENAI_MAX_OUTPUT_TOKENS を調整してください。';
  } else if (['model_not_loaded', 'no_model_loaded'].includes(code)
      || /(?:no models? (?:is |are )?loaded|models? (?:is |are )?not (?:currently )?loaded|load (?:a |the )?model (?:first|before))/.test(message)) {
    category = 'MODEL_NOT_LOADED'; hint = '接続先にモデルが読み込まれていません。LLMサーバーで使用するモデルをロードしてから再実行してください。';
  } else if (['model_not_found', 'model_does_not_exist', 'unknown_model', 'invalid_model'].includes(code)
      || /(?:model[^\n]{0,200}(?:not found|does not exist|doesn't exist|not exist)|unknown model|invalid model (?:id|name))/.test(message)) {
    category = 'MODEL_NOT_FOUND'; hint = '指定したモデルが接続先に見つかりません。接続先で利用可能なモデルIDを確認してください。';
  } else if (['response_format_not_supported', 'json_schema_not_supported'].includes(code)
      || (['unsupported_parameter', 'unknown_parameter', 'unrecognized_request_argument'].includes(code) && /^(?:response_format|text\.format)(?:\.|$)/.test(param))
      || (/(?:response_format|json_schema|structured outputs?)/.test(message) && /(?:not support|unsupported|unknown|unrecognized|not available)/.test(message))) {
    category = 'UNSUPPORTED_RESPONSE_FORMAT'; hint = '接続先またはモデルが指定されたJSON出力形式に対応していません。JSON出力方式・API形式・モデルの対応状況を確認してください。';
  } else {
    category = 'UPSTREAM_HTTP_ERROR';
    hint = ({ 400: 'モデルとAPI形式・推論設定を確認してください。', 404: '接続URLとモデル名を確認してください。', 413: '一度に翻訳する領域・文字数を減らしてください。' })[status] || '接続先の状態を確認してください。';
  }
  return classifiedError(`API接続エラー (${status})。${hint}`, status === 429 ? 429 : 502, category);
}

function networkError(error) {
  const codes = new Set(); const seen = new Set();
  const inspect = (value, depth = 0) => {
    if (!value || typeof value !== 'object' || seen.has(value) || depth > 3) return;
    seen.add(value);
    if (typeof value.code === 'string') codes.add(value.code);
    inspect(value.cause, depth + 1);
    if (Array.isArray(value.errors)) value.errors.slice(0, 8).forEach(item => inspect(item, depth + 1));
  };
  inspect(error);
  if ([...codes].some(code => /CERT|TLS|SSL|SELF_SIGNED|UNABLE_TO_VERIFY/.test(code))) {
    return classifiedError('TLS証明書を検証できません。社内CAを NODE_EXTRA_CA_CERTS に設定して再起動してください。', 502, 'TLS_CERTIFICATE');
  }
  if (codes.has('ENOTFOUND') || codes.has('EAI_AGAIN')) {
    return classifiedError('APIまたはプロキシのホスト名を解決できません。接続URL、DNS、社内ネットワークへの接続を確認してください。', 502, 'DNS_LOOKUP_FAILED');
  }
  if (codes.has('ECONNREFUSED')) {
    return classifiedError('APIまたはプロキシに接続を拒否されました。接続先のサーバーが起動しているか、ホスト名とポート番号を確認してください。', 502, 'CONNECTION_REFUSED');
  }
  if (codes.has('ECONNRESET') || codes.has('EPIPE')) {
    return classifiedError('APIまたはプロキシとの通信が途中で切断されました。LLMサーバーのログ・モデルの稼働状態とネットワークを確認してください。', 502, 'CONNECTION_RESET');
  }
  if (codes.has('ETIMEDOUT')) {
    return classifiedError('APIまたはプロキシへの接続がタイムアウトしました。接続先とネットワークを確認してください。', 504, 'CONNECTION_TIMEOUT');
  }
  return classifiedError('APIに接続できません。接続URL、プロキシ、認証設定を確認してください。', 502, 'CONNECTION_FAILED');
}

export function proxyEnvironment(env = process.env, useProxy = true) {
  if (!useProxy) return {};
  const proxy = {};
  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY']) {
    const raw = env[key.toLowerCase()] || env[key];
    if (!raw) continue;
    let url;
    try { url = new URL(raw); } catch { throw publicError(`${key} のURLが正しくありません。`, 400); }
    if (!['http:', 'https:'].includes(url.protocol)) throw publicError('プロキシは HTTP / HTTPS のURLで設定してください。', 400);
    if (env.PROXY_USERNAME) {
      url.username = env.PROXY_USERNAME;
      url.password = env.PROXY_PASSWORD || '';
    }
    proxy[key] = url.href;
  }
  proxy.NO_PROXY = env.no_proxy || env.NO_PROXY || '';
  return proxy;
}

// Native Agent proxy support includes CONNECT, Basic auth and NO_PROXY. TLS validation stays enabled.
export async function requestJson(url, body, { headers = {}, signal, env = process.env, useProxy = true, timeoutMs = 180000 } = {}) {
  const target = new URL(url);
  const transport = target.protocol === 'https:' ? https : http;
  const agent = new transport.Agent({ proxyEnv: proxyEnvironment(env, useProxy), keepAlive: false });
  const data = Buffer.from(JSON.stringify(body));
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  try {
    return await new Promise((resolve, reject) => {
      const req = transport.request(target, {
        method: 'POST', agent, signal: combined,
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'Content-Length': data.length, ...headers }
      }, res => {
        let size = 0; const chunks = [];
        res.on('data', chunk => {
          size += chunk.length;
          if (size > 4 * 1024 * 1024) res.destroy(publicError('APIの応答が大きすぎます。'));
          else chunks.push(chunk);
        });
        res.on('error', reject);
        res.on('end', () => {
          const status = res.statusCode;
          if (status < 200 || status >= 300) {
            reject(upstreamError(status, Buffer.concat(chunks).toString('utf8')));
            return;
          }
          try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
          catch { reject(publicError('APIからJSON形式の応答を受け取れませんでした。接続URLを確認してください。')); }
        });
      });
      req.on('error', reject);
      req.end(data);
    });
  } catch (error) {
    if (signal?.aborted) throw Object.assign(new Error('翻訳を中止しました。'), { name: 'AbortError', public: true, status: 499 });
    if (timeout.aborted) throw publicError('API接続がタイムアウトしました。REQUEST_TIMEOUT_MS または接続先を確認してください。', 504);
    if (error.public) throw error;
    throw networkError(error);
  } finally { agent.destroy(); }
}
