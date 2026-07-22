import multiprocessing

from backend.game_lock import game_lock_path, game_write_lock


def _lock_worker(root: str, entered, release, result) -> None:
    try:
        with game_write_lock(root, timeout=3):
            entered.set()
            release.wait(3)
        result.put("ok")
    except BaseException as error:
        result.put(f"{type(error).__name__}: {error}")


def test_preexisting_lock_file_does_not_block_and_is_not_deleted(tmp_path):
    lock_file = game_lock_path(tmp_path)
    lock_file.write_text("left by crashed process", encoding="utf-8")

    with game_write_lock(tmp_path, timeout=0.2):
        assert lock_file.is_file()

    assert lock_file.read_text(encoding="utf-8") == "left by crashed process"


def test_cross_process_lock_is_mutually_exclusive_and_persistent(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered_first = context.Event()
    entered_second = context.Event()
    release_first = context.Event()
    release_second = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_lock_worker,
        args=(str(tmp_path), entered_first, release_first, results),
    )
    second = context.Process(
        target=_lock_worker,
        args=(str(tmp_path), entered_second, release_second, results),
    )
    first.start()
    assert entered_first.wait(3)
    second.start()
    assert not entered_second.wait(0.3)
    release_first.set()
    assert entered_second.wait(3)
    release_second.set()
    first.join(3)
    second.join(3)

    assert first.exitcode == second.exitcode == 0
    assert sorted([results.get(timeout=1), results.get(timeout=1)]) == ["ok", "ok"]
    assert game_lock_path(tmp_path).is_file()
