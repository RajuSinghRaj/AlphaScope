/**
 * ALPHASCOPE COSMIC LANDING PAGE INTERACTION & LIVE ANIMATION
 * Provides "live video" moving cosmic data arcs, stars, 3D tilt, and live price engine
 */

(function () {
    "use strict";

    // 1. Live Cosmos Background Animation (Stars, Atmospheric Arcs, Flowing Data Packets)
    const canvas = document.getElementById("liveCosmosCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    let width = 0;
    let height = 0;
    const stars = [];
    const arcs = [];

    function resize() {
        width = canvas.width = window.innerWidth;
        height = canvas.height = window.innerHeight;
        initCosmos();
    }

    function initCosmos() {
        // Generate Star Field
        stars.length = 0;
        const starCount = Math.floor((width * height) / 10000);
        for (let i = 0; i < starCount; i++) {
            stars.push({
                x: Math.random() * width,
                y: Math.random() * (height * 0.7),
                size: Math.random() * 1.5 + 0.5,
                alpha: Math.random() * 0.7 + 0.3,
                pulseSpeed: (Math.random() * 0.02 + 0.005) * (Math.random() > 0.5 ? 1 : -1),
                vx: (Math.random() - 0.5) * 0.08,
                vy: (Math.random() - 0.5) * 0.08,
            });
        }

        // Generate Curved Orbital Arcs over Earth
        arcs.length = 0;
        const earthY = height * 0.85;
        const arcConfigs = [
            { startX: width * 0.15, startY: earthY, endX: width * 0.45, endY: earthY - 80, cpX: width * 0.3, cpY: earthY - 220, color: "rgba(56, 189, 248, 0.4)", speed: 0.004 },
            { startX: width * 0.35, startY: earthY - 50, endX: width * 0.72, endY: earthY - 30, cpX: width * 0.52, cpY: earthY - 260, color: "rgba(139, 92, 246, 0.4)", speed: 0.0035 },
            { startX: width * 0.60, startY: earthY - 30, endX: width * 0.92, endY: earthY, cpX: width * 0.78, cpY: earthY - 200, color: "rgba(52, 211, 153, 0.4)", speed: 0.0045 },
            { startX: width * 0.05, startY: earthY + 20, endX: width * 0.55, endY: earthY - 90, cpX: width * 0.28, cpY: earthY - 280, color: "rgba(56, 189, 248, 0.3)", speed: 0.003 },
        ];

        arcConfigs.forEach((cfg) => {
            arcs.push({
                ...cfg,
                t: Math.random(),
                pulseSize: 3.5,
            });
        });
    }

    function getQuadraticBezierPoint(p0, p1, p2, t) {
        const x = (1 - t) * (1 - t) * p0.x + 2 * (1 - t) * t * p1.x + t * t * p2.x;
        const y = (1 - t) * (1 - t) * p0.y + 2 * (1 - t) * t * p1.y + t * t * p2.y;
        return { x, y };
    }

    function drawCosmos() {
        ctx.clearRect(0, 0, width, height);

        // Draw Stars
        for (let i = 0; i < stars.length; i++) {
            const s = stars[i];
            s.alpha += s.pulseSpeed;
            if (s.alpha > 0.95 || s.alpha < 0.2) s.pulseSpeed = -s.pulseSpeed;
            s.x += s.vx;
            s.y += s.vy;
            if (s.x < 0) s.x = width;
            if (s.x > width) s.x = 0;
            if (s.y < 0) s.y = height;
            if (s.y > height) s.y = 0;

            ctx.beginPath();
            ctx.arc(s.x, s.y, s.size, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(226, 232, 240, ${s.alpha})`;
            ctx.fill();
        }

        // Draw Orbital Arcs with Moving Light Packets
        for (let i = 0; i < arcs.length; i++) {
            const arc = arcs[i];
            const p0 = { x: arc.startX, y: arc.startY };
            const p1 = { x: arc.cpX, y: arc.cpY };
            const p2 = { x: arc.endX, y: arc.endY };

            // Draw glowing trajectory curve
            ctx.beginPath();
            ctx.moveTo(p0.x, p0.y);
            ctx.quadraticCurveTo(p1.x, p1.y, p2.x, p2.y);
            ctx.strokeStyle = arc.color;
            ctx.lineWidth = 1.2;
            ctx.stroke();

            // Advance packet
            arc.t += arc.speed;
            if (arc.t > 1) arc.t = 0;

            const packetPos = getQuadraticBezierPoint(p0, p1, p2, arc.t);

            // Draw packet glow
            const radGrad = ctx.createRadialGradient(packetPos.x, packetPos.y, 0, packetPos.x, packetPos.y, 16);
            radGrad.addColorStop(0, "rgba(255, 255, 255, 1)");
            radGrad.addColorStop(0.25, arc.color.replace("0.4", "0.9"));
            radGrad.addColorStop(1, "transparent");

            ctx.beginPath();
            ctx.arc(packetPos.x, packetPos.y, 14, 0, Math.PI * 2);
            ctx.fillStyle = radGrad;
            ctx.fill();

            // Core dot
            ctx.beginPath();
            ctx.arc(packetPos.x, packetPos.y, 2.5, 0, Math.PI * 2);
            ctx.fillStyle = "#ffffff";
            ctx.fill();
        }

        requestAnimationFrame(drawCosmos);
    }

    window.addEventListener("resize", resize);
    resize();
    requestAnimationFrame(drawCosmos);

    // 2. Interactive 3D Cursor Parallax on the Terminal Stage
    const stage = document.getElementById("terminalStage");
    const mockupCard = document.querySelector(".terminal-mockup-card");

    if (stage && mockupCard && window.innerWidth > 960) {
        window.addEventListener("mousemove", (e) => {
            const rect = stage.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            const deltaX = (e.clientX - centerX) / (window.innerWidth / 2);
            const deltaY = (e.clientY - centerY) / (window.innerHeight / 2);

            const rotY = -8 + deltaX * 10;
            const rotX = 4 - deltaY * 10;

            mockupCard.style.transform = `rotateY(${rotY}deg) rotateX(${rotX}deg)`;
        });
    }

    // 3. Live Animated Chart inside 3D Terminal Mockup
    const chartCanvas = document.getElementById("mockupLiveChart");
    if (chartCanvas) {
        const cCtx = chartCanvas.getContext("2d");
        let cWidth = chartCanvas.width = chartCanvas.offsetWidth || 340;
        let cHeight = chartCanvas.height = chartCanvas.offsetHeight || 180;

        // Base price points
        const points = [
            58, 62, 59, 65, 63, 68, 72, 70, 75, 71, 79, 82, 80, 85, 83, 89, 94, 91, 98, 105, 102, 110, 115, 112, 120, 128
        ];

        let tickOffset = 0;

        function drawMockChart() {
            cWidth = chartCanvas.width = chartCanvas.offsetWidth || 340;
            cHeight = chartCanvas.height = chartCanvas.offsetHeight || 180;
            cCtx.clearRect(0, 0, cWidth, cHeight);

            const paddingX = 10;
            const paddingY = 20;
            const stepX = (cWidth - paddingX * 2) / (points.length - 1);
            const minVal = 50;
            const maxVal = 140;

            // Gradient Fill
            const grad = cCtx.createLinearGradient(0, 0, 0, cHeight);
            grad.addColorStop(0, "rgba(52, 211, 153, 0.32)");
            grad.addColorStop(0.7, "rgba(52, 211, 153, 0.06)");
            grad.addColorStop(1, "rgba(52, 211, 153, 0)");

            // Draw Area
            cCtx.beginPath();
            const firstY = cHeight - paddingY - ((points[0] - minVal) / (maxVal - minVal)) * (cHeight - paddingY * 2);
            cCtx.moveTo(paddingX, firstY);

            for (let i = 1; i < points.length; i++) {
                const px = paddingX + i * stepX;
                // Add tiny organic breathing wave to the latest 5 points
                let val = points[i];
                if (i >= points.length - 4) {
                    val += Math.sin(tickOffset + i) * 1.5;
                }
                const py = cHeight - paddingY - ((val - minVal) / (maxVal - minVal)) * (cHeight - paddingY * 2);
                cCtx.lineTo(px, py);
            }

            // Close Area
            cCtx.lineTo(paddingX + (points.length - 1) * stepX, cHeight);
            cCtx.lineTo(paddingX, cHeight);
            cCtx.closePath();
            cCtx.fillStyle = grad;
            cCtx.fill();

            // Draw Stroke Line
            cCtx.beginPath();
            cCtx.moveTo(paddingX, firstY);
            let lastX = paddingX;
            let lastY = firstY;

            for (let i = 1; i < points.length; i++) {
                lastX = paddingX + i * stepX;
                let val = points[i];
                if (i >= points.length - 4) {
                    val += Math.sin(tickOffset + i) * 1.5;
                }
                lastY = cHeight - paddingY - ((val - minVal) / (maxVal - minVal)) * (cHeight - paddingY * 2);
                cCtx.lineTo(lastX, lastY);
            }

            cCtx.strokeStyle = "#34d399";
            cCtx.lineWidth = 2.2;
            cCtx.stroke();

            // Pulsing Node at Latest Price
            const pulseRadius = 5 + Math.sin(tickOffset * 2) * 2;
            cCtx.beginPath();
            cCtx.arc(lastX, lastY, pulseRadius + 6, 0, Math.PI * 2);
            cCtx.fillStyle = "rgba(52, 211, 153, 0.25)";
            cCtx.fill();

            cCtx.beginPath();
            cCtx.arc(lastX, lastY, 4, 0, Math.PI * 2);
            cCtx.fillStyle = "#ffffff";
            cCtx.fill();

            tickOffset += 0.05;
            requestAnimationFrame(drawMockChart);
        }

        requestAnimationFrame(drawMockChart);
    }

    // 4. Subtle Live Price Simulation in the 3D Mockup Card
    const priceEl = document.querySelector(".mockup-price-num");
    const changeEl = document.querySelector(".mockup-price-change");
    let currentBtc = 67432.18;

    setInterval(() => {
        if (!priceEl) return;
        const delta = (Math.random() - 0.48) * 12;
        currentBtc = Math.max(65000, currentBtc + delta);
        priceEl.textContent = "$" + currentBtc.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }, 2400);

})();
