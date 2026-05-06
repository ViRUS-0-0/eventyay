"use strict";

document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("filter-form");
    var tableContainer = document.getElementById("orders-table");
    if (!form || !tableContainer) return;

    var clearBtn = form.querySelector(".filter-actions a");

    function fetchOrders(queryString) {
        var url = window.location.pathname;
        if (queryString) url += "?" + queryString;

        fetch(url, {
            headers: {"X-Requested-With": "XMLHttpRequest"},
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error(
                        "HTTP " + response.status + ": " + response.statusText + " for " + url
                    );
                }
                return response.text();
            })
            .then(function (html) {
                tableContainer.innerHTML = html;
                history.pushState(null, "", url);
                bindPaginationLinks();
            })
            .catch(function (err) {
                console.error("Failed to fetch orders:", err);
                tableContainer.innerHTML =
                    '<div class="alert alert-danger">' +
                    "An error occurred while loading orders. Please try again." +
                    "</div>";
            });
    }

    function bindPaginationLinks() {
        var links = tableContainer.querySelectorAll(".pagination a");
        links.forEach(function (link) {
            link.addEventListener("click", function (e) {
                e.preventDefault();
                var href = link.getAttribute("href");
                var qs = href.indexOf("?") !== -1 ? href.split("?")[1] : "";
                fetchOrders(qs);
            });
        });
    }

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        var params = new URLSearchParams(new FormData(form)).toString();
        fetchOrders(params);
    });

    if (clearBtn) {
        clearBtn.addEventListener("click", function (e) {
            e.preventDefault();
            form.reset();
            fetchOrders("");
        });
    }

    window.addEventListener("popstate", function () {
        var qs = window.location.search.replace(/^\?/, "");
        fetchOrders(qs);
    });

    bindPaginationLinks();
});