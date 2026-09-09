document.addEventListener('DOMContentLoaded', () => {
    fetchConfig();
    fetchBudget();
    fetchMetrics();
    fetchTrades();
    // fetchCryptoUniverse();
    // fetchCryptoMetrics();
    // fetchCryptoPrices();
    // fetchCryptoTrades();
    // Auto-refresh crypto prices every 60 seconds
    // setInterval(fetchCryptoPrices, 60000);
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

        const openTbody = document.getElementById('trades-open-tbody');
        const closedTbody = document.getElementById('trades-closed-tbody');
        openTbody.innerHTML = '';
        closedTbody.innerHTML = '';

        if (groups.length === 0) {
            openTbody.innerHTML = '<tr><td colspan="9" class="text-center">No open positions.</td></tr>';
            closedTbody.innerHTML = '<tr><td colspan="10" class="text-center">No closed trades found.</td></tr>';
            return;
        }

        let openCount = 0;
        let closedCount = 0;

        groups.forEach(trade => {
            const tr = document.createElement('tr');
            const isClosed = trade.status === 'CLOSED';

            let badgeClass, badgeLabel;
            if (!isClosed) {
                badgeClass = 'trade-type buy';
                badgeLabel = 'ΓùÅ OPEN';
            } else {
                const st = (trade.sell_type || '').toLowerCase();
                if (st.includes('tp'))         { badgeClass = 'trade-type sell-tp';     badgeLabel = 'Γ£ö SELL_TP'; }
                else if (st.includes('sl'))    { badgeClass = 'trade-type sell-sl';     badgeLabel = 'Γ£ÿ SELL_SL'; }
                else if (st.includes('eow'))   { badgeClass = 'trade-type sell';        badgeLabel = 'ΓÅ░ SELL_EOW'; }
                else if (st.includes('5d'))    { badgeClass = 'trade-type sell';        badgeLabel = '≡ƒôà SELL_5D'; }
                else if (st.includes('manual')){ badgeClass = 'trade-type sell-manual'; badgeLabel = '≡ƒöÆ MANUAL'; }
                else                           { badgeClass = 'trade-type sell';        badgeLabel = trade.sell_type || 'CLOSED'; }
            }

            let pnlHTML    = '<span class="muted">ΓÇö</span>';
            let pnlPctHTML = '<span class="muted">ΓÇö</span>';
            if (trade.pnl !== null && trade.pnl !== undefined) {
                const c = trade.pnl >= 0 ? 'positive' : 'negative';
                pnlHTML    = `<span class="${c}">${formatCurrency(trade.pnl)}</span>`;
                pnlPctHTML = `<span class="${c}">${formatPercent((trade.pnl_pct || 0) * 100)}</span>`;
            }

            const durHTML = (trade.duration_days !== null && trade.duration_days !== undefined)
                ? `${trade.duration_days}d`
                : '<span class="muted">ΓÇö</span>';

            const exitDateHTML = trade.exit_date
                ? trade.exit_date
                : '<span class="muted">in progressΓÇª</span>';

            const chartPrice = isClosed ? trade.exit_price : trade.entry_price;
            const chartType  = isClosed ? (trade.sell_type || 'SELL') : 'BUY';

            tr.className = isClosed ? 'row-closed' : 'row-open';
            tr.style.cursor = 'pointer';
            tr.title = 'Click to open chart';
            tr.addEventListener('click', () =>
                openChartModal(trade.ticker, trade.entry_date, chartPrice, chartType, trade.entry_price)
            );

            if (isClosed) {
                tr.innerHTML = `
                    <td class="date-cell">${trade.entry_date}</td>
                    <td class="date-cell">${exitDateHTML}</td>
                    <td><strong>${trade.ticker}</strong></td>
                    <td><span class="${badgeClass}">${badgeLabel}</span></td>
                    <td>${formatCurrency(trade.entry_price)}</td>
                    <td>${formatCurrency(trade.exit_price)}</td>
                    <td class="text-green">${trade.target_tp ? formatCurrency(trade.target_tp) : '-'}</td>
                    <td class="text-red">${trade.target_sl ? formatCurrency(trade.target_sl) : '-'}</td>
                    <td class="mono">${(trade.qty || 0).toFixed(4)}</td>
                    <td class="mono">${durHTML}</td>
                    <td>${pnlHTML}</td>
                    <td>${pnlPctHTML}</td>
                `;
                closedTbody.appendChild(tr);
                closedCount++;
            } else {
                tr.innerHTML = `
                    <td class="date-cell">${trade.entry_date}</td>
                    <td><strong>${trade.ticker}</strong></td>
                    <td><span class="${badgeClass}">${badgeLabel}</span></td>
                    <td>${formatCurrency(trade.entry_price)}</td>
                    <td><span class="muted">ΓÇö</span></td>
                    <td class="text-green">${trade.target_tp ? formatCurrency(trade.target_tp) : '-'}</td>
                    <td class="text-red">${trade.target_sl ? formatCurrency(trade.target_sl) : '-'}</td>
                    <td class="mono">${(trade.qty || 0).toFixed(4)}</td>
                    <td class="mono"><span class="muted">ΓÇö</span></td>
                    <td>${pnlHTML}</td>
                    <td>${pnlPctHTML}</td>
                    <td>
                        <button class="btn-close-trade" onclick="event.stopPropagation(); openCloseModal('equity', '${trade.ticker}', ${trade.buy_id ?? 'null'}, ${trade.qty || 0}, ${trade.entry_price || 0})">
                            &#x26A1; Cerrar
                        </button>
                    </td>
                `;
                openTbody.appendChild(tr);
                openCount++;
            }
        });

        if (openCount === 0) {
            openTbody.innerHTML = '<tr><td colspan="12" class="text-center">No open positions.</td></tr>';
        }
        if (closedCount === 0) {
            closedTbody.innerHTML = '<tr><td colspan="12" class="text-center">No closed trades found.</td></tr>';
        }

    } catch (error) {
        console.error('Error fetching trades:', error);
        document.getElementById('trades-open-tbody').innerHTML = '<tr><td colspan="9" class="text-center text-danger">Error loading trades.</td></tr>';
        document.getElementById('trades-closed-tbody').innerHTML = '<tr><td colspan="10" class="text-center text-danger">Error loading trades.</td></tr>';
    }
}

// Global chart instance to destroy on close
let currentChart = null;

async function openChartModal(ticker, date, actionPrice, type, entryPrice) {
    const modal = document.getElementById('chart-modal');
    const title = document.getElementById('modal-title');
    const loader = document.getElementById('modal-loader');
    const container = document.getElementById('chart-container');
    
    title.textContent = `${ticker} ΓÇö ${type}`;
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
        
        // ΓöÇΓöÇ Entry / Action price line (BUY=blue, SELL variants=amber) ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
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

        // ΓöÇΓöÇ TP / SL lines (only when entry_price was available) ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
        if (levels) {
            // Take-Profit line ΓÇö green
            candlestickSeries.createPriceLine({
                price: levels.tp,
                color: '#10b981',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dotted,
                axisLabelVisible: true,
                title: `TP +${levels.tp_pct.toFixed(0)}%  $${levels.tp.toFixed(2)}`,
            });
            // Stop-Loss line ΓÇö red
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

// ΓöÇΓöÇΓöÇ Tab Navigation ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    document.getElementById(`tab-${tab}`).classList.add('active');
    document.getElementById(`pane-${tab}`).classList.add('active');
    // Auto-close sidebar on mobile when switching tabs
    closeSidebar();
}

// ΓöÇΓöÇΓöÇ Mobile Sidebar Toggle ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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

// ΓöÇΓöÇΓöÇ Crypto Universe Sidebar ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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

// ΓöÇΓöÇΓöÇ Crypto KPIs ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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

// ΓöÇΓöÇΓöÇ Crypto Price Grid ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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
            const changeIcon = change24h >= 0 ? 'Γû▓' : 'Γû╝';
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

// ΓöÇΓöÇΓöÇ Crypto Trade History ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

async function fetchCryptoTrades() {
    try {
        const res = await fetch('/api/crypto/trades');
        const trades = await res.json();
        const openTbody = document.getElementById('crypto-trades-open-tbody');
        const closedTbody = document.getElementById('crypto-trades-closed-tbody');
        openTbody.innerHTML = '';
        closedTbody.innerHTML = '';

        if (!trades || trades.length === 0) {
            openTbody.innerHTML = '<tr><td colspan="9" class="text-center">No open positions.</td></tr>';
            closedTbody.innerHTML = '<tr><td colspan="10" class="text-center">No crypto trades yet.</td></tr>';
            return;
        }

        let openCount = 0;
        let closedCount = 0;

        trades.forEach(t => {
            const isClosed = t.status === 'CLOSED';
            const pnl = t.pnl;
            const pnlClass = pnl === null ? '' : (pnl >= 0 ? 'positive' : 'negative');
            let typeBadge;
            if (!isClosed) {
                typeBadge = '<span class="trade-type buy">ΓùÅ OPEN</span>';
            } else {
                // Correctly map crypto sell types to badges
                const st = (t.sell_type || '').toUpperCase();
                if (st.includes('TP')) {
                    typeBadge = '<span class="trade-type sell-tp">Γ£ö SELL_TP</span>';
                } else if (st.includes('SL')) {
                    typeBadge = '<span class="trade-type sell-sl">Γ£ÿ SELL_SL</span>';
                } else if (st.includes('TIME') || st.includes('72H') || st.includes('EOW')) {
                    typeBadge = `<span class="trade-type sell">ΓÅ░ ${t.sell_type}</span>`;
                } else if (st.includes('MANUAL')) {
                    typeBadge = '<span class="trade-type sell-manual">≡ƒöÆ MANUAL</span>';
                } else {
                    typeBadge = `<span class="trade-type sell">${t.sell_type || 'CLOSED'}</span>`;
                }
            }

            const tr = document.createElement('tr');
            tr.style.cursor = 'pointer';
            tr.title = 'Click to open chart';

            if (isClosed) {
                tr.innerHTML = `
                    <td class="date-cell">${t.entry_date}</td>
                    <td class="date-cell">${t.exit_date}</td>
                    <td><strong>${t.ticker || '-'}</strong></td>
                    <td>${typeBadge}</td>
                    <td>${formatCurrency(t.notional)}</td>
                    <td>${t.entry_price ? '$' + parseFloat(t.entry_price).toFixed(4) : '-'}</td>
                    <td>${t.exit_price ? '$' + parseFloat(t.exit_price).toFixed(4) : '-'}</td>
                    <td class="text-green">${t.target_tp ? '$' + parseFloat(t.target_tp).toFixed(4) : '-'}</td>
                    <td class="text-red">${t.target_sl ? '$' + parseFloat(t.target_sl).toFixed(4) : '-'}</td>
                    <td>${t.atr_at_entry ? parseFloat(t.atr_at_entry).toFixed(4) : '-'}</td>
                    <td class="${pnlClass}">${pnl !== null ? formatCurrency(pnl) : '-'}</td>
                    <td class="${pnlClass}">${t.pnl_pct !== null ? (t.pnl_pct * 100).toFixed(2) + '%' : '-'}</td>
                `;
                const sym = (t.ticker || '').replace('/USD', '');
                const chartPrice = t.exit_price ? parseFloat(t.exit_price) : null;
                tr.addEventListener('click', () => openCryptoChartModal(sym, t.entry_price ? parseFloat(t.entry_price) : null));
                closedTbody.appendChild(tr);
                closedCount++;
            } else {
                tr.innerHTML = `
                    <td class="date-cell">${t.entry_date}</td>
                    <td><strong>${t.ticker || '-'}</strong></td>
                    <td>${typeBadge}</td>
                    <td>${formatCurrency(t.notional)}</td>
                    <td>${t.entry_price ? '$' + parseFloat(t.entry_price).toFixed(4) : '-'}</td>
                    <td><span class="muted">ΓÇö</span></td>
                    <td class="text-green">${t.target_tp ? '$' + parseFloat(t.target_tp).toFixed(4) : '-'}</td>
                    <td class="text-red">${t.target_sl ? '$' + parseFloat(t.target_sl).toFixed(4) : '-'}</td>
                    <td>${t.atr_at_entry ? parseFloat(t.atr_at_entry).toFixed(4) : '-'}</td>
                    <td class="${pnlClass}">${pnl !== null ? formatCurrency(pnl) : '-'}</td>
                    <td class="${pnlClass}">${t.pnl_pct !== null ? (t.pnl_pct * 100).toFixed(2) + '%' : '-'}</td>
                    <td>
                        <button class="btn-close-trade" onclick="event.stopPropagation(); openCloseModal('crypto', '${t.ticker || ''}', null, ${t.qty || 0}, ${t.entry_price || 0})">
                            &#x26A1; Cerrar
                        </button>
                    </td>
                `;
                const sym = (t.ticker || '').replace('/USD', '');
                const chartPrice = t.entry_price ? parseFloat(t.entry_price) : null;
                tr.addEventListener('click', () => openCryptoChartModal(sym, chartPrice));
                openTbody.appendChild(tr);
                openCount++;
            }
        });

        if (openCount === 0) {
            openTbody.innerHTML = '<tr><td colspan="12" class="text-center">No open positions.</td></tr>';
        }
        if (closedCount === 0) {
            closedTbody.innerHTML = '<tr><td colspan="12" class="text-center">No crypto trades yet.</td></tr>';
        }
    } catch (e) {
        console.error('fetchCryptoTrades error:', e);
    }
}

// ΓöÇΓöÇΓöÇ Crypto Chart Modal ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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

    title.textContent = `${symbol}/USD ΓÇö Price Chart`;
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

// ΓöÇΓöÇΓöÇ Manual Close Trade Modal ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

// State of the pending close operation
let pendingClose = null;

/**
 * Opens the close-trade confirmation modal.
 * @param {'equity'|'crypto'} type - The asset type.
 * @param {string} ticker - Symbol (e.g. 'AAPL' or 'BTC/USD').
 * @param {number|null} buyId - DB buy row ID (equity only).
 * @param {number} qty - Quantity to sell.
 * @param {number} entryPrice - Original buy price.
 */
function openCloseModal(type, ticker, buyId, qty, entryPrice) {
    pendingClose = { type, ticker, buyId, qty, entryPrice };

    document.getElementById('close-trade-ticker').textContent = ticker;
    document.getElementById('close-trade-qty').textContent = qty.toFixed(type === 'crypto' ? 8 : 4);
    document.getElementById('close-trade-entry').textContent = formatCurrency(entryPrice);

    // Reset result area
    const resultEl = document.getElementById('close-trade-result');
    resultEl.className = 'close-trade-result hidden';
    resultEl.textContent = '';

    // Reset button
    const btn = document.getElementById('close-trade-confirm-btn');
    btn.disabled = false;
    btn.innerHTML = '&#x26A1; Confirmar Cierre';

    document.getElementById('close-trade-modal').style.display = 'flex';
}

function cancelCloseTrade() {
    pendingClose = null;
    document.getElementById('close-trade-modal').style.display = 'none';
}

async function confirmCloseTrade() {
    if (!pendingClose) return;

    const btn = document.getElementById('close-trade-confirm-btn');
    btn.disabled = true;
    btn.textContent = 'ΓÅ│ Ejecutando...';

    const { type, ticker, buyId, qty, entryPrice } = pendingClose;
    const endpoint = type === 'crypto' ? '/api/crypto/close-trade' : '/api/close-trade';
    const body = type === 'crypto'
        ? { ticker, qty, entry_price: entryPrice }
        : { ticker, buy_id: buyId, qty, entry_price: entryPrice };

    const resultEl = document.getElementById('close-trade-result');

    try {
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();

        if (res.ok && data.success) {
            const pnl = data.pnl ?? 0;
            const pnlPct = data.pnl_pct ?? 0;
            const pnlSign = pnl >= 0 ? '+' : '';
            resultEl.className = 'close-trade-result success';
            resultEl.innerHTML =
                `Γ£à Cerrado a <strong>${formatCurrency(data.exit_price)}</strong> &nbsp;|&nbsp; ` +
                `P&L: <strong>${pnlSign}${formatCurrency(pnl)}</strong> ` +
                `(${pnlSign}${(pnlPct * 100).toFixed(2)}%)`;

            // Auto-refresh all tables and KPIs after 1.8s
            setTimeout(() => {
                cancelCloseTrade();
                fetchTrades();
                fetchMetrics();
                fetchBudget();
                // fetchCryptoTrades();
                // fetchCryptoMetrics();
            }, 1800);
        } else {
            resultEl.className = 'close-trade-result error';
            resultEl.textContent = `Γ¥î Error: ${data.detail || data.error || 'Error desconocido'}`;
            btn.disabled = false;
            btn.innerHTML = '&#x26A1; Reintentar';
        }
    } catch (err) {
        resultEl.className = 'close-trade-result error';
        resultEl.textContent = `\u274C Error de conexi\u00F3n: ${err.message}`;
        btn.disabled = false;
        btn.innerHTML = '&#x26A1; Reintentar';
    }
}

// ─── Sync Alpaca → DB ──────────────────────────────────────────────────────

async function syncFromAlpaca() {
    const btn = document.getElementById('sync-btn');
    const icon = document.getElementById('sync-icon');
    const statusEl = document.getElementById('sync-status');
    btn.disabled = true;
    icon.textContent = '\u23F3';
    statusEl.textContent = 'Sincronizando...';
    try {
        const resp = await fetch('/api/sync', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            const eq = data.equities_inserted || 0;
            const cr = data.crypto_inserted || 0;
            const op = data.open_positions_inserted || 0;
            statusEl.textContent = '\u2705 Eq:' + eq + ' Cripto:' + cr + ' Pos:' + op;
            icon.textContent = '\u2705';
            fetchTrades();
            // fetchCryptoTrades();
        } else {
            statusEl.textContent = '\u274C ' + (data.detail || 'Error');
            icon.textContent = '\uD83D\uDD04';
        }
    } catch (err) {
        statusEl.textContent = '\u274C ' + err.message;
        icon.textContent = '\uD83D\uDD04';
    } finally {
        btn.disabled = false;
        setTimeout(function() {
            statusEl.textContent = '';
            icon.textContent = '\uD83D\uDD04';
        }, 8000);
    }
}
