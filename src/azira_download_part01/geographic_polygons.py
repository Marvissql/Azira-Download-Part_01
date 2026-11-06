import json
from pathlib import Path, PureWindowsPath
import re
import tempfile

import geopandas as gpd
import shapely

from . import common
from .s3_bucket_synchronization import save_state


def notify_step(config, step, details):
    message = f"Geographic polygons | {step} | {details}"
    print(f"WORKFLOW_PHASE: {message}", flush=True)
    common.send_mail(config, f"Azira geographic polygons: {step}", message)


def import_geojson(geojson_file):
    return gpd.read_file(geojson_file, rows=10000000)


def write_geopandas_to_sql_server(geodataframe, config):
    notify_step(config, "Convert geometry to 2D WKT", f"rows: {len(geodataframe):,}")
    geometry = geodataframe.geometry
    geodataframe["wkt"] = None
    for start in range(0, len(geometry), 1000):
        end = min(start + 1000, len(geometry))
        geometry_2d = shapely.force_2d(geometry.iloc[start:end].to_numpy())
        geodataframe.iloc[start:end, geodataframe.columns.get_loc("wkt")] = shapely.to_wkt(geometry_2d)
    output = geodataframe[["PolygonID", "Size", "FileDate", "vintage_id", "wkt", "isDummy"]]
    notify_step(config, "Load SQL staging", f"table: {config.database}.dbo.signals_geog_import | rows: {len(output):,}")
    engine = common.sql_engine(config)
    try:
        output.to_sql("signals_geog_import", engine, if_exists="append", index=False, chunksize=250000, method=None)
    finally:
        engine.dispose()


def full_process_new_geo_json_file(geojson_file, config):
    if geojson_file is None:
        return
    match = re.search(r"_(\d{8})_(\d{3})\.json$", Path(geojson_file).name)
    if not match:
        raise ValueError(f"Unrecognized GeoJSON filename: {Path(geojson_file).name}")
    vintage_date, vintage = match.groups()
    notify_step(config, "Read GeoJSON", f"file: {geojson_file} | vintage: {vintage}")
    frame = import_geojson(geojson_file)
    notify_step(config, "Clear SQL staging", f"table: {config.database}.dbo.signals_geog_import | file: {geojson_file}")
    connection = common.sql_connection(config)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("DELETE FROM signals_geog_import;")
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()
    frame["vintage_id"] = vintage
    frame["FileDate"] = f"{vintage_date[:4]}-{vintage_date[4:6]}-{vintage_date[6:]}"
    frame["isDummy"] = 0
    frame = frame.rename(columns={"id": "PolygonID"})
    write_geopandas_to_sql_server(frame, config)
    notify_step(config, "Execute geofence procedure", f"procedure: {config.database}.dbo.usp_Process_geofences | file: {geojson_file}")
    common.execute_call(config, "dbo.usp_Process_geofences", autocommit=True)


def discover_geojson(config):
    client = common.aws_session(config).client("s3")
    records = []
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=config.partner_bucket, Prefix=config.geojson_prefix)
    for page in pages:
        eligible = common.retrieve_s3_inventory(page, "*.json")
        keys = set(eligible["key"])
        records.extend(item for item in page.get("Contents", []) if item["Key"] in keys)
    return client, records


def run(config):
    root = Path(config.signals_root).resolve()
    state_path = root / ".azira_geojson_state.json"
    notify_step(config, "Read local tracking", f"state: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("GeoJSON local metadata must be a JSON object")
    notify_step(config, "Discover S3 files", f"source: s3://{config.partner_bucket}/{config.geojson_prefix}")
    client, records = discover_geojson(config)
    notify_step(config, "Compare local files", f"S3 candidates: {len(records):,} | destination: {root}")
    destinations = []
    seen = set()
    for item in records:
        key = item["Key"]
        relative = PureWindowsPath(key)
        target = root.joinpath(*relative.parts).resolve()
        if not key.startswith(config.geojson_prefix) or relative.drive or relative.root or ".." in relative.parts or "\\" in key or any(":" in part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in relative.parts) or not target.is_relative_to(root):
            raise ValueError(f"Unsafe GeoJSON S3 key: {key}")
        identity = str(target).casefold()
        if identity in seen:
            raise ValueError(f"GeoJSON local path collision: {key}")
        seen.add(identity)
        destinations.append((item, target))
    pending = []
    for item, target in destinations:
        key = item["Key"]
        metadata = {
            "bucket": config.partner_bucket,
            "size": item["Size"],
            "etag": item["ETag"],
            "last_modified": item["LastModified"].isoformat(),
        }
        previous = state.get(key, {})
        unchanged = False
        if target.is_file() and isinstance(previous, dict):
            stat = target.stat()
            unchanged = all(previous.get(name) == value for name, value in metadata.items()) and stat.st_size == item["Size"] and previous.get("local_mtime_ns") == stat.st_mtime_ns
        if not unchanged:
            notify_step(config, "Download GeoJSON", f"source: s3://{config.partner_bucket}/{key} | destination: {target} | bytes: {item['Size']:,}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="azira-geojson-", dir=target.parent) as folder:
                temporary = Path(folder) / "download.part"
                client.download_file(config.partner_bucket, key, str(temporary))
                notify_step(config, "Verify GeoJSON download", f"file: {key} | expected bytes: {item['Size']:,}")
                remote = client.head_object(Bucket=config.partner_bucket, Key=key)
                if temporary.stat().st_size != item["Size"] or remote["ContentLength"] != item["Size"] or remote["ETag"] != item["ETag"] or remote["LastModified"] != item["LastModified"]:
                    raise ValueError(f"GeoJSON changed during download or size differs: {key}")
                if target.exists():
                    common.rename_file_with_file_date(target)
                temporary.replace(target)
            metadata["local_mtime_ns"] = target.stat().st_mtime_ns
            metadata["processed"] = False
            state[key] = metadata
            save_state(state_path, state)
        if state[key].get("processed") is not True:
            pending.append((key, target))
    if not pending:
        print("no new JSON files today")
        common.send_mail(config, "Azira geographic polygons: no files pending", "No new, changed or pending GeoJSON files to process.")
        return
    key, target = pending[0]
    full_process_new_geo_json_file(target, config)
    notify_step(config, "Record processing completion", f"file: {key} | state: {state_path} | remaining pending files: {len(pending) - 1}")
    state[key]["processed"] = True
    save_state(state_path, state)
    common.send_mail(config, "New Azira geoJSON data", key)