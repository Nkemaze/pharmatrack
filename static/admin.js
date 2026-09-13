/* Pharmacy management list: live search + status filter + row count. */
(function () {
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
                var matchStatus = status === 'all' || row.dataset.status === status;
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
        document.addEventListener('DOMContentLoaded', setupPharmacyFilter);
    } else {
        setupPharmacyFilter();
    }
})();