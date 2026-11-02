"""
Test suite for metadata log parsing and database input functionality.

This test exercises the XML parsing and database ORM functions in laueportal.scans.importer
using the test_log.xml file.
"""

import logging
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.core.settings import BeamlineSettings
from laueportal.scans import importer as scan_import
from laueportal.scans.schemas import ScanImportOutcome, ScanImportResult, ScanLogEntry, ScanLogStatus
from tests.settings_support import make_database, make_settings

MOTOR_GROUPS = BeamlineSettings().motor_groups  # the default settings' motor groups
XMLNS = BeamlineSettings().scan_log_namespace  # the default settings' namespace
ZONE = ZoneInfo("America/Chicago")  # the default database.legacy_timezone: the beamline's local clock

# Global test XML paths - shared between all test classes
TEST_XML_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scan_logs", "test_log.xml")
TEST_XML_PATH_2 = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scan_logs", "test_log_2.xml")


@pytest.fixture
def test_xml_data():
    """Load test XML data from test_log.xml file."""
    with open(TEST_XML_PATH, "rb") as f:
        return f.read()


@pytest.fixture
def test_xml_data_2():
    """Load test XML data from test_log_2.xml file."""
    with open(TEST_XML_PATH_2, "rb") as f:
        return f.read()


class TestMetadataParsing:
    """Test class for metadata parsing functionality."""

    @pytest.fixture
    def temp_database(self, tmp_path):
        """Create a temporary database for testing (a plain SQLite file, foreign keys not enforced)."""
        temp_db_path = str(tmp_path / "scans.db")

        # Create engine and tables
        engine = create_engine(f"sqlite:///{temp_db_path}")
        models.Base.metadata.create_all(engine)

        yield engine, temp_db_path

        engine.dispose()

    def test_parse_metadata_basic_functionality(self, test_xml_data):
        """Test basic functionality of parse_metadata function."""
        # Test with the first scan (scan_no=0)
        log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=2)

        # Verify log_dict structure
        assert isinstance(log_dict, dict)
        assert "scanNumber" in log_dict
        assert log_dict["scanNumber"] == "276990"

        # Verify scan_dims_list structure
        assert isinstance(scan_dims_list, list)
        assert len(scan_dims_list) > 0

        # Check that all expected fields are present in log_dict
        expected_fields = [
            "scanNumber",
            "time_epoch",
            "time",
            "user_name",
            "source_beamBad",
            "source_CCDshutter",
            "source_monoTransStatus",
            "source_energy_unit",
            "source_energy",
            "source_IDgap_unit",
            "source_IDgap",
            "source_IDtaper_unit",
            "source_IDtaper",
            "source_ringCurrent_unit",
            "source_ringCurrent",
            "sample_XYZ_unit",
            "sample_XYZ_desc",
            "sample_XYZ",
            "knife-edge_XYZ_unit",
            "knife-edge_XYZ_desc",
            "knife-edge_XYZ",
            "knife-edge_knifeScan_unit",
            "knife-edge_knifeScan",
            "mda_file",
            "scanEnd_abort",
            "scanEnd_time_epoch",
            "scanEnd_time",
            "scanEnd_scanDuration_unit",
            "scanEnd_scanDuration",
            "scanEnd_source_beamBad",
            "scanEnd_source_ringCurrent_unit",
            "scanEnd_source_ringCurrent",
        ]

        for field in expected_fields:
            assert field in log_dict, f"Missing field: {field}"

    def test_parse_metadata_different_scans(self, test_xml_data):
        """Test parsing different scans from the XML data."""
        # Test scanning through different scan indices
        for scan_no in range(2, 5):  # Test first 5 scans
            try:
                log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=scan_no)

                # Verify each scan has a valid scanNumber
                assert "scanNumber" in log_dict
                assert log_dict["scanNumber"] is not None
                assert isinstance(scan_dims_list, list)

                logging.info(f"Successfully parsed scan {scan_no}: {log_dict['scanNumber']}")

            except IndexError:
                # This is expected when we run out of scans
                logging.info(f"No more scans available at index {scan_no}")
                break

    def test_parse_metadata_data_types(self, test_xml_data):
        """Test that numeric fields are properly converted to correct data types."""
        log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=2)

        # Test numeric fields are None or numeric types
        numeric_fields = [
            "time_epoch",
            "source_energy",
            "source_IDgap",
            "source_IDtaper",
            "source_ringCurrent",
            "knife-edge_knifeScan",
            "scanEnd_time_epoch",
            "scanEnd_scanDuration",
            "scanEnd_source_ringCurrent",
        ]

        for field in numeric_fields:
            if field in log_dict and log_dict[field] is not None:
                # Should be numeric or None, not empty string
                assert log_dict[field] != "", f"Field {field} should not be empty string"
                if log_dict[field] is not None:
                    try:
                        float(log_dict[field])
                    except (ValueError, TypeError):
                        pytest.fail(f"Field {field} should be numeric, got: {log_dict[field]}")

    def test_parse_metadata_scan_dimensions(self, test_xml_data):
        """Test parsing of scan dimension data."""
        log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=2)

        # Verify scan dimensions structure
        assert len(scan_dims_list) > 0

        for scan_dim in scan_dims_list:
            assert isinstance(scan_dim, dict)
            assert "scanNumber" in scan_dim

            # Check for expected scan dimension fields
            expected_scan_fields = ["scan_dim", "scan_npts", "scan_after", "scan_cpt"]

            for field in expected_scan_fields:
                assert field in scan_dim, f"Missing scan field: {field}"

    def test_parse_metadata_multidimensional_scan(self, test_xml_data):
        """Test parsing of multidimensional scans (scan with nested scan elements)."""
        # Test scan_no=5 which should be the 4D scan (scan number 276993)
        log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=5)

        assert log_dict["scanNumber"] == "276993"

        # This scan should have multiple dimensions
        assert len(scan_dims_list) > 1, "Multidimensional scan should have multiple scan dimensions"

        # Verify the dimensions are in the correct order (from innermost to outermost)
        for _, scan_dim in enumerate(scan_dims_list):
            assert "scan_dim" in scan_dim
            assert "scan_npts" in scan_dim
            assert "scan_cpt" in scan_dim

    def test_parse_metadata_test_log_2(self, test_xml_data_2):
        """Test parsing scans from test_log_2.xml."""
        # Test scanning through different scan indices in test_log_2.xml
        for scan_no in range(2, 9):  # Test scans in test_log_2.xml
            try:
                log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data_2, xmlns=XMLNS, scan_no=scan_no)

                # Verify each scan has a valid scanNumber
                assert "scanNumber" in log_dict
                assert log_dict["scanNumber"] is not None
                assert isinstance(scan_dims_list, list)

                logging.info(f"Successfully parsed scan {scan_no}: {log_dict['scanNumber']}")

            except IndexError:
                # This is expected when we run out of scans
                logging.info(f"No more scans available at index {scan_no}")
                break

    def test_parse_all_scans_from_xml(self, test_xml_data):
        """Test parsing all scans from a log file for the bulk import workflow."""
        parsed = scan_import.parse_all_scans_from_xml(test_xml_data, xmlns=XMLNS)

        assert len(parsed) > 0
        assert isinstance(parsed[0], scan_import.ParsedScan)
        assert (parsed[0].scan_number, parsed[0].scan_index) == (276990, 2)
        assert parsed[0].log["scanNumber"] == "276990"  # the metadata row values, as parsed
        assert len(parsed[0].scans) == 1

    def test_parse_all_scans_from_xml_logs_skipped_scans(self, test_xml_data, monkeypatch, caplog):
        """Test parse failures for individual scans are logged instead of silently disappearing."""
        original_parse_scan_element = scan_import._parse_scan_element

        def fail_first_scan(scan, *, xmlns, empty="\n\t\t"):
            if scan.get("scanNumber") == "276990":
                raise ValueError("bad scan")
            return original_parse_scan_element(scan, xmlns=xmlns, empty=empty)

        monkeypatch.setattr(scan_import, "_parse_scan_element", fail_first_scan)

        with caplog.at_level(logging.ERROR, logger="laueportal.scans.importer"):
            parsed = scan_import.parse_all_scans_from_xml(test_xml_data, xmlns=XMLNS)

        assert parsed
        assert all(scan.scan_index != 2 for scan in parsed)
        assert "Skipping scan at XML index 2 after parse failure" in caplog.text
        assert "bad scan" in caplog.text

    def test_scan_index_is_lightweight_and_parses_document_once(self, test_xml_data, monkeypatch):
        original_fromstring = scan_import.ET.fromstring
        parse_calls = 0

        def counted_fromstring(xml):
            nonlocal parse_calls
            parse_calls += 1
            return original_fromstring(xml)

        monkeypatch.setattr(scan_import.ET, "fromstring", counted_fromstring)

        index = scan_import.index_scans_from_xml(test_xml_data, xmlns=XMLNS)

        assert parse_calls == 1
        assert len(index) == 5
        assert index[0] == ScanLogEntry(  # typed raw values, keyed by position (no stringified "scan_key")
            scan_index=2,
            scan_number=276990,
            status=ScanLogStatus.NEW,
            time="2023-02-25T02:09:58",
            time_epoch=1046160598,
            user_name="User1",
            energy=21.3047,
            energy_unit="keV",
            sample_xyz="7411.3 -492.2 -917",
            num_dims=1,
        )

    def test_parse_all_scans_parses_document_once(self, test_xml_data, monkeypatch):
        original_fromstring = scan_import.ET.fromstring
        parse_calls = 0

        def counted_fromstring(xml):
            nonlocal parse_calls
            parse_calls += 1
            return original_fromstring(xml)

        monkeypatch.setattr(scan_import.ET, "fromstring", counted_fromstring)

        parsed = scan_import.parse_all_scans_from_xml(test_xml_data, xmlns=XMLNS)

        assert parsed
        assert parse_calls == 1

    def test_parse_selected_scans_supports_individuals_and_blocks(self, test_xml_data):
        parsed = scan_import.parse_selected_scans_from_xml(test_xml_data, [2, 4, 5], xmlns=XMLNS)

        assert [scan.scan_index for scan in parsed] == [2, 4, 5]
        assert [scan.scan_number for scan in parsed] == [276990, 276992, 276993]

    def test_parse_selected_scans_rejects_unknown_indices(self, test_xml_data):
        with pytest.raises(ValueError, match="999"):
            scan_import.parse_selected_scans_from_xml(test_xml_data, [2, 999], xmlns=XMLNS)

    def test_staged_scan_log_round_trip_and_expiry(self, test_xml_data, tmp_path, monkeypatch):
        monkeypatch.setattr(scan_import, "SCAN_LOG_CACHE_TTL_SECONDS", 60)
        files = make_settings(tmp_path).files

        token = scan_import.stage_scan_log(test_xml_data, files=files)
        cache_path = scan_import.staging_directory(files) / f"{token}.xml"

        assert scan_import.load_staged_scan_log(token, files=files) == test_xml_data
        assert cache_path.stat().st_mode & 0o777 == 0o600

        old_time = cache_path.stat().st_mtime - 120
        os.utime(cache_path, (old_time, old_time))
        with pytest.raises(FileNotFoundError, match="expired or is unavailable"):
            scan_import.load_staged_scan_log(token, files=files)

    def test_staging_past_the_total_limit_drops_the_logs_used_least_recently(self, tmp_path, monkeypatch):
        """At most ``STAGED_SCAN_LOGS_LIMIT`` bytes: a new log drops the least recently used (a load refreshes its log)
        until it fits."""
        monkeypatch.setattr(scan_import, "STAGED_SCAN_LOGS_LIMIT", 30)
        files = make_settings(tmp_path).files
        tokens = []
        for index in range(3):
            tokens.append(scan_import.stage_scan_log(b"x" * 10, files=files))
            path = scan_import.staging_directory(files) / f"{tokens[-1]}.xml"
            os.utime(path, (1_000_000 + index, time.time() - 100 + index))
        scan_import.load_staged_scan_log(tokens[0], files=files)  # used now: the most recent

        newest = scan_import.stage_scan_log(b"y" * 20, files=files)

        assert sorted(path.stem for path in scan_import.staging_directory(files).glob("*.xml")) == sorted(
            [tokens[0], newest]
        )

    @pytest.mark.parametrize("token", ["../scan-log", "not-a-uuid", None])
    def test_staged_scan_log_rejects_invalid_tokens(self, token, tmp_path):
        with pytest.raises(ValueError, match="Invalid staged scan-log token"):
            scan_import.load_staged_scan_log(token, files=make_settings(tmp_path).files)


class TestDatabaseIntegration:
    """Test class for database integration functionality."""

    @pytest.fixture
    def temp_database(self, tmp_path):
        """Create a temporary database for testing (a plain SQLite file, foreign keys not enforced)."""
        temp_db_path = str(tmp_path / "scans.db")

        # Create engine and tables
        engine = create_engine(f"sqlite:///{temp_db_path}")
        models.Base.metadata.create_all(engine)

        yield engine, temp_db_path

        engine.dispose()

    def test_import_metadata_row(self, test_xml_data, temp_database):
        """Test creating a Metadata ORM object from parsed data."""
        engine, temp_db_path = temp_database

        # Parse metadata from XML
        log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=2)

        # Create metadata row
        metadata_row = scan_import.import_metadata_row(log_dict, timezone=ZONE)

        # Verify the metadata row is a valid ORM object
        assert isinstance(metadata_row, models.Metadata)
        assert metadata_row.scan_number == "276990"
        assert metadata_row.user_name == "User1"

        # Test database insertion
        with Session(engine) as session:
            # Add required fields that aren't in the XML
            metadata_row.date = datetime(2023, 2, 25)
            metadata_row.commit_id = "test_commit"
            metadata_row.runtime = "test_runtime"
            metadata_row.computer_name = "test_computer"
            metadata_row.dataset_id = 1
            metadata_row.notes = "test_notes"

            session.add(metadata_row)
            session.commit()

            # Verify the record was inserted
            retrieved = session.query(models.Metadata).filter_by(scan_number="276990").first()
            assert retrieved is not None
            assert retrieved.user_name == "User1"

    def test_import_scan_row(self, test_xml_data, temp_database):
        """Test creating Scan ORM objects from parsed data."""
        engine, temp_db_path = temp_database

        # Parse metadata from XML
        log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=2)

        # Create scan rows
        scan_rows = []
        for scan_dict in scan_dims_list:
            scan_row = scan_import.import_scan_row(scan_dict)
            assert isinstance(scan_row, models.Scan)
            scan_rows.append(scan_row)

        # Test database insertion
        with Session(engine) as session:
            for i, scan_row in enumerate(scan_rows):
                scan_row.id = i + 1  # Set a unique ID
                session.add(scan_row)
            session.commit()

            # Verify the records were inserted
            retrieved_scans = session.query(models.Scan).filter_by(scan_number="276990").all()
            assert len(retrieved_scans) == len(scan_rows)

    def test_end_to_end_workflow(self, test_xml_data, temp_database):
        """Test the complete workflow from XML parsing to database insertion."""
        engine, temp_db_path = temp_database

        # Parse metadata from XML (test the first scan)
        log_dict, scan_dims_list = scan_import.parse_metadata(test_xml_data, xmlns=XMLNS, scan_no=2)

        # Create metadata row
        metadata_row = scan_import.import_metadata_row(log_dict, timezone=ZONE)

        # Create scan rows
        scan_rows = []
        for scan_dict in scan_dims_list:
            scan_row = scan_import.import_scan_row(scan_dict)
            scan_rows.append(scan_row)

        # Simulate the scan-creation workflow
        with Session(engine) as session:
            # Add required metadata fields
            metadata_row.date = datetime(2023, 2, 25)
            metadata_row.commit_id = "test_commit"
            metadata_row.runtime = "test_runtime"
            metadata_row.computer_name = "test_computer"
            metadata_row.dataset_id = 1
            metadata_row.notes = "test_notes"

            # Add metadata to session
            session.add(metadata_row)

            # Add scan rows to session
            scan_row_count = session.query(models.Scan).count()
            for i, scan_row in enumerate(scan_rows):
                scan_row.id = scan_row_count + i + 1
                session.add(scan_row)

            session.commit()

            # Verify everything was inserted correctly
            metadata_check = session.query(models.Metadata).filter_by(scan_number="276990").first()
            assert metadata_check is not None
            assert metadata_check.user_name == "User1"

            scan_check = session.query(models.Scan).filter_by(scan_number="276990").all()
            assert len(scan_check) == len(scan_rows)

    def test_multiple_scans_workflow(self, test_xml_data, temp_database):
        """Test processing multiple different scans from the XML."""
        engine, temp_db_path = temp_database

        with Session(engine) as session:
            scan_row_count = 0

            # Process first 3 scans (starting from valid scan indices)
            for scan_index in range(2, 5):  # Use indices 2, 3, 4
                try:
                    # Parse metadata from XML
                    log_dict, scan_dims_list = scan_import.parse_metadata(
                        test_xml_data, xmlns=XMLNS, scan_no=scan_index
                    )

                    # Create and insert metadata row
                    metadata_row = scan_import.import_metadata_row(log_dict, timezone=ZONE)
                    metadata_row.date = datetime(2023, 2, 25)
                    metadata_row.commit_id = f"test_commit_{scan_index}"
                    metadata_row.runtime = "test_runtime"
                    metadata_row.computer_name = "test_computer"
                    metadata_row.dataset_id = 1
                    metadata_row.notes = f"test_notes_{scan_index}"

                    session.add(metadata_row)

                    # Create and insert scan rows
                    for scan_dict in scan_dims_list:
                        scan_row = scan_import.import_scan_row(scan_dict)
                        scan_row.id = scan_row_count + 1
                        scan_row_count += 1
                        session.add(scan_row)

                except IndexError:
                    # No more scans available
                    break

            session.commit()

            # Verify multiple scans were inserted
            all_metadata = session.query(models.Metadata).all()
            assert len(all_metadata) >= 3

            all_scans = session.query(models.Scan).all()
            assert len(all_scans) > 0

    def test_multiple_scans_workflow_test_log_2(self, test_xml_data_2, temp_database):
        """Test processing multiple different scans from test_log_2.xml."""
        engine, temp_db_path = temp_database

        with Session(engine) as session:
            scan_row_count = 0

            # Process scans from test_log_2.xml (starting from valid scan indices)
            for scan_index in range(2, 9):  # Test all scans in test_log_2.xml
                try:
                    # Parse metadata from XML
                    log_dict, scan_dims_list = scan_import.parse_metadata(
                        test_xml_data_2, xmlns=XMLNS, scan_no=scan_index
                    )

                    # Create and insert metadata row
                    metadata_row = scan_import.import_metadata_row(log_dict, timezone=ZONE)
                    metadata_row.date = datetime(2022, 4, 19)
                    metadata_row.commit_id = f"test_commit_2_{scan_index}"
                    metadata_row.runtime = "test_runtime"
                    metadata_row.computer_name = "test_computer"
                    metadata_row.dataset_id = 1
                    metadata_row.notes = f"test_notes_2_{scan_index}"

                    session.add(metadata_row)

                    # Create and insert scan rows
                    for scan_dict in scan_dims_list:
                        scan_row = scan_import.import_scan_row(scan_dict)
                        scan_row.id = scan_row_count + 1
                        scan_row_count += 1
                        session.add(scan_row)

                except IndexError:
                    # No more scans available
                    break

            session.commit()

            # Verify scans were inserted
            all_metadata = session.query(models.Metadata).all()
            assert len(all_metadata) > 0

            all_scans = session.query(models.Scan).all()
            assert len(all_scans) > 0

    def test_bulk_import_scans_imports_metadata_scans_and_catalog(self, test_xml_data, temp_database):
        """Test the bulk import service imports all rows for a selected scan."""
        engine, temp_db_path = temp_database
        parsed_scan = scan_import.parse_all_scans_from_xml(test_xml_data, xmlns=XMLNS)[0]
        catalog_defaults = {
            "filefolder": "/data/scans",
            "filename_prefix": ["scan_"],
            "aperture": "wire",
            "sample_name": "Si",
            "notes": "bulk import test",
        }

        results = scan_import.bulk_import_scans(
            [parsed_scan], catalog_defaults, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE
        )

        assert results == [
            ScanImportResult(scan_index=2, scan_number=276990, outcome=ScanImportOutcome.IMPORTED, error=None)
        ]

        with Session(engine) as session:
            metadata = session.query(models.Metadata).filter_by(scan_number=parsed_scan.scan_number).one()
            scans = session.query(models.Scan).filter_by(scan_number=parsed_scan.scan_number).all()
            catalog = session.query(models.Catalog).filter_by(scan_number=parsed_scan.scan_number).one()

            assert metadata.user_name == parsed_scan.log["user_name"]
            assert len(scans) == len(parsed_scan.scans)
            assert catalog.filefolder == "/data/scans"
            assert catalog.filename_prefix == ["scan_"]
            assert catalog.aperture == "wire"
            assert catalog.sample_name == "Si"
            assert catalog.notes == "bulk import test"

    def test_bulk_import_scans_skips_existing_scans(self, test_xml_data, temp_database):
        """Test the bulk import service reports existing scans without duplicating rows."""
        engine, temp_db_path = temp_database
        parsed_scan = scan_import.parse_all_scans_from_xml(test_xml_data, xmlns=XMLNS)[0]
        catalog_defaults = {
            "filefolder": "/data/scans",
            "filename_prefix": ["scan_"],
            "aperture": "wire",
            "sample_name": "Si",
            "notes": "bulk import test",
        }

        first_results = scan_import.bulk_import_scans(
            [parsed_scan], catalog_defaults, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE
        )
        second_results = scan_import.bulk_import_scans(
            [parsed_scan], catalog_defaults, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE
        )

        assert [result.outcome for result in first_results] == [ScanImportOutcome.IMPORTED]
        assert second_results == [
            ScanImportResult(scan_index=2, scan_number=276990, outcome=ScanImportOutcome.EXISTS, error=None)
        ]

        with Session(engine) as session:
            assert session.query(models.Metadata).filter_by(scan_number=parsed_scan.scan_number).count() == 1
            assert session.query(models.Catalog).filter_by(scan_number=parsed_scan.scan_number).count() == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_motor_groups_come_from_the_caller():
    """Motor groups are settings (``beamline.motor_groups``), passed in rather than read from a YAML file."""

    class Dimension:
        scan_positioner1_pv = "34ide:mxv:c0:m1.VAL"
        scan_positioner2_pv = "lab:stage:x"
        scan_positioner3_pv = ""
        scan_positioner4_pv = ""
        scan_npts = 3
        scan_cpt = 2

    assert scan_import.find_motor_group("34ide:mxv:c0:m1.VAL", motor_groups=MOTOR_GROUPS) == "sample"
    assert scan_import.find_motor_group("lab:stage:x", motor_groups=MOTOR_GROUPS) == "other"
    custom = {"stage": ["lab:stage:x"]}
    assert scan_import.find_motor_group("lab:stage:x", motor_groups=custom) == "stage"
    assert scan_import.find_motor_group(None, motor_groups=custom) == "none"
    assert scan_import.build_technique_strings([Dimension()], motor_groups=MOTOR_GROUPS) == (
        "line + other",
        "sample; other",
    )
    assert scan_import.update_motor_group_totals({}, Dimension(), motor_groups=custom) == {
        "other": {"points": 3, "completed": 2},
        "stage": {"points": 3, "completed": 2},
    }


def test_existing_scan_numbers_are_read_from_the_given_database(tmp_path, test_xml_data):
    engine = create_engine(f"sqlite:///{tmp_path / 'existing.db'}")
    models.Base.metadata.create_all(engine)
    try:
        parsed = scan_import.parse_all_scans_from_xml(test_xml_data, xmlns=XMLNS)[0]
        scan_import.bulk_import_scans([parsed], {}, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE)
        number = parsed.scan_number
        assert scan_import.check_existing_scan_numbers([str(number), "1"], engine=engine) == {number}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Typed per-scan records and settings-driven namespace and staging


def _log_with_duplicate_and_invalid(test_xml_data: bytes) -> bytes:
    """test_log.xml with scan 276992 renumbered 276990 (a duplicate) and 276994 renumbered "x94" (invalid)."""
    return test_xml_data.replace(b'scanNumber="276992"', b'scanNumber="276990"').replace(
        b'scanNumber="276994"', b'scanNumber="x94"'
    )


def test_scan_log_index_statuses_new_exists_duplicate_invalid(tmp_path, test_xml_data):
    _, engine = make_database(tmp_path)
    try:
        existing = scan_import.parse_selected_scans_from_xml(test_xml_data, [3], xmlns=XMLNS)
        scan_import.bulk_import_scans(
            existing, {}, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE
        )  # scan 276991

        log = _log_with_duplicate_and_invalid(test_xml_data)
        offline = scan_import.index_scans_from_xml(log, xmlns=XMLNS)
        index = scan_import.index_scan_log(log, xmlns=XMLNS, engine=engine)
    finally:
        engine.dispose()

    assert [(entry.scan_index, entry.scan_number, entry.status) for entry in index] == [
        (2, 276990, ScanLogStatus.NEW),
        (3, 276991, ScanLogStatus.EXISTS),
        (4, 276990, ScanLogStatus.DUPLICATE),
        (5, 276993, ScanLogStatus.NEW),
        (6, None, ScanLogStatus.INVALID),
    ]
    assert offline[1].status == ScanLogStatus.NEW  # without the database nothing "exists"
    assert [entry.model_dump(exclude={"status"}) for entry in offline] == [
        entry.model_dump(exclude={"status"}) for entry in index
    ]


def test_import_selected_scans_reports_one_typed_result_per_selected_scan(tmp_path, test_xml_data, monkeypatch):
    original = scan_import._parse_scan_element

    def fail_scan_5(scan, *, xmlns, empty="\n\t\t"):
        if scan.get("scanNumber") == "276993":
            raise ValueError("bad positioner")
        return original(scan, xmlns=xmlns, empty=empty)

    monkeypatch.setattr(scan_import, "_parse_scan_element", fail_scan_5)
    log = _log_with_duplicate_and_invalid(test_xml_data)
    catalog_defaults = {"filefolder": "/data/scans", "filename_prefix": ["scan_"], "aperture": "wire"}
    _, engine = make_database(tmp_path)
    try:
        results = scan_import.import_selected_scans(
            log, [6, 5, 4, 2], catalog_defaults, xmlns=XMLNS, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE
        )
        with pytest.raises(ValueError, match="not present in the staged log: 999"):
            scan_import.import_selected_scans(
                log, [2, 999], catalog_defaults, xmlns=XMLNS, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE
            )
        with Session(engine) as session:
            assert session.query(models.Metadata.scan_number).all() == [(276990,)]
    finally:
        engine.dispose()

    assert [(result.scan_index, result.scan_number, result.outcome) for result in results] == [
        (2, 276990, ScanImportOutcome.IMPORTED),
        (4, 276990, ScanImportOutcome.EXISTS),  # the duplicate in the log: the first one was imported
        (5, 276993, ScanImportOutcome.FAILED),
        (6, None, ScanImportOutcome.FAILED),
    ]
    assert [result.error for result in results[:2]] == [None, None]
    assert results[2].error == "ValueError: bad positioner"
    assert results[3].error == "invalid scan number 'x94'"


def test_scan_logs_are_staged_under_the_uploads_dir_never_in_tmp(tmp_path, test_xml_data, monkeypatch):
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))  # what /tmp would be

    default = make_settings(tmp_path / "a").files  # uploads_dir not set: <runs_root>/.uploads
    configured = make_settings(tmp_path / "b", files={"uploads_dir": tmp_path / "uploads"}).files

    assert scan_import.staging_directory(default) == tmp_path / "a" / "runs" / ".uploads" / "scan-logs"
    assert scan_import.staging_directory(configured) == tmp_path / "uploads" / "scan-logs"
    for files in (default, configured):
        token = scan_import.stage_scan_log(test_xml_data, files=files)
        assert sorted(path.name for path in scan_import.staging_directory(files).iterdir()) == [f"{token}.xml"]
        assert scan_import.staging_directory(files).stat().st_mode & 0o777 == 0o700
    assert list(system_tmp.iterdir()) == []


def test_expired_staged_logs_and_stale_temporary_files_are_cleaned_up(tmp_path, test_xml_data):
    files = make_settings(tmp_path).files
    fresh = scan_import.stage_scan_log(test_xml_data, files=files)
    expired = scan_import.stage_scan_log(test_xml_data, files=files)
    directory = scan_import.staging_directory(files)
    stale_upload = directory / f".{expired}-crashed.tmp"
    stale_upload.write_bytes(b"partial")
    day_ago = time.time() - scan_import.SCAN_LOG_CACHE_TTL_SECONDS - 60
    for path in (directory / f"{expired}.xml", stale_upload):
        os.utime(path, (day_ago, day_ago))

    assert scan_import.cleanup_expired_scan_logs(files) == 2
    assert sorted(path.name for path in directory.iterdir()) == [f"{fresh}.xml"]
    assert scan_import.cleanup_expired_scan_logs(make_settings(tmp_path / "unused").files) == 0  # no directory yet
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:  # a folder the server may not open: nothing to clean up, not an error
        assert (
            scan_import.cleanup_expired_scan_logs(make_settings(tmp_path, files={"uploads_dir": locked / "u"}).files)
            == 0
        )
    finally:
        locked.chmod(0o700)

    # A new upload also sweeps: logs not used for 24 h go.
    os.utime(directory / f"{fresh}.xml", (day_ago, day_ago))
    newest = scan_import.stage_scan_log(test_xml_data, files=files)
    assert sorted(path.name for path in directory.iterdir()) == [f"{newest}.xml"]


def test_the_scan_log_namespace_comes_from_the_caller(test_xml_data):
    custom = test_xml_data.replace(XMLNS.encode(), b"urn:example:otherBeamline")

    [entry, *_] = scan_import.index_scans_from_xml(custom, xmlns="urn:example:otherBeamline")
    assert (entry.scan_number, entry.user_name, entry.energy, entry.num_dims) == (276990, "User1", 21.3047, 1)
    [parsed, *_] = scan_import.parse_all_scans_from_xml(custom, xmlns="urn:example:otherBeamline")
    assert parsed.log["user_name"] == "User1" and len(parsed.scans) == 1

    # The default namespace finds no fields in this log.
    [entry, *_] = scan_import.index_scans_from_xml(custom, xmlns=XMLNS)
    assert (entry.scan_number, entry.user_name, entry.energy, entry.num_dims) == (276990, None, None, 0)
    assert not hasattr(scan_import, "SCAN_LOG_XMLNS")


EPICS_EPOCH = datetime(1990, 1, 1, tzinfo=UTC)


def test_scan_time_is_stored_in_utc_and_matches_the_logs_epics_epoch(tmp_path, test_xml_data):
    """The log's ``time`` is the beamline's local time; it is stored in UTC, which is the instant the log's
    EPICS ``epoch`` (seconds since 1990-01-01 UTC) names. ``scan_end_time`` stays the log's text."""
    parsed = scan_import.parse_all_scans_from_xml(test_xml_data, xmlns=XMLNS)
    _, engine = make_database(tmp_path)
    try:
        results = scan_import.bulk_import_scans(parsed, {}, engine=engine, motor_groups=MOTOR_GROUPS, timezone=ZONE)
        assert {result.outcome for result in results} == {ScanImportOutcome.IMPORTED}
        with Session(engine) as session:
            rows = {row.scan_number: row for row in session.query(models.Metadata)}
        with engine.connect() as connection:
            stored = connection.exec_driver_sql("SELECT time FROM metadata WHERE scan_number = 276990").scalar_one()
    finally:
        engine.dispose()

    assert parsed[0].log["time"] == "2023-02-25T02:09:58"  # CST (UTC-6)
    assert rows[276990].time == datetime(2023, 2, 25, 8, 9, 58, tzinfo=UTC)
    assert stored == "2023-02-25T08:09:58.000000Z"
    assert len(rows) == len(parsed) > 1
    for scan in parsed:
        row = rows[scan.scan_number]
        assert row.time == EPICS_EPOCH + timedelta(seconds=row.time_epoch)
        assert row.scan_end_time == scan.log["scanEnd_time"]
