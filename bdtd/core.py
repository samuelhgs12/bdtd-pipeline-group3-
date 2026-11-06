"""Utilitários comuns: horário UTC, SHA-256 e escrita atômica."""
import hashlib
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Um nome exclusivo evita que dois exportadores escrevam no mesmo .tmp.
    # O handle é fechado antes de os.replace, exigência importante no Windows.
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',newline='',delete=False,
                                     dir=path.parent,prefix=path.name+'.',suffix='.tmp') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
        temp=Path(stream.name)
    for attempt in range(6):
        try:
            os.replace(temp,path)
            return
        except PermissionError:
            if attempt==5:
                # Mantém o temporário completo para inspeção/recuperação.
                raise
            time.sleep(0.05*(2**attempt))
