import { cp, rm, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';

const root = process.cwd();

function canRun(cmd, args) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: 'ignore', shell: process.platform === 'win32' });
    child.on('exit', (code) => resolve(code === 0));
    child.on('error', () => resolve(false));
  });
}

function run(cmd, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: 'inherit', shell: process.platform === 'win32' });
    child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`${cmd} exited with ${code}`))));
  });
}

async function main() {
  await rm(path.join(root, '.next'), { recursive: true, force: true });

  if (await canRun('npx', ['--version'])) {
    await run('npx', ['next', 'build']);
  } else if (await canRun('bunx', ['--version'])) {
    await run('bunx', ['next', 'build']);
  } else {
    throw new Error('No se encontró npx ni bunx para ejecutar Next.js build');
  }

  const standaloneRoot = path.join(root, '.next', 'standalone');
  const standaloneStatic = path.join(standaloneRoot, '.next', 'static');
  await mkdir(path.dirname(standaloneStatic), { recursive: true });

  if (existsSync(path.join(root, '.next', 'static'))) {
    await cp(path.join(root, '.next', 'static'), standaloneStatic, { recursive: true, force: true });
  }
  if (existsSync(path.join(root, 'public'))) {
    await cp(path.join(root, 'public'), path.join(standaloneRoot, 'public'), { recursive: true, force: true });
  }

  console.log('[build] Standalone bundle preparado.');
}

main().catch((err) => {
  console.error('[build] Failed:', err.message);
  process.exit(1);
});
