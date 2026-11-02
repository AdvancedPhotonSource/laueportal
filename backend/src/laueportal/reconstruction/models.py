"""Reconstruction runs (``reconstruction_run``) and wire parameters (``wire_reconstruction_parameters``).

The table, column and attribute names are v1's (``dde1bde``); ``created_at`` is UTC since revision 0005.
Revision 0004 removed v1's ``'ca'`` (coded aperture) method.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, CheckConstraint, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from laueportal.core.db import Base
from laueportal.core.timeutil import UTCDateTime

if TYPE_CHECKING:
    from laueportal.indexing.models import IndexingRun
    from laueportal.jobs.models import Job
    from laueportal.scans.models import Metadata


RECONSTRUCTION_METHODS = ("wire",)
"""Allowed ``reconstruction_run.method`` values (``ck_reconstruction_run_method``).

To add a reconstruction kind: add its value here and write a migration that rebuilds ``reconstruction_run``
with the longer list (SQLite cannot alter a CHECK constraint; copy ``_rebuild`` from
``migrations/versions/0004_drop_calib_and_ca.py``). The head-versus-models test fails until both agree.
"""


class ReconstructionRun(Base):
    """One reconstruction operation, independent of reconstruction method."""

    __tablename__ = "reconstruction_run"
    __table_args__ = (
        CheckConstraint(f"method IN ({', '.join(repr(method) for method in RECONSTRUCTION_METHODS)})", name="method"),
        Index("ix_reconstruction_run_scan_number", "scan_number"),
        Index("ix_reconstruction_run_method", "method"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_number: Mapped[int | None] = mapped_column(ForeignKey("metadata.scan_number"), nullable=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.job_id"), unique=True)
    method: Mapped[str] = mapped_column(String, nullable=False)
    input_path: Mapped[str] = mapped_column(String, nullable=False)
    output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    algorithm_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    scan: Mapped["Metadata | None"] = relationship(back_populates="reconstruction_runs")
    job: Mapped["Job"] = relationship(back_populates="reconstruction_run")
    wire_parameters: Mapped["WireReconstructionParameters | None"] = relationship(
        back_populates="reconstruction",
        cascade="all, delete-orphan",
        single_parent=True,
        uselist=False,
    )
    indexing_runs: Mapped[list["IndexingRun"]] = relationship(back_populates="reconstruction")

    def __repr__(self) -> str:
        return f"Reconstruction R{self.id} ({self.method})"


class WireReconstructionParameters(Base):
    """Configuration used by a wire reconstruction run."""

    __tablename__ = "wire_reconstruction_parameters"

    reconstruction_id: Mapped[int] = mapped_column(
        ForeignKey("reconstruction_run.id", ondelete="CASCADE"), primary_key=True
    )
    filename_prefixes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    geometry_file: Mapped[str] = mapped_column(String, nullable=False)
    percent_brightest: Mapped[float] = mapped_column(Float, nullable=False)
    wire_edges: Mapped[str] = mapped_column(String, nullable=False)
    depth_start: Mapped[float] = mapped_column(Float, nullable=False)
    depth_end: Mapped[float] = mapped_column(Float, nullable=False)
    depth_resolution: Mapped[float] = mapped_column(Float, nullable=False)
    # Null for runs recorded before the skip options; those runs used DEFAULT_SKIP.
    frame_skip: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wire_skip: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_threads: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    scan_points: Mapped[str] = mapped_column(String, nullable=False)
    scan_points_len: Mapped[int] = mapped_column(Integer, nullable=False)
    verbose: Mapped[int] = mapped_column(Integer, nullable=False)

    reconstruction: Mapped[ReconstructionRun] = relationship(back_populates="wire_parameters")


# The relationships above name other domains' models by string. Importing their modules here, at the
# end (the domain model modules import each other in a cycle), means that importing any one domain's
# models registers every mapper, so the first ORM use in any process can configure them.
# Nothing has to import laueportal.models first.
import laueportal.indexing.models  # noqa: E402, F401
import laueportal.jobs.models  # noqa: E402, F401
import laueportal.scans.models  # noqa: E402, F401
