"""A pipe-only child process whose descendants cannot outlive it or Grainy.

Windows starts suspended, assigns a kill-on-close Job Object, then resumes. This
avoids the race in taskkill /T when a launcher exits before its node/bridge child.
"""
import os
import signal
import subprocess
import threading


class ProcessTree:
    def __init__(self, argv, cwd, env=None):
        self.lock = threading.Lock()
        self.job = None
        options = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
            kernel.CreateJobObjectW.restype = w.HANDLE
            kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
            kernel.CloseHandle.argtypes = [w.HANDLE]

            class Basic(ctypes.Structure):
                _fields_ = [('per_process', ctypes.c_int64), ('per_job', ctypes.c_int64),
                            ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                            ('active', w.DWORD), ('affinity', ctypes.c_size_t),
                            ('priority', w.DWORD), ('scheduling', w.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                            ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                            ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

            self.kernel = kernel
            self.job = kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise OSError('Could not create the AI process job')
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            try:
                if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                    raise OSError('Could not contain the AI process')
                self.process = subprocess.Popen(argv, creationflags=0x08000000 | 0x00000004, **options)
                if not kernel.AssignProcessToJobObject(self.job, int(self.process._handle)):
                    self.process.kill()
                    self.process.wait()
                    raise OSError('Could not contain the AI process tree')
                resume = ctypes.WinDLL('ntdll').NtResumeProcess
                resume.argtypes = [w.HANDLE]
                resume.restype = ctypes.c_long
                if resume(int(self.process._handle)) != 0:
                    self.stop()
                    raise OSError('Could not start the AI process')
            except BaseException:
                self.close()
                raise
        else:
            self.process = subprocess.Popen(argv, start_new_session=True, **options)

    def stop(self):
        with self.lock:
            if os.name == 'nt':
                if self.job:
                    self.kernel.TerminateJobObject(self.job, 1)
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def close(self):
        self.stop()
        with self.lock:
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
        if hasattr(self, 'process'):
            self.process.wait(timeout=10)
            for pipe in (self.process.stdout, self.process.stderr):
                pipe.close()
