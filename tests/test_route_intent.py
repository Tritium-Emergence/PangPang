import copy
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service


class RouteIntentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')
        self.app.probe('codex-local') # make it routable
        self.work = self.app.store.get('work', self.app.send('bot-main', {'text': 'x'}, 'r')['work_id'])

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_adapter_name_resolves_to_installation(self):
        # The model passes the adapter name "codex" (what it sees in the UI/catalog),
        # not the internal id "codex-local". route() must resolve it and select.
        r = self.app.route('bot-main', self.work, {'installation_id': 'codex'})
        self.assertIsNotNone(r['selected'])
        self.assertEqual(r['selected']['installation_id'], 'codex-local')

    def test_real_id_still_selects(self):
        # No regression: the exact internal id keeps working.
        r = self.app.route('bot-main', self.work, {'installation_id': 'codex-local'})
        self.assertIsNotNone(r['selected'])
        self.assertEqual(r['selected']['installation_id'], 'codex-local')

    def test_nonexistent_name_rejected(self):
        r = self.app.route('bot-main', self.work, {'installation_id': 'no-such-thing'})
        self.assertIsNone(r['selected'])

    def test_host_name_resolves(self):
        # host_id given as the host's display name resolves to the real host id.
        host = self.app.execution.hosts['local']
        host['name'] = '我的本机'
        self.app.execution.hosts['local'] = host
        r = self.app.route('bot-main', self.work, {'host_id': '我的本机'})
        self.assertIsNotNone(r['selected'])
        self.assertEqual(r['selected']['host_id'], 'local')

    def test_ambiguous_adapter_across_hosts_not_guessed(self):
        # Same adapter on two hosts -> adapter name alone is ambiguous -> fail-safe:
        # not resolved (no selection), until a host_id disambiguates.
        cfg = copy.deepcopy(self.app.config)
        cfg['hosts'].append(dict(cfg['hosts'][0], id='host2')) # same test-host policy as 'local'
        cfg['installations'].append({'id': 'codex-host2', 'host_id': 'host2', 'adapter': 'codex',
                                     'argv': [__import__('sys').executable, '-c', 'print(1)'],
                                     'probe_argv': [__import__('sys').executable, '--version'],
                                     'cwd': str(self.root), 'risk': 'write'})
        app2 = Service(cfg, self.root / 'data2')
        try:
            app2.probe('codex-local'); app2.probe('codex-host2')
            w = app2.store.get('work', app2.send('bot-main', {'text': 'y'}, 'r2')['work_id'])
            # ambiguous adapter name -> not resolved -> no selection
            r = app2.route('bot-main', w, {'installation_id': 'codex'})
            self.assertIsNone(r['selected'])
            # disambiguate with host_id -> resolves
            r2 = app2.route('bot-main', w, {'installation_id': 'codex', 'host_id': 'host2'})
            self.assertIsNotNone(r2['selected'])
            self.assertEqual(r2['selected']['installation_id'], 'codex-host2')
        finally:
            app2.store.close()


if __name__ == '__main__':
    unittest.main()
