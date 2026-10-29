"""Regressions for Wirepas transport selection and image-zero activation."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from smpclient.requests.image_management import ImageStatesWrite
from smpclient.requests.os_management import ResetWrite
from smpmgr import common, main


def test_cli_help():
    result = CliRunner().invoke(main.app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "--wirepas" in result.output


def options(mtu=None, port=None, wirepas=21):
    return common.Options(10, common.TransportDefinition(port, None, wirepas), mtu)


def test_wirepas_default_mtu():
    client = common.get_smpclient(options())
    assert client._transport.mtu == 512
    assert client.address == 21


def test_serial_default_mtu_is_preserved():
    client = common.get_smpclient(options(port="/dev/test", wirepas=None))
    assert client._transport.mtu == 4096


def test_wirepas_invalid_mtu_is_a_cli_usage_error():
    result = CliRunner().invoke(main.app, ["--wirepas", "21", "--mtu", "4096", "image", "state-read"])
    assert result.exit_code == 2
    assert "Wirepas requires an MTU of 512" in result.output


@pytest.fixture
def upgrade_server(monkeypatch):
    events = []
    image_hash = bytes(range(32))
    failure = {"at": None}

    class Client:
        _address = 21

    async def connect(client, timeout):
        events.append("connect")

    async def upload(client, file, slot):
        assert file.read() == b"signed-image"
        events.append(("upload", slot))
        if failure["at"] == "upload":
            raise RuntimeError("upload failed")

    async def request(client, opts, value, description=None):
        if isinstance(value, ImageStatesWrite):
            assert value.hash == image_hash and value.confirm is False
            stage = "test"
            response = value._Response(images=[])
        else:
            assert isinstance(value, ResetWrite)
            stage = "reset"
            response = value._Response()
        events.append(stage)
        return value._ErrorV1(rc=1) if failure["at"] == stage else response

    info = SimpleNamespace(get_tlv=lambda kind: SimpleNamespace(value=image_hash))
    monkeypatch.setattr(main.ImageInfo, "load_file", lambda path: info)
    monkeypatch.setattr(main, "get_smpclient", lambda opts: Client())
    monkeypatch.setattr(main, "connect_with_spinner", connect)
    monkeypatch.setattr(main, "upload_with_progress_bar", upload)
    monkeypatch.setattr(main, "smp_request", request)
    return events, failure


@pytest.mark.parametrize("index", [0, 1])
def test_upgrade_marks_each_image_index_before_reset(tmp_path, upgrade_server, index):
    events, failure = upgrade_server
    image = tmp_path / "signed.bin"
    image.write_bytes(b"signed-image")
    args = ["--wirepas", "21", "upgrade", str(image)]
    if index:
        args += ["--slot", str(index)]
    result = CliRunner().invoke(main.app, args)
    assert result.exit_code == 0, result.output
    assert events == ["connect", ("upload", index), "test", "reset"]


@pytest.mark.parametrize("stage", ["upload", "test", "reset"])
def test_upgrade_failure_never_reports_success(tmp_path, upgrade_server, stage):
    events, failure = upgrade_server
    failure["at"] = stage
    image = tmp_path / "signed.bin"
    image.write_bytes(b"signed-image")
    result = CliRunner().invoke(main.app, ["--wirepas", "21", "upgrade", str(image)])
    assert result.exit_code == 1
    assert "Upgrade complete" not in result.output
    if stage != "reset":
        assert "reset" not in events
