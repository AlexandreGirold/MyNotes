'use strict';
const { app, BrowserWindow, dialog, ipcMain, safeStorage, session, shell } = require('electron');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { randomBytes } = require('node:crypto');
const { createInterface } = require('node:readline');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let win, backend, connection;
let quitting = false;
const root = path.resolve(__dirname, '..');
const uiUrl = pathToFileURL(path.join(root, 'dist', 'index.html')).href;
const executeFile = promisify(execFile);
const isolatedSmoke = !!process.env.MYNOTES_TEST_DATA_DIR && !!(process.env.MYNOTES_SMOKE_TEST || process.env.MYNOTES_LIVE_SMOKE_TEST);
if (isolatedSmoke) app.disableHardwareAcceleration();
if (process.env.MYNOTES_LIVE_SMOKE_TEST) {
  app.commandLine.appendSwitch('use-fake-device-for-media-stream');
  app.commandLine.appendSwitch('use-fake-ui-for-media-stream');
}

async function encryptionKey() {
  // Isolated simulator tests use session-only placeholder credentials, never the OS keyring.
  if (isolatedSmoke) return null;
  if (!safeStorage.isEncryptionAvailable() ||
      (process.platform === 'linux' && safeStorage.getSelectedStorageBackend() === 'basic_text')) return null;
  const keyFile = path.join(app.getPath('userData'), 'encryption-key.enc');
  try { return safeStorage.decryptString(await fs.readFile(keyFile)); }
  catch (e) { if (e.code !== 'ENOENT') throw e; }
  const key = randomBytes(32).toString('hex');
  await fs.mkdir(path.dirname(keyFile), { recursive: true, mode: 0o700 });
  await fs.writeFile(keyFile, safeStorage.encryptString(key), { mode: 0o600, flag: 'wx' });
  return key;
}

async function startBackend() {
  const dataDir = path.join(app.getPath('userData'), 'data');
  const token = randomBytes(32).toString('hex');
  const toolToken = randomBytes(32).toString('hex');
  const args = ['serve', '--port', '0', '--data-dir', dataDir];
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'mynotes', 'mynotes') : 'uv';
  const childArgs = app.isPackaged ? args : ['run', '--locked', 'python', '-m', 'mynotes', ...args];
  const env = { ...process.env };
  if (isolatedSmoke) {
    env.MYNOTES_RUNTIME_FILE = path.join(dataDir, 'runtime.json');
  }
  if (app.isPackaged) {
    const tools = path.join(process.resourcesPath, 'tools');
    env.MYNOTES_NODE = path.join(tools, 'node');
    env.MYNOTES_MARP = path.join(tools, 'marp', 'node_modules', '@marp-team', 'marp-cli', 'marp-cli.js');
    const browser = JSON.parse(await fs.readFile(path.join(tools, 'browser.json'), 'utf8'));
    env.MYNOTES_CHROMIUM = path.join(tools, browser.executable);
    const opencode = path.join(tools, 'opencode');
    try { await fs.access(opencode); env.MYNOTES_OPENCODE = opencode; } catch { /* Agent CLI may be installed on PATH. */ }
  } else {
    env.MYNOTES_MARP = path.join(root, 'node_modules', '@marp-team', 'marp-cli', 'marp-cli.js');
    env.MYNOTES_NODE = process.env.MYNOTES_NODE || 'node';
    try {
      const browser = JSON.parse(await fs.readFile(path.join(root, 'resources/tools/browser.json'), 'utf8'));
      env.MYNOTES_CHROMIUM = path.join(root, 'resources/tools', browser.executable);
    } catch { /* Development may use a system browser. */ }
  }
  backend = spawn(executable, childArgs, { cwd: app.isPackaged ? path.dirname(process.execPath) : root, env, stdio: ['pipe', 'pipe', 'pipe'] });
  backend.stderr.on('data', data => process.stderr.write(data));
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Backend startup timed out')), 30000);
    const lines = createInterface({ input: backend.stdout });
    backend.once('error', reject);
    backend.once('exit', code => {
      clearTimeout(timer);
      reject(new Error(`Backend exited (${code})`));
      if (!quitting && connection) { dialog.showErrorBox('MyNotes backend stopped', 'Restart MyNotes to continue.'); app.quit(); }
    });
    lines.on('line', line => {
      try {
        const info = JSON.parse(line);
        const url = new URL(info.url);
        if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || info.token !== token) return;
        clearTimeout(timer);
        resolve({ url: url.origin, token });
      } catch { /* Non-readiness output. */ }
    });
  });
  backend.stdin.end(JSON.stringify({ token, tool_token: toolToken, fernet_key: await encryptionKey() }) + '\n');
  return ready;
}

function checkSender(event) {
  if (!win || event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame ||
      event.senderFrame.url !== uiUrl) throw new Error('Untrusted IPC sender');
}
function recordingDir(id) {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(id)) throw new Error('Invalid recording ID');
  return path.join(app.getPath('userData'), 'recordings', id);
}
const arrayBuffer = data => data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);

ipcMain.handle('connection', event => { checkSender(event); return connection; });
async function microphoneVolume() {
  if (process.platform !== 'linux') throw new Error('System microphone control currently requires Linux with PipeWire.');
  const { stdout } = await executeFile('wpctl', ['get-volume', '@DEFAULT_AUDIO_SOURCE@'], { timeout: 3000, maxBuffer: 4096 });
  const match = /^Volume:\s+([0-9]+(?:\.[0-9]+)?)(?:\s+\[MUTED\])?\s*$/.exec(stdout);
  if (!match) throw new Error('Could not read the default microphone volume.');
  return { volume: Math.min(120, Math.round(Number(match[1]) * 100)), muted: stdout.includes('[MUTED]') };
}
ipcMain.handle('getMicrophoneVolume', async event => { checkSender(event); return microphoneVolume(); });
const _micSetGuard = { inFlight: false };
ipcMain.handle('setMicrophoneVolume', async (event, volume) => {
  checkSender(event);
  if (!Number.isInteger(volume) || volume < 0 || volume > 120) throw new Error('Microphone volume must be an integer between 0 and 120.');
  if (process.platform !== 'linux') throw new Error('System microphone control currently requires Linux with PipeWire.');
  if (_micSetGuard.inFlight) throw new Error('A microphone level change is already in progress.');
  _micSetGuard.inFlight = true;
  try {
    const { stdout: inspect } = await executeFile('wpctl', ['inspect', '@DEFAULT_AUDIO_SOURCE@'], { timeout: 3000, maxBuffer: 4096 });
    const srcMatch = /^id\s+(\d+),/im.exec(inspect);
    if (!srcMatch) throw new Error('Could not identify the default microphone.');
    const source = srcMatch[1];
    await executeFile('wpctl', ['set-volume', '-l', '1.2', source, `${volume}%`], { timeout: 3000, maxBuffer: 4096 });
    const target = Number(volume);
    let attempt = 0;
    const maxAttempts = 2;
    while (attempt < maxAttempts) {
      attempt++;
      const { stdout } = await executeFile('wpctl', ['get-volume', source], { timeout: 1000, maxBuffer: 4096 });
      const match = /^Volume:\s+([0-9]+(?:\.[0-9]+)?)(?:\s+\[MUTED\])?\s*$/.exec(stdout);
      if (match) {
        const actual = Math.min(120, Math.round(Number(match[1]) * 100));
        if (actual === target) return { volume: actual, muted: stdout.includes('[MUTED]') };
      }
      if (attempt < maxAttempts) await new Promise(r => setTimeout(r, 200));
    }
    throw new Error(`Microphone volume did not update: requested ${target}% but system reported a different value.`);
  } finally { _micSetGuard.inFlight = false; }
});
ipcMain.handle('selectDirectories', async event => {
  checkSender(event);
  const selected = await dialog.showOpenDialog(win, { title: 'Allow the agent to read these folders', properties: ['openDirectory', 'multiSelections'] });
  return selected.canceled ? [] : selected.filePaths;
});
ipcMain.handle('saveTextFile', async (event, request) => {
  checkSender(event);
  if (!request || typeof request.text !== 'string' || request.text.length > 2000000 ||
      !['md', 'txt', 'typ'].includes(request.format)) throw new Error('Invalid text export');
  const name = String(request.name || 'MyNotes').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 80);
  const options = {
    title: 'Save notes as…', defaultPath: path.join(app.getPath('documents'), `${name}.${request.format}`),
    filters: [{ name: request.format === 'md' ? 'Markdown document' : request.format === 'typ' ? 'Typst source' : 'Text document', extensions: [request.format] }],
    properties: ['createDirectory', 'showOverwriteConfirmation'],
  };
  const { canceled, filePath } = process.env.MYNOTES_LIVE_SMOKE_TEST && process.env.MYNOTES_SAVE_TEST_PATH
    ? { canceled: false, filePath: process.env.MYNOTES_SAVE_TEST_PATH }
    : await dialog.showSaveDialog(win, options);
  if (canceled || !filePath) return null;
  const temporary = path.join(path.dirname(filePath), `.${path.basename(filePath)}.${randomBytes(8).toString('hex')}.tmp`);
  try {
    await fs.writeFile(temporary, request.text, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    await fs.rename(temporary, filePath);
  } finally { await fs.unlink(temporary).catch(() => {}); }
  return { path: filePath, name: path.basename(filePath) };
});
ipcMain.handle('selectImport', async event => {
  checkSender(event);
  const { canceled, filePaths } = await dialog.showOpenDialog(win, {
    properties: ['openFile'], filters: [{ name: 'Documents', extensions: ['pdf', 'pptx', 'md', 'html', 'typ', 'zip'] }],
  });
  if (canceled || !filePaths.length) return null;
  const file = filePaths[0];
  const data = await fs.readFile(file);
  const kind = { '.pdf': 'pdf', '.pptx': 'pptx', '.md': 'markdown', '.html': 'html', '.typ': 'typst', '.zip': 'typst' }[path.extname(file).toLowerCase()];
  return { name: path.basename(file), data: arrayBuffer(data), kind };
});
ipcMain.handle('saveRecordingChunk', async (event, id, index, data) => {
  checkSender(event);
  const dir = recordingDir(id);
  if (!Number.isSafeInteger(index) || index < 0 || index > 999999 ||
      !(data instanceof ArrayBuffer) || data.byteLength > 50000000) throw new Error('Invalid recording chunk');
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const chunks = (await fs.readdir(dir)).filter(name => name.endsWith('.chunk'));
  if (chunks.length !== index) throw new Error('Recording chunks out of order');
  await fs.writeFile(path.join(dir, `${String(index).padStart(6, '0')}.chunk`), Buffer.from(data), { flag: 'wx', mode: 0o600 });
});
ipcMain.handle('finishRecording', async (event, id) => {
  checkSender(event);
  const dir = recordingDir(id);
  const chunks = (await fs.readdir(dir)).filter(name => name.endsWith('.chunk')).sort();
  if (!chunks.length) throw new Error('No recording chunks');
  let size = 0;
  for (const name of chunks) size += (await fs.stat(path.join(dir, name))).size;
  if (size > 500000000) throw new Error('Recording exceeds 500 MB');
  const merged = Buffer.concat(await Promise.all(chunks.map(name => fs.readFile(path.join(dir, name)))));
  await fs.writeFile(path.join(dir, 'recording.webm'), merged, { mode: 0o600 });
  return { name: `${id}.webm`, data: arrayBuffer(merged) };
});

if (process.env.MYNOTES_TEST_DATA_DIR) app.setPath('userData', process.env.MYNOTES_TEST_DATA_DIR);
if (!app.requestSingleInstanceLock()) app.quit();
else app.whenReady().then(async () => {
  connection = await startBackend();
  win = new BrowserWindow({ width: 1200, height: 800, show: false,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true,
      offscreen: isolatedSmoke, backgroundThrottling: !isolatedSmoke },
  });
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
      try {
        const link = new URL(url);
        if (link.protocol === 'https:' && !link.username && !link.password) {
          void shell.openExternal(link.href).catch(() => console.error('Could not open the external source link.'));
        }
      } catch { /* Reject malformed links. */ }
      return { action: 'deny' };
  });
  const trusted = contents => contents === win.webContents && contents.getURL() === uiUrl;
  session.defaultSession.setPermissionCheckHandler((contents, permission) => trusted(contents) && permission === 'media');
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(trusted(contents) && permission === 'media' && details.mediaTypes?.every(type => type === 'audio'));
  });
  await win.loadURL(uiUrl);
  win.show();
  if (process.env.MYNOTES_CONNECTION_REPORT) {
    const settingsResponse = await fetch(connection.url + '/settings', { headers: { Authorization: 'Bearer ' + connection.token } });
    const settings = await settingsResponse.json();
    const report = { saved_key_loaded: !!settings.has_key, credential_storage: settings.credential_storage, credential_error: settings.credential_error, models: [] };
    if (settings.has_key) {
      const response = await fetch(connection.url + '/provider/models', { headers: { Authorization: 'Bearer ' + connection.token } });
      report.models_status = response.status;
      if (response.ok) report.models = await response.json();
    }
    await fs.writeFile(process.env.MYNOTES_CONNECTION_REPORT, JSON.stringify(report, null, 2), { mode: 0o600 });
    console.log('Connection report saved (status and model IDs only; no API key).');
  }
  if (process.env.MYNOTES_SCREENSHOT) {
    await new Promise(resolve => setTimeout(resolve, 500));
    await fs.writeFile(process.env.MYNOTES_SCREENSHOT, (await win.webContents.capturePage()).toPNG());
  }
  if (process.env.MYNOTES_LIVE_SMOKE_TEST) {
    const upstream = process.env.MYNOTES_TEST_PROVIDER_URL;
    if (!upstream || !/^http:\/\/127\.0\.0\.1:\d+\/v1$/.test(upstream)) throw new Error('Live smoke requires a local test provider');
    const result = await win.webContents.executeJavaScript(`(async () => {
      const c = await window.mynotes.connection();
      const r = await fetch(c.url + '/settings', { method: 'PUT', headers: { Authorization: 'Bearer ' + c.token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: ${JSON.stringify(upstream)}, api_key: 'placeholder-test-key', realtime_model: 'voxtral', verified_realtime: true, verified_chat: true, agent_directories: [${JSON.stringify(path.join(process.env.MYNOTES_TEST_DATA_DIR, 'files'))}] }) });
      if (!r.ok) throw new Error('Could not configure local simulator');
      const wait = async (label, predicate) => { for (let i = 0; i < 150; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error(label + ': ' + document.body.innerText.slice(-2000)); };
      if (${!!process.env.MYNOTES_MIC_VOLUME_SMOKE_TEST}) {
        const current = await window.mynotes.getMicrophoneVolume();
        if (!Number.isInteger(current.volume) || current.volume < 0 || current.volume > 120) throw new Error('Invalid native microphone volume');
        let rejected = false;
        try { await window.mynotes.setMicrophoneVolume(-1); } catch { rejected = true; }
        if (!rejected) throw new Error('Invalid microphone volume was accepted');
        rejected = false;
        try { await window.mynotes.setMicrophoneVolume(121); } catch { rejected = true; }
        if (!rejected) throw new Error('Microphone volume over 120% was accepted');
        [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Settings').click();
        await wait('Microphone slider', () => document.querySelector('#microphone-volume') && !document.querySelector('#microphone-volume').disabled);
        const slider = document.querySelector('#microphone-volume');
        if (Number(slider.value) !== current.volume) throw new Error('Slider does not show actual input volume');
        slider.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
        await wait('Microphone slider applied', () => document.body.textContent.includes('Microphone input set to ' + current.volume + '%'));
        if ((await window.mynotes.getMicrophoneVolume()).volume !== current.volume) throw new Error('Microphone level changed unexpectedly');
      }
      document.querySelectorAll('[role="tab"]')[0].click();
      await wait('Notes navigation', () => document.querySelector('.view-notes'));
      document.querySelectorAll('[role="tab"]')[2].click();
      await wait('Provider setup', () => [...document.querySelectorAll('button')].some(b => b.textContent.includes('Start live transcription')));
      if (${!!process.env.MYNOTES_SPEAKER_SMOKE_TEST}) {
        const checkbox = () => [...document.querySelectorAll('label')].find(l => l.textContent.includes('Distinguish speakers'))?.querySelector('input');
        await wait('Speaker models ready', () => checkbox() && !checkbox().disabled);
        checkbox().click();
      }
      [...document.querySelectorAll('button')].find(b => b.textContent.includes('Start live transcription')).click();
      await wait('Live transcript', () => document.querySelector('[aria-label="Live transcript"]').value.includes('Live sentence'));
      if (!document.body.textContent.includes('Listening')) throw new Error('Transcript did not appear during capture');
      const noteEditor = document.querySelector('[aria-label="Personal notes"]');
       Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(noteEditor, '**PERSONAL_NOTE_SIGNAL**: this is my own interpretation.');
       noteEditor.dispatchEvent(new Event('input', { bubbles: true }));
       [...document.querySelectorAll('button')].find(b => b.textContent === 'Preview Markdown').click();
       await wait('Personal Markdown preview', () => document.querySelector('.personal-notes-preview strong')?.textContent === 'PERSONAL_NOTE_SIGNAL');
       [...document.querySelectorAll('button')].find(b => b.textContent === 'Edit Markdown').click();
       await wait('Personal Markdown source preserved', () => document.querySelector('[aria-label="Personal notes"]')?.value.includes('**PERSONAL_NOTE_SIGNAL**'));
      document.querySelectorAll('[role="tab"]')[0].click();
      await wait('Away from live page', () => document.querySelector('.view-notes'));
      await wait('Background transcript', () => document.querySelector('[aria-label="Live transcript"]').value.includes('Second live sentence'));
      if (!document.body.textContent.includes('Transcript running')) throw new Error('Missing background capture indicator');
      document.querySelectorAll('[role="tab"]')[2].click();
      await wait('Return to live page', () => document.querySelector('.view-recording'));
      [...document.querySelectorAll('button')].find(b => b.textContent.includes('Pause audio')).click();
      await wait('Audio paused', () => [...document.querySelectorAll('button')].some(b => b.textContent.includes('Resume audio')));
      [...document.querySelectorAll('button')].find(b => b.textContent.includes('Resume audio')).click();
      await wait('Audio resumed', () => [...document.querySelectorAll('button')].some(b => b.textContent.includes('Pause audio')));
      [...document.querySelectorAll('button')].find(b => b.textContent.includes('Stop recording')).click();
      await wait('Recording stopped without summary', () => [...document.querySelectorAll('button')].some(b => b.textContent.includes('Start live transcription')));
      if (document.querySelector('.paper-panel .text-document')) throw new Error('Stop unexpectedly generated a summary');
      [...document.querySelectorAll('button')].find(b => b.textContent === 'Summarize session').click();
      await wait('Summary', () => document.querySelector('.paper-panel .text-document')?.textContent.includes('Meeting summary'));
      await wait('Summary finished', () => [...document.querySelectorAll('button')].some(b => b.textContent.includes('Start live transcription')));
      const authHeaders = { Authorization: 'Bearer ' + c.token };
      const summaryChats = await (await fetch(c.url + '/chats', { headers: authHeaders })).json();
      if (summaryChats.length) throw new Error('Automatic summary leaked into assistant history');
      document.querySelectorAll('[role="tab"]')[1].click();
      await wait('Assistant ready', () => [...document.querySelectorAll('button')].some(b => b.textContent === 'New conversation' && !b.disabled));
      [...document.querySelectorAll('button')].find(b => b.textContent === 'New conversation').click();
      await wait('First conversation', () => document.querySelectorAll('.conversation-open').length === 1 && !document.querySelector('.conversation-open').disabled);
      const firstChat = document.querySelector('.conversation-open[aria-pressed="true"]').dataset.conversationId;
      [...document.querySelectorAll('button')].find(b => b.textContent === 'New conversation').click();
      await wait('Second conversation', () => document.querySelectorAll('.conversation-open').length === 2 && !document.querySelector('.conversation-open').disabled);
      if (document.querySelector('.conversation-open[aria-pressed="true"]').dataset.conversationId === firstChat) throw new Error('New conversation reused the old thread');
      if (!document.body.textContent.includes('Allowed-file & public web tools enabled')) throw new Error('Research capability missing from assistant');
      if (${!!process.env.MYNOTES_WEB_SMOKE_TEST}) {
        const task = document.querySelector('[aria-label="Chat task"]');
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(task, 'web');
        task.dispatchEvent(new Event('change', { bubbles: true }));
        const editor = document.querySelector('[aria-label="Question"]');
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, 'Read https://www.cloud-temple.com/en/products/large-language-model-as-a-service-llmaas/');
        editor.dispatchEvent(new Event('input', { bubbles: true }));
        await wait('Web composer ready', () => [...document.querySelectorAll('button')].some(b => b.textContent.includes('Send') && !b.disabled));
        [...document.querySelectorAll('button')].find(b => b.textContent.includes('Send')).click();
        await wait('Web evidence answer', () => document.querySelector('.conversation')?.textContent.includes('Public Internet source read.'));
        if (!document.querySelector('.source-chips a[href^="https://www.cloud-temple.com/"]')) throw new Error('Clickable Internet source missing');
      }
      await wait('Research composer ready', () => !document.querySelector('[aria-label="Chat task"]').disabled);
      const fileTask = document.querySelector('[aria-label="Chat task"]');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(fileTask, 'files');
      fileTask.dispatchEvent(new Event('change', { bubbles: true }));
      const fileQuestion = document.querySelector('[aria-label="Question"]');
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(fileQuestion, 'CANONICAL_FILE_SIGNAL');
      fileQuestion.dispatchEvent(new Event('input', { bubbles: true }));
      await wait('File composer ready', () => [...document.querySelectorAll('button')].some(b => b.textContent.includes('Send') && !b.disabled));
      [...document.querySelectorAll('button')].find(b => b.textContent.includes('Send')).click();
      await wait('Actual local-file evidence', () => document.querySelector('.conversation')?.textContent.includes('Local file source read: 42.'));
      if (![...document.querySelectorAll('.source-chips button')].some(b => b.textContent.includes('note.md'))) throw new Error('Local source citation missing');
      const deletedId = document.querySelector('.conversation-open[aria-pressed="true"]').dataset.conversationId;
      document.querySelector('.conversation-open[aria-pressed="true"]').parentElement.querySelector('.conversation-delete').click();
      await wait('Conversation deleted', () => document.querySelectorAll('.conversation-open').length === 1 && !document.querySelector('.conversation-open').disabled);
      if (document.querySelector('.conversation')?.textContent.includes('Local file source read: 42.')) throw new Error('Deleted chat remains visible');
      if ((await fetch(c.url + '/chats?conversation_id=' + deletedId, { headers: authHeaders })).status !== 404) throw new Error('Deleted chat remains accessible');
      [...document.querySelectorAll('button')].find(b => b.textContent === 'New conversation').click();
      await wait('Replacement conversation', () => document.querySelectorAll('.conversation-open').length === 2 && !document.querySelector('.conversation-open').disabled);
      document.querySelectorAll('[role="tab"]')[2].click();
      await wait('Return after assistant checks', () => document.querySelector('.view-recording'));
      console.log('Assistant desktop acceptance passed: persistent distinct conversations, web-tool indicator and summaries excluded.');
      if (${!!process.env.MYNOTES_SPEAKER_SMOKE_TEST}) {
        if (!document.querySelector('[aria-label="Live transcript"]').value.includes('Speaker unknown')) throw new Error('Speaker-labelled transcript missing');
        const auth = { Authorization: 'Bearer ' + c.token };
        const jobs = await (await fetch(c.url + '/jobs?kind=diarization', { headers: auth })).json();
        if (jobs.length !== 1 || jobs[0].status !== 'succeeded') throw new Error('Actual CPU speaker job did not complete');
        console.log('Speaker desktop acceptance passed: actual CPU models, raw preservation and labelled summary source.');
      }
      document.querySelector('[aria-label="Enable night mode"]')?.click();
      await wait('Night mode', () => document.documentElement.dataset.theme === 'dark');
      const exported = await window.mynotes.saveTextFile({ name: 'Chosen session name', text: document.querySelector('[aria-label="Live transcript"]').value, format: 'md' });
      if (!exported || !exported.name.endsWith('.md')) throw new Error('Native text export failed');
      return 'Live desktop UI acceptance passed: background capture, personal notes, streamed summary, night mode and native text export';
    })()`);
    if (process.env.MYNOTES_WIDE_LAYOUT_CHECK) {
      win.setSize(1920, 1080);
      await new Promise(resolve => setTimeout(resolve, 200));
      await win.webContents.executeJavaScript(`(() => {
        const content = document.querySelector('.view-recording');
        const page = document.querySelector('.meeting-page').getBoundingClientRect();
        const columns = document.querySelector('.meeting-columns').getBoundingClientRect();
        const box = content.getBoundingClientRect();
        const style = getComputedStyle(content);
        const availableWidth = content.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        if (Math.abs(page.width - availableWidth) > 2) throw new Error('Live page does not use full width');
        if (Math.abs(page.bottom - (box.bottom - parseFloat(style.paddingBottom))) > 3 || columns.height < 350) throw new Error('Live workspace does not fill available height');
      })()`);
      console.log('Wide desktop layout passed: full available width and height.');
    }
    console.log(result);
    const captureSmoke = async suffix => {
      if (!process.env.MYNOTES_SCREENSHOT) return;
      // Wait for the compositor, not just React's updated DOM, before reviewing the image.
      await new Promise(resolve => setTimeout(resolve, 250));
      await fs.writeFile(process.env.MYNOTES_SCREENSHOT.replace('.png', suffix + '.png'), (await win.webContents.capturePage()).toPNG());
    };
    await captureSmoke('-finished');
    await win.webContents.executeJavaScript(`(async () => {
      if ([...document.querySelectorAll('[role="tab"]')].some(t => t.textContent.includes('Presentations'))) throw new Error('Presentation navigation remains');
      document.querySelectorAll('[role="tab"]')[1].click();
      for (let i = 0; i < 100 && !document.querySelector('.conversation-open'); i++) await new Promise(r => setTimeout(r, 50));
      if (document.querySelectorAll('.conversation-open').length !== 2) throw new Error('Conversations were not preserved across navigation');
    })()`);
    await captureSmoke('-assistant');
    await win.webContents.executeJavaScript(`(async () => {
      document.querySelectorAll('[role="tab"]')[0].click();
      for (let i = 0; i < 100 && !document.querySelector('.file-tree-entry'); i++) await new Promise(r => setTimeout(r, 50));
      const root = document.querySelector('.file-tree-entry');
      if (!root) throw new Error('Allowed file tree missing');
      root.click();
      for (let i = 0; i < 100 && !document.querySelector('.file-tree .file-tree-entry[title$="Project 01"]'); i++) await new Promise(r => setTimeout(r, 50));
      const child = document.querySelector('.file-tree .file-tree-entry[title$="Project 01"]');
      if (!child || child.getBoundingClientRect().top < root.getBoundingClientRect().bottom - 1) throw new Error('Nested file tree is horizontally laid out');
      const browser = document.querySelector('.file-browser');
      if (browser.clientHeight > window.innerHeight) throw new Error('File browser is not bounded/scrollable');
    })()`);
    await captureSmoke('-files');
    app.quit();
  }
  if (process.env.MYNOTES_SMOKE_TEST) {
    const result = await win.webContents.executeJavaScript(`(async () => {
      const c = await window.mynotes.connection();
      const request = async (path, init = {}) => {
        const r = await fetch(c.url + path, { ...init, headers: { Authorization: 'Bearer ' + c.token, 'Content-Type': 'application/json' } });
        if (!r.ok) throw new Error(path + ': ' + await r.text());
        return r.json();
      };
      if (!Array.isArray(await request('/courses'))) throw new Error('Backend connection failed');
      if (!document.querySelector('#root')?.textContent.includes('Notes')) throw new Error('React failed to mount');
      const waitJob = async ack => {
        for (let i = 0; i < 100; i++) {
          const job = await request(ack.status_url);
          if (job.status === 'failed') throw new Error(job.error);
          if (job.status === 'succeeded') return job.result;
          await new Promise(resolve => setTimeout(resolve, 100));
        }
        throw new Error('Job timed out');
      };
      const first = await waitJob(await request('/notes', { method: 'POST', body: JSON.stringify({ title: 'Smoke note', content: 'Original quantum evidence', format: 'markdown' }) }));
      const doc = await request('/documents/' + first.document_id);
      const v = await request('/versions/' + doc.current_version_id);
      await waitJob(await request('/notes/' + doc.id, { method: 'PUT', body: JSON.stringify({ title: 'Updated note', content: 'Revised gravity evidence', format: 'markdown' }) }));
      const old = await request('/versions/' + v.id);
      if (old.blocks[0].text !== 'Original quantum evidence') throw new Error('Old version overwritten');
      if (!(await request('/search?q=gravity')).some(block => block.document_id === doc.id)) throw new Error('Search failed');
      const id = 'smoke-' + Date.now();
      await window.mynotes.saveRecordingChunk(id, 0, new Uint8Array([1, 2, 3]).buffer);
      const recording = await window.mynotes.finishRecording(id);
      if (new Uint8Array(recording.data).join() !== '1,2,3') throw new Error('Recording bytes corrupted');
      const source = (await request('/documents/' + doc.id)).blocks[0];
      const deck = await request('/decks', { method: 'POST', body: JSON.stringify({ title: 'Packaged export', markdown: '# Packaged lecture\\n\\nSelectable lecture text and $E=mc^2$', source_ids: [source.id] }) });
      const exported = await waitJob(await request('/slide-exports', { method: 'POST', body: JSON.stringify({ deck_id: deck.id, revision: deck.revision }) }));
      for (const field of ['html_path', 'pdf_path', 'manifest_path']) {
        const response = await fetch(c.url + '/files/' + exported[field], { headers: { Authorization: 'Bearer ' + c.token } });
        if (!response.ok || (await response.arrayBuffer()).byteLength === 0) throw new Error('Missing export: ' + field);
      }
      const backup = await waitJob(await request('/backups', { method: 'POST' }));
      const archive = await fetch(c.url + '/files/' + backup.backup_path, { headers: { Authorization: 'Bearer ' + c.token } });
      if (!archive.ok) throw new Error('Backup download failed');
      const archiveBytes = await archive.arrayBuffer();
      if (!archiveBytes.byteLength) throw new Error('Empty backup');
      const afterBackup = await waitJob(await request('/notes', { method: 'POST', body: JSON.stringify({ title: 'Restore sentinel', content: 'This should disappear after restore' }) }));
      const body = new FormData(); body.append('file', new Blob([archiveBytes], { type: 'application/zip' }), 'backup.zip');
      const restored = await fetch(c.url + '/restore', { method: 'POST', headers: { Authorization: 'Bearer ' + c.token }, body });
      if (!restored.ok) throw new Error('Restore submission failed');
      await waitJob(await restored.json());
      if ((await request('/documents')).some(item => item.id === afterBackup.document_id)) throw new Error('Restore did not restore the snapshot');
      return 'Desktop acceptance passed: renderer, auth, note create/update, immutable versions, FTS search, recording IPC, bundled HTML/PDF export, backup/restore';
    })()`);
    console.log(result);
    app.quit();
  }
}).catch(error => {
  console.error(error);
  backend?.kill('SIGTERM');
  if (!process.env.MYNOTES_SMOKE_TEST && !process.env.MYNOTES_LIVE_SMOKE_TEST) dialog.showErrorBox('MyNotes startup failed', error.message);
  app.exit(1);
});

app.on('second-instance', () => { win?.show(); win?.focus(); });
app.on('window-all-closed', () => app.quit());
app.on('before-quit', event => {
  if (quitting || !backend || backend.exitCode !== null) return;
  event.preventDefault();
  quitting = true;
  backend.once('exit', () => app.quit());
  backend.kill('SIGTERM');
  setTimeout(() => { backend.kill('SIGKILL'); app.quit(); }, 5000).unref();
});
