"""Wire reconstruction constants shared by the parameter model and the compute function.
Standard library only: the compute side imports it, and a compute node has no pydantic.
"""

DEFAULT_SKIP = 1
"""Leading stored slices (``frame_skip``) and wire-vector entries (``wire_skip``) that reconstruction skips:
``MULTI_IMAGE_SKIP`` and ``MULTI_IMAGE_SKIPV`` of reconstructN_cpu."""
