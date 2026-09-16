"""
crypto_utils.py — xifrat/desxifrat compatible amb CryptoJS.AES.encrypt(text, pass)

CryptoJS, per defecte, fa servir el format "OpenSSL salted": deriva la clau
(32 bytes) i el IV (16 bytes) d'una contrasenya via EVP_BytesToKey (MD5,
com el vell `openssl enc -aes-256-cbc` sense -pbkdf2), i xifra amb
AES-256-CBC + padding PKCS7. La sortida és:

    base64( b"Salted__" + salt(8 bytes) + ciphertext )

Aquest mòdul reprodueix exactament aquest format en Python (pycryptodome),
perquè el que es xifra aquí es pugui desxifrar al navegador amb
CryptoJS.AES.decrypt(text, pass).toString(CryptoJS.enc.Utf8), i viceversa.
"""

import base64
import hashlib
import os

from Crypto.Cipher import AES

_SALT_PREFIX = b"Salted__"
_KEY_LEN = 32  # AES-256
_IV_LEN = 16


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    d = b""
    dtot = b""
    while len(dtot) < key_len + iv_len:
        d = hashlib.md5(d + password + salt).digest()
        dtot += d
    return dtot[:key_len], dtot[key_len : key_len + iv_len]


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes) -> bytes:
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("Padding invalid (contrasenya incorrecta o dades corruptes)")
    return data[:-pad_len]


def encrypt_str(plaintext: str, passphrase: str) -> str:
    salt = os.urandom(8)
    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt, _KEY_LEN, _IV_LEN)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(_pkcs7_pad(plaintext.encode("utf-8")))
    return base64.b64encode(_SALT_PREFIX + salt + ct).decode("ascii")


def decrypt_str(ciphertext_b64: str, passphrase: str) -> str:
    raw = base64.b64decode(ciphertext_b64)
    if raw[:8] != _SALT_PREFIX:
        raise ValueError("Format invalid: no comença amb 'Salted__'")
    salt = raw[8:16]
    ct = raw[16:]
    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt, _KEY_LEN, _IV_LEN)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pt = _pkcs7_unpad(cipher.decrypt(ct))
    return pt.decode("utf-8")
