// Run the actual served player with deterministic peers, frames and timers.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {script, scenario} = JSON.parse(fs.readFileSync(0, 'utf8'));

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }
  dispatchEvent(event) {
    for (const callback of this.listeners.get(event.type) || []) callback(event);
  }
}
let now = 0;
let timerId = 0;
const timers = new Map();
const schedule = (callback, delay, interval = false) => {
  const id = ++timerId;
  timers.set(id, {callback, delay, interval, due: now + delay});
  return id;
};
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const advance = async (duration) => {
  const target = now + duration;
  while (true) {
    const next = [...timers].filter(([, timer]) => timer.due <= target)
      .sort((a, b) => a[1].due - b[1].due)[0];
    if (!next) break;
    const [id, timer] = next;
    now = timer.due;
    if (timer.interval) timer.due += timer.delay;
    else timers.delete(id);
    timer.callback();
    await flush();
  }
  now = target;
  await flush();
};
const fields = new Map();
const field = (name) => {
  if (!fields.has(name)) fields.set(name, {textContent: ''});
  return fields.get(name);
};
const card = new Events();
card.dataset = {liveMetrics: '/metrics', cameraName: 'Camera'};
card.hidden = false;
card.querySelectorAll = (selector) => [field(selector)];
const recordingLabel = {textContent: '', classList: {toggle() {}}};
card.querySelector = (selector) => selector === '[data-recording-alert]' ? recordingLabel
  : selector === '[data-recording-coverage-url]'
    ? {dataset: {recordingCoverageUrl: '/coverage'}, addEventListener() {}}
    : null;
const image = new Events();
image.dataset = {liveSource: '/live.jpg'};
image.hidden = false;
image.classList = {add() {}, remove() {}};
image.closest = () => card;
const video = new Events();
video.dataset = {liveWebrtc: '/webrtc'};
video.hidden = true;
video.play = async () => {};
video.requestVideoFrameCallback = (callback) => { video.frameCallback = callback; };
video.frame = () => {
  const callback = video.frameCallback;
  video.frameCallback = null;
  callback?.(now);
};
video.closest = (selector) => selector === '[data-fullscreen-view]'
  ? {querySelector: () => image} : card;
const peers = [];
class Peer extends Events {
  constructor() {
    super();
    this.iceGatheringState = 'complete';
    this.connectionState = 'new';
    peers.push(this);
  }
  addTransceiver() {}
  createOffer() { return Promise.resolve({type: 'offer', sdp: 'offer'}); }
  setLocalDescription(description) { this.localDescription = description; return Promise.resolve(); }
  setRemoteDescription() { return Promise.resolve(); }
  close() { this.closed = true; this.state('closed'); }
  state(value) { this.connectionState = value; this.dispatchEvent({type: 'connectionstatechange'}); }
}
const window = new Events();
Object.assign(window, {
  RTCPeerConnection: Peer, performance: {now: () => now},
  location: {href: 'http://osecameras.local/', hash: '', reload() { window.reloaded = true; }, assign(path) { window.redirectedTo = path; }},
  setTimeout: (callback, delay) => schedule(callback, delay),
  clearTimeout: (id) => timers.delete(id),
  setInterval: (callback, delay) => schedule(callback, delay, true),
  clearInterval: (id) => timers.delete(id),
});
const document = {
  hidden: false, querySelector: () => null,
  querySelectorAll: (selector) => ({
    'img[data-live-source]': [image], 'video[data-live-webrtc]': [video],
    '[data-camera-card]': [card],
  })[selector] || [],
};
let signal;
const fetch = async (url, options) => {
  if (scenario === 'expired_session' && url === '/metrics') return {status: 401, ok: false};
  if (url === '/coverage') return {ok: true, json: async () => ({coverage:
    scenario === 'empty_recording' ? {state: 'none'} : {state: 'indexed', latest_segment_end: 0}
  })};
  if (url === '/metrics') return {ok: true, json: async () => ({metrics: {detect_input_fps: 5.2}})};
  signal = options.signal;
  if (scenario === 'signaling_timeout') return new Promise(() => {});
  return {ok: true, text: async () => 'answer'};
};
vm.runInNewContext(script, {
  window, document, fetch, URL, Event, AbortController, AbortSignal, RTCPeerConnection: Peer,
  MediaStream: class {}, Date: {now: () => now}, console,
});
const metric = (name) => field(`[data-live-${name}]`).textContent;

(async () => {
  await flush();
  assert.equal(peers.length, 1);
  assert.equal(recordingLabel.textContent,
    scenario === 'empty_recording' ? 'No recent video indexed' : 'Video indexed 0s ago');
  if (scenario === 'signaling_timeout') {
    await advance(20000);
    assert.equal(peers[0].closed, true);
    assert.equal(signal.aborted, true);
    assert.equal(metric('connection'), 'WebRTC start timed out');
    image.dispatchEvent(new Event('load'));
    assert.equal(metric('path'), 'Snapshots (fallback)');
    assert.equal(metric('connection'), 'WebRTC start timed out');
    await advance(15000);
    assert.equal(peers.length, 2);
  } else if (scenario === 'stalled_video') {
    peers[0].state('connected');
    assert.equal(metric('connection'), 'Waiting for video');
    video.frame();
    assert.equal(metric('connection'), 'Connected');
    await advance(10000);
    assert.equal(peers[0].closed, true);
    assert.equal(metric('connection'), 'WebRTC video stalled');
    await advance(15000);
    assert.equal(peers.length, 2);
    peers[1].state('connected');
    video.frame();
    assert.equal(metric('path'), 'WebRTC');
    assert.equal(metric('connection'), 'Connected');
    assert.equal(image.dataset.fallbackReason, undefined);
  } else if (scenario === 'independent_rates') {
    peers[0].state('connected');
    for (let i = 0; i < 30; i++) { await advance(100); video.frame(); }
    assert.equal(metric('rate'), 'View 10.0 FPS');
    await advance(2000); // Another telemetry poll must not replace view FPS.
    assert.equal(metric('rate'), 'View 10.0 FPS');
    assert.equal(metric('input-rate'), 'Input 5.2 FPS, skipped unknown/s');
    assert.equal(peers[0].closed, undefined);
  } else if (scenario === 'brief_disconnect') {
    peers[0].state('connected'); video.frame();
    peers[0].state('disconnected');
    await advance(4000);
    peers[0].state('connected'); video.frame();
    await advance(1000);
    assert.equal(peers[0].closed, undefined);
    peers[0].state('disconnected');
    await advance(5000);
    assert.equal(metric('connection'), 'WebRTC disconnected');
  } else if (scenario === 'page_closed' || scenario === 'restored_page') {
    peers[0].state('connected'); video.frame();
    window.dispatchEvent(new Event('pagehide'));
    await advance(60000);
    assert.equal(peers[0].closed, true);
    assert.equal(signal.aborted, true);
    assert.equal(peers.length, 1);
    if (scenario === 'restored_page') {
      window.dispatchEvent({type: 'pageshow', persisted: true});
      assert.equal(window.reloaded, true);
    }
  } else if (scenario === 'background_tab') {
    peers[0].state('connected'); video.frame();
    document.hidden = true;
    await advance(30000);
    assert.equal(peers[0].closed, undefined);
    document.hidden = false;
    video.frame();
    assert.equal(metric('connection'), 'Connected');
  } else if (scenario === 'empty_recording') {
    assert.equal(recordingLabel.textContent, 'No recent video indexed');
  } else if (scenario === 'expired_session') {
    assert.equal(window.redirectedTo, '/login');
  } else throw new Error('Unknown scenario');
})().catch((error) => { console.error(error); process.exitCode = 1; });
