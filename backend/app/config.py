from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_prefix="CODEREVIVE_",env_file=".env",extra="ignore")

    sandbox_docker_image:str="maven:3.9-eclipse-temurin-17"
    sandbox_timeout_seconds:int=180
    sandbox_memory_limit:str="2g"
    sandbox_cpu_count:float=2.0
    sandbox_network_disabled:bool=False
    sandbox_maven_cache_volume:str="coderevive-m2-cache"
    sandbox_container_workdir:str="/app"
    sandbox_raw_log_char_limit:int=12000
    sandbox_default_maven_args:List[str]=Field(default_factory=lambda:["-B","-fae","clean","test"])

settings=Settings()