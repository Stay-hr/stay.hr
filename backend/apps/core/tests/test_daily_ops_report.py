from __future__ import annotations

import json
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.core.daily_ops_report.collectors.disk import DiskCollector
from apps.core.daily_ops_report.collectors.docker_signals import DockerSignalsCollector
from apps.core.daily_ops_report.collectors.gunicorn import GunicornCollector
from apps.core.daily_ops_report.export import SCHEMA_VERSION, export_json, metrics_from_snapshot
from apps.core.daily_ops_report.format import format_markdown, format_metric_delta
from apps.core.daily_ops_report.orchestrator import run_collectors
from apps.core.daily_ops_report.snapshot import (
    archive_markdown_path,
    load_snapshot,
    prune_old_files,
    report_dir,
    save_snapshot,
    snapshot_path,
)
from apps.core.daily_ops_report.tasks import run_daily_ops_report, send_daily_ops_report
from apps.core.daily_ops_report.types import MetricResult, ReportSection, Severity, max_severity
from apps.core.system_status import build_system_status_payload

ZAGREB = ZoneInfo("Europe/Zagreb")
TEST_MEDIA = "/tmp/stay-test-daily-ops-media"


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class SeverityTests(TestCase):
    def test_max_severity(self):
        self.assertEqual(max_severity(Severity.OK, Severity.WARN), Severity.WARN)
        self.assertEqual(max_severity(Severity.WARN, Severity.CRIT), Severity.CRIT)
        self.assertEqual(max_severity(Severity.OK), Severity.OK)


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class FormatDeltaTests(TestCase):
    def test_format_metric_delta_numeric(self):
        self.assertEqual(format_metric_delta(42.1, 40.0), "+2.1")
        self.assertEqual(format_metric_delta(40.0, 40.0), "0")
        self.assertEqual(format_metric_delta(None, 1), "—")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA,
    DAILY_OPS_REPORT_DISK_WARN_PCT=85,
    DAILY_OPS_REPORT_DISK_CRIT_PCT=95,
)
class DiskCollectorTests(TestCase):
    def test_disk_warn_and_crit_thresholds(self):
        collector = DiskCollector()
        with patch("apps.core.daily_ops_report.collectors.disk.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(total=100, used=86, free=14)
            section = collector.collect()
        pct_row = next(row for row in section.rows if row.key == "disk.used_pct")
        self.assertEqual(pct_row.status, Severity.WARN)

        with patch("apps.core.daily_ops_report.collectors.disk.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(total=100, used=96, free=4)
            section = collector.collect()
        pct_row = next(row for row in section.rows if row.key == "disk.used_pct")
        self.assertEqual(pct_row.status, Severity.CRIT)


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class DockerSignalsCollectorTests(TestCase):
    def setUp(self):
        Path(TEST_MEDIA).mkdir(parents=True, exist_ok=True)
        report_dir().mkdir(parents=True, exist_ok=True)
        for path in report_dir().glob("*"):
            if path.is_file():
                path.unlink()

    def test_missing_file_warns(self):
        section = DockerSignalsCollector().collect()
        self.assertEqual(section.severity, Severity.WARN)
        self.assertIn("missing file", section.rows[0].display)

    def test_reads_host_json(self):
        payload = {
            "generated_at": "2026-07-09T09:55:00+02:00",
            "metrics": {
                "worker_timeout_count": 2,
                "sse_stream_opened": 10,
                "sse_stream_closed": 9,
                "sse_invariant_breach": 0,
            },
        }
        path = report_dir() / "docker_signals.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        section = DockerSignalsCollector().collect()
        timeout_row = next(row for row in section.rows if row.key == "docker.worker_timeout_count")
        self.assertEqual(timeout_row.value, 2)
        self.assertEqual(timeout_row.status, Severity.WARN)
        breach_row = next(row for row in section.rows if row.key == "docker.sse_invariant_breach")
        self.assertEqual(breach_row.value, 0)
        self.assertEqual(breach_row.status, Severity.OK)

    def test_invariant_breach_is_crit(self):
        payload = {
            "generated_at": "2026-07-23T16:00:00+02:00",
            "metrics": {
                "worker_timeout_count": 0,
                "sse_stream_opened": 5,
                "sse_stream_closed": 5,
                "sse_invariant_breach": 1,
            },
        }
        path = report_dir() / "docker_signals.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        section = DockerSignalsCollector().collect()
        breach_row = next(row for row in section.rows if row.key == "docker.sse_invariant_breach")
        self.assertEqual(breach_row.value, 1)
        self.assertEqual(breach_row.status, Severity.CRIT)
        self.assertEqual(section.severity, Severity.CRIT)


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class GunicornCollectorTests(TestCase):
    def test_invariant_rows_ok_when_delta_zero(self):
        payload = {
            "schema_version": 1,
            "metrics_scope": "worker_process",
            "build": {"git_sha": "abc", "started_at": "2026-07-23T00:00:00+00:00", "hostname": "t"},
            "gunicorn": {
                "workers": 8,
                "worker_class": "sync",
                "pid": 1,
                "uptime_seconds": 10,
                "timeout": 3600,
            },
            "sse": {
                "active_connections": 1,
                "peak_connections": 2,
                "connections_opened_total": 5,
                "connections_closed_total": 4,
                "average_duration_seconds": 12.0,
                "active_stream_count": 1,
                "active_streams": [],
                "invariant_ok": True,
                "invariant_delta": 0,
            },
        }
        with patch(
            "apps.core.daily_ops_report.collectors.gunicorn.build_system_status_payload",
            return_value=payload,
        ):
            section = GunicornCollector().collect()
        delta_row = next(row for row in section.rows if row.key == "sse.invariant_delta")
        ok_row = next(row for row in section.rows if row.key == "sse.invariant_ok")
        self.assertEqual(delta_row.value, 0)
        self.assertEqual(delta_row.status, Severity.OK)
        self.assertEqual(ok_row.value, 1)
        self.assertEqual(ok_row.status, Severity.OK)

    def test_invariant_breach_is_crit(self):
        payload = {
            "schema_version": 1,
            "metrics_scope": "worker_process",
            "build": {"git_sha": "abc", "started_at": "2026-07-23T00:00:00+00:00", "hostname": "t"},
            "gunicorn": {
                "workers": 8,
                "worker_class": "sync",
                "pid": 1,
                "uptime_seconds": 10,
                "timeout": 3600,
            },
            "sse": {
                "active_connections": 0,
                "peak_connections": 1,
                "connections_opened_total": 5,
                "connections_closed_total": 3,
                "average_duration_seconds": None,
                "active_stream_count": 0,
                "active_streams": [],
                "invariant_ok": False,
                "invariant_delta": 2,
            },
        }
        with patch(
            "apps.core.daily_ops_report.collectors.gunicorn.build_system_status_payload",
            return_value=payload,
        ):
            section = GunicornCollector().collect()
        delta_row = next(row for row in section.rows if row.key == "sse.invariant_delta")
        self.assertEqual(delta_row.value, 2)
        self.assertEqual(delta_row.status, Severity.CRIT)
        self.assertEqual(section.severity, Severity.CRIT)


@override_settings(MEDIA_ROOT=TEST_MEDIA, DAILY_OPS_REPORT_KEEP_DAYS=90)
class SnapshotTests(TestCase):
    def setUp(self):
        Path(TEST_MEDIA).mkdir(parents=True, exist_ok=True)
        report_dir().mkdir(parents=True, exist_ok=True)
        for path in report_dir().glob("*"):
            if path.is_file():
                path.unlink()

    def _sample_report(self):
        from apps.core.daily_ops_report.types import DailyOpsReportResult

        metric = MetricResult(
            key="disk.used_pct",
            value=42.1,
            status=Severity.OK,
            display="42.1%",
        )
        section = ReportSection(
            title="Disk",
            severity=Severity.OK,
            rows=[metric],
            summary="test",
        )
        return DailyOpsReportResult(
            sections=[section],
            overall_severity=Severity.OK,
            duration_ms=100,
            generated_at_iso="2026-07-09T10:00:00+02:00",
            reporter_process="test",
            git_sha="abc1234",
            hostname="testhost",
            metrics={"disk.used_pct": metric},
        )

    def test_snapshot_schema_version_and_roundtrip(self):
        report = self._sample_report()
        save_snapshot(report)
        loaded = load_snapshot()
        assert loaded is not None
        self.assertEqual(loaded["schema_version"], SCHEMA_VERSION)
        self.assertEqual(loaded["metrics"]["disk.used_pct"]["value"], 42.1)

        metrics = metrics_from_snapshot(loaded)
        self.assertEqual(metrics["disk.used_pct"].value, 42.1)

    def test_prune_old_archive_files(self):
        old = archive_markdown_path("2020-01-01")
        old.write_text("# old", encoding="utf-8")
        recent = archive_markdown_path(date.today().isoformat())
        recent.write_text("# recent", encoding="utf-8")

        removed = prune_old_files(keep_days=90)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())


@override_settings(
    MEDIA_ROOT=TEST_MEDIA,
    DAILY_OPS_REPORT_ENABLED=False,
    DAILY_OPS_REPORT_EMAILS="",
    EMAIL_HOST="",
)
class DailyOpsTaskTests(TestCase):
    def test_task_skips_when_disabled(self):
        result = send_daily_ops_report()
        self.assertEqual(result["reason"], "disabled")

    @override_settings(
        DAILY_OPS_REPORT_ENABLED=True,
        DAILY_OPS_REPORT_EMAILS="",
        EMAIL_HOST="smtp.example.com",
    )
    def test_task_skips_without_recipients(self):
        result = send_daily_ops_report()
        self.assertEqual(result["reason"], "no_recipients")

    @override_settings(
        DAILY_OPS_REPORT_ENABLED=True,
        DAILY_OPS_REPORT_EMAILS="ops@example.com",
        EMAIL_HOST="",
    )
    def test_task_skips_without_smtp(self):
        result = send_daily_ops_report()
        self.assertEqual(result["reason"], "no_smtp")


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class SystemStatusServiceTests(TestCase):
    def test_build_system_status_payload_shape(self):
        payload = build_system_status_payload(reporter_process="test")
        self.assertEqual(payload["schema_version"], 3)
        self.assertIn("gunicorn", payload)
        self.assertIn("sse", payload)
        self.assertIn("event_bus", payload)
        self.assertEqual(payload["event_bus"]["backend"], "in_process")
        self.assertEqual(payload["event_bus"]["publish_count"], 0)
        self.assertEqual(payload["event_bus"]["receive_count"], 0)
        self.assertEqual(payload["event_bus"]["local_fallback_count"], 0)
        self.assertEqual(payload["event_bus"]["dedupe_drop_count"], 0)
        self.assertIn("database", payload)
        self.assertTrue(payload["database"]["ok"])
        self.assertIn("messaging", payload)
        self.assertIn("definitions", payload["messaging"])
        self.assertIn("outbox", payload["messaging"])
        self.assertIn("welcome_templates", payload["messaging"])
        wt = payload["messaging"]["welcome_templates"]
        self.assertIn("configured", wt)
        self.assertIn("missing_in_config", wt)
        self.assertIn("status", wt)
        self.assertEqual(
            payload["components"]["event_bus"]["status"],
            "healthy",
        )
        self.assertEqual(payload["components"]["sse"]["status"], "healthy")
        self.assertEqual(payload["components"]["database"]["status"], "healthy")
        self.assertEqual(payload["reporter_process"], "test")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA,
    DAILY_OPS_REPORT_TENANT_ID=999999,
)
class OrchestratorTests(TestCase):
    def setUp(self):
        Path(TEST_MEDIA).mkdir(parents=True, exist_ok=True)
        report_dir().mkdir(parents=True, exist_ok=True)

    @patch("apps.core.daily_ops_report.collectors.celery_workers.current_app")
    def test_run_collectors_produces_markdown_and_json(self, mock_app):
        mock_app.control.ping.return_value = [{"celery@stay_celery_worker": {"ok": "pong"}}]

        report = run_collectors()
        self.assertIn(report.overall_severity, Severity)
        self.assertTrue(report.metrics)

        markdown = format_markdown(report)
        self.assertIn("# Daily Ops Report", markdown)
        self.assertIn("disk.used_pct", markdown)

        exported = export_json(report)
        self.assertEqual(exported["schema_version"], SCHEMA_VERSION)
        self.assertIn("metrics", exported)


@override_settings(
    MEDIA_ROOT=TEST_MEDIA,
    DAILY_OPS_REPORT_EMAILS="ops@example.com",
    EMAIL_HOST="",
)
class ManagementCommandTests(TestCase):
    def setUp(self):
        Path(TEST_MEDIA).mkdir(parents=True, exist_ok=True)
        report_dir().mkdir(parents=True, exist_ok=True)
        for path in report_dir().glob("*"):
            if path.is_file():
                path.unlink()

    @patch("apps.core.daily_ops_report.collectors.celery_workers.current_app")
    def test_stdout_flag(self, mock_app):
        mock_app.control.ping.return_value = [{"celery@worker": {"ok": "pong"}}]
        out = StringIO()
        call_command("daily_ops_report", "--print-markdown", stdout=out)
        self.assertIn("# Daily Ops Report", out.getvalue())
        self.assertFalse(snapshot_path().exists())

    @patch("apps.core.daily_ops_report.collectors.celery_workers.current_app")
    def test_json_flag(self, mock_app):
        mock_app.control.ping.return_value = [{"celery@worker": {"ok": "pong"}}]
        out = StringIO()
        call_command("daily_ops_report", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)

    @patch("apps.core.daily_ops_report.tasks.send_ops_email", return_value=False)
    @patch("apps.core.daily_ops_report.collectors.celery_workers.current_app")
    def test_write_only_skips_email(self, mock_app, _mock_email):
        mock_app.control.ping.return_value = [{"celery@worker": {"ok": "pong"}}]
        out = StringIO()
        call_command("daily_ops_report", "--write-only", stdout=out)
        self.assertTrue(snapshot_path().exists())

    @patch("apps.core.daily_ops_report.tasks.send_ops_email", return_value=True)
    @patch("apps.core.daily_ops_report.collectors.celery_workers.current_app")
    def test_run_daily_ops_report_delta_in_markdown(self, mock_app, _mock_email):
        mock_app.control.ping.return_value = [{"celery@worker": {"ok": "pong"}}]
        run_daily_ops_report(send_email=False, write_files=True)

        report = run_collectors()
        previous = metrics_from_snapshot(load_snapshot())
        markdown = format_markdown(report, previous_metrics=previous)
        self.assertIn("| disk.used_pct |", markdown)
        self.assertIn("| Δ |", markdown)
