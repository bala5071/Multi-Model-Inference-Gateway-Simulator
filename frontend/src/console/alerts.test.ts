import {
  addressLines,
  channelList,
  channelRows,
  channelTarget,
  defaultMark,
  deliveryRows,
  draftRule,
  evaluatorBanner,
  followsProbe,
  formatCondition,
  formatMinute,
  incidentRows,
  incidentTimeline,
  initialRuleDraft,
  isOpen,
  lastValue,
  notificationRows,
  previewSummary,
  recipientRows,
  ruleBody,
  ruleRows,
} from "./alerts";
import type { AlertRule, Channel, Incident, IncidentDetail, PreviewMinute } from "./types";

function rule(overrides: Partial<AlertRule> = {}): AlertRule {
  return {
    id: "rul_1",
    version: 1,
    default_key: "error_rate",
    changed_from_default: false,
    name: "Error rate above 5 percent",
    signal: "error_rate",
    parameter: null,
    comparison: ">",
    threshold: 5,
    window: 5,
    for_minutes: 5,
    severity: "critical",
    channels: ["organization defaults"],
    cooldown_minutes: 30,
    repeat_every_minutes: null,
    quiet_hours: null,
    enabled: true,
    created_at_ms: 0,
    updated_at_ms: 0,
    ...overrides,
  };
}

function incident(overrides: Partial<Incident> = {}): Incident {
  return {
    id: "inc_1",
    application_id: "app-one",
    rule_id: "rul_1",
    rule_version: 1,
    series: null,
    state: "firing",
    severity: "critical",
    value: 7.5,
    opened_at_ms: 60_000,
    pending_since_ms: 60_000,
    fired_at_ms: 120_000,
    acknowledged_at_ms: null,
    acknowledged_by: null,
    resolved_at_ms: null,
    resolve_reason: null,
    no_data_since_ms: null,
    reopened: false,
    ...overrides,
  };
}

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: "chn_1",
    name: "Ops",
    type: "webhook",
    enabled: true,
    default: true,
    url_origin: "https://hooks.example.com",
    version: 1,
    created_at_ms: 0,
    updated_at_ms: 0,
    health: { last_delivered_at_ms: null, last_failed_at_ms: 0, consecutive_failures: 2 },
    ...overrides,
  };
}

const minute = (n: number, met: boolean, whyNot: string | null = null): PreviewMinute => ({
  minute: n,
  value: whyNot === null ? 1 : null,
  why_not: whyNot,
  met,
});

describe("evaluatorBanner", () => {
  it.each(["ok", "disabled"])("shows nothing when the gateway says %s", (state) => {
    expect(evaluatorBanner({ alert_evaluator: state }, false)).toBeNull();
  });

  it("says stale and unknown in their own words", () => {
    expect(evaluatorBanner({ alert_evaluator: "stale" }, false)?.state).toBe("stale");
    expect(evaluatorBanner({ alert_evaluator: "unknown" }, false)?.text).toContain("cannot read");
  });

  it("shows an answer it cannot read, never hides it", () => {
    expect(evaluatorBanner(null, true)?.state).toBe("unreadable");
    expect(evaluatorBanner({}, false)?.state).toBe("unreadable");
    expect(evaluatorBanner({ alert_evaluator: "fine" }, false)?.state).toBe("unreadable");
  });

  it("shows nothing before the first answer", () => {
    expect(evaluatorBanner(null, false)).toBeNull();
  });
});

describe("rules", () => {
  it("takes the body back without the id and the markers", () => {
    const body = ruleBody(rule());
    expect(Object.keys(body)).not.toContain("id");
    expect(Object.keys(body)).not.toContain("version");
    expect(Object.keys(body)).not.toContain("changed_from_default");
    expect(body.threshold).toBe(5);
  });

  it("words a condition", () => {
    expect(formatCondition(rule())).toBe("error_rate > 5 over 5 min, for 5 min");
    expect(
      formatCondition(rule({ signal: "rejections", parameter: "rate_limited", window: null })),
    ).toBe("rejections (rate_limited) > 5, for 5 min");
  });

  it("marks defaults and changed defaults as the gateway says", () => {
    expect(defaultMark(rule())).toBe("default");
    expect(defaultMark(rule({ changed_from_default: true }))).toBe("default, changed");
    expect(defaultMark(rule({ default_key: null }))).toBe("");
  });

  it("names channels from the gateway's list", () => {
    const names = [{ id: "chn_1", name: "Ops", type: "webhook", default: true, enabled: true }];
    expect(channelList(["organization defaults"], names)).toBe("organization defaults");
    expect(channelList(["chn_1", "chn_9"], names)).toBe("Ops, chn_9");
    expect(channelList([], names)).toBe("none");
  });

  it("gives the last minute's value, or too few to judge, or the incident's", () => {
    expect(lastValue({ minutes: [minute(1, false), minute(2, true)], met: 1 }, undefined)).toBe(
      "1",
    );
    expect(lastValue({ minutes: [minute(1, false, "too_few")], met: 0 }, undefined)).toBe(
      "too few to judge",
    );
    expect(lastValue({ minutes: [minute(1, false, "incomplete")], met: 0 }, undefined)).toBe(
      "incomplete",
    );
    expect(lastValue(null, incident())).toBe("7.50 (when it opened)");
    expect(lastValue(null, undefined)).toBe("not previewed");
  });

  it("gives each rule its open incident's state, or whether it is on", () => {
    const rows = ruleRows(
      [rule(), rule({ id: "rul_2", enabled: false }), rule({ id: "rul_3" })],
      {},
      [incident(), incident({ id: "inc_2", rule_id: "rul_3", state: "resolved" })],
      [],
    );
    expect(rows.map((r) => r.state)).toEqual(["firing", "disabled", "not firing"]);
    expect(isOpen(incident({ state: "acknowledged" }))).toBe(true);
  });

  it("drafts a body as JSON text and reads it back, naming fields that are not JSON", () => {
    const draft = initialRuleDraft({ threshold: 5, name: "x" });
    expect(draft).toEqual({ threshold: "5", name: '"x"' });
    expect(draftRule({ ...draft, threshold: "abc" })).toEqual({
      body: { name: "x" },
      unparsed: ["threshold"],
    });
  });

  it("sums up the preview as runs of minutes in which the condition held", () => {
    // 10:14 is minute 614 of 1970-01-01.
    const summary = previewSummary([
      minute(613, false),
      minute(614, true),
      minute(615, true),
      minute(616, false, "too_few"),
      minute(617, true),
    ]);
    expect(summary.runs).toEqual([
      "10:14 UTC to 10:16 UTC (2 minutes)",
      "10:17 UTC to 10:18 UTC (1 minute)",
    ]);
    expect(summary.tooFew).toBe(1);
    expect(summary.total).toBe(5);
    expect(previewSummary([minute(1, false)]).runs).toEqual([]);
    expect(formatMinute(614)).toBe("10:14 UTC");
  });

  it("breaks a run at a missing minute", () => {
    expect(previewSummary([minute(614, true), minute(616, true)]).runs).toHaveLength(2);
  });
});

describe("incidents", () => {
  it("names each incident's rule, and the built-in rule's by the gateway's meaning", () => {
    const rows = incidentRows(
      [incident(), incident({ id: "inc_2", application_id: null, rule_id: "rul_builtin" })],
      { "app-one": { rul_1: "Error rate above 5 percent" } },
    );
    expect(rows[0]?.rule).toBe("Error rate above 5 percent");
    expect(rows[1]?.rule).toBe("channel delivery failing (built-in)");
    expect(rows[1]?.application).toBe("the organization (built-in)");
  });

  it("lays out the timeline in order", () => {
    const timeline = incidentTimeline(
      incident({
        acknowledged_at_ms: 180_000,
        acknowledged_by: "usr_1",
        resolved_at_ms: 240_000,
        resolve_reason: "cleared",
      }),
    );
    expect(timeline.map(([, what]) => what)).toEqual([
      "Opened",
      "Fired",
      "Acknowledged by usr_1",
      "Resolved (cleared)",
    ]);
  });

  it("names each delivery's channel and outcome", () => {
    const detail: IncidentDetail = {
      ...incident(),
      notifications: [
        {
          kind: "firing",
          state: "recorded",
          at_ms: 120_000,
          reason: null,
          deliveries: [
            { channel_id: "chn_1", state: "failed", attempts: 2, last_code: "http_5xx" },
          ],
        },
      ],
    };
    const names = [{ id: "chn_1", name: "Ops", type: "webhook", default: true, enabled: true }];
    expect(notificationRows(detail, names)[0]?.deliveries).toBe(
      "Ops: failed (2 attempts, http_5xx)",
    );
  });
});

describe("channels", () => {
  it("shows a webhook's origin and an email channel's recipients by state, never a URL", () => {
    expect(channelTarget(channel())).toBe("https://hooks.example.com");
    const recipients = [
      {
        id: "r1",
        address: "a@example.com",
        state: "verified",
        added_at_ms: 0,
        verified_at_ms: 1,
        confirmation_expires_at_ms: null,
      },
      {
        id: "r2",
        address: "b@example.com",
        state: "unverified",
        added_at_ms: 0,
        verified_at_ms: null,
        confirmation_expires_at_ms: 5,
      },
    ];
    expect(channelTarget(channel({ type: "email", url_origin: null, recipients }))).toBe(
      "2 recipients, 1 verified",
    );
    expect(channelRows([channel()])[0]?.failures).toBe(2);
    expect(recipientRows(recipients).map((r) => r.unverified)).toEqual([false, true]);
  });

  it("lists deliveries' states", () => {
    const rows = deliveryRows([
      {
        id: "dlv_1",
        notification_id: "ntf_1",
        channel_id: "chn_1",
        state: "delivered",
        attempts: 1,
        next_attempt_at_ms: null,
        last_code: null,
        created_at_ms: 0,
        updated_at_ms: 0,
      },
    ]);
    expect(rows[0]).toMatchObject({ state: "delivered", lastCode: "-", next: "-" });
  });

  it("reads one address a line", () => {
    expect(addressLines(" a@example.com \r\n\nb@example.com\n")).toEqual([
      "a@example.com",
      "b@example.com",
    ]);
  });
});

describe("followsProbe", () => {
  it("shows a control only on the gateway's yes", () => {
    expect(followsProbe("allowed")).toBe(true);
    expect(followsProbe("forbidden")).toBe(false);
    expect(followsProbe("unknown")).toBe(false);
    expect(followsProbe(null)).toBe(false);
  });
});
