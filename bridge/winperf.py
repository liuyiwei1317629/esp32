"""Keep this process out of Windows' background efficiency mode.

Windows 11 marks processes it considers background work with EcoQoS and then
prefers to schedule their threads on the E-cores. Measured on this laptop
(2026-10-27, same sentence, same process context): CosyVoice RTF 1.8 with the
throttle bit set, 0.8 with it cleared. At 1.8 TTS cannot keep up with playback,
so long replies stutter and anything over ~5 s trips COSYVOICE_TIMEOUT and
silently switches voice mid-reply.
"""

import ctypes
import sys

_PROCESS_POWER_THROTTLING = 4
_EXECUTION_SPEED = 0x1
_CURRENT_VERSION = 1


class _ThrottlingState(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong)]


def unthrottle():
    """Clear the execution-speed throttle bit for this process.

    Returns True when the setting was applied. A False result is not fatal:
    the caller keeps running, just possibly at half speed.
    """
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_void_p, ctypes.c_ulong]
        state = _ThrottlingState(_CURRENT_VERSION, _EXECUTION_SPEED, 0)
        ok = k32.SetProcessInformation(k32.GetCurrentProcess(),
                                       _PROCESS_POWER_THROTTLING,
                                       ctypes.byref(state),
                                       ctypes.sizeof(state))
        return bool(ok)
    except (AttributeError, OSError):
        return False
