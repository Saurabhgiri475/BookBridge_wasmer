// upload-preview.js
// For every <input type="file" data-preview="<id of a container>">:
//   - shows a thumbnail (or a filename card for formats the browser can't draw, e.g. HEIC outside Safari)
//   - warns right away about wrong type / too-large files, before the form is submitted
//   - lets the person remove the chosen file, and shows the already-saved document when editing
// The server re-validates everything (accounts/validators.py:clean_proof_image) - this is just faster feedback.
(function () {
    'use strict';
    var MAX_BYTES = 5 * 1024 * 1024;
    var OK_EXT = ['jpg', 'jpeg', 'png', 'webp', 'heic', 'heif'];

    function ext(name) { var m = /\.([a-z0-9]+)$/i.exec(name || ''); return m ? m[1].toLowerCase() : ''; }
    function fmtSize(b) { return b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB'; }

    document.querySelectorAll('input[type=file][data-preview]').forEach(function (input) {
        var box = document.getElementById(input.getAttribute('data-preview'));
        if (!box) return;
        var label = input.getAttribute('data-label') || 'file';
        var savedUrl = box.getAttribute('data-saved-url') || '';
        var objectUrl = null;

        function clearObjectUrl() { if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; } }

        function render(html) { box.innerHTML = html; }
        function esc(t) { var d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

        function showSaved() {
            if (!savedUrl) { box.innerHTML = ''; return; }
            render('<div class="d-flex align-items-center gap-2 mt-2"><img src="' + esc(savedUrl) + '" alt="Current ' + esc(label) + '" class="rounded border" style="max-height:90px;max-width:140px;object-fit:cover">' +
                   '<span class="small text-muted">Current ' + esc(label) + ' (pick a new file to replace it)</span></div>');
        }

        function problem(text) {
            clearObjectUrl();
            input.value = '';
            render('<div class="alert alert-warning py-2 small mt-2 mb-0"><i class="bi bi-exclamation-triangle"></i> ' + esc(text) + '</div>');
        }

        input.addEventListener('change', function () {
            clearObjectUrl();
            var file = input.files && input.files[0];
            if (!file) { showSaved(); return; }
            var e = ext(file.name);
            if (OK_EXT.indexOf(e) === -1 && !/^image\/(jpeg|png|webp|heic|heif)$/.test(file.type)) {
                problem('That looks like a .' + (e || 'unknown') + ' file. Please choose a JPG, PNG, WebP or HEIC photo.'); return;
            }
            if (file.size > MAX_BYTES) {
                problem('This ' + label + ' is ' + fmtSize(file.size) + ' - the limit is 5 MB. Try a lower camera resolution.'); return;
            }
            var isHeic = e === 'heic' || e === 'heif' || /heic|heif/.test(file.type);
            var removeBtn = '<button type="button" class="btn btn-sm btn-link text-danger p-0 ms-2" data-remove>Remove</button>';
            if (isHeic) {
                // Chrome/Firefox can't decode HEIC; Safari can. Try, and fall back to a card if it fails.
                objectUrl = URL.createObjectURL(file);
                var probe = new Image();
                probe.onload = function () { drawThumb(file); };
                probe.onerror = function () {
                    render('<div class="d-flex align-items-center gap-2 mt-2 p-2 border rounded small"><i class="bi bi-file-earmark-image fs-3 text-secondary"></i>' +
                           '<div><div class="fw-semibold">' + esc(file.name) + ' (' + fmtSize(file.size) + ')</div>' +
                           '<div class="text-muted">HEIC photo - your browser can\u2019t preview it, but we\u2019ll convert it to JPG when you submit.</div></div>' + removeBtn + '</div>');
                    wireRemove();
                };
                probe.src = objectUrl;
                return;
            }
            objectUrl = URL.createObjectURL(file);
            drawThumb(file);

            function drawThumb(f) {
                render('<div class="d-flex align-items-center gap-2 mt-2"><img src="' + objectUrl + '" alt="Preview of ' + esc(label) + '" class="rounded border" style="max-height:90px;max-width:140px;object-fit:cover">' +
                       '<div class="small"><div class="fw-semibold">' + esc(f.name) + '</div><div class="text-muted">' + fmtSize(f.size) + '</div></div>' + removeBtn + '</div>');
                wireRemove();
            }
            function wireRemove() {
                var b = box.querySelector('[data-remove]');
                if (b) b.addEventListener('click', function () { clearObjectUrl(); input.value = ''; showSaved(); });
            }
        });
        showSaved();
    });

    // GSTIN / PAN: upper-case as the person types, strip spaces.
    document.querySelectorAll('input[data-gstin], input[name$="pan_number"]').forEach(function (el) {
        el.addEventListener('input', function () {
            var pos = el.selectionStart;
            el.value = el.value.toUpperCase().replace(/[\s-]/g, '');
            try { el.setSelectionRange(pos, pos); } catch (e) { /* not all input types support it */ }
        });
    });
})();
