"""Typed failures for the slicedoc layer.

These are *construction-* and *validation-time* errors. The whole point of
this package is that a `.gcode.3mf` whose AMS binding is internally
inconsistent (the §6.3 "printed air" failure) cannot be produced silently —
it raises here, before a single byte reaches the printer.
"""

from __future__ import annotations


class SlicedocError(ValueError):
    """Base: something about the print document is wrong."""


class SliceConsistencyError(SlicedocError):
    """The AMS binding is internally inconsistent (the §6.3 trap).

    Raised when filament arity, ``ams_mapping`` arity, the ``slice_info``
    filament records, and the gcode's ``M620`` tray selectors do not all
    resolve to the same physical tray(s).
    """


class TemperatureEnvelopeError(SlicedocError):
    """A gcode temperature command exceeds the safe physical envelope.

    Aragorn gates 4/5 — physical-safety. Nozzle > 280 °C or bed > 120 °C is
    refused regardless of what the slice claims.
    """


class ContainerError(SlicedocError):
    """The assembled (or supplied) ``.gcode.3mf`` is structurally invalid."""
