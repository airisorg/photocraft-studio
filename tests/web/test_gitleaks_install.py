"""Pinned scanner installer tests use harmless bytes; no network or binary execution."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("scanner_installer", Path(__file__).resolve().parents[2]/"packaging/web/install-gitleaks.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, content, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(content)
            if kind == tarfile.SYMTYPE:
                item.linkname = "/not-our-file"
            tar.addfile(item, io.BytesIO(content))
    return output.getvalue()


class GitleaksInstall(unittest.TestCase):
    def test_official_pin_and_valid_binary_install_without_execution(self):
        self.assertEqual(installer.SHA256, '79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e')
        self.assertEqual(installer.URL, 'https://github.com/gitleaks/gitleaks/releases/download/v8.30.0/gitleaks_8.30.0_linux_x64.tar.gz')
        payload = b'\x7fELFharmless fixture; never executable'
        data = archive([('LICENSE', b'fixture notice', tarfile.REGTYPE), ('gitleaks', payload, tarfile.REGTYPE)])
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, 'download', return_value=data), \
                patch.object(installer, 'SHA256', hashlib.sha256(data).hexdigest()):
            dest = Path(directory)/'owned-bin'
            installer.install(dest)
            self.assertEqual((dest/'gitleaks').read_bytes(), payload)
            self.assertEqual((dest/'gitleaks').stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(dest.iterdir()), [dest/'gitleaks'])

    def test_bad_hash_download_failure_and_existing_destination_leave_no_install(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)/'owned-bin'
            for value in [b'changed archive', ValueError('failed transfer')]:
                with self.subTest(value=type(value).__name__), patch.object(installer, 'download', side_effect=value if isinstance(value, Exception) else None, return_value=value):
                    with self.assertRaises(ValueError): installer.install(dest)
                    self.assertFalse(dest.exists())
            dest.mkdir(); (dest/'keep').write_text('original')
            with patch.object(installer, 'download') as download:
                with self.assertRaises(ValueError): installer.install(dest)
                download.assert_not_called()
                self.assertEqual((dest/'keep').read_text(), 'original')

    def test_verified_but_unsafe_archive_is_never_extracted(self):
        payload=b'\x7fELFfixture'
        examples=[('../gitleaks',payload,tarfile.REGTYPE), ('/gitleaks',payload,tarfile.REGTYPE),
                  ('gitleaks',b'',tarfile.SYMTYPE), ('folder\\gitleaks',payload,tarfile.REGTYPE)]
        for entry in examples:
            data=archive([entry])
            with self.subTest(entry=entry[0]), patch.object(installer,'SHA256',hashlib.sha256(data).hexdigest()):
                with self.assertRaises(ValueError): installer.binary_from_archive(data)
        data=archive([('gitleaks',payload,tarfile.REGTYPE)]*2)
        with patch.object(installer,'SHA256',hashlib.sha256(data).hexdigest()):
            with self.assertRaises(ValueError): installer.binary_from_archive(data)

    def test_partial_write_removes_only_new_owned_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            dest=Path(directory)/'owned-bin'; sibling=Path(directory)/'keep';sibling.write_text('original')
            with patch.object(installer,'download',return_value=b'fixture'), patch.object(installer,'binary_from_archive',return_value=b'\x7fELFfixture'), patch.object(Path,'chmod',side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError): installer.install(dest)
            self.assertFalse(dest.exists()); self.assertEqual(sibling.read_text(),'original')

    def test_download_bounds_and_ignores_proxy_and_curl_configuration(self):
        class Process:
            def __init__(self,data,code=0): self.stdout=io.BytesIO(data);self.code=code;self.killed=False
            def wait(self,timeout): return self.code
            def poll(self): return None if not self.killed else self.code
            def kill(self): self.killed=True
        for data,code in [(b'valid',0),(b'partial',22),(b'x'*9,0)]:
            process=Process(data,code)
            with self.subTest(code=code,size=len(data)), patch.object(installer,'MAX_ARCHIVE',8), \
                    patch.dict(os.environ,{'HTTPS_PROXY':'https://untrusted.invalid','CURL_HOME':'/untrusted'}), \
                    patch.object(installer.subprocess,'Popen',return_value=process) as launch:
                if code or len(data)>8:
                    with self.assertRaises(ValueError): installer.download()
                else: self.assertEqual(installer.download(),data)
                args=launch.call_args.args[0];env=launch.call_args.kwargs['env']
                self.assertEqual(args[:2],['curl','-q']);self.assertEqual(args[args.index('--max-time')+1],'90')
                self.assertEqual(args[args.index('--proto-redir')+1],'=https')
                self.assertNotIn('HTTPS_PROXY',env);self.assertNotIn('CURL_HOME',env)
                self.assertTrue(process.killed);self.assertTrue(process.stdout.closed)


if __name__=='__main__': unittest.main()
