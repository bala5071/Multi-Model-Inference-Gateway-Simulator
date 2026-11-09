import type { Api } from "./api";
import type { SessionUser } from "./types";

/** What every screen gets from the logged-in console. */
export interface ConsoleContext {
  api: Api;
  user: SessionUser;
  /**
   * Runs a change. When the gateway answers `step_up_required`, asks for the password, steps
   * up, and runs it once more; with `askFirst`, asks before the first try (the dry run said
   * the change loosens where prompts go). The password is never kept.
   */
  guarded: <T>(run: () => Promise<T>, askFirst?: boolean) => Promise<T>;
}
