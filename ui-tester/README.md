# Router tester

A small dev/showcase UI for the Router. Not production software.

```
npm install
ROUTER_URL=http://127.0.0.1:8000 npm run dev      # http://localhost:5173
```

Enter an API key (create one in the Supervisor admin UI). The dev server proxies `/api/*` to the Router, so no CORS setup is needed.
The key is kept in `sessionStorage` only. It lists the models the key can use, loads each model's schema and example, sends
`/predict` (sync models) or `/jobs` (queue models, polling `/jobs/{id}`), and shows status, `X-Model-Status`, `Retry-After`, latency and the body.
