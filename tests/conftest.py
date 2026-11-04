import pytest

from tests.fakes import FakeBroker


@pytest.fixture(autouse=True)
def no_duplicate_orders():
    """Inget test får producera en dubbelorder: samma request-id två gånger, eller två öppna GLD-positioner."""
    FakeBroker.instances.clear()
    yield
    for b in FakeBroker.instances:
        assert not b.duplicates, f"dubbelorder: {b.duplicates}"
        opens = [o["key"] for o in b.opened]
        assert len(opens) == len(set(opens)), "samma idempotensnyckel skickad två gånger"
