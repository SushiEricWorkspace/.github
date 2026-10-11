"""Windows待受の期限と接続完了が競合しても、成功した接続を破棄しない。"""
import os
import unittest
from unittest.mock import Mock, patch

from . import local_ipc


@unittest.skipUnless(os.name == "nt", "Windowsのoverlapped待受専用")
class WindowsAcceptTests(unittest.TestCase):
    def accept(self, error, waited=None):
        server = local_ipc.WindowsListener.__new__(local_ipc.WindowsListener)
        server.handle = 101
        server._create = Mock(return_value=102)
        overlapped = Mock(event=201)
        overlapped.GetOverlappedResult.return_value = (0, error)
        connection = object()
        with patch.object(local_ipc._winapi, "ConnectNamedPipe", return_value=overlapped), patch.object(local_ipc._winapi, "WaitForMultipleObjects", return_value=local_ipc._winapi.WAIT_TIMEOUT if waited is None else waited), patch.object(local_ipc.kernel, "CloseHandle") as close, patch.object(local_ipc, "PipeConnection", return_value=connection) as create:
            try:
                result = server.accept(timeout=.25)
            except OSError as failure:
                result = failure
        self.assertEqual(server.handle, 102)
        return result, connection, overlapped, close, create

    def test_connection_completed_during_cancel_is_returned(self):
        result, connection, overlapped, close, create = self.accept(0)
        self.assertIs(result, connection)
        overlapped.cancel.assert_called_once()
        create.assert_called_once_with(101)
        close.assert_not_called()

    def test_cancelled_wait_closes_only_previous_handle(self):
        result, _, overlapped, close, create = self.accept(local_ipc._winapi.ERROR_OPERATION_ABORTED)
        self.assertIsNone(result)
        overlapped.cancel.assert_called_once()
        close.assert_called_once_with(101)
        create.assert_not_called()

    def test_failed_completion_is_not_accepted(self):
        result, _, _, close, create = self.accept(5)
        self.assertIsInstance(result, OSError)
        close.assert_called_once_with(101)
        create.assert_not_called()

    def test_normal_completion_does_not_cancel(self):
        result, connection, overlapped, close, create = self.accept(0, local_ipc._winapi.WAIT_OBJECT_0)
        self.assertIs(result, connection)
        overlapped.cancel.assert_not_called()
        close.assert_not_called()
        create.assert_called_once_with(101)
