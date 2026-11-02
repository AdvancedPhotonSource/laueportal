"""Remote execution: the DM/Globus/PBS integration.

- Shared with the compute side: ``paths`` (path mapping and containment).
- Shared by the portal side and the launcher side: ``observation`` (a ``crux`` run's remote observation, its
  format and checks; standard library only).
- Launcher side (the DM host; YAML settings only, no database, no portal settings, import-linter rule 3):
  ``settings`` (``bdp.yaml``), ``plan`` (``bdp.json``), ``transport``, ``stage`` (the DM stage commands),
  ``flow`` (the ``crux`` Globus flow), ``pbs`` (the PBS script and Globus Compute's PBS task), ``auth``.
- Portal side: ``dm`` (the DM API client), ``submission`` (DM submission), ``poller`` (reconciliation and
  publication: the worker's remote poller thread, and ``laueportal remote poll --once``), ``cleanup`` (what
  published runs left behind), ``estimates`` (runtime estimates per execution target, from measured profiles).

Importing this package imports nothing else.
"""
