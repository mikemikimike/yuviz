"use client";

import { useState } from "react";
import { ApiError, CallerIdOption } from "@/lib/api";

export interface DialingValues {
  caller_id: string;
  max_attempts: number;
  retry_gap_minutes: number | "";
  max_concurrent_calls: number;
}

export const DEFAULT_RETRY_GAP_MINUTES = 60;

const PATH_LABELS: Record<string, string> = { native: "Native SIP", vobiz: "Vobiz", cloudonix: "Cloudonix" };
const pathLabel = (path: string) => PATH_LABELS[path] ?? path;

// Mirrors services/campaigns/schemas.py: attempts, gap and concurrency are all >= 1.
export function validateDialing(v: DialingValues): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!(v.max_attempts >= 1)) errors.max_attempts = "Must be at least 1.";
  if (v.retry_gap_minutes !== "" && !(v.retry_gap_minutes >= 1)) errors.retry_gap_minutes = "Must be at least 1 minute.";
  if (!(v.max_concurrent_calls >= 1)) errors.max_concurrent_calls = "Must be at least 1.";
  return errors;
}

const FORM_FIELDS = ["caller_id", "max_attempts", "retry_gap_minutes", "max_concurrent_calls"];

// Maps a server error to a form field: list detail by `loc`, string detail by a leading field token.
export function serverFieldError(e: unknown): { field: string | null; message: string } {
  if (!(e instanceof ApiError)) return { field: null, message: String(e) };
  const raw = e.body?.detail;
  if (Array.isArray(raw)) {
    const loc = raw[0]?.loc;
    const last = Array.isArray(loc) ? String(loc[loc.length - 1]) : null;
    return { field: last && FORM_FIELDS.includes(last) ? last : null, message: e.detail };
  }
  const lead = e.detail.split(" ")[0];
  return { field: FORM_FIELDS.includes(lead) ? lead : null, message: e.detail };
}

interface Props {
  values: DialingValues;
  onChange: (values: DialingValues) => void;
  callerIds: CallerIdOption[] | null;
  errors?: Record<string, string>;
}

export function CampaignDialingFields({ values, onChange, callerIds, errors = {} }: Props) {
  const [forceCustom, setForceCustom] = useState(false);
  const localErrors = { ...validateDialing(values), ...errors };
  const selected = callerIds?.find((c) => c.did === values.caller_id);
  const custom = forceCustom || (callerIds !== null && values.caller_id !== "" && !selected);
  const set = (patch: Partial<DialingValues>) => onChange({ ...values, ...patch });
  const fieldError = (field: string) =>
    localErrors[field] && <div className="form-hint" style={{ color: "var(--red)" }}>{localErrors[field]}</div>;

  return (
    <>
      <div className="form-group">
        <label className="form-label" htmlFor="campaign-field-caller_id">Caller ID</label>
        {!custom ? (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              id="campaign-field-caller_id"
              className="form-select"
              value={values.caller_id}
              onChange={(e) => {
                if (e.target.value === "__custom__") {
                  setForceCustom(true);
                  set({ caller_id: "" });
                } else {
                  set({ caller_id: e.target.value });
                }
              }}
            >
              <option value="">Select a caller ID…</option>
              {(callerIds ?? []).map((c) => (
                <option key={c.did} value={c.did}>
                  {c.did} · {pathLabel(c.path)}
                </option>
              ))}
              <option value="__custom__">Custom…</option>
            </select>
            {selected && <span className="badge gray">{pathLabel(selected.path)}</span>}
          </div>
        ) : (
          <div style={{ display: "flex", gap: 8 }}>
            <input
              id="campaign-field-caller_id"
              className="form-input"
              style={{ fontFamily: "var(--mono)" }}
              value={values.caller_id}
              onChange={(e) => set({ caller_id: e.target.value })}
              placeholder="5000"
            />
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => {
                setForceCustom(false);
                set({ caller_id: "" });
              }}>
              Use a registered number
            </button>
          </div>
        )}
        {fieldError("caller_id")}
        {!custom && callerIds?.length === 0 && (
          <div className="form-hint">No caller IDs on this account — pick &quot;Custom…&quot; to enter one.</div>
        )}
      </div>

      <div style={{ display: "flex", gap: 12 }}>
        <div className="form-group" style={{ flex: 1 }}>
          <label className="form-label" htmlFor="campaign-field-max_concurrent_calls">
            Max concurrent calls <span className="hint">calls in flight at once</span>
          </label>
          <input
            id="campaign-field-max_concurrent_calls"
            type="number"
            min={1}
            className="form-input"
            value={values.max_concurrent_calls}
            onChange={(e) => set({ max_concurrent_calls: Number(e.target.value) })}
          />
          {fieldError("max_concurrent_calls")}
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label className="form-label" htmlFor="campaign-field-max_attempts">
            Max attempts <span className="hint">retry ladder per contact</span>
          </label>
          <input
            id="campaign-field-max_attempts"
            type="number"
            min={1}
            className="form-input"
            value={values.max_attempts}
            onChange={(e) => set({ max_attempts: Number(e.target.value) })}
          />
          {fieldError("max_attempts")}
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label className="form-label" htmlFor="campaign-field-retry_gap_minutes">
            Retry gap (minutes) <span className="hint">blank = {DEFAULT_RETRY_GAP_MINUTES}</span>
          </label>
          <input
            id="campaign-field-retry_gap_minutes"
            type="number"
            min={1}
            className="form-input"
            value={values.retry_gap_minutes}
            onChange={(e) => set({ retry_gap_minutes: e.target.value === "" ? "" : Number(e.target.value) })}
          />
          {fieldError("retry_gap_minutes")}
        </div>
      </div>
    </>
  );
}
