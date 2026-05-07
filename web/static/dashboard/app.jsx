/* global React, ReactDOM, useTweaks, TweaksPanel, TweakSection, TweakRadio, TweakToggle */
const { useState, useEffect, useRef } = React;

/* ────────────────────────────────────────────────────────────
   Seed data — shown while real data is loading
   ──────────────────────────────────────────────────────────── */

const SEED_INCIDENTS = [
  { plate: "—", time: "—", note: "No incidents recorded yet", source: "button", duration: "" },
];

/* ────────────────────────────────────────────────────────────
   API hooks
   ──────────────────────────────────────────────────────────── */

function useLiveALPR() {
  const [data, setData] = useState({
    running: false, ready: false, sightings: [], best: null,
    frames_scanned: 0, detections_seen: 0, mode: "—",
  });
  useEffect(() => {
    let inFlight = false;
    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 2500);
      try {
        const r = await fetch("/alpr/live/status", { signal: controller.signal });
        if (r.ok) setData(await r.json());
      } catch {}
      finally {
        clearTimeout(timeout);
        inFlight = false;
      }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return data;
}

function useHealth() {
  const [data, setData] = useState(null);
  useEffect(() => {
    const tick = async () => {
      try {
        const r = await fetch("/health");
        if (r.ok) setData(await r.json());
      } catch {}
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => clearInterval(id);
  }, []);
  return data;
}

function useRecordingStatus() {
  const [data, setData] = useState({ recording: false, enabled: true, segments_completed: 0 });
  useEffect(() => {
    const tick = async () => {
      try {
        const r = await fetch("/recording/status");
        if (r.ok) setData(await r.json());
      } catch {}
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => clearInterval(id);
  }, []);
  return data;
}

function useApplianceStatus() {
  const [data, setData] = useState(null);
  useEffect(() => {
    const tick = async () => {
      try {
        const r = await fetch("/appliance/status");
        if (r.ok) setData(await r.json());
      } catch {}
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return data;
}

function useIncidents() {
  const [data, setData] = useState(SEED_INCIDENTS);
  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch("/incidents/json?limit=4");
        if (!r.ok) return;
        const body = await r.json();
        const rows = (body.incidents || []).map(inc => {
          const meta = inc.meta || {};
          const src = meta.trigger_source || "web";
          const durationSec = Math.round((meta.total_frames || 0) / 30);
          return {
            plate: inc.plate || "—",
            time: formatIncidentTime(inc.timestamp),
            note: meta.parsed_note || "",
            source: src === "web" ? "button" : src,
            duration: durationSec ? `${durationSec}s` : "",
          };
        });
        if (rows.length) setData(rows);
      } catch {}
    };
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);
  return data;
}

function formatIncidentTime(ts) {
  if (!ts) return "—";
  try {
    const s = String(ts);
    const m = s.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$/);
    if (m) return `${m[4]}:${m[5]}:${m[6]}`;
    const d = new Date(s);
    if (!isNaN(d)) return d.toLocaleTimeString("en-CA", { hour12: false });
  } catch {}
  return "—";
}

function transformSightings(sightings) {
  return (sightings || []).map(s => ({
    plate: s.plate,
    confidence: s.best_confidence,
    ago: s.last_seen_label,
    active: s.status === "visible",
    count: s.seen_count,
    region: (s.plate || "").replace(/\s/g, "").slice(0, 2),
  }));
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "—";
  const s = Math.max(0, Math.round(Number(seconds)));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`;
}

function fmtShortTime(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    if (!isNaN(d)) return d.toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {}
  return "—";
}

/* ────────────────────────────────────────────────────────────
   Icons (Lucide-style, 1.5px stroke, currentColor)
   ──────────────────────────────────────────────────────────── */
const Icon = ({ size = 20, className = "", children }) => (
  <svg
    width={size} height={size} viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="1.5"
    strokeLinecap="round" strokeLinejoin="round"
    className={className} aria-hidden="true"
  >
    {children}
  </svg>
);

const Icons = {
  camera:    () => <Icon><rect x="2.5" y="6" width="19" height="13" rx="2"/><circle cx="12" cy="12.5" r="3.5"/><path d="M8 6l1.5-2h5L16 6"/></Icon>,
  grid:      () => <Icon><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></Icon>,
  rec:       () => <Icon><circle cx="12" cy="12" r="6"/></Icon>,
  alert:     () => <Icon><path d="M12 3l10 18H2L12 3z"/><path d="M12 10v5"/><path d="M12 18.2v.1"/></Icon>,
  flag:      () => <Icon><path d="M5 21V4"/><path d="M5 4h11l-2 4 2 4H5"/></Icon>,
  mic:       () => <Icon><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></Icon>,
  micOff:    () => <Icon><path d="M3 3l18 18"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12"/><path d="M15 11V6a3 3 0 0 0-5.66-1.4"/><path d="M5 11a7 7 0 0 0 11.6 5.3"/><path d="M19 11a7 7 0 0 1-.6 2.8"/><path d="M12 18v3"/></Icon>,
  pin:       () => <Icon><path d="M12 21v-7"/><path d="M9 4h6l-1 5 3 3H7l3-3-1-5z"/></Icon>,
  hdd:       () => <Icon><rect x="3" y="13" width="18" height="6" rx="1"/><path d="M5 13l2-7h10l2 7"/><circle cx="7" cy="16" r="0.6" fill="currentColor"/><circle cx="10" cy="16" r="0.6" fill="currentColor"/></Icon>,
  pause:     () => <Icon><rect x="7" y="5" width="3.5" height="14" rx="0.5"/><rect x="13.5" y="5" width="3.5" height="14" rx="0.5"/></Icon>,
  play:      () => <Icon><path d="M7 5l12 7-12 7V5z"/></Icon>,
  bolt:      () => <Icon><path d="M13 3L4 14h6l-1 7 9-11h-6l1-7z"/></Icon>,
  battery:   () => <Icon><rect x="3" y="7" width="16" height="10" rx="2"/><path d="M21 11v2"/><path d="M7 11h6"/></Icon>,
  plate:     () => <Icon><rect x="3" y="7" width="18" height="10" rx="1.5"/><path d="M7 11h2M11 11h2M15 11h2M7 14h10"/></Icon>,
  car:       () => <Icon><path d="M5 16V11l2-5h10l2 5v5"/><path d="M3 16h18v3H3z"/><circle cx="7.5" cy="16" r="1.2"/><circle cx="16.5" cy="16" r="1.2"/></Icon>,
  swap:      () => <Icon><path d="M3 8h14l-3-3M21 16H7l3 3"/></Icon>,
  crosshair: () => <Icon><circle cx="12" cy="12" r="7"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></Icon>,
};

/* ────────────────────────────────────────────────────────────
   Camera viewport — real MJPEG feed from /camera/stream
   ──────────────────────────────────────────────────────────── */

function CameraViewport({ overlays = true, frozen = false, best = null }) {
  const [ts, setTs] = useState("");
  useEffect(() => {
    const tick = () => {
      const d = new Date();
      setTs(d.toISOString().replace("T", " ").slice(0, 19) + "Z");
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="feed">
      <img src="/camera/stream" className="feed-img" alt="Live camera"/>

      {overlays && (
        <>
          {best && best.vehicle_bbox && (() => {
            const vbox = best.vehicle_bbox;
            const fw = best.frame_w || 0;
            const fh = best.frame_h || 0;
            if (!vbox || !fw || !fh) return null;
            return (
              <div className="vehicle-box" style={{
                left:   `${(vbox[0] / fw) * 100}%`,
                top:    `${(vbox[1] / fh) * 100}%`,
                width:  `${((vbox[2] - vbox[0]) / fw) * 100}%`,
                height: `${((vbox[3] - vbox[1]) / fh) * 100}%`,
              }}>
                <span className="vehicle-tag">{best.vehicle_type || "vehicle"}</span>
              </div>
            );
          })()}

          {best && best.plate && (() => {
            const box = best.bbox;
            const fw = best.frame_w || 0;
            const fh = best.frame_h || 0;
            const conf = (best.best_confidence ?? best.confidence) || 0;
            const style = (box && fw && fh) ? {
              left:   `${(box[0] / fw) * 100}%`,
              top:    `${(box[1] / fh) * 100}%`,
              width:  `${((box[2] - box[0]) / fw) * 100}%`,
              height: `${((box[3] - box[1]) / fh) * 100}%`,
            } : {};
            return (
              <div className="alpr-box" style={style}>
                <span className="alpr-tag">
                  <span className="alpr-pulse"></span>
                  {best.plate}&nbsp;<span className="alpr-conf">{Math.round(conf * 100)}%</span>
                </span>
              </div>
            );
          })()}

          <div className="burn-stamp">{ts}</div>

          <div className="reticle">
            <span className="r-h"></span>
            <span className="r-v"></span>
          </div>

          <span className="corner-bl"></span>
          <span className="corner-br"></span>
        </>
      )}

      {frozen && <div className="freeze-pane">PAUSED</div>}
    </div>
  );
}

/* ────────────────────────────────────────────────────────────
   Top status bar
   ──────────────────────────────────────────────────────────── */

function TopBar({ view, setView, recording, muted, setMuted, recStatus }) {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  const ss = String(now.getSeconds()).padStart(2, "0");

  const segCount = recStatus?.segments_completed || 0;

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark"><Icons.crosshair/></span>
        <span className="brand-word">DASHLOG</span>
        <span className="brand-sep">//</span>
        <span className="brand-sub">UNIT 047 · IN-VEHICLE LOGGER</span>
      </div>

      <div className="topbar-mid">
        <div className="seg" role="tablist">
          <button className={`seg-btn ${view === "live" ? "is-on" : ""}`} onClick={() => setView("live")}>
            <Icons.camera/> <span>Live</span>
          </button>
          <button className={`seg-btn ${view === "controls" ? "is-on" : ""}`} onClick={() => setView("controls")}>
            <Icons.grid/> <span>Controls</span>
          </button>
        </div>
      </div>

      <div className="topbar-right">
        <span className={`rec-chip ${recording ? "is-on" : ""}`}>
          <span className="rec-dot"></span>
          <span className="mono">{recording ? "REC" : "OFF"}</span>
          <span className="muted mono">·</span>
          <span className="muted mono">{recording ? `seg ${segCount}` : "stopped"}</span>
        </span>
        <button
          className={`icon-btn ${muted ? "is-warn" : ""}`}
          title={muted ? "Audio muted" : "Audio recording"}
          onClick={() => setMuted(!muted)}
        >
          {muted ? <Icons.micOff/> : <Icons.mic/>}
        </button>
        <div className="clock">
          <span className="clock-hm mono">{hh}:{mm}</span>
          <span className="clock-s mono">{ss}</span>
        </div>
      </div>
    </header>
  );
}

/* ────────────────────────────────────────────────────────────
   Telemetry strip — live ALPR stats (GPS not in this system)
   ──────────────────────────────────────────────────────────── */

function TelemetryStrip({ alpr }) {
  const active    = (alpr.sightings || []).filter(s => s.status === "visible").length;
  const bestPlate = alpr.best ? alpr.best.plate : "—";
  const bestConf  = alpr.best ? `${Math.round(((alpr.best.best_confidence ?? alpr.best.confidence) || 0) * 100)}%` : "—";

  return (
    <div className="telemetry">
      <Stat label="alpr"       value={alpr.running ? "scanning" : "offline"} mono/>
      <Stat label="best plate" value={bestPlate} mono/>
      <Stat label="confidence" value={bestConf} mono/>
      <Stat label="visible"    value={active} unit="plates"/>
      <Stat label="detections" value={alpr.detections_seen || 0} mono/>
      <Stat label="frames"     value={alpr.frames_scanned || 0} mono/>
    </div>
  );
}

function Stat({ label, value, unit, mono }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${mono ? "mono" : "fig"}`}>
        {value}{unit && <span className="stat-unit"> {unit}</span>}
      </span>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────
   Sightings panel
   ──────────────────────────────────────────────────────────── */

function SightingsPanel({ rows, running }) {
  return (
    <section className="card sightings">
      <div className="card-head">
        <span className="eyebrow">Live plate sightings</span>
        <span className={`pill ${running ? "pill-accent" : ""}`}>
          {running && <span className="pulse"></span>}
          {running ? " scanning" : " offline"}
        </span>
      </div>
      <ul className="sight-list">
        {rows.length === 0 && (
          <li className="sight-row">
            <span className="sight-plate mono muted">no plates detected</span>
          </li>
        )}
        {rows.map((r, i) => (
          <li key={i} className={`sight-row ${r.active ? "is-active" : ""} ${r.flagged ? "is-flag" : ""}`}>
            <span className="sight-plate mono">{r.plate}</span>
            <span className="sight-meta">
              <span className="sight-conf fig">{Math.round((r.confidence || 0) * 100)}<span className="muted">%</span></span>
              <span className="sight-count mono">×{r.count}</span>
              <span className="sight-region mono">{r.region}</span>
              {r.flagged && <span className="sight-flag"><Icons.flag/></span>}
            </span>
            <span className="sight-ago mono">{r.ago}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
   Recent incidents
   ──────────────────────────────────────────────────────────── */

function IncidentsPanel({ rows }) {
  return (
    <section className="card incidents">
      <div className="card-head">
        <span className="eyebrow">Recent incidents</span>
        <a href="/incidents" className="link-mono">all →</a>
      </div>
      <ul className="inc-list">
        {rows.map((r, i) => (
          <li key={i} className="inc-row">
            <span className={`inc-source src-${r.source}`}>
              {r.source === "button"   && <Icons.bolt/>}
              {r.source === "alpr"     && <Icons.plate/>}
              {r.source === "g-sensor" && <Icons.alert/>}
              {r.source === "parking"  && <Icons.car/>}
            </span>
            <span className="inc-plate mono">{r.plate}</span>
            <span className="inc-note">{r.note}</span>
            <span className="inc-meta mono">
              {r.time} <span className="muted">{r.duration ? `· ${r.duration}` : ""}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
   Health rail — real system status from /health
   ──────────────────────────────────────────────────────────── */

function AppliancePanel({ appliance }) {
  const power = appliance?.power || {};
  const onBattery = power.on_ac === false;
  const onAc = power.on_ac === true;
  const pct = power.battery_percent;
  const remaining = appliance?.grace_remaining_seconds;
  const grace = appliance?.grace_seconds || 0;
  const used = onBattery && grace > 0 ? Math.max(0, Math.min(1, 1 - (Number(remaining || 0) / grace))) : 0;
  const tone = onBattery ? "warn" : onAc ? "ok" : "warn";
  const stateLabel = onBattery ? "battery" : onAc ? "AC online" : "unknown";
  const sub = onBattery
    ? `suspend in ${fmtDuration(remaining)} · ${pct ?? "—"}%`
    : `${pct ?? "—"}% battery · watcher ${fmtShortTime(appliance?.watcher_updated_at)}`;

  return (
    <section className={`card appliance appliance-${tone}`}>
      <div className="card-head">
        <span className="eyebrow">Linux appliance</span>
        <span className={`pill ${onBattery ? "pill-warn" : "pill-ok"}`}>
          {onBattery && <span className="pulse"></span>}
          {stateLabel}
        </span>
      </div>
      <div className="appliance-main">
        <span className="appliance-icon"><Icons.battery/></span>
        <span className="appliance-copy">
          <span className="appliance-title mono">{onBattery ? fmtDuration(remaining) : "armed"}</span>
          <span className="appliance-sub mono">{sub}</span>
        </span>
      </div>
      <div className="appliance-bar"><span style={{ width: `${used * 100}%` }}></span></div>
      <div className="appliance-mini mono">
        <span>cam {appliance?.camera_running ? "on" : "off"}</span>
        <span>rec {appliance?.recording ? "on" : "off"}</span>
        <span>resume {fmtShortTime(appliance?.last_resume_at)}</span>
      </div>
    </section>
  );
}

function HealthRail({ health, alpr }) {
  const cam   = health?.components?.camera;
  const disk  = health?.disk;
  const alprC = health?.components?.alpr;
  const rec   = health?.components?.loop_recorder;
  const buf   = health?.components?.dashcam_buffer;

  const mapStatus = (s) => s === "green" ? "ok" : s === "red" ? "bad" : "warn";

  const items = [
    {
      label: "Camera",
      v: cam ? (cam.running ? "running" : "offline") : "—",
      state: cam ? mapStatus(cam.status) : "warn",
    },
    {
      label: "Storage",
      v: disk ? `${Number(disk.free_gb || 0).toFixed(0)} GB free` : "—",
      state: disk ? mapStatus(disk.status) : "warn",
      bar: disk ? (disk.percent_used || 0) / 100 : undefined,
    },
    {
      label: "ALPR",
      v: alprC ? (alprC.running ? "scanning" : alprC.ready ? "ready" : "offline")
               : (alpr?.running ? "scanning" : "—"),
      state: alprC ? mapStatus(alprC.status) : (alpr?.running ? "ok" : "warn"),
    },
    {
      label: "Recorder",
      v: rec ? (rec.recording ? `${rec.segments_completed || 0} segs` : "stopped") : "—",
      state: rec ? mapStatus(rec.status) : "warn",
    },
    {
      label: "Buffer",
      v: buf ? (buf.armed ? "armed" : "disarmed") : "—",
      state: buf ? mapStatus(buf.status) : "warn",
    },
    {
      label: "System",
      v: health ? health.status : "loading",
      state: !health ? "warn" : mapStatus(health.status),
    },
  ];

  return (
    <aside className="health-rail">
      {items.map((it, i) => (
        <div key={i} className={`hr-row hr-${it.state}`}>
          <span className="hr-dot"></span>
          <span className="hr-label">{it.label}</span>
          <span className="hr-val mono">{it.v}</span>
        </div>
      ))}
    </aside>
  );
}

/* ────────────────────────────────────────────────────────────
   Incident button — POSTs to /dashcam/trigger
   ──────────────────────────────────────────────────────────── */

function IncidentButton({ size = "lg", onCapture }) {
  const [state, setState] = useState("idle");
  const [progress, setProgress] = useState(0);
  const holdRef = useRef(null);

  const start = () => {
    if (state !== "idle") return;
    setState("hold");
    setProgress(0);
    let p = 0;
    holdRef.current = setInterval(() => {
      p += 4;
      setProgress(p);
      if (p >= 100) {
        clearInterval(holdRef.current);
        runCapture();
      }
    }, 16);
  };

  const cancel = () => {
    if (state === "hold") {
      clearInterval(holdRef.current);
      setProgress(0);
      setState("idle");
    }
  };

  const runCapture = async () => {
    setState("capturing");
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      const r = await fetch("/dashcam/trigger", { method: "POST", signal: controller.signal });
      clearTimeout(timeout);
      const body = await r.json();
      if (body.ok) {
        await waitForCaptureDone(body);
      } else {
        setState("error");
        setTimeout(() => { setProgress(0); setState("idle"); }, 2500);
      }
    } catch {
      setState("error");
      setTimeout(() => { setProgress(0); setState("idle"); }, 2500);
    }
  };

  const waitForCaptureDone = async (initialBody) => {
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      try {
        const r = await fetch("/dashcam/status");
        if (r.ok) {
          const body = await r.json();
          if (body.capture_state === "saving") setState("saving");
          if (body.capture_state === "done") {
            setState("saved");
            onCapture && onCapture(body.last_result || initialBody);
            setTimeout(() => { setProgress(0); setState("idle"); }, 1800);
            return;
          }
          if (body.capture_state === "error") {
            setState("error");
            setTimeout(() => { setProgress(0); setState("idle"); }, 3500);
            return;
          }
        }
      } catch {}
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    setState("error");
    setTimeout(() => { setProgress(0); setState("idle"); }, 3500);
  };

  const tap = () => { if (state === "idle") runCapture(); };

  const label =
    state === "idle"      ? "INCIDENT"  :
    state === "hold"      ? "HOLD…"     :
    state === "capturing" ? "CAPTURING" :
    state === "saving"    ? "SAVING"    :
    state === "error"     ? "ERROR"     : "SAVED";

  const sub =
    state === "idle"      ? "hold 1.6s · save 30s+5s clip" :
    state === "hold"      ? `${Math.round(progress)}%`     :
    state === "capturing" ? "post-roll + encode"           :
    state === "saving"    ? "writing mp4 + json"           :
    state === "error"     ? "check service logs"           : "data/dashcam/";

  return (
    <button
      className={`incident-btn incident-${size} state-${state}`}
      onMouseDown={start} onMouseUp={cancel} onMouseLeave={cancel}
      onTouchStart={start} onTouchEnd={cancel}
      onClick={tap}
    >
      <svg className="incident-ring" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="46" className="ring-track"/>
        <circle cx="50" cy="50" r="46" className="ring-fill"
          style={{ strokeDashoffset: 289.0 * (1 - progress / 100) }}
        />
      </svg>
      <span className="incident-core">
        <span className="incident-dot"></span>
        <span className="incident-label">{label}</span>
        <span className="incident-sub mono">{sub}</span>
      </span>
    </button>
  );
}

/* ────────────────────────────────────────────────────────────
   View 1 — LIVE
   ──────────────────────────────────────────────────────────── */

function LiveView({ muted, sightings, incidents, frozen, setFrozen, alpr, health, appliance }) {
  return (
    <div className="live-view">
      <div className="live-stage">
        <CameraViewport frozen={frozen} best={alpr.best}/>
        <div className="feed-controls">
          <button className="ctl-pill" onClick={() => setFrozen(!frozen)}>
            {frozen ? <Icons.play/> : <Icons.pause/>}
            <span>{frozen ? "Resume" : "Pause"}</span>
          </button>
          <button className="ctl-pill">
            <Icons.pin/> <span>Lock segment</span>
          </button>
          <button className="ctl-pill">
            <Icons.swap/> <span>Front cam</span>
          </button>
        </div>
        <TelemetryStrip alpr={alpr}/>
      </div>

      <div className="live-side">
        <IncidentButton size="md"/>
        <SightingsPanel rows={sightings.slice(0, 5)} running={alpr.running}/>
        <AppliancePanel appliance={appliance}/>
        <HealthRail health={health} alpr={alpr}/>
      </div>

      <div className="live-bottom">
        <IncidentsPanel rows={incidents.slice(0, 3)}/>
      </div>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────
   View 2 — CONTROLS
   ──────────────────────────────────────────────────────────── */

function ControlsView({ recording, muted, setMuted, health, alpr, appliance }) {
  const [tags, setTags] = useState({});
  const flash = (k) => {
    setTags(t => ({ ...t, [k]: true }));
    setTimeout(() => setTags(t => ({ ...t, [k]: false })), 900);
  };

  const disk = health?.disk;
  const storeSub = disk
    ? `${Number(disk.free_gb || 0).toFixed(0)} GB free of ${Number(disk.total_gb || 0).toFixed(0)} · 7‑day loop`
    : "storage info loading…";
  const storeBar = disk ? (disk.percent_used || 0) / 100 : undefined;

  const activePlates = (alpr?.sightings || []).filter(s => s.status === "visible").length;
  const bestPlate = alpr?.best ? alpr.best.plate : "none";
  const power = appliance?.power || {};
  const onBattery = power.on_ac === false;
  const powerTitle = onBattery ? "Battery grace" : power.on_ac === true ? "AC online" : "Power unknown";
  const powerSub = onBattery
    ? `suspend in ${fmtDuration(appliance?.grace_remaining_seconds)} · ${power.battery_percent ?? "—"}% battery`
    : `${power.battery_percent ?? "—"}% battery · suspend watcher armed`;

  return (
    <div className="controls-view">
      <div className="ctl-hero">
        <div className="ctl-hero-text">
          <span className="eyebrow">at‑a‑glance</span>
          <h1 className="display">
            <em>One tap.</em> Pre-roll, audio, and ALPR plate are committed to disk before you've changed lanes.
          </h1>
        </div>
        <IncidentButton size="xl" onCapture={() => flash("incident")}/>
      </div>

      <div className="ctl-grid">
        <ActionCard
          title="Tag last 30s"
          sub="lock current segment from auto‑cleanup"
          icon={<Icons.pin/>}
          tone="ink"
          onClick={() => flash("lock")}
          active={tags.lock}
          activeText="Locked ✓"
        />
        <ActionCard
          title="Note plate"
          sub="hold to dictate, NATO phonetic"
          icon={<Icons.mic/>}
          tone="accent"
          big
          onClick={() => flash("note")}
          active={tags.note}
          activeText="Listening…"
          hold
        />
        <ActionCard
          title="Hazard event"
          sub="tag as near-miss · 60s clip"
          icon={<Icons.alert/>}
          tone="warn"
          onClick={() => flash("haz")}
          active={tags.haz}
          activeText="Saved as near-miss"
        />
        <ActionCard
          title={muted ? "Mic muted" : "Mic on"}
          sub={muted ? "video only on disk" : "audio mixed into segments"}
          icon={muted ? <Icons.micOff/> : <Icons.mic/>}
          tone={muted ? "muted" : "ink"}
          toggle={muted}
          onClick={() => setMuted(!muted)}
        />
        <ActionCard
          title={recording ? "Recording" : "Stopped"}
          sub={recording ? "60s segments · h264" : "loop recorder paused"}
          icon={<Icons.rec/>}
          tone={recording ? "danger" : "muted"}
          toggle={recording}
        />
        <ActionCard
          title="ALPR"
          sub={`${activePlates} plate${activePlates !== 1 ? "s" : ""} visible · best: ${bestPlate}`}
          icon={<Icons.plate/>}
          tone={alpr?.running ? "accent" : "muted"}
          toggle={alpr?.running || false}
        />
        <ActionCard
          title="Detections"
          sub={`${alpr?.detections_seen || 0} total · ${alpr?.frames_scanned || 0} frames scanned`}
          icon={<Icons.bolt/>}
          tone="muted"
          right={<span className="mono fig">{activePlates}<span className="muted"> vis</span></span>}
        />
        <ActionCard
          title="Storage"
          sub={storeSub}
          icon={<Icons.hdd/>}
          tone="muted"
          bar={storeBar}
        />
        <ActionCard
          title={powerTitle}
          sub={powerSub}
          icon={<Icons.battery/>}
          tone={onBattery ? "warn" : "ink"}
          toggle={appliance?.enabled || false}
        />
      </div>

      <div className="ctl-bottom">
        <div className="ctl-bottom-side">
          <span className="eyebrow">tap once · do not look down</span>
          <p className="body">
            The big button captures a 35‑second clip — 30 seconds of pre‑roll plus 5 seconds after — and writes the
            current ALPR plate and a metadata sidecar to disk. Everything else here is optional.
          </p>
        </div>
        <div className="ctl-bottom-meta">
          <Meta label="alpr status" value={alpr?.running ? "scanning" : "offline"} sub={alpr?.mode || "—"}/>
          <Meta label="best plate"  value={bestPlate} sub={alpr?.best ? `${Math.round(((alpr.best.best_confidence ?? alpr.best.confidence) || 0) * 100)}% conf` : "no detection"}/>
          <Meta label="disk free"   value={disk ? `${Number(disk.free_gb || 0).toFixed(0)} GB` : "—"} sub={disk ? `${disk.percent_used || 0}% used` : "loading"}/>
          <Meta label="power"       value={onBattery ? fmtDuration(appliance?.grace_remaining_seconds) : "AC"} sub={onBattery ? "until suspend" : `battery ${power.battery_percent ?? "—"}%`}/>
        </div>
      </div>
    </div>
  );
}

function ActionCard({ title, sub, icon, tone = "ink", big, hold, toggle, active, activeText, right, bar, onClick }) {
  return (
    <button
      className={`act-card tone-${tone} ${big ? "is-big" : ""} ${active ? "is-active" : ""} ${toggle === true ? "is-toggle-on" : toggle === false ? "is-toggle-off" : ""}`}
      onClick={onClick}
    >
      <span className="act-icon">{icon}</span>
      <span className="act-text">
        <span className="act-title">{active ? (activeText || title) : title}</span>
        <span className="act-sub mono">{sub}</span>
      </span>
      {right && <span className="act-right">{right}</span>}
      {hold && <span className="act-hold mono">HOLD</span>}
      {toggle !== undefined && (
        <span className="act-toggle"><span className="act-toggle-knob"></span></span>
      )}
      {bar !== undefined && (
        <span className="act-bar"><span className="act-bar-fill" style={{ width: `${bar * 100}%` }}></span></span>
      )}
    </button>
  );
}

function Meta({ label, value, sub }) {
  return (
    <div className="ctl-meta">
      <span className="eyebrow">{label}</span>
      <span className="ctl-meta-v fig">{value}</span>
      <span className="ctl-meta-sub mono">{sub}</span>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────
   Root
   ──────────────────────────────────────────────────────────── */

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "view": "live",
  "muted": false,
  "frozen": false,
  "density": "comfortable"
}/*EDITMODE-END*/;

function App() {
  const alpr      = useLiveALPR();
  const health    = useHealth();
  const recStatus = useRecordingStatus();
  const appliance = useApplianceStatus();
  const incidents = useIncidents();

  const [tweaks, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [view,   setView]  = useState(tweaks.view);
  const [muted,  setMuted] = useState(tweaks.muted);
  const [frozen, setFrozen] = useState(tweaks.frozen);

  useEffect(() => setView(tweaks.view),     [tweaks.view]);
  useEffect(() => setMuted(tweaks.muted),   [tweaks.muted]);
  useEffect(() => setFrozen(tweaks.frozen), [tweaks.frozen]);

  const sightings = transformSightings(alpr.sightings);
  const recording = recStatus.recording;

  return (
    <div className={`app density-${tweaks.density}`}>
      <TopBar
        view={view}
        setView={(v) => { setView(v); setTweak("view", v); }}
        recording={recording}
        muted={muted}
        setMuted={(m) => { setMuted(m); setTweak("muted", m); }}
        recStatus={recStatus}
      />

      <main className="stage">
        {view === "live" ? (
          <LiveView
            muted={muted}
            sightings={sightings}
            incidents={incidents}
            frozen={frozen}
            setFrozen={(f) => { setFrozen(f); setTweak("frozen", f); }}
            alpr={alpr}
            health={health}
            appliance={appliance}
          />
        ) : (
          <ControlsView
            recording={recording}
            muted={muted}
            setMuted={(m) => { setMuted(m); setTweak("muted", m); }}
            health={health}
            alpr={alpr}
            appliance={appliance}
          />
        )}
      </main>

      <TweaksPanel title="Tweaks">
        <TweakSection label="View">
          <TweakRadio
            label="Active screen"
            options={[{ value: "live", label: "Live" }, { value: "controls", label: "Controls" }]}
            value={tweaks.view}
            onChange={(v) => { setTweak("view", v); setView(v); }}
          />
        </TweakSection>
        <TweakSection label="State">
          <TweakToggle label="Audio muted"         value={tweaks.muted}  onChange={(v) => { setTweak("muted", v);  setMuted(v); }}/>
          <TweakToggle label="Frozen frame (live)"  value={tweaks.frozen} onChange={(v) => { setTweak("frozen", v); setFrozen(v); }}/>
        </TweakSection>
        <TweakSection label="Density">
          <TweakRadio
            label="Layout density"
            options={[{ value: "comfortable", label: "Comfortable" }, { value: "compact", label: "Compact" }]}
            value={tweaks.density}
            onChange={(v) => setTweak("density", v)}
          />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
