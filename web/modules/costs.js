export function initCosts({ ws, state }) {
    const page = document.createElement('div');
    page.id = 'page-costs';
    page.className = 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            <h2>Costs</h2>
            <div class="spacer"></div>
            <button class="btn btn-default btn-sm" id="btn-refresh-costs">Refresh</button>
        </div>
        <div class="costs-scroll" style="overflow-y:auto;flex:1;padding:16px 20px">
            <div class="stat-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px">
                <div class="stat-card"><div class="stat-label">Total Spent</div><div class="stat-value" id="cost-total">$0.00</div></div>
                <div class="stat-card"><div class="stat-label">Total Calls</div><div class="stat-value" id="cost-calls">0</div></div>
                <div class="stat-card"><div class="stat-label">Top Model</div><div class="stat-value" id="cost-top-model" style="font-size:12px">-</div></div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px">
                <div class="stat-card"><div class="stat-label">Budget Mode</div><div class="stat-value" id="executor-budget-mode">normal</div></div>
                <div class="stat-card"><div class="stat-label">Codex Daily</div><div class="stat-value" id="executor-codex-daily">0 / 0</div></div>
                <div class="stat-card"><div class="stat-label">Claude Daily</div><div class="stat-value" id="executor-claude-daily">0 / 0</div></div>
            </div>
            <div style="display:grid;grid-template-columns:1fr;gap:12px;margin-bottom:16px">
                <div class="stat-card"><div class="stat-label">MiniMax 5h Window</div><div class="stat-value" id="minimax-5h-window">0 / 0</div></div>
                <div class="stat-card"><div class="stat-label">MiniMax Weekly Window</div><div class="stat-value" id="minimax-weekly-window">0 / 0</div></div>
            </div>
            <div style="margin-bottom:24px">
                <h3 style="font-size:14px;color:var(--text-secondary);margin:0 0 8px">External Worker Limits</h3>
                <table class="cost-table" id="executor-limits-table">
                    <thead>
                        <tr><th>Provider</th><th>Auth</th><th>5h Remaining</th><th>Weekly Remaining</th><th>Daily Remaining</th></tr>
                    </thead>
                    <tbody></tbody>
                </table>
                <div id="executor-limits-note" style="margin-top:8px;font-size:12px;color:var(--text-muted)"></div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
                <div>
                    <h3 style="font-size:14px;color:var(--text-secondary);margin:0 0 8px">By Model</h3>
                    <table class="cost-table" id="cost-by-model"><thead><tr><th>Model</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 style="font-size:14px;color:var(--text-secondary);margin:0 0 8px">By API Key</h3>
                    <table class="cost-table" id="cost-by-key"><thead><tr><th>Key</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 style="font-size:14px;color:var(--text-secondary);margin:0 0 8px">By Model Category</h3>
                    <table class="cost-table" id="cost-by-model-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 style="font-size:14px;color:var(--text-secondary);margin:0 0 8px">By Task Category</h3>
                    <table class="cost-table" id="cost-by-task-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
            </div>
        </div>
    `;
    document.getElementById('content').appendChild(page);

    function renderBreakdownTable(tableId, data, totals) {
        const tbody = document.querySelector('#' + tableId + ' tbody');
        tbody.innerHTML = '';
        const metric = totals.displayMetric || 'cost';
        const metricTotal = metric === 'calls' ? (totals.totalCalls || 0) : (totals.totalCost || 0);
        for (const [name, info] of Object.entries(data)) {
            const basis = metric === 'calls' ? (info.calls || 0) : (info.cost || 0);
            const pct = metricTotal > 0 ? (basis / metricTotal * 100) : 0;
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td style="font-size:12px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${name}">${name}</td>
                <td style="text-align:right">${info.calls}</td>
                <td style="text-align:right">$${info.cost.toFixed(3)}</td>
                <td style="width:60px"><div style="background:var(--accent);height:6px;border-radius:3px;width:${Math.min(100,pct)}%;opacity:0.7"></div></td>
            `;
            tbody.appendChild(tr);
        }
        if (Object.keys(data).length === 0) {
            const tr = document.createElement('tr');
            tr.innerHTML = '<td colspan="4" style="color:var(--text-muted);text-align:center">No data</td>';
            tbody.appendChild(tr);
        }
    }

    function renderExecutorLimits(data) {
        const codex = data.codex || {};
        const claude = data.claude || {};
        const provider = data.provider_window_limits || {};
        const tbody = document.querySelector('#executor-limits-table tbody');
        tbody.innerHTML = '';

        const rows = [
            {
                provider: 'Codex',
                auth: `${Boolean(codex.logged_in)} | ${codex.auth_method || 'unknown'}`,
                fiveHour: codex.five_hour_remaining == null ? 'N/A' : String(codex.five_hour_remaining),
                weekly: codex.weekly_remaining == null ? 'N/A' : String(codex.weekly_remaining),
                daily: `${codex.daily_remaining || 0} (${codex.daily_used || 0}/${codex.daily_cap || 0})`,
            },
            {
                provider: 'Claude',
                auth: `${Boolean(claude.logged_in)} | ${claude.auth_method || 'unknown'} | ${claude.subscription_type || 'unknown'}`,
                fiveHour: claude.five_hour_remaining == null ? 'N/A' : String(claude.five_hour_remaining),
                weekly: claude.weekly_remaining == null ? 'N/A' : String(claude.weekly_remaining),
                daily: `${claude.daily_remaining || 0} (${claude.daily_used || 0}/${claude.daily_cap || 0})`,
            },
        ];

        for (const row of rows) {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${row.provider}</td>
                <td style="font-size:12px">${row.auth}</td>
                <td>${row.fiveHour}</td>
                <td>${row.weekly}</td>
                <td>${row.daily}</td>
            `;
            tbody.appendChild(tr);
        }

        document.getElementById('executor-budget-mode').textContent = data.external_budget_mode || 'normal';
        document.getElementById('executor-codex-daily').textContent = `${codex.daily_used || 0} / ${codex.daily_cap || 0}`;
        document.getElementById('executor-claude-daily').textContent = `${claude.daily_used || 0} / ${claude.daily_cap || 0}`;
        document.getElementById('executor-limits-note').textContent = provider.note || '';
    }

    async function loadCosts() {
        try {
            const resp = await fetch('/api/cost-breakdown');
            const d = await resp.json();
            document.getElementById('cost-total').textContent = '$' + (d.total_cost || 0).toFixed(2);
            document.getElementById('cost-calls').textContent = d.total_calls || 0;
            document.getElementById('cost-top-model').textContent = d.top_model || '-';
            const minimaxLimit = d.minimax_requests_5h_limit || 0;
            const minimaxUsed = d.minimax_requests_5h_used || 0;
            const minimaxRemaining = d.minimax_requests_5h_remaining;
            document.getElementById('minimax-5h-window').textContent =
                minimaxLimit > 0 ? `${minimaxUsed} / ${minimaxLimit} (${minimaxRemaining} left)` : `${minimaxUsed} / unknown`;
            const minimaxWeeklyLimit = d.minimax_requests_weekly_limit || 0;
            const minimaxWeeklyUsed = d.minimax_requests_weekly_used || 0;
            const minimaxWeeklyRemaining = d.minimax_requests_weekly_remaining;
            document.getElementById('minimax-weekly-window').textContent =
                minimaxWeeklyLimit > 0 ? `${minimaxWeeklyUsed} / ${minimaxWeeklyLimit} (${minimaxWeeklyRemaining} left)` : `${minimaxWeeklyUsed} / unknown`;
            const totals = {
                totalCost: d.total_cost || 0,
                totalCalls: d.total_calls || 0,
                displayMetric: d.display_metric || 'cost',
            };
            renderBreakdownTable('cost-by-model', d.by_model || {}, totals);
            renderBreakdownTable('cost-by-key', d.by_api_key || {}, totals);
            renderBreakdownTable('cost-by-model-cat', d.by_model_category || {}, totals);
            renderBreakdownTable('cost-by-task-cat', d.by_task_category || {}, totals);
        } catch {}
    }

    async function loadExecutorStatus() {
        try {
            const resp = await fetch('/api/executor/status', { cache: 'no-store' });
            const d = await resp.json();
            if (!resp.ok) throw new Error(d.error || `HTTP ${resp.status}`);
            renderExecutorLimits(d);
        } catch (e) {
            const tbody = document.querySelector('#executor-limits-table tbody');
            tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-muted);text-align:center">Failed to load limits</td></tr>';
            document.getElementById('executor-limits-note').textContent = String(e.message || e);
        }
    }

    async function refreshAll() {
        await Promise.all([loadCosts(), loadExecutorStatus()]);
    }

    document.getElementById('btn-refresh-costs').addEventListener('click', refreshAll);

    const obs = new MutationObserver(() => {
        if (page.classList.contains('active')) refreshAll();
    });
    obs.observe(page, { attributes: true, attributeFilter: ['class'] });
}
