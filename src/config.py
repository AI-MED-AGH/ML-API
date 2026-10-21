from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str
    upstream_timeout: float = 60.0
    upstream_connect_timeout: float = 3.0
    max_body_bytes: int = 10 * 1024 * 1024
    max_response_bytes: int = 50 * 1024 * 1024
    # Model replies held in memory at once, per Router process (0 = unlimited). Peak use is about twice this (the reply
    # is joined into one buffer), so keep it well under the pod's memory limit.
    max_inflight_response_bytes: int = 128 * 1024 * 1024
    # A client that has not taken a big reply within this many seconds is disconnected, freeing the memory it holds (0 = never).
    response_send_timeout: float = 120
    job_owner_ttl: int = 86400
    upstream_max_connections: int = 200
    upstream_max_connections_per_host: int = 20
    activity_throttle_seconds: int = 10
    wake_pending_ttl_seconds: int = 60
    retry_after_seconds: int = 5
    # Per API key, 0 = unlimited. Predict/submit calls and job polls are counted apart so polling cannot starve submits.
    rate_limit_per_minute: int = 120
    job_poll_limit_per_minute: int = 600
    job_submit_limit_per_day: int = 1000
    max_outstanding_jobs: int = 100
    # Per model, used when the model does not publish its own (0 = unlimited). Concurrency is per Router process.
    model_rpm_per_key: int = 0
    model_rpm_total: int = 0
    model_max_concurrency: int = 16
    usage_ttl_days: int = 35  # how long per-key usage counters are kept (0 = do not count)
    enable_docs: bool = False  # /docs, /redoc and /openapi.json describe the whole API: off unless asked for
    health_cache_seconds: float = 2.0  # /health reuses its last Redis check this long (0 = check every time)
    # Model 5xx bodies and failed-job error text can hold file paths and stack details. Off by default.
    expose_upstream_errors: bool = False
    log_level: str = "INFO"
