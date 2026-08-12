"""Deliberately has zero import statements — a tricky case for import-edge
extraction (Phase 2) and a sanity check that chunkers don't assume every
file has an imports section.
"""


class Address:
    def __init__(self, street: str, city: str, postal_code: str) -> None:
        self.street = street
        self.city = city
        self.postal_code = postal_code

    def format(self) -> str:
        return f"{self.street}, {self.city} {self.postal_code}"


class Profile:
    def __init__(self, display_name: str, bio: str = "") -> None:
        self.display_name = display_name
        self.bio = bio
