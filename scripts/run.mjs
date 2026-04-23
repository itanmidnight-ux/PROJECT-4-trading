import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

const root = process.cwd();
const logFile = path.join(root, 'logs', 'web.log');
const pidFile = path.join(root, '.web.pid');

async function waitFor(url, timeoutSec = 60) {
  const started = Date.now();
  while (Date.now() - started < timeoutSec * 1000) {
    try {
      const res = await fetch(url);
      if (res.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

async function startServer() {
  const serverJs = path.join(root, '.next', 'standalone', 'server.js');
  if (!existsSync(serverJs)) {
    throw new Error('Build no encontrado. Ejecuta primero node scripts/install.mjs');
  }

  await mkdir(path.join(root, 'logs'), { recursive: true });

  const out = await import('node:fs').then((fs) => fs.createWriteStream(logFile, { flags: 'a' }));
  const child = spawn(process.execPath, [serverJs], {
    cwd: root,
    env: { ...process.env, NODE_ENV: 'production', PORT: '3000' },
    detached: process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  child.stdout.pipe(out);
  child.stderr.pipe(out);

  await writeFile(pidFile, String(child.pid), 'utf8');

  if (process.platform !== 'win32') {
    child.unref();
  }

  const ready = await waitFor('http://127.0.0.1:3000/api/config/settings', 60);
  if (!ready) {
    const log = existsSync(logFile) ? await readFile(logFile, 'utf8') : '';
    throw new Error(`Dashboard no listo en puerto 3000.\n${log.slice(-2500)}`);
  }

  console.log('[ok] Dashboard listo: http://localhost:3000');
}

startServer().catch((err) => {
  console.error('[run] Error:', err.message);
  process.exit(1);
});
