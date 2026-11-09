# C1 screenshots

Taken by `frontend/e2e/console.e2e.ts` (`npm run e2e`, Chrome) against the plain server on
the e2e database of note 47: real console screens on throwaway accounts, not simulated live
mode. Each was taken only after the test found none of the run's generated secrets in the
page's text or its fields. Evidence for PR C1; they can be removed before it is merged.

- `application-settings-admin.png`: an org_admin, after the edits, step-up, revert and preset.
- `application-settings-viewer.png`: a viewer of the organization: read only, no write control.
- `organization-settings.png`: the organization's caps, the applications each limits, history.
