"""PBS scripts use the site's MPI launcher; the Globus task only submits and polls.

:func:`build_script` writes the PBS script: ``python -m laueportal.jobs.runner <control directory>``, or
``laueportal.jobs.mpi_runner`` when ``pbs.nodes x indexing_ranks_per_node > 1``; the target's ``code_directory`` is then
a checkout's ``backend/src``. A continued run (a ``pioneer`` plan's ``continuation``) has one script per allocation,
named so that the scheduler can be asked for it (:func:`allocation_name`). :func:`run_pbs_job` is the Globus Compute function of the ``crux`` flow (it runs on the
login node, so it imports nothing at module level). :func:`fallback` is the summary the task writes when PBS ended
without the runner's. Standard library and ``jobs.backends`` only (import-linter rule 3); :func:`run_pbs_job` uses
neither, so the endpoint never imports this package.
"""

from __future__ import annotations

import re
import shlex

from laueportal.jobs.backends import GLOBUS, transport

DEADLINE_MARGIN_SECONDS = 300
"""What a soft deadline leaves of the walltime: the frame in progress, the merge of the segments and the summary."""
RUNNER_MODULE = "laueportal.jobs.runner"
"""The standalone runner."""
MPI_RUNNER_MODULE = "laueportal.jobs.mpi_runner"
"""The MPI runner."""


def allocation_name(plan, sequence: int) -> str:
    """The PBS job name of a continued run's allocation ``sequence`` (``L<submission hex>-<sequence>``): unique to the
    allocation, so ``qselect -x -N`` finds the job a lost ``qsub`` reply did not name (``remote.stage``)."""
    return f"L{plan['submission_id'].replace('-', '')}-{int(sequence)}"


def runner_command(plan, sequence: int | None = None) -> list[str]:
    """The runner's command line: ``python -m <runner> <control directory>``, and ``--allocation <sequence>`` for a
    continued run's allocation."""
    command = [plan["target"]["analysis_python"], "-m", RUNNER_MODULE, plan["control_directory"]]
    return command if sequence is None else [*command, "--allocation", str(int(sequence))]


def build_script(plan, allocation: int | None = None, *, finalize: bool = False):
    """The PBS script of ``plan``; for a continued run (the plan's ``continuation``), of its allocation
    ``allocation`` (1 when not given): named by :func:`allocation_name`, not rerun by PBS after a node failure (the
    launcher decides what follows an allocation that ended without its result: ``remote.stage``), and running the
    runner with ``--allocation``. ``finalize``: a continued run's finalizing allocation, which only merges and
    publishes the committed work: it asks for the target's ``finalize_pbs`` (a CPU request, no GPU; required, never
    the GPU request instead) and has no soft deadline. The launcher submits one only after a stop."""
    target, pbs = plan["target"], plan["pbs"]
    continued = plan.get("continuation") is not None
    if finalize:
        if not continued:
            raise ValueError("Only a continued run has finalizing allocations")
        pbs = target.get("finalize_pbs")
        if not pbs:
            raise ValueError("The target has no finalize_pbs: a finalizing allocation never asks for the GPU queue")
        if int(pbs.get("ngpus") or 0) != 0:
            raise ValueError("A finalizing allocation asks for no GPU (finalize_pbs.ngpus 0 or absent)")
    sequence = (allocation or 1) if continued else None
    globus = transport(plan["backend"]) == GLOBUS
    nodes = int(pbs.get("nodes", 1))
    if nodes < 1 or (not globus and nodes != 1):
        raise ValueError("Multiple nodes are supported only by the Crux MPI runner")
    ranks_per_node = (
        int(plan["execution"].get("indexing_ranks_per_node", 1)) if plan["kind"] == "lauego_indexing" else 1
    )
    if ranks_per_node < 1 or ranks_per_node > 4:
        raise ValueError("Use between one and four indexing ranks per node")
    for key in ("queue", "walltime"):
        if not re.fullmatch(r"[A-Za-z0-9_:@.+-]+", str(pbs[key])):
            raise ValueError(f"Unsafe PBS {key}")
    if pbs.get("mem") is not None and not re.fullmatch(r"[0-9]+[kmgt]?b", str(pbs["mem"]), re.IGNORECASE):
        raise ValueError("Unsafe PBS mem (write it as PBS does, such as 64gb)")
    gpus = int(pbs.get("ngpus") or 0)
    if gpus < 0:
        raise ValueError("PBS ngpus must not be negative")
    deadline = None if finalize else plan["execution"].get("soft_deadline_seconds")
    if deadline is not None and float(deadline) + DEADLINE_MARGIN_SECONDS > walltime_seconds(pbs["walltime"]):
        raise ValueError(
            f"execution.soft_deadline_seconds must leave {DEADLINE_MARGIN_SECONDS} s of the walltime for the merge "
            "and the run summary"
        )
    account = pbs.get("account")
    if globus and not account:
        raise ValueError("An ALCF allocation is required")
    if account and not re.fullmatch(r"[A-Za-z0-9_:@.+-]+", str(account)):
        raise ValueError("Unsafe PBS account")
    module = MPI_RUNNER_MODULE if nodes * ranks_per_node > 1 else RUNNER_MODULE
    if continued and (globus or module != RUNNER_MODULE):
        raise ValueError("Only a shared-storage run with one runner process continues over allocations")
    command = (
        runner_command(plan, sequence)
        if continued
        else [target["analysis_python"], "-m", module, plan["control_directory"]]
    )
    lines = [
        "#!/bin/bash -l",
        f"#PBS -N {allocation_name(plan, sequence)}" if sequence is not None else "#PBS -N Laue",
        *(["#PBS -r n"] if continued else []),
        *([f"#PBS -A {account}"] if account else []),
        f"#PBS -q {pbs['queue']}",
        f"#PBS -l walltime={pbs['walltime']}",
        "#PBS -j oe",
    ]
    if globus:
        lines += [f"#PBS -l select={nodes}:system=crux", "#PBS -l place=scatter", "#PBS -l filesystems=home:eagle"]
    else:
        memory = f":mem={pbs['mem']}" if pbs.get("mem") is not None else ""
        lines += [f"#PBS -l select=1:ncpus={int(pbs.get('cpus', 32))}{memory}:ngpus={gpus}"]
    lines += [
        "set -euo pipefail",
        ': "${PBS_JOBID:?Compute allocation required}"',
        "export OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 OMP_NUM_THREADS=1",
        f"export PYTHONPATH={shlex.quote(target['code_directory'])}",
        f"cd {shlex.quote(plan['control_directory'])}",
    ]
    lines += list(target.get("module_commands", []))  # Administrator configuration, never form input.
    if globus:
        # Crux's launcher must start bash, not relocate the Conda Python executable.
        launcher = [
            "mpiexec",
            "-n",
            str(nodes * ranks_per_node),
            "--ppn",
            str(ranks_per_node),
            "--cpu-bind",
            "none",
            "/bin/bash",
            "-c",
            'exec "$@"',
            "bash",
            *command,
        ]
    else:
        launcher = command
    lines.append("exec " + shlex.join(launcher))
    return "\n".join(lines) + "\n"


def walltime_seconds(value) -> float:
    """A PBS walltime (``HH:MM:SS``, ``MM:SS`` or seconds) in seconds; ``ValueError`` for anything else."""
    parts = str(value).split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", part) for part in parts):
        raise ValueError("Unreadable PBS walltime")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def run_pbs_job(*, job_directory, script, fallback_summary, cancel_file, **unused):
    """Serializable, application-independent function for the login-node endpoint.

    The task does no data processing. It submits a fixed PBS script and waits
    for the scheduler, honoring a small cancellation file while queued or running.
    """
    import json
    import pathlib
    import re
    import subprocess
    import time

    root = pathlib.Path(job_directory)
    output = pathlib.Path(fallback_summary["output_directory"])
    output.mkdir(parents=True, exist_ok=True)
    control = root / "control"
    script_path = control / "job.pbs"
    state_path = output / "pbs.json"
    job_id = None
    last_status = None
    submitting = False

    def save(path, document):
        partial = path.with_name(path.name + ".tmp")
        partial.write_text(json.dumps(document))
        partial.replace(path)

    try:
        with open(script_path, "x", encoding="utf-8") as handle:
            handle.write(script)
        submitting = True
        response = subprocess.run(
            ["/opt/pbs/bin/qsub", "-o", str(output / "pbs-output.log"), str(script_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        submitting = False
        job_id = response.stdout.strip()
        if not re.fullmatch(r"[0-9]+(?:\.[A-Za-z0-9_.-]+)?", job_id):
            raise ValueError("PBS returned an invalid job ID")
        save(state_path, {"job_id": job_id, "status": "submitted"})
        cancel_sent = False
        deadline = time.monotonic() + 48 * 3600
        while time.monotonic() < deadline:
            if pathlib.Path(cancel_file).exists() and not cancel_sent:
                subprocess.run(["/opt/pbs/bin/qdel", job_id], check=False, capture_output=True, timeout=30)
                cancel_sent = True
            state = subprocess.run(
                ["/opt/pbs/bin/qstat", "-xf", "-F", "json", job_id], capture_output=True, text=True, timeout=30
            )
            if state.returncode:
                # Never infer success from a job disappearing from scheduler history.
                raise RuntimeError(f"PBS status unavailable for {job_id}: {state.stderr[-1000:]}")
            jobs = json.loads(state.stdout)["Jobs"]
            record = next(iter(jobs.values()))
            last_status = record["job_state"]
            save(
                state_path, {"job_id": job_id, "status": record["job_state"], "exit_status": record.get("Exit_status")}
            )
            if record["job_state"] == "F":
                if not (output / "run.json").exists():
                    raise RuntimeError(
                        f"PBS job {job_id} ended with exit status {record.get('Exit_status')}; no run summary"
                    )
                return {"job_id": job_id, "exit_status": record.get("Exit_status")}
            time.sleep(5)
        subprocess.run(["/opt/pbs/bin/qdel", job_id], capture_output=True, timeout=30)
        raise TimeoutError("PBS monitoring exceeded 48 hours; cancellation was requested")
    except Exception as error:
        if isinstance(error, FileExistsError) or (submitting and isinstance(error, subprocess.TimeoutExpired)):
            save(state_path, {"status": "SUBMISSION_UNCERTAIN", "message": "Inspect PBS before any retry"})
            raise RuntimeError("PBS submission needs reconciliation; automatic resubmission is disabled") from error
        if job_id and last_status != "F":
            # Do not return a terminal summary while a previously submitted job
            # may still be writing. Request cancellation and confirm scheduler termination.
            subprocess.run(["/opt/pbs/bin/qdel", job_id], check=False, capture_output=True, timeout=30)
            confirmed = False
            for _ in range(12):
                response = subprocess.run(
                    ["/opt/pbs/bin/qstat", "-xf", "-F", "json", job_id], capture_output=True, text=True, timeout=30
                )
                if response.returncode == 0:
                    jobs = json.loads(response.stdout)["Jobs"]
                    if next(iter(jobs.values()))["job_state"] == "F":
                        confirmed = True
                        break
                time.sleep(5)
            if not confirmed:
                save(state_path, {"job_id": job_id, "status": "STATE_UNCERTAIN"})
                raise RuntimeError(f"Cannot confirm PBS job {job_id} stopped; inspect the scheduler") from error
        if not (output / "run.json").exists():
            summary = dict(fallback_summary["summary"])
            progress = output / "progress.json"
            if progress.exists():
                counts = json.loads(progress.read_text())
                succeeded, failed = counts.get("n_succeeded", 0), counts.get("n_failed", 0)
                if 0 <= succeeded + failed <= summary["counters"]["n_inputs"]:
                    summary["counters"] = {
                        **summary["counters"],
                        "n_succeeded": succeeded,
                        "n_failed": failed,
                        "n_not_run": summary["counters"]["n_inputs"] - succeeded - failed,
                    }
            summary["message"] = str(error)
            summary["status"] = "Cancelled" if pathlib.Path(cancel_file).exists() else "Failed"
            summary["phase"] = summary["status"].lower()
            # Keep the runner's final file exclusive, including races with job shutdown.
            try:
                with open(output / "run.json", "x", encoding="utf-8") as handle:
                    json.dump(summary, handle)
            except FileExistsError:
                pass
        return {"error": str(error)}  # Transfer diagnostics back even when PBS failed.


def fallback(plan):
    return {
        "output_directory": plan["output_directory"],
        "summary": {
            "format": "laue-portal-run-summary",
            "version": 1,
            "job_id": plan["job_id"],
            "submission_id": plan["submission_id"],
            "kind": plan["kind"],
            "status": "Failed",
            "phase": "failed",
            "counters": {
                "n_inputs": plan["n_inputs"],
                "n_succeeded": 0,
                "n_failed": 0,
                "n_not_run": plan["n_inputs"],
                "n_indexed": None,
            },
            "artifacts": {},
        },
    }
