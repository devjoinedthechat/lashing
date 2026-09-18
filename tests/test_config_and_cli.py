from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from lashing.cli import main
from lashing.config import ConfigError, Scope, Shipper, load
from lashing.ledger import Ledger
from lashing.plans import PlanBook

ROOT = Path(__file__).parent.parent


def test_the_example_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load(ROOT / "lashing.example.toml")
    assert config.endpoints is not None and config.endpoints.booking == "http://127.0.0.1:8401/bkg"
    assert [g.id for g in config.grants] == ["rebook-to-another-sailing", "small-asia-europe-bookings"]
    assert config.state_dir == ROOT / ".lashing"
    small = config.grants[1]
    today = dt.date(2026, 10, 1)
    assert small.covers(Scope(action="create", lane="CNSHA-NLRTM", units=4), today)
    assert not small.covers(Scope(action="create", lane="KRPUS-USLAX", units=1), today)
    assert not small.covers(Scope(action="create", lane="CNSHA-NLRTM", units=5), today)
    assert not small.covers(Scope(action="create", lane="CNSHA-NLRTM", units=1), dt.date(2027, 1, 1))


def test_credentials_come_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lashing.toml"
    path.write_text('[carrier]\nbase_url = "http://c.example"\nauth_env = "CARRIER_TOKEN"\n')
    with pytest.raises(ConfigError, match="CARRIER_TOKEN"):
        load(path)
    monkeypatch.setenv("CARRIER_TOKEN", "s3cret")
    assert load(path).headers == {"Authorization": "Bearer s3cret"}


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[[grant]]\nactions = ["delete_everything"]\n', "actions must be"),
        ('[carrier]\nbooking_url = "x"\n', "needs base_url"),
        ('[carrier]\nbase_url = "x"\nvalidation = "maybe"\n', "validation"),
        ('[shipper]\nbooking_agent = "A"\ncontact_name = "B"\n', "contact_email or contact_phone"),
        ("not toml at all [", "cannot read"),
    ],
)
def test_bad_configs_are_explained(tmp_path: Path, toml: str, message: str) -> None:
    path = tmp_path / "lashing.toml"
    path.write_text(toml)
    with pytest.raises(ConfigError, match=message):
        load(path)


def test_a_shipper_needs_a_way_to_be_contacted() -> None:
    with pytest.raises(ConfigError):
        Shipper(booking_agent="A", contact_name="B")
    parties = Shipper(booking_agent="A", contact_name="B", contact_phone="+45 1").document_parties()
    assert parties["bookingAgent"]["partyContactDetails"] == [{"name": "B", "phone": "+45 1"}]


def _plan_in(state: Path) -> str:
    import asyncio  # noqa: PLC0415

    from lashing.server import demo  # noqa: PLC0415
    from lashing.service import EquipmentLine  # noqa: PLC0415

    service, _ = demo(state)
    return str(
        asyncio.run(service.propose_booking("CNSHA", "NLRTM", [EquipmentLine("45G1", 1, "Toys", 5000)]))["plan_id"]
    )


def test_approve_needs_a_terminal_or_an_explicit_yes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_id = _plan_in(tmp_path)
    assert main(["approve", plan_id, "--state-dir", str(tmp_path)]) == 1  # pytest's stdin is not a TTY
    assert "refusing to approve without a terminal" in capsys.readouterr().err
    assert main(["approve", plan_id, "--state-dir", str(tmp_path), "--yes", "--as", "alice"]) == 0
    state = PlanBook(Ledger(tmp_path / "ledger.jsonl")).get(plan_id)
    assert state is not None and state.approved_by == "operator:alice"
    assert main(["approve", "pln_nope", "--state-dir", str(tmp_path), "--yes"]) == 1


def test_plans_and_ledger_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_id = _plan_in(tmp_path)
    assert main(["plans", "--state-dir", str(tmp_path)]) == 0
    assert plan_id in capsys.readouterr().out
    assert main(["ledger", "verify", "--state-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith("ok: 1 entries")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(ledger.read_text().replace("CNSHA", "CNNGB"))
    assert main(["ledger", "verify", "--state-dir", str(tmp_path)]) == 1
    assert "TAMPERED: entry 1 was altered" in capsys.readouterr().err


def test_serve_without_a_carrier_points_to_demo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "lashing.toml"
    path.write_text('[shipper]\nbooking_agent = "A"\ncontact_name = "B"\ncontact_email = "b@x.example"\n')
    assert main(["serve", "--config", str(path)]) == 2
    assert "lashing demo" in capsys.readouterr().err
