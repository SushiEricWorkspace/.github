"""OS開始識別・boot・cwd・socket所有と、killしない子プロセス所有adapter。"""
import ctypes
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import uuid

from .local_ipc import supported_os
from .registry import require


def command_hash(arguments: list[str]) -> str:
    return hashlib.sha256(json.dumps(arguments, ensure_ascii=False).encode()).hexdigest()


if os.name == "nt":
    import _winapi
    import msvcrt
    from ctypes import wintypes as w
    from .windows_security import kernel, security_descriptor, _check, process_sid

    def function(dll, name, args, result):
        value = getattr(dll, name)
        value.argtypes, value.restype = args, result
        return value

    ntdll = ctypes.WinDLL("ntdll")
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    iphlp = ctypes.WinDLL("iphlpapi")
    function(kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    function(kernel, "GetProcessTimes", [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL)
    function(kernel, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL)
    function(kernel, "ReadProcessMemory", [w.HANDLE, w.LPCVOID, w.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], w.BOOL)
    function(kernel, "CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE)
    function(kernel, "AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL)
    function(kernel, "QueryInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
    function(kernel, "ResumeThread", [w.HANDLE], w.DWORD)
    function(ntdll, "NtQueryInformationProcess", [w.HANDLE, ctypes.c_int, w.LPVOID, w.ULONG, w.LPVOID], ctypes.c_long)
    function(ntdll, "NtQuerySystemInformation", [ctypes.c_int, w.LPVOID, w.ULONG, w.LPVOID], ctypes.c_long)
    function(shell, "CommandLineToArgvW", [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(w.LPWSTR))
    function(iphlp, "GetExtendedTcpTable", [w.LPVOID, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG], w.DWORD)
    function(iphlp, "GetExtendedUdpTable", [w.LPVOID, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG], w.DWORD)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]

    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", w.LPVOID)]

    class CurrentDirectory(ctypes.Structure):
        _fields_ = [("path", UnicodeString), ("handle", w.HANDLE)]

    class ProcessParameters(ctypes.Structure):
        _fields_ = [("maximum", w.ULONG), ("length", w.ULONG), ("flags", w.ULONG), ("debug", w.ULONG),
                    ("console", w.HANDLE), ("consoleFlags", w.ULONG),
                    ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE),
                    ("cwd", CurrentDirectory), ("dllPath", UnicodeString), ("imagePath", UnicodeString),
                    ("commandLine", UnicodeString)]

    class Peb(ctypes.Structure):
        _fields_ = [("reserved", ctypes.c_byte * 4), ("reservedPointers", w.LPVOID * 2),
                    ("ldr", w.LPVOID), ("parameters", w.LPVOID)]

    class BasicInformation(ctypes.Structure):
        _fields_ = [("reserved", w.LPVOID), ("peb", w.LPVOID), ("reservedPointers", w.LPVOID * 2),
                    ("pid", ctypes.c_size_t), ("reservedLast", w.LPVOID)]

    def read_memory(handle, address, size):
        result = ctypes.create_string_buffer(size)
        copied = ctypes.c_size_t()
        _check(kernel.ReadProcessMemory(handle, address, result, size, ctypes.byref(copied)))
        _check(copied.value == size)
        return result.raw

    def unicode_value(handle, value):
        return read_memory(handle, value.buffer, value.length).decode("utf-16-le")

    def boot_id() -> str:
        # SystemBootEnvironmentInformationのBootIdentifier。時刻からbootを推測しない。
        data = ctypes.create_string_buffer(64)
        require(ntdll.NtQuerySystemInformation(90, data, len(data), None) == 0,
                "PROCESS_UNVERIFIED", "OS boot識別を取得できません")
        return data.raw[:16].hex()

    def identity(pid: int) -> dict | None:
        supported_os()
        require(ctypes.sizeof(w.LPVOID) == 8, "UNSUPPORTED_PROCESS", "64bitのnative process adapterが必要です")
        handle = kernel.OpenProcess(0x100000 | 0x400 | 0x10, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if _winapi.WaitForSingleObject(int(handle), 0) == _winapi.WAIT_OBJECT_0:
                return None
            times = [w.FILETIME() for _ in range(4)]
            _check(kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in times]))
            started = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
            length, executable = w.DWORD(32768), ctypes.create_unicode_buffer(32768)
            _check(kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(length)))
            basic = BasicInformation()
            require(ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), None) == 0,
                    "PROCESS_UNVERIFIED", "OS process parameterを取得できません")
            peb = Peb.from_buffer_copy(read_memory(handle, basic.peb, ctypes.sizeof(Peb)))
            parameters = ProcessParameters.from_buffer_copy(read_memory(handle, peb.parameters, ctypes.sizeof(ProcessParameters)))
            command = unicode_value(handle, parameters.commandLine)
            count = ctypes.c_int()
            argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
            _check(bool(argv))
            try:
                arguments = [argv[index] for index in range(count.value)]
            finally:
                kernel.LocalFree(ctypes.cast(argv, w.HLOCAL))
            return {"pid": pid, "startId": str(started), "startMillis": (started - 116444736000000000) // 10000,
                    "bootId": boot_id(), "cwd": str(Path(unicode_value(handle, parameters.cwd.path)).resolve()),
                    "executable": str(Path(executable.value).resolve()), "commandHash": command_hash(arguments),
                    "owner": process_sid(handle)}
        finally:
            kernel.CloseHandle(handle)

    def sockets(pid: int) -> list[dict]:
        result = []
        for protocol in ("tcp", "udp"):
            for family in (socket.AF_INET, socket.AF_INET6):
                query = iphlp.GetExtendedTcpTable if protocol == "tcp" else iphlp.GetExtendedUdpTable
                size = w.DWORD()
                error = query(None, ctypes.byref(size), False, family, 3 if protocol == "tcp" else 1, 0)
                while error == 122:
                    data = ctypes.create_string_buffer(size.value)
                    error = query(data, ctypes.byref(size), False, family, 3 if protocol == "tcp" else 1, 0)
                if error:
                    raise ctypes.WinError(error)
                count = struct.unpack_from("<I", data.raw)[0]
                row_size = (24 if family == socket.AF_INET else 56) if protocol == "tcp" else (12 if family == socket.AF_INET else 28)
                for index in range(count):
                    row = data.raw[4 + index * row_size:4 + (index + 1) * row_size]
                    if struct.unpack_from("<I", row, row_size - 4)[0] != pid:
                        continue
                    offset = 4 if protocol == "tcp" and family == socket.AF_INET else 0
                    address_size = 4 if family == socket.AF_INET else 16
                    port_offset = offset + address_size + (4 if family == socket.AF_INET6 else 0)
                    address = socket.inet_ntop(family, row[offset:offset + address_size])
                    mapped = getattr(ipaddress.ip_address(address), "ipv4_mapped", None)
                    if mapped is not None:
                        address = str(mapped)
                    result.append({"protocol": protocol, "address": address,
                                   "port": struct.unpack_from(">H", row, port_offset)[0]})
        return result

    class OwnedProcess:
        """監督へ先に登録し、途中失敗もhandleを保全する。job-closeによるkillは設定しない。"""
        def __init__(self):
            self.job = self.handle = self.thread = self.pid = None
            self.group = None
            self.stage = "NOT_CREATED"

        def start(self, arguments, cwd, log, name, created):
            self.group = "Local\\sushieric-runtime-" + name
            with security_descriptor() as descriptor:
                attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
                self.job = kernel.CreateJobObjectW(ctypes.byref(attributes), self.group)
                _check(bool(self.job))
                if ctypes.get_last_error() == 183:
                    kernel.CloseHandle(self.job)
                    self.job = None
                    require(False, "PROCESS_MISMATCH", "既存jobを引き継げません")
            self.stage = "JOB_CREATED"
            with open(os.devnull, "rb") as empty:
                start = subprocess.STARTUPINFO()
                start.dwFlags = subprocess.STARTF_USESTDHANDLES
                start.hStdInput = msvcrt.get_osfhandle(empty.fileno())
                start.hStdOutput = start.hStdError = msvcrt.get_osfhandle(log.fileno())
                start.lpAttributeList = {"handle_list": [start.hStdInput, start.hStdOutput]}
                for handle in start.lpAttributeList["handle_list"]:
                    os.set_handle_inheritable(handle, True)
                try:
                    self.handle, self.thread, self.pid, _ = _winapi.CreateProcess(arguments[0], subprocess.list2cmdline(arguments), None, None,
                        True, 0x4 | subprocess.CREATE_NO_WINDOW, None, str(cwd), start)
                    self.stage = "CREATED"
                finally:
                    for handle in start.lpAttributeList["handle_list"]:
                        os.set_handle_inheritable(handle, False)
            created()
            self.stage = "VERIFIED"
            _check(kernel.AssignProcessToJobObject(self.job, self.handle))
            self.stage = "ASSIGNED"
            _check(kernel.ResumeThread(self.thread) != 0xFFFFFFFF)
            self.stage = "RUNNING"
            _winapi.CloseHandle(self.thread)
            self.thread = None

        def poll(self):
            return _winapi.GetExitCodeProcess(self.handle) if _winapi.WaitForSingleObject(self.handle, 0) == _winapi.WAIT_OBJECT_0 else None

        def members(self):
            size = 256
            while True:
                data = ctypes.create_string_buffer(size)
                if kernel.QueryInformationJobObject(self.job, 3, data, size, None):
                    count = struct.unpack_from("<I", data.raw, 4)[0]
                    return list(struct.unpack_from("<" + "Q" * count, data.raw, 8))
                if ctypes.get_last_error() not in (122, 234):
                    raise ctypes.WinError(ctypes.get_last_error())
                size *= 2

        def close(self):
            _winapi.CloseHandle(self.handle)
            kernel.CloseHandle(self.job)

else:
    class BsdInfo(ctypes.Structure):
        _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
                    ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                    ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
                    ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32), ("jobc", ctypes.c_uint32),
                    ("tdev", ctypes.c_uint32), ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
                    ("seconds", ctypes.c_uint64), ("micros", ctypes.c_uint64)]

    def library():
        supported_os()
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        return lib

    def sysctl(mib):
        lib = ctypes.CDLL(None, use_errno=True)
        lib.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                              ctypes.c_void_p, ctypes.c_size_t]
        keys = (ctypes.c_int * len(mib))(*mib)
        length = ctypes.c_size_t()
        if lib.sysctl(keys, len(mib), None, ctypes.byref(length), None, 0):
            raise OSError(ctypes.get_errno(), "sysctl識別を取得できません")
        data = ctypes.create_string_buffer(length.value)
        if lib.sysctl(keys, len(mib), data, ctypes.byref(length), None, 0):
            raise OSError(ctypes.get_errno(), "sysctl識別を取得できません")
        return data.raw[:length.value]

    def boot_id():
        lib = ctypes.CDLL(None, use_errno=True)
        lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                    ctypes.c_void_p, ctypes.c_size_t]
        data = ctypes.create_string_buffer(37)
        length = ctypes.c_size_t(len(data))
        if lib.sysctlbyname(b"kern.bootsessionuuid", data, ctypes.byref(length), None, 0):
            raise OSError(ctypes.get_errno(), "OS boot session識別を取得できません")
        return str(uuid.UUID(data.value.decode("ascii")))

    def bsd(pid):
        value = BsdInfo()
        lib = library()
        size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value))
        if size == 0 and ctypes.get_errno() == 3:
            return None
        require(size == ctypes.sizeof(value), "PROCESS_UNVERIFIED", "native process識別を取得できません")
        return value

    def identity(pid):
        value = bsd(pid)
        if value is None or value.status == 5:
            return None
        data = sysctl([1, 49, pid])  # KERN_PROCARGS2
        count = struct.unpack_from("i", data)[0]
        end = data.index(b"\0", 4)
        rest = data[end + 1:].lstrip(b"\0")
        arguments = [os.fsdecode(arg) for arg in rest.split(b"\0")[:count]]
        lib = library()
        executable = ctypes.create_string_buffer(4096)
        require(lib.proc_pidpath(pid, executable, len(executable)) > 0, "PROCESS_UNVERIFIED", "実行元を取得できません")
        output = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-F0pn"],
                                capture_output=True, check=True).stdout
        names = [os.fsdecode(part.lstrip(b"\n")[1:]) for part in output.split(b"\0") if part.lstrip(b"\n").startswith(b"n")]
        require(len(names) == 1, "PROCESS_UNVERIFIED", "実cwdを一意に取得できません")
        return {"pid": pid, "startId": f"{value.seconds}:{value.micros}",
                "startMillis": value.seconds * 1000 + value.micros // 1000,
                "bootId": boot_id(), "cwd": str(Path(names[0]).resolve()),
                "executable": str(Path(os.fsdecode(executable.value)).resolve()),
                "commandHash": command_hash(arguments), "owner": str(value.uid)}

    def sockets(pid):
        output = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-i", "-F0fPtnT"], capture_output=True)
        require(output.returncode in (0, 1), "PROCESS_UNVERIFIED", "実socketを取得できません")
        records, record = [], None
        for part in output.stdout.split(b"\0"):
            part = part.lstrip(b"\n")
            if part.startswith(b"f"):
                record = {}
                records.append(record)
            elif record is not None and part:
                if part[:1] != b"T" or part.startswith(b"TST="):
                    record[part[:1].decode()] = part[1:].decode()
        result = []
        for item in records:
            protocol = item.get("P", "").lower()
            if protocol not in ("tcp", "udp") or (protocol == "tcp" and item.get("T") != "ST=LISTEN"):
                continue
            name = item["n"]
            if "->" in name:
                continue
            address, port = name.rsplit(":", 1)
            if address == "*":
                address = "::" if item["t"] == "IPv6" else "0.0.0.0"
            address = ipaddress.ip_address(address.strip("[]"))
            result.append({"protocol": protocol, "address": str(getattr(address, "ipv4_mapped", None) or address), "port": int(port)})
        return result

    class OwnedProcess:
        """専用session/process groupの所有プロセスだけを待つ。signal/killは行わない。"""
        def __init__(self):
            self.process = self.pid = self.group = None
            self.stage = "NOT_CREATED"

        def start(self, arguments, cwd, log, name, created):
            self.process = subprocess.Popen(arguments, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            self.pid = self.process.pid
            self.group = self.pid
            self.stage = "CREATED"
            created()
            self.stage = "RUNNING"

        def poll(self):
            return self.process.poll()

        def members(self):
            lib = library()
            ctypes.set_errno(0)
            size = lib.proc_listpids(2, self.pid, None, 0)
            require(size >= 0 and ctypes.get_errno() == 0, "PROCESS_UNVERIFIED", "所有groupを取得できません")
            while True:
                data = ctypes.create_string_buffer(size + 256)
                ctypes.set_errno(0)
                count = lib.proc_listpids(2, self.pid, data, len(data))
                require(count >= 0 and ctypes.get_errno() == 0, "PROCESS_UNVERIFIED", "所有groupを取得できません")
                if count < len(data):
                    pids = struct.unpack_from("=" + "I" * (count // 4), data.raw)
                    result = []
                    for pid in pids:
                        value = bsd(pid) if pid else None
                        if value is not None and value.status != 5:
                            result.append(pid)
                    return result
                size = count

        def close(self):
            self.process.wait(timeout=0)


def recorded_members(creation: dict, nonce: str) -> list[int]:
    """記録済み所有groupの生存PIDを読む。groupの引継ぎ・終了は行わない。"""
    supported_os()
    require(type(creation.get("pid")) is int and creation["pid"] > 0,
            "PROCESS_UNVERIFIED", "生成PIDの記録がありません")
    probe = OwnedProcess()
    if os.name == "nt":
        expected = "Local\\sushieric-runtime-" + nonce
        require(creation.get("ownerGroup") == expected, "PROCESS_MISMATCH", "所有job識別が一致しません")
        function(kernel, "OpenJobObjectW", [w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE)
        probe.job = kernel.OpenJobObjectW(4, False, expected)
        if not probe.job:
            require(ctypes.get_last_error() == 2, "PROCESS_UNVERIFIED", "所有jobを照合できません")
            return []
        try:
            return probe.members()
        finally:
            kernel.CloseHandle(probe.job)
    require(creation.get("ownerGroup") == creation["pid"], "PROCESS_MISMATCH", "所有group識別が一致しません")
    probe.pid = creation["pid"]
    return probe.members()


def require_identity(expected: dict, actual: dict | None) -> None:
    """PIDだけで所有を推定せず、native開始識別・boot・cwd・実行元・起動commandを照合する。"""
    require(actual is not None and expected == actual, "PROCESS_MISMATCH", "プロセス所有識別が一致しません")


def proof_start_millis(value: str) -> int:
    require(type(value) is str, "PROOF_MISMATCH", "起動時刻の形式が不正です")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(instant.tzinfo is not None, "PROOF_MISMATCH", "起動時刻にtimezoneが必要です")
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
