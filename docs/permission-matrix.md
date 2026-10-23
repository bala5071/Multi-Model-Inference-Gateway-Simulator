# Permission matrix

Generated from `src/gateway/permissions.py` by
`uv run python -m gateway.permissions --matrix > docs/permission-matrix.md`;
do not edit. Every action, every built-in role and every scope relation, through
`authorize`. `n/a`: the role cannot stand in that relation to that kind of resource.
The table it comes from, and what each relation means, are in docs/28-roles.md.

## `organizations.create` (level platform, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | n/a | n/a | n/a | deny |

## `organizations.list` (level platform, wired now)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | n/a | n/a | n/a | deny |

## `usage.aggregate` (level platform, wired later)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | n/a | n/a | n/a | deny |

## `organizations.disable` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | deny | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `organizations.enable` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | deny | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `users.create` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `users.disable` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `users.enable` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `users.reset_password` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `users.unlock` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `users.list` (level organization, wired step 7)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `roles.assign` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `roles.revoke` (level organization, wired now, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `roles.list` (level organization, wired now)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `applications.create` (level organization, wired step 7)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `applications.list` (level organization, wired step 7)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | allow | n/a | n/a | deny |

## `usage.read_organization` (level organization, wired later)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | allow | n/a | n/a | deny |

## `audit.read_organization` (level organization, wired later)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | allow | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | deny | n/a | n/a | deny |

## `applications.read` (level application, wired step 7)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | allow | allow | deny |

## `usage.read_application` (level application, wired later)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | allow | allow | deny |

## `application_keys.issue` (level application, wired step 7, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `application_keys.rotate` (level application, wired step 7, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `application_keys.revoke` (level application, wired step 7, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `application_keys.list` (level application, wired step 7)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `application_roles.define` (level application, wired later, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `application_roles.delete` (level application, wired later, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `application_roles.list` (level application, wired later)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `audit.read_application` (level application, wired later)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `provider_keys.add` (level application, wired A5, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `provider_keys.replace` (level application, wired A5, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `provider_keys.delete` (level application, wired A5, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `provider_keys.read` (level application, wired A5)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `mode.change` (level application, wired A5, step-up)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | deny | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | allow | n/a | deny |
| app_admin | n/a | n/a | n/a | allow | deny |
| viewer | n/a | n/a | deny | deny | deny |

## `monitoring.read` (level platform, wired A8)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | n/a | n/a | n/a | deny |

## `control.write` (level platform, wired A8)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | n/a | n/a | n/a | deny |

## `playground.use` (level platform, wired A8)

| role | platform | own_organization | application_in_own_organization | own_application | other |
|---|---|---|---|---|---|
| platform_admin | allow | n/a | n/a | n/a | n/a |
| org_admin | n/a | n/a | n/a | n/a | deny |
| app_admin | n/a | n/a | n/a | n/a | deny |
| viewer | n/a | n/a | n/a | n/a | deny |
