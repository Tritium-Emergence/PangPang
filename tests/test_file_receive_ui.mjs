// The devices page binds 允许接收文件 to the connector's reported flag.
// Missing is off. The control posts to the host file-receive route.
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import zhCN from '../web-src/i18n/zh-CN.ts';
import enUS from '../web-src/i18n/en-US.ts';

const hosts = readFileSync(new URL('../web-src/components/HostsView.tsx', import.meta.url), 'utf8');

test('both locales name the switch and the file-receive error points at it', () => {
  for (const dict of [zhCN, enUS]) {
    for (const key of ['allowFileReceive', 'allowFileReceiveHint', 'fileReceiveSaved', 'fileReceivePending'])
      assert.equal(typeof dict.hosts[key], 'string', `hosts.${key}`);
    for (const code of ['local_file_receive_disabled', 'file_receive_connector_only',
      'invalid_allow_file_receive', 'allow_file_receive_required', 'device_action_rejected'])
      assert.equal(typeof dict.errors[code], 'string', `errors.${code}`);
    assert.equal(dict.errors.local_file_receive_disabled.includes('restart'), false);
    assert.equal(dict.errors.local_file_receive_disabled.includes('配置连接器'), false);
  }
  assert.match(zhCN.hosts.allowFileReceive, /允许接收文件/);
  assert.match(zhCN.errors.local_file_receive_disabled, /允许接收文件/);
  assert.match(zhCN.errors.local_file_receive_disabled, /我的电脑/);
  assert.match(enUS.hosts.allowFileReceive, /Allow file receive/);
  assert.match(enUS.errors.local_file_receive_disabled, /Allow file receive/);
  assert.match(enUS.errors.local_file_receive_disabled, /My computers/);
});

test('the control is the connector flag, missing counts as off, and it posts the route', () => {
  assert.match(hosts, /allow_file_receive === true/);
  assert.match(hosts, /hosts\/\$\{host\.id\}\/file-receive/);
  assert.match(hosts, /transport === 'connector'/);
  assert.match(hosts, /host-file-receive-/);
  assert.equal(hosts.includes('Configure the connector'), false);
});
