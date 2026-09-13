/* Pharmacy management list: live search + status filter + row count, plus the
   "Add Pharmacy" dialog (open/close, temporary-password generation). */
(function () {
    function setupAddPharmacyDialog() {
        var open = document.getElementById('add-pharmacy-open');
        var dialog = document.getElementById('add-pharmacy-dialog');
        if (!open || !dialog) return;

        function show() {
            dialog.classList.remove('hidden');
            document.body.style.overflow = 'hidden';
            var email = document.getElementById('ph-email');
            if (email) {
                window.setTimeout(function () { email.focus(); }, 50);
            }
        }

        function hide() {
            dialog.classList.add('hidden');
            document.body.style.overflow = '';
        }

        open.addEventListener('click', show);
        dialog.addEventListener('click', function (e) {
            if (e.target.closest('[data-dialog-close]')) hide();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !dialog.classList.contains('hidden')) hide();
        });

        // Re-open the dialog if creation failed validation, pre-filled.
        if (document.querySelector('#add-pharmacy-dialog .bg-error\\/10')) {
            show();
        }
    }

    function setupPasswordGenerator() {
        var btn = document.getElementById('generate-password');
        var input = document.getElementById('ph-password');
        if (!btn || !input) return;
        // Same safe alphabet as the Flutter app (no 0/O, 1/l/I).
        var chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789';
        btn.addEventListener('click', function () {
            var pw = '';
            var arr = new Uint32Array(10);
            if (window.crypto && crypto.getRandomValues) {
                crypto.getRandomValues(arr);
                for (var i = 0; i < 10; i++) pw += chars[arr[i] % chars.length];
            } else {
                for (var i = 0; i < 10; i++) pw += chars[Math.floor(Math.random() * chars.length)];
            }
            input.value = pw;
        });
    }

    function setupPharmacyFilter() {
        var search = document.getElementById('pharmacy-search');
        var filter = document.getElementById('pharmacy-status-filter');
        var count = document.getElementById('pharmacy-count');
        var rows = document.querySelectorAll('.pharmacy-row');
        if (!search || !filter || !count || rows.length === 0) return;

        function apply() {
            var q = search.value.trim().toLowerCase();
            var status = filter.value;
            var shown = 0;
            rows.forEach(function (row) {
                var display = row.dataset.display;
                var matchStatus = status === 'all' || display === status;
                var matchQuery = !q ||
                    (row.dataset.name && row.dataset.name.indexOf(q) !== -1) ||
                    (row.dataset.email && row.dataset.email.indexOf(q) !== -1);
                var visible = matchStatus && matchQuery;
                row.style.display = visible ? '' : 'none';
                if (visible) shown++;
            });
            count.textContent = shown + ' of ' + rows.length + ' pharmacies';
        }

        search.addEventListener('input', apply);
        filter.addEventListener('change', apply);
        apply();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            setupPharmacyFilter();
            setupAddPharmacyDialog();
            setupPasswordGenerator();
        });
    } else {
        setupPharmacyFilter();
        setupAddPharmacyDialog();
        setupPasswordGenerator();
    }
})();