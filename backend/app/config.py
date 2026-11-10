from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor .env to the backend directory so keys load no matter which
# directory the app/tests are launched from. config.py lives at
# backend/app/config.py, so parents[1] is the backend root.
BACKEND_DIR=Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_prefix="CODEREVIVE_",env_file=BACKEND_DIR/".env",extra="ignore")

    sandbox_docker_image:str="maven:3.9-eclipse-temurin-17"
    sandbox_timeout_seconds:int=180
    sandbox_memory_limit:str="2g"
    sandbox_cpu_count:float=2.0
    sandbox_network_disabled:bool=False
    sandbox_maven_cache_volume:str="coderevive-m2-cache"
    sandbox_container_workdir:str="/app"
    sandbox_raw_log_char_limit:int=12000
    sandbox_default_maven_args:List[str]=Field(default_factory=lambda:["-B","-fae","clean","test"])

    # Swap providers here
    llm_provider:str="gemini"

    gemini_api_key:str=""
    gemini_model:str="gemini-3.1-flash-lite"

    nvidia_api_key:str=""
    nvidia_base_url:str="https://integrate.api.nvidia.com/v1"
    nvidia_model:str="mistralai/mistral-large-4-1t-a52b"

settings=Settings()