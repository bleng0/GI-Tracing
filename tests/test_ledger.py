import pytest

from gi_trace.crypto import CUSTOMS, Node
from gi_trace.ledger import LedgerError


def test_append_and_verify(world):
    ledger = world.ledgers["NP"]
    ledger.append("NOTE", {"a": 1}, world.nodes["CUSTOMS-NP"])
    ledger.append("NOTE", {"a": 2}, world.nodes["GI-REG-NP"])
    assert ledger.verify_chain() == []
    assert ledger.blocks[1].prev_hash == ledger.blocks[0].hash


def test_non_member_cannot_append(world):
    with pytest.raises(LedgerError):
        world.ledgers["NP"].append("NOTE", {}, world.nodes["CUSTOMS-IN"])
    impostor = Node("CUSTOMS-NP", CUSTOMS, "NP")  # same name, different key
    with pytest.raises(LedgerError):
        world.ledgers["NP"].append("NOTE", {}, impostor)


def test_unauthorised_edit_is_detected(world):
    ledger = world.ledgers["NP"]
    ledger.append("NOTE", {"weight_kg": 120.0}, world.nodes["CUSTOMS-NP"])
    ledger.blocks[0].payload["weight_kg"] = 12.0
    assert "block 0: payload does not match its chameleon digest" in ledger.verify_chain()


def test_threshold_redaction_keeps_chain_valid(world):
    ledger = world.ledgers["NP"]
    target = ledger.append("NOTE", {"weight_kg": 120.0}, world.nodes["CUSTOMS-NP"])
    later = ledger.append("NOTE", {"next": True}, world.nodes["CUSTOMS-NP"])
    hash_before = target.hash
    shares = dict(list(world.shares["NP"].items())[3:6])

    audit = ledger.redact(0, {"weight_kg": 12.0}, shares, "typo", world.nodes["GI-REG-NP"])

    assert ledger.blocks[0].payload == {"weight_kg": 12.0}
    assert target.hash == hash_before
    assert later.prev_hash == hash_before
    assert ledger.verify_chain() == []
    assert audit.tx_type == "REDACTION"
    assert audit.payload["approvers"] == sorted(shares)


def test_redaction_needs_threshold(world):
    ledger = world.ledgers["NP"]
    ledger.append("NOTE", {"weight_kg": 120.0}, world.nodes["CUSTOMS-NP"])
    two = dict(list(world.shares["NP"].items())[:2])
    with pytest.raises(LedgerError, match="needs 3"):
        ledger.redact(0, {"weight_kg": 12.0}, two, "typo", world.nodes["CUSTOMS-NP"])
    assert ledger.blocks[0].payload == {"weight_kg": 120.0}


def test_shares_from_another_ledger_are_rejected(world):
    ledger = world.ledgers["NP"]
    ledger.append("NOTE", {"weight_kg": 120.0}, world.nodes["CUSTOMS-NP"])
    foreign = dict(list(world.shares["PK"].items())[:3])
    with pytest.raises(LedgerError, match="do not reconstruct"):
        ledger.redact(0, {"weight_kg": 12.0}, foreign, "typo", world.nodes["CUSTOMS-NP"])
