"""Integration errors, plus a switch to simulate outages so fallback paths can be demonstrated and tested."""


class IntegrationError(Exception):
    pass


class Faults:
    NAMES = ("calendar", "crm")

    def __init__(self):
        self.active: set[str] = set()

    def set(self, name: str, on: bool) -> None:
        if name not in self.NAMES:
            raise ValueError(f"unknown integration {name}")
        (self.active.add if on else self.active.discard)(name)

    def check(self, name: str) -> None:
        if name in self.active:
            raise IntegrationError(f"{name} unavailable (simulated outage)")
