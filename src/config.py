from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str
    upstream_timeout: float = 60.0
    upstream_connect_timeout: float = 3.0
    max_body_bytes: int = 10 * 1024 * 1024
    max_response_bytes: int = 50 * 1024 * 1024
    job_owner_ttl: int = 86400
    upstream_max_connections: int = 200
    upstream_max_connections_per_host: int = 20
    activity_throttle_seconds: int = 10
    wake_pending_ttl_seconds: int = 60
    retry_after_seconds: int = 5
    log_level: str = "INFO"
