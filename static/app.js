document.addEventListener('DOMContentLoaded', () => {
    fetchConfig();
    fetchBudget();
    fetchMetrics();
    fetchTrades();
});

const formatCurrency = (val) => {
    if (val === null || val === undefined) return '-';
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val);
};

const formatPercent = (val) => {
    if (val === null || val === undefined) return '-';
    return (val).toFixed(2) + '%';
};

const formatDate = (dateStr) => {
    if (!dateStr) return '-';
    const d = new Date(dateStr);
    return d.toLocaleString();
};

async function fetchConfig() {
    try {
        const res = await fetch('/api/config');
        const data = await res.json();
        
        // Render Universe Tags
        const tagsContainer = document.getElementById('tickers-container');
        tagsContainer.innerHTML = '';
        data.Universe.forEach(ticker => {
            const span = document.createElement('span');
            span.className = 'tag';
            span.textContent = ticker;
            tagsContainer.appendChild(span);
        });

        // Render Config List
        const configList = document.getElementById('config-list');
        configList.innerHTML = `
            <li><strong>Kelly Fraction</strong> ${data['Kelly Fraction']}</li>
            <li><strong>Max Position</strong> ${data['Max Position %']}</li>
            <li><strong>Take Profit</strong> <span class="positive">${data['Take Profit']}</span></li>
            <li><strong>Stop Loss</strong> <span class="negative">${data['Stop Loss']}</span></li>
            <li><strong>Max Hold</strong> ${data['Max Hold Days']} Days</li>
        `;
    } catch (error) {
        console.error('Error fetching config:', error);
        document.getElementById('config-list').innerHTML = '<li>Error loading config</li>';
    }
}

async function fetchBudget() {
    try {
        const res = await fetch('/api/budget');
        const data = await res.json();
        
        if (data.error) {
            document.getElementById('kpi-equity').textContent = 'Error';
            document.getElementById('equity-source').textContent = data.error;
            return;
        }

        document.getElementById('kpi-equity').textContent = formatCurrency(data.equity);
        document.getElementById('kpi-buyingpower').textContent = formatCurrency(data.buying_power);
        document.getElementById('equity-source').textContent = `Source: ${data.source}`;
    } catch (error) {
        console.error('Error fetching budget:', error);
    }
}

async function fetchMetrics() {
    try {
        const res = await fetch('/api/metrics');
        const data = await res.json();

        const pnlEl = document.getElementById('kpi-pnl');
        pnlEl.textContent = formatCurrency(data.total_pnl);
        if (data.total_pnl > 0) pnlEl.classList.add('positive');
        if (data.total_pnl < 0) pnlEl.classList.add('negative');

        const winRateEl = document.getElementById('kpi-winrate');
        winRateEl.textContent = data.win_rate.toFixed(1) + '%';
        if (data.win_rate >= 50) winRateEl.classList.add('positive');
        else winRateEl.classList.add('negative');

        document.getElementById('winrate-details').textContent = `W: ${data.winning_trades} | L: ${data.losing_trades}`;
    } catch (error) {
        console.error('Error fetching metrics:', error);
    }
}

async function fetchTrades() {
    try {
        const res = await fetch('/api/trades');
        const groups = await res.json();

        const tbody = document.getElementById('trades-tbody');
        tbody.innerHTML = '';

        if (groups.length === 0) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center">No trades found.</td></tr>';
            return;
        }

        groups.forEach(trade => {
            const tr = document.createElement('tr');
            const isClosed = trade.status === 'CLOSED';

            // ── Status badge ────────────────────────────────────────────────
            let badgeClass, badgeLabel;
            if (!isClosed) {
                badgeClass = 'trade-type buy';
                badgeLabel = '● OPEN';
            } else {
                const st = (trade.sell_type || '').toLowerCase();
                if (st.includes('tp'))       { badgeClass = 'trade-type sell-tp'; badgeLabel = '✔ SELL_TP'; }
                else if (st.includes('sl'))  { badgeClass = 'trade-type sell-sl'; badgeLabel = '✘ SELL_SL'; }
                else if (st.includes('eow')) { badgeClass = 'trade-type sell';    badgeLabel = '⏰ SELL_EOW'; }
                else if (st.includes('5d'))  { badgeClass = 'trade-type sell';    badgeLabel = '📅 SELL_5D'; }
                else                         { badgeClass = 'trade-type sell';    badgeLabel = trade.sell_type || 'CLOSED'; }
            }

            // ── P&L ─────────────────────────────────────────────────────────
            let pnlHTML    = '<span class="muted">—</span>';
            let pnlPctHTML = '<span class="muted">—</span>';
            if (trade.pnl !== null && trade.pnl !== undefined) {
                const c = trade.pnl >= 0 ? 'positive' : 'negative';
                pnlHTML    = `<span class="${c}">${formatCurrency(trade.pnl)}</span>`;
                pnlPctHTML = `<span class="${c}">${formatPercent((trade.pnl_pct || 0) * 100)}</span>`;
            }

            // ── Duration ────────────────────────────────────────────────────
            const durHTML = (trade.duration_days !== null && trade.duration_days !== undefined)
                ? `${trade.duration_days}d`
                : '<span class="muted">—</span>';

            // ── Exit date ───────────────────────────────────────────────────
            const exitDateHTML = trade.exit_date
                ? trade.exit_date
                : '<span class="muted">in progress…</span>';

            // ── Row click ───────────────────────────────────────────────────
            const chartPrice = isClosed ? trade.exit_price : trade.entry_price;
            const chartType  = isClosed ? (trade.sell_type || 'SELL') : 'BUY';

            tr.className = isClosed ? 'row-closed' : 'row-open';
            tr.style.cursor = 'pointer';
            tr.title = 'Click to open chart';
            tr.addEventListener('click', () =>
                openChartModal(trade.ticker, trade.entry_date, chartPrice, chartType, trade.entry_price)
            );

            tr.innerHTML = `
                <td class="date-cell">${trade.entry_date}</td>
                <td class="date-cell">${exitDateHTML}</td>
                <td><strong>${trade.ticker}</strong></td>
                <td><span class="${badgeClass}">${badgeLabel}</span></td>
                <td>${formatCurrency(trade.entry_price)}</td>
                <td>${formatCurrency(trade.exit_price)}</td>
                <td class="mono">${(trade.qty || 0).toFixed(4)}</td>
                <td class="mono">${durHTML}</td>
                <td>${pnlHTML}</td>
                <td>${pnlPctHTML}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (error) {
        console.error('Error fetching trades:', error);
        document.getElementById('trades-tbody').innerHTML =
            '<tr><td colspan="10" class="text-center text-danger">Error loading trades.</td></tr>';
    }
}

// Global chart instance to destroy on close
let currentChart = null;

async function openChartModal(ticker, date, actionPrice, type, entryPrice) {
    const modal = document.getElementById('chart-modal');
    const title = document.getElementById('modal-title');
    const loader = document.getElementById('modal-loader');
    const container = document.getElementById('chart-container');
    
    title.textContent = `${ticker} — ${type}`;
    container.innerHTML = ''; // Clear previous chart
    loader.style.display = 'block';
    modal.style.display = 'flex';
    
    // entryPrice is always the BUY price; use it to request TP/SL from the API
    const epParam = (entryPrice && entryPrice > 0) ? `&entry_price=${entryPrice}` : '';
    
    try {
        const res = await fetch(`/api/chart/${ticker}?_t=${Date.now()}${epParam}`);
        const data = await res.json();
        
        loader.style.display = 'none';
        
        // New API returns { bars, levels }; gracefully handle old array format too
        const bars = Array.isArray(data) ? data : data.bars;
        const levels = Array.isArray(data) ? null : data.levels;
        
        if (!bars || !bars.length) {
            container.innerHTML = '<div class="text-center">No chart data available.</div>';
            return;
        }

        const chartOptions = { 
            layout: { 
                textColor: '#f0f2f5', 
                background: { type: 'solid', color: 'transparent' } 
            },
            grid: {
                vertLines: { color: 'rgba(255,255,255,0.05)' },
                horzLines: { color: 'rgba(255,255,255,0.05)' }
            }
        };
        
        currentChart = LightweightCharts.createChart(container, chartOptions);
        const candlestickSeries = currentChart.addCandlestickSeries({
            upColor: '#10b981', 
            downColor: '#ef4444', 
            borderVisible: false, 
            wickUpColor: '#10b981', 
            wickDownColor: '#ef4444'
        });
        
        candlestickSeries.setData(bars);
        
        // ── Entry / Action price line (BUY=blue, SELL variants=amber) ──────────
        const isBuy = type.toLowerCase() === 'buy';
        const actionColor = isBuy ? '#3b82f6' : '#f59e0b';
        candlestickSeries.createPriceLine({
            price: actionPrice,
            color: actionColor,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: isBuy ? `BUY  $${actionPrice.toFixed(2)}` : `${type}  $${actionPrice.toFixed(2)}`,
        });

        // ── TP / SL lines (only when entry_price was available) ──────────────
        if (levels) {
            // Take-Profit line — green
            candlestickSeries.createPriceLine({
                price: levels.tp,
                color: '#10b981',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dotted,
                axisLabelVisible: true,
                title: `TP +${levels.tp_pct.toFixed(0)}%  $${levels.tp.toFixed(2)}`,
            });
            // Stop-Loss line — red
            candlestickSeries.createPriceLine({
                price: levels.sl,
                color: '#ef4444',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dotted,
                axisLabelVisible: true,
                title: `SL -${levels.sl_pct.toFixed(0)}%  $${levels.sl.toFixed(2)}`,
            });
        }

        currentChart.timeScale().fitContent();
        
    } catch (e) {
        loader.style.display = 'none';
        container.innerHTML = '<div class="text-center text-danger">Error loading chart.</div>';
        console.error(e);
    }
}

function closeChartModal() {
    document.getElementById('chart-modal').style.display = 'none';
    if (currentChart) {
        currentChart.remove();
        currentChart = null;
    }
}
