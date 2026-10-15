// Snapshots produced by the gateway (tests/test_dashboard.py keeps them current), typed
// by the hand-written API models. If the API and the models drift apart, this file stops
// compiling.

import type { ControlStatus } from "../api/control";
import type { Snapshot } from "../api/types";
import controlOffJson from "./fixtures/control-off.json";
import controlSimulatedJson from "./fixtures/control-simulated.json";
import passthroughJson from "./fixtures/snapshot-passthrough.json";
import simulatedJson from "./fixtures/snapshot-simulated.json";

/** Three simulated models with plans and SLOs; premium is degraded. Taken at 6782 ms. */
export const simulated: Snapshot = simulatedJson;

/** Three models behind pass-through workers, no plans and no SLOs. Taken at 70 ms. */
export const passthrough: Snapshot = passthroughJson;

/** GET /v1/control ten simulated seconds into simulated live mode: seed 7, speed 5. */
export const controlSimulated: ControlStatus = controlSimulatedJson;

/** GET /v1/control on a gateway with neither load control nor simulation. */
export const controlOff: ControlStatus = controlOffJson;
