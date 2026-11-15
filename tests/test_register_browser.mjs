// The register button is the submission. The scope is on the card before the click.
// Not clicking does not POST. A dialog is not part of this action.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import test from 'node:test';
import {chromium} from 'playwright';

const PORT = 8792;
const TOKEN = 'browser-access-token-32-characters';
const SERVER = `
from pathlib import Path
from pangpang.http import serve
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Store, uid
from pangpang.vault import Vault

root = Path("/tmp/pp-register-browser")
(root / "workspace").mkdir(parents=True, exist_ok=True)
app = Service(empty_config(), root / "cloud")
data = root / "device"
data.mkdir(exist_ok=True)
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
print("READY " + pair["host_id"], flush=True)
serve(app, ${JSON.stringify(TOKEN)}, port=${PORT}).serve_forever()
`;

function startServer() {
  const child = spawn('python3', ['-c', SERVER], {stdio: ['ignore', 'pipe', 'pipe']});
  let hostId = '';
  let log = '';
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server did not start: ' + log)), 20000);
    const take = (chunk) => {
      log += String(chunk);
      const match = log.match(/READY (\S+)/);
      if (match && !hostId) {
        hostId = match[1];
        clearTimeout(timer);
        resolve(hostId);
      }
    };
    child.stdout.on('data', take);
    child.stderr.on('data', take);
    child.on('exit', (code) => {
      if (!hostId) reject(new Error('server exited ' + code + ': ' + log));
    });
  });
  return {child, ready};
}

test('the register button submits the disclosed default scope and does not open a dialog', async () => {
  const {child, ready} = startServer();
  let browser;
  try {
    const hostId = await ready;
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
    await page.goto('http://127.0.0.1:' + PORT + '/');
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
    await page.screenshot({path: '/opt/cursor/artifacts/register-disclosed.png', fullPage: true});
    await page.getByTestId('register-executor').click();
    await page.getByText('正在这台电脑上登记').waitFor();
    assert.deepEqual(dialogs, []);
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], {host_id: hostId, action: 'configure', adapter: 'codex'});
    const after = await page.evaluate(async () => (await fetch('/api/v1/snapshot')).json());
    const requests = after.setup.requests || [];
    assert.equal(requests.length, 1);
    assert.equal(requests[0].status, 'configuring');
    assert.equal(requests[0].origin, 'operator');
    assert.equal(requests[0].proposal.auth, 'existing');
    assert.equal(requests[0].proposal.endpoint, '');
    assert.equal(Object.hasOwn(requests[0].proposal, 'cwd'), false);
    await page.screenshot({path: '/opt/cursor/artifacts/register-submitted.png', fullPage: true});
  } finally {
    if (browser) await browser.close();
    child.kill();
  }
});
