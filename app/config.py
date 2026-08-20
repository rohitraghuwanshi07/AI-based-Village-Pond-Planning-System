"""
Central config loader. Reads from a local .env file (never committed to git).
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    opentopography_api_key: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
