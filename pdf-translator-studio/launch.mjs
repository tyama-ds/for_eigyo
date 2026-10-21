import { spawn } from 'node:child_process';
import { constants } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const projectDirectory = fileURLToPath(new URL('./', import.meta.url));

/** Load .env before the application's Node process starts, including extra CAs. */
export function startApplication({
  cwd = projectDirectory,
  entrypoint = path.join(projectDirectory, 'server.mjs'),
  environmentFile = path.join(projectDirectory, '.env'),
  stdio = 'inherit',
} = {}) {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 24 || (major === 24 && minor < 5)) {
    throw new Error('Node.js 24.5 以上が必要です。');
  }
  try { process.loadEnvFile(environmentFile); }
  catch (error) {
    if (error.code !== 'ENOENT') throw new Error('.env ファイルを読み込めませんでした。');
  }

  const child = spawn(process.execPath, [entrypoint], {
    cwd,
    env: { ...process.env },
    stdio,
    windowsHide: true,
  });
  let requestedSignal;
  const forward = signal => {
    requestedSignal = signal;
    if (!child.killed) child.kill(signal);
  };
  const onInterrupt = () => forward('SIGINT');
  const onTerminate = () => forward('SIGTERM');
  const cleanup = () => {
    process.off('SIGINT', onInterrupt);
    process.off('SIGTERM', onTerminate);
  };
  process.on('SIGINT', onInterrupt);
  process.on('SIGTERM', onTerminate);

  const exited = new Promise((resolve, reject) => {
    child.once('error', () => {
      cleanup();
      reject(new Error('PDF Lingua のサーバーを起動できませんでした。'));
    });
    child.once('exit', (code, signal) => {
      cleanup();
      const terminalSignal = signal || requestedSignal;
      resolve(code ?? (terminalSignal ? 128 + (constants.signals[terminalSignal] || 1) : 1));
    });
  });
  return { child, exited };
}

const isMain = process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;
if (isMain) {
  try {
    const { exited } = startApplication();
    process.exitCode = await exited;
  } catch (error) {
    // Only launcher-owned, fixed messages are emitted; no configuration contents.
    console.error(error.message);
    process.exitCode = 1;
  }
}
