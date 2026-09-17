from stop.cli import main


def test_main_runs(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "stop" in out


def test_fixture_flag(capsys):
    assert main(["--fixture", "/nowhere"]) == 0
    assert "fixture:/nowhere" in capsys.readouterr().out
