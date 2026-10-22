"""Organizations: what they are, how their names are checked, and who may join one.

See docs/26-organizations.md and docs/identity-plan.md, section 3.

An organization owns applications. Its kind decides who may belong to it:

- ``business``: any number of members.
- ``individual``: exactly one member, its org_admin; a second is refused.

Users do not exist yet (identity step 4). The membership rule is here as a pure function,
``membership_decision``, so that step 4 can call it when it adds a user; nothing calls it
in this step. Nothing here assumes there is only one organization: the ``default``
organization is data that migration 4 creates, not a special case in the code.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

# Created by migration 4 (and by every in-memory store), and where an application goes
# when no organization is named.
DEFAULT_ORGANIZATION_ID = "default"
DEFAULT_ORGANIZATION_NAME = "default"
MAX_NAME_LENGTH = 100


class OrganizationKind(StrEnum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"


@dataclass(frozen=True, slots=True)
class Organization:
    id: str
    name: str
    kind: OrganizationKind
    # Epoch time.
    created_at_ms: float
    disabled: bool = False


def normalize_name(name: str) -> str:
    """The name in Unicode NFC, or ``ValueError`` if it is not acceptable.

    Names are compared exactly after this, with no case folding. Refused: an empty name,
    leading or trailing whitespace, more than ``MAX_NAME_LENGTH`` characters, and any
    character in Unicode's "other" categories: control characters, format characters (which
    include the zero-width ones and the bidirectional overrides), surrogates, private-use
    and unassigned code points. Look-alike names (``Acme`` spelled with a Cyrillic letter) are not
    prevented.
    """
    normalized = unicodedata.normalize("NFC", name)
    if not normalized:
        raise ValueError("a name may not be empty")
    if normalized != normalized.strip():
        raise ValueError("a name may not start or end with whitespace")
    if len(normalized) > MAX_NAME_LENGTH:
        raise ValueError(f"a name may have at most {MAX_NAME_LENGTH} characters")
    for character in normalized:
        if unicodedata.category(character).startswith("C"):
            raise ValueError(
                f"a name may not contain U+{ord(character):04X} (control, format, zero-width "
                "or unassigned characters are refused)"
            )
    return normalized


def new_organization(
    organization_id: str, name: str, kind: str, *, created_at_ms: float
) -> Organization:
    """A new, enabled organization, with its name checked and normalized."""
    if not organization_id:
        raise ValueError("an organization needs an id")
    try:
        parsed = OrganizationKind(kind)
    except ValueError:
        kinds = ", ".join(k.value for k in OrganizationKind)
        raise ValueError(f"unknown kind {kind!r}; kinds are: {kinds}") from None
    return Organization(organization_id, normalize_name(name), parsed, created_at_ms)


# --- who may join -------------------------------------------------------------------------


class MembershipRefusal(StrEnum):
    INDIVIDUAL_HAS_A_MEMBER = "individual_has_a_member"


@dataclass(frozen=True, slots=True)
class MembershipDecision:
    """Whether one more member may be added. ``refusal`` says why not."""

    allowed: bool
    refusal: MembershipRefusal | None = None


def membership_decision(kind: OrganizationKind, current_members: int) -> MembershipDecision:
    """May one more member join an organization of ``kind`` that has ``current_members``?

    An individual organization takes its first member (who becomes its org_admin) and
    refuses any other; a business organization takes any number. Step 4 calls this before
    creating a user.
    """
    if current_members < 0:
        raise ValueError("current_members cannot be negative")
    if kind is OrganizationKind.INDIVIDUAL and current_members >= 1:
        return MembershipDecision(False, MembershipRefusal.INDIVIDUAL_HAS_A_MEMBER)
    return MembershipDecision(True)
