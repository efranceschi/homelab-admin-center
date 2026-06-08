# Functional Specification — Interactive Network Diagram

> **Version:** 1.0  
> **Technology base:** HTML5 Canvas 2D (pure JavaScript, no external dependencies)  
> **Reference:** Implementation demonstrated in a Claude session (June 2026)

---

## 1. Overview

The interactive network diagram is a **Canvas 2D**-based visualization that renders network topologies with draggable components, simulated spring physics, and component-type-specific icons. It requires no external libraries — it runs on native browser JavaScript.

---

## 2. Technical Architecture

### 2.1 Stack

| Layer | Technology |
|-------|-----------|
| Rendering | `HTMLCanvasElement` + `CanvasRenderingContext2D` |
| Layout | Force simulation (force-directed graph) |
| Interactivity | Native event listeners (mouse + touch) |
| Dependencies | None |

### 2.2 Data Structures

#### Node (`Node`)

```js
{
  id:     string,   // unique identifier, e.g. 'fw', 'sw', 'ap1'
  label:  string,   // primary name shown below the icon
  sub:    string,   // subtitle (model, role), e.g. 'OPNsense'
  type:   string,   // type: 'fw' | 'sw' | 'srv' | 'ap' | 'router' | 'lb' | 'proxy'
  r:      number,   // circle radius in px (defines visual hierarchy)
  x:      number,   // X position in world space (world coordinates)
  y:      number,   // Y position in world space
  vx:     number,   // X velocity (physics)
  vy:     number,   // Y velocity (physics)
  pinned: boolean,  // true while the node is being dragged
}
```

#### Edge (`Edge`)

```js
{
  a:     string,  // source node id
  b:     string,  // target node id
  label: string,  // connection label, e.g. '1 Gbps', '100M', 'WireGuard'
}
```

---

## 3. Coordinate System

The diagram uses two distinct coordinate systems:

| System | Description |
|--------|-------------|
| **World** | Logical node coordinates (`node.x`, `node.y`). Origin at the center. |
| **Screen** | Canvas pixel coordinates, used for rendering. |

### Conversion

```js
// World → Screen
function worldToScreen(x, y) {
  return [
    x * scale + W/2 + offsetX,
    y * scale + H/2 + offsetY
  ];
}

// Screen → World
function screenToWorld(sx, sy) {
  return [
    (sx - W/2 - offsetX) / scale,
    (sy - H/2 - offsetY) / scale
  ];
}
```

**Viewport state variables:**

| Variable | Type | Description |
|----------|------|-------------|
| `scale` | `number` | Current zoom factor. Default `1.0`, range `[0.3, 3.0]` |
| `offsetX` | `number` | Horizontal pan offset |
| `offsetY` | `number` | Vertical pan offset |
| `W`, `H` | `number` | Current canvas dimensions in CSS pixels |

---

## 4. Force Simulation

The layout is computed by a **force-directed** algorithm run frame by frame via `requestAnimationFrame`.

### 4.1 Applied Forces

#### Central gravity
Pulls all nodes toward the origin `(0, 0)` to prevent infinite dispersion.

```js
fx += -node.x * GRAVITY;
fy += -node.y * GRAVITY;
// recommended GRAVITY: 0.008 – 0.015
```

#### Node repulsion (Coulomb)
Each pair of nodes repels to avoid overlap.

```js
const d = distance(a, b);
const f = REPULSE / (d * d);
fx += (dx / d) * f;
// recommended REPULSE: 6000 – 12000
```

#### Edge attraction (Hooke's law)
Nodes connected by an edge attract like springs.

```js
const f = K * (distance - SPRING);
fx += (dx / d) * f;
// SPRING (natural length): 140 – 180 px
// K (stiffness): 0.012 – 0.025
```

#### Damping
Reduces velocity each frame for stabilization.

```js
node.vx = (node.vx + fx) * DAMP;
node.vy = (node.vy + fy) * DAMP;
// recommended DAMP: 0.75 – 0.82
```

### 4.2 Recommended Parameters by Topology

| Topology | SPRING | REPULSE | K | DAMP |
|----------|--------|---------|---|------|
| Small (≤ 10 nodes) | 160 | 9000 | 0.018 | 0.78 |
| Medium (10–30 nodes) | 120 | 7000 | 0.022 | 0.80 |
| Large (30+ nodes) | 90 | 5000 | 0.028 | 0.82 |

---

## 5. Component Types and Icons

Each component type has a **color palette** and an **icon drawn via the Canvas API**.

### 5.1 Palette by Type

| Type | ID | Fill (light) | Stroke | Fill (dark) |
|------|----|-------------|--------|-------------|
| Firewall | `fw` | `#FCEBEB` | `#E24B4A` | `#501313` |
| Switch | `sw` | `#EEEDFE` | `#7F77DD` | `#26215C` |
| Server | `srv` | `#E1F5EE` | `#1D9E75` | `#04342C` |
| Access Point | `ap` | `#E6F1FB` | `#378ADD` | `#042C53` |
| Router | `router` | `#FAEEDA` | `#BA7517` | `#412402` |
| Load Balancer | `lb` | `#FBEAF0` | `#D4537E` | `#4B1528` |
| Proxy | `proxy` | `#EAF3DE` | `#639922` | `#173404` |

### 5.2 Icons by Type

All icons are scaled proportionally to the node radius `r` using `s = r / r_base`.

#### Firewall (`r_base = 38`)
Shield with an internal checkmark. Conveys protection and traffic inspection.

```js
function drawFirewallIcon(ctx, x, y, r, pal) {
  const s = r / 38;
  // Shield (rounded pentagon)
  ctx.beginPath();
  ctx.moveTo(x, y - 18*s);
  ctx.lineTo(x + 14*s, y - 12*s);
  ctx.lineTo(x + 14*s, y + 2*s);
  ctx.quadraticCurveTo(x + 14*s, y + 16*s, x, y + 20*s);
  ctx.quadraticCurveTo(x - 14*s, y + 16*s, x - 14*s, y + 2*s);
  ctx.lineTo(x - 14*s, y - 12*s);
  ctx.closePath();
  ctx.fillStyle = pal.stroke;
  ctx.fill();
  // Checkmark
  ctx.beginPath();
  ctx.moveTo(x - 7*s, y + 4*s);
  ctx.lineTo(x - 1*s, y + 10*s);
  ctx.lineTo(x + 9*s, y - 4*s);
  ctx.strokeStyle = 'white';
  ctx.lineWidth = 2.5 * s;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.stroke();
}
```

#### Switch (`r_base = 34`)
Chassis with RJ45 ports and status LEDs.

```js
function drawSwitchIcon(ctx, x, y, r, pal) {
  const s = r / 34;
  // Two rows of ports
  for (let row = 0; row < 2; row++) {
    const ry = y - 9*s + row * 9*s;
    ctx.beginPath();
    ctx.roundRect(x - 14*s, ry - 4*s, 28*s, 8*s, 1.5*s);
    ctx.fillStyle = pal.stroke;
    ctx.globalAlpha = 0.5;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  // LEDs (4 dots)
  [0,1,2,3].forEach(i => {
    ctx.beginPath();
    ctx.arc(x - 9*s + i * 6*s, y + 10*s, 1.8*s, 0, Math.PI*2);
    ctx.fillStyle = i < 3 ? '#63C94A' : '#888780';
    ctx.fill();
  });
  // Switching lines (horizontal arrows)
  ctx.beginPath();
  ctx.moveTo(x - 12*s, y + 16*s);
  ctx.lineTo(x + 12*s, y + 16*s);
  ctx.strokeStyle = pal.stroke;
  ctx.lineWidth = 1.5 * s;
  ctx.lineCap = 'round';
  ctx.stroke();
}
```

#### Server (`r_base = 30`)
Stacked cylinders (database/rack style).

```js
function drawServerIcon(ctx, x, y, r, pal) {
  const s = r / 30;
  for (let i = 0; i < 3; i++) {
    const ry = y - 12*s + i * 9*s;
    // Unit cap
    ctx.beginPath();
    ctx.ellipse(x, ry, 12*s, 4*s, 0, 0, Math.PI*2);
    ctx.fillStyle = pal.stroke;
    ctx.globalAlpha = 0.55;
    ctx.fill();
    ctx.globalAlpha = 1;
    // Body
    if (i < 2) {
      ctx.beginPath();
      ctx.rect(x - 12*s, ry, 24*s, 9*s);
      ctx.fillStyle = pal.stroke;
      ctx.globalAlpha = 0.4;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
    // Side LED
    ctx.beginPath();
    ctx.arc(x + 8*s, ry, 2.2*s, 0, Math.PI*2);
    ctx.fillStyle = i < 2 ? '#63C94A' : '#888780';
    ctx.fill();
  }
}
```

#### Access Point (`r_base = 26`)
Vertical antenna with radiating Wi-Fi waves.

```js
function drawAPIcon(ctx, x, y, r, pal) {
  const s = r / 26;
  // Vertical mast
  ctx.beginPath();
  ctx.moveTo(x, y + 14*s);
  ctx.lineTo(x, y - 2*s);
  ctx.strokeStyle = pal.stroke;
  ctx.lineWidth = 2.5 * s;
  ctx.lineCap = 'round';
  ctx.stroke();
  // Diagonal supports
  [[x - 10*s, y + 8*s], [x + 10*s, y + 8*s]].forEach(([ex, ey]) => {
    ctx.beginPath();
    ctx.moveTo(x, y - 2*s);
    ctx.lineTo(ex, ey);
    ctx.lineWidth = 1.8 * s;
    ctx.stroke();
  });
  // Concentric waves (3 arcs)
  [16*s, 11*s, 7*s].forEach((rad, i) => {
    ctx.beginPath();
    ctx.arc(x, y - 2*s, rad, Math.PI * 1.1, Math.PI * 1.9);
    ctx.lineWidth = (2.2 - i * 0.4) * s;
    ctx.stroke();
  });
}
```

---

## 6. Interactivity

### 6.1 Dragging Nodes (`drag`)

```
mousedown / touchstart
  → identify the node under the cursor via nodeAt(sx, sy)
  → if found: set dragNode, pinned = true, capture offset
  → if not: start pan

mousemove / touchmove
  → if dragNode: convert screen → world and update node.x, node.y
  → if panning: update offsetX, offsetY

mouseup / touchend
  → if dragNode:
      → if position did not change (click): trigger action (e.g. sendPrompt)
      → set pinned = false, dragNode = null
  → end pan
```

### 6.2 Pan (drag the background)

Click and drag on empty space moves the entire scene by updating `offsetX` and `offsetY`.

### 6.3 Zoom (scroll)

```js
canvas.addEventListener('wheel', e => {
  const factor = e.deltaY < 0 ? 1.1 : 0.9;
  // Cursor-centered zoom
  const [wx, wy] = screenToWorld(cursorX, cursorY);
  scale = clamp(scale * factor, 0.3, 3.0);
  offsetX = cursorX - wx * scale - W/2;
  offsetY = cursorY - wy * scale - H/2;
});
```

Zoom range: `0.3×` (overview) to `3.0×` (detail).

### 6.4 Node Hit Detection

```js
function nodeAt(sx, sy) {
  // Iterate back to front (last rendered = topmost)
  for (let i = NODES.length - 1; i >= 0; i--) {
    const n = NODES[i];
    const [nx, ny] = worldToScreen(n.x, n.y);
    const r = n.r * scale;
    if ((sx - nx)**2 + (sy - ny)**2 < r * r) return n;
  }
  return null;
}
```

### 6.5 UI Controls

| Button | Action |
|--------|--------|
| ↺ Reset | Restores initial positions and reactivates physics |
| ⚡ Physics on/off | Pauses/resumes the force simulation |
| ⊡ Fit to screen | Centers and fits all nodes |

---

## 7. Render Loop

```
requestAnimationFrame(tick)
  → simulate()   : updates positions via physics
  → draw()       : clears the canvas and redraws everything
  → if physicsOn : schedules the next tick
```

### Render Order (`draw`)

1. Clear canvas + background fill
2. Draw all **edges** (lines + labels)
3. Draw all **nodes** (circle + icon + label + sublabel)

Drawing edges before nodes ensures the lines do not cover the icons.

---

## 8. Responsiveness and DPI

```js
function resize() {
  const r = canvas.getBoundingClientRect();
  canvas.width  = r.width  * devicePixelRatio;
  canvas.height = r.height * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);
  W = r.width;
  H = r.height;
}
window.addEventListener('resize', () => { resize(); draw(); });
```

The canvas is resized using `devicePixelRatio` for crisp rendering on HiDPI/Retina displays.

---

## 9. Dark Mode Support

All colors are defined as a function of `matchMedia('(prefers-color-scheme: dark)').matches` at initialization time. Each component type has a `{ fill, stroke, text, sub }` palette for each mode.

```js
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
const PAL = {
  fw: {
    fill:   dark ? '#501313' : '#FCEBEB',
    stroke: '#E24B4A',
    text:   dark ? '#F7C1C1' : '#791F1F',
    sub:    dark ? '#F09595' : '#A32D2D',
  },
  // ...
};
```

---

## 10. Extensibility

### Adding a New Component Type

1. Define an entry in the `PAL` palette with fill/stroke/text/sub for light and dark
2. Implement a `drawXxxIcon(ctx, x, y, r, pal)` function
3. Add the call in the node-rendering block
4. Choose a radius `r` appropriate to the visual hierarchy (firewall larger, APs smaller)

### Adding a New Node to the Diagram

```js
NODES.push({
  id: 'proxy1', label: 'Proxy', sub: 'Nginx', type: 'proxy',
  r: 28, x: 100, y: 50, vx: 0, vy: 0, pinned: false
});
```

### Adding a New Edge

```js
EDGES.push({ a: 'sw', b: 'proxy1', label: 'eth6' });
```

---

## 11. Recommended Radii by Hierarchy

| Component | Radius (`r`) | Rationale |
|-----------|--------------|-----------|
| Firewall | 38 | Entry point; greatest emphasis |
| Core switch | 34 | Central hub of the topology |
| Router | 34 | Same level as the switch |
| Load Balancer | 32 | Distribution layer |
| Proxy / Reverse Proxy | 30 | Application layer |
| Server | 30 | High-importance leaf |
| Access Point | 26 | Peripheral leaf |
| Client device | 22 | Terminal leaf |

---

## 12. Known Limitations

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| No native export | Does not auto-generate PNG/SVG | Use `canvas.toDataURL()` |
| Simple 2D physics | Nodes may overlap in dense topologies | Increase `REPULSE` |
| Static dark mode | Does not react to runtime theme changes | Reinitialize the component |
| No port labels | Does not graphically show port IDs on edges | Add them to the edge `label` field |
| Touch: no pinch-zoom | Touch zoom not implemented | Add a two-finger `touchmove` handler |
