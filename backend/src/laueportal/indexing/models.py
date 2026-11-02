"""Indexing runs (``indexing_run``), LaueGo parameters (``lauego_indexing_parameters``) and results conversions
(``conversion``).

The table, column and attribute names are v1's (``dde1bde``); ``created_at`` is UTC since revision 0005, revision
0006 adds ``indexing_run.selected_xml`` and revision 0007 ``conversion``.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from laueportal.core.db import Base
from laueportal.core.timeutil import UTCDateTime

if TYPE_CHECKING:
    from laueportal.jobs.models import Job
    from laueportal.reconstruction.models import ReconstructionRun
    from laueportal.scans.models import Metadata


INDEXING_METHODS = ("lauego", "laue_matching")
"""Allowed ``indexing_run.method`` values (``ck_indexing_run_method``). Adding one takes a migration that
rebuilds the table with the new CHECK constraint (as ``0004_drop_calib_and_ca`` does for reconstructions)."""


class IndexingRun(Base):
    """One indexing operation, optionally based on a reconstruction."""

    __tablename__ = "indexing_run"
    __table_args__ = (
        CheckConstraint(f"method IN ({', '.join(repr(method) for method in INDEXING_METHODS)})", name="method"),
        Index("ix_indexing_run_scan_number", "scan_number"),
        Index("ix_indexing_run_reconstruction_id", "reconstruction_id"),
        Index("ix_indexing_run_method", "method"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_number: Mapped[int | None] = mapped_column(ForeignKey("metadata.scan_number"), nullable=True)
    reconstruction_id: Mapped[int | None] = mapped_column(ForeignKey("reconstruction_run.id"), nullable=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.job_id"), unique=True)
    method: Mapped[str] = mapped_column(String, nullable=False)
    input_path: Mapped[str] = mapped_column(String, nullable=False)
    output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    algorithm_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # Frames whose processing succeeded and found at least one pattern. NULL means
    # unknown (historical runs whose XML has not been converted). Distinct from
    # Job.n_succeeded, which counts frames processed without error.
    n_frames_indexed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Explicit reference to the run's validated indexing-results HDF5 file (native output or a
    # conversion of the historical XML). Set only after the file was published and validated.
    results_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # The Output XML a person chose for the results viewer (POST .../artifact/select-xml, revision 0006) when the
    # run directory holds several and none is recorded: its file name in the run directory. Not a parameter.
    selected_xml: Mapped[str | None] = mapped_column(String, nullable=True)

    scan: Mapped["Metadata | None"] = relationship(back_populates="indexing_runs")
    reconstruction: Mapped["ReconstructionRun | None"] = relationship(back_populates="indexing_runs")
    job: Mapped["Job"] = relationship(back_populates="indexing_run")
    lauego_parameters: Mapped["LaueGoIndexingParameters | None"] = relationship(
        back_populates="indexing",
        cascade="all, delete-orphan",
        single_parent=True,
        uselist=False,
    )

    def __repr__(self) -> str:
        return f"Indexing I{self.id} ({self.method})"


class LaueGoIndexingParameters(Base):
    """Configuration used by a LaueGo indexing run."""

    __tablename__ = "lauego_indexing_parameters"

    indexing_id: Mapped[int] = mapped_column(ForeignKey("indexing_run.id", ondelete="CASCADE"), primary_key=True)
    filename_prefixes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A float (lauelab's type) in the baseline's INTEGER column: SQLite keeps 4.5 as REAL there, so no migration.
    threshold_ratio: Mapped[float | None] = mapped_column(Integer, nullable=True)
    max_rfactor: Mapped[float] = mapped_column(Float, nullable=False)
    box_size: Mapped[int] = mapped_column(Integer, nullable=False)
    max_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_separation: Mapped[int] = mapped_column(Integer, nullable=False)
    peak_shape: Mapped[str] = mapped_column(String, nullable=False)
    scan_points: Mapped[str] = mapped_column(String, nullable=False)
    scan_points_len: Mapped[int] = mapped_column(Integer, nullable=False)
    depth_range: Mapped[str | None] = mapped_column(String, nullable=True)
    depth_range_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detector_crop_x1: Mapped[int] = mapped_column(Integer, nullable=False)
    detector_crop_x2: Mapped[int] = mapped_column(Integer, nullable=False)
    detector_crop_y1: Mapped[int] = mapped_column(Integer, nullable=False)
    detector_crop_y2: Mapped[int] = mapped_column(Integer, nullable=False)
    min_size: Mapped[float] = mapped_column(Float, nullable=False)
    max_peaks: Mapped[int] = mapped_column(Integer, nullable=False)
    smooth: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mask_file: Mapped[str | None] = mapped_column(String, nullable=True)
    index_kev_max_calc: Mapped[float] = mapped_column(Float, nullable=False)
    index_kev_max_test: Mapped[float] = mapped_column(Float, nullable=False)
    index_angle_tolerance: Mapped[float] = mapped_column(Float, nullable=False)
    index_h: Mapped[int] = mapped_column(Integer, nullable=False)
    index_k: Mapped[int] = mapped_column(Integer, nullable=False)
    index_l: Mapped[int] = mapped_column(Integer, nullable=False)
    index_cone: Mapped[float] = mapped_column(Float, nullable=False)
    energy_unit: Mapped[str] = mapped_column(String, nullable=False)
    exposure_unit: Mapped[str] = mapped_column(String, nullable=False)
    cosmic_filter: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reciprocal_lattice_unit: Mapped[str] = mapped_column(String, nullable=False)
    lattice_parameters_unit: Mapped[str] = mapped_column(String, nullable=False)
    output_xml: Mapped[str | None] = mapped_column(String, nullable=True)
    geometry_file: Mapped[str] = mapped_column(String, nullable=False)
    crystal_file: Mapped[str] = mapped_column(String, nullable=False)
    depth: Mapped[str | None] = mapped_column(String, nullable=True)
    beamline: Mapped[str] = mapped_column(String, nullable=False)

    indexing: Mapped[IndexingRun] = relationship(back_populates="lauego_parameters")


class Conversion(Base):
    """A run's results conversion to ``output.h5`` (revision 0007): one row
    per indexing run, replaced by a new conversion, still no ``job`` row. Status ``queued``, ``running``,
    ``finished`` or ``failed``; the job process the worker's dispatcher started (host, pid, start in seconds since
    boot). The worker's dispatcher takes the queued ones (``indexing.conversion``, ``jobs.dispatch``)."""

    __tablename__ = "conversion"

    indexing_id: Mapped[int] = mapped_column(ForeignKey("indexing_run.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    message: Mapped[str | None] = mapped_column(String, nullable=True)
    process_host: Mapped[str | None] = mapped_column(String, nullable=True)
    process_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_since_boot: Mapped[float | None] = mapped_column(Float, nullable=True)


# The relationships above name other domains' models by string. Importing their modules here, at the
# end (the domain model modules import each other in a cycle), means that importing any one domain's
# models registers every mapper, so the first ORM use in any process can configure them.
# Nothing has to import laueportal.models first.
import laueportal.jobs.models  # noqa: E402, F401
import laueportal.reconstruction.models  # noqa: E402, F401
import laueportal.scans.models  # noqa: E402, F401
