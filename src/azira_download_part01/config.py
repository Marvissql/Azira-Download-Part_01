from dataclasses import dataclass, fields
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    sql_server: str = r"LI-DEV-VM-SQL02\SQL2019"
    inventory_server: str = r"LI-DEV-VM-SQL02.kalibratetech.com\SQL2019"
    core_sql_server: str = r"LI-DEV-VM-SQL02.kalibratetech.com\SQL2019"
    database: str = "near_signals"
    core_procedure_database: str = "HD_Panel_Store_Level_Summary_v2"
    driver: str = "SQL Server"
    bulk_driver: str = "ODBC Driver 17 for SQL Server"
    signals_root: str = "I:/Near_download/signals/"
    masterfile_root: str = "I:/Near_download/HD_Panel_Data_Source/AllWeeks_HDv2/"
    excel_dir: str = "I:/Near_download/core_metrics_municipality/HD_V2"
    signals_bucket: str = "signals1.5"
    partner_bucket: str = "partner-kalibrate"
    signals_profile: str = ""
    partner_profile: str = ""
    signals_region: str = "us-east-1"
    geojson_prefix: str = "signals_weekly_jsons/"
    ppoi_prefix: str = "ppoi_change_log/"
    masterfile_prefix: str = "hdv2/"
    masterfile_strip_chars: int = 13
    masterfile_inventory_strip_chars: int = 13
    masterfile_mapping_verified: bool = False
    sync_bucket: str = "signals1.5"
    sync_prefix: str = "hdv2/"
    sync_root: str = "I:/Near_download/HD_Panel_Data_Source/HDv2_Download"
    sync_credentials_file: str = ""
    recipients: str = "hartwell.hooper@kalibrate.com;marvis.ediagbonya@kalibrate.com"


def load_config(path=None):
    if path is None:
        return Config()
    with Path(path).open("rb") as handle:
        values = tomllib.load(handle)
    allowed = {field.name: field.type for field in fields(Config)}
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")
    for name, value in values.items():
        if type(value) is not allowed[name]:
            raise ValueError(f"Invalid type for configuration field {name}")
    return Config(**values)