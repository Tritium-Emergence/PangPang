"""Chat files: upload (paste / drag / pick), directory browsing for references, and
attached images reaching a vision planner."""
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config, call
from test_model_protocol import Endpoint
from pangpang import runner
from pangpang.context import projection
from pangpang.onboarding import PROBE_PNG
from pangpang.pi_runtime import PiModel
from pangpang.runtime import Runtime
from pangpang.service import Service, image_mime
from pangpang.store import Fault

PNG = base64.b64decode(PROBE_PNG)


def b64(raw):
    return base64.b64encode(raw).decode()


class RunnerFileActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_action(self, action, spec):
        done = subprocess.run([sys.executable, runner.__file__, action, str(Path(self.tmp.name) / "tools")],
                              input=json.dumps(spec).encode(), capture_output=True)
        return json.loads(done.stdout) if done.returncode == 0 else None

    def test_receive_writes_a_sanitized_name_into_the_dated_inbox(self):
        got = self.run_action("receive", {"path": "../../etc/passwd", "data": b64(b"x"),
                                          "allowed_roots": [str(self.root)]})
        path = Path(got["path"])
        self.assertEqual(path.parent.parent, self.root.resolve() / ".pangpang-inbox")
        self.assertTrue(path.name.endswith("-passwd"))
        self.assertEqual(path.read_bytes(), b"x")
        self.assertEqual(got["digest"], hashlib.sha256(b"x").hexdigest())

    def test_receive_refuses_oversize_and_an_inbox_link_out_of_the_roots(self):
        big = b64(b"\0" * (runner.HARVEST_MAX_FILE_BYTES + 1))
        self.assertIsNone(self.run_action("receive", {"path": "a", "data": big, "allowed_roots": [str(self.root)]}))
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        try:
            (self.root / ".pangpang-inbox").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted on this platform")
        self.assertIsNone(self.run_action("receive", {"path": "a", "data": b64(b"x"), "allowed_roots": [str(self.root)]}))
        self.assertEqual(list(outside.iterdir()), [])

    def test_list_stays_inside_the_roots(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.txt").write_text("hi")
        (self.root / "b.png").write_bytes(PNG)
        roots = self.run_action("list", {"path": "", "allowed_roots": [str(self.root)]})
        self.assertEqual(roots["entries"], [{"name": str(self.root.resolve()), "type": "dir"}])
        top = self.run_action("list", {"path": str(self.root), "allowed_roots": [str(self.root)]})
        self.assertIsNone(top["parent"])
        self.assertEqual([(e["name"], e["type"]) for e in top["entries"]], [("docs", "dir"), ("b.png", "file")])
        sub = self.run_action("list", {"path": str(self.root / "docs"), "allowed_roots": [str(self.root)]})
        self.assertEqual(sub["parent"], str(self.root.resolve()))
        self.assertEqual(sub["entries"][0]["size"], 2)
        self.assertIsNone(self.run_action("list", {"path": self.tmp.name, "allowed_roots": [str(self.root)]}))


class ServiceFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = base / "ws"
        self.ws.mkdir()
        cfg = config(base)
        cfg["hosts"][0]["allowed_roots"] = [str(self.ws)] # never overlapping the data dir
        cfg["installations"][0]["cwd"] = str(self.ws) # the executor work folder references browse within
        self.app = Service(cfg, base / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def upload(self, raw, name="shot.png"):
        return self.app.upload_file("bot-main", {"host_id": "local", "name": name, "data": b64(raw)})

    def test_upload_lands_in_the_host_inbox_and_keeps_a_model_copy_of_images(self):
        art = self.upload(PNG)
        self.assertEqual(Path(art["path"]).parent.parent, self.ws.resolve() / ".pangpang-inbox")
        self.assertEqual(Path(art["path"]).read_bytes(), PNG)
        self.assertEqual((art["source"], art["mime"], art["size"]), ("user_upload", "image/png", len(PNG)))
        self.assertEqual(self.app.model_image(art["model_image"]), PNG)
        text = self.upload(b"not an image", name="fake.png") # a name is not a type
        self.assertNotIn("model_image", text)

    def test_upload_rejects_bad_input(self):
        for body, code in (({"host_id": "local", "data": "@@"}, "invalid_upload"),
                           ({"host_id": "local", "data": ""}, "invalid_upload"),
                           ({"host_id": "nope", "data": b64(b"x")}, "host_not_found")):
            with self.assertRaises(Fault) as cm:
                self.app.upload_file("bot-main", body)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(Fault) as cm:
            self.upload(b"\0" * (runner.HARVEST_MAX_FILE_BYTES + 1))
        self.assertEqual(cm.exception.code, "upload_too_large")

    def test_browse_and_reference_an_image_by_path(self):
        (self.ws / "pic.png").write_bytes(PNG)
        roots = self.app.browse("local", {})
        self.assertEqual(roots["entries"][0]["name"], str(self.ws.resolve()))
        listing = self.app.browse("local", {"path": str(self.ws)})
        self.assertIn("pic.png", [e["name"] for e in listing["entries"]])
        art = self.app.import_file("bot-main", {"host_id": "local", "path": str(self.ws / "pic.png")})
        self.assertEqual((art["name"], art["mime"]), ("pic.png", "image/png"))
        self.assertEqual(self.app.model_image(art["model_image"]), PNG)

    def test_browse_is_confined_to_executor_work_dirs(self):
        # allowed_roots may be wider than where the executor actually works; a file
        # reference must only reach the work folder, never the rest of allowed_roots.
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.app.execution.hosts["local"]["allowed_roots"] = [str(self.ws), str(outside)] # wider than cwd
        top = self.app.browse("local", {})
        self.assertEqual([e["name"] for e in top["entries"]], [str(self.ws.resolve())])
        with self.assertRaises(Fault): # a path outside the work dir is rejected
            self.app.browse("local", {"path": str(outside)})
        # a device whose executors declare no work dir cannot be browsed at all
        self.app.execution.hosts["bare"] = {"id": "bare", "transport": "ssh", "os": "Linux",
                                            "allowed_roots": [str(self.ws)]}
        with self.assertRaises(Fault) as cm:
            self.app.browse("bare", {})
        self.assertEqual(cm.exception.code, "host_no_workspace")

    def test_model_image_ignores_a_tampered_or_unknown_copy(self):
        sha = self.upload(PNG)["model_image"]
        (self.app.data / "model-images" / sha).write_bytes(b"tampered")
        self.assertIsNone(self.app.model_image(sha))
        self.assertIsNone(self.app.model_image("../bots.db"))

    def test_projection_puts_this_turns_images_on_the_user_message_only(self):
        images = [self.upload(PNG, name="p%d.png" % i) for i in range(5)]
        doc = self.upload(b"plain text", name="notes.txt")
        sent = self.app.send("bot-main", {"text": "看图", "refs": [a["id"] for a in images] + [doc["id"]]}, "img")
        messages = projection(self.app, self.app.store.get("turn", sent["turn_id"]))
        refs = messages[-1]["images"]
        self.assertEqual(len(refs), 1) # five identical images are one picture
        self.assertEqual(refs[0]["mime"], "image/png")
        self.assertTrue(all("images" not in m for m in messages[:-1]))

    def test_pi_bridge_sends_images_only_to_a_vision_profile(self):
        sha = self.upload(PNG)["model_image"]
        history = [{"role": "user", "content": "看", "images": [{"sha": sha, "mime": "image/png"},
                                                                {"sha": "0" * 64, "mime": "image/png"}]}]
        model = PiModel(self.app)
        seen = model.with_images({"vision": True}, history)[0]["images"]
        self.assertEqual(seen, [{"data": b64(PNG), "mime": "image/png"}]) # unknown copy dropped
        self.assertNotIn("images", model.with_images({}, history)[0])


class VisionEndToEndTests(unittest.TestCase):
    """Real node Pi worker against a local OpenAI-compatible endpoint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "ws").mkdir()
        self.cfg = config(base)
        self.cfg["hosts"][0]["allowed_roots"] = [str(base / "ws")]
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def run_turn(self, vision):
        endpoint = Endpoint(lambda p, n: {"content": "看到了。"})
        try:
            cfg = json.loads(json.dumps(self.cfg))
            cfg["models"][0].update(endpoint=endpoint.url, vision=vision)
            app = Service(cfg, self.base / ("data-%s" % vision))
            try:
                art = app.upload_file("bot-main", {"host_id": "local", "name": "a.png", "data": b64(PNG)})
                sent = app.send("bot-main", {"text": "这张图是什么颜色", "refs": [art["id"]]}, "v")
                Runtime(app).step()
                self.assertEqual(app.store.get("turn", sent["turn_id"])["status"], "done")
                return endpoint.received[0]["messages"]
            finally:
                app.store.close()
        finally:
            endpoint.close()

    def test_vision_profile_receives_the_image(self):
        last = self.run_turn(True)[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("这张图是什么颜色", last["content"][0]["text"])
        self.assertEqual(last["content"][1], {"type": "image_url",
                                              "image_url": {"url": "data:image/png;base64," + b64(PNG)}})

    def test_text_profile_never_sees_image_data(self):
        payload = json.dumps(self.run_turn(False))
        self.assertNotIn("image_url", payload)
        self.assertNotIn(b64(PNG), payload)


class VisionSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Service(config(Path(self.tmp.name)), Path(self.tmp.name) / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_vision_is_verified_with_an_image_before_it_is_saved(self):
        def respond(payload, n):
            if any(isinstance(m.get("content"), list) for m in payload["messages"]):
                return call("setup_probe", value="pangpang-ready")
            return call("setup_probe", value="pangpang-ready") if n == 1 else {"content": "ready"}
        endpoint = Endpoint(respond)
        try:
            self.app.onboarding.model({"endpoint": endpoint.url, "model": "test", "no_auth": True, "vision": True})
            self.assertTrue(self.app.onboarding.status()["model"]["vision"])
            self.assertIn("image_url", json.dumps(endpoint.received[-1]))
        finally:
            endpoint.close()

    def test_an_endpoint_that_rejects_images_is_not_saved_as_vision(self):
        endpoint = Endpoint(lambda p, n: 400 if "image_url" in json.dumps(p)
                            else call("setup_probe", value="pangpang-ready") if n == 1 else {"content": "ready"})
        try:
            with self.assertRaises(Fault) as cm:
                self.app.onboarding.model({"endpoint": endpoint.url, "model": "test", "no_auth": True, "vision": True})
            self.assertEqual(cm.exception.code, "model_vision_unverified")
            self.assertFalse(self.app.onboarding.status()["model"]["vision"])
        finally:
            endpoint.close()


class ImageMimeTests(unittest.TestCase):
    def test_magic_bytes(self):
        self.assertEqual(image_mime(PNG), "image/png")
        self.assertEqual(image_mime(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(image_mime(b"RIFF\0\0\0\0WEBPVP8 "), "image/webp")
        self.assertEqual(image_mime(b"GIF89a..."), "image/gif")
        self.assertIsNone(image_mime(b"<svg/>"))


if __name__ == "__main__":
    unittest.main()
