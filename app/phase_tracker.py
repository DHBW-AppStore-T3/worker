"""Progress phases of a job, for the live stepper in the UI."""

from typing import Any


class PhaseTracker:
    """Drives ``StructuredLogger.progress`` calls.

    The set of phases is fixed at construction time so the percent bar
    monotonically advances; ``mark()`` looks up the index of the named
    phase and sends a progress event with the correct ``idx/total``.
    """

    def __init__(self, logger: Any, phases: tuple[str, ...]):
        self._logger = logger
        self._phases = phases
        self._index_by_name = {name: i for i, name in enumerate(phases, start=1)}

    @property
    def total(self) -> int:
        return len(self._phases)

    def mark(self, phase_name: str, message: str = "") -> None:
        idx = self._index_by_name.get(phase_name)
        if idx is None:
            # Unknown phase — emit a transcript marker but no progress
            # update so the bar doesn't reset.
            self._logger.phase(phase_name)
            return
        # Buffer the readable phase header and emit the live progress event.
        # The full phase-name sequence rides on every event so the UI can
        # render each stepper slot with its real label immediately.
        self._logger.phase(phase_name)
        self._logger.progress(
            phase_name,
            idx,
            self.total,
            message,
            phase_names=self._phases,
        )
