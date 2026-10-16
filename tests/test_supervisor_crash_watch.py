import importlib.util
import xmlrpc.client
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "deployment" / "supervisor_crash_watch.py"
_spec = importlib.util.spec_from_file_location("supervisor_crash_watch", _PATH)
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)


def test_counts_only_exits_inside_the_window():
    c = cw.CrashCounter(max_crashes=3, window_s=600)
    assert c.record("gateway", 0) == 1
    assert c.record("gateway", 100) == 2
    assert c.record("gateway", 700) == 2  # the exit at t=0 fell out of the window
    assert c.record("gateway", 750) == 2  # so did t=100
    assert c.record("gateway", 760) == 3


def test_services_are_counted_separately_and_reset_clears_one():
    c = cw.CrashCounter(max_crashes=3, window_s=600)
    c.record("gateway", 0)
    c.record("gateway", 1)
    assert c.record("webcall", 2) == 1
    c.reset("gateway")
    assert c.record("gateway", 3) == 1


def test_parses_supervisor_header_and_payload_tokens():
    header = cw.parse_tokens("ver:3.0 server:supervisor serial:21 pool:crash-watch poolserial:10 "
                             "eventname:PROCESS_STATE_EXITED len:84\n")
    assert header["eventname"] == "PROCESS_STATE_EXITED" and header["len"] == "84"
    event = cw.parse_tokens("processname:gateway groupname:gateway from_state:RUNNING expected:0 pid:2766")
    assert event["processname"] == "gateway" and event["expected"] == "0"


class _FakeSupervisor:
    def __init__(self, states):
        self._states = list(states)
        self.stopped = []

    def getProcessInfo(self, name):
        return {"statename": self._states.pop(0) if len(self._states) > 1 else self._states[0]}

    def stopProcess(self, name, wait):
        self.stopped.append((name, wait))
        return True


class _FakeProxy:
    def __init__(self, states):
        self.supervisor = _FakeSupervisor(states)


def test_waits_out_the_exited_state_then_stops_the_restart():
    proxy = _FakeProxy(["EXITED", "EXITED", "STARTING"])
    assert cw.stop_service(proxy, "gateway", settle_s=5)
    assert proxy.supervisor.stopped == [("gateway", False)]


def test_retries_when_stop_races_a_state_change():
    proxy = _FakeProxy(["BACKOFF", "STARTING"])
    calls = []

    def flaky_stop(name, wait):
        calls.append(name)
        if len(calls) == 1:
            raise xmlrpc.client.Fault(70, "NOT_RUNNING")
        return True

    proxy.supervisor.stopProcess = flaky_stop
    assert cw.stop_service(proxy, "gateway", settle_s=5)
    assert len(calls) == 2


def test_already_stopped_needs_no_stop_call():
    proxy = _FakeProxy(["FATAL"])
    assert cw.stop_service(proxy, "gateway", settle_s=1)
    assert proxy.supervisor.stopped == []
