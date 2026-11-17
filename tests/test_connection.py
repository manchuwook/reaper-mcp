"""Connection handling: connect once, and recover when REAPER is restarted."""

import pytest
import reapy  # the fake installed by conftest

from reaper_mcp import connection


@pytest.fixture(autouse=True)
def fresh_connection(monkeypatch):
    monkeypatch.setattr(connection, "_connected", False)
    for mock in (reapy.connect, reapy.reconnect):
        mock.reset_mock(return_value=True, side_effect=True)
    yield
    for mock in (reapy.connect, reapy.reconnect):
        mock.reset_mock(return_value=True, side_effect=True)


def test_the_first_call_connects(rpr):
    connection.ensure_connected()
    reapy.connect.assert_called_once_with()
    reapy.reconnect.assert_not_called()


def test_a_live_connection_is_left_alone(rpr):
    connection.ensure_connected()
    connection.ensure_connected()
    reapy.connect.assert_called_once_with()
    reapy.reconnect.assert_not_called()


def test_it_reconnects_after_reaper_is_restarted(rpr):
    # Given a working connection
    connection.ensure_connected()
    # When REAPER restarts: the old socket fails once, the new one answers
    rpr.GetAppVersion.side_effect = [RuntimeError("socket closed"), "7.84/win64"]
    connection.ensure_connected()
    # Then the dead client is replaced instead of failing every later call
    reapy.reconnect.assert_called_once_with()


def test_a_reconnect_that_still_gets_no_answer_is_an_error(rpr):
    connection.ensure_connected()
    rpr.GetAppVersion.side_effect = RuntimeError("still down")
    with pytest.raises(RuntimeError, match="Cannot connect to REAPER"):
        connection.ensure_connected()


def test_an_unreachable_reaper_gets_a_helpful_error(rpr):
    reapy.connect.side_effect = ConnectionRefusedError("refused")
    with pytest.raises(RuntimeError, match="distant API"):
        connection.ensure_connected()
