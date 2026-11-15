// The register button is the submission. Each run gets its own data directory and port.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {rm} from 'node:fs/promises';
import test from 'node:test';
import {chromium} from 'playwright';

const TOKEN = 'browser-access-token-32-characters';
const SERVER = `
import tempfile
from pathlib import Path
from pangpang.http import serve
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Store, uid
from pangpang.vault import Vault

root = Path(tempfile.mkdtemp(prefix="pp-register-"))
(root / "workspace").mkdir()
app = Service(empty_config(), root / "cloud")
data = root / "device"
data.mkdir()
store = Store(data / "connector.db")
identity = uid("device")
store.put("identity", {"id": "device", "instance_id": identity})
store.close()
vault = Vault(data / "vault")
code = app.onboarding.pairing_code()["code"]
pair = app.onboarding.pair({"code": code, "instance_id": identity, "public_key": vault.public_key(),
                            "name": "工作室电脑", "os": "Linux"})
vault.close()
app.store.put("inventory", {"id": pair["host_id"], "items": [
    {"adapter": "codex", "binary": "codex", "version": "0.98.0"}]})
server = serve(app, ${JSON.stringify(TOKEN)}, port=0)
print("READY " + pair["host_id"] + " " + str(server.server_port) + " " + str(root), flush=True)
server.serve_forever()
`;

function startServer() {
  const child = spawn('python3', ['-c', SERVER], {stdio: ['ignore', 'pipe', 'pipe']});
  let heard = null;
  let log = '';
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server did not start: ' + log)), 20000);
    const take = (chunk) => {
      log += String(chunk);
      const match = log.match(/READY (\S+) (\d+) (\S+)/);
      if (match && !heard) {
        heard = {hostId: match[1], port: match[2], root: match[3]};
        clearTimeout(timer);
        resolve(heard);
      }
    };
    child.stdout.on('data', take);
    child.stderr.on('data', take);
    child.on('exit', (code) => {
      if (!heard) reject(new Error('server exited ' + code + ': ' + log));
    });
  });
  return {child, ready};
}

async function registerOnce() {
  const {child, ready} = startServer();
  let browser;
  let root = '';
  try {
    const started = await ready;
    root = started.root;
    browser = await chromium.launch({executablePath: '/opt/google/chrome/chrome', headless: true});
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const dialogs = [];
    const posts = [];
    page.on('dialog', (dialog) => {
      dialogs.push(dialog.message());
      dialog.dismiss();
    });
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().includes('/api/v1/setup/request'))
        posts.push(request.postDataJSON());
    });
    await page.addInitScript(() => localStorage.setItem('pangpang.uiLocale.v1', 'zh-CN'));
    await page.goto('http://127.0.0.1:' + started.port + '/');
    await page.locator('#token').fill(TOKEN);
    await page.locator('#login-form button').click();
    await page.locator('[data-view="hosts"]').click();
    const scope = page.getByTestId('register-scope');
    await scope.waitFor();
    const disclosed = await scope.innerText();
    assert.match(disclosed, /默认工作目录/);
    assert.match(disclosed, /不证明该程序已经登录/);
    assert.match(disclosed, /不会核对它是否仍一致/);
    assert.match(disclosed, /不会因此允许它执行任务/);
    assert.match(disclosed, /不点这个按钮就不会登记/);
    const before = await page.evaluate(async () => (await fetch('/api/v1/snapshot')).json());
    assert.equal((before.setup.requests || []).length, 0);
    assert.equal(posts.length, 0);
    assert.deepEqual(dialogs, []);
    await page.getByTestId('register-executor').click();
    await page.getByText('正在这台电脑上登记').waitFor();
    assert.deepEqual(dialogs, []);
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], {host_id: started.hostId, action: 'configure', adapter: 'codex'});
    const after = await page.evaluate(async () => (await fetch('/api/v1/snapshot')).json());
    const requests = after.setup.requests || [];
    assert.equal(requests.length, 1);
    assert.equal(requests[0].status, 'configuring');
    assert.equal(requests[0].origin, 'operator');
    assert.equal(requests[0].proposal.auth, 'existing');
    assert.equal(requests[0].proposal.endpoint, '');
    assert.equal(Object.hasOwn(requests[0].proposal, 'cwd'), false);
    return {port: started.port, root};
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM');
      await Promise.race([
        new Promise((resolve) => child.once('exit', resolve)),
        new Promise((resolve) => setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 2000)),
      ]);
    }
    if (root) await rm(root, {recursive: true, force: true});
  }
}

test('register click, first isolated run', async () => {
  const result = await registerOnce();
  assert.match(result.root, /pp-register-/);
  assert.match(result.port, /^[0-9]+$/);
});

test('register click, second isolated run', async () => {
  const result = await registerOnce();
  assert.match(result.root, /pp-register-/);
  assert.match(result.port, /^[0-9]+$/);
});
