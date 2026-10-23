const state = {
    symbol: window.APP_CONFIG?.defaultSymbol || "AAPL",
    market: "us",
    range: "day",
    chartType: "line",
    chart: null,
    refreshTimer: null,
    overviewTick: 0,
    currentPrice: 0,
    sessionReady: false,
    marketSocket: null,
    socketRetryTimer: null,
    socketRetry: 1000,
    streamConnected: false,
    streamSymbol: null,
    lastStreamEventAt: 0,
    pendingOrderId: null,
    previewInFlight: false,
    orderRequestKey: null,
};

const themes = ["dark", "light", "graphite", "emerald"];
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const themeLabels = {
    dark: "Dark",
    light: "Light",
    graphite: "Graphite",
    emerald: "Emerald",
};

const presets = {
    crypto: [
        { symbol: "BTC-USD", name: "Bitcoin" },
        { symbol: "ETH-USD", name: "Ethereum" },
        { symbol: "SOL-USD", name: "Solana" },
        { symbol: "DOGE-USD", name: "Dogecoin" },
    ],
    india: [
        { symbol: "TCS.NS", name: "TCS" },
        { symbol: "RELIANCE.NS", name: "Reliance" },
        { symbol: "INFY.NS", name: "Infosys" },
        { symbol: "HDFCBANK.NS", name: "HDFC Bank" },
    ],
    us: [
        { symbol: "AAPL", name: "Apple" },
        { symbol: "MSFT", name: "Microsoft" },
        { symbol: "NVDA", name: "NVIDIA" },
        { symbol: "AMZN", name: "Amazon" },
    ],
    forex: [
        { symbol: "USDINR=X", name: "USD / INR" },
        { symbol: "EURUSD=X", name: "EUR / USD" },
        { symbol: "GBPUSD=X", name: "GBP / USD" },
        { symbol: "USDJPY=X", name: "USD / JPY" },
    ],
};

const elements = {
    body: document.body,
    marketOverlay: document.getElementById("marketOverlay"),
    portfolioLauncher: document.getElementById("portfolioLauncher"),
    portfolioDrawer: document.getElementById("portfolioDrawer"),
    closePortfolio: document.getElementById("closePortfolio"),
    portfolioRefresh: document.getElementById("portfolioRefresh"),
    portfolioPredict: document.getElementById("portfolioPredict"),
    portfolioPredictionText: document.getElementById("portfolioPredictionText"),
    portfolioPredictionRisk: document.getElementById("portfolioPredictionRisk"),
    portfolioPredictionAction: document.getElementById("portfolioPredictionAction"),
    portfolioDrawerInner: document.querySelector(".portfolio-drawer-inner"),
    themeToggle: document.getElementById("themeToggle"),
    themeLabel: document.getElementById("themeLabel"),
    statusMessage: document.getElementById("statusMessage"),
    streamIndicator: document.getElementById("streamIndicator"),
    stockName: document.getElementById("stockName"),
    stockPrice: document.getElementById("stockPrice"),
    stockChange: document.getElementById("stockChange"),
    riskLevel: document.getElementById("riskLevel"),
    currencyLabel: document.getElementById("currencyLabel"),
    trendMomentum: document.getElementById("trendMomentum"),
    trendSignal: document.getElementById("trendSignal"),
    trendHealth: document.getElementById("trendHealth"),
    heroMarket: document.getElementById("heroMarket"),
    heroExchange: document.getElementById("heroExchange"),
    assetMarketChip: document.getElementById("assetMarketChip"),
    updateBadge: document.getElementById("updateBadge"),
    insightText: document.getElementById("insightText"),
    insightRisk: document.getElementById("insightRisk"),
    beginnerTip: document.getElementById("beginnerTip"),
    insightSource: document.getElementById("insightSource"),
    newsList: document.getElementById("newsList"),
    tickerRowTop: document.getElementById("tickerRowTop"),
    tickerRowBottom: document.getElementById("tickerRowBottom"),
    symbolForm: document.getElementById("symbolForm"),
    symbolInput: document.getElementById("symbolInput"),
    portfolioForm: document.getElementById("portfolioForm"),
    portfolioSymbol: document.getElementById("portfolioSymbol"),
    portfolioQuantity: document.getElementById("portfolioQuantity"),
    totalInvestment: document.getElementById("totalInvestment"),
    currentValue: document.getElementById("currentValue"),
    profitLoss: document.getElementById("profitLoss"),
    portfolioList: document.getElementById("portfolioList"),
    marketChoices: document.querySelectorAll(".market-choice"),
    marketTabs: document.querySelectorAll(".market-tab"),
    rangeButtons: document.querySelectorAll(".range-btn"),
    chartButtons: document.querySelectorAll(".chart-btn"),
    addCurrentWatchlist: document.getElementById("addCurrentWatchlist"),
    watchlistForm: document.getElementById("watchlistForm"),
    watchlistSymbol: document.getElementById("watchlistSymbol"),
    watchlistMarket: document.getElementById("watchlistMarket"),
    watchlistRows: document.getElementById("watchlistRows"),
    orderForm: document.getElementById("orderForm"),
    orderSymbol: document.getElementById("orderSymbol"),
    orderMarket: document.getElementById("orderMarket"),
    orderSide: document.getElementById("orderSide"),
    orderType: document.getElementById("orderType"),
    orderQuantity: document.getElementById("orderQuantity"),
    orderLimitPrice: document.getElementById("orderLimitPrice"),
    limitPriceLabel: document.getElementById("limitPriceLabel"),
    orderPreview: document.getElementById("orderPreview"),
    orderPreviewText: document.getElementById("orderPreviewText"),
    orderConfirmCheck: document.getElementById("orderConfirmCheck"),
    confirmOrderButton: document.getElementById("confirmOrderButton"),
    dismissOrderButton: document.getElementById("dismissOrderButton"),
    orderMessage: document.getElementById("orderMessage"),
    orderRows: document.getElementById("orderRows"),
    refreshOrders: document.getElementById("refreshOrders"),
};

function formatCurrency(value, currency = "USD") {
    if (value == null || !Number.isFinite(Number(value))) return "--";
    return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency,
        maximumFractionDigits: currency === "USD" ? 2 : 2,
    }).format(value || 0);
}

function formatPrice(value, currency = "USD", market = "US") {
    if (market === "Forex") {
        return `${Number(value || 0).toFixed(4)} ${currency}`;
    }
    try {
        return formatCurrency(value, currency);
    } catch {
        return `${Number(value || 0).toFixed(2)} ${currency || "currency unavailable"}`;
    }
}

function formatPercent(value) {
    const number = Number(value || 0);
    return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function toneClass(value) {
    if (value > 0) return "profit";
    if (value < 0) return "loss";
    return "neutral";
}

function setTheme(theme) {
    document.body.setAttribute("data-theme", theme);
    localStorage.setItem("afip-theme", theme);
    if (elements.themeLabel) {
        elements.themeLabel.textContent = themeLabels[theme] || theme;
    }
}

function initTheme() {
    const saved = localStorage.getItem("afip-theme") || "dark";
    setTheme(themes.includes(saved) ? saved : "dark");
}

function setActiveButton(group, value, attr) {
    group.forEach((button) => {
        button.classList.toggle("active", button.dataset[attr] === value);
    });
}

function setMarketContext(market, symbol) {
    state.market = market;
    state.symbol = symbol;
    state.sessionReady = true;
    elements.symbolInput.value = symbol;
    setActiveButton(elements.marketTabs, market, "market");
    if (elements.orderSymbol) elements.orderSymbol.value = symbol;
    if (elements.orderMarket) elements.orderMarket.value = market;
    if (elements.watchlistSymbol) elements.watchlistSymbol.value = symbol;
    if (elements.watchlistMarket) elements.watchlistMarket.value = market;
}

function togglePortfolioDrawer(show) {
    elements.portfolioDrawer.classList.toggle("open", show);
    elements.portfolioLauncher.classList.toggle("active", show);
    document.body.style.overflow = show ? "hidden" : "";
    if (show && elements.portfolioDrawer) {
        elements.portfolioDrawer.scrollTop = 0;
        elements.portfolioDrawer.focus();
    }
}

function getChartColors() {
    const dark = document.body.getAttribute("data-theme") === "dark";
    return {
        line: dark ? "#6ee7ff" : "#1d4ed8",
        fill: dark ? "rgba(110, 231, 255, 0.12)" : "rgba(29, 78, 216, 0.12)",
        grid: dark ? "rgba(117, 139, 173, 0.12)" : "rgba(93, 116, 150, 0.14)",
        tick: dark ? "#9bb1d1" : "#53657f",
        up: "#38d39f",
        down: "#fb7185",
    };
}

function createDataset(data, chartType, colors) {
    if (chartType === "candlestick") {
        return {
            type: "candlestick",
            label: "Candles",
            data,
            color: { up: colors.up, down: colors.down, unchanged: "#94a3b8" },
            borderColor: { up: colors.up, down: colors.down, unchanged: "#94a3b8" },
        };
    }

    return {
        type: "line",
        data,
        borderColor: colors.line,
        backgroundColor: colors.fill,
        borderWidth: 2.5,
        pointRadius: 0,
        tension: 0.15,
        spanGaps: false,
        cubicInterpolationMode: "monotone",
        fill: true,
    };
}

function createOrUpdateChart(history) {
    if (typeof Chart === "undefined") {
        elements.statusMessage.textContent = "Chart library unavailable.";
        return;
    }

    const colors = getChartColors();
    const ctx = document.getElementById("stockChart");
    const timeUnit =
        state.range === "hour" ? "minute" :
        state.range === "day" ? "hour" :
        state.range === "week" ? "day" :
        state.range === "month" ? "week" :
        "month";
    const options = {
        parsing: false,
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 450, easing: "easeOutQuart" },
        plugins: { legend: { display: false } },
        scales: {
            x: {
                type: "time",
                time: { unit: timeUnit },
                grid: { display: false },
                ticks: { color: colors.tick },
            },
            y: {
                grid: { color: colors.grid },
                ticks: { color: colors.tick },
            },
        },
    };

    const buildChart = (chartType) => {
        const chartData = chartType === "candlestick" ? history.candles : history.points;
        const dataset = createDataset(chartData, chartType, colors);
        if (state.chart) {
            state.chart.destroy();
        }
        state.chart = new Chart(ctx, {
            type: dataset.type,
            data: { datasets: [dataset] },
            options,
        });
    };

    try {
        buildChart(state.chartType);
    } catch (error) {
        console.error("Chart mode failed, falling back to line", error);
        state.chartType = "line";
        setActiveButton(elements.chartButtons, state.chartType, "chart");
        elements.statusMessage.textContent = "Candlestick mode is unavailable here, so line mode is being used.";
        buildChart("line");
    }
}

function renderNews(newsItems) {
    if (!newsItems?.length) {
        elements.newsList.innerHTML = '<p class="empty-state">No headlines available.</p>';
        return;
    }
    elements.newsList.innerHTML = newsItems.map((item) => `
        <article class="news-card glass-inner">
            <div class="news-top">
                <a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer"><strong>${escapeHtml(item.headline)}</strong></a>
                <span>${escapeHtml(item.source || "News source")}</span>
            </div>
            <p>${escapeHtml(item.summary || "")}</p>
            <small>${item.published_at ? `Published ${escapeHtml(new Date(item.published_at).toLocaleString())}` : "Publication time unavailable"}</small>
        </article>
    `).join("");
}

function renderTicker(groups) {
    const topItems = [...groups.crypto, ...groups.india];
    const bottomItems = [...groups.us, ...groups.forex];
    const build = (items) => items.map((item) => `
        <button class="ticker-pill" type="button" data-symbol="${escapeHtml(item.symbol)}" data-market="${escapeHtml((item.market || "").toLowerCase())}">
            <div>
                <strong>${escapeHtml(item.symbol)}</strong>
                <span>${escapeHtml(item.name)}</span>
            </div>
            <div class="ticker-meta">
                <strong>${item.price == null ? "--" : formatPrice(item.price, item.currency || "USD", item.market || "US")}</strong>
                <span class="${toneClass(item.change_percent)}">${item.change_percent == null ? "Watch" : `${item.change == null ? "" : formatPrice(item.change, item.currency || "USD", item.market || "US") + " | "}${formatPercent(item.change_percent)}`}</span>
                <small>${escapeHtml(item.data_status || (item.price == null ? "unavailable" : "snapshot"))} · ${escapeHtml(item.freshness || "unknown")} · ${escapeHtml(item.source || "Source unavailable")}${item.event_timestamp ? ` · ${escapeHtml(new Date(item.event_timestamp).toLocaleString())}` : ""}</small>
            </div>
        </button>
    `).join("");

    elements.tickerRowTop.innerHTML = `${build(topItems)}${build(topItems)}`;
    elements.tickerRowBottom.innerHTML = `${build(bottomItems)}${build(bottomItems)}`;

    document.querySelectorAll(".ticker-pill").forEach((button) => {
        button.addEventListener("click", async () => {
            const market = button.dataset.market || state.market;
            setMarketContext(market, button.dataset.symbol);
            await loadAsset();
        });
    });
}

async function loadMarketOverview() {
    try {
        const response = await fetch("/get_market_overview");
        const result = await response.json();
        if (result.success) {
            renderTicker(result.data);
            return;
        }
    } catch (error) {
        console.error("Overview failed", error);
    }
    renderTicker(presets);
}

function renderInsight(insight) {
    elements.insightText.textContent = insight.summary || "No AI insight available.";
    elements.insightRisk.textContent = insight.risk_level || "--";
    elements.riskLevel.textContent = insight.risk_level || "--";
    elements.beginnerTip.textContent = insight.beginner_tip || "Observe first, then decide.";
    elements.insightSource.textContent = "Price history only";
}

function renderMoveAnalysis(move, technicalInsight) {
    if (!move) {
        renderInsight(technicalInsight || {});
        return;
    }
    elements.insightText.textContent = move.summary || "Not enough evidence to measure this move.";
    elements.insightRisk.textContent = technicalInsight?.risk_level || "--";
    elements.riskLevel.textContent = technicalInsight?.risk_level || "--";
    elements.beginnerTip.textContent = move.confidence || "Evidence unavailable";
    elements.insightSource.textContent = move.status === "measured_move" ? "Measured · price history" : "Insufficient evidence";
}

async function loadMacroDashboard() {
    const panel = document.getElementById("macroDashboard");
    if (!panel) return;
    try {
        const response = await fetch("/api/macro");
        const result = await response.json();
        if (!result.success) throw new Error("Macro source unavailable");
        panel.innerHTML = result.data.map((series) => {
            const latest = series.observations?.[0];
            return `<article class="glass-inner macro-card"><strong>${escapeHtml(series.name || series.series_id || "Series")}</strong><span>${latest ? `${escapeHtml(latest.value)} ${escapeHtml(latest.unit)}` : "Unavailable"}</span><small>${latest ? `Observation ${escapeHtml(latest.observation_date)} · ${escapeHtml(series.freshness || series.status)}` : escapeHtml(series.message || "No observation")}</small><small>${escapeHtml(series.source || "FRED")}</small></article>`;
        }).join("");
    } catch (error) {
        panel.innerHTML = '<p class="empty-state">Macro provider unavailable. Price and portfolio features remain available.</p>';
    }
}

async function loadAsset() {
    elements.statusMessage.textContent = `Analyzing ${state.symbol} in ${state.market.toUpperCase()}...`;
    try {
        const response = await fetch(`/get_stock_data?symbol=${encodeURIComponent(state.symbol)}&market=${encodeURIComponent(state.market)}&range=${encodeURIComponent(state.range)}`);
        const result = await response.json();
        if (!result.success) {
            elements.statusMessage.textContent = result.message || "Unable to fetch asset.";
            return;
        }

        const asset = result.data;
        state.symbol = asset.symbol;
        elements.orderSymbol.value = asset.symbol;
        elements.orderMarket.value = state.market;
        elements.watchlistSymbol.value = asset.symbol;
        elements.watchlistMarket.value = state.market;
        elements.stockName.textContent = `${asset.name} (${asset.symbol})`;
        elements.stockPrice.textContent = formatPrice(asset.price, asset.currency, asset.market);
        elements.stockChange.textContent = `${asset.change == null ? "" : formatPrice(asset.change, asset.currency, asset.market) + " | "}${formatPercent(asset.change_percent)}`;
        elements.stockChange.className = toneClass(asset.change_percent);
        elements.currencyLabel.textContent = asset.currency;
        elements.heroMarket.textContent = asset.market;
        elements.heroExchange.textContent = asset.exchange;
        elements.assetMarketChip.textContent = asset.market;
        elements.trendMomentum.textContent = asset.trend_status.momentum;
        elements.trendSignal.textContent = asset.trend_status.signal;
        elements.trendHealth.textContent = asset.trend_status.health;
        elements.updateBadge.textContent = "Snapshot";
        const sourceTime = asset.event_timestamp ? new Date(asset.event_timestamp).toLocaleString() : "time unavailable";
        const receivedTime = asset.last_received_at ? new Date(asset.last_received_at).toLocaleString() : "time unavailable";
        elements.statusMessage.textContent = `${asset.market} snapshot for ${asset.symbol}; source time ${sourceTime}, received ${receivedTime}. Stream status is shown separately.`;
        renderMoveAnalysis(asset.move_analysis, asset.insight);
        renderNews(asset.news);
        createOrUpdateChart(asset.history);
        updateStreamSubscription();
    } catch (error) {
        console.error("Load asset failed", error);
        elements.statusMessage.textContent = "Live request failed. Please try again.";
    }
}

loadMacroDashboard();

function streamSymbolForCurrentAsset() {
    const symbol = state.symbol.toUpperCase();
    if (symbol.endsWith("-USD")) return symbol;
    if (/^[A-Z]{1,10}$/.test(symbol) && state.market === "us") return symbol;
    return null;
}

function updateStreamSubscription() {
    const symbol = streamSymbolForCurrentAsset();
    if (state.marketSocket?.readyState !== WebSocket.OPEN) return;
    if (state.streamSymbol && state.streamSymbol !== symbol) {
        state.marketSocket.send(JSON.stringify({ action: "unsubscribe", symbols: [state.streamSymbol] }));
        state.streamSymbol = null;
    }
    if (symbol && state.streamSymbol !== symbol) {
        state.marketSocket.send(JSON.stringify({ action: "subscribe", symbols: [symbol] }));
        state.streamSymbol = symbol;
    }
}

function applyStreamEvent(event) {
    if (event.type !== "trade" || event.symbol !== state.symbol.toUpperCase() || !Number.isFinite(Number(event.price))) return;
    state.currentPrice = Number(event.price);
    elements.stockPrice.textContent = formatPrice(state.currentPrice, event.currency, state.market);
    elements.updateBadge.textContent = "Stream trade";
    elements.statusMessage.textContent = `Alpaca ${event.asset_class} trade for ${event.symbol}; event time ${event.timestamp || "unavailable"}.`;
    state.streamConnected = true;
    setStreamIndicator("connected");
    state.lastStreamEventAt = Date.now();
    if (state.chart && state.chart.config.type === "line") {
        const series = state.chart.data.datasets[0].data;
        series.push({ x: Date.parse(event.timestamp), y: state.currentPrice });
        if (series.length > 300) series.shift();
        state.chart.update("none");
    }
}

function setStreamIndicator(status) {
    const labels = {
        unknown: "Market stream status unknown",
        unconfigured: "Market stream is not configured",
        connecting: "Connecting to market stream",
        connected: "Market stream connected",
        stale: "Market stream is stale or unavailable",
        offline: "Market stream disconnected",
    };
    if (!elements.streamIndicator) return;
    const label = labels[status] || labels.unknown;
    elements.streamIndicator.dataset.state = status;
    elements.streamIndicator.setAttribute("aria-label", label);
    elements.streamIndicator.title = label;
}

function connectMarketStream() {
    const url = window.APP_CONFIG?.marketSocketUrl;
    if (!url || !window.WebSocket) {
        setStreamIndicator("unconfigured");
        return;
    }
    setStreamIndicator("connecting");
    const socket = new WebSocket(url);
    state.marketSocket = socket;
    socket.addEventListener("open", () => {
        state.socketRetry = 1000;
        updateStreamSubscription();
    });
    socket.addEventListener("message", (message) => {
        let event;
        try { event = JSON.parse(message.data); } catch { return; }
        if (event.type === "trade") applyStreamEvent(event);
        if (event.type === "status") {
            state.streamConnected = event.state === "connected";
            setStreamIndicator(state.streamConnected ? "connected" : "stale");
            if (!state.streamConnected) elements.statusMessage.textContent = "Streaming unavailable or stale; using market snapshots.";
        }
    });
    socket.addEventListener("close", () => {
        state.streamConnected = false;
        state.streamSymbol = null;
        setStreamIndicator("offline");
        clearTimeout(state.socketRetryTimer);
        const delay = state.socketRetry + Math.random() * state.socketRetry * 0.25;
        state.socketRetry = Math.min(state.socketRetry * 2, 30000);
        state.socketRetryTimer = setTimeout(connectMarketStream, delay);
    });
    socket.addEventListener("error", () => { setStreamIndicator("offline"); socket.close(); });
}

function renderPortfolioPrediction(prediction) {
    elements.portfolioPredictionRisk.textContent = prediction.risk_level || "--";
    elements.portfolioPredictionText.textContent = prediction.summary || "No portfolio prediction available.";
    elements.portfolioPredictionAction.textContent = prediction.action || "No action suggestion available.";
}

function renderPortfolio(portfolio) {
    const { summary, holdings } = portfolio;
    elements.totalInvestment.textContent = formatCurrency(summary.total_investment, summary.currency || "USD");
    elements.currentValue.textContent = formatCurrency(summary.current_value, summary.currency || "USD");
    elements.profitLoss.textContent = formatCurrency(summary.profit_loss, summary.currency || "USD");
    elements.profitLoss.className = toneClass(Number(summary.profit_loss));
    if (!holdings?.length) {
        elements.portfolioList.innerHTML = '<p class="empty-state">No local holdings yet. Broker cash, equity, and buying power are unavailable.</p>';
        return;
    }
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>Instrument</th><th>Qty</th><th>Avg entry</th><th>Mark</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Source / freshness</th><th></th></tr></thead>";
    const tbody = document.createElement("tbody");
    holdings.forEach((item) => {
        const row = document.createElement("tr");
        const fields = [
            item.symbol + " (" + item.market + ", " + item.currency + ")",
            item.quantity,
            formatPrice(item.average_entry, item.currency, item.market),
            item.price == null ? "--" : formatPrice(item.price, item.currency, item.market),
            item.value == null ? "--" : formatPrice(item.value, item.currency, item.market),
            item.unrealized_pnl == null ? "--" : formatPrice(item.unrealized_pnl, item.currency, item.market),
            (item.data_status || "unavailable") + " | " + (item.freshness || "unknown") + " | " + (item.source || "Unavailable") + (item.event_timestamp ? " | " + new Date(item.event_timestamp).toLocaleString() : ""),
        ];
        fields.forEach((value, index) => {
            const cell = document.createElement("td");
            cell.textContent = value;
            if (index === 5) cell.className = toneClass(Number(item.unrealized_pnl));
            row.appendChild(cell);
        });
        const actions = document.createElement("td");
        const analyze = document.createElement("button");
        analyze.type = "button";
        analyze.className = "market-tab analyze-holding";
        analyze.textContent = "Analyze";
        analyze.dataset.symbol = item.symbol;
        analyze.dataset.market = String(item.market).toLowerCase();
        analyze.addEventListener("click", async () => {
            setMarketContext(analyze.dataset.market, analyze.dataset.symbol);
            togglePortfolioDrawer(false);
            await loadAsset();
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "market-tab delete-holding";
        remove.textContent = "Remove";
        remove.addEventListener("click", async () => await deleteHolding(item.id));
        actions.append(analyze, remove);
        row.appendChild(actions);
        tbody.appendChild(row);
    });
    table.appendChild(tbody);
    const tableWrap = document.createElement("div");
    tableWrap.className = "table-wrap";
    tableWrap.appendChild(table);
    const note = document.createElement("p");
    note.className = "muted-line";
    note.textContent = summary.account_source + " | valuation " + summary.valuation_status + " | cash, equity, buying power, and realized P&L unavailable.";
    elements.portfolioList.replaceChildren(tableWrap, note);
}

async function loadPortfolio() {
    try {
        const response = await fetch("/get_portfolio");
        if (response.status === 401) { window.location.assign("/login"); return; }
        const result = await response.json();
        if (result.success) {
            renderPortfolio(result.data);
        }
    } catch (error) {
        console.error("Portfolio failed", error);
    }
}

async function loadWatchlist() {
    try {
        const response = await fetch("/api/watchlist");
        if (response.status === 401) { window.location.assign("/login"); return; }
        const result = await response.json();
        if (!result.success) throw new Error(result.message || "Watchlist unavailable");
        if (!result.data.length) {
            elements.watchlistRows.innerHTML = '<p class="empty-state">Your watchlist is empty.</p>';
            return;
        }
        const table = document.createElement("table");
        table.innerHTML = "<thead><tr><th>Instrument</th><th>Price</th><th>Move</th><th>Status / source</th><th></th></tr></thead>";
        const body = document.createElement("tbody");
        result.data.forEach((item) => {
            const row = document.createElement("tr");
            const values = [item.symbol + " | " + item.market, item.price == null ? "--" : formatPrice(item.price, item.currency, item.market),
                item.change_percent == null ? "--" : (item.change == null ? "" : formatPrice(item.change, item.currency, item.market) + " | ") + formatPercent(item.change_percent),
                item.data_status + " | " + item.freshness + " | " + item.source + (item.event_timestamp ? " | " + new Date(item.event_timestamp).toLocaleString() : "")];
            values.forEach((value) => { const cell = document.createElement("td"); cell.textContent = value; row.appendChild(cell); });
            const actionCell = document.createElement("td");
            const remove = document.createElement("button");
            remove.type = "button"; remove.className = "market-tab"; remove.textContent = "Remove";
            remove.addEventListener("click", async () => await removeWatchlistItem(item.symbol));
            actionCell.appendChild(remove); row.appendChild(actionCell); body.appendChild(row);
        });
        table.appendChild(body);
        elements.watchlistRows.replaceChildren(table);
    } catch (error) {
        elements.watchlistRows.innerHTML = '<p class="empty-state">Watchlist could not be loaded.</p>';
    }
}

async function addWatchlistItem(symbol, market) {
    const response = await fetch("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ symbol, market }) });
    const result = await response.json();
    elements.statusMessage.textContent = result.message || "Watchlist request completed.";
    if (result.success) await loadWatchlist();
    return result;
}

async function removeWatchlistItem(symbol) {
    const response = await fetch("/api/watchlist", { method: "DELETE", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ symbol }) });
    const result = await response.json();
    elements.statusMessage.textContent = result.message || "Watchlist request completed.";
    if (result.success) await loadWatchlist();
}

async function loadOrders() {
    try {
        const response = await fetch("/api/orders");
        if (response.status === 401) { window.location.assign("/login"); return; }
        const result = await response.json();
        if (!result.success) throw new Error("Order activity unavailable");
        if (!result.data.length) {
            elements.orderRows.innerHTML = '<p class="empty-state">No order activity. Drafts and confirmed simulator entries appear here.</p>';
            return;
        }
        const table = document.createElement("table");
        table.innerHTML = "<thead><tr><th>Created</th><th>Status updated</th><th>Symbol</th><th>Action</th><th>Type</th><th>Qty</th><th>Limit</th><th>Status</th><th>Mode / fill</th><th></th></tr></thead>";
        const body = document.createElement("tbody");
        result.data.forEach((order) => {
            const row = document.createElement("tr");
            const values = [new Date(order.created_at).toLocaleString(), new Date(order.updated_at || order.created_at).toLocaleString(), order.symbol, order.side.toUpperCase(), order.order_type,
                order.quantity, order.limit_price || "--", order.status, order.provider + " | " + order.fill_status];
            values.forEach((value) => { const cell = document.createElement("td"); cell.textContent = value; row.appendChild(cell); });
            const actionCell = document.createElement("td");
            if (["draft", "accepted"].includes(order.status)) {
                const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "market-tab"; cancel.textContent = "Cancel";
                cancel.addEventListener("click", async () => await cancelOrder(order.id)); actionCell.appendChild(cancel);
            }
            row.appendChild(actionCell); body.appendChild(row);
        });
        table.appendChild(body);
        const tableWrap = document.createElement("div");
        tableWrap.className = "table-wrap";
        tableWrap.appendChild(table);
        const timeline = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Status timeline (" + (result.events || []).length + ")";
        timeline.appendChild(summary);
        (result.events || []).forEach((event) => {
            const entry = document.createElement("p");
            entry.className = "muted-line";
            entry.textContent = new Date(event.created_at).toLocaleString() + " | " + event.status + " | " + event.detail;
            timeline.appendChild(entry);
        });
        elements.orderRows.replaceChildren(tableWrap, timeline);
    } catch (error) {
        elements.orderRows.innerHTML = '<p class="empty-state">Order activity is unavailable.</p>';
    }
}

function displayOrderPreview(order) {
    state.pendingOrderId = order.order_id;
    const preview = order.preview;
    elements.orderPreviewText.textContent = preview.side.toUpperCase() + " " + preview.quantity + " " + preview.symbol + " " +
        preview.order_type.toUpperCase() + (preview.limit_price ? " limit " + preview.limit_price : "") +
        " | reference " + preview.reference_price + " " + preview.currency + " | estimated notional " + preview.estimated_notional +
        " " + preview.currency + " | " + preview.price_status + " from " + preview.price_source +
        (preview.price_timestamp ? " at " + preview.price_timestamp : "");
    elements.orderConfirmCheck.checked = false;
    elements.confirmOrderButton.disabled = true;
    elements.orderPreview.hidden = false;
}

async function previewOrder(event) {
    event.preventDefault();
    if (state.previewInFlight) return;
    if (state.pendingOrderId) {
        elements.orderMessage.textContent = "Discard the current draft preview before creating another.";
        return;
    }
    const payload = { symbol: elements.orderSymbol.value.trim(), market: elements.orderMarket.value,
        side: elements.orderSide.value, order_type: elements.orderType.value, quantity: elements.orderQuantity.value,
        limit_price: elements.orderType.value === "limit" ? elements.orderLimitPrice.value : null };
    state.previewInFlight = true;
    const submitButton = elements.orderForm.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    state.orderRequestKey = state.orderRequestKey || crypto.randomUUID();
    try {
        const response = await fetch("/api/orders/preview", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken, "Idempotency-Key": state.orderRequestKey }, body: JSON.stringify(payload) });
        const result = await response.json();
        state.orderRequestKey = null;
        elements.orderMessage.textContent = result.message || (result.success ? "Review the preview before confirming." : "Order preview failed.");
        if (result.success && result.status === "draft" && result.confirmation_required) {
            displayOrderPreview(result);
            await loadOrders();
        } else if (result.success) {
            elements.orderMessage.textContent = "This request already exists with status: " + result.status + ". Review order activity.";
            await loadOrders();
        }
    } catch (error) { elements.orderMessage.textContent = "Order preview is unavailable. Retry with the same request to avoid duplicate drafts."; }
    finally { state.previewInFlight = false; submitButton.disabled = false; }
}

async function confirmSimulatedOrder() {
    if (!state.pendingOrderId || !elements.orderConfirmCheck.checked) return;
    elements.confirmOrderButton.disabled = true;
    try {
        const response = await fetch("/api/orders/" + encodeURIComponent(state.pendingOrderId) + "/confirm", { method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify({ confirmed: true }) });
        const result = await response.json();
        elements.orderMessage.textContent = result.message || "Confirmation failed.";
        if (result.success) {
            elements.orderPreview.hidden = true; state.pendingOrderId = null;
            await loadOrders();
        } else elements.confirmOrderButton.disabled = false;
    } catch (error) {
        elements.orderMessage.textContent = "Order confirmation failed; check activity before retrying.";
        elements.confirmOrderButton.disabled = false;
    }
}

async function cancelOrder(orderId) {
    try {
        const response = await fetch("/api/orders/" + encodeURIComponent(orderId) + "/cancel", { method: "POST", headers: { "X-CSRF-Token": csrfToken } });
        const result = await response.json();
        elements.orderMessage.textContent = result.message || "Cancel request completed.";
        await loadOrders();
    } catch (error) { elements.orderMessage.textContent = "Cancel request failed."; }
}

async function addHolding(event) {
    event.preventDefault();
    const symbol = elements.portfolioSymbol.value.trim();
    const quantity = elements.portfolioQuantity.value.trim();
    if (!symbol || !quantity || Number(quantity) <= 0) return;

    try {
        const response = await fetch("/add_stock", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID(), "X-CSRF-Token": csrfToken },
            body: JSON.stringify({ symbol, quantity, market: state.market }),
        });
        const result = await response.json();
        elements.statusMessage.textContent = result.message || "Portfolio updated.";
        if (result.success) {
            elements.portfolioForm.reset();
            await loadPortfolio();
        }
    } catch (error) {
        console.error("Add holding failed", error);
    }
}

async function deleteHolding(holdingId) {
    try {
        const response = await fetch(`/delete_stock/${holdingId}`, { method: "DELETE", headers: { "X-CSRF-Token": csrfToken } });
        const result = await response.json();
        elements.statusMessage.textContent = result.message || "Holding removed.";
        if (result.success) {
            await loadPortfolio();
        }
    } catch (error) {
        console.error("Delete holding failed", error);
    }
}

async function predictPortfolio() {
    try {
        elements.statusMessage.textContent = "Generating portfolio prediction...";
        const response = await fetch("/portfolio_predict");
        const result = await response.json();
        if (result.success) {
            renderPortfolioPrediction(result.data);
            elements.statusMessage.textContent = "Portfolio prediction updated.";
        }
    } catch (error) {
        console.error("Portfolio prediction failed", error);
    }
}

function bindOverlay() {
    elements.marketChoices.forEach((button) => {
        button.addEventListener("click", async () => {
            setMarketContext(button.dataset.market, button.dataset.symbol);
            elements.marketOverlay.classList.add("hidden");
            await loadAsset();
            await loadMarketOverview();
        });
    });
}

function bindControls() {
    elements.portfolioLauncher.addEventListener("click", async () => {
        togglePortfolioDrawer(true);
        await loadPortfolio();
    });

    elements.closePortfolio.addEventListener("click", () => togglePortfolioDrawer(false));
    elements.portfolioRefresh.addEventListener("click", async () => await loadPortfolio());
    elements.addCurrentWatchlist.addEventListener("click", async () => await addWatchlistItem(state.symbol, state.market));
    elements.watchlistForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        await addWatchlistItem(elements.watchlistSymbol.value.trim(), elements.watchlistMarket.value);
    });
    elements.orderForm.addEventListener("submit", previewOrder);
    elements.orderType.addEventListener("change", () => {
        elements.limitPriceLabel.hidden = elements.orderType.value !== "limit";
        elements.orderLimitPrice.required = elements.orderType.value === "limit";
    });
    elements.orderConfirmCheck.addEventListener("change", () => {
        elements.confirmOrderButton.disabled = !elements.orderConfirmCheck.checked || !state.pendingOrderId;
    });
    elements.confirmOrderButton.addEventListener("click", confirmSimulatedOrder);
    elements.dismissOrderButton.addEventListener("click", () => {
        const pendingId = state.pendingOrderId;
        state.pendingOrderId = null;
        elements.orderPreview.hidden = true;
        elements.orderConfirmCheck.checked = false;
        elements.confirmOrderButton.disabled = true;
        if (pendingId) cancelOrder(pendingId);
    });
    elements.refreshOrders.addEventListener("click", loadOrders);

    elements.themeToggle.addEventListener("click", () => {
        const current = document.body.getAttribute("data-theme") || "dark";
        const index = themes.indexOf(current);
        const next = themes[(index + 1) % themes.length];
        setTheme(next);
        if (state.chart) {
            state.chart.destroy();
            state.chart = null;
            loadAsset();
        }
    });

    elements.symbolForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        state.symbol = elements.symbolInput.value.trim() || state.symbol;
        await loadAsset();
    });

    elements.marketTabs.forEach((button) => {
        button.addEventListener("click", async () => {
            setMarketContext(button.dataset.market, button.dataset.symbol);
            elements.marketOverlay.classList.add("hidden");
            await loadAsset();
        });
    });

    elements.rangeButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            state.range = button.dataset.range;
            setActiveButton(elements.rangeButtons, state.range, "range");
            await loadAsset();
        });
    });

    elements.chartButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            state.chartType = button.dataset.chart;
            setActiveButton(elements.chartButtons, state.chartType, "chart");
            await loadAsset();
        });
    });

    elements.portfolioForm.addEventListener("submit", addHolding);
}

function startAutoRefresh() {
    clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(async () => {
        if (!state.sessionReady) return;
        state.overviewTick += 1;
        if (!state.streamConnected || Date.now() - state.lastStreamEventAt > 60000) await loadAsset();
        if (state.overviewTick % 4 === 0) {
            await loadMarketOverview();
        }
    }, 30000);
}

async function init() {
    initTheme();
    bindOverlay();
    bindControls();
    setActiveButton(elements.marketTabs, state.market, "market");
    setActiveButton(elements.rangeButtons, state.range, "range");
    setActiveButton(elements.chartButtons, state.chartType, "chart");
    renderTicker(presets);
    elements.orderSymbol.value = state.symbol;
    elements.orderMarket.value = state.market;
    elements.watchlistSymbol.value = state.symbol;
    elements.watchlistMarket.value = state.market;
    await loadMarketOverview();
    await loadPortfolio();
    await loadWatchlist();
    await loadOrders();
    connectMarketStream();
    startAutoRefresh();
}

init();
