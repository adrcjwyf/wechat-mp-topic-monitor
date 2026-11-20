"use strict";

const historyForm = document.querySelector(".history-form");
if (historyForm) {
    let version = 0;
    const labels = document.querySelectorAll("[data-estimate]");
    historyForm.addEventListener("change", async event => {
        if (!["start", "end", "unlimited"].includes(event.target.name)) return;
        const request = ++version;
        const query = new URLSearchParams();
        for (const name of ["start", "end", "unlimited"]) {
            const value = new FormData(historyForm).get(name);
            if (value) query.set(name, value);
        }
        if (!query.has("unlimited") && (!query.has("start") || !query.has("end") || query.get("start") > query.get("end"))) {
            for (const label of labels) label.textContent = "请选好起止日期";
            return;
        }
        for (const label of labels) label.textContent = "正在本机核算…";
        try {
            const response = await fetch("/history-estimate?" + query, {cache:"no-store"});
            if (!response.ok) throw Error();
            const estimates = await response.json();
            if (request !== version) return;
            for (const label of labels) label.textContent = estimates[label.dataset.estimate] || "暂无估算";
        } catch {
            if (request === version) for (const label of labels) label.textContent = "请点开始补抓";
        }
    });
}

function totalLabel(cost, unknown) {
    if (unknown && Number(cost) === 0) return "暂无法估算";
    return "约 " + cost + " 元" + (unknown ? "（另有 " + unknown + " 个待估）" : "");
}

function protectConfirmation(form) {
    let submitting = false;
    const submit = form.querySelector("button:not([type=button])");
    form.addEventListener("submit", event => {
        form.elements.force_ack.value = "";
        if (submitting) {
            event.preventDefault();
            return;
        }
        if (form.querySelector("[data-repeat]:checked, input[type=hidden][data-repeat]")) {
            if (!window.confirm("包含已抓取过的重复时间段，确定要重复付费重拉吗？")) {
                event.preventDefault();
                return;
            }
            form.elements.force_ack.value = "yes";
        }
        submitting = true;
        submit.disabled = true;
    });
    window.addEventListener("pageshow", () => {
        submitting = false;
        submit.disabled = false;
        form.elements.force_ack.value = "";
    });
}

const dialog = document.querySelector("#history-dialog");
if (historyForm && dialog && typeof dialog.showModal === "function" && typeof fetch === "function") {
    const form = dialog.querySelector("form");
    const launch = historyForm.querySelector("button");
    const notice = historyForm.querySelector("[data-preview-error]");
    protectConfirmation(form);
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => launch.focus());

    function showPlan(plan, view) {
        dialog.querySelector("h2").textContent = view.period;
        const table = dialog.querySelector("tbody");
        const fields = form.querySelector("[data-choices]");
        table.replaceChildren();
        fields.replaceChildren();
        view.rows.forEach((row, index) => {
            const option = row.choices[0];
            const line = table.insertRow();
            const account = line.insertCell();
            account.textContent = row.region && view.rows.filter(r => r.region === row.region).length === 1 ? row.region : row.name;
            account.title = row.name;
            line.insertCell().textContent = [row.badge, option.label].filter(Boolean).join("，");
            line.insertCell().textContent = option.price;
            const choice = document.createElement("input");
            choice.type = "hidden";
            choice.name = "choice_" + index;
            choice.value = option.key;
            if (option.repeat) choice.dataset.repeat = "";
            fields.append(choice);
        });
        form.elements.plan.value = plan;
        form.elements.force_ack.value = "";
        dialog.querySelector("[data-details]").href = "/history?" + new URLSearchParams({plan});
        dialog.querySelector("[data-summary]").textContent = "预计合计" + totalLabel(view.cost, view.unknown)
            + " · 当前余额 " + (view.balance === null ? "未知" : view.balance + " 元");
        dialog.querySelector("[data-short]").hidden = !view.short;
        dialog.showModal();
    }

    historyForm.addEventListener("submit", async event => {
        event.preventDefault();
        if (launch.disabled) return;
        launch.disabled = true;
        notice.hidden = true;
        try {
            const response = await fetch(historyForm.action, {
                method:"POST", headers:{Accept:"application/json"},
                body:new URLSearchParams(new FormData(historyForm))
            });
            const result = await response.json();
            if (!response.ok) throw Error(result.error || "本地估算失败，请再试一次。");
            showPlan(result.plan, result.view);
        } catch (error) {
            notice.textContent = error instanceof TypeError || error instanceof SyntaxError
                ? "未能读取本地估算，请刷新后再试。" : error.message;
            notice.hidden = false;
        } finally {
            launch.disabled = false;
        }
    });
}

const detailForm = document.querySelector("#history-confirm");
if (detailForm) {
    protectConfirmation(detailForm);
    function updateTotal() {
        let cents = 0;
        let unknown = 0;
        for (const choice of detailForm.querySelectorAll("input[type=radio]:checked")) {
            if (choice.dataset.cost === "") unknown++;
            else cents += Math.round(Number(choice.dataset.cost) * 100);
        }
        detailForm.querySelector("[data-total]").textContent = totalLabel((cents / 100).toFixed(2), unknown);
        const {balance, floor} = detailForm.dataset;
        detailForm.querySelector("[data-short]").hidden = !(balance !== "" && cents > 0
            && cents > Math.round((Number(balance) - Number(floor)) * 100));
    }
    detailForm.addEventListener("change", updateTotal);
    window.addEventListener("pageshow", updateTotal);
}

const newAccounts = document.querySelector("#new-accounts");
const addAccountBtn = document.querySelector("#add-account-row");
if (newAccounts && addAccountBtn) {
    let nextIndex = Number(newAccounts.dataset.nextIndex) || 1;
    const prototype = newAccounts.querySelector(".new-account-row").cloneNode(true);
    newAccounts.addEventListener("click", event => {
        if (event.target.closest(".remove-row")) event.target.closest(".new-account-row").remove();
    });
    addAccountBtn.addEventListener("click", () => {
        const row = prototype.cloneNode(true);
        const input = row.querySelector("input");
        input.name = "name_" + nextIndex++;
        input.value = "";
        newAccounts.append(row);
        input.focus();
    });
}

const removeAccountForm = document.querySelector("#remove-account");
if (removeAccountForm) {
    removeAccountForm.addEventListener("submit", event => {
        const name = event.submitter?.dataset.accountName;
        const confirmed = name && window.confirm("确定删除公众号「" + name + "」吗？");
        removeAccountForm.elements.ack.value = confirmed ? "yes" : "";
        if (!confirmed) event.preventDefault();
    });
}

// Carry the exact list URL and card position with this visit, including through a body save.
function rememberListPosition(event) {
    const link = event.target.closest("a[data-open-article]");
    if (!link) return;
    const card = link.closest("article");
    const url = new URL(link.href);
    url.hash = new URLSearchParams({
        list: location.pathname + location.search,
        entry: card.id,
        top: Math.round(card.getBoundingClientRect().top),
        y: Math.round(window.scrollY)
    }).toString();
    link.href = url.href;
}
for (const type of ["pointerdown", "click", "contextmenu"]) {
    document.addEventListener(type, rememberListPosition);
}

const position = new URLSearchParams(location.hash.slice(1));
const backLink = document.querySelector("[data-back-to-list]");
if (backLink && position.has("list")) {
    const list = new URL(position.get("list"), location.origin);
    if (list.origin === location.origin && ["/", "/target", "/topic", "/all"].includes(list.pathname)) {
        position.delete("list");
        list.hash = position.toString();
        backLink.href = list.href;
        backLink.textContent = list.pathname === "/all" ? "← 返回全部文章" : "← 返回筛选结果";
        document.querySelector("form[method=post]").action = location.href;
    }
} else if (!backLink && position.has("entry") && position.has("y")) {
    window.addEventListener("pageshow", () => {
        const card = document.getElementById(position.get("entry"));
        const target = card ? window.scrollY + card.getBoundingClientRect().top - Number(position.get("top"))
                            : Number(position.get("y"));
        if (Number.isFinite(target)) window.scrollTo(0, target);
        history.replaceState(history.state, "", location.pathname + location.search);
    }, {once:true});
}
