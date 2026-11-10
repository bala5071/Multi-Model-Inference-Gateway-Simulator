// Snapshots produced by the gateway (tests/test_dashboard.py keeps them current), typed
// by the hand-written API models. If the API and the models drift apart, this file stops
// compiling.

import type { ControlStatus } from "../api/control";
import type { Snapshot } from "../api/types";
import type { Capabilities } from "../console/types";
import controlOffJson from "./fixtures/control-off.json";
import controlSimulatedJson from "./fixtures/control-simulated.json";
import passthroughJson from "./fixtures/snapshot-passthrough.json";
import simulatedJson from "./fixtures/snapshot-simulated.json";
import capabilitiesAliceJson from "./fixtures/capabilities-alice.json";
import capabilitiesBobJson from "./fixtures/capabilities-bob.json";
import capabilitiesCleoJson from "./fixtures/capabilities-cleo.json";
import capabilitiesNinaJson from "./fixtures/capabilities-nina.json";
import capabilitiesOliveJson from "./fixtures/capabilities-olive.json";
import capabilitiesRootJson from "./fixtures/capabilities-root.json";
import capabilitiesVeraJson from "./fixtures/capabilities-vera.json";

/** Three simulated models with plans and SLOs; premium is degraded. Taken at 6782 ms. */
export const simulated: Snapshot = simulatedJson;

/** Three models behind pass-through workers, no plans and no SLOs. Taken at 70 ms. */
export const passthrough: Snapshot = passthroughJson;

/** GET /v1/control ten simulated seconds into simulated live mode: seed 7, speed 5. */
export const controlSimulated: ControlStatus = controlSimulatedJson;

/** GET /v1/control on a gateway with neither load control nor simulation. */
export const controlOff: ControlStatus = controlOffJson;

/**
 * GET /v1/auth/capabilities for each user of tests/test_capabilities.py's world, in their own
 * organization. Named by user: the tests read each one's own answers, never a role.
 */
export const capabilities = {
  alice: capabilitiesAliceJson,
  bob: capabilitiesBobJson,
  cleo: capabilitiesCleoJson,
  nina: capabilitiesNinaJson,
  olive: capabilitiesOliveJson,
  root: capabilitiesRootJson,
  vera: capabilitiesVeraJson,
} satisfies Record<string, Capabilities>;

export type CapabilityUser = keyof typeof capabilities;
