"""Redact injected credentials across arbitrary stdout chunk boundaries."""
class Redactor:
    def __init__(self, secrets):
        self.secrets = sorted({s.encode() for s in secrets if s}, key=len, reverse=True)
        self.keep = max([len(s) - 1 for s in self.secrets] + [0])
        self.buffer = b""

    def feed(self, block, final=False):
        self.buffer += block
        cut = len(self.buffer) if final else max(0, len(self.buffer) - self.keep)
        for secret in self.secrets:
            start = self.buffer.find(secret)
            while start >= 0:
                if start < cut < start + len(secret):
                    cut = start
                start = self.buffer.find(secret, start + 1)
        ready, self.buffer = self.buffer[:cut], self.buffer[cut:]
        for secret in self.secrets:
            ready = ready.replace(secret, b"[REDACTED]")
        return ready
