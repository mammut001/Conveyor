/** Real ChatPanel browser regression checks with a controlled SSE endpoint.
 * Run: node scripts/web_chat_browser_smoke.mjs
 * Set PLAYWRIGHT_MODULE_PATH if Playwright is provided outside this repository.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../web');
const require = createRequire(path.join(root, 'package.json'));
const { createServer } = await import(require.resolve('vite'));
const playwright = await import(process.env.PLAYWRIGHT_MODULE_PATH || require.resolve('playwright'));
const { chromium } = playwright.default || playwright;
const requests = [];
const taskRequests = [];
const handoffSession = 'web:web-console:webchat-handoff';
let appScenario = false;
let appHistory = [];
let queuedTask;
let holdRaceReplay = false;
let replayReply;
let liveTaskStream;
const taskConnections = [];
let includeOtherSession = false;
let delayedSessionId = '';
let failingSessionId = '';
const delayedSessionReplies = [];
const otherSession = {id:'web-other',title:'Second conversation',channel:'web',operator_id:'web-console',source_chat_id:'other',job_count:0,created_at:new Date().toISOString(),last_activity:new Date().toISOString()};
const jsonReply = (res,data) => {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
const taskEvent = (sequence,name,kind='tool.started') => ({schema_version:1,event_id:`race-${sequence}`,sequence,timestamp:new Date().toISOString(),kind,job_id:'race-job',payload:{name,text:name}});
const waitForFixture = async predicate => {
  for (let attempt=0;attempt<500;attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve,20));
  }
  throw new Error('Timed out waiting for the controlled fixture');
};
const appSession = () => ({id:handoffSession, channel:'web', operator_id:'web-console', source_chat_id:'webchat-handoff', title:'Handoff conversation', created_at:new Date().toISOString(), last_activity:new Date().toISOString(), job_count:queuedTask ? 1 : 0, latest_job:queuedTask});
let pending;
let historyPending;
let delayHistory = false;
const history = Array.from({ length: 24 }, (_, index) => ({
  role: index % 2 ? 'assistant' : 'user',
  text: `History ${index}: ${'A previous conversation. '.repeat(30)}`,
}));
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const server = await createServer({
  root, configFile: false, server: { host: '127.0.0.1', port: 0 },
  optimizeDeps: { include: ['react', 'react-dom/client', 'react-markdown', 'remark-gfm'] },
  plugins: [{
    name: 'chat-smoke-fixture',
    configureServer(vite) {
      vite.middlewares.use(async (req, res, next) => {
        if (req.url === '/__chat_smoke' || req.url === '/__other_chat_smoke' || req.url === '/__app_smoke') {
          appScenario = req.url === '/__app_smoke';
          const html = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="margin:0"><div id="root" class="primary-interface" style="height:100vh"></div><script type="module">
            import React from 'react';
            import ReactDOM from 'react-dom/client'; const {createRoot} = ReactDOM;
            import {ChatPanel} from '/src/components/ChatPanel.tsx';
            import App from '/src/App.tsx';
            import '/src/styles.css'; import '/src/v2.css'; import '/src/primary.css';
            createRoot(document.getElementById('root')).render(${appScenario ? 'React.createElement(App)' : "React.createElement(ChatPanel, {token:'test', agentSessionId:'" + (req.url === '/__other_chat_smoke' ? 'other' : 'smoke') + "', agentName:'Test agent'})"});
          </script></body></html>`;
          res.setHeader('Content-Type', 'text/html');
          res.end(await vite.transformIndexHtml(req.url, html));
          return;
        }
        if (appScenario && req.url?.startsWith('/api/') && !req.url.startsWith('/api/chat')) {
          let data;
          if (req.url === '/api/tasks' && req.method === 'POST') {
            let body = ''; for await (const chunk of req) body += chunk;
            taskRequests.push(JSON.parse(body));
            queuedTask = {id:'handoff-job', state:'queued', mode:'run', channel:'web', operator_id:'web-console', chat_id:'webchat-handoff', created_at:new Date().toISOString(), prompt_preview:taskRequests.at(-1).prompt};
            data = {job_id:queuedTask.id,session_id:handoffSession};
          } else if (req.url === '/api/sessions') data = {sessions:includeOtherSession ? [appSession(),otherSession] : [appSession()]};
          else if (req.url.startsWith('/api/sessions/')) {
            const sessionId = decodeURIComponent(req.url.slice('/api/sessions/'.length));
            if (sessionId === failingSessionId) {res.statusCode=503;jsonReply(res,{error:'Controlled transcript outage'});return;}
            data = sessionId === otherSession.id
              ? {...otherSession,messages:[{id:'second-message',session_id:otherSession.id,role:'assistant',content:'Second session active',created_at:new Date().toISOString()}],jobs:[]}
              : {...appSession(),messages:appHistory.map((m,index) => ({id:`handoff-${index}`,session_id:handoffSession,role:m.role,content:m.text,created_at:new Date().toISOString()})),jobs:queuedTask ? [queuedTask] : []};
            if (sessionId === delayedSessionId) {const snapshot=data;delayedSessionReplies.push(() => {if (!res.destroyed) jsonReply(res,snapshot);});return;}
          }
          else if (req.url === '/api/jobs') data = {jobs:queuedTask ? [queuedTask] : []};
          else if (req.url.startsWith('/api/jobs/') && req.url.includes('/events')) {
            if (holdRaceReplay && req.url.includes('/race-job/')) {replayReply=() => jsonReply(res,{events:[taskEvent(1,'Historical operation')]});return;}
            data = {events:[]};
          }
          else if (req.url.startsWith('/api/jobs/')) data = {diff:''};
          else if (req.url === '/api/agents') data = {enabled:false,agents:[]};
          else if (req.url === '/api/approvals') data = {approvals:[]};
          else if (req.url === '/api/nodes') data = {nodes:[]};
          else if (req.url === '/api/computer/status') data = {armed:false,arm_remaining_seconds:0,screenshots:[]};
          else if (req.url === '/api/system/status') data = {uptime_seconds:1,load_average:[0],cpu_count:1,memory:{total:null,available:null},disk:{total:1,used:0,free:1},queue:{depth:0,paused:false,states:{}},channels:{telegram:{configured:false},feishu:{configured:false}},nodes:[],features:{teammate:false,mobile_ui:true}};
          else if (req.url.startsWith('/api/events/stream')) {
            res.setHeader('Content-Type','text/event-stream');
            if (req.url.includes('job_id=race-job')) {
              taskConnections.push(new URL(req.url,'http://fixture').searchParams.get('after'));
              liveTaskStream=res;res.write(': heartbeat\n\n');return;
            }
            res.end(': heartbeat\n\n');return;
          }
          else {res.statusCode = 404; data = {error:`Unexpected fixture endpoint: ${req.url}`};}
          res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;
        }
        if (req.url?.startsWith('/api/chat/history')) {
          const reply = () => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({messages:appScenario ? appHistory : history})); };
          if (delayHistory) historyPending = reply;
          else reply();
          return;
        }
        if (req.url === '/api/chat' && req.method === 'POST') {
          let body = '';
          for await (const chunk of req) body += chunk;
          requests.push(JSON.parse(body));
          res.setHeader('Content-Type', 'text/event-stream');
          res.write(frame('session', {session_id:appScenario ? handoffSession : 'smoke'}));
          pending = res;
          return;
        }
        next();
      });
    },
  }],
});
let browser;
try {
  await server.listen();
  browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1100,height:800}});
  page.setDefaultTimeout(10000);
  const failures = [];
  page.on('pageerror', err => failures.push(err.message));
  const url = `http://127.0.0.1:${server.httpServer.address().port}/__chat_smoke`;
  await page.goto(url);
  const input = page.getByRole('textbox');
  const send = async text => {
    await input.fill(text);
    const response = page.waitForResponse(res => res.url().endsWith('/api/chat') && res.request().method() === 'POST');
    await input.press('Enter');
    await response;
    await page.getByRole('status').waitFor();
  };
  await page.getByText('History 23:', {exact:false}).waitFor();
  await input.fill('中文输入');
  await input.evaluate(node => node.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',isComposing:true,bubbles:true,cancelable:true})));
  assert.equal(requests.length, 0, 'IME confirmation must not submit');
  await input.evaluate(node => node.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',keyCode:229,bubbles:true,cancelable:true})));
  assert.equal(requests.length, 0, 'legacy IME confirmation must not submit');
  await input.press('Shift+Enter');
  assert.match(await input.inputValue(), /\n/, 'Shift+Enter inserts newline');
  await send('first message');
  assert.equal(await input.isEnabled(), true, 'can prepare next message while waiting');
  await input.fill('next draft');
  const stream = page.locator('.event-stream');
  await stream.evaluate(node => {node.scrollTo({top:0,behavior:'instant'}); node.dispatchEvent(new Event('scroll'));});
  await page.getByRole('button', {name:'回到最新消息',exact:false}).waitFor();
  await page.waitForFunction(() => document.querySelector('.event-stream').scrollTop === 0);
  pending.write(frame('delta', {text:'Partial answer that must survive disconnection'}));
  await page.getByText('Partial answer that must survive disconnection', {exact:true}).waitFor();
  assert.equal(await stream.evaluate(node => node.scrollTop), 0, 'streaming must not interrupt reading history');
  await page.getByRole('button', {name:'回到最新消息',exact:false}).click();
  assert.ok(await stream.evaluate(node => node.scrollTop) > 0);
  pending.end();
  await page.getByRole('alert').waitFor();
  await page.getByText('回答中断 · 已保留收到的内容').waitFor();
  assert.equal(await input.inputValue(), 'next draft', 'failure preserves next draft');
  console.log('[pass] IME, multiline input, immediate feedback, draft while busy, scroll control, interrupted answer');

  await send('retryable request');
  pending.write(frame('error', {error:'Test server failure'}));
  pending.end(frame('done', {outcome:'error'}));
  await page.getByRole('alert').waitFor();
  await page.waitForFunction(() => document.querySelector('textarea').value === 'retryable request');
  console.log('[pass] failed request restores draft without automatic resubmission');

  await send('successful request');
  pending.write(frame('delta', {text:'Successful answer'}));
  pending.write(frame('message', {text:'Successful answer'}));
  pending.end(frame('done', {outcome:'ok'}));
  await page.getByText('Successful answer', {exact:true}).waitFor();
  await page.getByRole('button', {name:'Send',exact:false}).waitFor();
  assert.equal(await page.getByText('Successful answer', {exact:true}).count(), 1);
  assert.equal(await page.getByRole('alert').count(), 0);
  console.log('[pass] complete SSE answer renders once');
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(() => {const node=document.querySelector('.event-stream'); return node.scrollHeight-node.scrollTop-node.clientHeight < 5;});
  await page.locator('.event-stream').evaluate(node => {node.scrollTo({top:0,behavior:'instant'});node.dispatchEvent(new Event('scroll'));});
  await page.getByRole('button', {name:'回到最新消息',exact:false}).waitFor();
  await page.setViewportSize({width:1100,height:800});
  assert.equal(await page.locator('.event-stream').evaluate(node => node.scrollTop), 0, 'reader in history stays there during resize');
  await page.getByRole('button', {name:'回到最新消息',exact:false}).click();
  console.log('[pass] viewport reflow follows the latest reply while respecting history readers');


  const codeSnippet = 'if True:\n    print("ok")\n\nurl = "https://example.com/' + 'x'.repeat(120) + '"';
  await send('Show a structured answer');
  pending.write(frame('delta', {text:'## 执行计划\n\n```python\n' + codeSnippet}));
  await page.locator('.code-block-pre code').waitFor();
  assert.equal(await page.locator('.code-block-pre code').textContent(), codeSnippet, 'unfinished streamed fence is already formatted as code');
  const richAnswer = [
    '## 执行计划', '', '3. 检查 **当前状态**', '4. 执行验证', '',
    '- 外层步骤', '  - 内层步骤', '', '> 保留现有行为', '',
    '| 状态 | 耗时 | 负责人 | 说明 |', '| :--- | ---: | --- | --- |',
    '| **通过** | 120ms | Conveyor | 已验证 |', '',
    '```python', codeSnippet, '```', '',
    '[查看说明](https://example.com/path_(a))', '',
    '<img src="x" onerror="window.__unsafe=true">', '',
    '[危险链接](javascript:alert(1))', '',
    '![参考图片](https://example.com/image.png)',
  ].join('\n');
  pending.write(frame('message', {text:richAnswer}));
  pending.end(frame('done', {outcome:'answered'}));
  await page.getByRole('heading', {name:'执行计划',exact:true}).waitFor();
  assert.equal(await page.locator('ol[start="3"] > li').count(), 2);
  assert.equal(await page.locator('ul ul li').textContent(), '内层步骤');
  assert.equal((await page.locator('blockquote').textContent()).trim(), '保留现有行为');
  assert.equal(await page.getByRole('table').getByRole('row').count(), 2);
  assert.equal(await page.getByRole('table').locator('strong').textContent(), '通过');
  assert.equal(await page.getByRole('link', {name:'查看说明',exact:true}).getAttribute('href'), 'https://example.com/path_(a)');
  await page.getByText('危险链接', {exact:true}).waitFor();
  assert.equal(await page.locator('a[href^="javascript:"]').count(), 0);
  assert.equal(await page.getByRole('link', {name:'参考图片',exact:true}).getAttribute('href'), 'https://example.com/image.png');
  assert.equal(await page.locator('img').count(), 0, 'markdown HTML and remote image references do not create image elements');
  assert.equal(await page.evaluate(() => window.__unsafe), undefined);
  await page.context().grantPermissions(['clipboard-read','clipboard-write']);
  await page.locator('.code-copy-btn').click();
  await page.getByRole('button', {name:'✓ Copied',exact:true}).waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), codeSnippet, 'copy preserves indentation and blank lines');
  await page.evaluate(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async () => {throw new Error('denied');}}}));
  await page.locator('.code-copy-btn').click();
  await page.getByRole('button', {name:'Copy failed',exact:true}).waitFor();
  await page.evaluate(() => { delete navigator.clipboard; });
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'rich markdown must not widen the mobile page');
  assert.ok(await page.locator('.formatted-table-scroll').evaluate(node => node.scrollWidth > node.clientWidth), 'wide table scrolls inside its own region');
  assert.ok(await page.locator('.code-block-pre').evaluate(node => node.scrollWidth > node.clientWidth), 'long code scrolls inside the code block');
  await page.getByRole('heading', {name:'执行计划',exact:true}).scrollIntoViewIfNeeded();
  if (process.env.CHAT_MARKDOWN_SCREENSHOT) await page.screenshot({path:process.env.CHAT_MARKDOWN_SCREENSHOT,fullPage:true});
  await page.setViewportSize({width:1100,height:800});
  console.log('[pass] structured markdown, unfinished code fences, clipboard feedback, safe links, and mobile table/code scrolling');


  delayHistory = true;
  await page.reload();
  await send('message before history');
  historyPending();
  pending.write(frame('message', {text:'Fresh answer'}));
  pending.end(frame('done', {outcome:'ok'}));
  await page.getByText('Fresh answer', {exact:true}).waitFor();
  assert.equal(await page.getByText('message before history', {exact:true}).count(), 1);
  assert.equal(await page.getByText('History 23:', {exact:false}).count(), 0, 'stale history must not overwrite current turn');
  console.log('[pass] slow history cannot overwrite a new conversation turn');
  await page.setViewportSize({width:390,height:844});
  await input.fill('手机端草稿');
  assert.equal(await input.isVisible(), true);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile layout has no horizontal overflow');
  console.log('[pass] mobile composer and browser runtime');
  await page.reload();
  assert.equal(await input.inputValue(), '手机端草稿', 'draft survives browser reload');
  await page.goto(url.replace('/__chat_smoke','/__other_chat_smoke'));
  assert.equal(await input.inputValue(), '', 'another agent starts with its own draft');
  await input.fill('另一位 Agent 的草稿');
  await page.reload();
  assert.equal(await input.inputValue(), '另一位 Agent 的草稿');
  await page.goto(url);
  assert.equal(await input.inputValue(), '手机端草稿', 'agent drafts remain isolated');
  console.log('[pass] draft restoration across reloads and isolation between agents');


  delayHistory = false;
  await page.setViewportSize({width:1100,height:800});
  await page.addInitScript(() => sessionStorage.setItem('conveyor-token','test'));
  await page.goto(url.replace('/__chat_smoke','/__app_smoke'));
  await page.getByRole('button', {name:'Chat',exact:true}).first().click();
  const handoffPrompt = '修复项目测试，保留现有行为';
  await send(handoffPrompt);
  const suggestion = {prompt:handoffPrompt,session_id:handoffSession};
  appHistory = [{role:'user',text:handoffPrompt},{role:'assistant',text:'需要执行任务',task_suggestion:suggestion}];
  pending.write(frame('message', {text:'需要执行任务',task_suggestion:suggestion}));
  pending.end(frame('done', {outcome:'escalate'}));
  await page.getByRole('button', {name:'准备执行任务',exact:false}).click();
  assert.equal(await input.inputValue(), handoffPrompt, 'original request prefilled in task composer');
  assert.equal(taskRequests.length, 0, 'preparing a task must not execute it');
  await page.getByText('原请求已带入任务页。你可以编辑内容，再点击发送执行。').waitFor();
  await page.getByRole('heading', {name:'继续聊天任务',exact:true}).waitFor();
  await page.locator('.transcript-panel').getByText('需要执行任务', {exact:true}).waitFor();
  if (process.env.CHAT_SMOKE_SCREENSHOT) await page.screenshot({path:process.env.CHAT_SMOKE_SCREENSHOT,fullPage:true});
  await input.evaluate(node => node.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',isComposing:true,bubbles:true,cancelable:true})));
  assert.equal(taskRequests.length, 0, 'task composer also protects IME confirmation');
  const submitted = page.waitForResponse(res => res.url().endsWith('/api/tasks'));
  await input.press('Enter');
  await submitted;
  assert.deepEqual(taskRequests, [{prompt:handoffPrompt,mode:'run',session_id:handoffSession,execution_only:true}]);
  await page.getByText('Handoff conversation', {exact:true}).first().waitFor();
  assert.equal(await page.locator('.session-item').count(), 1, 'promoted chat session appears in Tasks');
  await page.reload();
  await page.getByRole('button', {name:'Chat',exact:true}).first().click();
  await page.getByRole('button', {name:'准备执行任务',exact:false}).waitFor();
  console.log('[pass] full App chat-to-task handoff, review before submission, same session, task list, restored action');
  await input.fill('尚未发送的下一步想法');
  await page.getByRole('button', {name:'Tasks',exact:true}).first().click();
  await page.getByRole('button', {name:'Chat',exact:true}).first().click();
  assert.equal(await input.inputValue(), '尚未发送的下一步想法', 'draft survives tab navigation');
  await send('continue while I inspect Tasks');
  const backgroundReply = pending;
  await input.fill('回复期间写的草稿');
  await page.getByRole('button', {name:'Tasks',exact:true}).first().click();
  assert.equal(backgroundReply.destroyed, false, 'switching views must not cancel the chat request');
  backgroundReply.write(frame('delta', {text:'Reply completed while Tasks was open'}));
  backgroundReply.write(frame('message', {text:'Reply completed while Tasks was open'}));
  backgroundReply.end(frame('done', {outcome:'answered'}));
  await page.getByRole('button', {name:'Chat',exact:true}).first().click();
  await page.getByText('Reply completed while Tasks was open', {exact:true}).waitFor();
  assert.equal(await input.inputValue(), '回复期间写的草稿');
  assert.equal(await page.getByRole('alert').count(), 0);
  assert.equal(await page.getByRole('button', {name:'Send',exact:false}).isEnabled(), true);
  console.log('[pass] chat completes in background across view switches, preserving the next draft');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await input.isVisible(), true);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'full mobile App has no horizontal overflow');
  await page.getByRole('button', {name:'＋ New',exact:true}).click();
  assert.equal(await input.inputValue(), '', 'new conversation clears its draft');
  assert.equal(await page.evaluate(() => sessionStorage.getItem('conveyor-chat-draft:v1:free-chat')), null);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('conveyor-chat-draft:v1:smoke')), '手机端草稿', 'new conversation does not clear another agent draft');
  console.log('[pass] full mobile App layout and new-conversation draft reset');

  await page.setViewportSize({width:1100,height:800});
  queuedTask = {...queuedTask,id:'race-job',state:'running'};
  holdRaceReplay = true;
  await page.reload();
  await waitForFixture(() => replayReply && liveTaskStream);
  liveTaskStream.write(frame('event',taskEvent(2,'Live operation')));
  await page.locator('.live-tools-list summary strong').getByText('Live operation',{exact:true}).waitFor();
  const replayReceived = page.waitForResponse(res => res.url().includes('/race-job/events'));
  holdRaceReplay = false;
  replayReply();
  await replayReceived;
  await page.locator('.live-tools-list summary strong').getByText('Historical operation',{exact:true}).waitFor();
  assert.deepEqual(await page.locator('.live-tools-list summary strong').allTextContents(), ['Historical operation','Live operation']);
  liveTaskStream.end();
  await page.getByText('正在重新连接任务进度',{exact:true}).waitFor();
  await waitForFixture(() => taskConnections.length >= 2);
  assert.equal(taskConnections.at(-1),'2','replay arrival must not regress the reconnect cursor');
  liveTaskStream.write(frame('event',taskEvent(2,'Live operation')));
  liveTaskStream.write(frame('event',taskEvent(3,'Recovered operation')));
  await page.locator('.live-tools-list summary strong').getByText('Recovered operation',{exact:true}).waitFor();
  assert.deepEqual(await page.locator('.live-tools-list summary strong').allTextContents(), ['Historical operation','Live operation','Recovered operation']);
  console.log('[pass] late event replay preserves live progress, ordered deduplication, and reconnect cursor');

  includeOtherSession = true;
  queuedTask = {...queuedTask,state:'failed',error:'Controlled task failure'};
  liveTaskStream.write(frame('event',taskEvent(4,'Controlled task failure','task.failed')));
  await page.getByRole('alert').filter({hasText:'任务执行失败'}).waitFor();
  assert.equal(await page.getByText('Controlled task failure',{exact:true}).first().isVisible(),true);
  assert.equal(await page.locator('.transcript-panel').count(),1,'failure remains visible with an existing transcript');
  assert.equal(await page.getByRole('heading',{name:'Conveyor Control Console',exact:true}).count(),0,'failed jobs must not show the empty welcome state');
  console.log('[pass] task failure and its reason stay visible alongside prior conversation');
  queuedTask = {...queuedTask,state:'cancelled'};
  liveTaskStream.write(frame('event',taskEvent(5,'Cancelled','task.cancelled')));
  await page.getByText('任务已取消',{exact:true}).waitFor();
  queuedTask = {...queuedTask,state:'interrupted',error:'Controlled interruption'};
  liveTaskStream.write(frame('event',taskEvent(6,'Interrupted','task.interrupted')));
  await page.getByText('任务已中断',{exact:true}).waitFor();
  assert.equal(await page.locator('.transcript-panel').count(),1);
  console.log('[pass] cancelled and interrupted task notices retain prior conversation');


  await page.locator('.session-item').filter({hasText:'Second conversation'}).click();
  await page.getByText('Second session active',{exact:true}).waitFor();
  delayedSessionId = handoffSession;
  appHistory = [{role:'assistant',text:'OLD session response must not overwrite'}];
  await page.locator('.session-item').filter({hasText:'Handoff conversation'}).click();
  await waitForFixture(() => delayedSessionReplies.length > 0);
  await page.locator('.session-item').filter({hasText:'Second conversation'}).click();
  await page.getByText('Second session active',{exact:true}).waitFor();
  const staleReceived = page.waitForResponse(res => res.url().includes(encodeURIComponent(handoffSession)));
  delayedSessionId = '';
  for (const reply of delayedSessionReplies.splice(0)) reply();
  await staleReceived;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.getByText('Second session active',{exact:true}).isVisible(),true);
  assert.equal(await page.getByText('OLD session response must not overwrite',{exact:true}).count(),0);
  console.log('[pass] a late transcript cannot overwrite a newly selected task session');
  failingSessionId = otherSession.id;
  await page.getByText('暂时无法更新会话记录',{exact:true}).waitFor();
  assert.equal(await page.getByText('Second session active',{exact:true}).isVisible(),true,'polling failure preserves existing conversation');
  failingSessionId = '';
  await page.getByRole('button',{name:'重试读取',exact:true}).click();
  await page.getByText('暂时无法更新会话记录',{exact:true}).waitFor({state:'hidden'});
  assert.equal(await page.getByText('Second session active',{exact:true}).isVisible(),true);
  delayedSessionId = otherSession.id;
  await waitForFixture(() => delayedSessionReplies.length > 0);
  for (let poll=0;poll<2;poll++) await page.waitForResponse(res => res.url().endsWith('/api/system/status'));
  assert.equal(delayedSessionReplies.length,1,'slow transcript loads must not overlap on polling ticks');
  const slowReceived = page.waitForResponse(res => res.url().endsWith('/api/sessions/web-other'));
  delayedSessionId = '';
  delayedSessionReplies.shift()();
  await slowReceived;
  assert.equal(await page.getByText('Second session active',{exact:true}).isVisible(),true);
  console.log('[pass] transcript outages preserve content, explicit retry recovers, slow polling requests are deduplicated');
  delayedSessionId = otherSession.id;
  await waitForFixture(() => delayedSessionReplies.length > 0);
  await page.getByText('暂时无法更新会话记录',{exact:true}).waitFor({timeout:20_000});
  assert.equal(await page.getByText('Second session active',{exact:true}).isVisible(),true,'a hung load times out without clearing the conversation');
  delayedSessionId = '';
  for (const reply of delayedSessionReplies.splice(0)) reply();
  // Polling can recover as soon as the fixture is released, removing Retry
  // before Playwright finishes clicking it. Explicit retry is covered above;
  // here verify automatic recovery of a timed-out read.
  await page.getByText('暂时无法更新会话记录',{exact:true}).waitFor({state:'hidden',timeout:20_000});
  assert.equal(await page.getByText('Second session active',{exact:true}).isVisible(),true);
  console.log('[pass] hung transcript loads time out and polling recovers without losing visible content');



  const plainPage = await browser.newPage();
  await plainPage.route('**/src/components/MarkdownContent.tsx', route => route.abort());
  await plainPage.goto(url);
  await plainPage.locator('[data-render-mode="plain-text"]').last().waitFor();
  await plainPage.getByText('History 23:', {exact:false}).waitFor();
  assert.equal(await plainPage.getByRole('textbox').isEnabled(), true, 'a failed optional markdown chunk must leave chat usable');
  assert.equal(await plainPage.locator('.formatted-text-flow').count(), history.length);
  await plainPage.close();
  console.log('[pass] optional markdown-load failure falls back to readable text');
  assert.deepEqual(failures, []);
} finally {
  pending?.destroy();
  liveTaskStream?.destroy();
  await browser?.close();
  await server.close();
}
