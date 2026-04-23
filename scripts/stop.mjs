import { existsSync } from 'node:fs';
import { readFile, rm } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import path from 'node:path';

const root = process.cwd();
const pidFile = path.join(root, '.web.pid');

function run(cmd, args) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: 'ignore', shell: process.platform === 'win32' });
    child.on('exit', (code) => resolve(code === 0));
    child.on('error', () => resolve(false));
  });
}

async function main() {
  if (!existsSync(pidFile)) {
    console.log('[stop] No hay PID registrado.');
    return;
  }

  const pid = (await readFile(pidFile, 'utf8')).trim();
  if (!pid) return;

  if (process.platform === 'win32') {
    await run('taskkill', ['/PID', pid, '/T', '/F']);
  } else {
    await run('kill', ['-TERM', pid]);
  }

  await rm(pidFile, { force: true });
  console.log(`[ok] Proceso ${pid} detenido.`);
}

main().catch((err) => {
  console.error('[stop] Error:', err.message);
  process.exit(1);
});
