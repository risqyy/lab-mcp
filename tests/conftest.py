import sys
from pathlib import Path

# Make the simulated instruments (tests/<category>/fake_*.py) importable.
TESTS = Path(__file__).parent
for category in (TESTS / "power_supplies", TESTS / "multimeters", TESTS / "signal_generators", TESTS / "oscilloscopes"):
    sys.path.insert(0, str(category))
