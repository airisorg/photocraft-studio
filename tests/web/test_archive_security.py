"""Small synthetic hostile-native uploads against an isolated loopback worker.

The fixtures never allocate large documents. Compressed manifest amplification is
bounded to4MiB; native unit tests exercise decoded limits with16-byte blobs.
"""
import hashlib
import io
import json
import unittest
import zipfile

import test_api


class ArchiveSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_api.CloudContract.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        test_api.CloudContract.tearDownClass.__func__(cls)

    req = test_api.CloudContract.req
    project = test_api.CloudContract.project
    begin = test_api.CloudContract.begin
    upload = test_api.CloudContract.upload
    tearDown = test_api.CloudContract.tearDown

    def manifest(self):
        with zipfile.ZipFile(io.BytesIO(self.fixture)) as archive:
            return json.loads(archive.read("manifest.json"))

    def bundle(self, manifest, extra=(), duplicate=False):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            data = json.dumps(manifest).encode()
            archive.writestr("manifest.json", data)
            if duplicate:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr("manifest.json", data)
            for name, data in extra:
                archive.writestr(name, data)
        return out.getvalue()

    def rejects_without_changing_saved_document(self, data):
        pid = self.project()
        self.upload(pid)
        upload = self.begin(pid, data, base=1)
        from test_api import CHUNK
        for offset in range(0, len(data), CHUNK):
            self.req(self.owner, "PUT", f"/api/uploads/{upload}/{offset//CHUNK}", data=data[offset:offset+CHUNK])
        reply = self.req(self.owner, "POST", f"/api/uploads/{upload}/commit", status=400, json={})
        self.assertTrue(reply.json().get("error"))
        project = self.req(self.owner, "GET", f"/api/projects/{pid}").json()
        self.assertEqual(project["revision"], 1)
        content = self.req(self.owner, "GET", f"/api/projects/{pid}/content?revision=1&part=0").content
        self.assertEqual(hashlib.sha256(content).digest(), hashlib.sha256(self.fixture).digest())
        self.assertEqual(self.db.execute("SELECT count(*) FROM photocraft.uploads WHERE id=%s", (upload,)).fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM photocraft.versions WHERE project_id=%s", (pid,)).fetchone()[0], 1)

    def test_01_missing_referenced_native_blob_is_rejected(self):
        manifest = self.manifest()
        manifest["document"]["icc_profile"] = "0"*64
        self.rejects_without_changing_saved_document(self.bundle(manifest))

    def test_02_corrupt_referenced_native_blob_is_rejected(self):
        manifest = self.manifest()
        manifest["document"]["icc_profile"] = "0"*64
        self.rejects_without_changing_saved_document(self.bundle(manifest, [("blobs/"+"0"*64+".zst", b"not zstd")]))

    def test_03_compressed_manifest_expansion_is_bounded(self):
        manifest = self.manifest()
        manifest["generator"] = "x"*(4*1024*1024+1)
        data = self.bundle(manifest)
        self.assertLess(len(data), 8*1024)
        self.rejects_without_changing_saved_document(data)

    def test_04_duplicate_archive_names_are_rejected(self):
        self.rejects_without_changing_saved_document(self.bundle(self.manifest(), duplicate=True))

    def test_05_corrupt_unused_archive_entry_is_rejected(self):
        data = bytearray(self.bundle(self.manifest(), [("unused.bin", b"bounded integrity fixture")]))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entry = archive.getinfo("unused.bin")
            position = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
        data[position] ^= 0xff
        self.rejects_without_changing_saved_document(bytes(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
