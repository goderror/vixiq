"""验证源码与安装版的实例隔离，不启动或退出真实安装版。"""
import ctypes
import sys
import unittest
from unittest.mock import MagicMock, patch

import main


class SourceIsolationTests(unittest.TestCase):
    def test_source_mutex_does_not_share_packaged_mutex(self):
        kernel = MagicMock()
        kernel.CreateMutexW.return_value = 123
        kernel.GetLastError.return_value = 0
        with patch.object(ctypes.windll, 'kernel32', kernel), patch.object(main, '_instance_mutex', None):
            with patch.object(sys, 'frozen', False, create=True):
                main._acquire_single_instance()
                source_name = kernel.CreateMutexW.call_args.args[2]
                main._acquire_single_instance()
                self.assertEqual(source_name, kernel.CreateMutexW.call_args.args[2])
            with patch.object(sys, 'frozen', True, create=True):
                main._acquire_single_instance()
                self.assertEqual(kernel.CreateMutexW.call_args.args[2], 'vixiq_web')
        self.assertTrue(source_name.startswith('vixiq_web_source_'))

    def test_isolated_quit_only_contacts_source_port(self):
        cfg = {'web': {'port': 7811}, 'launcher': {'isolated': True}}
        with patch.object(sys, 'argv', ['main.py', '--quit']), \
                patch.object(sys, 'frozen', False, create=True), \
                patch.object(main, 'load_config', return_value=cfg), \
                patch.object(main, '_safe_console'), \
                patch.object(main, '_close_native_splash'), \
                patch('http.client.HTTPConnection') as connection, \
                patch('utils.tray.find_tray_window') as tray:
            main.main()
            connection.assert_called_once_with('127.0.0.1', 7811, timeout=2)
            connection.return_value.request.assert_called_once_with('POST', '/api/app/quit')
            tray.assert_not_called()


if __name__ == '__main__':
    unittest.main()
