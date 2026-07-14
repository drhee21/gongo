# -*- coding: utf-8 -*-
"""비밀번호 해시, API 키 암호화, 세션 토큰 발급을 담당한다."""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Tuple

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parent
SECRET_KEY_PATH = ROOT / "data" / "secret.key"

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> Tuple[str, str]:
    """비밀번호를 해시하고 (salt_hex, hash_hex)를 반환한다."""
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, digest_hex = hash_password(password, salt)
    return secrets.compare_digest(digest_hex, hash_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def _load_or_create_fernet_key() -> bytes:
    env_key = os.environ.get("APP_SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8") if not isinstance(env_key, bytes) else env_key
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes().strip()
    SECRET_KEY_PATH.parent.mkdir(exist_ok=True)
    key = Fernet.generate_key()
    SECRET_KEY_PATH.write_bytes(key)
    return key


def _fernet() -> Fernet:
    key = _load_or_create_fernet_key()
    try:
        return Fernet(key)
    except ValueError as e:
        raise RuntimeError(
            "APP_SECRET_KEY가 올바른 Fernet 키 형식이 아닙니다. "
            "Python에서 `from cryptography.fernet import Fernet; Fernet.generate_key()`로 생성한 값을 사용하세요."
        ) from e


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError("저장된 API 키를 복호화하지 못했습니다. 키를 다시 등록해주세요.") from e
