"""Пример плагина: печатает номер версии и SHA после каждого коммита."""


def register(host):
    host.subscribe("after_history", on_history)
    host.subscribe("after_commit", on_commit)


def on_history(history, current_version):
    print(f"К обработке версий: {len(history)} (текущая в git: {current_version})")


def on_commit(version, work_dir, sha):
    print(f"{version.number}: {version.author} -> {sha}")
