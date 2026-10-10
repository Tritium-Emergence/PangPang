import logging
import unittest
from pangpang import logs


class LogsTests(unittest.TestCase):
    def test_get_logger_is_pangpang_child(self):
        lg = logs.get_logger("unit")
        self.assertEqual(lg.name, "pangpang.unit")

    def test_configure_logging_is_idempotent(self):
        # Reset module state so we can assert handler count deterministically.
        logs._configured = False
        root = logging.getLogger("pangpang")
        root.handlers.clear()
        logs.configure_logging()
        first = len(root.handlers)
        logs.configure_logging()
        self.assertEqual(len(root.handlers), first)
        self.assertGreaterEqual(first, 1)

    def test_safe_detail_flattens_and_truncates(self):
        from pangpang.pi_runtime import _safe_detail
        self.assertIsNone(_safe_detail(None))
        self.assertIsNone(_safe_detail(""))
        # newlines/控制字符压平，避免多行污染日志与回显正文
        self.assertEqual(_safe_detail("400 bad\nrequest\r\n x"), "400 bad request x")
        long = _safe_detail("a" * 300)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(len(long), 201)


if __name__ == "__main__":
    unittest.main()
