#!/usr/bin/env python3
"""nym.py - keep the browsing of anti.py inside NymVPN.

Talks to the NymVPN service (nym-vpnd, installed by the NymVPN app) through
NymVPN's official command-line client, nym-vpnc:

    python3 nym.py              show the tunnel state
    python3 nym.py --selftest

The NymVPN app for Windows does not include nym-vpnc. Take nym-vpnc.exe from
nym-vpn-core-<version>_windows_x86_64.zip of the same NymVPN release
(github.com/nymtech/nym-vpn-client/releases) and put it in the data folder, or
point NYM_VPNC at it. On macOS install the nym-vpnc .pkg, on Linux the .deb.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import persona

EXE = "nym-vpnc.exe" if sys.platform == "win32" else "nym-vpnc"
STATE = re.compile(r"^State:\s*(.+)$", re.M)
HOWTO = (
    f"Install NymVPN (nym.com/download), then get nym-vpnc. Windows: take {EXE} from the nym-vpn-core zip of the "
    f"same NymVPN release (github.com/nymtech/nym-vpn-client/releases) and put it in {persona.DATA}. "
    "macOS: install the nym-vpnc .pkg. Linux: install the nym-vpnc .deb. Or set NYM_VPNC to its path."
)


class NymError(Exception):
    """NymVPN is required but not usable: the run stops before anything goes online."""


def find_cli():
    """nym-vpnc from $NYM_VPNC, the PATH, the data folder, next to the scripts, or the usual install folders."""
    places = [os.environ.get("NYM_VPNC"), shutil.which("nym-vpnc"), persona.DATA / EXE, persona.ROOT / EXE]
    if sys.platform == "win32":
        places += [Path(os.environ[v]) / "NymVPN" / EXE for v in ("PROGRAMFILES", "LOCALAPPDATA") if os.environ.get(v)]
    else:
        places += [Path(d) / EXE for d in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin")]
    return next((str(p) for p in places if p and Path(p).is_file()), None)


def _run(cli, *args, timeout=10):
    """(exit code, output). A missing, broken or hanging nym-vpnc is just a failed call."""
    hide = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        p = subprocess.run([cli, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, **hide)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def status(cli=None):
    """{'cli', 'state', 'connected'}; state is nym-vpnc's own words, e.g. 'Connected wg to 1.2.3.4 [...] -> ...'."""
    cli = cli or find_cli()
    if not cli:
        return {"cli": None, "state": "nym-vpnc not found", "connected": False}
    code, out = _run(cli, "status")
    m = STATE.search(out)
    state = (m.group(1) if m else (out.splitlines() or [f"exit code {code}"])[-1]).strip()[:200]
    return {"cli": cli, "state": state, "connected": code == 0 and re.match(r"Connected\b", state) is not None}


def connected(st):
    """Still connected? Asked again before every action of a run."""
    return status(st["cli"])["connected"]


def settle(cli, seconds, every=2.0):
    """Wait while the tunnel is still connecting (someone else started it), then report."""
    end = time.time() + seconds
    st = status(cli)
    while st["state"].startswith("Connecting") and time.time() < end:
        time.sleep(every)
        st = status(cli)
    return st


@contextmanager
def guard(cfg, every=2.0):
    """Yield while NymVPN is connected. Connect it first if allowed; if this run turned it on, turn it off again.

    With cfg["require"] off, yield None and touch nothing."""
    if not cfg.get("require", True):
        yield None
        return
    cli = find_cli()
    if not cli:
        raise NymError("NymVPN is required, but nym-vpnc was not found. " + HOWTO)
    st = settle(cli, 120, every)
    ours = False
    if not st["connected"]:
        if not cfg.get("auto_connect", True):
            raise NymError(f"NymVPN is not connected ({st['state']}). Connect it in the NymVPN app first.")
        print("* connecting NymVPN ...", flush=True)
        ours = True
        _, out = _run(cli, "connect", "--wait", timeout=180)
        st = status(cli)
        if not st["connected"]:
            _run(cli, "disconnect", "--wait", timeout=60)
            raise NymError(f"NymVPN did not connect ({st['state']}). {out[-300:]}".strip())
    try:
        yield st
    finally:
        if ours:
            print("* turning NymVPN off again, as it was before this run", flush=True)
            _run(cli, "disconnect", "--wait", timeout=60)


def selftest():
    global _run, find_cli
    now, calls = {"state": "Disconnected", "after_connect": "Connected wg to 1.2.3.4:51820 [aa] → 5.6.7.8:51820 [bb]"}, []

    def fake(cli, *args, timeout=10):
        calls.append(args[0])
        if args == ("status",):
            return (1, "Error: failed to connect to nym-vpnd") if now["state"] is None else (0, f"State: {now['state']}")
        if args[0] == "connect":
            now["state"] = now["after_connect"]
        if args[0] == "disconnect":
            now["state"] = "Disconnected"
        return 0, ""

    keep = _run, find_cli
    _run, find_cli = fake, (lambda: "nym-vpnc")
    try:
        for state, up in (("Connected wg to 1.2.3.4 [a] → 5.6.7.8 [b]", True), ("Connecting wg, Init, try #1", False),
                          ("Error state: BandwidthExceeded", False), ("Disconnected", False), ("Offline", False)):
            now["state"] = state
            assert status()["connected"] is up, state
        now["state"] = None
        st = status()
        assert not st["connected"] and "nym-vpnd" in st["state"], st  # daemon not running

        calls.clear()
        with guard({"require": False}) as st:
            assert st is None and not calls  # not required: nothing is touched

        now["state"] = "Connected wg to 1.2.3.4 [a] → 5.6.7.8 [b]"
        calls.clear()
        with guard({}) as st:
            assert st["connected"]
        assert "connect" not in calls and "disconnect" not in calls  # the person's own tunnel is left alone

        now["state"] = "Disconnected"
        calls.clear()
        with guard({}) as st:
            assert st["connected"] and connected(st) and calls.count("connect") == 1
        assert calls[-1] == "disconnect" and now["state"] == "Disconnected"  # we turned it on, so we turn it off

        try:
            with guard({"auto_connect": False}):
                raise AssertionError("ran without NymVPN")
        except NymError:
            pass

        now["after_connect"] = "Error state: BandwidthExceeded"
        calls.clear()
        try:
            with guard({}):
                raise AssertionError("ran on a failed tunnel")
        except NymError as e:
            assert "BandwidthExceeded" in str(e) and calls[-1] == "disconnect", (e, calls)

        seq = iter(["Connecting wg, Init, try #1", "Connected wg to 1.2.3.4 [a] → 5.6.7.8 [b]"])
        _run = lambda cli, *args, timeout=10: (0, f"State: {next(seq, 'Connected wg')}")  # noqa: E731
        assert settle("nym-vpnc", 5, every=0)["connected"]  # someone else's connect is waited for, not repeated

        find_cli = lambda: None  # noqa: E731
        try:
            with guard({}):
                raise AssertionError("ran without nym-vpnc")
        except NymError as e:
            assert "nym-vpnc was not found" in str(e)
        assert status(None)["cli"] is None
    finally:
        _run, find_cli = keep
    print("nym selftest ok")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    st = status()
    print(f"NymVPN: {st['state']}")
    if not st["cli"]:
        print(HOWTO)
    return 0 if st["connected"] else 1


if __name__ == "__main__":
    sys.exit(main())
