from stop.cli import main


def test_main_once_fixture(capsys):
    from pathlib import Path

    fixture = str(Path(__file__).parent / "fixtures" / "basic")
    assert main(["--once", "--fixture", fixture]) == 0
    out = capsys.readouterr().out
    assert "athena" in out
    assert "bramwell" in out
    assert "unmanaged" in out
    assert "System" in out


def test_version(capsys):
    try:
        main(["--version"])
    except SystemExit as e:
        assert e.code == 0
