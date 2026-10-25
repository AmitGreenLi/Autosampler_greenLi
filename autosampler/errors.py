"""Domain-specific errors. Errors are never silently swallowed by the backend."""


class AutosamplerError(Exception):
    """Base class for operator-facing autosampler failures."""


class ConnectionFailure(AutosamplerError):
    pass


class SerialPermissionError(ConnectionFailure):
    pass


class DeviceNotFound(ConnectionFailure):
    pass


class AmbiguousDevice(ConnectionFailure):
    pass


class ControllerError(AutosamplerError):
    pass


class ControllerAlarm(ControllerError):
    pass


class SerialTimeout(ControllerError):
    pass


class RecoveryRequired(AutosamplerError):
    pass


class MissingCalibration(AutosamplerError):
    pass


class ValidationError(AutosamplerError):
    pass


class ScaleError(AutosamplerError):
    pass


class ScaleUnstable(ScaleError):
    pass


class OperationAborted(AutosamplerError):
    pass


class CommandCancelled(AutosamplerError):
    """A queued action could not run because an earlier machine action failed."""


class CorrectionFailure(AutosamplerError):
    pass
