import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Problem(ValueError):
    """A domain error safe to display in the local panel."""


def web_url(value: str) -> str:
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise Problem("Geçerli bir HTTPS bağlantısı girin.")
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.port not in (None, 443)):
            raise ValueError
    except ValueError:
        raise Problem("Geçerli bir HTTPS bağlantısı girin.") from None
    return value


def allowed_url(value: str, hosts: list[str]) -> str:
    value = web_url(value)
    host = urlsplit(value).hostname
    if host not in hosts or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise Problem("Hedef, kaynak izin kaydında yer almıyor.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return value
    raise Problem("Kaynaklar için alan adı kullanın.")


class Source(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    platform: Literal["greenhouse", "lever", "wwr", "ashby", "jsonld", "fixture", "manual"]
    company: str = ""
    board: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]*$")
    region: Literal["global", "eu"] = "global"
    url: str = ""
    hosts: list[str] = []
    permission_url: str = ""
    checked_on: str = ""
    interval_seconds: int = Field(default=21600, ge=60)
    delay_seconds: float = Field(default=1, ge=0.25)
    read: bool = True
    prefill: bool = False
    submit: bool = False
    secret_env: str = Field(default="", pattern=r"^(?:ISAKISI_[A-Z0-9_]+)?$")
    prefill_fields: dict[Literal["name", "first_name", "last_name", "email", "phone", "resume"], str] = {}

    @model_validator(mode="after")
    def validate_permission(self):
        if self.platform in {"fixture", "manual"}:
            if self.submit or self.prefill:
                raise ValueError("Yerel kaynaklara canlı işlem yetkisi verilemez.")
            return self
        from datetime import date

        web_url(self.permission_url)
        date.fromisoformat(self.checked_on)
        if not self.hosts:
            raise ValueError("Kaynak için izinli alan adı gerekli.")
        if self.platform in {"greenhouse", "lever", "ashby"}:
            if not self.board or not self.company.strip():
                raise ValueError("Pano ve işveren adı gerekli.")
        else:
            allowed_url(self.url, self.hosts)
        if self.platform == "wwr" and urlsplit(self.url).hostname != "weworkremotely.com":
            raise ValueError("WWR için resmî RSS adresini kullanın.")
        if self.prefill and not self.prefill_fields:
            raise ValueError("Form alanlarının etiketleri gerekli.")
        if self.submit and (self.platform != "greenhouse" or not self.secret_env):
            raise ValueError("Gönderim için desteklenen ATS ve anahtar ayarı gerekli.")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_data_dir: Path = Path.home() / ".isakisi"
    source_config: Path | None = None
    discovery_mode: Literal["fixture", "live"] = "fixture"
    submit_mode: Literal["simulate", "manual", "authorized"] = "simulate"
    dashboard_bind: Literal["127.0.0.1"] = "127.0.0.1"
    dashboard_port: int = Field(default=8765, ge=1024, le=65535)
    app_timezone: str = "Europe/Istanbul"
    llm_backend: Literal["off", "ollama"] = "off"
    ollama_model: str = "qwen3:4b"
    ollama_digest: str = Field(default="", pattern=r"^(?:sha256:)?[0-9a-f]{64}$|^$")
    pdf_font: Path | None = None
    raw_retention_days: int = Field(default=7, ge=1)
    log_retention_days: int = Field(default=30, ge=1)
    closed_retention_days: int = Field(default=90, ge=1)

    @field_validator("app_data_dir")
    @classmethod
    def absolute(cls, value):
        if not value.expanduser().is_absolute():
            raise ValueError("APP_DATA_DIR mutlak yol olmalı.")
        return value.expanduser()

    @field_validator("app_timezone")
    @classmethod
    def timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("ollama_model")
    @classmethod
    def model_name(cls, value):
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", value) or value.endswith((":cloud", "-cloud")):
            raise ValueError("Geçersiz Ollama model adı.")
        return value

    def sources(self) -> list[Source]:
        if self.discovery_mode == "fixture":
            return [Source(id="fixture", platform="fixture", company="Örnek panolar")]
        if not self.source_config:
            raise Problem("Canlı keşif için SOURCE_CONFIG dosyasını seçin.")
        try:
            data = yaml.safe_load(self.source_config.read_text(encoding="utf-8"))
            sources = [Source.model_validate(x) for x in data["sources"]]
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
            raise Problem("Kaynak dosyasındaki izin ve pano alanlarını denetleyin.") from None
        if (not sources or any(s.platform in {"fixture", "manual"} or s.id in {"fixture", "manual"} for s in sources)
                or len({s.id for s in sources}) != len(sources)):
            raise Problem("Canlı kaynak kimlikleri benzersiz olmalı.")
        return sources
