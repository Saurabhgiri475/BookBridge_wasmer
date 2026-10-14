"""
Content checks for paid digital files (the file buyers receive after paying).

The extension alone proves nothing - `virus.exe` renamed to `notes.pdf` passes
FileExtensionValidator. These checks read the file's actual bytes:

* the leading "magic bytes" must match the extension (PDF, PNG, JPEG, legacy Word, DOCX/ZIP);
* a PDF must not carry auto-run / launch actions;
* a ZIP/DOCX must be a real, intact archive with no path traversal, no zip-bomb behaviour
  (entry count, total size, compression ratio) and no executable or macro payloads.

This is defence in depth, not an antivirus: for an extra layer, scan new uploads with ClamAV
(e.g. `clamdscan`) in the same place. The files are never executed by the server and are always
served as a download with `nosniff` (see orders.views.download_digital_file).
"""
import os
import zipfile

from django.core.exceptions import ValidationError

MAX_ZIP_ENTRIES = 2000
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024   # 200 MB when unpacked
MAX_ZIP_RATIO = 100                        # one entry may not expand more than 100x
SCAN_CHUNK = 1024 * 1024

# Files that can run code if someone double-clicks them. Not allowed inside a ZIP/DOCX.
BLOCKED_INNER_EXTENSIONS = {
    '.exe', '.dll', '.bat', '.cmd', '.com', '.scr', '.msi', '.ps1', '.vbs', '.vbe', '.js', '.jse',
    '.wsf', '.jar', '.apk', '.sh', '.lnk', '.hta', '.reg', '.cpl', '.pif', '.app', '.dmg',
}
PDF_ACTIVE_MARKERS = (b'/Launch', b'/JavaScript', b'/JS ', b'/JS(', b'/JS\n', b'/JS\r', b'/RichMedia')

_OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_ZIP_MAGICS = (b'PK\x03\x04', b'PK\x05\x06')


def _error(msg):
    return ValidationError(msg, code='bad_file_content')


def _head(f, n=16):
    f.seek(0)
    data = f.read(n)
    f.seek(0)
    return data


def _check_pdf(f):
    f.seek(0)
    first = f.read(1024)
    if b'%PDF-' not in first:
        raise _error('This file is not a real PDF - its contents do not match the .pdf extension.')
    f.seek(0)
    tail = b''
    while True:
        chunk = f.read(SCAN_CHUNK)
        if not chunk:
            break
        window = tail + chunk
        if any(marker in window for marker in PDF_ACTIVE_MARKERS):
            raise _error('PDFs that contain scripts or auto-launch actions are not allowed. Re-save it as a plain PDF ("Print to PDF").')
        tail = window[-16:]
    f.seek(0)


def _check_zip(f, *, is_docx):
    f.seek(0)
    try:
        archive = zipfile.ZipFile(f)
    except zipfile.BadZipFile:
        raise _error('This file is not a valid ZIP/Word archive - it may be corrupt or renamed.')
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise _error(f'The archive has too many files ({len(infos)}; limit {MAX_ZIP_ENTRIES}).')
        total = 0
        names = set()
        for info in infos:
            name = info.filename
            names.add(name)
            norm = name.replace('\\', '/')
            if norm.startswith('/') or '..' in norm.split('/') or (len(norm) > 1 and norm[1] == ':'):
                raise _error('The archive contains unsafe file paths.')
            if info.flag_bits & 0x1:
                raise _error('Password-protected archives are not allowed - buyers could not be checked or refunded fairly.')
            ext = os.path.splitext(norm)[1].lower()
            if ext in BLOCKED_INNER_EXTENSIONS:
                raise _error(f'The archive contains a program file ({ext}). Only documents and images may be sold.')
            total += info.file_size
            if total > MAX_ZIP_UNCOMPRESSED:
                raise _error('The archive is too large once unpacked.')
            if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO and info.file_size > 1024 * 1024:
                raise _error('The archive looks like a compression bomb and was rejected.')
        if is_docx:
            if '[Content_Types].xml' not in names or not any(n.startswith('word/') for n in names):
                raise _error('This is not a real Word (.docx) document.')
            if any(n.lower().endswith('vbaproject.bin') for n in names):
                raise _error('Word documents with macros are not allowed. Save it as a macro-free .docx.')
        bad = archive.testzip()  # CRC check of every entry
        if bad:
            raise _error('The archive is corrupt.')
    f.seek(0)


def validate_digital_file_content(uploaded):
    """Field validator for Book.digital_file. Only inspects NEW uploads - a file already in
    storage (editing a listing without re-uploading) was checked when it came in."""
    if getattr(uploaded, '_committed', False) is True:
        return
    f = getattr(uploaded, 'file', uploaded)
    name = getattr(uploaded, 'name', '') or ''
    ext = os.path.splitext(name)[1].lower().lstrip('.')
    try:
        head = _head(f, 16)
        if ext == 'pdf':
            _check_pdf(f)
        elif ext == 'png':
            if not head.startswith(b'\x89PNG\r\n\x1a\n'):
                raise _error('This file is not a real PNG image.')
        elif ext in ('jpg', 'jpeg'):
            if not head.startswith(b'\xff\xd8\xff'):
                raise _error('This file is not a real JPEG image.')
        elif ext == 'doc':
            if not head.startswith(_OLE):
                raise _error('This file is not a real Word (.doc) document.')
        elif ext in ('zip', 'docx'):
            if not head.startswith(_ZIP_MAGICS):
                raise _error('This file is not a real ZIP/Word archive.')
            _check_zip(f, is_docx=(ext == 'docx'))
    finally:
        try:
            f.seek(0)
        except Exception:
            pass
