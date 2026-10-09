# Changelog

## 0.2.0

- Routes are read from Redis (`route:<model>`), replacing the local `model_routes.json` file.
- API-key authentication with per-model scopes (`X-API-Key`); out-of-scope models return 404.
- **Breaking:** `POST /predict` now takes `{model, data, metadata}` and forwards everything but `model`,
  replacing the `model_data` wrapper.
- New endpoints: `GET /models`, `GET /models/{name}/schema`, `GET /health`.
- Uniform error responses, upstream timeouts, request and response size limits, wake requests for sleeping models.
- Removed the old `UvicornServer` wrapper (it returned raw exception text on 500).
