"""Keep photo processing at full speed when Grainy is not the foreground window.

Windows applies EcoQoS (efficiency cores, lower clocks) to processes it considers background.
Measured on an i9-12900K: repeated 12 MP TIFF exports slowed from ~0.57 s to ~1.1 s after a few
seconds. Opting out only affects threads that are running; an idle app uses no CPU either way.
"""
import ctypes
import os
import sys

PROCESS_POWER_THROTTLING = 4             # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
EXECUTION_SPEED = 0x1                    # PROCESS_POWER_THROTTLING_EXECUTION_SPEED


class _State(ctypes.Structure):
    _fields_ = [('Version', ctypes.c_ulong), ('ControlMask', ctypes.c_ulong), ('StateMask', ctypes.c_ulong)]

_mac_activity = None


def _mac_begin_activity():
    """macOS App Nap slows a hidden app's timers and threads; an activity that runs for the life of the
    process opts out. Display and system sleep stay allowed (NSActivityUserInitiatedAllowingIdleSystemSleep)."""
    global _mac_activity
    if _mac_activity is not None:
        return True
    try:
        objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
        objc.objc_getClass.restype = ctypes.c_void_p; objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p; objc.sel_registerName.argtypes = [ctypes.c_char_p]
        def send(result, *types):
            call = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(result, ctypes.c_void_p, ctypes.c_void_p, *types))
            return lambda target, name, *values: call(target, objc.sel_registerName(name), *values)
        text = send(ctypes.c_void_p, ctypes.c_char_p)(objc.objc_getClass(b'NSString'), b'stringWithUTF8String:', b'Photo processing')
        process = send(ctypes.c_void_p)(objc.objc_getClass(b'NSProcessInfo'), b'processInfo')
        activity = send(ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)(process, b'beginActivityWithOptions:reason:', 0x00FFFFFF, text)
        if not activity:
            return False
        _mac_activity = send(ctypes.c_void_p)(activity, b'retain')     # kept until the process ends
        return True
    except (OSError, AttributeError):
        return False


def disable_background_throttling():
    """Request high QoS for this process; returns True when the system accepted it."""
    if sys.platform == 'darwin':
        return _mac_begin_activity()
    if os.name != 'nt':
        return False
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetProcessInformation.restype = ctypes.c_int
        state = _State(1, EXECUTION_SPEED, 0)  # control execution speed, state off = never throttle
        return bool(kernel.SetProcessInformation(kernel.GetCurrentProcess(), PROCESS_POWER_THROTTLING,
                                                 ctypes.byref(state), ctypes.sizeof(state)))
    except (OSError, AttributeError):
        return False
