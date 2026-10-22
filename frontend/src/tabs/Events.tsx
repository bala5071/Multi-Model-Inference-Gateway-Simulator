import { useState } from "react";
import {
  ANY_EVENT,
  ago,
  describeEvent,
  distinct,
  eventTitle,
  filterEvents,
  type EventFilter,
} from "../api/derive";
import type { MonitorState } from "../api/store";
import { EVENT_TYPES, type GatewayEvent } from "../api/types";

function Choice(props: {
  label: string;
  value: string;
  options: readonly string[];
  render?: (option: string) => string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="choice">
      {props.label}
      <select
        value={props.value}
        onChange={(event) => {
          props.onChange(event.target.value);
        }}
      >
        <option value="">Any</option>
        {props.options.map((option) => (
          <option key={option} value={option}>
            {props.render?.(option) ?? option}
          </option>
        ))}
      </select>
    </label>
  );
}

/** The live feed of gateway events, newest first, with filters and a pause. */
export function Events({ state }: { state: MonitorState }) {
  const [filter, setFilter] = useState<EventFilter>(ANY_EVENT);
  const [held, setHeld] = useState<{ events: GatewayEvent[]; nowMs: number } | null>(null);
  const snapshot = state.snapshot;
  if (snapshot === null) return <p className="empty">Waiting for the first snapshot…</p>;

  const shown = held ?? { events: state.events, nowMs: snapshot.at_ms };
  const matching = filterEvents(shown.events, filter);
  const types = [...new Set([...EVENT_TYPES, ...distinct(shown.events, "type")])];

  return (
    <>
      <div className="filters">
        <Choice
          label="Type"
          value={filter.type}
          options={types}
          render={eventTitle}
          onChange={(type) => {
            setFilter({ ...filter, type });
          }}
        />
        <Choice
          label="Model"
          value={filter.model}
          options={Object.keys(snapshot.models)}
          onChange={(model) => {
            setFilter({ ...filter, model });
          }}
        />
        <Choice
          label="Application"
          value={filter.application}
          options={distinct(shown.events, "application_id")}
          onChange={(application) => {
            setFilter({ ...filter, application });
          }}
        />
        <button
          type="button"
          aria-pressed={held !== null}
          onClick={() => {
            setHeld(held === null ? { events: state.events, nowMs: snapshot.at_ms } : null);
          }}
        >
          {held === null ? "Pause" : "Resume"}
        </button>
      </div>
      <p className="muted">
        Showing {matching.length} of {shown.events.length} events held, newest first.
        {held !== null && " Paused: new events are not shown."}
        {state.missedEvents > 0 &&
          ` ${state.missedEvents} older events had already left the log of the gateway.`}
      </p>
      {matching.length === 0 ? (
        <p className="empty">
          {shown.events.length === 0 ? "No events have been published yet." : "No events match."}
        </p>
      ) : (
        <ol className="events" aria-label="Events">
          {matching.map((event) => (
            <li key={event.seq} className="event">
              <span className="event-type">{eventTitle(event.type)}</span>
              <span className="event-text">{describeEvent(event)}</span>
              <span className="muted">
                {event.application_id !== null && `${event.application_id} · `}
                {ago(event.at_ms, shown.nowMs)} · #{event.seq}
              </span>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}
