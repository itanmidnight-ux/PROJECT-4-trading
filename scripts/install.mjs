import { access, chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import crypto from 'node:crypto';

const root = process.cwd();
const envPath = path.join(root, '.env');
const dbDir = path.join(root, 'data');
const logsDir = path.join(root, 'logs');
const dbPath = path.join(dbDir, 'reco_trading.db');
const dbUrl = `file:${dbPath}`;

function hasBun() {
  return new Promise((resolve) => {
    const child = spawn('bun', ['--version'], { stdio: 'ignore', shell: process.platform === 'win32' });
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

function setEnv(content, key, value) {
  const re = new RegExp(`^${key}=.*$`, 'm');
  if (re.test(content)) return content.replace(re, `${key}=${value}`);
  return `${content.trimEnd()}\n${key}=${value}\n`;
}

async function ensureEnv() {
  const secret = crypto.randomBytes(32).toString('hex');
  const template = `DATABASE_URL=${dbUrl}\n\nBROKER_ACTIVE=weltrade_mt5\nTRADING_SYMBOL=XAU_USD\nPRIMARY_TIMEFRAME=5m\nCONFIRMATION_TIMEFRAME=15m\n\nWELTRADE_MT5_LOGIN=\nWELTRADE_MT5_PASSWORD=\nWELTRADE_MT5_SERVER=Weltrade-Live\nWELTRADE_MT5_DEMO_SERVER=MetaQuotes-Demo\nWELTRADE_MT5_IS_DEMO=true\nWELTRADE_MT5_BRIDGE_URL=http://127.0.0.1:5001\nWELTRADE_MT5_TIMEOUT_MS=10000\n\nOANDA_ACCOUNT_ID=\nOANDA_API_TOKEN=\nOANDA_IS_DEMO=true\n\nDASHBOARD_PORT=3000\nNEXTAUTH_SECRET=${secret}\nNEXTAUTH_URL=http://localhost:3000\n`;

  try {
    await access(envPath, constants.F_OK);
    let content = await readFile(envPath, 'utf8');
    content = setEnv(content, 'DATABASE_URL', dbUrl);
    content = setEnv(content, 'BROKER_ACTIVE', 'weltrade_mt5');
    content = setEnv(content, 'WELTRADE_MT5_DEMO_SERVER', 'MetaQuotes-Demo');
    content = setEnv(content, 'WELTRADE_MT5_BRIDGE_URL', 'http://127.0.0.1:5001');
    await writeFile(envPath, content, 'utf8');
  } catch {
    await writeFile(envPath, template, 'utf8');
  }

  if (process.platform !== 'win32') {
    await chmod(envPath, 0o600).catch(() => {});
  }
}

async function main() {
  await mkdir(dbDir, { recursive: true });
  await mkdir(logsDir, { recursive: true });
  await ensureEnv();

  const bunAvailable = await hasBun();
  if (bunAvailable) {
    await run('bun', ['install']);
    await run('bunx', ['prisma', 'generate']);
    await run('bunx', ['prisma', 'db', 'push', '--skip-generate']);
    await run('bun', ['run', 'build']);
  } else {
    await run('npm', ['install']);
    await run('npx', ['prisma', 'generate']);
    await run('npx', ['prisma', 'db', 'push', '--skip-generate']);
    await run('npm', ['run', 'build']);
  }

  console.log('\n[ok] Instalación lista.');
  console.log('[next] Ejecuta: node scripts/run.mjs');
}

main().catch((err) => {
  console.error('[install] Error:', err.message);
  process.exit(1);
});
