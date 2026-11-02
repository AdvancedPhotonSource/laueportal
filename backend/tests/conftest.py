"""
Shared pytest fixtures and utilities for the Laue Portal test suite.

This module provides reusable database fixtures and entity factories
to reduce code duplication across test files. Databases are real temporary
SQLite files built from each test's own settings (``tests.settings_support``).
"""

import datetime
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from laueportal import models
from tests.settings_support import make_database

CHECKOUT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True, scope="session")
def git_without_user_config() -> Iterator[None]:
    """Git, run by a test or by a script or process a test starts, reads no user or system configuration: commit
    signing, an ignore or attributes file or ``core.whitespace`` would change what it does. When no configuration sets
    ``core.excludesFile`` or ``core.attributesFile``, Git reads the files in ``$XDG_CONFIG_HOME/git`` (or
    ``~/.config/git``), so both name an empty file here."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        patch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        files = ("core.excludesFile", "core.attributesFile")
        patch.setenv("GIT_CONFIG_COUNT", str(len(files)))
        for index, key in enumerate(files):
            patch.setenv(f"GIT_CONFIG_KEY_{index}", key)
            patch.setenv(f"GIT_CONFIG_VALUE_{index}", os.devnull)
        yield


@pytest.fixture
def outside_checkout(tmp_path: Path) -> Iterator[Path]:
    """``tmp_path``, or a new folder in /tmp while ``tmp_path`` is in the checkout (the default workspace,
    ``.workspace``, holds TMPDIR): the deploy renderers refuse to write in the checkout."""
    if not tmp_path.resolve().is_relative_to(CHECKOUT):
        yield tmp_path
        return
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="laueportal-test-") as folder:
        yield Path(folder)


def create_test_metadata(scan_number: int = 1) -> Any:
    """
    Factory function to create a test Metadata record.

    Args:
        scan_number: The scan number for the metadata record

    Returns:
        models.Metadata: A test metadata record
    """
    return models.Metadata(
        scan_number=scan_number,
        time_epoch=1640995200,
        time=datetime.datetime(2022, 1, 1, 0, 0, 0, tzinfo=datetime.UTC),
        user_name="test_user",
        source_beam_bad="false",
        source_ccd_shutter="open",
        source_mono_trans_status="ok",
        source_energy_unit="keV",
        source_energy=10.0,
        source_id_gap_unit="mm",
        source_id_gap=5.0,
        source_id_taper_unit="mm",
        source_id_taper=0.0,
        source_ring_current_unit="mA",
        source_ring_current=100.0,
        sample_xyz_unit="mm",
        sample_xyz_desc="Sample position",
        sample_xyz="0,0,0",
        knife_edge_xyz_unit="mm",
        knife_edge_xyz_desc="Knife edge position",
        knife_edge_xyz="0,0,0",
        knife_edge_knife_scan_unit="mm",
        knife_edge_knife_scan=1.0,
        mda_file="test.mda",
        scan_end_abort="false",
        scan_end_time_epoch=1640995300,
        scan_end_time="2022-01-01T00:01:40",
        scan_end_scan_duration_unit="s",
        scan_end_scan_duration=100.0,
        scan_end_source_beam_bad="false",
        scan_end_source_ring_current_unit="mA",
        scan_end_source_ring_current=100.0,
    )


def create_test_reconstruction_run(scan_number: int | None = 1, job_id: int = 1) -> Any:
    """Create a unified wire reconstruction run for tests."""
    return models.ReconstructionRun(
        scan_number=scan_number,
        job_id=job_id,
        method="wire",
        input_path="/test/input",
        output_path="/test/output/rec_1",
        author="test_user",
        notes="test reconstruction",
        algorithm_version="test-version",
        created_at=datetime.datetime(2022, 1, 1, 0, 0, 0, tzinfo=datetime.UTC),
    )


def create_test_wire_reconstruction_parameters(reconstruction_id: int | None = None) -> Any:
    """Create wire-specific parameters for a unified reconstruction run."""
    values = {
        "filename_prefixes": ["test_*.h5"],
        "geometry_file": "/test/geometry.xml",
        "percent_brightest": 10.0,
        "wire_edges": "0 1",
        "depth_start": -10.0,
        "depth_end": 10.0,
        "depth_resolution": 0.5,
        "num_threads": 4,
        "memory_limit_mb": 1024,
        "scan_points": "1-10",
        "scan_points_len": 10,
        "verbose": 1,
    }
    if reconstruction_id is not None:
        values["reconstruction_id"] = reconstruction_id
    return models.WireReconstructionParameters(**values)


def create_test_indexing_run(
    scan_number: int | None = 1,
    job_id: int = 2,
    reconstruction_id: int | None = None,
) -> Any:
    """Create a unified LaueGo indexing run for tests."""
    return models.IndexingRun(
        scan_number=scan_number,
        reconstruction_id=reconstruction_id,
        job_id=job_id,
        method="lauego",
        input_path="/test/input",
        output_path="/test/output/index_1",
        author="test_user",
        notes="test indexing",
        algorithm_version="test-version",
        created_at=datetime.datetime(2022, 1, 1, 0, 0, 0, tzinfo=datetime.UTC),
    )


def create_test_lauego_parameters(indexing_id: int | None = None) -> Any:
    """Create LaueGo-specific parameters for a unified indexing run."""
    values = {
        "filename_prefixes": ["test_%d.h5"],
        "threshold": 250,
        "threshold_ratio": None,
        "max_rfactor": 0.5,
        "box_size": 18,
        "max_number": 300,
        "min_separation": 20,
        "peak_shape": "Lorentzian",
        "scan_points": "1-2",
        "scan_points_len": 2,
        "depth_range": None,
        "depth_range_len": None,
        "detector_crop_x1": 0,
        "detector_crop_x2": 2047,
        "detector_crop_y1": 0,
        "detector_crop_y2": 2047,
        "min_size": 3.0,
        "max_peaks": 200,
        "smooth": False,
        "mask_file": None,
        "index_kev_max_calc": 17.2,
        "index_kev_max_test": 35.0,
        "index_angle_tolerance": 0.1,
        "index_h": 0,
        "index_k": 0,
        "index_l": 1,
        "index_cone": 72.0,
        "energy_unit": "keV",
        "exposure_unit": "sec",
        "cosmic_filter": True,
        "reciprocal_lattice_unit": "1/nm",
        "lattice_parameters_unit": "nm",
        "output_xml": "output.xml",
        "geometry_file": "/test/geometry.xml",
        "crystal_file": "/test/crystal.xtal",
        "depth": None,
        "beamline": "34ID-E",
    }
    if indexing_id is not None:
        values["indexing_id"] = indexing_id
    return models.LaueGoIndexingParameters(**values)


def create_test_catalog(scan_number: int = 1) -> Any:
    """
    Factory function to create a test Catalog record.

    Args:
        scan_number: The scan number for the catalog record

    Returns:
        models.Catalog: A test catalog record
    """
    return models.Catalog(
        scan_number=scan_number,
        filefolder="/test/folder",
        filename_prefix="test_prefix",
        aperture="50um",
        sample_name="test_sample",
    )


def create_test_scan(scan_number: int = 1) -> Any:
    """
    Factory function to create a test Scan record.

    Args:
        scan_number: The scan number for the scan record

    Returns:
        models.Scan: A test scan record
    """
    return models.Scan(
        scan_number=scan_number,
        scan_dim=2,
        scan_npts=100,
        scan_after="true",
        scan_positioner1_pv="test:pos1",
        scan_positioner1_ar="true",
        scan_positioner1_mode="absolute",
        scan_positioner1="motor1",
        scan_positioner2_pv="test:pos2",
        scan_positioner2_ar="true",
        scan_positioner2_mode="absolute",
        scan_positioner2="motor2",
        scan_positioner3_pv="test:pos3",
        scan_positioner3_ar="false",
        scan_positioner3_mode="relative",
        scan_positioner3="motor3",
        scan_positioner4_pv="test:pos4",
        scan_positioner4_ar="false",
        scan_positioner4_mode="relative",
        scan_positioner4="motor4",
        scan_detector_trig1_pv="test:det1",
        scan_detector_trig1_val="1",
        scan_detector_trig2_pv="test:det2",
        scan_detector_trig2_val="1",
        scan_detector_trig3_pv="test:det3",
        scan_detector_trig3_val="0",
        scan_detector_trig4_pv="test:det4",
        scan_detector_trig4_val="0",
        scan_cpt=1000,
    )


def create_test_job(scan_number: int = 1) -> Any:
    """
    Factory function to create a test Job record.

    Args:
        scan_number: The scan number for the job record (used as job_id)

    Returns:
        models.Job: A test job record
    """
    return models.Job(
        job_id=scan_number,
        computer_name="TEST_COMPUTER",
        status=1,  # Running
        priority=5,
        submit_time=datetime.datetime(2022, 1, 1, 0, 0, 0, tzinfo=datetime.UTC),
        start_time=datetime.datetime(2022, 1, 1, 0, 1, 0, tzinfo=datetime.UTC),
        finish_time=datetime.datetime(2022, 1, 1, 0, 2, 0, tzinfo=datetime.UTC),
        messages="Test job for smoke test",
    )


def create_test_database_with_entities(
    tmp_path: Path, entities: list[str], scan_number: int = 1
) -> tuple[Any, str, list[Any]]:
    """
    Create a temporary database with specified entities.

    Args:
        tmp_path: Directory for the database file
        entities: Entity types to create (metadata, catalog, scan, or job)
        scan_number: The scan number to use for all entities

    Returns:
        tuple: (test_engine, test_db_file, [created_entities])
    """
    settings, test_engine = make_database(tmp_path)

    # Create requested entities
    created_entities = []
    entity_map = {
        "metadata": create_test_metadata,
        "catalog": create_test_catalog,
        "scan": create_test_scan,
        "job": create_test_job,
    }

    for entity_type in entities:
        if entity_type in entity_map:
            entity = entity_map[entity_type](scan_number)
            created_entities.append(entity)
        else:
            raise ValueError(f"Unknown entity type: {entity_type}")

    return test_engine, str(settings.database.path), created_entities


@pytest.fixture
def test_metadata_database(tmp_path):
    """
    Pytest fixture that creates a temporary database with metadata, scan, and catalog data.

    Returns:
        tuple: (test_engine, test_db_file, test_metadata, test_scan, test_catalog)
    """
    test_engine, test_db_file, entities = create_test_database_with_entities(tmp_path, ["metadata", "scan", "catalog"])

    try:
        yield test_engine, test_db_file, entities[0], entities[1], entities[2]
    finally:
        test_engine.dispose()


@pytest.fixture
def empty_test_database(tmp_path):
    """
    Pytest fixture that creates a temporary empty database (no test data).
    Compatible with all existing empty database tests.

    Returns:
        tuple: (test_engine, test_db_file)
    """
    settings, test_engine = make_database(tmp_path)
    try:
        yield test_engine, str(settings.database.path)
    finally:
        test_engine.dispose()


# Alias fixtures for backward compatibility with different names used in test files
empty_metadata_database = empty_test_database

# Session-scoped visualization fixtures (a native results file from the synthetic frames), ``live`` (a database
# and a worker settings file) and ``gc_disabled`` (the timing tests of the file probe).
pytest_plugins = ["tests.viz_support", "tests.worker_support", "tests.probe_support"]
