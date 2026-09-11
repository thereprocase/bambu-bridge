"""Recoverable native code for the owner dashboard, encrypted in the database."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class NativeCodeStore:
    def __init__(self, directory: Path):
        self.key_path = directory / "native-code.key"

    def encrypt(self, code: str) -> str:
        if self.key_path.is_symlink():
            raise ValueError("Native code key must not be a symlink")
        try:
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        self.key_path.chmod(0o600)
        return Fernet(self.key_path.read_bytes()).encrypt(code.encode()).decode()

    def decrypt(self, encrypted: str | None) -> str | None:
        if not encrypted or self.key_path.is_symlink():
            return None
        try:
            return Fernet(self.key_path.read_bytes()).decrypt(encrypted.encode()).decode()
        except (OSError, ValueError, InvalidToken):
            return None
