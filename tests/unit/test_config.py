from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from blackholememory.config import Settings


def test_embedding_defaults_without_environment(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("BHM_MEM0_EMBEDDING_MODEL", None)
    environment.pop("BHM_MEM0_EMBEDDING_DIMS", None)
    environment["HOME"] = str(tmp_path)
    environment["USERPROFILE"] = str(tmp_path)
    code = (
        "import json\n"
        "from blackholememory.config import Settings\n"
        "settings = Settings(_env_file=None)\n"
        "print(json.dumps({'model': settings.mem0_embedding_model, 'dims': settings.mem0_embedding_dims}))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"model": "text-embedding-bge-m3", "dims": 1024}


def test_embedding_dimensions_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("BHM_MEM0_EMBEDDING_DIMS", "2048")

    assert Settings(_env_file=None).mem0_embedding_dims == 2048
