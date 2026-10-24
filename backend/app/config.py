"""Runtime configuration from environment variables, optionally loaded from backend/.env."""

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader; existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    database_path: str
    fleet_file: str
    sim_time_scale: float
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_reasoning_effort: str
    llm_timeout_s: float

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


def load_settings() -> Settings:
    _load_env_file(BACKEND_DIR / ".env")
    env = os.environ
    database_path = env.get("DATABASE_PATH", "data/orchestrator.db")
    if database_path != ":memory:":
        # Relative paths are relative to backend/, not the shell's working directory.
        database_path = str(BACKEND_DIR / database_path)
    return Settings(
        database_path=database_path,
        # Relative to backend/ like the database path.
        fleet_file=str(BACKEND_DIR / env.get("FLEET_FILE", "config/fleet.json")),
        sim_time_scale=float(env.get("SIM_TIME_SCALE", "1.0")),
        llm_base_url=env.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=env.get("LLM_API_KEY", ""),
        # No default model: the identifier must be confirmed with the provider.
        llm_model=env.get("LLM_MODEL", ""),
        llm_reasoning_effort=env.get("LLM_REASONING_EFFORT", ""),
        llm_timeout_s=float(env.get("LLM_TIMEOUT_S", "120")),
    )
