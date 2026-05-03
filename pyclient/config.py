"""Load driver settings from ``wsjt-driver.toml``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "Need Python 3.11+ or install the TOML parser:  pip install tomli\n"
            "Then re-run the driver."
        ) from exc


@dataclass
class DriverConfig:
    host: str = "127.0.0.1"
    port: int = 7374
    password: str | None = None
    callsign: str = ""
    quiet_qso_bursts: int = 4
    wait_reply_bursts: int = 8
    idle_answer_passes_call: int = 6  # ANSWERING_CQ: idle passes with no workable CQ → calling CQ mode.
    max_answer_cq_ignore_passes: int = 4
    # ANSWERING_CQ: give up locked CQ station after N consecutive failing ``answer`` (OK never seen).
    max_chase_answer_retries: int = 8
    quiet_wire_transcript: bool = False
    verbose: bool = False  # True → logging.DEBUG; otherwise INFO for --config driver runs.
    comment_holds: bool = True


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def load_driver_config(path: Path) -> DriverConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = DriverConfig(
        host=str(data.get("host", DriverConfig.host)),
        port=int(data.get("port", DriverConfig.port)),
        password=_opt_str(data.get("password")),
        callsign=str(data.get("callsign", "")).strip(),
        quiet_qso_bursts=int(data.get("quiet_qso_bursts", DriverConfig.quiet_qso_bursts)),
        wait_reply_bursts=int(data.get("wait_reply_bursts", DriverConfig.wait_reply_bursts)),
        idle_answer_passes_call=int(
            data.get("idle_answer_passes_call", DriverConfig.idle_answer_passes_call),
        ),
        max_answer_cq_ignore_passes=int(
            data.get(
                "max_answer_cq_ignore_passes",
                DriverConfig.max_answer_cq_ignore_passes,
            ),
        ),
        max_chase_answer_retries=int(
            data.get(
                "max_chase_answer_retries",
                DriverConfig.max_chase_answer_retries,
            ),
        ),
        quiet_wire_transcript=_as_bool(data.get("quiet_wire_transcript", False)),
        verbose=_as_bool(data.get("verbose", False)),
        comment_holds=_as_bool(data.get("comment_holds", DriverConfig.comment_holds)),
    )
    if not cfg.callsign:
        raise ValueError("config: 'callsign' is required")
    return cfg


def resolve_default_config_path(cli_path: str | None) -> Path:
    if cli_path:
        p = Path(cli_path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--config not found: {p}")
        return p.resolve()
    for rel in (
        Path("wsjt-driver.toml"),
        Path("pyclient/wsjt-driver.toml"),
        Path(__file__).resolve().parent / "wsjt-driver.toml",
    ):
        if rel.is_file():
            return rel.resolve()
    raise FileNotFoundError(
        "No wsjt-driver.toml found (try cwd, pyclient/wsjt-driver.toml, or next to "
        "pyclient/config.py). Pass --config PATH or copy wsjt-driver.example.toml.",
    )
