/** Browser -> real Supervisor HTTP API -> isolated Git worktree regression. */
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const web = path.join(root, 'web');
const require = createRequire(path.join(web, 'package.json'));
const { createServer } = await import(require.resolve('vite'));
const playwright = await import(process.env.PLAYWRIGHT_MODULE_PATH || require.resolve('playwright'));
const { chromium } = playwright.default || playwright;
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'conveyor-supervisor-ui-'));
const repo = path.join(temp, 'repo'); fs.mkdirSync(repo);
const git = (...args) => execFileSync('git', ['-C', repo, ...args], { stdio: 'ignore' });
git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.com');
fs.mkdirSync(path.join(repo, 'tests'));
fs.writeFileSync(path.join(repo, 'tests', 'test_result.py'), `import unittest\nfrom pathlib import Path\nclass ResultTest(unittest.TestCase):\n    def test_result(self):\n        self.assertEqual(Path('result.txt').read_text(), 'done')\n`);
git('add', '.'); git('commit', '-m', 'initial');
const executor = path.join(temp, 'executor');
fs.writeFileSync(executor, `#!/usr/bin/env python3
import json, pathlib, sys, time
prompt = sys.stdin.read()
if 'hold execution' in prompt:
    time.sleep(60)
if 'You are the implementer' in prompt:
    pathlib.Path('result.txt').write_text('done')
print(json.dumps({'result': 'PASS' if 'independent reviewer' in prompt else 'done', 'is_error': False}))
`);
fs.chmodSync(executor, 0o755);
const python = path.join(root, '.venv', 'bin', 'python');
const code = `
import asyncio, os, threading
from pathlib import Path
from types import SimpleNamespace
from web_console import WebConsoleHandler, WebConsoleServer
root=Path(os.environ['FIXTURE_ROOT'])
settings=SimpleNamespace(codex_memory_root=root/'memory',codex_task_root=root/'tasks',codex_model=None,agent_supervisor_enabled=True)
loop=asyncio.new_event_loop()
threading.Thread(target=loop.run_forever,daemon=True).start()
server=WebConsoleServer(('127.0.0.1',0),WebConsoleHandler,control=SimpleNamespace(settings=settings),loop=loop,token='test-token')
print(server.server_port,flush=True)
server.serve_forever()
`;
const backend = spawn(python, ['-u', '-c', code], { cwd: root, env: { ...process.env, FIXTURE_ROOT: temp, CONVEYOR_CLAUDE_BIN: executor }, stdio: ['ignore', 'pipe', 'pipe'] });
let backendErrors = '';
backend.stderr.on('data', chunk => { backendErrors += chunk.toString(); });
const lines = readline.createInterface({ input: backend.stdout });
const backendPort = Number(await new Promise((resolve, reject) => {
  lines.once('line', resolve); backend.once('exit', code => reject(new Error(`fixture exited ${code}: ${backendErrors}`)));
}));
const payload = data => JSON.stringify(data);
const server = await createServer({
  root: web, configFile: false, server: { host: '127.0.0.1', port: 0 },
  plugins: [{ name: 'supervisor-fixture', configureServer(vite) {
    vite.middlewares.use((req, res, next) => {
      if (req.url?.startsWith('/api/supervisor/')) {
        const upstream = http.request({ host: '127.0.0.1', port: backendPort, path: req.url, method: req.method, headers: req.headers }, reply => {
          res.writeHead(reply.statusCode, reply.headers); reply.pipe(res);
        });
        upstream.on('error', error => { res.statusCode = 502; res.end(payload({ error: error.message })); });
        req.pipe(upstream); return;
      }
      if (!req.url?.startsWith('/api/')) return next();
      let data = {};
      if (req.url === '/api/sessions') data = { sessions: [] };
      else if (req.url === '/api/jobs') data = { jobs: [] };
      else if (req.url === '/api/approvals') data = { approvals: [] };
      else if (req.url === '/api/nodes') data = { nodes: [] };
      else if (req.url === '/api/agents') data = { enabled: false, agents: [] };
      else if (req.url === '/api/workspaces/local') data = { workspaces: [{ path: repo, name: 'repo', workspace_status: 'ok' }], default_workspace: repo };
      else if (req.url === '/api/computer/status') data = { armed: false, arm_remaining_seconds: 0, screenshots: [] };
      else if (req.url === '/api/system/status') data = { uptime_seconds: 1, load_average: [0], cpu_count: 1, memory: { total: null, available: null }, disk: { total: 1, used: 0, free: 1 }, queue: { depth: 0, paused: false, states: {} }, channels: { telegram: { configured: false }, feishu: { configured: false } }, nodes: [], features: { agent_supervisor: true, teammate: false } };
      else if (req.url.startsWith('/api/chat/history')) data = { messages: [] };
      res.setHeader('Content-Type', 'application/json'); res.end(payload(data));
    });
  }}],
});
let browser;
try {
  await server.listen();
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 850 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => sessionStorage.setItem('conveyor-token', 'test-token'));
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}`);
  await page.getByRole('button', { name: '自动任务', exact: true }).click();
  await page.getByLabel('想完成什么').fill('add result');
  await page.getByRole('button', { name: '开始任务' }).click();
  await page.getByText('验收：通过', { exact: false }).waitFor({ timeout: 20000 });
  await page.getByText('validator → reviewer').click();
  await page.getByText('Ran 1 test', { exact: false }).waitFor();
  assert.ok(fs.existsSync(path.join(temp, 'tasks', 'supervisor-worktrees')));
  console.log('[pass] real API task, automatic worktree and discovered test, acceptance and validation displayed');
  await page.getByLabel('想完成什么').fill('hold execution');
  await page.getByRole('button', { name: '开始任务' }).click();
  await page.getByRole('button', { name: '停止任务' }).click({ timeout: 10000 });
  await page.getByText('状态：已停止', { exact: false }).waitFor();
  console.log('[pass] browser cancellation reaches the live execution');
  assert.deepEqual(errors, []);
} finally {
  if (browser) await browser.close();
  await server.close();
  backend.kill('SIGTERM');
  await new Promise(resolve => backend.once('exit', resolve));
  fs.rmSync(temp, { recursive: true, force: true });
}
