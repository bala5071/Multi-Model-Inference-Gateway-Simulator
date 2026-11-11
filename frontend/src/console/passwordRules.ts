// The gateway's password policy and lockout, as the login screens state them (docs/54). Help text
// only: the gateway decides, and says why it refused. tests/test_dashboard.py pins each number to
// the gateway's own constant (gateway.passwords, gateway.auth), so the two cannot drift apart.

export const PASSWORD_MIN_LENGTH = 12;
export const PASSWORD_MAX_LENGTH = 1024;
export const PRODUCT_NAME = "gateway";
export const LOCK_FAILURES = 5;
export const LOCK_WINDOW_MINUTES = 15;
export const LOCK_MINUTES = 15;

/** The rules a new password must meet, as the change-password screen lists them. */
export const PASSWORD_RULES: readonly string[] = [
  `At least ${PASSWORD_MIN_LENGTH.toString()} characters (at most ${PASSWORD_MAX_LENGTH.toLocaleString("en-US")})`,
  "Not one of the very common passwords",
  "Does not contain your username",
  `Does not contain the word \u201c${PRODUCT_NAME}\u201d`,
  "Different from your current password",
];

/** What the login screen says after a refused login: the gateway answers every refusal alike. */
export const LOCKOUT_SENTENCE =
  `After ${LOCK_FAILURES.toString()} failed attempts within ${LOCK_WINDOW_MINUTES.toString()} minutes, ` +
  `an account is locked for ${LOCK_MINUTES.toString()} minutes. Wait, or ask an administrator to unlock it.`;
