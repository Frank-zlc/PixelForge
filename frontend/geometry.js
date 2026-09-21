/**
 * Client-side coordinate maths, mirroring backend `geometry/mapper.py`.
 *
 * This is a deliberate, bounded duplication. The crosshair readout has to follow
 * the cursor, and a round trip per mousemove would make it lag visibly. So the
 * client computes the *display* values itself.
 *
 * The rule that keeps the duplication safe: anything that gets persisted -- a
 * crop, a recorded coordinate, a tap that a script will replay -- is computed by
 * the server. The client's numbers are for the reader's eyes only. If the two ever
 * disagree, the server is right.
 */

/** @typedef {{width: number, height: number}} Size */
/** @typedef {{x: number, y: number}} Point */

/**
 * Where the frame is actually drawn inside the canvas element, in CSS pixels.
 * The frame is contain-fitted, so unless the aspect ratios match there are bars
 * on two sides that correspond to no device pixel at all.
 */
export function contentRect(element, frame) {
  const scale = Math.min(element.width / frame.width, element.height / frame.height);
  const width = frame.width * scale;
  const height = frame.height * scale;
  return {
    x: (element.width - width) / 2,
    y: (element.height - height) / 2,
    width,
    height,
    scale,
  };
}

/**
 * CSS point -> frame point, or null when the point is in the letterbox.
 *
 * Returns null rather than clamping: a click on a black bar is not a click near
 * the edge, and pretending otherwise fabricates an interaction.
 */
export function cssToFrame(point, element, frame) {
  const content = contentRect(element, frame);
  const x = (point.x - content.x) / content.scale;
  const y = (point.y - content.y) / content.scale;
  if (x < 0 || y < 0 || x > frame.width || y > frame.height) return null;
  return { x, y };
}

/**
 * Frame -> device. Per-axis, because the H.264 encoder aligns width and height
 * down independently: the two scale factors genuinely differ, and assuming one
 * uniform scale is the classic off-by-three-pixels bug.
 */
export function frameToDevice(point, frame, display) {
  return {
    x: point.x * (display.width / frame.width),
    y: point.y * (display.height / frame.height),
  };
}

export function deviceToNorm(point, display) {
  return { x: point.x / display.width, y: point.y / display.height };
}

export function frameToCss(point, element, frame) {
  const content = contentRect(element, frame);
  return { x: content.x + point.x * content.scale, y: content.y + point.y * content.scale };
}

/** All four representations of one CSS point, for the readout. */
export function describe(cssPoint, element, frame, display) {
  const framePoint = cssToFrame(cssPoint, element, frame);
  if (!framePoint) return null;
  const devicePoint = frameToDevice(framePoint, frame, display);
  const norm = deviceToNorm(devicePoint, display);
  return {
    css: [Math.round(cssPoint.x), Math.round(cssPoint.y)],
    frame: [Math.round(framePoint.x), Math.round(framePoint.y)],
    device: [Math.round(devicePoint.x), Math.round(devicePoint.y)],
    norm: [Number(norm.x.toFixed(4)), Number(norm.y.toFixed(4))],
  };
}

/** Normalise a drag into a positive-area rect, whichever way it was dragged. */
export function normaliseRect(a, b) {
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(b.x - a.x),
    height: Math.abs(b.y - a.y),
  };
}
