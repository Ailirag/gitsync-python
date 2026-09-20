"""Провенанс сборки: что именно попало в колесо.

Печатается в журнал сборки, чтобы приёмка могла сверить содержимое готового
образа с исходным деревом, не разбирая слои.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys


def main() -> int:
    for item in sorted(pathlib.Path("/wheels").iterdir()):
        print(f"PROVENANCE wheel {item.name} sha256={hashlib.sha256(item.read_bytes()).hexdigest()}")
    cfe = pathlib.Path("src/gitsync/data/tempExtension.cfe").read_bytes()
    print(f"PROVENANCE tempExtension.cfe size={len(cfe)} "
          f"sha256={hashlib.sha256(cfe).hexdigest()}")
    print(f"PROVENANCE python {sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
