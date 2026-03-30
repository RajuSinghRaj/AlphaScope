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
};

const themes = ["dark", "light", "graphite", "emerald"];
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
};

function formatCurrency(value, currency = "USD") {
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
    return formatCurrency(value, currency);
}

function formatPercent(value) {
    const number = Number(value || 0);
    return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
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
                <strong>${item.headline}</strong>
                <span class="${toneClass(item.sentiment === "Positive" ? 1 : item.sentiment === "Negative" ? -1 : 0)}">${item.sentiment}</span>
            </div>
            <p>${item.summary}</p>
        </article>
    `).join("");
}

function renderTicker(groups) {
    const topItems = [...groups.crypto, ...groups.india];
    const bottomItems = [...groups.us, ...groups.forex];
    const build = (items) => items.map((item) => `
        <button class="ticker-pill" type="button" data-symbol="${item.symbol}" data-market="${(item.market || "").toLowerCase()}">
            <div>
                <strong>${item.symbol}</strong>
                <span>${item.name}</span>
            </div>
            <div class="ticker-meta">
                <strong>${item.price == null ? "--" : formatPrice(item.price, item.currency || "USD", item.market || "US")}</strong>
                <span class="${toneClass(item.change_percent)}">${item.change_percent == null ? "Watch" : formatPercent(item.change_percent)}</span>
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
    elements.insightSource.textContent = insight.source === "ollama" ? "Ollama Live" : "Rule Logic";
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
        elements.stockName.textContent = `${asset.name} (${asset.symbol})`;
        elements.stockPrice.textContent = formatPrice(asset.price, asset.currency, asset.market);
        elements.stockChange.textContent = formatPercent(asset.change_percent);
        elements.stockChange.className = toneClass(asset.change_percent);
        elements.currencyLabel.textContent = asset.currency;
        elements.heroMarket.textContent = asset.market;
        elements.heroExchange.textContent = asset.exchange;
        elements.assetMarketChip.textContent = asset.market;
        elements.trendMomentum.textContent = asset.trend_status.momentum;
        elements.trendSignal.textContent = asset.trend_status.signal;
        elements.trendHealth.textContent = asset.trend_status.health;
        elements.updateBadge.textContent = new Date().toLocaleTimeString();
        elements.statusMessage.textContent = `${asset.market} feed updated for ${asset.symbol}.`;
        renderInsight(asset.insight);
        renderNews(asset.news);
        createOrUpdateChart(asset.history);
    } catch (error) {
        console.error("Load asset failed", error);
        elements.statusMessage.textContent = "Live request failed. Please try again.";
    }
}

function renderPortfolioPrediction(prediction) {
    elements.portfolioPredictionRisk.textContent = prediction.risk_level || "--";
    elements.portfolioPredictionText.textContent = prediction.summary || "No portfolio prediction available.";
    elements.portfolioPredictionAction.textContent = prediction.action || "No action suggestion available.";
}

function renderPortfolio(portfolio) {
    const { summary, holdings } = portfolio;
    elements.totalInvestment.textContent = formatCurrency(summary.total_investment);
    elements.currentValue.textContent = formatCurrency(summary.current_value);
    elements.profitLoss.textContent = formatCurrency(summary.profit_loss);
    elements.profitLoss.className = toneClass(summary.profit_loss);

    if (!holdings?.length) {
        elements.portfolioList.innerHTML = '<p class="empty-state">Add assets to track holdings here.</p>';
        return;
    }

    elements.portfolioList.innerHTML = holdings.map((item) => `
        <article class="holding-card glass-inner">
            <div class="news-top">
                <div>
                    <strong>${item.symbol}</strong>
                    <p>${item.market} market</p>
                </div>
                <span class="${toneClass(item.change_percent)}">${formatPercent(item.change_percent)}</span>
            </div>
            <p>Quantity: ${item.quantity}</p>
            <p>Entry: ${formatPrice(item.purchase_price, item.currency, item.market)}</p>
            <p>Live: ${formatPrice(item.price, item.currency, item.market)}</p>
            <p>${item.beginner_label}</p>
            <div class="holding-actions">
                <button type="button" class="market-tab analyze-holding" data-symbol="${item.symbol}" data-market="${item.market.toLowerCase()}">Analyze</button>
                <button type="button" class="market-tab delete-holding" data-id="${item.id}">Delete</button>
            </div>
        </article>
    `).join("");

    document.querySelectorAll(".analyze-holding").forEach((button) => {
        button.addEventListener("click", async () => {
            setMarketContext(button.dataset.market, button.dataset.symbol);
            togglePortfolioDrawer(false);
            await loadAsset();
        });
    });

    document.querySelectorAll(".delete-holding").forEach((button) => {
        button.addEventListener("click", async () => {
            await deleteHolding(button.dataset.id);
        });
    });
}

async function loadPortfolio() {
    try {
        const response = await fetch("/get_portfolio");
        const result = await response.json();
        if (result.success) {
            renderPortfolio(result.data);
        }
    } catch (error) {
        console.error("Portfolio failed", error);
    }
}

async function addHolding(event) {
    event.preventDefault();
    const symbol = elements.portfolioSymbol.value.trim();
    const quantity = Number(elements.portfolioQuantity.value);
    if (!symbol || quantity <= 0) return;

    try {
        const response = await fetch("/add_stock", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
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
        const response = await fetch(`/delete_stock/${holdingId}`, { method: "DELETE" });
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
    elements.portfolioPredict.addEventListener("click", async () => await predictPortfolio());

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
        await loadAsset();
        if (state.overviewTick % 4 === 0) {
            await loadMarketOverview();
        }
    }, 5000);
}

async function init() {
    initTheme();
    bindOverlay();
    bindControls();
    setActiveButton(elements.marketTabs, state.market, "market");
    setActiveButton(elements.rangeButtons, state.range, "range");
    setActiveButton(elements.chartButtons, state.chartType, "chart");
    renderTicker(presets);
    renderPortfolioPrediction({
        risk_level: "--",
        summary: "Request a prediction to get an overall portfolio read.",
        action: "Suggested action will appear here.",
    });
    await loadMarketOverview();
    await loadPortfolio();
    startAutoRefresh();
}

init();
