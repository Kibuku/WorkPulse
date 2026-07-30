from pathlib import Path

from workpulse.signals import watcher


def test_windows_ignores_macos_cloud_root_and_adds_onedrive(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "_expand_root", lambda value: Path(value))
    one_drive = tmp_path / "OneDrive - University"

    roots = watcher._watch_roots(
        ["~/Documents", "~/Library/CloudStorage"],
        platform="win32",
        environ={"OneDriveCommercial": str(one_drive), "OneDrive": str(one_drive)},
    )

    assert Path("~/Documents") in roots
    assert Path("~/Library/CloudStorage") not in roots
    assert roots.count(one_drive.resolve()) == 1


def test_macos_keeps_cloud_storage(monkeypatch):
    monkeypatch.setattr(watcher, "_expand_root", lambda value: Path(value))

    roots = watcher._watch_roots(
        ["~/Documents", "~/Library/CloudStorage"],
        platform="darwin",
        environ={"OneDrive": r"C:\Users\Person\OneDrive"},
    )

    assert roots == [Path("~/Documents"), Path("~/Library/CloudStorage")]
