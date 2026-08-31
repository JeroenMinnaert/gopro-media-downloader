"""The command surface itself: argument validation, exit codes, and the
messages a user actually sees when something is wrong."""

import httpx
import pytest
import respx
from conftest import make_item

from gopro_dl.api import API_HOST
from gopro_dl.circuit import CircuitBreaker
from gopro_dl.cli import main
from gopro_dl.manifest import Manifest

CONTENT = b"x" * 500


def seeded_dest(tmp_path, state="done"):
    """A destination that a sync has already run against."""
    dest = tmp_path / "media"
    with Manifest(dest / ".gopro-dl" / "manifest.db") as m:
        m.upsert_item(make_item("aaa111"), "2023-07-15")
        file_id = m.upsert_file(
            "aaa111", 1, "GX010001.MP4", "2023-07-15/GX010001.MP4", len(CONTENT)
        )
        if state == "done":
            m.mark_done(file_id, len(CONTENT))
            path = dest / "2023-07-15" / "GX010001.MP4"
            path.parent.mkdir(parents=True)
            path.write_bytes(CONTENT)
        elif state == "failed":
            m.claim_file(file_id)
            m.mark_failed(file_id, "boom")
    return dest


def run(*args, dest=None):
    return main([*args, "--dest", str(dest), "--token", "test-token"])


# -- reading a destination that was never synced ---------------------------


@pytest.mark.parametrize("command", ["status", "report", "verify", "retry", "fix-dates"])
def test_a_destination_with_no_manifest_says_so_instead_of_inventing_one(
    tmp_path, capsys, command
):
    """A typo in --dest must not be answered with an empty library: opening a
    manifest creates it, so these commands check first."""
    dest = tmp_path / "typo"
    assert run(command, dest=dest) == 1
    assert "no manifest" in capsys.readouterr().out
    assert not (dest / ".gopro-dl" / "manifest.db").exists()


def test_sync_still_creates_the_manifest_it_needs(tmp_path):
    with respx.mock:
        respx.get(f"{API_HOST}/media/user").mock(httpx.Response(200, json={"id": "u1"}))
        respx.get(f"{API_HOST}/media/search").mock(
            httpx.Response(200, json={"_embedded": {"media": []}, "_pages": {"total_pages": 1}})
        )
        assert run("sync", dest=tmp_path / "fresh") == 0
    assert (tmp_path / "fresh" / ".gopro-dl" / "manifest.db").exists()


# -- exit codes and wiring -------------------------------------------------


def test_status_reports_the_real_number_of_failures(tmp_path, capsys):
    dest = seeded_dest(tmp_path, state="failed")
    assert run("status", dest=dest) == 0
    assert "1 failed file(s)" in capsys.readouterr().out


def test_verify_passes_its_flags_through_and_exits_1_on_a_problem(tmp_path, capsys):
    dest = seeded_dest(tmp_path)
    assert run("verify", dest=dest) == 0

    (dest / "2023-07-15" / "GX010001.MP4").unlink()
    assert run("verify", "--fix", dest=dest) == 1
    assert "missing" in capsys.readouterr().out
    with Manifest(dest / ".gopro-dl" / "manifest.db") as m:
        assert m.get_file("aaa111", 1)["state"] == "pending"  # --fix re-queued it


def test_retry_resets_failed_files(tmp_path):
    dest = seeded_dest(tmp_path, state="failed")
    assert run("retry", dest=dest) == 0
    with Manifest(dest / ".gopro-dl" / "manifest.db") as m:
        assert m.get_file("aaa111", 1)["state"] == "pending"


def test_report_writes_a_csv(tmp_path):
    dest = seeded_dest(tmp_path)
    out = tmp_path / "report.csv"
    assert run("report", "--csv", str(out), dest=dest) == 0
    assert "2023-07-15/GX010001.MP4" in out.read_text()


def test_report_to_an_unwritable_path_is_an_error_not_a_traceback(tmp_path, capsys):
    dest = seeded_dest(tmp_path)
    assert run("report", "--csv", str(tmp_path / "nope" / "out.csv"), dest=dest) == 1
    assert "Cannot write" in capsys.readouterr().out


# -- argument validation ---------------------------------------------------


@pytest.mark.parametrize("value", ["01-06-2024", "2024/06/01", "yesterday"])
def test_a_date_that_is_not_iso_is_rejected_up_front(tmp_path, value):
    """These compare as plain strings in SQL, so a wrong format silently
    matches nothing -- and the user believes the library is complete."""
    with pytest.raises(SystemExit):
        run("sync", "--since", value, dest=tmp_path / "media")


def test_a_compact_date_is_normalised_rather_than_passed_through():
    """`fromisoformat` accepts 20240601, which would compare against
    YYYY-MM-DD folders and match nothing."""
    from gopro_dl.cli import build_parser

    args = build_parser().parse_args(["sync", "--since", "20240601", "--until", "2024-06-30"])
    assert (args.since, args.until) == ("2024-06-01", "2024-06-30")


def test_limit_zero_is_rejected_rather_than_meaning_unlimited(tmp_path):
    with pytest.raises(SystemExit):
        run("sync", "--limit", "0", dest=tmp_path / "media")


# -- an unreachable API is not a rejected token ----------------------------


def test_a_network_failure_is_reported_as_one(tmp_path, capsys, monkeypatch):
    """Told "token rejected", a user goes and fetches a new token for nothing."""
    monkeypatch.setattr("gopro_dl.api.backoff_delay", lambda *a, **k: 0.0)
    # The real cooldown is a minute; the point here is the message, not the wait.
    monkeypatch.setattr("gopro_dl.cli.CircuitBreaker", lambda: CircuitBreaker(base_cooldown=0.01))
    with respx.mock:
        respx.get(f"{API_HOST}/media/user").mock(
            side_effect=httpx.ConnectError("Network is unreachable")
        )
        assert run("sync", dest=tmp_path / "media") == 1
    out = capsys.readouterr().out
    assert "Could not reach api.gopro.com" in out
    assert "rejected" not in out


def test_a_genuinely_rejected_token_still_says_so(tmp_path, capsys):
    with respx.mock:
        respx.get(f"{API_HOST}/media/user").mock(httpx.Response(401))
        assert run("sync", dest=tmp_path / "media") == 1
    assert "Token rejected" in capsys.readouterr().out


# -- no stray state under an unconfirmed destination -----------------------


def test_token_leaves_no_log_directory_behind(tmp_path, capsys):
    """`token` and `setup` run before the user has settled on a destination;
    writing a run log there would litter a directory they never chose."""
    dest = tmp_path / "not-chosen-yet"
    with respx.mock:
        respx.get(f"{API_HOST}/media/user").mock(httpx.Response(200, json={"id": "u1"}))
        assert run("token", dest=dest) == 0
    assert not dest.exists()
