"""Local-only browser-test fixture. Never used by the product entrypoint."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_bot_runtime import config, ScriptedModel
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.http import serve
root=Path(tempfile.mkdtemp(prefix='pangpang-ui-'))
app=Service(config(root),root/'data');app.probe('codex-local')
runtime=Runtime(app,ScriptedModel(app))
def work():
    while True:
        runtime.step();time.sleep(.1)
threading.Thread(target=work,daemon=True).start()
server=serve(app,'local-browser-test-token-12345678',port=8799)
print('http://127.0.0.1:8799',flush=True)
server.serve_forever()
