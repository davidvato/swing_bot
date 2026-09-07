document.addEventListener('DOMContentLoaded', () => {
    fetchConfig();
    fetchBudget();
    fetchMetrics();
    fetchTrades();
    fetchCryptoUniverse();
    fetchCryptoMetrics();
    fetchCryptoPrices();
    fetchCryptoTrades();
    // Auto-refresh crypto prices every 60 seconds
    setInterval(fetchCryptoPrices, 60000);
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

// ─── Tab Navigation ───────────────────────────────────────────────────────────

function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    document.getElementById(`tab-${tab}`).classList.add('active');
    document.getElementById(`pane-${tab}`).classList.add('active');
    // Auto-close sidebar on mobile when switching tabs
    closeSidebar();
}

// ─── Mobile Sidebar Toggle ───────────────────────────────────────────────────────────────────────

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    const btn     = document.getElementById('hamburger-btn');
    const isOpen  = sidebar.classList.toggle('open');
    overlay.classList.toggle('active', isOpen);
    btn.classList.toggle('open', isOpen);
    document.body.style.overflow = isOpen ? 'hidden' : '';
}

function closeSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    const btn     = document.getElementById('hamburger-btn');
    sidebar.classList.remove('open');
    overlay.classList.remove('active');
    btn.classList.remove('open');
    document.body.style.overflow = '';
}

// ─── Crypto Universe Sidebar ──────────────────────────────────────────────────

async function fetchCryptoUniverse() {
    try {
        const res = await fetch('/api/crypto/universe');
        const data = await res.json();
        const container = document.getElementById('crypto-tickers-container');
        const meta = document.getElementById('crypto-universe-meta');
        container.innerHTML = '';
        (data.active_universe || []).forEach(sym => {
            const tag = document.createElement('span');
            tag.className = 'tag crypto-tag';
            tag.textContent = sym.replace('/USD','');
            container.appendChild(tag);
        });
        if (data.last_updated) {
            meta.textContent = `Updated: ${new Date(data.last_updated).toLocaleDateString()}`;
        } else {
            meta.textContent = 'Fallback (CoinGecko pending)';
        }
    } catch (e) {
        console.error('fetchCryptoUniverse error:', e);
    }
}

// ─── Crypto KPIs ──────────────────────────────────────────────────────────────

async function fetchCryptoMetrics() {
    try {
        const res = await fetch('/api/crypto/metrics');
        const data = await res.json();
        const pnl = data.total_pnl || 0;
        const pnlEl = document.getElementById('crypto-kpi-pnl');
        pnlEl.textContent = formatCurrency(pnl);
        pnlEl.className = `kpi-value ${pnl >= 0 ? 'positive' : 'negative'}`;
        document.getElementById('crypto-kpi-winrate').textContent = formatPercent(data.win_rate);
        document.getElementById('crypto-winrate-details').textContent =
            `W: ${data.winning_trades} | L: ${data.losing_trades}`;
        document.getElementById('crypto-kpi-total').textContent = data.total_trades || 0;
    } catch (e) {
        console.error('fetchCryptoMetrics error:', e);
    }
}

// ─── Crypto Price Grid ────────────────────────────────────────────────────────

const CRYPTO_NAMES = {
    'BTC/USD': 'Bitcoin', 'ETH/USD': 'Ethereum', 'BNB/USD': 'BNB',
    'SOL/USD': 'Solana', 'XRP/USD': 'Ripple', 'DOGE/USD': 'Dogecoin',
    'ADA/USD': 'Cardano', 'AVAX/USD': 'Avalanche', 'LINK/USD': 'Chainlink',
    'DOT/USD': 'Polkadot', 'SHIB/USD': 'Shiba Inu', 'LTC/USD': 'Litecoin',
    'UNI/USD': 'Uniswap', 'BCH/USD': 'Bitcoin Cash', 'XLM/USD': 'Stellar',
};

async function fetchCryptoPrices() {
    try {
        // Fetch universe to know which symbols to display
        const uniRes = await fetch('/api/crypto/universe');
        const uniData = await uniRes.json();
        const symbols = uniData.active_universe || [];

        // Fetch prices cache
        const priceRes = await fetch('/api/crypto/prices');
        const priceData = await priceRes.json();
        const prices = priceData.prices || {};

        const grid = document.getElementById('crypto-price-grid');
        grid.innerHTML = '';

        symbols.forEach(sym => {
            const info = prices[sym] || {};
            const price = info.price;
            const change24h = info.change_24h;
            const rsi = info.rsi;
            const signal = info.signal;
            const shortName = sym.replace('/USD', '');
            const fullName = CRYPTO_NAMES[sym] || shortName;

            const changeClass = change24h >= 0 ? 'change-up' : 'change-down';
            const changeIcon = change24h >= 0 ? '▲' : '▼';
            const signalBadge = signal
                ? '<span class="signal-buy">BUY</span>'
                : '';
            const rsiColor = rsi !== null && rsi < 30 ? 'rsi-oversold' : 'rsi-normal';

            const card = document.createElement('div');
            card.className = 'crypto-price-card glass-panel';
            card.style.cursor = 'pointer';
            card.title = `Click to open ${shortName} chart`;
            card.innerHTML = `
                <div class="crypto-card-header">
                    <span class="crypto-symbol">${shortName}</span>
                    ${signalBadge}
                </div>
                <div class="crypto-card-name">${fullName}</div>
                <div class="crypto-price">${price !== null && price !== undefined ? formatCurrency(price) : '---'}</div>
                <div class="crypto-meta">
                    <span class="${changeClass}">
                        ${change24h !== null && change24h !== undefined ? `${changeIcon} ${Math.abs(change24h).toFixed(2)}%` : '---'}
                    </span>
                    <span class="${rsiColor}">RSI: ${rsi !== null && rsi !== undefined ? rsi.toFixed(1) : '---'}</span>
                </div>
            `;
            // Open crypto chart on card click
            card.addEventListener('click', () => openCryptoChartModal(shortName, price));
            grid.appendChild(card);
        });

        // If no symbols yet show placeholder cards
        if (symbols.length === 0) {
            grid.innerHTML = '<p class="text-center" style="color:var(--text-muted);padding:2rem;">Crypto universe loading... (first run may take a few minutes)</p>';
        }

        const lastUpdate = document.getElementById('crypto-last-update');
        if (priceData.last_updated) {
            lastUpdate.textContent = `Updated: ${new Date(priceData.last_updated).toLocaleTimeString()}`;
        } else {
            lastUpdate.textContent = 'Pending first bot cycle...';
        }
    } catch (e) {
        console.error('fetchCryptoPrices error:', e);
    }
}

// ─── Crypto Trade History ─────────────────────────────────────────────────────

async function fetchCryptoTrades() {
    try {
        const res = await fetch('/api/crypto/trades');
        const trades = await res.json();
        const tbody = document.getElementById('crypto-trades-tbody');
        if (!trades || trades.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9" class="text-center">No crypto trades yet.</td></tr>';
            return;
        }
        tbody.innerHTML = '';
        trades.forEach(t => {
            const pnl = t.pnl;
            const pnlClass = pnl === null ? '' : (pnl >= 0 ? 'positive' : 'negative');
            const typeBadge = t.trade_type === 'CRYPTO_BUY'
                ? '<span class="badge badge-buy">BUY</span>'
                : t.trade_type === 'CRYPTO_SELL_TP'
                    ? '<span class="badge badge-tp">TP</span>'
                    : t.trade_type === 'CRYPTO_SELL_SL'
                        ? '<span class="badge badge-sl">SL</span>'
                        : `<span class="badge badge-time">${t.trade_type}</span>`;

            const tr = document.createElement('tr');
            tr.style.cursor = 'pointer';
            tr.title = 'Click to open chart';
            tr.innerHTML = `
                <td>${formatDate(t.date)}</td>
                <td><strong>${t.ticker || '-'}</strong></td>
                <td>${typeBadge}</td>
                <td>${formatCurrency(t.notional)}</td>
                <td>${t.entry_price ? '$' + parseFloat(t.entry_price).toFixed(4) : '-'}</td>
                <td>${t.exit_price ? '$' + parseFloat(t.exit_price).toFixed(4) : '-'}</td>
                <td>${t.atr_at_entry ? parseFloat(t.atr_at_entry).toFixed(4) : '-'}</td>
                <td class="${pnlClass}">${pnl !== null ? formatCurrency(pnl) : '-'}</td>
                <td class="${pnlClass}">${t.pnl_pct !== null ? (t.pnl_pct * 100).toFixed(2) + '%' : '-'}</td>
            `;

            // Determine symbol and price for chart
            const sym = (t.ticker || '').replace('/USD', '');
            const chartPrice = t.entry_price ? parseFloat(t.entry_price) : null;
            tr.addEventListener('click', () => openCryptoChartModal(sym, chartPrice));
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('fetchCryptoTrades error:', e);
    }
}

// ─── Crypto Chart Modal ──────────────────────────────────────────────────────────────────────

/**
 * Opens the shared chart modal for a crypto pair.
 * @param {string} symbol  - Short symbol, e.g. 'BTC'
 * @param {number|null} entryPrice - Optional entry price for TP/SL levels
 */
async function openCryptoChartModal(symbol, entryPrice) {
    const modal     = document.getElementById('chart-modal');
    const title     = document.getElementById('modal-title');
    const loader    = document.getElementById('modal-loader');
    const container = document.getElementById('chart-container');

    title.textContent = `${symbol}/USD — Price Chart`;
    container.innerHTML = '';
    loader.style.display = 'block';
    modal.style.display = 'flex';

    const epParam = (entryPrice && entryPrice > 0) ? `?entry_price=${entryPrice}` : '';

    try {
        const res  = await fetch(`/api/crypto/chart/${symbol}${epParam}`);
        const data = await res.json();

        loader.style.display = 'none';

        const bars   = data.bars || [];
        const levels = data.levels || null;

        if (!bars.length) {
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
            upColor:      '#10b981',
            downColor:    '#ef4444',
            borderVisible: false,
            wickUpColor:  '#10b981',
            wickDownColor: '#ef4444'
        });
        candlestickSeries.setData(bars);

        // Entry price line (amber for crypto)
        if (entryPrice && entryPrice > 0) {
            candlestickSeries.createPriceLine({
                price: entryPrice,
                color: '#f59e0b',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: `Entry  $${entryPrice.toFixed(4)}`,
            });
        }

        // TP / SL lines
        if (levels) {
            candlestickSeries.createPriceLine({
                price: levels.tp,
                color: '#10b981',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dotted,
                axisLabelVisible: true,
                title: `TP +${levels.tp_pct.toFixed(0)}%  $${levels.tp.toFixed(4)}`,
            });
            candlestickSeries.createPriceLine({
                price: levels.sl,
                color: '#ef4444',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dotted,
                axisLabelVisible: true,
                title: `SL -${levels.sl_pct.toFixed(0)}%  $${levels.sl.toFixed(4)}`,
            });
        }

        currentChart.timeScale().fitContent();

    } catch (e) {
        loader.style.display = 'none';
        container.innerHTML = '<div class="text-center">Error loading crypto chart.</div>';
        console.error('openCryptoChartModal error:', e);
    }
}

