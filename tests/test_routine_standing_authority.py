""" B′: a Routine carries exactly the operator-approved standing authority
envelope (_authority()'s three fields); it never produces authority itself."""
import http.cookiejar
import itertools
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from test_bot_runtime import config
from pangpang.http import serve
from pangpang.service import Service
from pangpang.store import Fault, digest

DAILY = {'type': 'daily', 'time': '09:00'}
KEYS = itertools.count()


class RoutineStandingAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')
        # Strong supervision so the executor is routable on any test host; the
        # approval outcome then depends only on the Work's grant_write.
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _user_turn(self, key):
        sent = self.app.send('bot-main', {'text': '每天出日报'}, key)
        return self.app.store.get('turn', sent['turn_id'])

    def _proposal(self, **body):
        # Model path: a real user-message turn, so the routine starts pending.
        return self.app.create_routine('bot-main', dict({'instruction': '日报', 'timezone': 'UTC',
                                                        'schedule': DAILY}, **body), turn=self._user_turn('proposal-' + str(next(KEYS))))

    def _approve(self, routine_id, authority=None):
        review = self.app.routine_review(self.app.store.get('routine', routine_id))
        return self.app.approve_routine('bot-main', routine_id, expected_digest=review['approval_digest'],
                                        authority=authority)

    def _fire(self, routine_id):
        return self.app.store.get('work', self.app.run_routine_now('bot-main', routine_id)['work_id'])

    def test_model_cannot_propose_authority(self):
        r = self._proposal(authority={'grant_write': True})
        self.assertEqual(self.app.store.get('routine', r['id'])['authority'], {'grant_write': False})
        self.assertEqual(self.app.routine_review(r)['authority'], {'grant_write': False})

    def test_approval_defaults_no_standing_write_and_submit_waits_for_approval(self):
        r = self._proposal()
        approved = self._approve(r['id'])
        self.assertEqual(approved['authority'], {'grant_write': False})
        work = self._fire(r['id'])
        self.assertIs(work['grant_write'], False)
        task = self.app.submit_task('bot-main', work['id'], {'prompt': 'write report'})
        self.assertEqual(self.app.store.get('task', task['task_id'])['status'], 'awaiting_approval')
        child_bot = self.app.create_bot({'name': 'child', 'role': 'test'})
        child = self.app.delegate('bot-main', work['id'], {'target_bot_id': child_bot['id'], 'goal': 'child',
                                  'request': 'answer', 'acceptance': ['done'], 'mode': 'return_to_sender'})
        self.assertIs(self.app.store.get('work', child['work_id'])['grant_write'], False)

    def test_explicit_write_envelope_queues_write_tasks(self):
        r = self._proposal()
        self._approve(r['id'], {'grant_write': True})
        work = self._fire(r['id'])
        self.assertIs(work['grant_write'], True)
        task = self.app.submit_task('bot-main', work['id'], {'prompt': 'write report'})
        self.assertEqual(self.app.store.get('task', task['task_id'])['status'], 'queued')

    def test_no_standing_write_keeps_existing_gate_not_read_only(self):
        # grant_write=False only feeds the unchanged submit_task gate; it is
        # not a read-only Routine. A verified-read installation stays exempt from
        # per-task approval exactly as for any other Work (gate formula owned by).
        cfg = config(self.root)
        cfg['installations'][0].update(risk='read', permission_verified=True)
        app = Service(cfg, self.root / 'data-verified-read')
        try:
            app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})
            r = app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'UTC', 'schedule': DAILY})
            work = app.store.get('work', app.run_routine_now('bot-main', r['id'])['work_id'])
            self.assertIs(work['grant_write'], False)
            task = app.submit_task('bot-main', work['id'], {'prompt': 'read-only query'})
            self.assertEqual(app.store.get('task', task['task_id'])['status'], 'queued')
        finally:
            app.store.close()

    def test_operator_creation_is_explicit_and_defaults_no_standing_write(self):
        plain = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'UTC', 'schedule': DAILY})
        self.assertIs(self._fire(plain['id'])['grant_write'], False)
        write = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'UTC', 'schedule': DAILY,
                                                     'authority': {'grant_write': True}})
        self.assertEqual(write['approval_state'], 'approved')
        self.assertIs(self._fire(write['id'])['grant_write'], True)

    def test_restrictions_propagate_and_cannot_be_widened(self):
        r = self._proposal()
        self._approve(r['id'], {'grant_write': True, 'required_host_id': 'local',
                                'required_installation_id': 'inst-elsewhere'})
        work = self._fire(r['id'])
        self.assertEqual((work['required_host_id'], work['required_installation_id']), ('local', 'inst-elsewhere'))
        # codex-local is not the approved installation -> refused, not silently rerouted.
        with self.assertRaises(Fault):
            self.app.submit_task('bot-main', work['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
        child_bot = self.app.create_bot({'name': 'child', 'role': 'test'})
        child = self.app.delegate('bot-main', work['id'], {'target_bot_id': child_bot['id'], 'goal': 'child',
                                  'request': 'answer', 'acceptance': ['done'], 'mode': 'return_to_sender'})
        child_work = self.app.store.get('work', child['work_id'])
        self.assertEqual(child_work['required_installation_id'], 'inst-elsewhere')
        # A per-request restriction may only narrow, never swap the approved one.
        with self.assertRaisesRegex(Fault, 'authorization_constraint_conflict'):
            self.app.delegate('bot-main', work['id'], {'target_bot_id': child_bot['id'], 'goal': 'c2',
                              'request': 'r', 'acceptance': ['d'], 'mode': 'return_to_sender'},
                              authorization={'grant_write': True, 'required_installation_id': 'codex-local'})

    def test_any_envelope_field_change_voids_approval(self):
        for field, value in (('grant_write', True), ('required_host_id', 'other-host'),
                             ('required_installation_id', 'other-inst')):
            with self.subTest(field=field):
                r = self._proposal()
                self._approve(r['id'])
                routine = self.app.store.get('routine', r['id'])
                routine['authority'] = dict(routine['authority'], **{field: value})
                routine['next_run_at'] = 1.0
                self.app.store.put('routine', routine)
                self.assertEqual(self.app.routine_review(routine)['approval_state'], 'pending')
                with self.assertRaisesRegex(Fault, 'routine_not_approved'):
                    self.app.run_routine_now('bot-main', r['id'])
                self.assertFalse(self.app.routine_step())
                self.assertFalse(any(w.get('routine_id') == r['id'] for w in self.app.store.find('work', limit=100)))

    def test_invalid_envelope_is_rejected_without_approving(self):
        r = self._proposal()
        for bad in ({'grant_write': 'yes'}, {'grant_write': True, 'role': 'admin'},
                    {'required_host_id': 7}, {'required_installation_id': 'x' * 129}, ['grant_write']):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(Fault, 'invalid_routine_authority'):
                    self._approve(r['id'], bad)
                self.assertEqual(self.app.store.get('routine', r['id'])['approval_state'], 'pending')

    def test_model_edit_regates_and_reapproval_is_explicit(self):
        r = self._proposal()
        self._approve(r['id'], {'grant_write': True, 'required_host_id': 'local'})
        rev = self.app.store.get('routine', r['id'])['revision']
        self.app.update_routine('bot-main', r['id'], {'instruction': '改过的日报', 'revision': rev,
                                                      'authority': {'grant_write': True}}, turn=self._user_turn('edit'))
        routine = self.app.store.get('routine', r['id'])
        self.assertEqual(routine['approval_state'], 'pending')
        # The model edit could not touch authority; re-approval without a grant has no standing write.
        self.assertEqual(routine['authority'], {'grant_write': True, 'required_host_id': 'local'})
        self.assertEqual(self._approve(r['id'])['authority'], {'grant_write': False})

    def test_upgrade_voids_pre_envelope_approvals(self):
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'UTC', 'schedule': DAILY,
                                                 'authority': {'grant_write': True}})
        in_flight = self._fire(r['id'])
        legacy = self.app.store.get('routine', r['id'])
        legacy.pop('authority')
        # Exactly the previously digest: instruction + schedule + timezone + owner policy.
        legacy['approved_digest'] = digest([legacy['instruction'], legacy['schedule'], legacy['timezone'],
                                            self.app._routine_owner_policy(legacy)])
        self.app.store.put('routine', legacy)
        self.app.store.close()
        self.app = Service(config(self.root), self.root / 'data')
        saved = self.app.store.get('routine', r['id'])
        self.assertEqual((saved['approval_state'], saved['active'], saved['next_run_at']), ('pending', False, None))
        review = self.app.routine_review(saved)
        self.assertEqual(review['authority'], {'grant_write': False})
        self.assertEqual(self._approve(r['id'])['authority'], {'grant_write': False})
        # Already-materialized Work runs to completion under its frozen authority.
        self.assertIs(self.app.store.get('work', in_flight['id'])['grant_write'], True)

    def test_http_approve_carries_operator_envelope(self):
        r = self._proposal()
        server = serve(self.app, 'x' * 32, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(server.server_port)
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def post(path, body, key):
            req = urllib.request.Request(origin + '/api/v1/' + path, data=json.dumps(body).encode(),
                                         headers={'Origin': origin, 'Content-Type': 'application/json',
                                                  'Idempotency-Key': key})
            with client.open(req) as response:
                return json.load(response)
        try:
            post('login', {'token': 'x' * 32}, 'login')
            reviewed = self.app.routine_review(r)['approval_digest']
            with self.assertRaises(urllib.error.HTTPError) as error:
                post('routines/' + r['id'] + '/approve', {'digest': reviewed, 'authority': {'grant_write': 1}}, 'bad')
            self.assertEqual(error.exception.code, 400)
            approved = post('routines/' + r['id'] + '/approve',
                            {'digest': reviewed, 'authority': {'grant_write': True}}, 'ok')
            self.assertEqual(approved['authority'], {'grant_write': True})
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
