import { useCallback, useEffect, useRef, useState } from "react";
import { call, errorType, type ModelInfo, type Reply, type Schema } from "./router";

type Mode = "auto" | "predict" | "job";
type Entry = { id: number; at: string; model: string; endpoint: string; reply: Reply; request: unknown };

const get = (k: string) => {
  try {
    return sessionStorage.getItem(k) ?? "";
  } catch {
    return "";
  }
};
const put = (k: string, v: string) => {
  try {
    sessionStorage.setItem(k, v);
  } catch {
    /* storage blocked */
  }
};
const stored = () => {
  try {
    return localStorage.getItem("theme") === "light" ? "light" : "dark"; // the mark is drawn for a dark ground
  } catch {
    return "dark";
  }
};
const pretty = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));

function Json({ value }: { value: unknown }) {
  return <pre className="json">{pretty(value)}</pre>;
}

export function App() {
  const [key, setKey] = useState(get("apikey"));
  const [models, setModels] = useState<ModelInfo[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [schema, setSchema] = useState<Schema | null>(null);
  const [payload, setPayload] = useState("{\n  \"data\": null\n}");
  const [mode, setMode] = useState<Mode>("auto");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<Entry[]>([]);
  const [view, setView] = useState<Entry | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [health, setHealth] = useState<string>("…");
  const [theme, setTheme] = useState<"dark" | "light">(stored);
  const nextId = useRef(1);
  const polling = useRef(false);
  const loadSeq = useRef(0); // the key field reloads on every keystroke; only the latest response may win

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    try {
      localStorage.setItem("theme", theme);
    } catch {
      /* storage blocked */
    }
  }, [theme]);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    setListError(null);
    const [h, r] = await Promise.all([call("", "GET", "/health"), key ? call(key, "GET", "/models") : null]);
    if (seq !== loadSeq.current) return;
    setHealth(h.status === 200 ? "ok" : h.status === 0 ? "unreachable" : "degraded");
    if (!r) return setModels(null);
    if (r.ok) setModels((r.body as { models: ModelInfo[] }).models);
    else {
      setModels(null);
      setListError(r.status === 401 ? "That API key was rejected." : `Could not list models (${r.status}).`);
    }
  }, [key]);

  useEffect(() => {
    void load();
  }, [load]);

  async function select(name: string) {
    setSelected(name);
    setSchema(null);
    const r = await call(key, "GET", `/models/${name}/schema`);
    if (r.ok) {
      const s = r.body as Schema;
      setSchema(s);
      if (s.example !== undefined) setPayload(pretty(s.example));
    }
  }

  const record = (model: string, endpoint: string, request: unknown, reply: Reply) => {
    const entry: Entry = { id: nextId.current++, at: new Date().toLocaleTimeString(), model, endpoint, reply, request };
    setHistory((h) => [entry, ...h].slice(0, 30));
    setView(entry);
    return entry;
  };

  async function send() {
    if (!selected) return;
    let body: Record<string, unknown>;
    try {
      const parsed = JSON.parse(payload);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("must be a JSON object");
      body = parsed;
    } catch (e) {
      return setJsonError(`Request body: ${(e as Error).message}`);
    }
    setJsonError(null);
    const full = { model: selected, ...body };
    setBusy(true);
    try {
      let endpoint = mode === "job" ? "/jobs" : "/predict";
      let reply = await call(key, "POST", endpoint, full);
      if (mode === "auto" && reply.status === 400 && errorType(reply) === "WrongMode") {
        record(selected, endpoint, full, reply);
        endpoint = "/jobs";
        reply = await call(key, "POST", endpoint, full);
      }
      record(selected, endpoint, full, reply);
      if (endpoint === "/jobs" && reply.status === 202) await poll((reply.body as { job_id: string }).job_id, selected);
    } finally {
      setBusy(false);
    }
  }

  async function poll(jobId: string, model: string) {
    polling.current = true;
    for (let i = 0; i < 120 && polling.current; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      if (!polling.current) break;
      const reply = await call(key, "GET", `/jobs/${jobId}`);
      record(model, `/jobs/${jobId}`, null, reply);
      const status = typeof reply.body === "object" && reply.body ? (reply.body as Record<string, unknown>).status : undefined;
      const finished = !reply.ok || (typeof status === "string" && !["pending", "queued", "running", "processing", "in_progress"].includes(status));
      if (finished) break;
    }
    polling.current = false;
  }

  const stateClass = (s: string) => (s === "ready" ? "ok" : s === "sleeping" ? "idle" : s === "failed" ? "bad" : ["deploying", "starting"].includes(s) ? "live" : "warn");

  return (
    <div className="app">
      <header className="bar">
        <span className="brand">
          <span className="mark" aria-hidden="true" />
          <span>
            MLAPI <small>Router</small>
          </span>
        </span>
        <span className="bar-end">
          <span className={`pill ${health === "ok" ? "ok" : health === "…" ? "idle" : "bad"}`}>router {health}</span>
          <button className="ghost" aria-pressed={theme === "dark"} onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}>
            {theme === "dark" ? "Light theme" : "Dark theme"}
          </button>
        </span>
      </header>

      <div className="hero">
        <h1>Router tester</h1>
        <p className="lede">Call a model through the Router with an API key. Sync models answer on /predict; queue models are submitted to /jobs and polled.</p>
      </div>

      <section className="panel">
        <label htmlFor="key">API key</label>
        <div className="row">
          <input
            id="key"
            type="password"
            autoComplete="off"
            placeholder="mlapi_<id>_<secret>"
            value={key}
            onChange={(e) => {
              setKey(e.target.value);
              put("apikey", e.target.value);
            }}
          />
          <button onClick={() => void load()}>Refresh</button>
        </div>
        {listError && <p className="error" role="alert">{listError}</p>}
      </section>

      <div className="grid">
        <section className="panel">
          <h2>Models</h2>
          {!key ? (
            <p className="muted">Enter an API key to list the models it may use.</p>
          ) : models === null ? (
            <p className="muted">{listError ? "" : "Loading…"}</p>
          ) : models.length === 0 ? (
            <p className="muted">This key has no accessible models.</p>
          ) : (
            <ul className="models">
              {models.map((m) => (
                <li key={m.name}>
                  <button className={selected === m.name ? "sel" : ""} onClick={() => void select(m.name)}>
                    <strong>{m.name}</strong>
                    <span className={`pill ${stateClass(m.state)}`}>{m.state.replace(/_/g, " ")}</span>
                    {m.version && <small>v{m.version}</small>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel">
          <h2>{selected ? `Call ${selected}` : "Call a model"}</h2>
          {!selected ? (
            <div className="empty"><span className="mark" aria-hidden="true" /><p className="muted">Pick a model.</p></div>
          ) : (
            <>
              <label htmlFor="body">Request body (without “model”)</label>
              <textarea id="body" rows={9} spellCheck={false} value={payload} onChange={(e) => setPayload(e.target.value)} />
              {jsonError && <p className="error" role="alert">{jsonError}</p>}
              <div className="row">
                <select aria-label="Endpoint" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
                  <option value="auto">Auto (try /predict, fall back to /jobs)</option>
                  <option value="predict">POST /predict</option>
                  <option value="job">POST /jobs</option>
                </select>
                <button className="primary" disabled={busy} onClick={() => void send()}>
                  {busy ? "Sending…" : "Send"}
                </button>
                {busy && <button onClick={() => (polling.current = false)}>Stop polling</button>}
                {schema?.example !== undefined && <button onClick={() => setPayload(pretty(schema.example))}>Reset to example</button>}
              </div>
              {schema && (
                <details>
                  <summary>Schema {schema.model_version && `(v${schema.model_version})`}</summary>
                  <Json value={{ request: schema.request_schema, response: schema.response_schema }} />
                </details>
              )}
            </>
          )}
        </section>
      </div>

      <section className="panel" aria-live="polite">
        <h2>Response</h2>
        {!view ? (
          <div className="empty"><span className="mark" aria-hidden="true" /><p className="muted">Nothing sent yet.</p></div>
        ) : (
          <>
            <div className="status">
              <span className={`code ${view.reply.ok ? "" : "err"}`}>{view.reply.status || "ERR"}</span>
              <span className="meta">
                <code>{view.endpoint}</code> · {view.reply.ms} ms{!view.reply.ok && " · failed"}
              </span>
            </div>
            {Object.keys(view.reply.headers).length > 0 && (
              <div className="hdrs">
                {Object.entries(view.reply.headers).map(([k, v]) => (
                  <span key={k} className="hdr">{k}: {v}</span>
                ))}
              </div>
            )}
            <Json value={view.reply.body} />
          </>
        )}
        {history.length > 0 && (
          <>
            <h3>History</h3>
            <ul className="history">
              {history.map((e) => (
                <li key={e.id}>
                  <button className={view?.id === e.id ? "sel" : ""} onClick={() => setView(e)}>
                    {e.at} · {e.model} · {e.endpoint.length > 30 ? e.endpoint.slice(0, 30) + "…" : e.endpoint} ·{" "}
                    <span className={e.reply.ok ? "okt" : "errt"}>{e.reply.status || "ERR"}</span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
