"""Digital uploads are checked by their real content, not just their extension."""
import io
import zipfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from .file_checks import validate_digital_file_content


def _zip(entries, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _upload(name, data):
    return SimpleUploadedFile(name, data)


class DigitalFileContentTests(SimpleTestCase):
    def ok(self, name, data):
        validate_digital_file_content(_upload(name, data))

    def bad(self, name, data):
        with self.assertRaises(ValidationError):
            validate_digital_file_content(_upload(name, data))

    def test_real_pdf_png_jpeg_doc_pass(self):
        self.ok('n.pdf', b'%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF')
        self.ok('n.png', b'\x89PNG\r\n\x1a\n' + b'\0' * 20)
        self.ok('n.jpg', b'\xff\xd8\xff\xe0' + b'\0' * 20)
        self.ok('n.jpeg', b'\xff\xd8\xff\xe0' + b'\0' * 20)
        self.ok('n.doc', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\0' * 20)

    def test_renamed_programs_are_rejected(self):
        exe = b'MZ\x90\x00\x03\x00\x00\x00 this is a Windows program'
        for name in ('book.pdf', 'book.png', 'book.jpg', 'book.doc', 'book.docx', 'book.zip'):
            self.bad(name, exe)

    def test_pdf_with_javascript_or_launch_is_rejected(self):
        self.bad('n.pdf', b'%PDF-1.7\n1 0 obj\n<< /S /JavaScript /JS (app.alert(1)) >>\nendobj')
        self.bad('n.pdf', b'%PDF-1.7\n1 0 obj\n<< /S /Launch /F (cmd.exe) >>\nendobj')

    def test_marker_split_across_read_chunks_is_still_caught(self):
        from . import file_checks
        pad = b'x' * (file_checks.SCAN_CHUNK - 4)
        self.bad('n.pdf', b'%PDF-1.7\n' + pad + b'/Launch' + b'\n%%EOF')

    def test_plain_zip_with_documents_passes(self):
        self.ok('notes.zip', _zip({'ch1.pdf': b'%PDF-1.4 x', 'img/p.png': b'\x89PNG'}))

    def test_zip_with_a_program_inside_is_rejected(self):
        self.bad('notes.zip', _zip({'ch1.pdf': b'%PDF-1.4 x', 'setup.exe': b'MZ'}))
        self.bad('notes.zip', _zip({'run.js': b'alert(1)'}))

    def test_zip_with_path_traversal_is_rejected(self):
        self.bad('notes.zip', _zip({'../evil.txt': b'x'}))
        self.bad('notes.zip', _zip({'/etc/passwd': b'x'}))

    def test_zip_bomb_is_rejected(self):
        self.bad('bomb.zip', _zip({'zeros.txt': b'\0' * (50 * 1024 * 1024)}))

    def test_too_many_entries_is_rejected(self):
        self.bad('many.zip', _zip({f'f{i}.txt': b'a' for i in range(2100)}))

    def test_corrupt_zip_is_rejected(self):
        data = bytearray(_zip({'a.txt': b'hello world' * 100}))
        self.bad('c.zip', bytes(data[:len(data) // 2]))

    def test_docx_needs_word_structure_and_no_macros(self):
        good = {'[Content_Types].xml': b'<x/>', 'word/document.xml': b'<w/>'}
        self.ok('n.docx', _zip(good))
        self.bad('n.docx', _zip({'readme.txt': b'just a zip'}))
        self.bad('n.docx', _zip({**good, 'word/vbaProject.bin': b'macro'}))

    def test_file_position_is_reset_for_saving(self):
        upload = _upload('n.pdf', b'%PDF-1.7 hello')
        validate_digital_file_content(upload)
        self.assertEqual(upload.read(5), b'%PDF-')

    def test_already_stored_files_are_not_rescanned(self):
        class Stored:
            _committed = True
            name = 'x.pdf'
        validate_digital_file_content(Stored())  # must not try to read anything
