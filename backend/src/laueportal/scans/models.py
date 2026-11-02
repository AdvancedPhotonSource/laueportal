"""Scan tables: ``metadata`` (one row per scan, from the scan-log XML), ``scan`` (one row per scan
dimension) and ``catalog`` (what the scan log does not record: data folder, filename prefixes, aperture).

The table names are v1's (``dde1bde``); the columns are snake_case since revision 0003 (v1's camelCase
names, e.g. ``scanNumber``, are listed there). ``metadata.time`` is UTC since 0005; ``scan_end_time`` is
the scan log's text, as written (local time).
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from laueportal.core.db import Base
from laueportal.core.timeutil import UTCDateTime

if TYPE_CHECKING:
    from laueportal.indexing.models import IndexingRun
    from laueportal.reconstruction.models import ReconstructionRun


class Metadata(Base):
    __tablename__ = "metadata"

    scan_number: Mapped[int] = mapped_column(primary_key=True)

    time_epoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time: Mapped[datetime] = mapped_column(UTCDateTime)  # scan start (the log's local time, converted)
    user_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    source_beam_bad: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # bool?
    source_ccd_shutter: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # bool?
    source_mono_trans_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # bool?
    source_energy_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_energy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_id_gap_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_id_gap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_id_taper_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_id_taper: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_ring_current_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_ring_current: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    sample_xyz_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sample_xyz_desc: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sample_xyz: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    knife_edge_xyz_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    knife_edge_xyz_desc: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    knife_edge_xyz: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    knife_edge_knife_scan_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    knife_edge_knife_scan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # scan:

    mda_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    scan_end_abort: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # bool?
    scan_end_time_epoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scan_end_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # the log's local-time text
    scan_end_scan_duration_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scan_end_scan_duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scan_end_source_beam_bad: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # bool?
    scan_end_source_ring_current_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scan_end_source_ring_current: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    motor_group_sample_npts_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    motor_group_sample_cpt_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    motor_group_energy_npts_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    motor_group_energy_cpt_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    motor_group_depth_npts_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    motor_group_depth_cpt_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    motor_group_other_npts_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    motor_group_other_cpt_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Parent of:
    scan_: Mapped["Scan"] = relationship(backref="metadata")
    catalog_: Mapped["Catalog"] = relationship(backref="metadata")
    reconstruction_runs: Mapped[list["ReconstructionRun"]] = relationship(back_populates="scan")
    indexing_runs: Mapped[list["IndexingRun"]] = relationship(back_populates="scan")


# Table of scan PV parameters extracted from the MDA scan log XML.
class Scan(Base):
    __tablename__ = "scan"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_number: Mapped[int] = mapped_column(ForeignKey("metadata.scan_number"))

    scan_dim: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scan_npts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scan_after: Mapped[str] = mapped_column(String)  # bool?
    scan_positioner1_pv: Mapped[str] = mapped_column(String)
    scan_positioner1_ar: Mapped[str] = mapped_column(String)  # bool?
    scan_positioner1_mode: Mapped[str] = mapped_column(String)
    scan_positioner1: Mapped[str] = mapped_column(String)
    scan_positioner2_pv: Mapped[str] = mapped_column(String)
    scan_positioner2_ar: Mapped[str] = mapped_column(String)  # bool?
    scan_positioner2_mode: Mapped[str] = mapped_column(String)
    scan_positioner2: Mapped[str] = mapped_column(String)
    scan_positioner3_pv: Mapped[str] = mapped_column(String)
    scan_positioner3_ar: Mapped[str] = mapped_column(String)  # bool?
    scan_positioner3_mode: Mapped[str] = mapped_column(String)
    scan_positioner3: Mapped[str] = mapped_column(String)
    scan_positioner4_pv: Mapped[str] = mapped_column(String)
    scan_positioner4_ar: Mapped[str] = mapped_column(String)  # bool?
    scan_positioner4_mode: Mapped[str] = mapped_column(String)
    scan_positioner4: Mapped[str] = mapped_column(String)
    scan_detector_trig1_pv: Mapped[str] = mapped_column(String)
    scan_detector_trig1_val: Mapped[str] = mapped_column(String)  # int?
    scan_detector_trig2_pv: Mapped[str] = mapped_column(String)
    scan_detector_trig2_val: Mapped[str] = mapped_column(String)  # int?
    scan_detector_trig3_pv: Mapped[str] = mapped_column(String)
    scan_detector_trig3_val: Mapped[str] = mapped_column(String)  # int?
    scan_detector_trig4_pv: Mapped[str] = mapped_column(String)
    scan_detector_trig4_val: Mapped[str] = mapped_column(String)  # int?
    scan_cpt: Mapped[int] = mapped_column(Integer)


# Scan metadata not contained in the scan XML log.
class Catalog(Base):
    __tablename__ = "catalog"

    catalog_id: Mapped[int] = mapped_column(primary_key=True)
    scan_number: Mapped[int] = mapped_column(ForeignKey("metadata.scan_number"), unique=True)

    filefolder: Mapped[str] = mapped_column(String)  # infile
    filename_prefix: Mapped[list[str]] = mapped_column(JSON)  # infile

    aperture: Mapped[str] = mapped_column(String)
    sample_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)


# The relationships above name other domains' models by string. Importing their modules here, at the
# end (the domain model modules import each other in a cycle), means that importing any one domain's
# models registers every mapper, so the first ORM use in any process can configure them.
# Nothing has to import laueportal.models first.
import laueportal.indexing.models  # noqa: E402, F401
import laueportal.reconstruction.models  # noqa: E402, F401
