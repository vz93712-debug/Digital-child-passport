"""Ключи детей и crypto-shredding.

У каждого ребёнка свой ключ AES-256-GCM. Им шифруются payload событий журнала и
свободный текст (наблюдения наставника). Удалить ключ = сделать все копии данных
нечитаемыми, включая бэкапы, которые физически стереть нельзя.
В продакшене — отдельное хранилище ключей (KMS/HSM в РФ), ключи не попадают в бэкапы БД.
"""
from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyShredded(Exception):
    """Ключ ребёнка уничтожен: данные прочитать невозможно."""


class KeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}
        self.shredded: set[str] = set()

    def create(self, child_id: str) -> None:
        if child_id in self.shredded:
            raise KeyShredded(child_id)
        self._keys.setdefault(child_id, AESGCM.generate_key(bit_length=256))

    def encrypt(self, child_id: str, plaintext: bytes) -> bytes:
        key = self._key(child_id)
        nonce = os.urandom(12)
        # child_id как associated data: шифртекст нельзя «пересадить» другому ребёнку
        return nonce + AESGCM(key).encrypt(nonce, plaintext, child_id.encode())

    def decrypt(self, child_id: str, blob: bytes) -> bytes:
        key = self._key(child_id)
        return AESGCM(key).decrypt(blob[:12], blob[12:], child_id.encode())

    def shred(self, child_id: str) -> None:
        self._keys.pop(child_id, None)
        self.shredded.add(child_id)

    def _key(self, child_id: str) -> bytes:
        try:
            return self._keys[child_id]
        except KeyError:
            raise KeyShredded(child_id) from None
