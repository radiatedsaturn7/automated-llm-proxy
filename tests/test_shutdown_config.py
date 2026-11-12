import sys

import pytest

from gateway import cli


@pytest.mark.parametrize('env,args,expected', [(None, [], 360), ('22', [], 22), ('22', ['--shutdown-grace-seconds', '3'], 3)])
def test_cli_passes_bounded_shutdown_window(monkeypatch, env, args, expected):
    monkeypatch.delenv('SHUTDOWN_GRACE_SECONDS', raising=False)
    if env is not None:
        monkeypatch.setenv('SHUTDOWN_GRACE_SECONDS', env)
    monkeypatch.setattr(sys, 'argv', ['llm-gateway', *args])
    called = []
    monkeypatch.setattr(cli.uvicorn, 'run', lambda *a, **kw: called.append(kw))
    cli.main()
    assert called[0]['timeout_graceful_shutdown'] == expected
    assert called[0]['access_log'] is False and called[0]['proxy_headers'] is False


@pytest.mark.parametrize('value', ['0', '-1', '3601', 'nan', 'inf', '0.5', 'bad'])
def test_cli_invalid_shutdown_environment_fails_before_start(monkeypatch, value):
    monkeypatch.setenv('SHUTDOWN_GRACE_SECONDS', value)
    monkeypatch.setattr(sys, 'argv', ['llm-gateway'])
    called = []
    monkeypatch.setattr(cli.uvicorn, 'run', lambda *a, **kw: called.append(kw))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and not called
