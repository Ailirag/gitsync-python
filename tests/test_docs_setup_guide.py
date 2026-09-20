"""Руководство по установке не должно расходиться с продуктом.

Документ проверяется как исполняемый контракт: манифест из руководства разбирается настоящим
валидатором схемы, а таблица ключей сверяется с тем, что CLI действительно принимает. Иначе
инструкция для администратора тихо устареет при следующей правке кода.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pytest

from gitsync.cli import _BATCH_ENTRY_KEYS, _BATCH_TOP_KEYS, _validate_batch_config

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "setup-guide.md"
README = ROOT / "README.md"
SCRIPT = ROOT / "examples" / "sync-and-push.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_blocks(text: str) -> list[str]:
    return re.findall(r"```json\n(.*?)```", text, re.S)


def _anchor(heading: str) -> str:
    cleaned = heading.replace("`", "").strip().lower()
    kept = [
        ch for ch in cleaned
        if ch.isalnum() or ch in "-_ " or unicodedata.category(ch).startswith("M")
    ]
    return "".join(kept).replace(" ", "-")


def test_guide_exists_and_is_linked_from_readme():
    assert GUIDE.is_file()
    readme = _text(README)
    assert "docs/setup-guide.md" in readme
    # Ссылка должна быть в начале файла, а не только в конце списка документации.
    assert "docs/setup-guide.md" in readme[:1200]


def test_guide_manifest_is_accepted_by_the_real_validator(tmp_path):
    blocks = _json_blocks(_text(GUIDE))
    assert blocks, "в руководстве должен быть хотя бы один блок JSON с манифестом"
    manifest = json.loads(blocks[0])

    repository = tmp_path / "base"
    manifest["repository"] = repository.as_posix()
    entries = _validate_batch_config(manifest)

    assert len(entries) == 3, "пример охватывает основную конфигурацию и два расширения"
    names = [entry["name"] for entry in entries]
    assert names[0] == "Конфигурация"
    # Каталог источника — подкаталог общего репозитория, а не отдельная копия.
    for entry in entries:
        assert Path(entry["workdir"]).parent == repository
    # Имя расширения в 1С и имя подкаталога — разные вещи, и пример это показывает.
    extensions = [entry.get("extension") for entry in entries]
    assert extensions[0] is None
    assert all(value for value in extensions[1:])
    assert extensions[1] != entries[1]["subtree"]


def test_additional_extension_snippet_is_a_valid_entry(tmp_path):
    blocks = _json_blocks(_text(GUIDE))
    assert len(blocks) >= 2, "в руководстве должен быть пример добавления ещё одного расширения"
    extra = json.loads(blocks[1])
    manifest = json.loads(blocks[0])
    manifest["repository"] = (tmp_path / "base").as_posix()
    manifest["storages"].append(extra)

    entries = _validate_batch_config(manifest)

    assert len(entries) == 4, "добавление источника не требует ничего, кроме новой записи"
    assert entries[-1]["name"] == extra["name"]


def test_guide_documents_every_supported_manifest_key():
    text = _text(GUIDE)
    for key in sorted(_BATCH_ENTRY_KEYS | _BATCH_TOP_KEYS):
        assert f"`{key}`" in text, f"ключ {key} не описан в руководстве"


def test_guide_does_not_invent_manifest_keys():
    text = _text(GUIDE)
    table = re.findall(r"^\| `([a-z_]+)`(?: / `([a-z_]+)`)? \|", text, re.M)
    mentioned = {name for pair in table for name in pair if name}
    assert mentioned, "таблица ключей манифеста должна быть в руководстве"
    unknown = sorted(mentioned - _BATCH_ENTRY_KEYS - _BATCH_TOP_KEYS)
    assert not unknown, f"в руководстве описаны ключи, которых нет в CLI: {unknown}"


@pytest.mark.parametrize("document", [GUIDE, README])
def test_document_links_resolve(document: Path):
    text = _text(document)
    headings = {_anchor(match.group(2)) for match in re.finditer(r"^(#{1,6})\s+(.+)$", text, re.M)}
    broken: list[str] = []
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
        target = match.group(1)
        if target.startswith("http"):
            continue
        if target.startswith("#"):
            if target[1:] not in headings:
                broken.append(target)
            continue
        path, _, fragment = target.partition("#")
        resolved = (document.parent / path).resolve()
        if not resolved.exists():
            broken.append(target)
        elif fragment:
            other = _text(resolved)
            other_anchors = {
                _anchor(item.group(2)) for item in re.finditer(r"^(#{1,6})\s+(.+)$", other, re.M)
            }
            if fragment not in other_anchors:
                broken.append(target)
    assert not broken, f"битые ссылки в {document.name}: {broken}"


def test_example_script_is_documented_and_readable_by_powershell_5():
    assert SCRIPT.is_file()
    # PowerShell 5.1 читает .ps1 как ANSI, если нет BOM: кириллица в скрипте требует BOM.
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")
    body = _text(SCRIPT)
    assert "--force" not in body, "скрипт не должен уметь принудительную отправку"
    for parameter in ("-Manifest", "-GitSync", "-LogDir", "-Push", "-MinFreeGB", "-KeepLogDays"):
        assert parameter in _text(GUIDE), f"параметр {parameter} не описан в руководстве"
        assert parameter.lstrip("-") in body


def test_guide_describes_the_cleanup_the_script_actually_does():
    """Обёртка каталоги не удаляет — руководство обязано обещать ровно это."""
    guide = _text(GUIDE)
    body = _text(SCRIPT)
    assert "CleanTempDays" not in body and "CleanTempDays" not in guide, \
        "параметра удаления каталогов больше нет: имя каталога не доказывает принадлежность"
    assert "-StaleTempDays" in guide and "StaleTempDays" in body
    assert "чужие каталоги не трогает" not in guide, "обещание держалось на префиксе имени"
    # Обещание «удаляем только своё» осталось ровно для собственных журналов.
    assert "sync-ГГГГММДД.log" in guide


def test_guide_does_not_promise_that_a_bom_manifest_is_rejected():
    """CLI читает манифест как utf-8-sig, значит BOM в манифесте — не отказ."""
    guide = _text(GUIDE)
    assert "Unexpected UTF-8 BOM" not in guide, "такой ошибки инструмент больше не выдаёт"
    body = _text(SCRIPT)
    assert "сохранён с BOM" not in body, "обёртка не должна отвергать то, что принимает CLI"
