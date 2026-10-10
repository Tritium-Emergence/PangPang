"""First-run installer and launcher. Run: python launch.py cloud|connector [options].

The documented pip entry is ``python -m pangpang.cli`` (:func:`pangpang.cli.main`).
pip may also install ``pangpang`` / ``pangpang.exe``; use ``-m`` if Windows blocks that exe.
This file remains the source-checkout and Release-zip entry and still bootstraps a virtualenv.
"""
from pangpang.cli import ensure_model_runtime, resolve_access_token, run_frozen, source_main as main

if __name__ == "__main__":
    raise SystemExit(main())
