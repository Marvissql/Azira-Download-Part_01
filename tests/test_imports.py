import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from azira_download_part01 import cli
from azira_download_part01.config import Config, load_config


class CliTests(unittest.TestCase):
    def test_all_order(self):
        with patch.object(cli, "run_capability") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["all"]), 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], list(cli.CAPABILITIES))

    def test_failure_stops_batch(self):
        with patch.object(cli, "run_capability", side_effect=RuntimeError("failed")) as run:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["all"]), 1)
        run.assert_called_once()

    def test_each_command(self):
        for name in cli.CAPABILITIES:
            with self.subTest(name=name), patch.object(cli, "run_capability") as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main([name]), 0)
                run.assert_called_once_with(name, Config())

    def test_help_does_not_import_clients(self):
        for args in (["--help"], *[[name, "--help"] for name in (*cli.CAPABILITIES, "all", "download", "azira-s3-bucket-synchronization", "verify-masterfile-mapping")]):
            with patch.object(cli.importlib, "import_module") as imported:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
                    cli.main(args)
                self.assertEqual(result.exception.code, 0)
                imported.assert_not_called()

    def test_help_in_clean_process(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        code = (
            f"import sys; sys.path.insert(0, {source!r}); "
            "from azira_download_part01.cli import main; "
            "\ntry: main(['--help'])"
            "\nexcept SystemExit as result: assert result.code == 0"
            "\nassert not {'boto3', 'pyodbc', 'sqlalchemy', 'geopandas'} & set(sys.modules)"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("polygon-masterfile", result.stdout)


class S3SynchronizationTests(unittest.TestCase):
    def test_recent_all_files_week_layout_and_metadata_changes(self):
        from dataclasses import replace
        from datetime import datetime, timedelta, timezone
        from azira_download_part01 import s3_bucket_synchronization as sync
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        recent = {"Key": "hdv2/2026-10/2026-10-11/nested/data.csv", "Size": 4, "ETag": "first", "LastModified": now - timedelta(weeks=6)}
        old = dict(recent, Key="hdv2/2026-09/2026-09-20/old.zip", LastModified=now - timedelta(weeks=6, seconds=1))
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [{"Contents": [old]}, {"Contents": [recent]}]
        client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"data")
        client.head_object.side_effect = lambda **kwargs: {"ContentLength": recent["Size"], "ETag": recent["ETag"], "LastModified": recent["LastModified"]}
        with tempfile.TemporaryDirectory() as folder:
            config = replace(Config(), sync_root=folder)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sync.synchronize(config, client=client, now=now), (1, 0))
                self.assertEqual(sync.synchronize(config, client=client, now=now), (0, 1))
                recent["ETag"] = "second"
                self.assertEqual(sync.synchronize(config, client=client, now=now), (1, 0))
            self.assertEqual((Path(folder) / "2026-10-11/nested/data.csv").read_bytes(), b"data")
            self.assertFalse((Path(folder) / "2026-10").exists())
            self.assertFalse((Path(folder) / "2026-09-20").exists())
        self.assertEqual(client.download_file.call_count, 2)

    def test_invalid_layout_and_collision_rejected_before_download(self):
        from dataclasses import replace
        from datetime import datetime, timezone
        from azira_download_part01 import s3_bucket_synchronization as sync
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for key in ("hdv2/2026-09/2026-10-11/data.zip", "hdv2/2026-10/2026-10-11/../data.zip", "hdv2/2026-10/2026-10-11/C:data.zip"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    sync.destination(root, "hdv2/", key)
            item = {"Key": "hdv2/2026-10/2026-10-11/data.zip", "Size": 4, "ETag": "first", "LastModified": now}
            client = MagicMock()
            client.get_paginator.return_value.paginate.return_value = [{"Contents": [item, dict(item, Key=item["Key"].replace("data", "DATA"))]}]
            with self.assertRaisesRegex(ValueError, "same local file"):
                sync.synchronize(replace(Config(), sync_root=folder), client=client, now=now)
            client.download_file.assert_not_called()

    def test_failed_transfer_preserves_existing_file_and_no_state(self):
        from dataclasses import replace
        from datetime import datetime, timezone
        from azira_download_part01 import s3_bucket_synchronization as sync
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        item = {"Key": "hdv2/2026-10/2026-10-11/data.zip", "Size": 4, "ETag": "first", "LastModified": now}
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [{"Contents": [item]}]
        client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"bad")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "2026-10-11/data.zip"
            target.parent.mkdir()
            target.write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "size differs"):
                sync.synchronize(replace(Config(), sync_root=folder), client=client, now=now)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse((Path(folder) / ".azira_s3_sync.json").exists())

    def test_credential_assignments_are_loaded_without_execution(self):
        from dataclasses import replace
        from azira_download_part01 import s3_bucket_synchronization as sync
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "AWS_Credentials.py"
            path.write_text("signals_aws_access_key_id = 'synthetic-id'\nsignals_aws_secret_access_key = 'synthetic-secret'\nraise RuntimeError('must not execute')\n", encoding="utf-8")
            with patch.object(sync.boto3, "Session") as session:
                sync.s3_client(replace(Config(), sync_credentials_file=str(path)))
            session.assert_called_once_with(aws_access_key_id="synthetic-id", aws_secret_access_key="synthetic-secret", region_name="us-east-1")
            with self.assertRaises(FileNotFoundError):
                sync.s3_client(replace(Config(), sync_credentials_file=str(Path(folder) / "missing.py")))

    def test_cli_is_separate_from_import_and_download_all(self):
        from azira_download_part01 import s3_bucket_synchronization as sync
        with patch.object(sync, "synchronize") as synchronize, patch.object(cli, "run_capability") as process:
            self.assertEqual(cli.main(["azira-s3-bucket-synchronization"]), 0)
            synchronize.assert_called_once_with(Config())
            process.assert_not_called()
        with patch.object(sync, "synchronize") as synchronize, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            sync.main(["--help"])
        synchronize.assert_not_called()


class PartnerTests(unittest.TestCase):
    def test_download_only_paginates_without_sql_or_mapping_gate(self):
        from dataclasses import replace
        from azira_download_part01 import common
        key = "hdv2/2026-10/2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz"
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "hdv2/unrelated.tsv.gz", "Size": 4}]},
            {"Contents": [{"Key": key, "Size": 4}]},
        ]
        def download(bucket, remote_key, filename):
            Path(filename).write_bytes(b"data")
        client.download_file.side_effect = download
        with tempfile.TemporaryDirectory() as folder:
            config = replace(Config(), masterfile_root=folder)
            with patch.object(common, "aws_session") as session, patch.object(common, "sql_connection") as sql:
                session.return_value.client.return_value = client
                with contextlib.redirect_stdout(io.StringIO()):
                    common.download_s3_files(config, "polygon-masterfile")
                    common.download_s3_files(config, "polygon-masterfile")
                sql.assert_not_called()
            self.assertEqual((Path(folder) / key).read_bytes(), b"data")
        client.download_file.assert_called_once()
        client.get_paginator.assert_called_with("list_objects_v2")

    def test_download_only_partner_and_failed_download_preserves_local_file(self):
        from dataclasses import replace
        from azira_download_part01 import common
        client = MagicMock()
        cases = (("geographic-polygons", "signals_weekly_jsons/example.json"), ("ppoi-change-logs", "ppoi_change_log/example.zip"))
        with tempfile.TemporaryDirectory() as folder:
            config = replace(Config(), signals_root=folder)
            with patch.object(common, "aws_session") as session:
                session.return_value.client.return_value = client
                for source, key in cases:
                    target = Path(folder) / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"old")
                    client.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": key, "Size": 4}]}]
                    client.download_file.side_effect = lambda bucket, remote_key, filename: Path(filename).write_bytes(b"bad")
                    with self.assertRaisesRegex(ValueError, "size differs"):
                        common.download_s3_files(config, source)
                    self.assertEqual(target.read_bytes(), b"old")
                    self.assertFalse(list(target.parent.glob("azira-download-*")))
                    client.download_file.side_effect = lambda bucket, remote_key, filename: Path(filename).write_bytes(b"data")
                    with contextlib.redirect_stdout(io.StringIO()):
                        common.download_s3_files(config, source)
                    self.assertEqual(target.read_bytes(), b"data")

    def test_download_only_rejects_unsafe_key(self):
        from azira_download_part01 import common
        with tempfile.TemporaryDirectory() as folder:
            from dataclasses import replace
            client = MagicMock()
            client.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "signals_weekly_jsons/../../escape.json", "Size": 4}]}]
            with patch.object(common, "aws_session") as session:
                session.return_value.client.return_value = client
                with self.assertRaisesRegex(ValueError, "Unsafe S3"):
                    common.download_s3_files(replace(Config(), signals_root=folder), "geographic-polygons")
            client.download_file.assert_not_called()

    def test_download_cli_routes_only_to_downloads(self):
        from azira_download_part01 import common
        with patch.object(common, "download_s3_files") as download, patch.object(cli, "run_capability") as process:
            self.assertEqual(cli.main(["download", "--source", "all"]), 0)
        self.assertEqual([call.args[1] for call in download.call_args_list], ["polygon-masterfile", "geographic-polygons", "ppoi-change-logs"])
        process.assert_not_called()
        with patch.object(common, "download_s3_files", side_effect=RuntimeError("transfer failed")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["download", "--source", "polygon-masterfile"]), 1)

    def test_real_geojson_reader(self):
        from azira_download_part01 import geographic_polygons as geo
        data = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"id": "P1", "Size": 4},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        }]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "signals_20260917_251.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            frame = geo.import_geojson(path)
        self.assertEqual(frame.iloc[0]["id"], "P1")
        self.assertEqual(frame.iloc[0].geometry.geom_type, "Polygon")
        self.assertEqual(frame.crs.to_epsg(), 4326)

    def test_no_new_ppoi_files_do_not_process(self):
        from azira_download_part01 import common, ppoi_change_logs
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ppoi_change_logs, "discover_ppoi", return_value=(MagicMock(), [])), patch.object(ppoi_change_logs, "import_ppoi_logs") as process:
                with patch.object(common, "append_to_local_inventory_sql") as append, patch.object(common, "send_mail") as mail, contextlib.redirect_stdout(io.StringIO()):
                    ppoi_change_logs.run(Config(signals_root=folder))
            process.assert_not_called()
            append.assert_not_called()
            mail.assert_not_called()

    def test_download_creates_log_and_preserves_backup(self):
        import pandas as pd
        from azira_download_part01 import common
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "data.json"
            target.parent.mkdir()
            target.write_text("old", encoding="utf-8")
            bucket = MagicMock()
            common.download_new_bucket_files(bucket, folder, pd.DataFrame({"key": ["nested/data.json"]}), "JSONS")
            self.assertEqual(len(list((Path(folder) / "files_download_log").glob("*.csv"))), 1)
            backup = list(target.parent.glob("old_*.json_bak"))
            self.assertEqual(len(backup), 1)
            self.assertEqual(backup[0].read_text(encoding="utf-8"), "old")
            bucket.download_file.assert_called_once_with("nested/data.json", str(target))

    def test_zip_overwrite_and_unsafe_member(self):
        import zipfile
        from azira_download_part01 import common
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "changes.zip"
            output = Path(folder) / "out"
            output.mkdir()
            (output / "added.csv").write_text("old", encoding="utf-8")
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("added.csv", "new")
            common.extract_zip_to_folder(archive, output)
            self.assertEqual((output / "added.csv").read_text(encoding="utf-8"), "new")
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside.csv", "unsafe")
            with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                common.extract_zip_to_folder(archive, output)

    def test_inventory_append_retains_latest_versions(self):
        import pandas as pd
        from azira_download_part01 import common
        frame = pd.DataFrame({
            "key": ["new", "changed"], "name_key": [None, "changed"], "latest": [None, 1],
            "last_modified": ["2026-01-01 01:00:00+00:00"] * 2, "size": [1, 2], "version": [1, 2],
        })
        engine = MagicMock()
        captured = []
        def capture(output, name, *args, **kwargs):
            captured.append(output.copy())
        with patch.object(common, "sql_engine", return_value=engine), patch.object(pd.DataFrame, "to_sql", capture):
            common.append_to_local_inventory_sql(Config(), frame)
        self.assertEqual(captured[0]["name_key"].tolist(), ["new", "changed"])
        self.assertEqual(captured[0]["latest"].tolist(), [1, 2])
        self.assertEqual(captured[0]["last_modified"].tolist(), ["2026-01-01 01:00:00"] * 2)
        connection = engine.connect.return_value.__enter__.return_value
        self.assertEqual(connection.execute.call_count, 2)
        connection.commit.assert_called_once()

    def test_ppoi_local_tracking_without_sql_inventory(self):
        from datetime import datetime, timezone
        from azira_download_part01 import common, ppoi_change_logs
        now = datetime.now(timezone.utc)
        records = [{"Key": f"ppoi_change_log/changes_{vintage}.zip", "Size": 4, "LastModified": now, "ETag": str(vintage)} for vintage in (251, 252)]
        client = MagicMock()
        client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"data")
        client.head_object.side_effect = lambda **kwargs: next({"ContentLength": item["Size"], "LastModified": item["LastModified"], "ETag": item["ETag"]} for item in records if item["Key"] == kwargs["Key"])
        with tempfile.TemporaryDirectory() as folder:
            config = Config(signals_root=folder)
            with patch.object(ppoi_change_logs, "discover_ppoi", return_value=(client, records)), patch.object(ppoi_change_logs, "import_ppoi_logs") as process, patch.object(common, "extract_zip_to_folder"):
                with patch.object(common, "read_local_inventory_from_sql", side_effect=AssertionError("inventory read")), patch.object(common, "append_to_local_inventory_sql", side_effect=AssertionError("inventory append")), patch.object(common, "update_processed", side_effect=AssertionError("inventory update")), patch.object(common, "send_mail") as mail:
                    ppoi_change_logs.run(config)
                    process.assert_called_once_with("251", config)
                    state = json.loads((Path(folder) / ".azira_ppoi_state.json").read_text())
                    self.assertTrue(state[records[0]["Key"]]["processed"])
                    self.assertFalse(state[records[1]["Key"]]["processed"])
                    ppoi_change_logs.run(config)
                    self.assertEqual(process.call_args.args[0], "252")
                    with contextlib.redirect_stdout(io.StringIO()):
                        ppoi_change_logs.run(config)
                    self.assertEqual(process.call_count, 2)
                    self.assertEqual(mail.call_count, 2)
                    self.assertEqual(client.download_file.call_count, 2)
                    records[0]["ETag"] = "changed-same-size"
                    ppoi_change_logs.run(config)
                    self.assertEqual(process.call_count, 3)
                    self.assertEqual(client.download_file.call_count, 3)

    def test_ppoi_failed_processing_retries_without_redownload(self):
        from datetime import datetime, timezone
        from azira_download_part01 import ppoi_change_logs as ppoi, common
        item = {"Key": "ppoi_change_log/changes_251.zip", "Size": 4, "LastModified": datetime.now(timezone.utc), "ETag": "first"}
        client = MagicMock()
        client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"data")
        client.head_object.return_value = {"ContentLength": 4, "LastModified": item["LastModified"], "ETag": item["ETag"]}
        with tempfile.TemporaryDirectory() as folder:
            config = Config(signals_root=folder)
            with patch.object(ppoi, "discover_ppoi", return_value=(client, [item])), patch.object(common, "extract_zip_to_folder"), patch.object(ppoi, "import_ppoi_logs", side_effect=RuntimeError("SQL failed")), patch.object(common, "send_mail") as mail:
                with self.assertRaisesRegex(RuntimeError, "SQL failed"):
                    ppoi.run(config)
                mail.assert_not_called()
            state = json.loads((Path(folder) / ".azira_ppoi_state.json").read_text())
            self.assertFalse(state[item["Key"]]["processed"])
            with patch.object(ppoi, "discover_ppoi", return_value=(client, [item])), patch.object(common, "extract_zip_to_folder"), patch.object(ppoi, "import_ppoi_logs") as process, patch.object(common, "send_mail"):
                ppoi.run(config)
            process.assert_called_once_with("251", config)
            client.download_file.assert_called_once()

    def test_ppoi_discovery_paginates_without_sql(self):
        from datetime import datetime, timedelta, timezone
        from azira_download_part01 import ppoi_change_logs as ppoi, common
        now = datetime.now(timezone.utc)
        item = {"Key": "ppoi_change_log/changes_251.zip", "Size": 4, "LastModified": now, "ETag": "first"}
        old = dict(item, Key="ppoi_change_log/old_250.zip", LastModified=now - timedelta(days=361))
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [{"Contents": [old]}, {"Contents": [item]}]
        with patch.object(common, "aws_session") as session, patch.object(common, "sql_connection", side_effect=AssertionError("SQL connection")):
            session.return_value.client.return_value = client
            self.assertEqual(ppoi.discover_ppoi(Config()), (client, [item]))
        client.get_paginator.assert_called_once_with("list_objects_v2")

    def test_ppoi_unsafe_keys_and_failed_transfer_do_not_process(self):
        from datetime import datetime, timezone
        from azira_download_part01 import ppoi_change_logs as ppoi
        item = {"Key": "ppoi_change_log/changes_251.zip", "Size": 4, "LastModified": datetime.now(timezone.utc), "ETag": "first"}
        client = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            config = Config(signals_root=folder)
            for key in ("ppoi_change_log/../escape.zip", "ppoi_change_log/C:escape.zip"):
                with patch.object(ppoi, "discover_ppoi", return_value=(client, [dict(item, Key=key)])):
                    with self.assertRaisesRegex(ValueError, "Unsafe PPOI"):
                        ppoi.run(config)
            with patch.object(ppoi, "discover_ppoi", return_value=(client, [item, dict(item, Key=item["Key"].upper().replace("PPOI_CHANGE_LOG", "ppoi_change_log"))])):
                with self.assertRaisesRegex(ValueError, "collision"):
                    ppoi.run(config)
            client.download_file.assert_not_called()
            target = Path(folder) / item["Key"]
            target.parent.mkdir()
            target.write_bytes(b"old")
            client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"data")
            client.head_object.return_value = {"ContentLength": 4, "LastModified": item["LastModified"], "ETag": "changed"}
            with patch.object(ppoi, "discover_ppoi", return_value=(client, [item])), patch.object(ppoi, "import_ppoi_logs") as process:
                with self.assertRaisesRegex(ValueError, "changed during download"):
                    ppoi.run(config)
            process.assert_not_called()
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse((Path(folder) / ".azira_ppoi_state.json").exists())

    def test_geojson_local_tracking_without_sql_inventory(self):
        from datetime import datetime, timezone
        from azira_download_part01 import geographic_polygons as geo, common
        now = datetime.now(timezone.utc)
        records = [{"Key": f"signals_weekly_jsons/signals_20260917_{vintage}.json", "Size": 4, "LastModified": now, "ETag": str(vintage)} for vintage in (251, 252)]
        client = MagicMock()
        client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"data")
        client.head_object.side_effect = lambda **kwargs: next({"ContentLength": item["Size"], "LastModified": item["LastModified"], "ETag": item["ETag"]} for item in records if item["Key"] == kwargs["Key"])
        with tempfile.TemporaryDirectory() as folder:
            config = Config(signals_root=folder)
            with patch.object(geo, "discover_geojson", return_value=(client, records)), patch.object(geo, "full_process_new_geo_json_file") as process:
                with patch.object(common, "read_local_inventory_from_sql", side_effect=AssertionError("inventory read")), patch.object(common, "append_to_local_inventory_sql", side_effect=AssertionError("inventory append")), patch.object(common, "update_processed", side_effect=AssertionError("inventory update")), patch.object(common, "send_mail") as mail:
                    geo.run(config)
                    state = json.loads((Path(folder) / ".azira_geojson_state.json").read_text())
                    self.assertTrue(state[records[0]["Key"]]["processed"])
                    self.assertFalse(state[records[1]["Key"]]["processed"])
                    geo.run(config)
                    with contextlib.redirect_stdout(io.StringIO()):
                        geo.run(config)
                    self.assertEqual(process.call_count, 2)
                    self.assertEqual(sum(call.args[1] == "New Azira geoJSON data" for call in mail.call_args_list), 2)
                    self.assertEqual(client.download_file.call_count, 2)
                    records[0]["ETag"] = "changed-same-size"
                    geo.run(config)
                    self.assertEqual(process.call_count, 3)
                    self.assertEqual(client.download_file.call_count, 3)

    def test_geojson_failed_processing_retries_without_redownload(self):
        from datetime import datetime, timezone
        from azira_download_part01 import geographic_polygons as geo, common
        item = {"Key": "signals_weekly_jsons/signals_20260917_251.json", "Size": 4, "LastModified": datetime.now(timezone.utc), "ETag": "first"}
        client = MagicMock()
        client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"data")
        client.head_object.return_value = {"ContentLength": 4, "LastModified": item["LastModified"], "ETag": item["ETag"]}
        with tempfile.TemporaryDirectory() as folder:
            config = Config(signals_root=folder)
            with patch.object(geo, "discover_geojson", return_value=(client, [item])), patch.object(geo, "full_process_new_geo_json_file", side_effect=RuntimeError("SQL failed")), patch.object(common, "send_mail") as mail:
                with self.assertRaisesRegex(RuntimeError, "SQL failed"):
                    geo.run(config)
                self.assertNotIn("New Azira geoJSON data", [call.args[1] for call in mail.call_args_list])
            state = json.loads((Path(folder) / ".azira_geojson_state.json").read_text())
            self.assertFalse(state[item["Key"]]["processed"])
            with patch.object(geo, "discover_geojson", return_value=(client, [item])), patch.object(geo, "full_process_new_geo_json_file") as process, patch.object(common, "send_mail"):
                geo.run(config)
            process.assert_called_once()
            client.download_file.assert_called_once()

    def test_geojson_discovery_paginates_without_sql(self):
        from datetime import datetime, timezone
        from azira_download_part01 import geographic_polygons as geo, common
        item = {"Key": "signals_weekly_jsons/signals_20260917_251.json", "Size": 4, "LastModified": datetime.now(timezone.utc), "ETag": "first"}
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [{}, {"Contents": [item]}]
        with patch.object(common, "aws_session") as session, patch.object(common, "sql_connection", side_effect=AssertionError("SQL connection")):
            session.return_value.client.return_value = client
            self.assertEqual(geo.discover_geojson(Config()), (client, [item]))
        client.get_paginator.assert_called_once_with("list_objects_v2")

    def test_geojson_no_candidates_or_unsafe_keys_do_not_process(self):
        from azira_download_part01 import geographic_polygons as geo, common
        with tempfile.TemporaryDirectory() as folder:
            config = Config(signals_root=folder)
            client = MagicMock()
            with patch.object(geo, "discover_geojson", return_value=(client, [])), patch.object(geo, "full_process_new_geo_json_file") as process, patch.object(common, "send_mail") as mail, contextlib.redirect_stdout(io.StringIO()):
                geo.run(config)
            process.assert_not_called()
            self.assertEqual(mail.call_args.args[1], "Azira geographic polygons: no files pending")
            for key in ("signals_weekly_jsons/../escape.json", "signals_weekly_jsons/C:escape.json"):
                with patch.object(geo, "discover_geojson", return_value=(client, [{"Key": key}])), patch.object(geo, "notify_step"):
                    with self.assertRaisesRegex(ValueError, "Unsafe GeoJSON"):
                        geo.run(config)
            client.download_file.assert_not_called()

    def test_geojson_failed_transfer_preserves_file_and_metadata(self):
        from datetime import datetime, timezone
        from azira_download_part01 import geographic_polygons as geo
        item = {"Key": "signals_weekly_jsons/signals_20260917_251.json", "Size": 4, "LastModified": datetime.now(timezone.utc), "ETag": "first"}
        client = MagicMock()
        client.download_file.side_effect = lambda bucket, key, filename: Path(filename).write_bytes(b"data")
        client.head_object.return_value = {"ContentLength": 4, "LastModified": item["LastModified"], "ETag": "changed"}
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / item["Key"]
            target.parent.mkdir()
            target.write_bytes(b"old")
            with patch.object(geo, "discover_geojson", return_value=(client, [item])), patch.object(geo, "full_process_new_geo_json_file") as process, patch.object(geo, "notify_step"):
                with self.assertRaisesRegex(ValueError, "changed during download"):
                    geo.run(Config(signals_root=folder))
            process.assert_not_called()
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse((Path(folder) / ".azira_geojson_state.json").exists())

    def test_empty_s3_and_date_filter(self):
        from datetime import datetime, timedelta, timezone
        from azira_download_part01.common import retrieve_s3_inventory
        self.assertTrue(retrieve_s3_inventory({}, "*.zip").empty)
        now = datetime.now(timezone.utc)
        response = {"Contents": [
            {"Key": "recent.zip", "Size": 1, "LastModified": now - timedelta(days=100)},
            {"Key": "old.zip", "Size": 1, "LastModified": now - timedelta(days=361)},
            {"Key": "other.json", "Size": 1, "LastModified": now - timedelta(days=1)},
        ]}
        self.assertEqual(retrieve_s3_inventory(response, "*.zip")["key"].tolist(), ["recent.zip"])

    def test_inventory_comparison(self):
        import pandas as pd
        from azira_download_part01.common import identify_files_to_download
        remote = pd.DataFrame({"key": ["same", "changed", "new"], "last_modified": ["2026-01-01 01:00:00"] * 3, "size": [1, 2, 3]})
        local = pd.DataFrame({"key": ["same", "changed"], "name_key": ["same", "changed"], "last_modified": ["2026-01-01 01:00:00"] * 2, "size": [1, 1], "latest": [1, 1]})
        selected = identify_files_to_download(remote, local)
        self.assertEqual(selected["key"].tolist(), ["new", "changed"])
        self.assertEqual(selected["version"].tolist(), [1, 2])

    def test_ppoi_all_edit_types_and_summary(self):
        import pandas as pd
        from azira_download_part01 import ppoi_change_logs, common
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ppoi_change_log"
            root.mkdir()
            for edit_type in ("updated", "added", "closed", "removed"):
                pd.DataFrame({"Polygon ID": ["P1"], "Polygon Name": ["Shop"], "Category Name": ["Retail"], "Country": ["USA"], "Area Diff(sqft)": [2]}).to_csv(root / f"{edit_type}_ppoi_251.csv", index=False)
            captured = []
            def capture(frame, name, *args, **kwargs):
                captured.append((name, frame.copy(), kwargs))
            with patch.object(common, "sql_engine", return_value=MagicMock()), patch.object(pd.DataFrame, "to_sql", capture):
                output = ppoi_change_logs.import_ppoi_logs("251", Config(signals_root=folder))
            self.assertEqual(output["EditType"].tolist(), ["updated", "added", "closed", "removed"])
            self.assertEqual(output["PolygonID"].tolist(), ["P1"] * 4)
            self.assertTrue(output.loc[1:, "Area Diff(sqft)"].isna().all())
            self.assertEqual([item[0] for item in captured], ["PPOI_Change_Log", "PPOI_Change_Log_Summary"])
            for edit_type in ("updated", "added", "closed", "removed"):
                self.assertEqual(captured[1][1].iloc[0][edit_type], 1)
            for path in root.glob("*.csv"):
                if not path.name.startswith("updated"):
                    path.unlink()
            with patch.object(ppoi_change_logs, "append_to_log_tables"):
                self.assertEqual(len(ppoi_change_logs.import_ppoi_logs("251", Config(signals_root=folder))), 1)

    def test_geojson_metadata_and_2d_output(self):
        import pandas as pd
        import geopandas as gpd
        from shapely.geometry import Polygon
        from azira_download_part01 import geographic_polygons as geo, common
        frame = gpd.GeoDataFrame({"id": ["P1"], "Size": [4]}, geometry=[Polygon([(0, 0, 2), (1, 0, 2), (1, 1, 2), (0, 0, 2)])], crs="EPSG:4326")
        writes = []
        def capture(output, name, *args, **kwargs):
            writes.append((output.copy(), name, kwargs))
        with patch.object(geo, "import_geojson", return_value=frame), patch.object(common, "sql_connection", return_value=MagicMock()) as sql, patch.object(common, "send_mail") as mail:
            with patch.object(common, "sql_engine", return_value=MagicMock()), patch.object(common, "execute_call") as procedure:
                with patch.object(pd.DataFrame, "to_sql", capture):
                    geo.full_process_new_geo_json_file("signals_20260917_251.json", Config())
        output, table, options = writes[0]
        self.assertEqual(list(output.columns), ["PolygonID", "Size", "FileDate", "vintage_id", "wkt", "isDummy"])
        self.assertEqual(output.iloc[0]["FileDate"], "2026-09-17")
        self.assertEqual(output.iloc[0]["vintage_id"], "251")
        self.assertNotIn(" Z ", output.iloc[0]["wkt"])
        sql.return_value.cursor.return_value.execute.assert_called_once_with("DELETE FROM signals_geog_import;")
        procedure.assert_called_once_with(Config(), "dbo.usp_Process_geofences", autocommit=True)
        self.assertEqual(table, "signals_geog_import")
        self.assertEqual(options["chunksize"], 250000)
        self.assertEqual([call.args[1] for call in mail.call_args_list], [
            "Azira geographic polygons: Read GeoJSON",
            "Azira geographic polygons: Clear SQL staging",
            "Azira geographic polygons: Convert geometry to 2D WKT",
            "Azira geographic polygons: Load SQL staging",
            "Azira geographic polygons: Execute geofence procedure",
        ])

    def test_geojson_step_notifications_precede_operations(self):
        import geopandas as gpd
        from shapely.geometry import Point
        from azira_download_part01 import geographic_polygons as geo, common
        events = []
        frame = gpd.GeoDataFrame({"id": ["P1"], "Size": [4]}, geometry=[Point(0, 0)])
        def read(filename):
            events.append("read")
            return frame
        def connection(config):
            events.append("clear")
            return MagicMock()
        def engine(config):
            events.append("load")
            return MagicMock()
        with patch.object(common, "send_mail", side_effect=lambda config, subject, body: events.append(subject)), patch.object(geo, "import_geojson", side_effect=read), patch.object(common, "sql_connection", side_effect=connection), patch.object(common, "sql_engine", side_effect=engine), patch.object(gpd.GeoDataFrame, "to_sql"), patch.object(common, "execute_call", side_effect=lambda *args, **kwargs: events.append("procedure")), contextlib.redirect_stdout(io.StringIO()):
            geo.full_process_new_geo_json_file("signals_20260917_251.json", Config())
        for step, operation in (("Read GeoJSON", "read"), ("Clear SQL staging", "clear"), ("Load SQL staging", "load"), ("Execute geofence procedure", "procedure")):
            self.assertLess(events.index(f"Azira geographic polygons: {step}"), events.index(operation))

    def test_geojson_notification_failure_stops_before_sql_clear(self):
        from azira_download_part01 import geographic_polygons as geo, common
        def mail(config, subject, body):
            if subject.endswith("Clear SQL staging"):
                raise RuntimeError("mail unavailable")
        with patch.object(common, "send_mail", side_effect=mail), patch.object(geo, "import_geojson", return_value=MagicMock()), patch.object(common, "sql_connection") as sql, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "mail unavailable"):
                geo.full_process_new_geo_json_file("signals_20260917_251.json", Config())
        sql.assert_not_called()


class WorkbookTests(unittest.TestCase):
    def test_real_workbook_selection_and_melt(self):
        import pandas as pd
        from azira_download_part01 import common, municipality_core_metrics as core
        with tempfile.TemporaryDirectory() as folder:
            config = Config(excel_dir=folder)
            old = Path(folder) / "core_metrics_HDV2_external_202601_NorthAmerica.xlsx"
            old.touch()
            latest = Path(folder) / "core_metrics_HDV2_external_202602_NorthAmerica.xlsx"
            frame = pd.DataFrame({
                **{f"key{index}": [f"value{index}"] for index in range(6)},
                "Jan2026": [2], "Feb2026": [3], "Oct2025": [99], "Nov2025": [99], "Dec2025": [99],
            })
            with pd.ExcelWriter(latest, engine="openpyxl") as writer:
                for sheet in core.WORKSHEETS:
                    frame.to_excel(writer, sheet_name=sheet, index=False)
            self.assertEqual(core.resolve_latest_excel_file(config), (latest.name, str(latest), "202602"))
            engine = MagicMock()
            engine.connect.return_value.__enter__.return_value.execute.return_value.scalar.side_effect = [1, 0, 6]
            procedure_connection = MagicMock()
            procedure_connection.cursor.return_value.description = None
            procedure_connection.cursor.return_value.nextset.return_value = False
            captured = []
            def capture(output, table, *args, **kwargs):
                captured.append((output.copy(), kwargs))
            with patch.object(common, "sql_engine", return_value=engine), patch.object(common, "sql_connection", return_value=procedure_connection):
                with patch.object(common, "send_mail"), patch.object(common, "comment"), patch.object(pd.DataFrame, "to_sql", capture):
                    with contextlib.redirect_stdout(io.StringIO()):
                        core.run(config)
            output, options = captured[0]
            self.assertEqual(list(output.columns), [f"key{index}" for index in range(6)] + ["metric_column", "value", "source_sheet", "vintage"])
            self.assertEqual(output["metric_column"].tolist(), ["Jan2026", "Feb2026"] * 3)
            self.assertEqual(output["value"].tolist(), [2, 3] * 3)
            self.assertEqual(output["source_sheet"].tolist(), ["DataPoints"] * 2 + ["MAUs"] * 2 + ["DAUs"] * 2)
            self.assertEqual(output["vintage"].tolist(), ["202602"] * 6)
            self.assertTrue(all(dtype.length == 500 for dtype in options["dtype"].values()))

    def test_missing_workbook(self):
        from azira_download_part01.municipality_core_metrics import resolve_latest_excel_file
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                resolve_latest_excel_file(Config(excel_dir=folder))


class CoreImportTests(unittest.TestCase):
    def setUp(self):
        from azira_download_part01 import common, municipality_core_metrics
        self.core = municipality_core_metrics
        self.common = common
        self.engine = MagicMock()
        self.connection = self.engine.connect.return_value.__enter__.return_value
        self.mail = MagicMock()
        for owner, name, value in (
            (self.core, "resolve_latest_excel_file", MagicMock(return_value=("test.xlsx", "test.xlsx", "202601"))),
            (common, "send_mail", self.mail), (common, "comment", MagicMock()),
            (common, "sql_engine", MagicMock(return_value=self.engine)),
        ):
            patcher = patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_existing_vintage_skips(self):
        self.connection.execute.return_value.scalar.side_effect = [1, 20]
        with patch.object(self.common, "sql_connection") as sql, patch("builtins.print") as output:
            self.core.run(Config())
        sql.assert_not_called()
        self.engine.dispose.assert_called_once()
        output.assert_any_call("WORKFLOW_SKIPPED", flush=True)

    def run_import(self, count, late_error=None):
        frame = self.core.pd.DataFrame({**{f"id{index}": [index] for index in range(6)}, "Jan2026": [2], "Oct2025": [99]})
        self.connection.execute.return_value.scalar.side_effect = [1, 0, count]
        sql_connection = MagicMock()
        cursor = sql_connection.cursor.return_value
        cursor.description = None
        cursor.nextset.side_effect = late_error
        cursor.nextset.return_value = False
        with patch.object(self.core.pd, "read_excel", return_value=frame), patch.object(self.core.pd.DataFrame, "to_sql") as write:
            with patch.object(self.common, "sql_connection", return_value=sql_connection), patch("builtins.print"):
                self.core.run(Config())
        written = write.call_args.kwargs
        self.assertEqual(written["schema"], "dbo")
        self.assertEqual(written["if_exists"], "append")
        return sql_connection

    def test_new_vintage_success(self):
        sql = self.run_import(3)
        sql.close.assert_called_once()
        sql.cursor.return_value.nextset.assert_called_once()
        self.assertIn("complete", self.mail.call_args.args[1])

    def test_count_mismatch(self):
        with self.assertRaisesRegex(RuntimeError, "Import verification failed"):
            self.run_import(2)
        self.assertEqual(self.mail.call_count, 1)

    def test_deferred_sql_error(self):
        with self.assertRaisesRegex(RuntimeError, "late core SQL error"):
            self.run_import(3, RuntimeError("late core SQL error"))
        self.assertEqual(self.mail.call_count, 1)


class MasterfileTests(unittest.TestCase):
    def test_distinct_inventory_keys_cannot_share_a_local_file(self):
        from azira_download_part01 import polygon_masterfile as master, common
        with tempfile.TemporaryDirectory() as folder:
            config = Config(masterfile_root=folder, masterfile_inventory_strip_chars=5, masterfile_mapping_verified=True)
            filename = "2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz"
            path = Path(folder) / filename
            path.parent.mkdir()
            path.write_bytes(b"data")
            client = MagicMock()
            client.get_paginator.return_value.paginate.return_value = [{"Contents": [
                {"Key": f"hdv2/2026-09/{filename}", "Size": 4, "LastModified": "2026-10-01"},
                {"Key": f"hdv2/2026-10/{filename}", "Size": 4, "LastModified": "2026-10-01"},
            ]}]
            with patch.object(common, "aws_session") as aws, patch.object(common, "append_to_local_inventory_sql") as append:
                aws.return_value.client.return_value = client
                with self.assertRaisesRegex(ValueError, "collision"):
                    master.acquire_masterfiles(config)
            append.assert_not_called()
            client.download_file.assert_not_called()

    def test_month_inventory_and_date_only_local_paths(self):
        from datetime import datetime, timezone
        import pandas as pd
        from azira_download_part01 import polygon_masterfile as master, common
        with tempfile.TemporaryDirectory() as folder:
            config = Config(masterfile_root=folder, masterfile_inventory_strip_chars=5, masterfile_mapping_verified=True)
            s3_key = "hdv2/2026-10/2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz"
            inventory_key = s3_key[5:]
            local_key = s3_key[13:]
            self.assertEqual(master.mapped_key(config, s3_key), inventory_key)
            self.assertEqual(master.local_file(config, inventory_key), Path(folder) / local_key)
            path = Path(folder) / local_key
            path.parent.mkdir()
            path.write_bytes(b"data")
            client = MagicMock()
            client.head_object.return_value = {"ContentLength": 4}
            client.get_paginator.return_value.paginate.return_value = [{"Contents": [
                {"Key": s3_key, "Size": 4, "LastModified": datetime.now(timezone.utc)},
            ]}]
            connection = MagicMock()
            connection.cursor.return_value.fetchall.return_value = [(inventory_key, 4)]
            local = pd.DataFrame(columns=["key", "name_key", "last_modified", "size", "latest"])
            with patch.object(common, "aws_session") as aws, patch.object(common, "sql_connection", return_value=connection):
                aws.return_value.client.return_value = client
                with contextlib.redirect_stdout(io.StringIO()):
                    master.verify_mapping(config, s3_key, inventory_key)
                with patch.object(common, "read_local_inventory_from_sql", return_value=local), patch.object(common, "append_to_local_inventory_sql") as append:
                    master.acquire_masterfiles(config)
            client.download_file.assert_not_called()
            self.assertEqual(append.call_args.args[1]["key"].tolist(), [inventory_key])

    def test_read_only_mapping_verifier(self):
        from azira_download_part01 import polygon_masterfile as master, common
        with tempfile.TemporaryDirectory() as folder:
            config = Config(masterfile_root=folder)
            key = "hdv2/2026-10/2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz"
            inventory_key = key[13:]
            path = Path(folder) / inventory_key
            path.parent.mkdir()
            path.write_bytes(b"data")
            connection = MagicMock()
            connection.cursor.return_value.fetchall.return_value = [(inventory_key, 4)]
            with patch.object(common, "aws_session") as aws, patch.object(common, "sql_connection", return_value=connection):
                client = aws.return_value.client.return_value
                client.head_object.return_value = {"ContentLength": 4}
                with contextlib.redirect_stdout(io.StringIO()):
                    master.verify_mapping(config, key, inventory_key)
                client.download_file.assert_not_called()
                connection.commit.assert_not_called()
                statement = connection.cursor.return_value.execute.call_args.args[0]
                self.assertTrue(statement.startswith("SELECT"))
                self.assertFalse(config.masterfile_mapping_verified)
                with self.assertRaisesRegex(ValueError, "does not match"):
                    master.verify_mapping(config, key, "wrong-key")
                connection.cursor.return_value.fetchall.return_value = []
                with self.assertRaisesRegex(ValueError, "missing"):
                    master.verify_mapping(config, key, inventory_key)

    def test_unsafe_local_key(self):
        from azira_download_part01.polygon_masterfile import local_file
        for key in ("../escape.gz", "C:/absolute.gz", "/root.gz", "\\\\server\\share\\file.gz"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                local_file(Config(), key)

    def test_no_new_inventory_vintage(self):
        import pandas as pd
        from azira_download_part01 import polygon_masterfile as master, common
        with patch.object(common, "sql_connection", return_value=MagicMock()), patch.object(pd, "read_sql", return_value=pd.DataFrame()):
            self.assertIsNone(master.check_spm_inventory_from_sql(Config()))

    def test_unverified_mapping_blocks_external_calls(self):
        from azira_download_part01 import polygon_masterfile as master, common
        with patch.object(common, "aws_session") as aws, patch.object(common, "sql_connection") as sql:
            with self.assertRaisesRegex(RuntimeError, "unverified"):
                master.run(Config())
        aws.assert_not_called()
        sql.assert_not_called()

    def test_only_full_masterfiles(self):
        from azira_download_part01.polygon_masterfile import is_masterfile
        self.assertTrue(is_masterfile("hdv2/2026-10/2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz"))
        self.assertFalse(is_masterfile("hdv2/Signals_Polygon_Masterfile_diff_20261015_252.tsv.gz"))
        self.assertFalse(is_masterfile("hdv2/store_level_origins_252.tsv.gz"))

    def test_pagination_and_skipped_file_inventory(self):
        from datetime import datetime, timezone
        import pandas as pd
        from azira_download_part01 import polygon_masterfile as master, common
        with tempfile.TemporaryDirectory() as folder:
            config = Config(masterfile_root=folder, masterfile_mapping_verified=True)
            key = "hdv2/2026-10/2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz"
            local_key = key[13:]
            target = Path(folder) / local_key
            target.parent.mkdir(parents=True)
            target.write_bytes(b"existing")
            client = MagicMock()
            client.get_paginator.return_value.paginate.return_value = [
                {}, {"Contents": [{"Key": key, "Size": 8, "LastModified": datetime.now(timezone.utc)}, {"Key": "hdv2/unrelated.gz", "Size": 1}]},
            ]
            local = pd.DataFrame(columns=["key", "name_key", "last_modified", "size", "latest"])
            with patch.object(common, "aws_session") as session, patch.object(common, "read_local_inventory_from_sql", return_value=local):
                session.return_value.client.return_value = client
                with patch.object(common, "append_to_local_inventory_sql") as append:
                    master.acquire_masterfiles(config)
            client.download_file.assert_not_called()
            self.assertEqual(append.call_args.args[1]["key"].tolist(), [local_key])

    def test_transform_preserves_folder_date(self):
        import gzip
        import pandas as pd
        from azira_download_part01 import polygon_masterfile as master, common
        with tempfile.TemporaryDirectory() as folder:
            key = "2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz"
            path = Path(folder) / key
            path.parent.mkdir()
            source = pd.DataFrame({name: ["001"] for name in common.SPM_IMPORT_SCHEMA})
            with gzip.open(path, "wt", encoding="utf-8") as archive:
                source.to_csv(archive, sep="\t", index=False)
            captured = []
            def capture(frame, name, *args, **kwargs):
                captured.append((frame.copy(), name, kwargs))
            with patch.object(common, "sql_engine", return_value=MagicMock()), patch.object(common, "comment"), patch.object(common, "send_mail"):
                with patch.object(common, "execute_call"), patch.object(common, "update_processed"), patch.object(pd.DataFrame, "to_sql", capture):
                    master.update_spm_in_sql(key, Config(masterfile_root=folder))
            output, name, options = captured[0]
            self.assertEqual(output.iloc[0]["vintage"], "2026-10-04")
            self.assertEqual(output.iloc[0]["vintage_id"], "252")
            self.assertEqual(output.iloc[0]["PolygonID"], "001")
            self.assertEqual(output.iloc[0]["OpenStatus"], "open")
            self.assertEqual(len(output.columns), 29)
            self.assertEqual(options["if_exists"], "replace")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertFalse(load_config().masterfile_mapping_verified)

    def test_config_with_spaces_and_invalid_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local config.toml"
            path.write_text('signals_root = "C:/Data with spaces/"\n', encoding="utf-8")
            self.assertEqual(load_config(path).signals_root, "C:/Data with spaces/")
            path.write_text('secret_key = "not-supported"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)
            path.write_text('masterfile_mapping_verified = "true"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()