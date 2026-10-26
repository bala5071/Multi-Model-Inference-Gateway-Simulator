"""Roles, the permission table, and ``authorize``.

See docs/28-roles.md and docs/identity-plan.md, section 7.

One table, ``PERMISSIONS``: each action, the level of resource it acts on, the (role, scope
relation) pairs that allow it, whether it needs step-up (enforced from step 6), and when it is
wired. ``authorize(actor, action, resource)`` denies by default: an unknown action, a resource
of the wrong level, a disabled user or organization, and every role and relation the table
does not list, are all denied.

The **scope relation** is how one of the actor's assignments stands to the resource:
``platform`` for a platform-scope assignment (any resource), ``own_organization`` for an
organization-scope assignment on that organization, ``application_in_own_organization`` for
one on an application in it, ``own_application`` for an application-scope assignment on that
application, and ``other`` for anything else. A non-platform assignment only ever relates to
resources of the actor's own organization, so any request that crosses organizations is
denied whatever the role; platform_admin is the exception, and only for the actions the table
gives it, none of them inside an application.

Application-defined roles (such as ``clinic_staff``) are carried for applications to
interpret and grant nothing here: ``authorize`` ignores every role that is not built in.

    uv run python -m gateway.permissions --matrix > docs/permission-matrix.md
"""

from __future__ import annotations

import argparse
import itertools
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    ORG_ADMIN = "org_admin"
    APP_ADMIN = "app_admin"
    VIEWER = "viewer"


class ScopeType(StrEnum):
    PLATFORM = "platform"
    ORGANIZATION = "organization"
    APPLICATION = "application"


PLATFORM_SCOPE_ID = "*"


class Relation(StrEnum):
    PLATFORM = "platform"
    OWN_ORGANIZATION = "own_organization"
    APPLICATION_IN_OWN_ORGANIZATION = "application_in_own_organization"
    OWN_APPLICATION = "own_application"
    OTHER = "other"


class Level(StrEnum):
    """The kind of resource an action acts on."""

    PLATFORM = "platform"
    ORGANIZATION = "organization"
    APPLICATION = "application"


class Wired(StrEnum):
    """Where an action is enforced today."""

    NOW = "now"  # enforced now (the CLI, acting as a user with --as)
    STEP_7 = "step 7"  # the management endpoints
    A5 = "A5"  # provider keys and modes
    A8 = "A8"  # monitoring and control: local only until then
    LATER = "later"  # declared; no endpoint is planned in A2


# Which roles may be held in which scope.
ROLE_SCOPES: Mapping[Role, frozenset[ScopeType]] = {
    Role.PLATFORM_ADMIN: frozenset({ScopeType.PLATFORM}),
    Role.ORG_ADMIN: frozenset({ScopeType.ORGANIZATION}),
    Role.APP_ADMIN: frozenset({ScopeType.APPLICATION}),
    Role.VIEWER: frozenset({ScopeType.ORGANIZATION, ScopeType.APPLICATION}),
}


@dataclass(frozen=True, slots=True)
class Permission:
    action: str
    level: Level
    allowed: frozenset[tuple[Role, Relation]]
    step_up: bool
    # Consume-on-use (docs/32): a step-up window is used up by one successful call, so each
    # such change needs a fresh password. Always with step_up.
    consume: bool
    wired: Wired


def _allow(*pairs: str) -> frozenset[tuple[Role, Relation]]:
    """``"role@relation"`` strings, as the documented table writes them."""
    allowed = set()
    for pair in pairs:
        role, relation = pair.split("@")
        allowed.add((Role(role), Relation(relation)))
    return frozenset(allowed)


# In the step-up column of _rows: step-up, used up by one successful call (consume-on-use).
CONSUME = "consume"

_PLATFORM = "platform_admin@platform"
_ORG_ADMIN = "org_admin@own_organization"
_ORG_ADMIN_APP = "org_admin@application_in_own_organization"
_APP_ADMIN = "app_admin@own_application"


def _rows() -> list[Permission]:
    p, o, a = Level.PLATFORM, Level.ORGANIZATION, Level.APPLICATION
    w = Wired
    in_app = (_ORG_ADMIN_APP, _APP_ADMIN)
    rows = [
        # Organizations and aggregate data: platform_admin only.
        ("organizations.create", p, (_PLATFORM,), True, w.NOW),
        ("organizations.list", p, (_PLATFORM,), False, w.NOW),
        ("usage.aggregate", p, (_PLATFORM,), False, w.LATER),
        ("organizations.disable", o, (_PLATFORM,), True, w.NOW),
        ("organizations.enable", o, (_PLATFORM,), True, w.NOW),
        # Users: platform_admin in any organization, org_admin in its own.
        ("users.create", o, (_PLATFORM, _ORG_ADMIN), True, w.NOW),
        ("users.disable", o, (_PLATFORM, _ORG_ADMIN), True, w.NOW),
        ("users.enable", o, (_PLATFORM, _ORG_ADMIN), True, w.NOW),
        ("users.reset_password", o, (_PLATFORM, _ORG_ADMIN), True, w.NOW),
        # Unlocking lifts a lockout, a defense: step-up, like the other user changes.
        ("users.unlock", o, (_PLATFORM, _ORG_ADMIN), True, w.NOW),
        ("users.list", o, (_PLATFORM, _ORG_ADMIN), False, w.STEP_7),
        # Roles (which roles each may grant is checked when granting: docs/28).
        ("roles.assign", o, (_PLATFORM, _ORG_ADMIN), True, w.NOW),
        ("roles.revoke", o, (_PLATFORM, _ORG_ADMIN), True, w.NOW),
        ("roles.list", o, (_PLATFORM, _ORG_ADMIN), False, w.NOW),
        # Applications, and usage, in an organization.
        ("applications.create", o, (_ORG_ADMIN,), False, w.STEP_7),
        ("applications.list", o, (_ORG_ADMIN, "viewer@own_organization"), False, w.STEP_7),
        ("usage.read_organization", o, (_ORG_ADMIN, "viewer@own_organization"), False, w.LATER),
        # Model providers and their keys belong to the organization (docs/32): only its
        # org_admin configures them or sets a key; platform_admin may see that they exist and
        # switch one off or on. No action reads a key.
        ("providers.list", o, (_ORG_ADMIN,), False, w.A5),
        ("providers.status", o, (_PLATFORM, _ORG_ADMIN), False, w.A5),
        ("providers.create", o, (_ORG_ADMIN,), True, w.A5),
        ("providers.update", o, (_ORG_ADMIN,), True, w.A5),
        ("providers.disable", o, (_PLATFORM, _ORG_ADMIN), True, w.A5),
        ("providers.enable", o, (_PLATFORM, _ORG_ADMIN), True, w.A5),
        ("providers.delete", o, (_ORG_ADMIN,), CONSUME, w.A5),
        ("providers.test", o, (_ORG_ADMIN,), False, w.A5),
        ("provider_keys.set", o, (_ORG_ADMIN,), CONSUME, w.A5),
        ("provider_keys.delete", o, (_ORG_ADMIN,), CONSUME, w.A5),
        ("mode.set_organization", o, (_ORG_ADMIN,), CONSUME, w.A5),
        # The audit log: organization-, user-, role- and login-level entries.
        ("audit.read_organization", o, (_PLATFORM, _ORG_ADMIN), False, w.LATER),
        # Inside an application: never platform_admin.
        (
            "applications.read",
            a,
            (*in_app, "viewer@own_application", "viewer@application_in_own_organization"),
            False,
            w.STEP_7,
        ),
        (
            "usage.read_application",
            a,
            (*in_app, "viewer@own_application", "viewer@application_in_own_organization"),
            False,
            w.LATER,
        ),
        ("application_keys.issue", a, in_app, True, w.STEP_7),
        ("application_keys.rotate", a, in_app, True, w.STEP_7),
        ("application_keys.revoke", a, in_app, True, w.STEP_7),
        ("application_keys.list", a, in_app, False, w.STEP_7),
        ("application_roles.define", a, in_app, True, w.LATER),
        ("application_roles.delete", a, in_app, True, w.LATER),
        ("application_roles.list", a, in_app, False, w.LATER),
        # The audit log: entries about application keys and application roles.
        ("audit.read_application", a, in_app, False, w.LATER),
        # Modes (A5 step 3): an application may be stricter than its organization.
        ("mode.set_application", a, in_app, CONSUME, w.A5),
        # Monitoring, control and the Playground: local only until A8.
        ("monitoring.read", p, (_PLATFORM,), False, w.A8),
        ("control.write", p, (_PLATFORM,), False, w.A8),
        ("playground.use", p, (_PLATFORM,), False, w.A8),
    ]
    return [
        Permission(action, level, _allow(*pairs), bool(step_up), step_up is CONSUME, wired)
        for action, level, pairs, step_up, wired in rows
    ]


PERMISSIONS: Mapping[str, Permission] = {row.action: row for row in _rows()}


@dataclass(frozen=True, slots=True)
class Assignment:
    role: str
    scope_type: ScopeType
    scope_id: str


@dataclass(frozen=True, slots=True)
class Actor:
    """A user as ``authorize`` sees them: their organization and every assignment, read on
    each request (so a removed role is gone at the next one)."""

    user_id: str
    organization_id: str
    assignments: tuple[Assignment, ...]
    disabled: bool = False
    organization_disabled: bool = False
    # The operator at the CLI acting within this user's roles (--as): the operator is the
    # one who acts, and is recorded so (docs/28).
    via_operator: bool = False

    def holds(self, role: Role) -> bool:
        return any(a.role == role.value for a in self.assignments)


@dataclass(frozen=True, slots=True)
class Resource:
    level: Level
    organization_id: str | None = None
    application_id: str | None = None

    @staticmethod
    def platform() -> Resource:
        return Resource(Level.PLATFORM)

    @staticmethod
    def organization(organization_id: str) -> Resource:
        return Resource(Level.ORGANIZATION, organization_id)

    @staticmethod
    def application(organization_id: str, application_id: str) -> Resource:
        return Resource(Level.APPLICATION, organization_id, application_id)


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str


def relation(actor: Actor, assignment: Assignment, resource: Resource) -> Relation:
    """How ``assignment`` stands to ``resource``."""
    if assignment.scope_type is ScopeType.PLATFORM:
        return Relation.PLATFORM
    # Every other scope lies inside the actor's own organization.
    if resource.organization_id != actor.organization_id:
        return Relation.OTHER
    if assignment.scope_type is ScopeType.ORGANIZATION:
        if assignment.scope_id != actor.organization_id:
            return Relation.OTHER
        if resource.level is Level.ORGANIZATION:
            return Relation.OWN_ORGANIZATION
        if resource.level is Level.APPLICATION:
            return Relation.APPLICATION_IN_OWN_ORGANIZATION
        return Relation.OTHER
    if (
        assignment.scope_type is ScopeType.APPLICATION
        and resource.level is Level.APPLICATION
        and resource.application_id == assignment.scope_id
    ):
        return Relation.OWN_APPLICATION
    return Relation.OTHER


def authorize(actor: Actor | None, action: str, resource: Resource) -> Decision:
    """Allow only what the table allows; deny everything else."""
    permission = PERMISSIONS.get(action)
    if permission is None:
        return Decision(False, "unknown action")
    if actor is None:
        return Decision(False, "no actor")
    if resource.level is not permission.level:
        return Decision(False, f"{action} acts on a {permission.level} resource")
    if actor.disabled or actor.organization_disabled:
        return Decision(False, "disabled")
    built_in = {role.value for role in Role}
    for assignment in actor.assignments:
        if assignment.role not in built_in:
            continue  # application-defined roles grant nothing here
        pair = (Role(assignment.role), relation(actor, assignment, resource))
        if pair in permission.allowed:
            return Decision(True, f"{pair[0]}@{pair[1]}")
    return Decision(False, "no role allows it")


# --- the generated matrix --------------------------------------------------------------------


def _example(role: Role, rel: Relation, level: Level) -> tuple[Actor, Resource] | None:
    """An actor holding ``role`` that stands in ``rel`` to a resource of ``level``, or None
    if no such pair exists."""
    org, other_org, app, other_app = "org-a", "org-b", "app-a", "app-b"
    resources = {
        Level.PLATFORM: Resource.platform(),
        Level.ORGANIZATION: Resource.organization(org),
        Level.APPLICATION: Resource.application(org, app),
    }
    other_resources = {
        Level.PLATFORM: Resource.platform(),
        Level.ORGANIZATION: Resource.organization(other_org),
        Level.APPLICATION: Resource.application(other_org, other_app),
    }
    for scope_type in sorted(ROLE_SCOPES[role]):
        scope_id = {ScopeType.PLATFORM: PLATFORM_SCOPE_ID, ScopeType.ORGANIZATION: org}.get(
            scope_type, app
        )
        actor = Actor("usr_0000000000000001", org, (Assignment(role.value, scope_type, scope_id),))
        for resource in (resources[level], other_resources[level]):
            if relation(actor, actor.assignments[0], resource) is rel:
                return actor, resource
    return None


def matrix_rows() -> Iterable[tuple[str, Role, Relation, str]]:
    """(action, role, relation, "allow" / "deny" / "n/a") for every combination."""
    for action, role, rel in itertools.product(PERMISSIONS, Role, Relation):
        example = _example(role, rel, PERMISSIONS[action].level)
        if example is None:
            yield action, role, rel, "n/a"
        else:
            decision = authorize(example[0], action, example[1])
            yield action, role, rel, "allow" if decision.allowed else "deny"


def render_matrix() -> str:
    lines = [
        "# Permission matrix",
        "",
        "Generated from `src/gateway/permissions.py` by",
        "`uv run python -m gateway.permissions --matrix > docs/permission-matrix.md`;",
        "do not edit. Every action, every built-in role and every scope relation, through",
        "`authorize`. `n/a`: the role cannot stand in that relation to that kind of resource.",
        "The table it comes from, and what each relation means, are in docs/28-roles.md.",
        "",
    ]
    results = {(action, role, rel): result for action, role, rel, result in matrix_rows()}
    for action in PERMISSIONS:
        permission = PERMISSIONS[action]
        flags = f"level {permission.level}, wired {permission.wired}"
        if permission.consume:
            flags += ", step-up, consume"
        elif permission.step_up:
            flags += ", step-up"
        lines += [f"## `{action}` ({flags})", ""]
        lines.append("| role | " + " | ".join(r.value for r in Relation) + " |")
        lines.append("|---" * (len(Relation) + 1) + "|")
        for role in Role:
            cells = [results[(action, role, rel)] for rel in Relation]
            lines.append(f"| {role.value} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The permission table.")
    parser.add_argument("--matrix", action="store_true", help="print the generated matrix")
    args = parser.parse_args(argv)
    if not args.matrix:
        parser.error("say what to print: --matrix")
    sys.stdout.write(render_matrix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
