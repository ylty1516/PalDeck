const LEVEL_COUNTS = Object.freeze({ off: 0, low: 18, medium: 42, high: 80 });

export function createEffects({ canvas = document.querySelector("#petalCanvas"), root = document } = {}) {
  const context = canvas?.getContext("2d");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let level = "off";
  let particles = [];
  let frame = 0;
  let previous = 0;
  let destroyed = false;

  function resize() {
    if (!canvas || !context) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(innerWidth * dpr);
    canvas.height = Math.floor(innerHeight * dpr);
    canvas.style.width = `${innerWidth}px`;
    canvas.style.height = `${innerHeight}px`;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function petal(seed = Math.random()) {
    return { x: seed * innerWidth, y: -20 - Math.random() * innerHeight, size: 5 + Math.random() * 7, speed: 18 + Math.random() * 30, drift: Math.random() * 2 - 1, phase: Math.random() * Math.PI * 2 };
  }

  function syncParticles() {
    const count = reduced.matches ? 0 : Math.min(80, LEVEL_COUNTS[level] ?? 0);
    while (particles.length < count) particles.push(petal());
    particles.length = count;
  }

  function draw(now) {
    frame = 0;
    if (destroyed || document.hidden || reduced.matches || level === "off" || !context) return;
    const delta = Math.min((now - previous) / 1000 || 0, 0.04);
    previous = now;
    context.clearRect(0, 0, innerWidth, innerHeight);
    context.fillStyle = "rgba(255, 190, 210, .72)";
    for (const item of particles) {
      item.y += item.speed * delta;
      item.x += (Math.sin(item.phase + item.y / 80) + item.drift) * 14 * delta;
      if (item.y > innerHeight + 20) Object.assign(item, petal(Math.random()), { y: -20 });
      context.beginPath();
      context.ellipse(item.x, item.y, item.size, item.size * 0.55, item.y / 90, 0, Math.PI * 2);
      context.fill();
    }
    frame = requestAnimationFrame(draw);
  }

  function start() {
    if (!frame && !document.hidden && particles.length) {
      previous = performance.now();
      frame = requestAnimationFrame(draw);
    }
  }

  function update(nextLevel) {
    level = Object.hasOwn(LEVEL_COUNTS, nextLevel) ? nextLevel : "off";
    syncParticles();
    if (!particles.length && context) context.clearRect(0, 0, innerWidth, innerHeight);
    start();
  }

  function visibilitychange() {
    if (document.hidden && frame) { cancelAnimationFrame(frame); frame = 0; }
    else start();
  }

  function addRipple(target, clientX, clientY) {
    if (reduced.matches || !target?.matches("button, .btn")) return;
    const rect = target.getBoundingClientRect();
    const ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.left = `${(clientX ?? rect.left + rect.width / 2) - rect.left}px`;
    ripple.style.top = `${(clientY ?? rect.top + rect.height / 2) - rect.top}px`;
    target.append(ripple);
    ripple.addEventListener("animationend", () => ripple.remove(), { once: true });
  }

  function pointerdown(event) {
    const target = event.target.closest("button, .btn");
    if (target) addRipple(target, event.clientX, event.clientY);
  }
  function keydown(event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest("button, .btn");
    if (target) addRipple(target);
  }
  function motionChanged() { syncParticles(); start(); }

  resize();
  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", visibilitychange);
  root.addEventListener("pointerdown", pointerdown);
  root.addEventListener("keydown", keydown);
  reduced.addEventListener("change", motionChanged);

  function destroy() {
    destroyed = true;
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    particles = [];
    window.removeEventListener("resize", resize);
    document.removeEventListener("visibilitychange", visibilitychange);
    root.removeEventListener("pointerdown", pointerdown);
    root.removeEventListener("keydown", keydown);
    reduced.removeEventListener("change", motionChanged);
  }

  return Object.freeze({ update, destroy });
}
