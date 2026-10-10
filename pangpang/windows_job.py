"""Windows process-tree ownership using a Job Object (no external dependency).

A gated Python child is assigned before it can launch the actual CLI. Descendants
inherit the job; the wrapper cannot race a CLI launch before assignment.
"""
import ctypes
from ctypes import wintypes


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(x, ctypes.c_ulonglong) for x in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]


class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [('BasicLimitInformation',BASIC_LIMIT),('IoInfo',IO_COUNTERS),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]


class ACCOUNTING(ctypes.Structure):
    _fields_ = [(x,ctypes.c_longlong) for x in ('TotalUserTime','TotalKernelTime','ThisPeriodTotalUserTime','ThisPeriodTotalKernelTime')] + [(x,wintypes.DWORD) for x in ('TotalPageFaultCount','TotalProcesses','ActiveProcesses','TotalTerminatedProcesses')]


class Job:
    def __init__(self):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype=wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.api.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]
        self.api.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
        self.api.CloseHandle.argtypes=[wintypes.HANDLE]
        self.handle=self.api.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        info=EXTENDED_LIMIT();info.BasicLimitInformation.LimitFlags=0x2000 # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    def assign(self,process):
        if not self.api.AssignProcessToJobObject(self.handle,int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    def alive(self):
        info=ACCOUNTING()
        if not self.api.QueryInformationJobObject(self.handle,1,ctypes.byref(info),ctypes.sizeof(info),None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.ActiveProcesses>0
    def terminate(self):
        if not self.api.TerminateJobObject(self.handle,1):raise ctypes.WinError(ctypes.get_last_error())
    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None
