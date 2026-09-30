"""CLI failure guidance with synthetic errors and no real login or network."""

import sys

import pytest

from bili_comment_bot import __main__ as cli
from bili_comment_bot.adapters.bilibili.errors import ProtocolFault, QRLoginProtocolFault
from bili_comment_bot.config import Settings


@pytest.mark.parametrize("error_type", [QRLoginProtocolFault, ProtocolFault])
def test_login_protocol_failure_is_sanitized_and_specific(
    error_type, tmp_path, monkeypatch, capsys
):
    error = error_type()
    error.args = ("synthetic-url-secret synthetic-qr-key synthetic-cookie",)

    async def fail_auth(settings, command):
        assert command == "login"
        raise error

    monkeypatch.setattr(cli, "auth_command", fail_auth)
    monkeypatch.setattr(cli, "load_settings", lambda path: Settings(data_dir=tmp_path))
    monkeypatch.setattr(sys, "argv", ["bili-comment-bot", "login"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    captured = capsys.readouterr()
    assert caught.value.code == 2
    assert captured.out == ""
    assert "synthetic" not in captured.err
    assert error_type.__name__ in captured.err
    if error_type is QRLoginProtocolFault:
        assert "可信 HTTPS 校验" in captured.err
        assert "重新构建镜像" in captured.err
        assert "不要分享二维码、完整 URL、密钥或 Cookie" in captured.err
        assert "请重新扫码" not in captured.err
    else:
        assert "二维码生成响应" not in captured.err
        assert "检查状态" in captured.err
