"""Isolated browser acceptance; does not touch the running user instance."""
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from run_sandbox_unit import test_mkdir, test_directory
from pangpang.service import Service
from pangpang.http import serve
from test_bot_runtime import config

with patch('os.mkdir', test_mkdir), patch('pangpang.service.secure_directory', test_directory), patch('pangpang.vault.secure_directory', test_directory):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        app = Service(config(root), root / 'data')
        r = app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'UTC',
            'schedule': {'type': 'interval', 'minutes': 5}})
        r.update(approval_state='pending', active=False, approved_digest=None, next_run_at=None)
        app.store.put('routine', r)
        server = serve(app, 'x' * 32, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(['node', 'tests/routine_ui.mjs',
                'http://127.0.0.1:' + str(server.server_port), r['id']], timeout=60)
        finally:
            server.shutdown(); server.server_close(); thread.join(); app.store.close()
        raise SystemExit(result.returncode)
