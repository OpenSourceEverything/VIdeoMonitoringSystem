"""Small, platform-neutral local web interface for the product.

The web layer knows only the product vocabulary and an injected application
facade.  Platform adapters, persistence, and media-writing code live behind
that facade.
"""

from __future__ import annotations

import hmac
import io
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from socketserver import ThreadingMixIn
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import segno

from .configuration import ProvisioningConfig, build_provisioning_config
from .model import SOFTWARE_CAMERA_LIMIT
from .notifications import PushNotificationError
from .regulatory import validate_wifi_country
from .verification import (
    HEALTH_ATTENTION,
    HEALTH_NOT_CHECKED,
    HEALTH_OK,
    CameraHealth,
    VerificationReport,
    camera_health_summary,
    camera_support_report,
)
from .device.journal_reader import (
    ALLOWED_LOG_LINES,
    CAMERA_LOG_SOURCES,
    RECORDER_LOG_SOURCES,
)


@runtime_checkable
class ApplicationFacade(Protocol):
    """Operations used by both the local GUI and an equivalent CLI."""

    def status(self) -> Mapping[str, object]: ...

    def list_cameras(self) -> Sequence[Mapping[str, object]]: ...

    def register_camera_endpoint(
        self, *, payload: bytes, signature: str, source_address: str
    ) -> Mapping[str, object]: ...

    def create_owner(self, *, name: str, password: str) -> object: ...

    def authenticate_owner(self, *, password: str) -> bool: ...

    def change_owner_password(
        self, *, current_password: str, new_password: str
    ) -> object: ...

    def create_backup_download(self, *, password: str) -> tuple[bytes, str, object]: ...

    def initialize_storage(self) -> object: ...

    def list_recording_storage_targets(self) -> Sequence[object]: ...

    def initialize_selected_recording_storage(
        self, *, stable_id: str, confirmation: str
    ) -> object: ...

    def claim_camera(
        self, *, camera_id: str, claim_code: str, name: str
    ) -> object: ...

    def claim_camera_payload(
        self, *, claim_payload: str, name: str
    ) -> object: ...

    def claim_camera_code(self, *, camera_code: str, name: str) -> object: ...

    def camera_setup_status(
        self, *, camera_id: str
    ) -> Mapping[str, object] | None: ...

    def retry_camera_network_setup(
        self, *, camera_id: str
    ) -> Mapping[str, object]: ...

    def resolve_camera_identity_conflict(
        self, *, camera_id: str
    ) -> Mapping[str, object]: ...

    def name_camera(self, *, camera_id: str, name: str) -> object: ...

    def position_camera(self, *, camera_id: str, position: int) -> object: ...

    def set_camera_recording(self, *, camera_id: str, enabled: bool) -> object: ...

    def set_alerts_enabled(self, *, enabled: bool) -> object: ...

    def set_camera_alerts(self, *, camera_id: str, enabled: bool) -> object: ...

    def verify_recorder_camera(self, *, camera_id: str) -> VerificationReport: ...

    def set_recording(self, *, mode: str, days: int) -> object: ...

    def begin_private_remote_access(self) -> Mapping[str, object]: ...

    def disable_private_remote_access(self) -> Mapping[str, object]: ...

    def setup_owner_gateway_access(
        self, *, domain: str, enrollment_code: str
    ) -> Mapping[str, object]: ...

    def push_public_key(self) -> str: ...

    def subscribe_push(self, subscription: Mapping[str, object]) -> str: ...

    def unsubscribe_push(self, endpoint: str) -> None: ...

    def send_test_push(self, endpoint: str) -> None: ...

    def recording_storage_status(self) -> Mapping[str, object]: ...

    def list_recordings(
        self, *, limit: int = 100
    ) -> Sequence[Mapping[str, object]]: ...

    def list_video_events(
        self, *, limit: int = 100, camera_id: str | None = None
    ) -> Sequence[Mapping[str, object]]: ...

    def video_engine_status(self) -> Mapping[str, object]: ...

    def read_video_event_thumbnail(self, *, event_id: str) -> bytes: ...

    def read_video_event_preview(self, *, event_id: str) -> bytes: ...

    def read_video_event_vod(
        self, *, event_id: str, resource: str
    ) -> tuple[bytes, str]: ...

    def save_video_event(self, *, event_id: str) -> Mapping[str, object]: ...

    def delete_video_event(self, *, event_id: str) -> None: ...

    def list_saved_video_clips(
        self, *, limit: int = 100, camera_id: str | None = None
    ) -> Sequence[Mapping[str, object]]: ...

    def read_saved_video_clip(self, *, clip_id: str) -> bytes: ...

    def delete_saved_video_clip(self, *, clip_id: str) -> None: ...

    def read_recording_media(self, *, reference: str) -> tuple[bytes, str]: ...

    def read_recording_preview(self, *, reference: str) -> bytes: ...

    def read_live_snapshot(self, *, camera_id: str) -> bytes: ...

    def camera_live_metrics(self, *, camera_id: str) -> Mapping[str, object]: ...

    def camera_recording_coverage(self, *, camera_id: str) -> Mapping[str, object]: ...

    def diagnostic_history(self) -> Mapping[str, object]: ...

    def read_diagnostic_log(
        self,
        *,
        target: str,
        component: str,
        lines: int,
        camera_id: str | None = None,
    ) -> Mapping[str, object]: ...

    def negotiate_live_webrtc(self, *, camera_id: str, offer: str) -> str: ...

    def prepare_provisioning(
        self, request: ProvisioningConfig, **options: object
    ) -> object: ...

    def provision_selected_camera(
        self, request: ProvisioningConfig, **options: object
    ) -> object: ...

    def flash_next(self) -> object: ...

    def list_media_targets(self) -> Sequence[object]: ...

    def flash_selected(self, *, stable_id: str, confirmation: str) -> object: ...

    def verify_kit(self) -> object: ...

    def pilot_diagnostics(self) -> Sequence[Mapping[str, object]]: ...


_NAVIGATION = (
    ("Feeds", "/"),
    ("Settings", "/settings"),
)

_NOTICES = {
    "owner-created": "Owner created.",
    "owner-password-changed": "Password changed.",
    "storage-ready": "Recording storage is ready.",
    "camera-claimed": "Camera added.",
    "camera-named": "Camera named.",
    "camera-moved": "Camera moved.",
    "camera-recording-saved": "Camera recording setting saved.",
    "alerts-saved": "Alert setting saved.",
    "camera-alerts-saved": "Camera alert setting saved.",
    "camera-checked": "Camera checks completed.",
    "camera-setup-retried": "Secure camera setup restarted.",
    "camera-conflict-resolved": "Network conflict cleared. The camera will be checked again.",
    "recording-saved": "Recording settings saved.",
    "event-saving": "Saving clip. It will appear under Saved Clips shortly.",
    "event-deleted": "Event deleted.",
    "saved-clip-deleted": "Saved clip deleted.",
    "remote-ready": "Remote access is ready.",
    "remote-approval": "Approve this recorder once in Tailscale.",
    "remote-disabled": "Remote access is off. Local access still works.",
    "remote-gateway-ready": "The verified owner-controlled address is ready.",
    "push-subscribed": "Notifications enabled on this phone.",
    "push-unsubscribed": "Notifications disabled on this phone.",
    "push-test-sent": "Push service accepted the test. Check this phone for arrival.",
    "kit-created": "Kit created. Insert the first card when ready.",
    "camera-provisioned": "Camera provisioned.",
    "flash-complete": "Card completed. Continue with the next item.",
    "kit-verified": "Kit verification completed.",
}

_CAMERA_MODELS = {
    "zero-w": "Raspberry Pi Zero W",
    "zero-2-w": "Raspberry Pi Zero 2 W",
}
_MIN_OWNER_PASSWORD_LENGTH = 12
_HLS_RESOURCE = re.compile(
    r"(?:master|index(?:-v[0-9]+)?)\.m3u8|[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.(?:m4s|mp4|ts)"
)
_HLS_URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')


class _FormValidationError(ValueError):
    """A safe validation message that can be shown to the owner."""


_MAX_BODY_BYTES = 64 * 1024
_SESSION_COOKIE = "vms_session"
_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_MAX_SESSIONS = 32
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_FAILURE_WINDOW_SECONDS = 60
_LIVE_REFRESH_SCRIPT = b"""(() => {
  window.addEventListener('pageshow', (event) => {
    // A restored page has peers closed by pagehide; get a fresh live session.
    if (event.persisted) window.location.reload();
  });
  const grid = document.querySelector('.live-grid');
  const offlineSummary = document.querySelector('[data-offline-summary]');
  let signInRequired = false;
  const requireSession = (response) => {
    if (response.status === 401) {
      if (!signInRequired) {
        signInRequired = true;
        window.location.assign('/login');
      }
      throw new Error('Sign in is required');
    }
  };
  const setLiveMetric = (card, metric, value) => {
    for (const target of card?.querySelectorAll(`[data-live-${metric}]`) || []) {
      target.textContent = value;
    }
  };
  const updateRecordingStatus = (card) => {
    const label = card.querySelector('[data-recording-alert]');
    if (!label) return;
    label.hidden = false;
    const savedAt = Number(card.dataset.lastRecordedAt);
    const age = Math.max(0, Math.round(Date.now() / 1000 - savedAt));
    let text = 'Checking saved video';
    let warning = false;
    if (card.dataset.recordingEnabled === 'false') text = 'Recording paused';
    else if (card.dataset.engineBound === 'false') text = 'Saved video not configured';
    else if (card.dataset.detectStopped === 'true') {
      text = 'No video input';
      warning = true;
    } else if (card.dataset.recordingStatus === 'indexed' && Number.isFinite(savedAt)) {
      text = age < 60 ? `Video indexed ${age}s ago` : `Video indexed ${Math.floor(age / 60)}m ago`;
    } else if (card.dataset.recordingStatus === 'none') text = 'No recent video indexed';
    else if (card.dataset.recordingStatus === 'unavailable') {
      text = 'Saved-video check unavailable';
      warning = true;
    }
    label.textContent = text;
    label.classList.toggle('recording-alert', warning);
  };
  const updateOfflineSummary = () => {
    if (!grid || !offlineSummary) return;
    const offline = Array.from(
      grid.querySelectorAll('[data-camera-card]')
    ).filter((card) => card.hidden || card.dataset.liveOffline === 'true');
    const count = offlineSummary.querySelector('[data-offline-count]');
    const names = offlineSummary.querySelector('[data-offline-names]');
    if (count) count.textContent = offline.length.toString();
    if (names) {
      names.textContent = offline.map((card) => card.dataset.cameraName).join(', ');
    }
    offlineSummary.hidden = offline.length === 0;
  };

  for (const image of document.querySelectorAll('img[data-live-source]')) {
    const card = image.closest('.live-card');
    const status = card?.querySelector('[data-live-status]');
    let consecutiveFailures = 0;
    let snapshotFrames = 0;
    let snapshotStartedAt = window.performance.now();
    let snapshotTimer = null;
    const refresh = () => {
      if (image.dataset.livePaused === 'true') return;
      const source = new URL(image.dataset.liveSource, window.location.href);
      source.searchParams.set('refresh', Date.now().toString());
      image.src = source.toString();
    };
    const schedule = () => {
      window.clearTimeout(snapshotTimer);
      snapshotTimer = window.setTimeout(refresh, 1500);
    };
    image.addEventListener('load', () => {
      if (image.dataset.livePaused === 'true') return;
      consecutiveFailures = 0;
      image.classList.remove('offline');
      if (card) {
        card.hidden = false;
        delete card.dataset.liveOffline;
      }
      if (status) status.textContent = 'Live';
      if (image.dataset.livePaused !== 'true') {
        snapshotFrames += 1;
        const now = window.performance.now();
        const elapsed = now - snapshotStartedAt;
        setLiveMetric(card, 'path', image.dataset.fallbackReason ? 'Snapshots (fallback)' : 'Snapshots');
        setLiveMetric(card, 'connection', image.dataset.fallbackReason || 'Connected');
        if (elapsed >= 3000) {
          const fps = snapshotFrames * 1000 / elapsed;
          setLiveMetric(card, 'rate', `View ${fps.toFixed(1)} FPS`);
          snapshotFrames = 0;
          snapshotStartedAt = now;
        }
      }
      updateOfflineSummary();
      schedule();
    });
    image.addEventListener('error', () => {
      if (image.dataset.livePaused === 'true') return;
      consecutiveFailures += 1;
      image.classList.add('offline');
      if (status) status.textContent = consecutiveFailures < 3 ? 'Reconnecting' : 'Offline';
      if (card && consecutiveFailures >= 3) {
        card.dataset.liveOffline = 'true';
        card.hidden = true;
      }
      updateOfflineSummary();
      schedule();
    });
    window.addEventListener('pagehide', () => {
      image.dataset.livePaused = 'true';
      window.clearTimeout(snapshotTimer);
    });
    refresh();
  }

  const waitForIce = (connection) => new Promise((resolve) => {
    if (connection.iceGatheringState === 'complete') {
      resolve();
      return;
    }
    const timeout = window.setTimeout(resolve, 3000);
    connection.addEventListener('icegatheringstatechange', () => {
      if (connection.iceGatheringState === 'complete') {
        window.clearTimeout(timeout);
        resolve();
      }
    });
  });
  for (const video of document.querySelectorAll('video[data-live-webrtc]')) {
    const view = video.closest('[data-fullscreen-view]');
    const image = view?.querySelector('img[data-live-source]');
    const card = video.closest('.live-card');
    const status = card?.querySelector('[data-live-status]');
    if (!image) continue;
    if (card?.dataset.engineBound === 'false') {
      setLiveMetric(card, 'path', 'Snapshots');
      setLiveMetric(card, 'connection', 'Connected through snapshots');
      continue;
    }
    if (!window.RTCPeerConnection) {
      setLiveMetric(card, 'path', 'Snapshots');
      setLiveMetric(card, 'connection', 'WebRTC unsupported');
      continue;
    }
    let retryTimer = null;
    let stopped = false;
    let stopAttempt = () => {};
    window.addEventListener('pagehide', () => {
      stopped = true;
      window.clearTimeout(retryTimer);
      stopAttempt();
    });
    const startWebrtc = () => {
    if (stopped) return;
    const connection = new RTCPeerConnection({bundlePolicy: 'max-bundle'});
    const signaling = new AbortController();
    let frameMonitorStarted = false;
    let didFallback = false;
    let disconnectedTimer = null;
    let frameTimer = null;
    let attemptTimer = null;
    stopAttempt = () => {
      didFallback = true;
      window.clearTimeout(attemptTimer);
      window.clearTimeout(disconnectedTimer);
      window.clearInterval(frameTimer);
      signaling.abort();
      connection.close();
    };
    const monitorFrames = () => {
      if (frameMonitorStarted) return;
      frameMonitorStarted = true;
      let frames = 0;
      let startedAt = window.performance.now();
      let lastFrameAt = startedAt;
      const measureFrame = (now, count = 1) => {
        if (didFallback) return;
        lastFrameAt = now;
        window.clearTimeout(attemptTimer);
        delete image.dataset.fallbackReason;
        if (card) card.hidden = false;
        if (status) status.textContent = 'Live';
        setLiveMetric(card, 'connection', 'Connected');
        updateOfflineSummary();
        frames += count;
        const elapsed = now - startedAt;
        if (elapsed >= 3000) {
          const fps = frames * 1000 / elapsed;
          setLiveMetric(card, 'rate', `View ${fps.toFixed(1)} FPS`);
          frames = 0;
          startedAt = now;
        }
      };
      const frame = (now) => {
        if (didFallback) return;
        measureFrame(now);
        video.requestVideoFrameCallback(frame);
      };
      if (video.requestVideoFrameCallback) {
        video.requestVideoFrameCallback(frame);
      } else if (!video.getVideoPlaybackQuality) {
        setLiveMetric(card, 'rate', 'Browser cannot measure');
      }
      let previousDecoded = video.getVideoPlaybackQuality?.().totalVideoFrames || 0;
      frameTimer = window.setInterval(() => {
        if (didFallback) return;
        const now = window.performance.now();
        // Background tabs may suspend presentation, not camera delivery.
        if (document.hidden) {
          lastFrameAt = now;
          return;
        }
        if (!video.requestVideoFrameCallback && video.getVideoPlaybackQuality) {
          const decoded = video.getVideoPlaybackQuality().totalVideoFrames;
          if (decoded > previousDecoded) measureFrame(now, decoded - previousDecoded);
          previousDecoded = decoded;
        }
        if (now - lastFrameAt >= 10000) fallback('WebRTC video stalled');
      }, 2000);
    };
    const fallback = (reason) => {
      if (didFallback) return;
      stopAttempt();
      video.srcObject = null;
      video.hidden = true;
      image.hidden = false;
      image.dataset.livePaused = 'false';
      image.dataset.fallbackReason = reason;
      setLiveMetric(card, 'path', 'Snapshots (fallback)');
      setLiveMetric(card, 'connection', reason);
      setLiveMetric(card, 'rate', 'Measuring view');
      if (status) status.textContent = 'Reconnecting';
      image.dispatchEvent(new Event('error'));
      // A failed WebRTC attempt must not strand the owner permanently on the
      // deliberately low-rate snapshot path until a full page reload.
      if (!retryTimer && !stopped) {
        retryTimer = window.setTimeout(() => {
          retryTimer = null;
          startWebrtc();
        }, 15000);
      }
    };
    attemptTimer = window.setTimeout(() => fallback('WebRTC start timed out'), 20000);
    connection.addTransceiver('video', {direction: 'recvonly'});
    connection.addEventListener('track', (event) => {
      if (didFallback) return;
      const stream = event.streams[0] || new MediaStream([event.track]);
      video.srcObject = stream;
    });
    connection.addEventListener('connectionstatechange', () => {
      if (didFallback) return;
      const state = connection.connectionState;
      if (state !== 'connected' && state !== 'failed' && state !== 'closed') {
        setLiveMetric(card, 'connection', state === 'new' ? 'Starting' : state);
      }
      if (connection.connectionState === 'connected') {
        if (disconnectedTimer) {
          window.clearTimeout(disconnectedTimer);
          disconnectedTimer = null;
        }
        image.dataset.livePaused = 'true';
        image.hidden = true;
        video.hidden = false;
        video.play().catch(() => fallback('Browser could not play video'));
        setLiveMetric(card, 'path', 'WebRTC');
        setLiveMetric(card, 'connection', 'Waiting for video');
        setLiveMetric(card, 'rate', 'Measuring view');
        monitorFrames();
        if (status) status.textContent = 'Starting video';
      } else if (connection.connectionState === 'disconnected') {
        if (!disconnectedTimer) {
          disconnectedTimer = window.setTimeout(() => {
            disconnectedTimer = null;
            if (connection.connectionState === 'disconnected') {
              fallback('WebRTC disconnected');
            }
          }, 5000);
        }
      } else if (connection.connectionState === 'failed') {
        fallback('WebRTC connection failed');
      }
    });
    (async () => {
      try {
        await connection.setLocalDescription(await connection.createOffer());
        await waitForIce(connection);
        if (didFallback) return;
        const response = await fetch(video.dataset.liveWebrtc, {
          method: 'POST',
          body: connection.localDescription.sdp,
          headers: {'Content-Type': 'application/sdp'},
          credentials: 'same-origin',
          cache: 'no-store',
          signal: signaling.signal,
        });
        requireSession(response);
        if (!response.ok) throw new Error('WebRTC signaling rejected');
        const answer = await response.text();
        if (didFallback) return;
        await connection.setRemoteDescription({type: 'answer', sdp: answer});
      } catch (_) {
        fallback('WebRTC signaling failed');
      }
    })();
    };
    startWebrtc();
  }
  updateOfflineSummary();

  const cards = Array.from(document.querySelectorAll('[data-camera-card]'));
  const refreshRecording = async (card) => {
    if (card.dataset.recordingPending === 'true') return;
    if (card.dataset.recordingEnabled === 'false' || card.dataset.engineBound === 'false') {
      updateRecordingStatus(card);
      return;
    }
    const check = card.querySelector('[data-recording-coverage-url]');
    if (!check) return;
    card.dataset.recordingPending = 'true';
    try {
      const response = await fetch(check.dataset.recordingCoverageUrl, {
        credentials: 'same-origin', cache: 'no-store',
        signal: AbortSignal.timeout(8000),
      });
      requireSession(response);
      if (!response.ok) throw new Error('coverage unavailable');
      const coverage = (await response.json()).coverage || {};
      card.dataset.recordingStatus = coverage.state === 'indexed' || coverage.state === 'none'
        ? coverage.state : 'unavailable';
      if (Number.isFinite(coverage.latest_segment_end)) {
        card.dataset.lastRecordedAt = coverage.latest_segment_end.toString();
      } else delete card.dataset.lastRecordedAt;
      setLiveMetric(card, 'recording-detail', coverage.state === 'indexed'
        ? 'Retained video indexed; playback not automatically verified'
        : coverage.state === 'none' ? 'None retained in last 5 minutes; may be normal in event-only mode'
        : 'Retained-video check unavailable');
    } catch (_) {
      card.dataset.recordingStatus = 'unavailable';
      delete card.dataset.lastRecordedAt;
    } finally {
      card.dataset.recordingPending = 'false';
      updateRecordingStatus(card);
    }
  };
  const refreshSignal = async (card) => {
    if (!card.dataset.liveMetrics || card.dataset.metricsPending === 'true') return;
    card.dataset.metricsPending = 'true';
    try {
      const response = await fetch(card.dataset.liveMetrics, {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: AbortSignal.timeout(8000),
      });
      requireSession(response);
      if (!response.ok) throw new Error('telemetry unavailable');
      const metrics = (await response.json()).metrics || {};
      const signal = card.querySelector('[data-live-signal]');
      if (Number.isFinite(metrics.rssi_dbm)) {
        if (signal) signal.hidden = false;
        setLiveMetric(card, 'signal', `${metrics.rssi_dbm} dBm`);
        setLiveMetric(card, 'signal-detail', `${metrics.rssi_dbm} dBm`);
      } else if (Number.isFinite(metrics.signal_percent)) {
        if (signal) signal.hidden = false;
        setLiveMetric(card, 'signal', `Signal ${metrics.signal_percent}%`);
        setLiveMetric(card, 'signal-detail', `${metrics.signal_percent}%`);
      } else if (['excellent', 'good', 'fair', 'poor', 'unusable'].includes(metrics.connection_quality)) {
        if (signal) signal.hidden = false;
        const quality = metrics.connection_quality[0].toUpperCase() + metrics.connection_quality.slice(1);
        setLiveMetric(card, 'signal', `Input ${quality}`);
        setLiveMetric(card, 'signal-detail', `RSSI not reported; recorder input is ${quality.toLowerCase()}`);
      } else {
        if (signal) signal.hidden = false;
        setLiveMetric(card, 'signal', 'RSSI unavailable');
        setLiveMetric(card, 'signal-detail', 'Not reported by this camera');
      }
      card.dataset.detectStopped = (metrics.detect_input_state === 'stopped').toString();
      updateRecordingStatus(card);
      if (Number.isFinite(metrics.detect_input_fps)) {
        const inputRate = metrics.detect_input_fps.toFixed(1);
        const skipped = Number.isFinite(metrics.skipped_fps)
          ? metrics.skipped_fps.toFixed(1)
          : 'unknown';
        setLiveMetric(card, 'input-rate', `Input ${inputRate} FPS, skipped ${skipped}/s`);
        setLiveMetric(card, 'detect-input-rate', `${inputRate} FPS`);
      } else {
        setLiveMetric(card, 'input-rate', 'Detect input unavailable');
        setLiveMetric(card, 'detect-input-rate', 'Unavailable');
      }
      setLiveMetric(
        card,
        'processed-rate',
        Number.isFinite(metrics.processed_fps) ? `${metrics.processed_fps.toFixed(1)} FPS` : 'Unavailable',
      );
      setLiveMetric(
        card,
        'object-rate',
        Number.isFinite(metrics.object_detection_fps) ? `${metrics.object_detection_fps.toFixed(1)}/s` : 'Unavailable',
      );
      setLiveMetric(
        card,
        'skip-rate',
        Number.isFinite(metrics.skipped_fps) ? `${metrics.skipped_fps.toFixed(1)}/s` : 'Unavailable',
      );
      const stalls = Number.isFinite(metrics.source_stalls_last_hour)
        ? metrics.source_stalls_last_hour
        : 0;
      const reconnects = Number.isFinite(metrics.source_reconnects_last_hour)
        ? metrics.source_reconnects_last_hour
        : 0;
      setLiveMetric(card, 'source-health', `${stalls} stalls / ${reconnects} reconnects in 1 hour`);
      const cameraStream = metrics.camera_media_state === 'advancing'
        ? 'Frames reached recorder in last check'
        : metrics.camera_media_state === 'unavailable'
          ? 'No advancing camera stream in last check'
          : 'Not recently tested';
      setLiveMetric(card, 'camera-stream', cameraStream);
      const diagnoses = {
        'weak-camera-signal': 'Weak camera Wi-Fi signal',
        'detect-input-stopped': '0 Frigate detect input FPS; saved recording status is unknown',
        'detect-input-unverified': 'Frigate detect input has not been measured',
        'camera-control-unreachable': 'Camera control endpoint is unreachable',
        'camera-control-only-reachable': 'Camera control responds; video path still needs checking',
        'camera-media-unavailable': 'Camera stream did not reach recorder; check camera, power, and network',
        'recorder-detect-ingest-failed': 'Camera stream reached recorder; Frigate detect ingest path is failing',
        'source-interruptions': 'Recorder input is interrupted; camera versus recorder cause is not yet proven',
        'detection-not-keeping-up': 'Object detection is skipping frames; saved recording status is unknown',
        'recorder-overloaded': 'Recorder CPU is overloaded',
        'legacy-transcoding': 'Legacy camera requires recorder video conversion',
        'healthy': 'No current detect-input fault; saved recording not verified',
      };
      const findingCodes = Array.isArray(metrics.diagnoses) ? metrics.diagnoses : [metrics.diagnosis];
      const findings = findingCodes.map((code) => diagnoses[code]).filter(Boolean);
      setLiveMetric(card, 'diagnosis', findings.join('; ') || 'Cause not yet identified');
      const codec = metrics.source_codec === 'h264' ? 'Native H.264' :
        metrics.source_codec === 'mjpeg' ? 'MJPEG (recorder conversion required)' : 'Unknown';
      setLiveMetric(card, 'source-format', codec);
      const recorderLoad = Number.isFinite(metrics.recorder_cpu_percent)
        ? `${metrics.recorder_cpu_percent.toFixed(1)}% CPU`
        : 'Not reported';
      setLiveMetric(card, 'recorder-load', recorderLoad);
    } catch (_) {
      const signal = card.querySelector('[data-live-signal]');
      if (signal) signal.hidden = false;
      setLiveMetric(card, 'signal', 'RSSI unavailable');
      setLiveMetric(card, 'input-rate', 'Detect input unavailable');
      setLiveMetric(card, 'detect-input-rate', 'Unavailable');
      setLiveMetric(card, 'processed-rate', 'Unavailable');
      setLiveMetric(card, 'object-rate', 'Unavailable');
      setLiveMetric(card, 'skip-rate', 'Unavailable');
      setLiveMetric(card, 'camera-stream', 'Check unavailable');
      setLiveMetric(card, 'diagnosis', 'Diagnostics temporarily unavailable');
    } finally {
      card.dataset.metricsPending = 'false';
    }
  };
  for (const card of cards) {
    refreshSignal(card);
    refreshRecording(card);
    window.setInterval(() => refreshSignal(card), 5000);
    window.setInterval(() => refreshRecording(card), 15000);
    const check = card.querySelector('[data-recording-coverage-url]');
    const result = card.querySelector('[data-recording-coverage-result]');
    check?.addEventListener('click', async () => {
      if (!result) return;
      check.disabled = true;
      result.textContent = 'Checking retained video...';
      try {
        const response = await fetch(check.dataset.recordingCoverageUrl, {
          credentials: 'same-origin', cache: 'no-store',
        });
        if (!response.ok) throw new Error('coverage unavailable');
        const coverage = (await response.json()).coverage || {};
        if (coverage.state === 'indexed' &&
            Number.isFinite(coverage.indexed_seconds) &&
            Number.isFinite(coverage.window_seconds) &&
            Number.isFinite(coverage.largest_gap_seconds)) {
          result.textContent = `Indexed main-stream video: ${coverage.indexed_seconds.toFixed(1)} of ` +
            `${coverage.window_seconds}s; largest gap ${coverage.largest_gap_seconds.toFixed(1)}s. ` +
            'Index only; playback unverified.';
        } else if (coverage.state === 'none') {
          result.textContent = 'No retained main-stream video in the last 5 minutes. ' +
            'This can be normal in event-only mode; playback unverified.';
        } else {
          result.textContent = 'Retained video could not be verified.';
        }
      } catch (_) {
        result.textContent = 'Retained video check unavailable.';
      } finally {
        check.disabled = false;
      }
    });
  }
  const selectCard = (selected) => {
    for (const card of cards) {
      const active = card === selected;
      card.classList.toggle('selected', active);
      const selector = card.querySelector('[data-select-camera]');
      const actions = card.querySelector('[data-camera-actions]');
      const renameForm = card.querySelector('[data-camera-rename-form]');
      const diagnostics = card.querySelector('[data-camera-diagnostics]');
      if (selector) selector.setAttribute('aria-expanded', active.toString());
      if (actions) actions.hidden = !active;
      if (renameForm && !active) renameForm.hidden = true;
      if (diagnostics) {
        if (!active) diagnostics.open = false;
      }
    }
  };
  for (const card of cards) {
    card.querySelector('[data-select-camera]')?.addEventListener('click', () => {
      selectCard(card);
    });
    const renameForm = card.querySelector('[data-camera-rename-form]');
    card.querySelector('[data-rename-camera]')?.addEventListener('click', () => {
      if (!renameForm) return;
      renameForm.hidden = false;
      const input = renameForm.querySelector('input[name="name"]');
      input?.focus();
      input?.select();
    });
    renameForm?.querySelector('[data-cancel-rename]')?.addEventListener('click', () => {
      renameForm.hidden = true;
    });
    card.querySelector('[data-fullscreen]')?.addEventListener('click', async () => {
      const view = card.querySelector('[data-fullscreen-view]');
      if (!view) return;
      try {
        if (document.fullscreenElement === view) {
          await document.exitFullscreen();
        } else if (view.requestFullscreen) {
          await view.requestFullscreen();
        }
      } catch (_) {
        // Fullscreen may be disabled by browser or embedding policy.
      }
    });
  }

  let linked = null;
  try {
    linked = window.location.hash
      ? document.querySelector(window.location.hash)
      : null;
  } catch (_) {
    linked = null;
  }
  if (linked) {
    const card = linked.closest('[data-camera-card]');
    if (card) selectCard(card);
    if (linked.matches('details')) linked.open = true;
  }

  const orderForm = document.querySelector('[data-camera-order-form]');
  let dragged = null;
  if (orderForm && grid) {
    for (const handle of document.querySelectorAll('[data-drag-camera]')) {
      const card = handle.closest('[data-camera-card]');
      handle.addEventListener('dragstart', (event) => {
        dragged = card;
        card?.classList.add('dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', card?.dataset.cameraSlot || '');
      });
      handle.addEventListener('dragend', () => {
        card?.classList.remove('dragging');
        dragged = null;
      });
    }
    for (const card of cards) {
      card.addEventListener('dragover', (event) => {
        if (!dragged || dragged === card) return;
        event.preventDefault();
        const bounds = card.getBoundingClientRect();
        const sameRow = Math.abs(event.clientY - (bounds.top + bounds.height / 2))
          < bounds.height / 3;
        const before = sameRow
          ? event.clientX < bounds.left + bounds.width / 2
          : event.clientY < bounds.top + bounds.height / 2;
        grid.insertBefore(dragged, before ? card : card.nextSibling);
      });
    }
    grid.addEventListener('drop', (event) => {
      if (!dragged) return;
      event.preventDefault();
      const ordered = Array.from(grid.querySelectorAll('[data-camera-card]'));
      const position = ordered.indexOf(dragged) + 1;
      orderForm.querySelector('[name="slot"]').value = dragged.dataset.cameraSlot;
      orderForm.querySelector('[name="position"]').value = position.toString();
      orderForm.requestSubmit();
    });
  }
})();
"""

_EVENTS_SCRIPT = b"""(() => {
  for (const time of document.querySelectorAll('time[data-event-time]')) {
    const parsed = new Date(time.dateTime);
    if (!Number.isNaN(parsed.valueOf())) {
      time.textContent = parsed.toLocaleString([], {
        dateStyle: 'medium',
        timeStyle: 'short',
      });
    }
  }
  for (const button of document.querySelectorAll('[data-event-toggle]')) {
    const image = button.querySelector('img');
    const label = button.querySelector('[data-event-label]');
    if (!image || !label) continue;
    button.addEventListener('click', () => {
      const playing = button.getAttribute('aria-pressed') === 'true';
      button.setAttribute('aria-pressed', (!playing).toString());
      image.src = playing ? image.dataset.previewSource : button.dataset.eventSource;
      label.textContent = playing ? 'Play' : 'Stop';
    });
  }
  for (const button of document.querySelectorAll('[data-event-video]')) {
    button.addEventListener('click', async () => {
      const video = document.createElement('video');
      video.controls = true;
      video.playsInline = true;
      video.preload = 'metadata';
      video.setAttribute('aria-label', button.dataset.eventLabel || 'Event video');
      if (button.dataset.eventPoster) {
        video.poster = button.dataset.eventPoster;
      }
      if (button.dataset.eventVod) {
        const hls = document.createElement('source');
        hls.src = button.dataset.eventVod;
        hls.type = 'application/vnd.apple.mpegurl';
        video.append(hls);
      }
      if (button.dataset.eventPreview) {
        const preview = document.createElement('source');
        preview.src = button.dataset.eventPreview;
        preview.type = 'video/mp4';
        video.append(preview);
      }
      button.replaceWith(video);
      video.load();
      try {
        await video.play();
      } catch (_) {
        // The controls remain available if this browser requires a second tap.
      }
    }, {once: true});
  }
  if (document.querySelector('[data-active-event]')) {
    window.setTimeout(() => window.location.reload(), 3000);
  }
})();
"""

_REMOTE_ACCESS_SCRIPT = b"""(() => {
  const form = document.querySelector('[data-remote-toggle-form]');
  const toggle = form?.querySelector('[data-remote-toggle]');
  toggle?.addEventListener('change', () => {
    toggle.disabled = true;
    form.requestSubmit();
  });

  const feedback = document.querySelector('[data-remote-feedback]');
  const report = (message) => {
    if (feedback) feedback.textContent = message;
  };
  const copy = async (value) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch (_) {
      const field = document.createElement('textarea');
      field.value = value;
      field.setAttribute('readonly', '');
      field.style.position = 'fixed';
      field.style.opacity = '0';
      document.body.appendChild(field);
      field.select();
      document.execCommand('copy');
      field.remove();
    }
    report('Link copied.');
  };
  for (const button of document.querySelectorAll('[data-copy-link]')) {
    button.addEventListener('click', () => copy(button.dataset.copyLink));
  }
  for (const button of document.querySelectorAll('[data-share-link]')) {
    button.addEventListener('click', async () => {
      const url = button.dataset.shareLink;
      if (navigator.share) {
        try {
          await navigator.share({title: 'OSE Cameras', url});
          report('Link shared.');
          return;
        } catch (error) {
          if (error?.name === 'AbortError') return;
        }
      }
      await copy(url);
      report('Sharing is unavailable here, so the link was copied.');
    });
  }

  let installPrompt = null;
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installPrompt = event;
  });
  document.querySelector('[data-install-app]')?.addEventListener('click', async () => {
    if (installPrompt) {
      installPrompt.prompt();
      await installPrompt.userChoice;
      installPrompt = null;
    } else {
      report('Open your browser menu and choose Add to Home screen.');
    }
  });

  if (document.querySelector('[data-remote-pending]')) {
    window.setInterval(async () => {
      try {
        const response = await fetch('/api/v1/status', {cache: 'no-store'});
        const result = await response.json();
        if (result?.status?.access?.state === 'ready') window.location.reload();
      } catch (_) {
        // Keep the approval screen usable if this status check is interrupted.
      }
    }, 2500);
  }
})();
"""

_PUSH_SCRIPT = b"""(() => {
  const panel = document.querySelector('[data-push-panel]');
  if (!panel) return;
  const message = panel.querySelector('[data-push-message]');
  const enable = panel.querySelector('[data-push-enable]');
  const test = panel.querySelector('[data-push-test]');
  const disable = panel.querySelector('[data-push-disable]');
  const report = (value, state = 'idle') => {
    message.textContent = value;
    message.dataset.state = state;
    panel.setAttribute('aria-busy', state === 'working' ? 'true' : 'false');
  };
  const failureText = (error, fallback) => {
    if (error?.name === 'NotAllowedError') {
      return 'Chrome blocked notifications. Allow them in Site settings, then retry.';
    }
    if (error?.name === 'AbortError') {
      return 'Chrome could not reach its push service. Check Internet access, then retry.';
    }
    const detail = typeof error?.message === 'string' ? error.message.trim() : '';
    return detail ? `${fallback} (${detail.slice(0, 180)})` : fallback;
  };
  const timeout = (promise, milliseconds) => Promise.race([
    promise,
    new Promise((_, reject) => window.setTimeout(() => {
      const error = new Error('Chrome push setup timed out.');
      error.name = 'AbortError';
      reject(error);
    }, milliseconds))
  ]);
  if (!window.isSecureContext || !('serviceWorker' in navigator) ||
      !('PushManager' in window) || !('Notification' in window)) {
    report('Unavailable here: open the private HTTPS link in Chrome on Android.', 'error');
    return;
  }
  let registration;
  let publicKey;
  const endpoint = async () => (await registration.pushManager.getSubscription())?.endpoint;
  const update = async () => {
    const subscribed = Boolean(await endpoint());
    enable.hidden = subscribed;
    test.hidden = !subscribed;
    disable.hidden = !subscribed;
    report(subscribed ? 'Enabled on this phone. Send a test to verify delivery.' :
      'Not enabled on this phone.', subscribed ? 'ready' : 'idle');
  };
  const post = async (path, fields) => {
    const body = new URLSearchParams({csrf_token: panel.dataset.csrf, ...fields});
    const response = await fetch(path, {
      method: 'POST', body, credentials: 'same-origin', cache: 'no-store'
    });
    if (!response.ok || !response.redirected ||
        !response.url.startsWith(window.location.origin + '/settings?')) {
      const reason = response.headers.get('X-VMS-Error');
      throw new Error(reason || 'Settings did not accept the request. Sign in again and retry.');
    }
  };
  const setup = async () => {
    const response = await fetch('/api/v1/push/config', {cache: 'no-store'});
    if (!response.ok) throw new Error('Sign in to enable notifications.');
    publicKey = (await response.json()).public_key;
    registration = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
    await timeout(navigator.serviceWorker.ready, 15000);
    const existing = await registration.pushManager.getSubscription();
    if (existing) {
      report('Finishing notification setup...', 'working');
      await post('/actions/push-subscribe', {subscription: JSON.stringify(existing)});
    }
    await update();
  };
  setup().catch((error) => report(
    failureText(error, 'Notifications could not start. Reload and try again.'), 'error'));

  enable.addEventListener('click', async () => {
    enable.disabled = true;
    try {
      if (!registration || !publicKey) throw new Error('Reload and try again.');
      report('Waiting for Chrome notification permission...', 'working');
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('Allow notifications in Chrome settings.');
      report('Connecting this phone...', 'working');
      const key = publicKey.replace(/-/g, '+').replace(/_/g, '/');
      const bytes = Uint8Array.from(atob(key + '='.repeat((4 - key.length % 4) % 4)),
        (character) => character.charCodeAt(0));
      const subscription = await timeout(registration.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: bytes
      }), 20000);
      report('Saving this phone on the recorder...', 'working');
      await post('/actions/push-subscribe', {subscription: JSON.stringify(subscription)});
      await update();
    } catch (error) {
      report(failureText(error, 'This phone could not enable notifications.'), 'error');
    } finally { enable.disabled = false; }
  });
  test.addEventListener('click', async () => {
    test.disabled = true;
    try {
      const current = await endpoint();
      if (!current) throw new Error('Enable notifications first.');
      await post('/actions/push-test', {endpoint: current});
      report('Push service accepted the test. Check this phone for arrival.', 'ready');
    } catch (error) {
      report(failureText(error, 'The test could not be sent.'), 'error');
    } finally { test.disabled = false; }
  });
  disable.addEventListener('click', async () => {
    disable.disabled = true;
    try {
      const current = await registration.pushManager.getSubscription();
      if (current) {
        await post('/actions/push-unsubscribe', {endpoint: current.endpoint});
        await current.unsubscribe();
      }
      await update();
    } catch (error) {
      report(failureText(error, 'Notifications could not be disabled.'), 'error');
    } finally { disable.disabled = false; }
  });
})();
"""

_SERVICE_WORKER_SCRIPT = b"""self.addEventListener('push', (event) => {
  let message = {};
  try { message = event.data?.json() || {}; } catch (_) { /* Ignore malformed payload. */ }
  const requested = typeof message.url === 'string' ? message.url : '/events';
  let target = '/events';
  try {
    const parsed = new URL(requested, self.location.origin);
    if (parsed.origin === self.location.origin && parsed.pathname === '/events') {
      target = parsed.pathname + parsed.search;
    }
  } catch (_) { /* Keep the safe events fallback. */ }
  event.waitUntil(self.registration.showNotification(
    typeof message.title === 'string' ? message.title : 'OSE Cameras', {
    body: typeof message.body === 'string' ? message.body : 'Camera activity',
    icon: '/app-icon.svg',
    tag: typeof message.tag === 'string' ? message.tag : 'ose-camera-alert',
    data: {url: target}
  }));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const requested = event.notification.data?.url;
    let target = new URL('/events', self.location.origin);
    try {
      const parsed = new URL(requested, self.location.origin);
      if (parsed.origin === self.location.origin && parsed.pathname === '/events') target = parsed;
    } catch (_) { /* Keep the safe events fallback. */ }
    for (const client of await self.clients.matchAll({type: 'window', includeUncontrolled: true})) {
      if (client.url === target.href) return client.focus();
      if (new URL(client.url).origin === self.location.origin) {
        await client.navigate(target.href);
        return client.focus();
      }
    }
    return self.clients.openWindow(target.href);
  })());
});
"""

_CAMERA_SETUP_SCRIPT = b"""(() => {
  const storageKey = 'ose-camera-claim';
  const prefix = '#add-camera?';
  if (window.location.hash.startsWith(prefix)) {
    const query = window.location.hash.slice(prefix.length);
    if (query.length > 0 && query.length <= 1900) {
      sessionStorage.setItem(storageKey, 'vms://claim?' + query);
    }
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  const pending = sessionStorage.getItem(storageKey);
  if (!pending) return;
  const input = document.querySelector('input[name="claim_payload"]');
  if (input) {
    input.value = pending;
    sessionStorage.removeItem(storageKey);
    const cameras = document.getElementById('cameras');
    if (cameras) cameras.open = true;
    const add = input.closest('details');
    if (add) add.open = true;
    input.scrollIntoView({block: 'center'});
    input.focus();
  } else if (window.location.pathname !== '/login') {
    window.location.replace('/settings#cameras');
  }
})();
"""

_WEB_MANIFEST = json.dumps(
    {
        "id": "/",
        "name": "OSE Cameras",
        "short_name": "OSE Cameras",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#edf2fb",
        "theme_color": "#50abe7",
        "icons": [
            {
                "src": "/app-icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    },
    separators=(",", ":"),
).encode("utf-8")

_APP_ICON = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="104" fill="#50abe7"/>
<path fill="#ffffff" d="M104 168h72l25-40h110l25 40h72a40 40 0 0 1 40 40v176a40 40 0 0 1-40 40H104a40 40 0 0 1-40-40V208a40 40 0 0 1 40-40Z"/>
<circle cx="256" cy="296" r="92" fill="#87ceeb"/>
<circle cx="256" cy="296" r="52" fill="#edf2fb"/>
</svg>"""


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1000:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1000
    return f"{amount:.1f} TB"


def _event_duration_text(started_at: object, ended_at: object) -> str:
    if ended_at is None:
        return "In progress"
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
        elapsed = (ended - started).total_seconds()
    except (TypeError, ValueError):
        return ""
    if elapsed < 0:
        return ""
    if elapsed < 1:
        return "Under 1 sec"
    seconds = round(elapsed)
    if seconds < 60:
        return f"{seconds} sec"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes} min {remainder} sec" if remainder else f"{minutes} min"


def _field(
    name: str,
    label: str,
    *,
    input_type: str = "text",
    autocomplete: str = "off",
    minimum: int | None = None,
    maximum: int | None = None,
    minimum_length: int | None = None,
    value: str | None = None,
    required: bool = True,
) -> str:
    limits = ""
    if minimum is not None:
        limits += f' min="{minimum}"'
    if maximum is not None:
        limits += f' max="{maximum}"'
    if minimum_length is not None:
        limits += f' minlength="{minimum_length}"'
    preset = "" if value is None else f' value="{_text(value)}"'
    requirement = " required" if required else ""
    return (
        f'<label for="{_text(name)}">{_text(label)}</label>'
        f'<input id="{_text(name)}" name="{_text(name)}" '
        f'type="{_text(input_type)}" autocomplete="{_text(autocomplete)}"'
        f"{limits}{preset}{requirement}>"
    )


class LocalWebApplication:
    """WSGI application for the recorder and factory provisioning screens."""

    def __init__(
        self,
        facade: ApplicationFacade,
        *,
        csrf_token: str | None = None,
        factory_enabled: bool = False,
        wifi_country_default: str | None = None,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self.facade = facade
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        self.factory_enabled = bool(factory_enabled)
        self._time_source = time_source
        self._sessions: dict[str, float] = {}
        self._login_failures: list[float] = []
        self._authentication_lock = threading.Lock()
        self._remote_access_lock = threading.Lock()
        self._remote_approval_url: str | None = None
        self.wifi_country_default = (
            None
            if wifi_country_default is None
            else validate_wifi_country(wifi_country_default)
        )
        if not self.csrf_token:
            raise ValueError("csrf_token must not be empty")
        if not callable(self._time_source):
            raise ValueError("time_source must be callable")

    def __call__(self, environ: dict[str, object], start_response):
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        authenticated = self._authenticated(environ)
        authentication_required = self._authentication_required()

        if method in {"GET", "HEAD"} and path == "/manifest.webmanifest":
            return self._respond(
                start_response,
                HTTPStatus.OK,
                _WEB_MANIFEST,
                head=method == "HEAD",
                content_type="application/manifest+json; charset=utf-8",
            )
        if method in {"GET", "HEAD"} and path == "/app-icon.svg":
            return self._respond(
                start_response,
                HTTPStatus.OK,
                _APP_ICON,
                head=method == "HEAD",
                content_type="image/svg+xml; charset=utf-8",
            )
        if method in {"GET", "HEAD"} and path == "/sw.js":
            return self._respond(
                start_response,
                HTTPStatus.OK,
                _SERVICE_WORKER_SCRIPT,
                head=method == "HEAD",
                content_type="text/javascript; charset=utf-8",
                extra_headers=(("Service-Worker-Allowed", "/"),),
            )
        if method in {"GET", "HEAD"} and path == "/camera-setup.js":
            return self._respond(
                start_response,
                HTTPStatus.OK,
                _CAMERA_SETUP_SCRIPT,
                head=method == "HEAD",
                content_type="text/javascript; charset=utf-8",
            )

        if method == "POST" and path == "/api/v1/camera-registration":
            return self._api_camera_registration(start_response, environ)

        if path == "/login" and method in {"GET", "HEAD"}:
            if not authentication_required or authenticated:
                return self._respond(
                    start_response,
                    HTTPStatus.SEE_OTHER,
                    "/",
                    head=method == "HEAD",
                )
            return self._respond(
                start_response,
                HTTPStatus.OK,
                self._login_page(),
                head=method == "HEAD",
            )

        public_login = path == "/actions/login" and method == "POST"
        if authentication_required and not authenticated and not public_login:
            if path == "/api/v1" or path.startswith("/api/v1/"):
                return self._api_error(
                    start_response,
                    HTTPStatus.UNAUTHORIZED,
                    message="Sign in is required.",
                    head=method == "HEAD",
                )
            if method in {"GET", "HEAD"} and not self._private_media_path(path):
                return self._respond(
                    start_response,
                    HTTPStatus.SEE_OTHER,
                    "/login",
                    head=method == "HEAD",
                )
            return self._respond(
                start_response,
                HTTPStatus.UNAUTHORIZED,
                self._login_page("Sign in to continue."),
                head=method == "HEAD",
            )

        response_headers: list[tuple[str, str]] = []
        if method in {"GET", "HEAD"}:
            if path == "/api/v1" or path.startswith("/api/v1/"):
                return self._api_get(
                    start_response,
                    path,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                )
            if path == "/live.jpg":
                return self._live_snapshot(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                )
            if path == "/live.js":
                return self._respond(
                    start_response,
                    HTTPStatus.OK,
                    _LIVE_REFRESH_SCRIPT,
                    head=method == "HEAD",
                    content_type="text/javascript; charset=utf-8",
                )
            if path == "/events.js":
                return self._respond(
                    start_response,
                    HTTPStatus.OK,
                    _EVENTS_SCRIPT,
                    head=method == "HEAD",
                    content_type="text/javascript; charset=utf-8",
                )
            if path == "/remote-access.js":
                return self._respond(
                    start_response,
                    HTTPStatus.OK,
                    _REMOTE_ACCESS_SCRIPT,
                    head=method == "HEAD",
                    content_type="text/javascript; charset=utf-8",
                )
            if path == "/push.js":
                return self._respond(
                    start_response,
                    HTTPStatus.OK,
                    _PUSH_SCRIPT,
                    head=method == "HEAD",
                    content_type="text/javascript; charset=utf-8",
                )
            if path == "/remote-access-qr.svg":
                return self._remote_access_qr(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                )
            if path == "/recording-preview.jpg":
                return self._recording_preview(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                )
            if path == "/event-thumbnail.jpg":
                return self._video_event_thumbnail(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                )
            if path == "/event-preview.mp4":
                return self._video_event_preview(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                    range_header=environ.get("HTTP_RANGE"),
                )
            if path == "/saved-clip.mp4":
                return self._video_saved_clip(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                    range_header=environ.get("HTTP_RANGE"),
                )
            if path == "/event-vod":
                return self._video_event_vod(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                )
            if path == "/recording.mjpeg":
                return self._recording_media(
                    start_response,
                    str(environ.get("QUERY_STRING", "")),
                    head=method == "HEAD",
                )
            if path == "/support-report.json":
                try:
                    body = camera_support_report(
                        self._camera_health(self.facade.list_cameras())
                    )
                    history_reader = getattr(self.facade, "diagnostic_history", None)
                    if callable(history_reader):
                        report = json.loads(body)
                        try:
                            report["diagnostics"] = history_reader()
                        except Exception:
                            report["diagnostics"] = {"state": "unavailable"}
                        body = json.dumps(report, indent=2) + "\n"
                except Exception:
                    return self._respond(
                        start_response,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        self._error_page("The support report is temporarily unavailable."),
                        head=method == "HEAD",
                    )
                return self._respond(
                    start_response,
                    HTTPStatus.OK,
                    body,
                    head=method == "HEAD",
                    content_type="application/json; charset=utf-8",
                    download_name="camera-health-support-report.json",
                )
            if path == "/diagnostics/logs.json":
                try:
                    record = self._diagnostic_log_record(
                        str(environ.get("QUERY_STRING", ""))
                    )
                    body = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
                except ValueError:
                    return self._respond(
                        start_response,
                        HTTPStatus.BAD_REQUEST,
                        self._error_page("Choose a listed device, log source, and line count."),
                        head=method == "HEAD",
                    )
                return self._respond(
                    start_response,
                    HTTPStatus.OK,
                    body,
                    head=method == "HEAD",
                    content_type="application/json; charset=utf-8",
                    download_name="vms-service-log.json",
                )
            status, body = self._get(path, str(environ.get("QUERY_STRING", "")))
        elif method == "POST":
            if path.startswith("/api/v1/cameras/") and path.endswith("/webrtc"):
                return self._api_webrtc(start_response, path, environ)
            if path == "/actions/backup":
                return self._backup_download(start_response, environ)
            status, body, response_headers = self._post(path, environ)
        else:
            status, body = HTTPStatus.METHOD_NOT_ALLOWED, self._error_page(
                "That action is not available."
            )
        return self._respond(
            start_response,
            status,
            body,
            head=method == "HEAD",
            extra_headers=response_headers,
        )

    def _authentication_required(self) -> bool:
        if self.factory_enabled:
            return False
        try:
            return self.facade.status().get("owner_ready") is True
        except Exception:
            return True

    @staticmethod
    def _private_media_path(path: str) -> bool:
        return (
            path == "/live.jpg"
            or path == "/live.js"
            or path == "/events.js"
            or path == "/remote-access.js"
            or path == "/push.js"
            or path == "/camera-setup.js"
            or path == "/remote-access-qr.svg"
            or path == "/recording-preview.jpg"
            or path == "/event-thumbnail.jpg"
            or path == "/recording.mjpeg"
            or path == "/event-preview.mp4"
            or path == "/saved-clip.mp4"
            or path == "/event-vod"
            or path == "/support-report.json"
            or path == "/diagnostics/logs.json"
            or path == "/api/v1"
            or path.startswith("/api/v1/")
        )

    def _authenticated(self, environ: Mapping[str, object]) -> bool:
        if not self._authentication_required():
            return True
        token = self._session_token(environ)
        if token is None:
            return False
        now = self._time_source()
        with self._authentication_lock:
            self._expire_sessions(now)
            return self._sessions.get(token, 0) > now

    @staticmethod
    def _session_token(environ: Mapping[str, object]) -> str | None:
        raw = str(environ.get("HTTP_COOKIE", ""))
        if not raw or len(raw) > 4096:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            return None
        morsel = cookie.get(_SESSION_COOKIE)
        if morsel is None or not 20 <= len(morsel.value) <= 128:
            return None
        return morsel.value

    def _new_session(self) -> str:
        now = self._time_source()
        with self._authentication_lock:
            self._expire_sessions(now)
            while len(self._sessions) >= _MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.get)
                del self._sessions[oldest]
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now + _SESSION_TTL_SECONDS
        return token

    def _revoke_session(self, environ: Mapping[str, object]) -> None:
        token = self._session_token(environ)
        if token is None:
            return
        with self._authentication_lock:
            self._sessions.pop(token, None)

    def _expire_sessions(self, now: float) -> None:
        expired = [
            token for token, expires_at in self._sessions.items() if expires_at <= now
        ]
        for token in expired:
            del self._sessions[token]

    def _api_get(self, start_response, path: str, query: str, *, head: bool):
        try:
            if path == "/api/v1":
                return self._api_json(
                    start_response,
                    HTTPStatus.OK,
                    {
                        "api_version": "v1",
                        "links": {
                            "status": "/api/v1/status",
                            "cameras": "/api/v1/cameras",
                            "storage": "/api/v1/storage",
                            "recordings": "/api/v1/recordings",
                        },
                    },
                    head=head,
                )
            if path == "/api/v1/status":
                return self._api_json(
                    start_response,
                    HTTPStatus.OK,
                    {"api_version": "v1", "status": self.facade.status()},
                    head=head,
                )
            if path == "/api/v1/push/config":
                if self.factory_enabled or not self._authentication_required():
                    return self._api_error(
                        start_response,
                        HTTPStatus.CONFLICT,
                        message="Create an owner before enabling notifications.",
                        head=head,
                    )
                return self._api_json(
                    start_response,
                    HTTPStatus.OK,
                    {"api_version": "v1", "public_key": self.facade.push_public_key()},
                    head=head,
                )
            if path == "/api/v1/cameras":
                return self._api_json(
                    start_response,
                    HTTPStatus.OK,
                    {
                        "api_version": "v1",
                        "cameras": self._api_cameras(),
                    },
                    head=head,
                )
            if path == "/api/v1/storage":
                return self._api_json(
                    start_response,
                    HTTPStatus.OK,
                    {
                        "api_version": "v1",
                        "storage": self.facade.recording_storage_status(),
                    },
                    head=head,
                )
            if path.startswith("/api/v1/cameras/"):
                parts = path.split("/")
                if len(parts) not in {5, 6} or not parts[4].isdigit():
                    return self._api_error(start_response, HTTPStatus.NOT_FOUND, head=head)
                slot = int(parts[4])
                camera = self._camera_at_slot(slot)
                if camera is None:
                    return self._api_error(start_response, HTTPStatus.NOT_FOUND, head=head)
                if len(parts) == 5:
                    return self._api_json(
                        start_response,
                        HTTPStatus.OK,
                        {"api_version": "v1", "camera": self._api_camera_document(slot, camera)},
                        head=head,
                    )
                if parts[5] == "setup":
                    setup = self._camera_setup_public_status(camera)
                    if not isinstance(setup, Mapping):
                        return self._api_error(
                            start_response, HTTPStatus.NOT_FOUND, head=head
                        )
                    return self._api_json(
                        start_response,
                        HTTPStatus.OK,
                        {"api_version": "v1", "setup": setup},
                        head=head,
                    )
                if parts[5] == "metrics":
                    metrics = self.facade.camera_live_metrics(
                        camera_id=str(camera.get("id", ""))
                    )
                    return self._api_json(
                        start_response,
                        HTTPStatus.OK,
                        {"api_version": "v1", "metrics": metrics},
                        head=head,
                    )
                if parts[5] == "recording-coverage":
                    coverage = self.facade.camera_recording_coverage(
                        camera_id=str(camera.get("id", ""))
                    )
                    return self._api_json(
                        start_response,
                        HTTPStatus.OK,
                        {"api_version": "v1", "coverage": coverage},
                        head=head,
                    )
                if parts[5] != "snapshot":
                    return self._api_error(start_response, HTTPStatus.NOT_FOUND, head=head)
                return self._api_snapshot(start_response, slot, head=head)
            if path == "/api/v1/recordings":
                values = parse_qs(query, keep_blank_values=False).get("limit", [])
                if values:
                    if len(values) != 1 or not values[0].isdigit():
                        return self._api_error(
                            start_response,
                            HTTPStatus.BAD_REQUEST,
                            message="Recording limit is invalid.",
                            head=head,
                        )
                    limit = int(values[0])
                else:
                    limit = 100
                if not 1 <= limit <= 1000:
                    return self._api_error(
                        start_response,
                        HTTPStatus.BAD_REQUEST,
                        message="Recording limit is invalid.",
                        head=head,
                    )
                return self._api_json(
                    start_response,
                    HTTPStatus.OK,
                    {
                        "api_version": "v1",
                        "recordings": self._api_recordings(limit),
                    },
                    head=head,
                )
        except Exception:
            return self._api_error(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                message="Recorder data is temporarily unavailable.",
                head=head,
            )
        return self._api_error(start_response, HTTPStatus.NOT_FOUND, head=head)

    def _api_camera_registration(
        self, start_response, environ: dict[str, object]
    ):
        """Accept a camera-authenticated local heartbeat without owner login."""

        content_type = str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            return self._api_error(
                start_response,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                message="Camera registration requires JSON.",
                head=False,
            )
        try:
            length = int(str(environ.get("CONTENT_LENGTH", "0")))
        except ValueError:
            length = 0
        stream = environ.get("wsgi.input")
        if not 1 <= length <= 4096 or not hasattr(stream, "read"):
            return self._api_error(
                start_response,
                HTTPStatus.BAD_REQUEST,
                message="Camera registration is invalid.",
                head=False,
            )
        signature = str(environ.get("HTTP_X_VMS_SIGNATURE", ""))
        source_address = str(environ.get("REMOTE_ADDR", ""))
        if re.fullmatch(r"[0-9a-f]{64}", signature) is None:
            return self._api_error(
                start_response,
                HTTPStatus.UNAUTHORIZED,
                message="Camera registration was not accepted.",
                head=False,
            )
        try:
            payload = stream.read(length)  # type: ignore[union-attr]
            if not isinstance(payload, bytes) or len(payload) != length:
                raise ValueError
            result = self.facade.register_camera_endpoint(
                payload=payload,
                signature=signature,
                source_address=source_address,
            )
        except ValueError:
            return self._api_error(
                start_response,
                HTTPStatus.BAD_REQUEST,
                message="Camera registration is invalid.",
                head=False,
            )
        except Exception:
            return self._api_error(
                start_response,
                HTTPStatus.UNAUTHORIZED,
                message="Camera registration was not accepted.",
                head=False,
            )
        return self._api_json(
            start_response,
            HTTPStatus.OK,
            {"api_version": "v1", "registration": result},
            head=False,
        )

    def _api_cameras(self) -> list[dict[str, object]]:
        return [
            self._api_camera_document(slot, camera)
            for slot, camera in enumerate(self.facade.list_cameras(), start=1)
        ]

    def _api_camera(self, slot: int) -> dict[str, object] | None:
        camera = self._camera_at_slot(slot)
        return None if camera is None else self._api_camera_document(slot, camera)

    def _camera_at_slot(self, slot: int) -> Mapping[str, object] | None:
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            return None
        return cameras[slot - 1]

    def _api_camera_document(
        self, slot: int, camera: Mapping[str, object]
    ) -> dict[str, object]:
        document: dict[str, object] = {
            "slot": slot,
            "name": str(camera.get("name") or f"Camera {slot}"),
            "state": str(camera.get("state", "pending")),
            "connected": (
                camera.get("connected")
                if isinstance(camera.get("connected"), bool)
                else None
            ),
            "recording_enabled": (
                camera.get("recording_enabled")
                if isinstance(camera.get("recording_enabled"), bool)
                else True
            ),
            "alerts_enabled": (
                camera.get("alerts_enabled")
                if isinstance(camera.get("alerts_enabled"), bool)
                else True
            ),
            "links": {
                "self": f"/api/v1/cameras/{slot}",
                "snapshot": f"/api/v1/cameras/{slot}/snapshot",
                "webrtc": f"/api/v1/cameras/{slot}/webrtc",
                "metrics": f"/api/v1/cameras/{slot}/metrics",
                "recording_coverage": f"/api/v1/cameras/{slot}/recording-coverage",
            },
        }
        setup = self._camera_setup_public_status(camera)
        if setup is not None:
            document["setup"] = setup
            links = document["links"]
            assert isinstance(links, dict)
            links["setup"] = f"/api/v1/cameras/{slot}/setup"
        return document

    def _camera_setup_public_status(
        self, camera: Mapping[str, object]
    ) -> dict[str, object] | None:
        camera_id = camera.get("id")
        if not isinstance(camera_id, str):
            return None
        status = self.facade.camera_setup_status(camera_id=camera_id)
        if status is None:
            return None
        state = status.get("state")
        transport = status.get("transport")
        issue = status.get("issue")
        updated_at = status.get("updated_at")
        if (
            state
            not in {
                "waiting-network",
                "waiting-heartbeat",
                "verifying-video",
                "ready",
                "needs-attention",
            }
            or not isinstance(transport, str)
            or not isinstance(updated_at, (int, float))
            or isinstance(updated_at, bool)
            or (
                issue is not None
                and issue
                not in {
                    "unsupported-device",
                    "network-timeout",
                    "identity-mismatch",
                    "heartbeat-timeout",
                    "video-verification-failed",
                    "route-commit-failed",
                }
            )
        ):
            raise ValueError("Camera setup status is invalid.")
        return {
            "state": state,
            "transport": transport,
            "issue": issue,
            "updated_at": updated_at,
        }

    def _api_snapshot(self, start_response, slot: int, *, head: bool):
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            return self._api_error(start_response, HTTPStatus.NOT_FOUND, head=head)
        camera = cameras[slot - 1]
        if camera.get("state") not in {"ready", "claimed"}:
            return self._api_error(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                message="Camera is unavailable.",
                head=head,
            )
        try:
            frame = self.facade.read_live_snapshot(camera_id=str(camera.get("id", "")))
        except Exception:
            return self._api_error(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                message="Live image is temporarily unavailable.",
                head=head,
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            frame,
            head=head,
            content_type="image/jpeg",
        )

    def _api_webrtc(self, start_response, path: str, environ: dict[str, object]):
        parts = path.split("/")
        if (
            len(parts) != 6
            or not parts[4].isdigit()
            or parts[5] != "webrtc"
        ):
            return self._api_error(
                start_response, HTTPStatus.NOT_FOUND, head=False
            )
        content_type = str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0]
        if content_type.strip().lower() != "application/sdp":
            return self._api_error(
                start_response,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                message="WebRTC signaling requires SDP.",
                head=False,
            )
        try:
            length = int(str(environ.get("CONTENT_LENGTH", "0")))
        except ValueError:
            length = 0
        stream = environ.get("wsgi.input")
        if not 1 <= length <= 64 * 1024 or not hasattr(stream, "read"):
            return self._api_error(
                start_response,
                HTTPStatus.BAD_REQUEST,
                message="WebRTC offer is invalid.",
                head=False,
            )
        try:
            offer_bytes = stream.read(length)  # type: ignore[union-attr]
            if not isinstance(offer_bytes, bytes) or len(offer_bytes) != length:
                raise ValueError
            offer = offer_bytes.decode("utf-8", errors="strict")
            cameras = tuple(self.facade.list_cameras())
            slot = int(parts[4])
            if not 1 <= slot <= len(cameras):
                return self._api_error(
                    start_response, HTTPStatus.NOT_FOUND, head=False
                )
            camera = cameras[slot - 1]
            if camera.get("state") not in {"ready", "claimed"}:
                raise ValueError
            answer = self.facade.negotiate_live_webrtc(
                camera_id=str(camera.get("id", "")),
                offer=offer,
            )
        except (UnicodeDecodeError, ValueError):
            return self._api_error(
                start_response,
                HTTPStatus.BAD_REQUEST,
                message="WebRTC offer is invalid.",
                head=False,
            )
        except Exception:
            return self._api_error(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                message="Live WebRTC video is temporarily unavailable.",
                head=False,
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            answer,
            head=False,
            content_type="application/sdp",
        )

    def _api_recordings(self, limit: int) -> list[dict[str, object]]:
        camera_slots = {
            str(camera.get("id", "")): slot
            for slot, camera in enumerate(self.facade.list_cameras(), start=1)
        }
        documents = []
        for recording in self.facade.list_recordings(limit=limit):
            recording_id = str(recording.get("id", ""))
            camera_id = str(recording.get("camera_id", ""))
            started_at = str(recording.get("started_at", ""))
            if not recording_id or not started_at or camera_id not in camera_slots:
                continue
            documents.append(
                {
                    "id": recording_id,
                    "camera_slot": camera_slots[camera_id],
                    "started_at": started_at,
                }
            )
        return documents

    def _api_json(
        self,
        start_response,
        status: HTTPStatus,
        document: Mapping[str, object],
        *,
        head: bool,
    ):
        return self._respond(
            start_response,
            status,
            json.dumps(document, separators=(",", ":")) + "\n",
            head=head,
            content_type="application/json; charset=utf-8",
        )

    def _api_error(
        self,
        start_response,
        status: HTTPStatus,
        *,
        message: str = "Resource not found.",
        head: bool,
    ):
        return self._api_json(
            start_response,
            status,
            {
                "api_version": "v1",
                "error": {
                    "code": status.name.lower(),
                    "message": message,
                },
            },
            head=head,
        )

    def _get(self, path: str, query: str) -> tuple[HTTPStatus, str]:
        legacy_settings_paths = {
            "/recorder": "/settings",
            "/cameras": "/settings#cameras",
            "/storage": "/settings#storage",
        }
        if path in legacy_settings_paths:
            return HTTPStatus.SEE_OTHER, legacy_settings_paths[path]
        if path == "/camera":
            slots = parse_qs(query, keep_blank_values=False).get("slot", [])
            target = (
                f"/#camera-{slots[0]}"
                if len(slots) == 1 and slots[0].isdigit()
                else "/"
            )
            return HTTPStatus.SEE_OTHER, target
        pages = {
            "/": self._home_page,
            "/settings": self._settings_page,
            "/diagnostics": lambda notice: self._system_log_page(query, notice),
            "/diagnostics/logs": lambda notice: self._raw_log_page(query, notice),
            "/live": self._live_page,
            "/events": lambda notice: self._recordings_page(query, notice),
            "/alerts": lambda notice: self._simple_page(
                "Alerts", "Choose alert preferences after setup.", notice
            ),
            "/update": lambda notice: self._simple_page(
                "Update", "Verified updates will appear here.", notice
            ),
        }
        if self.factory_enabled:
            pages["/provisioning"] = self._provisioning_page
        renderer = pages.get(path)
        if renderer is None:
            return HTTPStatus.NOT_FOUND, self._error_page("Page not found.")
        notice_values = parse_qs(query, keep_blank_values=False).get("notice", [])
        notice = notice_values[0] if len(notice_values) == 1 else None
        try:
            return HTTPStatus.OK, renderer(notice)
        except Exception:
            return HTTPStatus.INTERNAL_SERVER_ERROR, self._error_page(
                "This page is temporarily unavailable."
            )

    def _post(
        self, path: str, environ: dict[str, object]
    ) -> tuple[HTTPStatus, str, list[tuple[str, str]]]:
        try:
            form = self._read_form(environ)
        except ValueError:
            return (
                HTTPStatus.BAD_REQUEST,
                self._error_page("The submitted form could not be read."),
                [],
            )

        tokens = form.get("csrf_token", [])
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.csrf_token):
            return (
                HTTPStatus.FORBIDDEN,
                self._error_page("This form expired. Reload the page and try again."),
                [],
            )

        if path == "/actions/login":
            return self._login(form, environ)
        if path == "/actions/logout":
            self._revoke_session(environ)
            return (
                HTTPStatus.SEE_OTHER,
                "/login",
                [("Set-Cookie", self._expired_session_cookie(environ))],
            )

        actions = {
            "/actions/owner": self._create_owner,
            "/actions/owner-password": self._change_owner_password,
            "/actions/storage": self._initialize_storage,
            "/actions/camera": self._claim_camera,
            "/actions/camera-name": self._name_camera,
            "/actions/camera-name-slot": self._name_camera_by_slot,
            "/actions/camera-position": self._position_camera,
            "/actions/camera-recording-slot": self._set_camera_recording_by_slot,
            "/actions/alerts": self._set_alerts,
            "/actions/camera-alerts-slot": self._set_camera_alerts_by_slot,
            "/actions/camera-check-slot": self._verify_camera_by_slot,
            "/actions/camera-conflict-slot": self._resolve_camera_conflict_by_slot,
            "/actions/camera-setup-retry-slot": self._retry_camera_setup_by_slot,
            "/actions/recording": self._set_recording,
            "/actions/event-save": self._save_event,
            "/actions/event-delete": self._delete_event,
            "/actions/saved-clip-delete": self._delete_saved_clip,
            "/actions/remote-access": self._set_remote_access,
            "/actions/owner-gateway": self._set_owner_gateway,
            "/actions/push-subscribe": self._subscribe_push,
            "/actions/push-unsubscribe": self._unsubscribe_push,
            "/actions/push-test": self._test_push,
            "/actions/kit": self._create_kit,
            "/actions/camera-provision": self._provision_selected_camera,
            "/actions/flash": self._flash_next,
            "/actions/verify": self._verify_kit,
        }
        if not self.factory_enabled and path in {
            "/actions/kit",
            "/actions/camera-provision",
            "/actions/flash",
            "/actions/verify",
        }:
            return HTTPStatus.NOT_FOUND, self._error_page("Page not found."), []
        action = actions.get(path)
        if action is None:
            return HTTPStatus.NOT_FOUND, self._error_page("Page not found."), []
        try:
            location, notice = action(form)
        except _FormValidationError as error:
            return (
                HTTPStatus.BAD_REQUEST,
                self._error_page(str(error)),
                [("X-VMS-Error", str(error))],
            )
        except PushNotificationError as error:
            return (
                HTTPStatus.BAD_REQUEST,
                self._error_page(str(error)),
                [("X-VMS-Error", str(error))],
            )
        except (TypeError, ValueError):
            return (
                HTTPStatus.BAD_REQUEST,
                self._error_page("Check the entries and try again."),
                [],
            )
        except Exception:
            return (
                HTTPStatus.INTERNAL_SERVER_ERROR,
                self._error_page("That step could not be completed. Try again."),
                [],
            )
        target = self._notice_target(location, notice)
        response_headers = []
        if path == "/actions/owner":
            response_headers.append(
                ("Set-Cookie", self._session_cookie(environ, self._new_session()))
            )
        elif path == "/actions/owner-password":
            with self._authentication_lock:
                self._sessions.clear()
                self._login_failures.clear()
            response_headers.append(
                ("Set-Cookie", self._session_cookie(environ, self._new_session()))
            )
        return HTTPStatus.SEE_OTHER, target, response_headers

    @staticmethod
    def _notice_target(location: str, notice: str) -> str:
        base, separator, fragment = location.partition("#")
        joiner = "&" if "?" in base else "?"
        target = f"{base}{joiner}{urlencode({'notice': notice})}"
        return f"{target}#{fragment}" if separator else target

    def _login(
        self,
        form: Mapping[str, list[str]],
        environ: Mapping[str, object],
    ) -> tuple[HTTPStatus, str, list[tuple[str, str]]]:
        if not self._authentication_required():
            return HTTPStatus.SEE_OTHER, "/", []
        if self._login_rate_limited():
            return (
                HTTPStatus.TOO_MANY_REQUESTS,
                self._login_page("Too many attempts. Wait one minute and try again."),
                [("Retry-After", str(_LOGIN_FAILURE_WINDOW_SECONDS))],
            )
        try:
            password = self._secret(form, "password")
        except ValueError:
            self._record_login_failure()
            return (
                HTTPStatus.UNAUTHORIZED,
                self._login_page("Password is incorrect."),
                [],
            )
        try:
            authenticated = self.facade.authenticate_owner(password=password)
        except Exception:
            authenticated = False
        if not authenticated:
            self._record_login_failure()
            return (
                HTTPStatus.UNAUTHORIZED,
                self._login_page("Password is incorrect."),
                [],
            )
        with self._authentication_lock:
            self._login_failures.clear()
        token = self._new_session()
        return (
            HTTPStatus.SEE_OTHER,
            "/",
            [("Set-Cookie", self._session_cookie(environ, token))],
        )

    def _login_rate_limited(self) -> bool:
        now = self._time_source()
        with self._authentication_lock:
            self._login_failures = [
                attempted
                for attempted in self._login_failures
                if attempted > now - _LOGIN_FAILURE_WINDOW_SECONDS
            ]
            return len(self._login_failures) >= _LOGIN_FAILURE_LIMIT

    def _backup_download(self, start_response, environ: dict[str, object]):
        try:
            form = self._read_form(environ)
            tokens = form.get("csrf_token", [])
            if len(tokens) != 1 or not hmac.compare_digest(
                tokens[0], self.csrf_token
            ):
                return self._respond(
                    start_response,
                    HTTPStatus.FORBIDDEN,
                    self._error_page(
                        "This form expired. Reload the page and try again."
                    ),
                    head=False,
                )
            password = self._secret(form, "password")
            if self._login_rate_limited():
                return self._respond(
                    start_response,
                    HTTPStatus.TOO_MANY_REQUESTS,
                    self._error_page(
                        "Too many attempts. Wait one minute and try again."
                    ),
                    head=False,
                    extra_headers=(
                        ("Retry-After", str(_LOGIN_FAILURE_WINDOW_SECONDS)),
                    ),
                )
            if not self.facade.authenticate_owner(password=password):
                self._record_login_failure()
                return self._respond(
                    start_response,
                    HTTPStatus.UNAUTHORIZED,
                    self._error_page("Password is incorrect."),
                    head=False,
                )
            with self._authentication_lock:
                self._login_failures.clear()
            content, filename, _ = self.facade.create_backup_download(
                password=password
            )
        except ValueError:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Check the entries and try again."),
                head=False,
            )
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                self._error_page("The backup could not be created. Try again."),
                head=False,
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            content,
            head=False,
            content_type="application/octet-stream",
            download_name=filename,
        )

    def _record_login_failure(self) -> None:
        with self._authentication_lock:
            self._login_failures.append(self._time_source())

    @staticmethod
    def _secure_request(environ: Mapping[str, object]) -> bool:
        forwarded = str(environ.get("HTTP_X_FORWARDED_PROTO", ""))
        return (
            str(environ.get("wsgi.url_scheme", "")).lower() == "https"
            or forwarded.split(",", 1)[0].strip().lower() == "https"
        )

    def _session_cookie(self, environ: Mapping[str, object], token: str) -> str:
        secure = "; Secure" if self._secure_request(environ) else ""
        return (
            f"{_SESSION_COOKIE}={token}; Path=/; Max-Age={_SESSION_TTL_SECONDS}; "
            f"HttpOnly; SameSite=Strict{secure}"
        )

    def _expired_session_cookie(self, environ: Mapping[str, object]) -> str:
        secure = "; Secure" if self._secure_request(environ) else ""
        return (
            f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; "
            f"HttpOnly; SameSite=Strict{secure}"
        )

    @staticmethod
    def _read_form(environ: dict[str, object]) -> dict[str, list[str]]:
        content_type = str(environ.get("CONTENT_TYPE", ""))
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise ValueError("unsupported form encoding")
        try:
            length = int(str(environ.get("CONTENT_LENGTH", "0")))
        except ValueError as exc:
            raise ValueError("invalid body length") from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ValueError("invalid body length")
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            raise ValueError("missing request body")
        raw = stream.read(length)
        try:
            body = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid form text") from exc
        return parse_qs(body, keep_blank_values=True, strict_parsing=True)

    @staticmethod
    def _one(form: Mapping[str, list[str]], name: str, *, limit: int = 256) -> str:
        values = form.get(name, [])
        if len(values) != 1:
            raise ValueError("missing field")
        value = values[0].strip()
        if not value or len(value) > limit:
            raise ValueError("invalid field")
        return value

    @staticmethod
    def _secret(
        form: Mapping[str, list[str]], name: str, *, limit: int = 1024
    ) -> str:
        values = form.get(name, [])
        if len(values) != 1 or not values[0] or len(values[0]) > limit:
            raise ValueError("invalid secret field")
        return values[0]

    def _create_owner(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        name = self._one(form, "name", limit=100)
        password = self._secret(form, "password")
        password_confirm = self._secret(form, "password_confirm")
        if len(password) < _MIN_OWNER_PASSWORD_LENGTH:
            raise _FormValidationError(
                f"Password must contain at least {_MIN_OWNER_PASSWORD_LENGTH} characters."
            )
        if not hmac.compare_digest(
            password.encode("utf-8"), password_confirm.encode("utf-8")
        ):
            raise _FormValidationError("Passwords do not match.")
        self.facade.create_owner(name=name, password=password)
        return "/", "owner-created"

    def _change_owner_password(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        current_password = self._secret(form, "current_password")
        new_password = self._secret(form, "new_password")
        new_password_confirm = self._secret(form, "new_password_confirm")
        if len(new_password) < _MIN_OWNER_PASSWORD_LENGTH:
            raise _FormValidationError(
                f"New password must contain at least {_MIN_OWNER_PASSWORD_LENGTH} characters."
            )
        if not hmac.compare_digest(
            new_password.encode("utf-8"), new_password_confirm.encode("utf-8")
        ):
            raise _FormValidationError("New passwords do not match.")
        if not self.facade.authenticate_owner(password=current_password):
            raise _FormValidationError("Current password is incorrect.")
        self.facade.change_owner_password(
            current_password=current_password, new_password=new_password
        )
        return "/settings", "owner-password-changed"

    def _initialize_storage(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        stable_id = self._one(form, "storage_id", limit=128)
        targets = [
            target
            for target in self.facade.list_recording_storage_targets()
            if str(getattr(target, "stable_id", "")) == stable_id
        ]
        if len(targets) != 1:
            raise ValueError("Recording drive is unavailable")
        confirmation = str(getattr(targets[0], "confirmation", ""))
        if not confirmation:
            raise ValueError("Recording drive is unavailable")
        self.facade.initialize_selected_recording_storage(
            stable_id=stable_id,
            confirmation=confirmation,
        )
        return "/", "storage-ready"

    def _claim_camera(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        name = self._one(form, "name", limit=100)
        has_payload = bool(form.get("claim_payload", [""])[0].strip())
        has_code = bool(form.get("camera_code", [""])[0].strip())
        if not has_payload and not has_code and form.get("camera_id"):
            self.facade.claim_camera(
                camera_id=self._one(form, "camera_id", limit=128),
                claim_code=self._secret(form, "claim_code"),
                name=name,
            )
            return "/", "camera-claimed"
        if has_payload == has_code:
            raise _FormValidationError("Scan the QR code or enter the camera code.")
        if has_payload:
            claim_payload = self._one(form, "claim_payload", limit=2048)
            self.facade.claim_camera_payload(
                claim_payload=claim_payload,
                name=name,
            )
            return "/", "camera-claimed"
        self.facade.claim_camera_code(
            camera_code=self._secret(form, "camera_code", limit=256),
            name=name,
        )
        return "/", "camera-claimed"

    def _name_camera(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        camera_id = self._one(form, "camera_id", limit=128)
        name = self._one(form, "name", limit=100)
        self.facade.name_camera(camera_id=camera_id, name=name)
        return "/", "camera-named"

    def _name_camera_by_slot(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        slot = int(self._one(form, "slot", limit=3))
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            raise ValueError("invalid camera slot")
        camera_id = str(cameras[slot - 1].get("id", ""))
        if not camera_id:
            raise ValueError("invalid camera")
        self.facade.name_camera(
            camera_id=camera_id,
            name=self._one(form, "name", limit=100),
        )
        return "/", "camera-named"

    def _position_camera(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        slot = int(self._one(form, "slot", limit=3))
        position = int(self._one(form, "position", limit=3))
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras) or not 1 <= position <= len(cameras):
            raise ValueError("invalid camera position")
        camera_id = str(cameras[slot - 1].get("id", ""))
        if not camera_id:
            raise ValueError("invalid camera")
        self.facade.position_camera(camera_id=camera_id, position=position)
        return "/", "camera-moved"

    def _set_camera_recording_by_slot(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        slot = int(self._one(form, "slot", limit=3))
        enabled_value = self._one(form, "enabled", limit=5)
        if enabled_value not in {"true", "false"}:
            raise ValueError("invalid camera recording setting")
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            raise ValueError("invalid camera slot")
        camera_id = str(cameras[slot - 1].get("id", ""))
        if not camera_id:
            raise ValueError("invalid camera")
        self.facade.set_camera_recording(
            camera_id=camera_id,
            enabled=enabled_value == "true",
        )
        return f"/#camera-{slot}", "camera-recording-saved"

    def _set_alerts(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        enabled_value = self._one(form, "enabled", limit=5)
        if enabled_value not in {"true", "false"}:
            raise ValueError("invalid alert setting")
        self.facade.set_alerts_enabled(enabled=enabled_value == "true")
        return "/#alerts", "alerts-saved"

    def _set_camera_alerts_by_slot(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        slot = int(self._one(form, "slot", limit=3))
        enabled_value = self._one(form, "enabled", limit=5)
        if enabled_value not in {"true", "false"}:
            raise ValueError("invalid camera alert setting")
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            raise ValueError("invalid camera slot")
        camera_id = str(cameras[slot - 1].get("id", ""))
        if not camera_id:
            raise ValueError("invalid camera")
        self.facade.set_camera_alerts(
            camera_id=camera_id,
            enabled=enabled_value == "true",
        )
        return f"/#camera-{slot}", "camera-alerts-saved"

    def _verify_camera_by_slot(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        slot = int(self._one(form, "slot", limit=3))
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            raise ValueError("invalid camera slot")
        camera_id = str(cameras[slot - 1].get("id", ""))
        if not camera_id:
            raise ValueError("invalid camera")
        self.facade.verify_recorder_camera(camera_id=camera_id)
        return f"/#camera-{slot}-diagnostics", "camera-checked"

    def _retry_camera_setup_by_slot(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        slot = int(self._one(form, "slot", limit=3))
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            raise ValueError("invalid camera slot")
        camera_id = str(cameras[slot - 1].get("id", ""))
        if not camera_id:
            raise ValueError("invalid camera")
        self.facade.retry_camera_network_setup(camera_id=camera_id)
        return "/settings#cameras", "camera-setup-retried"

    def _resolve_camera_conflict_by_slot(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        slot = int(self._one(form, "slot", limit=3))
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            raise ValueError("invalid camera slot")
        camera_id = str(cameras[slot - 1].get("id", ""))
        if not camera_id:
            raise ValueError("invalid camera")
        self.facade.resolve_camera_identity_conflict(camera_id=camera_id)
        return f"/#camera-{slot}-diagnostics", "camera-conflict-resolved"

    def _set_recording(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        mode = self._one(form, "mode", limit=16)
        if mode != "continuous":
            raise ValueError("invalid recording mode")
        days = int(self._one(form, "days", limit=5))
        if days not in {1, 3, 7, 14, 30}:
            raise ValueError("invalid retention")
        self.facade.set_recording(mode=mode, days=days)
        return "/settings", "recording-saved"

    def _save_event(self, form: Mapping[str, list[str]]) -> tuple[str, str]:
        event_id = self._one(form, "event", limit=200)
        self.facade.save_video_event(event_id=event_id)
        return "/events", "event-saving"

    def _delete_event(self, form: Mapping[str, list[str]]) -> tuple[str, str]:
        event_id = self._one(form, "event", limit=200)
        self.facade.delete_video_event(event_id=event_id)
        return "/events", "event-deleted"

    def _delete_saved_clip(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        clip_id = self._one(form, "clip", limit=200)
        self.facade.delete_saved_video_clip(clip_id=clip_id)
        return "/events", "saved-clip-deleted"

    def _set_remote_access(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        values = form.get("enabled", [])
        if not values or any(value not in {"0", "1"} for value in values):
            raise ValueError("invalid remote access setting")
        enabled = "1" in values
        if not enabled:
            self.facade.disable_private_remote_access()
            with self._remote_access_lock:
                self._remote_approval_url = None
            return "/settings#remote-access", "remote-disabled"

        result = self.facade.begin_private_remote_access()
        state = result.get("state")
        if state == "ready":
            with self._remote_access_lock:
                self._remote_approval_url = None
            return "/settings#remote-access", "remote-ready"
        approval_url = result.get("approval_url")
        if state != "approval-required" or not self._tailscale_approval_url(
            approval_url
        ):
            raise ValueError("invalid remote access result")
        with self._remote_access_lock:
            self._remote_approval_url = str(approval_url)
        return "/settings#remote-access", "remote-approval"

    def _set_owner_gateway(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        domain = self._one(form, "domain", limit=253)
        enrollment_code = self._secret(form, "enrollment_code", limit=128)
        result = self.facade.setup_owner_gateway_access(
            domain=domain,
            enrollment_code=enrollment_code,
        )
        remote_url = result.get("remote_url")
        if (
            result.get("mode") != "owner-gateway"
            or result.get("state") != "ready"
            or not isinstance(remote_url, str)
            or not remote_url.startswith("https://")
        ):
            raise ValueError("invalid owner gateway result")
        with self._remote_access_lock:
            self._remote_approval_url = None
        return "/settings#remote-access", "remote-gateway-ready"

    def _push_owner_ready(self) -> None:
        if self.factory_enabled or not self._authentication_required():
            raise _FormValidationError("Create an owner before enabling notifications.")

    def _subscribe_push(self, form: Mapping[str, list[str]]) -> tuple[str, str]:
        self._push_owner_ready()
        subscription = json.loads(self._one(form, "subscription", limit=4096))
        if not isinstance(subscription, Mapping):
            raise _FormValidationError("The phone's notification details are invalid.")
        self.facade.subscribe_push(subscription)
        return "/settings#notifications", "push-subscribed"

    def _unsubscribe_push(self, form: Mapping[str, list[str]]) -> tuple[str, str]:
        self._push_owner_ready()
        self.facade.unsubscribe_push(self._one(form, "endpoint", limit=2048))
        return "/settings#notifications", "push-unsubscribed"

    def _test_push(self, form: Mapping[str, list[str]]) -> tuple[str, str]:
        self._push_owner_ready()
        self.facade.send_test_push(self._one(form, "endpoint", limit=2048))
        return "/settings#notifications", "push-test-sent"

    @staticmethod
    def _tailscale_approval_url(value: object) -> bool:
        if not isinstance(value, str) or len(value) > 512:
            return False
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "login.tailscale.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and parsed.path.startswith("/a/")
            and not parsed.query
            and not parsed.fragment
        )

    def _remote_access_qr(self, start_response, query: str, *, head: bool):
        kinds = parse_qs(query, keep_blank_values=False).get("kind", [])
        if len(kinds) != 1 or kinds[0] not in {"open", "approval"}:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Remote access code is invalid."),
                head=head,
            )
        if kinds[0] == "approval":
            with self._remote_access_lock:
                value = self._remote_approval_url
            valid = self._tailscale_approval_url(value)
        else:
            access = self.facade.status().get("access", {})
            value = access.get("remote_url") if isinstance(access, Mapping) else None
            valid = isinstance(value, str) and value.startswith("https://")
        if not valid:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Remote access is not ready."),
                head=head,
            )
        output = io.BytesIO()
        segno.make_qr(str(value)).save(output, kind="svg", scale=5, border=2)
        return self._respond(
            start_response,
            HTTPStatus.OK,
            output.getvalue(),
            head=head,
            content_type="image/svg+xml",
        )

    def _recording_media(self, start_response, query: str, *, head: bool):
        values = parse_qs(query, keep_blank_values=False)
        references = values.get("reference", [])
        if len(references) != 1:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Recording reference is invalid."),
                head=head,
            )
        download_values = values.get("download", [])
        if download_values and download_values != ["1"]:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Recording download option is invalid."),
                head=head,
            )
        try:
            media, media_type = self.facade.read_recording_media(
                reference=references[0]
            )
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Recording is unavailable."),
                head=head,
            )
        download_name = None
        if download_values:
            segment = references[0].rsplit("/", 1)[-1]
            safe_segment = "".join(
                character
                for character in segment
                if character.isascii()
                and (character.isalnum() or character in {"-", "_"})
            )[:96]
            download_name = f"vms-recording-{safe_segment or 'event'}.mjpeg"
        return self._respond(
            start_response,
            HTTPStatus.OK,
            media,
            head=head,
            content_type=media_type,
            download_name=download_name,
        )

    def _recording_preview(self, start_response, query: str, *, head: bool):
        references = parse_qs(query, keep_blank_values=False).get("reference", [])
        if len(references) != 1:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Recording reference is invalid."),
                head=head,
            )
        try:
            frame = self.facade.read_recording_preview(reference=references[0])
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Recording preview is unavailable."),
                head=head,
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            frame,
            head=head,
            content_type="image/jpeg",
        )

    def _video_event_preview(
        self,
        start_response,
        query: str,
        *,
        head: bool,
        range_header: object = None,
    ):
        parameters = parse_qs(query, keep_blank_values=False)
        events = parameters.get("event", [])
        if len(events) != 1 or not 1 <= len(events[0]) <= 200:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Video event is invalid."),
                head=head,
            )
        try:
            preview = self.facade.read_video_event_preview(event_id=events[0])
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Video event is unavailable."),
                head=head,
            )
        download_name = None
        if parameters.get("download") == ["1"]:
            safe_segment = "".join(
                character
                for character in events[0]
                if character.isascii()
                and (character.isalnum() or character in {"-", "_"})
            )[-80:]
            download_name = f"ose-camera-event-{safe_segment or 'clip'}.mp4"
        return self._mp4_response(
            start_response,
            preview,
            head=head,
            range_header=range_header,
            download_name=download_name,
        )

    def _video_saved_clip(
        self,
        start_response,
        query: str,
        *,
        head: bool,
        range_header: object = None,
    ):
        parameters = parse_qs(query, keep_blank_values=False)
        clips = parameters.get("clip", [])
        if len(clips) != 1 or not 1 <= len(clips[0]) <= 200:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Saved clip is invalid."),
                head=head,
            )
        try:
            video = self.facade.read_saved_video_clip(clip_id=clips[0])
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Saved clip is unavailable."),
                head=head,
            )
        download_name = None
        if parameters.get("download") == ["1"]:
            safe_segment = "".join(
                character
                for character in clips[0]
                if character.isascii()
                and (character.isalnum() or character in {"-", "_"})
            )[-80:]
            download_name = f"ose-saved-clip-{safe_segment or 'video'}.mp4"
        return self._mp4_response(
            start_response,
            video,
            head=head,
            range_header=range_header,
            download_name=download_name,
        )

    def _mp4_response(
        self,
        start_response,
        video: bytes,
        *,
        head: bool,
        range_header: object,
        download_name: str | None,
    ):
        range_headers = (("Accept-Ranges", "bytes"),)
        if range_header is None or range_header == "":
            return self._respond(
                start_response,
                HTTPStatus.OK,
                video,
                head=head,
                content_type="video/mp4",
                download_name=download_name,
                extra_headers=range_headers,
            )
        requested = self._byte_range(range_header, len(video))
        if requested is None:
            return self._respond(
                start_response,
                HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                b"",
                head=head,
                content_type="video/mp4",
                download_name=download_name,
                extra_headers=(
                    *range_headers,
                    ("Content-Range", f"bytes */{len(video)}"),
                ),
            )
        first, last = requested
        return self._respond(
            start_response,
            HTTPStatus.PARTIAL_CONTENT,
            video[first : last + 1],
            head=head,
            content_type="video/mp4",
            download_name=download_name,
            extra_headers=(
                *range_headers,
                ("Content-Range", f"bytes {first}-{last}/{len(video)}"),
            ),
        )

    def _video_event_thumbnail(self, start_response, query: str, *, head: bool):
        events = parse_qs(query, keep_blank_values=False).get("event", [])
        if len(events) != 1 or not 1 <= len(events[0]) <= 200:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Video event is invalid."),
                head=head,
            )
        try:
            thumbnail = self.facade.read_video_event_thumbnail(event_id=events[0])
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Video event thumbnail is unavailable."),
                head=head,
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            thumbnail,
            head=head,
            content_type="image/jpeg",
        )

    def _video_event_vod(self, start_response, query: str, *, head: bool):
        values = parse_qs(query, keep_blank_values=False)
        events = values.get("event", [])
        resources = values.get("resource", [])
        if (
            len(events) != 1
            or not 1 <= len(events[0]) <= 200
            or len(resources) != 1
            or not self._valid_hls_resource(resources[0])
        ):
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                self._error_page("Video event resource is invalid."),
                head=head,
            )
        try:
            body, content_type = self.facade.read_video_event_vod(
                event_id=events[0], resource=resources[0]
            )
            if content_type == "application/vnd.apple.mpegurl":
                body = self._rewrite_hls_manifest(body, event_id=events[0])
        except (TypeError, UnicodeError, ValueError):
            return self._respond(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                self._error_page("Video event playback is temporarily unavailable."),
                head=head,
            )
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Video event is unavailable."),
                head=head,
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            body,
            head=head,
            content_type=content_type,
        )

    @staticmethod
    def _valid_hls_resource(resource: object) -> bool:
        return (
            isinstance(resource, str)
            and ".." not in resource
            and _HLS_RESOURCE.fullmatch(resource) is not None
        )

    @classmethod
    def _rewrite_hls_manifest(cls, body: bytes, *, event_id: str) -> bytes:
        text = body.decode("utf-8", errors="strict")

        def route(resource: str) -> str:
            if not cls._valid_hls_resource(resource):
                raise ValueError("Frigate returned an unsafe HLS resource.")
            return "/event-vod?" + urlencode(
                {"event": event_id, "resource": resource}
            )

        rewritten: list[str] = []
        for line in text.splitlines():
            if not line:
                rewritten.append(line)
                continue
            if not line.startswith("#"):
                rewritten.append(route(line))
                continue

            def replace_uri(match: re.Match[str]) -> str:
                return f'URI="{route(match.group(1))}"'

            rewritten.append(_HLS_URI_ATTRIBUTE.sub(replace_uri, line))
        return ("\n".join(rewritten) + "\n").encode("utf-8")

    @staticmethod
    def _byte_range(value: object, total: int) -> tuple[int, int] | None:
        if (
            not isinstance(value, str)
            or not 0 < len(value) <= 128
            or total <= 0
            or not value.startswith("bytes=")
        ):
            return None
        bounds = value[6:]
        if "," in bounds or bounds.count("-") != 1:
            return None
        first_text, last_text = bounds.split("-", 1)
        if first_text:
            if not first_text.isdigit() or (last_text and not last_text.isdigit()):
                return None
            first = int(first_text)
            last = total - 1 if not last_text else min(int(last_text), total - 1)
            if first >= total or last < first:
                return None
            return first, last
        if not last_text.isdigit():
            return None
        suffix_length = int(last_text)
        if suffix_length <= 0:
            return None
        return max(total - suffix_length, 0), total - 1

    def _live_snapshot(self, start_response, query: str, *, head: bool):
        camera = self._camera_for_query(query)
        if camera is None or camera.get("state") not in {"ready", "claimed"}:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._error_page("Camera is unavailable."),
                head=head,
            )
        try:
            frame = self.facade.read_live_snapshot(camera_id=str(camera.get("id", "")))
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                self._error_page("Live image is temporarily unavailable."),
                head=head,
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            frame,
            head=head,
            content_type="image/jpeg",
        )

    def _camera_for_query(self, query: str) -> Mapping[str, object] | None:
        slots = parse_qs(query, keep_blank_values=False).get("slot", [])
        if len(slots) != 1 or not slots[0].isdigit():
            return None
        slot = int(slots[0])
        cameras = tuple(self.facade.list_cameras())
        if not 1 <= slot <= len(cameras):
            return None
        return cameras[slot - 1]

    def _create_kit(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        count = int(self._one(form, "camera_count", limit=2))
        if not 1 <= count <= SOFTWARE_CAMERA_LIMIT:
            raise ValueError("invalid camera count")
        model = self._one(form, "camera_model", limit=32)
        if model not in _CAMERA_MODELS:
            raise ValueError("invalid camera model")
        country = validate_wifi_country(
            self._one(form, "wifi_country", limit=2)
        )
        request = build_provisioning_config(
            mode="kit",
            camera_count=count,
            camera_model=model,
            country=country,
        )
        self.facade.prepare_provisioning(request)
        return "/provisioning", "kit-created"

    def _provision_selected_camera(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        number = int(self._one(form, "single_camera_number", limit=2))
        if not 1 <= number <= SOFTWARE_CAMERA_LIMIT:
            raise ValueError("invalid camera number")
        model = self._one(form, "single_camera_model", limit=32)
        if model not in _CAMERA_MODELS:
            raise ValueError("invalid camera model")
        country = validate_wifi_country(
            self._one(form, "single_wifi_country", limit=2)
        )
        passphrase = self._secret(form, "single_wifi_passphrase")
        confirmation_passphrase = self._secret(
            form, "single_wifi_passphrase_confirm"
        )
        if not hmac.compare_digest(
            passphrase.encode("utf-8"), confirmation_passphrase.encode("utf-8")
        ):
            raise ValueError("Wi-Fi passphrases do not match")
        request = build_provisioning_config(
            mode="camera",
            camera_number=number,
            camera_model=model,
            ssid=self._one(form, "single_wifi_ssid", limit=32),
            country=country,
        )
        self.facade.provision_selected_camera(
            request,
            wifi_passphrase=passphrase,
            stable_id=self._one(form, "single_stable_id", limit=128),
            confirmation=self._one(form, "single_confirmation", limit=160),
        )
        return "/provisioning", "camera-provisioned"

    def _flash_next(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        stable_id = self._one(form, "stable_id", limit=128)
        confirmation = self._one(form, "confirmation", limit=160)
        self.facade.flash_selected(
            stable_id=stable_id,
            confirmation=confirmation,
        )
        return "/provisioning", "flash-complete"

    def _verify_kit(
        self, form: Mapping[str, list[str]]
    ) -> tuple[str, str]:
        self.facade.verify_kit()
        return "/provisioning", "kit-verified"

    def _csrf(self) -> str:
        return (
            '<input type="hidden" name="csrf_token" '
            f'value="{_text(self.csrf_token)}">'
        )

    def _form(self, action: str, body: str, button: str) -> str:
        return (
            f'<form method="post" action="{_text(action)}">'
            f"{self._csrf()}{body}"
            f'<button type="submit">{_text(button)}</button></form>'
        )

    @staticmethod
    def _settings_row(
        title: str,
        value: str,
        body: str,
        *,
        section_id: str | None = None,
        attributes: str = "",
    ) -> str:
        identifier = "" if section_id is None else f' id="{_text(section_id)}"'
        extra = f" {attributes.strip()}" if attributes.strip() else ""
        return (
            f'<details class="settings-row"{identifier}{extra}>'
            '<summary><span class="settings-row-title">'
            f'{_text(title)}</span><span class="settings-row-value">{_text(value)}</span>'
            '<span class="settings-row-chevron" aria-hidden="true">&#x203A;</span>'
            '</summary><div class="settings-row-body">'
            f"{body}</div></details>"
        )

    def _camera_boolean_control(
        self,
        *,
        slot: int,
        action: str,
        label: str,
        enabled: bool,
        paused: bool = False,
    ) -> str:
        state = "Active" if enabled else "Paused"
        displayed_state = f"{state} (globally paused)" if enabled and paused else state
        next_enabled = not enabled
        action_label = f'{"Pause" if enabled else "Resume"} {label.lower()}'
        return (
            '<details class="camera-control">'
            f'<summary class="camera-action setting-toggle '
            f'{"active" if enabled and not paused else "inactive"}" '
            f'title="Change {_text(label.lower())}">'
            '<span aria-hidden="true">&#x25CF;</span>'
            f'<span>{_text(label)}: {_text(displayed_state)}</span></summary>'
            f'<form class="camera-control-form" method="post" action="{_text(action)}">'
            f'{self._csrf()}<input type="hidden" name="slot" value="{slot}">'
            f'<input type="hidden" name="enabled" value="{str(next_enabled).lower()}">'
            f'<button type="submit">{_text(action_label)}</button></form></details>'
        )

    def _global_alert_control(self, *, enabled: bool) -> str:
        next_enabled = not enabled
        action_label = "Pause alerts" if enabled else "Resume alerts"
        return (
            '<details class="global-alert-control" id="alerts">'
            f'<summary>Alerts: {"Active" if enabled else "Paused"}</summary>'
            '<form method="post" action="/actions/alerts">'
            f'{self._csrf()}<input type="hidden" name="enabled" '
            f'value="{str(next_enabled).lower()}">'
            f'<button type="submit">{_text(action_label)}</button></form></details>'
        )

    def _camera_position_form(
        self,
        *,
        camera_slot: int,
        position: int,
        icon: str,
        label: str,
        disabled: bool,
    ) -> str:
        disabled_attribute = " disabled" if disabled else ""
        return (
            '<form class="camera-action-form" method="post" '
            'action="/actions/camera-position">'
            f"{self._csrf()}"
            f'<input type="hidden" name="slot" value="{camera_slot}">'
            f'<input type="hidden" name="position" value="{position}">'
            f'<button class="camera-action camera-position" type="submit" '
            f'title="{_text(label)}" aria-label="{_text(label)}"'
            f'{disabled_attribute}><span aria-hidden="true">{_text(icon)}</span>'
            "</button></form>"
        )

    def _camera_rename_form(self, *, camera_slot: int, name: str) -> str:
        field_id = f"camera-{camera_slot}-name"
        return (
            '<form class="camera-rename" method="post" '
            'action="/actions/camera-name-slot" data-camera-rename-form hidden>'
            f'{self._csrf()}<input type="hidden" name="slot" value="{camera_slot}">'
            f'<label class="visually-hidden" for="{field_id}">Camera name</label>'
            f'<input id="{field_id}" name="name" type="text" maxlength="100" '
            f'autocomplete="off" value="{_text(name)}" required>'
            '<button type="submit">Save</button>'
            '<button class="secondary" type="button" data-cancel-rename>Cancel</button>'
            "</form>"
        )

    def _owner_form(self) -> str:
        return self._form(
            "/actions/owner",
            _field("name", "Owner name", autocomplete="name")
            + _field(
                "password",
                "Password",
                input_type="password",
                autocomplete="new-password",
                minimum_length=_MIN_OWNER_PASSWORD_LENGTH,
            )
            + _field(
                "password_confirm",
                "Confirm password",
                input_type="password",
                autocomplete="new-password",
                minimum_length=_MIN_OWNER_PASSWORD_LENGTH,
            ),
            "Create Owner",
        )

    def _owner_password_form(self) -> str:
        return self._form(
            "/actions/owner-password",
            _field(
                "current_password",
                "Current password",
                input_type="password",
                autocomplete="current-password",
            )
            + _field(
                "new_password",
                "New password",
                input_type="password",
                autocomplete="new-password",
                minimum_length=_MIN_OWNER_PASSWORD_LENGTH,
            )
            + _field(
                "new_password_confirm",
                "Confirm new password",
                input_type="password",
                autocomplete="new-password",
                minimum_length=_MIN_OWNER_PASSWORD_LENGTH,
            ),
            "Change Password",
        )

    def _storage_form(self) -> str:
        try:
            targets = tuple(self.facade.list_recording_storage_targets())
        except Exception:
            targets = ()
        options = []
        for target in targets:
            stable_id = str(getattr(target, "stable_id", ""))
            display_name = str(getattr(target, "display_name", "Recording Drive"))
            confirmation = str(getattr(target, "confirmation", ""))
            if not stable_id or not confirmation:
                continue
            options.append(
                f'<option value="{_text(stable_id)}">{_text(display_name)}</option>'
            )
        if not options:
            return '<p class="empty">No prepared recording drive found.</p>'
        body = (
            '<label for="storage_id">Recording drive</label>'
            f'<select id="storage_id" name="storage_id" required>{"".join(options)}</select>'
        )
        return self._form("/actions/storage", body, "Use Recording Drive")

    def _home_page(self, notice: str | None) -> str:
        status = self.facade.status()
        setup = status.get("setup")
        if isinstance(setup, Mapping) and setup.get("ready") is not True:
            return self._setup_page(status, notice)
        return self._live_page(notice)

    def _setup_page(
        self, status: Mapping[str, object], notice: str | None
    ) -> str:
        setup = status.get("setup")
        next_step = setup.get("next") if isinstance(setup, Mapping) else "owner"
        if next_step == "owner":
            step = "1 of 3"
            title = "Create Owner"
            body = self._owner_form()
        elif next_step == "storage":
            step = "2 of 3"
            title = "Recording Storage"
            body = self._storage_form()
        elif next_step == "cameras":
            step = "3 of 3"
            title = "Name Cameras"
            forms = []
            for camera in self.facade.list_cameras():
                if camera.get("state") != "ready":
                    continue
                camera_id = str(camera.get("id", ""))
                forms.append(
                    "<div>"
                    f"<h2>{_text(camera.get('name') or 'Camera')}</h2>"
                    + self._form(
                        "/actions/camera-name",
                        '<input type="hidden" name="camera_id" '
                        f'value="{_text(camera_id)}">'
                        + _field("name", "Location name"),
                        "Save Name",
                    )
                    + "</div>"
                )
            body = (
                "".join(forms)
                if forms
                else '<p class="empty">Waiting for included cameras.</p>'
            )
        else:
            return self._settings_page(notice)
        content = (
            f'<section class="hero"><p class="eyebrow">Step {_text(step)}</p>'
            f"<h1>{_text(title)}</h1></section>"
            f'<section class="panel narrow">{body}</section>'
        )
        return self._layout("Setup", content, notice)

    def _settings_page(self, notice: str | None) -> str:
        status = self.facade.status()
        cameras = status.get("cameras", {})
        if not isinstance(cameras, Mapping):
            cameras = {}
        connected = cameras.get("connected", 0)
        expected = cameras.get("expected", 0)
        owner_ready = bool(status.get("owner_ready", False))
        recording = status.get("recording", {})
        if not isinstance(recording, Mapping):
            recording = {}
        current_days = str(recording.get("days", 7))
        access = status.get("access", {})
        if not isinstance(access, Mapping):
            access = {}
        local_url = str(
            access.get("local_url") or "http://osecameras.local"
        )
        remote_url_value = access.get("remote_url")
        remote_url = (
            str(remote_url_value)
            if isinstance(remote_url_value, str)
            and remote_url_value.startswith("https://")
            else None
        )
        with self._remote_access_lock:
            approval_url = self._remote_approval_url
        approval_pending = self._tailscale_approval_url(approval_url)
        remote_ready = access.get("state") == "ready" and remote_url is not None
        owner_form = (
            self._owner_password_form() if owner_ready else self._owner_form()
        )
        recording_form = self._form(
            "/actions/recording",
            '<input type="hidden" name="mode" value="continuous">'
            + '<label for="days">Keep recordings for</label>'
            + '<select id="days" name="days">'
            + "".join(
                f'<option value="{days}"'
                f'{" selected" if current_days == str(days) else ""}>'
                f'{days} {"day" if days == 1 else "days"}</option>'
                for days in (1, 3, 7, 14, 30)
            )
            + "</select>",
            "Save Recording",
        )
        backup_form = self._form(
            "/actions/backup",
            "<p>Download settings, camera access, diagnostics, and the full "
            "event catalog in one encrypted file. Video recordings stay on "
            "the recording drive.</p>"
            + _field(
                "password",
                "Owner password",
                input_type="password",
                autocomplete="current-password",
            ),
            "Download Encrypted Backup",
        )
        checked = (
            " checked"
            if access.get("mode") == "private-vpn" and (remote_ready or approval_pending)
            else ""
        )
        access_body = (
            '<div class="local-access"><span>At home</span>'
            f'<a href="{_text(local_url)}">{_text(local_url)}</a></div>'
            '<form method="post" class="remote-toggle-form" '
            'action="/actions/remote-access" data-remote-toggle-form>'
            f'{self._csrf()}<input type="hidden" name="enabled" value="0">'
            '<label class="switch-row"><span><strong>Enable Remote Access</strong>'
            '<small>Private access through your Tailscale account. No OSE cloud.</small>'
            '</span><input type="checkbox" name="enabled" value="1" '
            f'role="switch" data-remote-toggle{checked}></label>'
            '<noscript><button type="submit">Apply</button></noscript></form>'
        )
        if approval_pending:
            access_body += (
                '<div class="remote-setup" data-remote-pending>'
                '<div><h3>Approve once</h3><p>Scan this code or open Tailscale. '
                'This screen finishes setup automatically.</p>'
                f'<a class="primary-action" href="{_text(approval_url)}">Approve in Tailscale</a></div>'
                '<img class="remote-qr" src="/remote-access-qr.svg?kind=approval" '
                'alt="QR code to approve this recorder in Tailscale"></div>'
            )
        elif remote_ready and remote_url is not None:
            safe_remote_url = _text(remote_url)
            access_body += (
                '<div class="remote-ready"><strong>Ready away from home</strong>'
                '<p>Scan or share once. After adding it to the phone, just tap OSE Cameras.</p>'
                '<div class="remote-actions">'
                f'<a class="primary-action" href="{safe_remote_url}">Open</a>'
                f'<button type="button" data-share-link="{safe_remote_url}">Share Link</button>'
                f'<button type="button" data-copy-link="{safe_remote_url}">Copy Link</button>'
                '<button type="button" data-install-app>Add OSE Cameras</button>'
                '</div><p class="remote-feedback" role="status" aria-live="polite" '
                'data-remote-feedback></p>'
                '<img class="remote-qr" src="/remote-access-qr.svg?kind=open" '
                'alt="QR code to open OSE Cameras"></div>'
            )
        else:
            access_body += (
                '<p class="remote-off"><strong>Away from home</strong><br>'
                'Off. Turn on the switch when you want private phone access.</p>'
            )
        access_body += (
            '<details class="advanced-access"><summary>Use my own HTTPS address</summary>'
            '<p>This is for an owner-controlled gateway. The address is shown only after '
            'the gateway enrollment and HTTPS check both pass.</p>'
            + self._form(
                "/actions/owner-gateway",
                _field(
                    "domain",
                    "HTTPS hostname",
                    autocomplete="off",
                )
                + _field(
                    "enrollment_code",
                    "One-time gateway code",
                    input_type="password",
                    autocomplete="one-time-code",
                ),
                "Verify and Use Address",
            )
            + "</details>"
        )
        push_body = (
            '<p>On your Android phone, open the private HTTPS link in Chrome. '
            'Allow notifications once, then send a test.</p>'
            '<div class="remote-actions">'
            '<button type="button" data-push-enable hidden>Enable on this phone</button>'
            '<button type="button" data-push-test hidden>Send test</button>'
            '<button type="button" data-push-disable hidden>Disable on this phone</button>'
            '</div><p class="push-status" role="status" aria-live="assertive" '
            'data-push-message data-state="working">Checking this phone...</p>'
            '<p class="storage-note">Alerts are sent only for supported detected events, '
            'not background recording segments.</p>'
            '<p class="storage-note">Phone battery restrictions, notification permission '
            'and Do Not Disturb can delay or hide alerts. Test once with the phone '
            'locked; a successful send does not prove the phone displayed it.</p>'
        )
        push_row = (
            self._settings_row(
                "Notifications",
                "Manage this phone",
                push_body,
                section_id="notifications",
                attributes=(
                    f'data-push-panel data-csrf="{_text(self.csrf_token)}"'
                ),
            )
            if owner_ready and not self.factory_enabled
            else ""
        )
        remote_summary = (
            "Approval needed" if approval_pending else "On" if remote_ready else "Off"
        )
        camera_summary = f"{connected} of {expected} online"
        recording_summary = f"Keep {current_days} days"
        password_summary = "Change" if owner_ready else "Set up"
        content = (
            '<section class="settings-heading"><h1>Settings</h1>'
            '<p>Tap a row to change it.</p></section>'
            '<section class="settings-list">'
            f'{self._settings_row("Cameras", camera_summary, self._camera_management_body(), section_id="cameras")}'
            f'{self._settings_row("Recording", recording_summary, recording_form)}'
            f'{self._settings_row("Storage", "Drive space", self._storage_body(), section_id="storage")}'
            f'{self._settings_row("Remote Access", remote_summary, access_body, section_id="remote-access")}'
            f'{push_row}'
            f'{self._settings_row("Password", password_summary, owner_form)}'
            f'{self._settings_row("Backup", "Download", backup_form)}'
            '<p><a class="primary-action" href="/diagnostics">System log</a></p>'
            "</section>"
            '<script src="/remote-access.js" defer></script>'
            '<script src="/push.js" defer></script>'
        )
        return self._layout("Settings", content, notice)

    def _system_log_page(self, query: str, notice: str | None) -> str:
        from .diagnostic_view import render_system_log
        reader = getattr(self.facade, "diagnostic_history", None)
        try:
            history = reader() if callable(reader) else {"samples": [], "events": []}
        except Exception:
            return self._simple_page("System log", "System measurements are unavailable. Try again shortly.", notice)
        names = {slot: str(camera.get("name") or f"Camera {slot}") for slot, camera in enumerate(self.facade.list_cameras(), 1)}
        values = parse_qs(query).get("sample", [])
        selected = int(values[0]) if len(values) == 1 and values[0].isascii() and values[0].isdigit() and len(values[0]) < 5 else None
        return self._layout("System log", render_system_log(history, names, selected=selected), notice)

    def _diagnostic_log_record(self, query: str) -> Mapping[str, object]:
        values = parse_qs(query, keep_blank_values=False, strict_parsing=True)
        if set(values) - {"target", "component", "lines"}:
            raise ValueError("invalid diagnostic log query")
        if any(len(items) != 1 for items in values.values()):
            raise ValueError("invalid diagnostic log query")
        target = values.get("target", ["recorder"])[0]
        component = values.get(
            "component", ["portal" if target == "recorder" else "camera"]
        )[0]
        line_text = values.get("lines", ["100"])[0]
        if not line_text.isascii() or not line_text.isdigit():
            raise ValueError("invalid diagnostic log line count")
        lines = int(line_text)
        if type(lines) is not int or lines not in ALLOWED_LOG_LINES:
            raise ValueError("invalid diagnostic log line count")
        camera_id = None
        if target == "recorder":
            sources = RECORDER_LOG_SOURCES
        else:
            match = re.fullmatch(r"camera-([1-9][0-9]{0,2})", target)
            if match is None:
                raise ValueError("invalid diagnostic log target")
            slot = int(match.group(1))
            cameras = tuple(self.facade.list_cameras())
            if not 1 <= slot <= len(cameras):
                raise ValueError("invalid diagnostic log target")
            camera_id = str(cameras[slot - 1].get("id", ""))
            if not camera_id:
                raise ValueError("invalid diagnostic log target")
            sources = CAMERA_LOG_SOURCES
        if component not in sources:
            raise ValueError("invalid diagnostic log source")
        reader = getattr(self.facade, "read_diagnostic_log", None)
        if not callable(reader):
            raise RuntimeError("diagnostic log reader is unavailable")
        return reader(
            target="recorder" if target == "recorder" else "camera",
            component=component,
            lines=lines,
            camera_id=camera_id,
        )

    def _raw_log_page(self, query: str, notice: str | None) -> str:
        try:
            record = self._diagnostic_log_record(query)
        except ValueError:
            return self._simple_page(
                "Raw service logs",
                "Choose a listed device, log source, and line count.",
                notice,
            )
        except Exception:
            record = {
                "schema_version": 1,
                "state": "unavailable",
                "label": "Service log",
                "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "lines_requested": 100,
                "lines_returned": 0,
                "truncated": False,
                "text": "Log source is unavailable. Check device connectivity and recorder access.",
            }
        query_values = parse_qs(query, keep_blank_values=False)
        target = query_values.get("target", ["recorder"])[0]
        component = query_values.get("component", ["portal"])[0]
        lines = query_values.get("lines", ["100"])[0]
        target_options = ['<option value="recorder"' + (' selected' if target == 'recorder' else '') + '>Recorder</option>']
        target_options.extend(
            f'<option value="camera-{slot}"{" selected" if target == f"camera-{slot}" else ""}>Camera {slot}: {_text(camera.get("name") or f"Camera {slot}")}</option>'
            for slot, camera in enumerate(self.facade.list_cameras(), 1)
        )
        selected_sources = RECORDER_LOG_SOURCES if target == "recorder" else CAMERA_LOG_SOURCES
        source_options = "".join(
            f'<option value="{_text(key)}"{" selected" if key == component else ""}>'
            f'{_text(value)}</option>'
            for key, (value, _units) in selected_sources.items()
        )
        source_select = (
            f'<select name="component" aria-label="Log source">{source_options}</select>'
        )
        line_options = "".join(
            f'<option value="{count}"{" selected" if lines == str(count) else ""}>{count} lines</option>'
            for count in sorted(ALLOWED_LOG_LINES)
        )
        download_query = urlencode({"target": target, "component": component, "lines": lines})
        text = record.get("text", "Log source is unavailable.")
        content = (
            '<p><a href="/diagnostics">System summary</a> · '
            f'<a href="/diagnostics/logs.json?{_text(download_query)}" download>Download this log (JSON)</a></p>'
            '<h1>Raw service logs</h1>'
            '<p>Read-only, recent device output. Credentials are redacted.</p>'
            '<form action="/diagnostics/logs" method="get">'
            f'<label>Device <select name="target">{"".join(target_options)}</select></label> '
            '<button type="submit">Open device</button></form>'
            '<form action="/diagnostics/logs" method="get">'
            f'<input type="hidden" name="target" value="{_text(target)}">'
            f'{source_select} '
            f'<label>Lines <select name="lines">{line_options}</select></label> '
            '<button type="submit">Show log</button></form>'
            f'<p>{_text(record.get("label", "Service log"))} · '
            f'{_text(record.get("state", "unavailable"))} · '
            f'Captured {_text(record.get("captured_at", "unknown"))} · '
            f'{_text(record.get("lines_returned", 0))} lines</p>'
            f'<pre class="raw-service-log" style="max-height:70vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere">{escape(str(text))}</pre>'
        )
        return self._layout("Raw service logs", content, notice)

    def _camera_management_body(self) -> str:
        cameras = self.facade.list_cameras()
        rows: list[str] = []
        for slot, camera in enumerate(cameras, start=1):
            name = camera.get("name") or f"Camera {slot}"
            setup = self._camera_setup_public_status(camera)
            state = self._camera_setup_owner_message(setup)
            if state is None:
                state = "Online" if camera.get("connected") is True else "Offline"
            retry = ""
            if setup is not None and setup.get("state") == "needs-attention":
                retry = self._form(
                    "/actions/camera-setup-retry-slot",
                    f'<input type="hidden" name="slot" value="{slot}">',
                    "Retry setup",
                )
            rows.append(
                "<li>"
                f'<a href="/#camera-{slot}"><strong>{_text(name)}</strong></a>'
                f"<span>{_text(state)}</span>"
                f"{retry}"
                "</li>"
            )
        listing = (
            f'<ul class="camera-list">{"".join(rows)}</ul>'
            if rows
            else '<p class="empty">No cameras are ready to claim.</p>'
        )
        claim_form = self._form(
            "/actions/camera",
            _field(
                "claim_payload",
                "Scanned QR code",
                input_type="password",
                autocomplete="one-time-code",
                required=False,
            )
            + '<p class="camera-claim-help">Scan the label with your phone camera. '
            'If it does not open automatically, paste the scanned text here.</p>'
            + _field(
                "camera_code",
                "Or enter camera code",
                input_type="password",
                autocomplete="one-time-code",
                required=False,
            )
            + '<p class="camera-claim-help">The camera code is printed below the QR code.</p>'
            + _field("name", "Location name"),
            "Add Camera",
        )
        return (
            f"{listing}"
            '<section class="settings-extra"><h3>Add camera</h3>'
            f"{claim_form}</section>"
        )

    @staticmethod
    def _camera_setup_owner_message(
        setup: Mapping[str, object] | None,
    ) -> str | None:
        if setup is None:
            return None
        state = setup.get("state")
        if state == "waiting-network":
            return "Waiting for secure network setup"
        if state == "waiting-heartbeat":
            return "Connecting to recorder"
        if state == "verifying-video":
            return "Checking live video"
        if state == "ready":
            return "Setup complete"
        issues = {
            "unsupported-device": "Secure setup is not supported",
            "network-timeout": "Camera did not join the network",
            "identity-mismatch": "Camera identity could not be verified",
            "heartbeat-timeout": "Camera did not contact the recorder",
            "video-verification-failed": "Camera connected, but video failed",
            "route-commit-failed": "Camera video could not be activated",
        }
        return issues.get(str(setup.get("issue")), "Camera setup needs attention")

    def _live_page(self, notice: str | None) -> str:
        cards = []
        offline_names = []
        cameras = tuple(self.facade.list_cameras())
        alert_state = self.facade.status().get("alerts", {})
        all_alerts_enabled = (
            alert_state.get("enabled") is not False
            if isinstance(alert_state, Mapping)
            else True
        )
        for slot, camera in enumerate(cameras, start=1):
            name = str(camera.get("name") or f"Camera {slot}")
            state = str(camera.get("state", "pending"))
            initially_online = (
                state in {"ready", "claimed"}
                and camera.get("connected") is True
            )
            if not initially_online:
                offline_names.append(name)
            hidden_attribute = "" if initially_online else " hidden"
            if state in {"ready", "claimed"}:
                source = f"/live.jpg?slot={slot}"
                visual = (
                    f'<video data-live-webrtc="/api/v1/cameras/{slot}/webrtc" '
                    f'poster="{source}" autoplay muted playsinline hidden '
                    f'aria-label="Live view from {_text(name)}"></video>'
                    f'<img src="{source}" data-live-source="{source}" '
                    f'alt="Live view from {_text(name)}">'
                )
                status = (
                    "Network conflict"
                    if camera.get("network_issue") == "duplicate-identity"
                    else "Live" if camera.get("connected") is True else "Connecting"
                )
            else:
                visual = '<div class="live-unavailable">Waiting for camera</div>'
                status = "Not ready"
            actions_id = f"camera-{slot}-actions"
            move_earlier = self._camera_position_form(
                camera_slot=slot,
                position=max(1, slot - 1),
                icon="←",
                label="Move earlier",
                disabled=slot == 1,
            )
            move_later = self._camera_position_form(
                camera_slot=slot,
                position=min(len(cameras), slot + 1),
                icon="→",
                label="Move later",
                disabled=slot == len(cameras),
            )
            rename_form = self._camera_rename_form(camera_slot=slot, name=name)
            recording_enabled = camera.get("recording_enabled") is not False
            recording_action = (
                self._camera_boolean_control(
                    slot=slot,
                    action="/actions/camera-recording-slot",
                    label="Recording",
                    enabled=recording_enabled,
                )
                if state in {"ready", "claimed"}
                else ""
            )
            camera_alerts_enabled = camera.get("alerts_enabled") is not False
            alerts_action = (
                self._camera_boolean_control(
                    slot=slot,
                    action="/actions/camera-alerts-slot",
                    label="Alerts",
                    enabled=camera_alerts_enabled,
                    paused=not all_alerts_enabled,
                )
                if state in {"ready", "claimed"}
                else ""
            )
            diagnostics = self._camera_diagnostics_dropdown(camera, slot=slot)
            cards.append(
                f'<article class="live-card" id="camera-{slot}" data-camera-card '
                f'data-camera-slot="{slot}" '
                f'data-live-metrics="/api/v1/cameras/{slot}/metrics" '
                f'data-recording-enabled="{str(recording_enabled).lower()}" '
                f'data-engine-bound="{str(camera.get("video_engine_bound") is not False).lower()}" '
                f'data-camera-name="{_text(name)}"{hidden_attribute} '
                f'data-live-offline="{str(not initially_online).lower()}"'
                '>'
                f'<button class="live-select" type="button" data-select-camera '
                f'aria-expanded="false" aria-controls="{actions_id}">'
                f'<span class="live-visual" data-fullscreen-view>{visual}</span>'
                f'<span class="live-caption"><strong>{_text(name)}</strong>'
                '<span class="live-caption-metrics">'
                '<small data-live-signal>RSSI unavailable</small>'
                '<small data-live-rate title="Frames shown by this browser, not the camera or recording rate.">Measuring view</small>'
                '<small data-live-input-rate title="Detect is frames reaching Frigate. Skipped per second are frames Frigate did not process; this does not prove recording loss.">Checking detect input</small>'
                '<small class="recording-alert" data-recording-alert hidden title="Frigate found an indexed video segment; playback has not been checked.">Video index not checked</small>'
                f'<small data-live-status>{_text(status)}</small>'
                '</span></span></button>'
                f'<div class="camera-actions" id="{actions_id}" '
                'data-camera-actions role="toolbar" '
                f'aria-label="Actions for {_text(name)}" hidden>'
                '<button class="camera-action" type="button" data-fullscreen '
                'title="Full screen" aria-label="Full screen">'
                '<span aria-hidden="true">&#x26F6;</span><span>Full screen</span></button>'
                '<button class="camera-action" type="button" data-rename-camera '
                'title="Rename camera" aria-label="Rename camera">'
                '<span aria-hidden="true">&#x270E;</span><span>Rename</span></button>'
                f'<a class="camera-action" href="/events?camera={slot}" '
                'title="Recorded events"><span aria-hidden="true">&#x25B6;</span>'
                '<span>Events</span></a>'
                f'{recording_action}'
                f'{alerts_action}'
                f'{move_earlier}'
                '<span class="camera-action camera-drag" draggable="true" '
                'data-drag-camera role="button" tabindex="0" title="Move camera" '
                'aria-label="Drag to move camera"><span aria-hidden="true">&#x2630;</span>'
                '<span>Move</span></span>'
                f'{move_later}'
                f'</div>{rename_form}{diagnostics}</article>'
            )
        listing = (
            f'<section class="live-grid">{"".join(cards)}</section>'
            if cards
            else '<p class="empty">Waiting for cameras.</p>'
        )
        connected = sum(camera.get("connected") is True for camera in cameras)
        offline_summary = (
            '<section class="offline-strip" data-offline-summary'
            + ("" if offline_names else " hidden")
            + '><strong><span data-offline-count>'
            + str(len(offline_names))
            + '</span> offline</strong><span data-offline-names>'
            + _text(", ".join(offline_names))
            + '</span></section>'
        )
        content = (
            '<section class="live-heading"><h1>Feeds</h1>'
            f'<p>{connected} of {len(cameras)} online</p>'
            f'{self._global_alert_control(enabled=all_alerts_enabled)}</section>'
            f"{listing}"
            f"{offline_summary}"
            '<form method="post" action="/actions/camera-position" '
            'data-camera-order-form hidden>'
            f'{self._csrf()}'
            '<input type="hidden" name="slot">'
            '<input type="hidden" name="position"></form>'
            '<script src="/live.js" defer></script>'
        )
        return self._layout("Feeds", content, notice)

    def _camera_page(self, query: str, notice: str | None) -> str:
        camera = self._camera_for_query(query)
        if camera is None:
            return self._error_page("Camera is unavailable.")
        cameras = tuple(self.facade.list_cameras())
        slot = cameras.index(camera) + 1
        name = str(camera.get("name") or f"Camera {slot}")
        state = str(camera.get("state", "pending"))
        if state in {"ready", "claimed"}:
            source = f"/live.jpg?slot={slot}"
            visual = (
                f'<video class="camera-view" '
                f'data-live-webrtc="/api/v1/cameras/{slot}/webrtc" '
                f'poster="{source}" autoplay muted playsinline hidden '
                f'aria-label="Live view from {_text(name)}"></video>'
                f'<img class="camera-view" src="{source}" '
                f'data-live-source="{source}" alt="Live view from {_text(name)}">'
            )
            naming = self._form(
                "/actions/camera-name",
                '<input type="hidden" name="camera_id" '
                f'value="{_text(camera.get("id", ""))}">'
                + _field("name", "Camera name", value=name),
                "Save Name",
            )
            recording_enabled = camera.get("recording_enabled") is not False
            recording_setting = self._form(
                "/actions/camera-recording-slot",
                f'<input type="hidden" name="slot" value="{slot}">'
                '<label for="camera-recording-state">Recording</label>'
                '<select id="camera-recording-state" name="enabled">'
                f'<option value="true"{" selected" if recording_enabled else ""}>'
                'Active</option>'
                f'<option value="false"{" selected" if not recording_enabled else ""}>'
                'Inactive</option></select>'
                f'<p>Recording is {"on" if recording_enabled else "paused"}. '
                "Live viewing remains available either way.</p>",
                "Save Recording",
            )
        else:
            visual = '<div class="live-unavailable">Waiting for camera</div>'
            naming = ""
            recording_setting = ""
        health = self._camera_health((camera,))
        diagnostics = self._camera_health_panel(
            health,
            section_id="diagnostics",
            heading="Diagnostics",
            message="Current camera checks and support information.",
            action=self._form(
                "/actions/camera-check-slot",
                f'<input type="hidden" name="slot" value="{slot}">',
                "Run checks",
            ),
        )
        content = (
            f'<section class="live-heading"><p><a href="/live">All cameras</a></p>'
            f"<h1>{_text(name)}</h1></section>{visual}"
            f'<section class="panel narrow camera-settings" id="settings">'
            f'<h2>Settings</h2>{naming}{recording_setting}</section>'
            f"{diagnostics}"
            '<script src="/live.js" defer></script>'
        )
        return self._layout("Camera", content, notice)

    def _recordings_page(self, query: str, notice: str | None) -> str:
        cameras = tuple(self.facade.list_cameras())
        camera_names = {
            str(camera.get("id", "")): str(camera.get("name") or f"Camera {slot}")
            for slot, camera in enumerate(cameras, start=1)
        }
        camera_slots = {
            str(camera.get("id", "")): slot
            for slot, camera in enumerate(cameras, start=1)
        }
        selected_camera_id = None
        selected_camera_name = None
        selected_values = parse_qs(query, keep_blank_values=False).get("camera", [])
        if len(selected_values) == 1 and selected_values[0].isdigit():
            selected_slot = int(selected_values[0])
            if 1 <= selected_slot <= len(cameras):
                selected_camera = cameras[selected_slot - 1]
                selected_camera_id = str(selected_camera.get("id", ""))
                selected_camera_name = camera_names.get(selected_camera_id, "Camera")
        event_values = parse_qs(query, keep_blank_values=False).get("event", [])
        selected_event_id = event_values[0] if len(event_values) == 1 else None
        event_history_unavailable = False
        saved_clips_unavailable = False
        try:
            video_events = tuple(
                self.facade.list_video_events(
                    limit=100,
                    camera_id=selected_camera_id,
                )
            )
        except Exception:
            video_events = ()
            event_history_unavailable = True
        try:
            saved_clips = tuple(
                self.facade.list_saved_video_clips(
                    limit=100,
                    camera_id=selected_camera_id,
                )
            )
        except Exception:
            saved_clips = ()
            saved_clips_unavailable = True
        try:
            engine_enabled = self.facade.video_engine_status().get("engine") != "legacy"
        except Exception:
            # Never present background recording chunks as detections when the
            # event engine's state is temporarily unavailable.
            engine_enabled = True
            event_history_unavailable = True
        try:
            recordings = (
                ()
                if selected_event_id is not None or engine_enabled
                else tuple(self.facade.list_recordings(limit=100))
            )
        except Exception:
            recordings = ()
            event_history_unavailable = True
        items = []
        for event in video_events:
            event_id = str(event.get("id", ""))
            camera_id = str(event.get("camera_id", ""))
            started_at = str(event.get("started_at", ""))
            ended_at = event.get("ended_at")
            if not event_id or not camera_id or not started_at:
                continue
            if selected_event_id is not None and event_id != selected_event_id:
                continue
            if selected_camera_id is not None and camera_id != selected_camera_id:
                continue
            labels = event.get("labels", ())
            label = (
                str(labels[0]).replace("_", " ").title()
                if isinstance(labels, (tuple, list)) and labels
                else "Activity"
            )
            severity = str(event.get("severity", "detection"))
            preview = "/event-preview.mp4?" + urlencode({"event": event_id})
            vod = "/event-vod?" + urlencode(
                {"event": event_id, "resource": "master.m3u8"}
            )
            thumbnail = "/event-thumbnail.jpg?" + urlencode({"event": event_id})
            download = "/event-preview.mp4?" + urlencode(
                {"event": event_id, "download": "1"}
            )
            camera_name = camera_names.get(camera_id, "Camera")
            duration = _event_duration_text(started_at, ended_at)
            duration_markup = (
                f'<span class="event-duration">{_text(duration)}</span>'
                if duration
                else ""
            )
            if ended_at is None:
                camera_slot = camera_slots.get(camera_id)
                live_link = (
                    f"/#camera-{camera_slot}" if camera_slot is not None else "/"
                )
                items.append(
                    '<article class="event-card" data-active-event>'
                    f'<a class="active-event-view" href="{_text(live_link)}">'
                    f'<img src="{_text(thumbnail)}" loading="lazy" decoding="async" '
                    f'alt="{_text(camera_name)} activity in progress">'
                    '<span class="event-live">Live · Recording now</span></a>'
                    f"<strong>{_text(camera_name)}</strong>"
                    f'<span class="event-kind">{_text(label)} {_text(severity)}</span>'
                    f'<time data-event-time datetime="{_text(started_at)}">'
                    f"{_text(started_at)}</time>"
                    "</article>"
                )
                continue
            items.append(
                '<article class="event-card">'
                f'<button class="event-view" type="button" data-event-video '
                f'data-event-vod="{_text(vod)}" '
                f'data-event-preview="{_text(preview)}" '
                f'data-event-poster="{_text(thumbnail)}" '
                f'data-event-label="{_text(camera_name)} {severity} preview">'
                f'<img src="{_text(thumbnail)}" loading="lazy" decoding="async" '
                f'alt="{_text(camera_name)} {severity} preview">'
                '<span class="event-play"><span aria-hidden="true">&#x25B6;</span> '
                '<span>Play event</span></span></button>'
                f"<strong>{_text(camera_name)}</strong>"
                f'<span class="event-kind">{_text(label)} {_text(severity)}</span>'
                f"{duration_markup}"
                f'<time data-event-time datetime="{_text(started_at)}">'
                f"{_text(started_at)}</time>"
                '<div class="event-actions">'
                + self._form(
                    "/actions/event-save",
                    f'<input type="hidden" name="event" value="{_text(event_id)}">',
                    "Save Clip",
                )
                + f'<a href="{_text(download)}" download>Download</a>'
                + self._form(
                    "/actions/event-delete",
                    f'<input type="hidden" name="event" value="{_text(event_id)}">',
                    "Delete",
                )
                + "</div>"
                "</article>"
            )
        for recording in recordings:
            reference = str(recording.get("reference", ""))
            camera_id = str(recording.get("camera_id", ""))
            started_at = str(recording.get("started_at", ""))
            if not reference or not camera_id or not started_at:
                continue
            if selected_camera_id is not None and camera_id != selected_camera_id:
                continue
            source = "/recording.mjpeg?" + urlencode({"reference": reference})
            download = "/recording.mjpeg?" + urlencode(
                {"reference": reference, "download": "1"}
            )
            preview = "/recording-preview.jpg?" + urlencode(
                {"reference": reference}
            )
            camera_name = camera_names.get(camera_id, "Camera")
            items.append(
                '<article class="event-card">'
                f'<button class="event-view" type="button" data-event-toggle '
                f'data-event-source="{_text(source)}" aria-pressed="false">'
                f'<img src="{_text(preview)}" data-preview-source="{_text(preview)}" '
                f'alt="{_text(camera_name)} recording preview">'
                '<span class="event-play"><span aria-hidden="true">&#x25B6;</span> '
                '<span data-event-label>Play</span></span></button>'
                f"<strong>{_text(camera_name)}</strong>"
                f'<time data-event-time datetime="{_text(started_at)}">'
                f"{_text(started_at)}</time>"
                f'<a class="event-download" href="{_text(download)}" download>'
                "Download</a>"
                "</article>"
            )
        listing = (
            f'<section class="recording-grid">{"".join(items)}</section>'
            if items
            else (
                '<p class="notice" role="alert" data-event-history-unavailable>'
                'Event history is unavailable. Check recording storage and '
                'recorder status; this does not mean there were no events.</p>'
                if event_history_unavailable
                else (
                    '<p class="empty">That event is no longer available.</p>'
                    if selected_event_id is not None
                    else '<p class="empty">No events yet.</p>'
                )
            )
        )
        saved_items = []
        for clip in saved_clips:
            clip_id = str(clip.get("id", ""))
            camera_id = str(clip.get("camera_id", ""))
            created_at = str(clip.get("created_at", ""))
            in_progress = clip.get("in_progress") is True
            if not clip_id or not camera_id or not created_at:
                continue
            source = "/saved-clip.mp4?" + urlencode({"clip": clip_id})
            download = "/saved-clip.mp4?" + urlencode(
                {"clip": clip_id, "download": "1"}
            )
            media = (
                '<div class="saved-clip-pending">Saving clip…</div>'
                if in_progress
                else (
                    '<button class="event-view saved-clip-view" type="button" '
                    'data-event-video '
                    f'data-event-preview="{_text(source)}" '
                    f'data-event-label="Saved clip from '
                    f'{_text(camera_names.get(camera_id, "Camera"))}">'
                    '<span class="event-play saved-clip-play">'
                    '<span aria-hidden="true">&#x25B6;</span> '
                    '<span>Play saved clip</span></span></button>'
                )
            )
            actions = (
                '<span>Finishing…</span>'
                if in_progress
                else f'<a href="{_text(download)}" download>Download</a>'
            )
            saved_items.append(
                '<article class="event-card saved-clip-card">'
                f"{media}<strong>{_text(camera_names.get(camera_id, 'Camera'))}</strong>"
                '<span class="event-kind">Saved clip</span>'
                f'<time data-event-time datetime="{_text(created_at)}">'
                f"{_text(created_at)}</time>"
                f'<div class="event-actions">{actions}'
                + self._form(
                    "/actions/saved-clip-delete",
                    f'<input type="hidden" name="clip" value="{_text(clip_id)}">',
                    "Delete",
                )
                + "</div></article>"
            )
        saved_listing = (
            '<section class="saved-clips"><h2>Saved Clips</h2>'
            '<p>These clips are kept until you delete them.</p>'
            f'<div class="recording-grid">{"".join(saved_items)}</div></section>'
            if saved_items
            else ""
        )
        if saved_clips_unavailable:
            saved_listing += (
                '<p class="notice" role="alert" data-saved-clips-unavailable>'
                'Saved clips are unavailable. Check recording storage and '
                'recorder status.</p>'
            )
        heading = "Events" if selected_camera_name is None else f"{selected_camera_name} Events"
        back = (
            ""
            if selected_camera_name is None
            else '<p><a href="/events">All camera events</a></p>'
        )
        content = (
            f'<section class="hero"><h1>{_text(heading)}</h1>{back}</section>'
            + listing
            + saved_listing
            + '<script src="/events.js" defer></script>'
        )
        return self._layout("Events", content, notice)

    @staticmethod
    def _camera_health(cameras: Sequence[Mapping[str, object]]) -> tuple[CameraHealth, ...]:
        summaries = []
        for camera in cameras:
            verification = camera.get("verification")
            if not isinstance(verification, VerificationReport):
                verification = None
            connected = camera.get("connected")
            summaries.append(
                camera_health_summary(
                    str(camera.get("id", "")),
                    camera_name=str(camera.get("name") or "Unnamed camera"),
                    connected=connected if isinstance(connected, bool) else None,
                    verification=verification,
                )
            )
        return tuple(summaries)

    def _camera_diagnostics_dropdown(
        self, camera: Mapping[str, object], *, slot: int
    ) -> str:
        health = self._camera_health((camera,))[0]
        state_classes = {
            HEALTH_OK: "health-ok",
            HEALTH_ATTENTION: "health-attention",
            HEALTH_NOT_CHECKED: "health-unknown",
        }
        statuses = "".join(
            "<div>"
            f"<dt>{_text(item.name)}</dt>"
            f'<dd class="{state_classes[item.status]}">{_text(item.status)}</dd>'
            "</div>"
            for item in health.statuses
        )
        checked = (
            f'<p class="health-checked">Last checked: {_text(health.checked_at)}</p>'
            if health.checked_at
            else '<p class="health-checked">No diagnostic check recorded yet.</p>'
        )
        action = self._form(
            "/actions/camera-check-slot",
            f'<input type="hidden" name="slot" value="{slot}">',
            "Check camera stream now",
        )
        conflict = ""
        if camera.get("network_issue") == "duplicate-identity":
            conflict = (
                '<div class="health-attention"><strong>Duplicate camera identity</strong>'
                '<p>The working feed was kept. Reset or unplug the extra camera, then recheck.</p>'
                + self._form(
                    "/actions/camera-conflict-slot",
                    f'<input type="hidden" name="slot" value="{slot}">',
                    "Recheck after reset",
                )
                + "</div>"
            )
        live = (
            '<h4>Live video</h4><dl class="health-list live-diagnostics">'
            '<div><dt>Path</dt><dd data-live-path>Starting</dd></div>'
            '<div><dt>Connection</dt><dd data-live-connection>Starting</dd></div>'
            '<div><dt>Displayed rate</dt><dd data-live-rate>Measuring</dd></div>'
            '<div><dt>Detection input</dt><dd data-live-detect-input-rate>Checking</dd></div>'
            '<div><dt>Frames processed</dt><dd data-live-processed-rate>Checking</dd></div>'
            '<div><dt>AI checks</dt><dd data-live-object-rate>Checking</dd></div>'
            '<div><dt>Frames skipped</dt><dd data-live-skip-rate>Checking</dd></div>'
            '<div><dt>Saved video</dt><dd data-live-recording-detail>Checking retained video index</dd></div>'
            '<div><dt>Camera signal</dt><dd data-live-signal-detail>Checking</dd></div>'
            '<div><dt>Camera stream</dt><dd data-live-camera-stream>Not recently tested</dd></div>'
            '<div><dt>Recorder input</dt><dd data-live-source-health>Checking</dd></div>'
            '<div><dt>Recorder load</dt><dd data-live-recorder-load>Checking</dd></div>'
            '<div><dt>Source format</dt><dd data-live-source-format>Checking</dd></div>'
            '<div><dt>Likely cause</dt><dd data-live-diagnosis>Checking</dd></div>'
            '</dl>'
            '<p>View FPS is what this browser displays. Detection input is what Frigate receives. AI checks are separate. Skipped frames are not proof that saved video was lost.</p>'
            f'<button type="button" data-recording-coverage-url="/api/v1/cameras/{slot}/recording-coverage">'
            'Check saved video now</button>'
            '<p data-recording-coverage-result>Saved video not checked.</p>'
        )
        return (
            f'<details class="camera-diagnostics-dropdown" '
            f'id="camera-{slot}-diagnostics" data-camera-diagnostics>'
            '<summary><span aria-hidden="true">&#x24D8;</span> Diagnostics</summary>'
            '<div class="camera-diagnostics-content">'
            f'<dl class="health-list">{statuses}</dl>{checked}{conflict}{live}'
            '<p>To separate camera/network loss from recorder ingest failure, '
            'run the camera stream check while input FPS is zero.</p>'
            f'{action}'
            '<a class="download" href="/support-report.json" download>'
            "Download Support Report</a></div></details>"
        )

    @staticmethod
    def _camera_health_panel(
        cameras: Sequence[CameraHealth],
        *,
        section_id: str | None = None,
        heading: str = "Camera Health",
        message: str = "See what is working and what needs attention.",
        action: str = "",
    ) -> str:
        cards = []
        state_classes = {
            HEALTH_OK: "health-ok",
            HEALTH_ATTENTION: "health-attention",
            HEALTH_NOT_CHECKED: "health-unknown",
        }
        for camera in cameras:
            statuses = "".join(
                "<div>"
                f"<dt>{_text(item.name)}</dt>"
                f'<dd class="{state_classes[item.status]}">{_text(item.status)}</dd>'
                "</div>"
                for item in camera.statuses
            )
            checked = (
                f'<p class="health-checked">Last checked: {_text(camera.checked_at)}</p>'
                if camera.checked_at
                else '<p class="health-checked">No diagnostic check recorded yet.</p>'
            )
            cards.append(
                '<article class="health-card">'
                f"<h3>{_text(camera.camera_name)}</h3>"
                f'<dl class="health-list">{statuses}</dl>'
                f"{checked}"
                "</article>"
            )
        listing = (
            f'<div class="health-grid">{"".join(cards)}</div>'
            if cards
            else '<p class="empty">No cameras have been added.</p>'
        )
        identifier = "" if section_id is None else f' id="{_text(section_id)}"'
        return (
            f'<section class="panel camera-diagnostics"{identifier}>'
            f'<h2>{_text(heading)}</h2>'
            f'<p>{_text(message)}</p>'
            f"{listing}"
            f"{action}"
            '<p><a class="download" href="/support-report.json" download>'
            "Download Support Report</a></p></section>"
        )

    def _storage_body(self) -> str:
        try:
            storage = self.facade.recording_storage_status()
        except Exception:
            storage = {"state": "unavailable"}
        state = str(storage.get("state", "planned"))
        if state == "ready":
            total = _nonnegative_int(storage.get("total_bytes"))
            used = _nonnegative_int(storage.get("used_bytes"))
            available = _nonnegative_int(storage.get("available_bytes"))
            reserved = _nonnegative_int(storage.get("reserved_bytes"))
            if total is None or used is None or available is None or reserved is None:
                body = '<p class="empty">Recording drive details are unavailable.</p>'
            else:
                progress = min(used, total)
                body = (
                    '<p class="storage-state"><strong>Ready</strong>'
                    '<span>Recordings are being kept on the attached drive.</span></p>'
                    f'<progress value="{progress}" max="{max(1, total)}" '
                    'aria-label="Recording drive space used"></progress>'
                    '<dl class="storage-facts">'
                    f'<div><dt>Used</dt><dd>{_text(_format_bytes(used))}</dd></div>'
                    f'<div><dt>Available</dt><dd>{_text(_format_bytes(available))}</dd></div>'
                    f'<div><dt>Drive size</dt><dd>{_text(_format_bytes(total))}</dd></div>'
                    '</dl>'
                    f'<p class="storage-note">{_text(_format_bytes(reserved))} is kept '
                    'free so recording can stop safely before the drive fills.</p>'
                )
        elif state == "unavailable":
            body = (
                '<p class="empty">The configured recording drive is unavailable. '
                'Check its power and connection.</p>'
            )
        else:
            body = '<h2>Recording Drive</h2>' + self._storage_form()
        return body

    def _provisioning_page(self, notice: str | None) -> str:
        model_options = "".join(
            f'<option value="{_text(value)}">{_text(label)}</option>'
            for value, label in _CAMERA_MODELS.items()
        )
        kit_form = self._form(
            "/actions/kit",
            _field(
                "camera_count",
                "Number of cameras",
                input_type="number",
                minimum=1,
                maximum=SOFTWARE_CAMERA_LIMIT,
                value="4",
            )
            + '<label for="camera_model">Camera model</label>'
            f'<select id="camera_model" name="camera_model" required>{model_options}</select>'
            + _field(
                "wifi_country",
                "Wi-Fi country code",
                value=self.wifi_country_default,
            ),
            "Create Kit",
        )
        try:
            targets = tuple(self.facade.list_media_targets())
        except Exception:
            targets = ()
        camera_form = "<p>Insert a supported system card, then reload.</p>"
        if targets:
            target_options = []
            confirmations = []
            for target in targets:
                stable_id = str(getattr(target, "stable_id", ""))
                display_name = str(getattr(target, "display_name", "System card"))
                phrase = str(getattr(target, "confirmation", ""))
                if not stable_id or not phrase:
                    continue
                target_options.append(
                    f'<option value="{_text(stable_id)}">{_text(display_name)}</option>'
                )
                confirmations.append(
                    f"<li>{_text(display_name)}: <code>{_text(phrase)}</code></li>"
                )
            if target_options:
                flash_body = (
                    '<label for="stable_id">System card</label>'
                    f'<select id="stable_id" name="stable_id" required>{"".join(target_options)}</select>'
                    '<p>Type the matching erase phrase:</p>'
                    f'<ul>{"".join(confirmations)}</ul>'
                    + _field("confirmation", "Erase confirmation")
                )
                flash_form = self._form("/actions/flash", flash_body, "Flash")
                camera_body = (
                    _field(
                        "single_camera_number",
                        "Camera number",
                        input_type="number",
                        minimum=1,
                        maximum=SOFTWARE_CAMERA_LIMIT,
                        value="1",
                    )
                    + '<label for="single_camera_model">Camera model</label>'
                    f'<select id="single_camera_model" name="single_camera_model" required>{model_options}</select>'
                    + _field("single_wifi_ssid", "Wi-Fi network")
                    + _field(
                        "single_wifi_passphrase",
                        "Wi-Fi passphrase",
                        input_type="password",
                        autocomplete="new-password",
                    )
                    + _field(
                        "single_wifi_passphrase_confirm",
                        "Confirm Wi-Fi passphrase",
                        input_type="password",
                        autocomplete="new-password",
                    )
                    + _field(
                        "single_wifi_country",
                        "Wi-Fi country code",
                        value=self.wifi_country_default,
                    )
                    + '<label for="single_stable_id">System card</label>'
                    f'<select id="single_stable_id" name="single_stable_id" required>{"".join(target_options)}</select>'
                    + '<p>Type the matching erase phrase:</p>'
                    + f'<ul>{"".join(confirmations)}</ul>'
                    + _field("single_confirmation", "Erase confirmation")
                )
                camera_form = self._form(
                    "/actions/camera-provision", camera_body, "Provision Camera"
                )
            else:
                flash_form = "<p>Insert a supported system card, then reload.</p>"
        else:
            flash_form = "<p>Insert a supported system card, then reload.</p>"
        verify_form = self._form(
            "/actions/verify",
            "<p>Check the recorder and every camera before packing.</p>",
            "Verify Kit",
        )
        try:
            diagnostic_items = tuple(self.facade.pilot_diagnostics())
        except Exception:
            diagnostic_items = ()
        diagnostics = ""
        if diagnostic_items:
            rows = []
            for item in diagnostic_items:
                rows.append(
                    "<li>"
                    f"<strong>Camera {_text(item.get('camera_number', ''))}</strong>"
                    f"<span>SSH: {_text(item.get('username', ''))}@"
                    f"{_text(item.get('private_network_ssh', ''))}</span>"
                    f"<span>USB: {_text(item.get('usb_serial', ''))}</span>"
                    f"<span>UART: {_text(item.get('gpio_uart', ''))}</span>"
                    f"<span>Availability: {_text(item.get('early_boot', ''))}</span>"
                    f"<span>SSH key: <code>{_text(item.get('ssh_key_file', ''))}</code></span>"
                    f"<span>Credentials: <code>{_text(item.get('credentials_file', ''))}</code></span>"
                    "</li>"
                )
            diagnostics = (
                '<section class="panel"><h2>Pilot Diagnostics</h2>'
                '<p>Recovery secrets stay in the private files shown below.</p>'
                f'<ul class="camera-list">{"".join(rows)}</ul></section>'
            )
        content = (
            '<section class="hero"><p class="eyebrow">Factory</p><h1>Provisioning</h1>'
            "<p>Create a complete kit or provision one camera.</p></section>"
            f'<section class="panel narrow"><h2>Provision Camera</h2>{camera_form}</section>'
            '<section class="columns three">'
            f'<article class="panel"><h2>Create Kit</h2>{kit_form}</article>'
            f'<article class="panel"><h2>Flash</h2>{flash_form}</article>'
            f'<article class="panel"><h2>Final Check</h2>{verify_form}</article>'
            "</section>"
            f"{diagnostics}"
        )
        return self._layout("Provisioning", content, notice, factory=True)

    def _simple_page(self, title: str, message: str, notice: str | None) -> str:
        return self._layout(
            title,
            f'<section class="hero"><h1>{_text(title)}</h1><p>{_text(message)}</p></section>',
            notice,
        )

    def _error_page(self, message: str) -> str:
        return self._layout(
            "Unable to continue",
            '<section class="hero"><h1>Unable to continue</h1>'
            f"<p>{_text(message)}</p></section>",
            None,
        )

    def _login_page(self, message: str | None = None) -> str:
        notice = (
            f'<p class="login-error" role="alert">{_text(message)}</p>'
            if message
            else ""
        )
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#50abe7">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="OSE Cameras">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/app-icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/app-icon.svg">
<title>Sign in · OSE Cameras</title>
<style>
:root{{--ink:#102a43;--paper:#edf2fb;--card:#ffffff;--accent:#50abe7;--line:#cbdceb}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:1rem;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}
.login{{width:min(100%,380px);padding:1.6rem;background:var(--card);border:1px solid var(--line);border-radius:1rem;box-shadow:0 12px 35px #102a431a}}
.brand{{color:#1778ad;font-size:.82rem;font-weight:800;letter-spacing:.1em}}h1{{margin:.45rem 0 .25rem;font-size:2rem}}p{{margin:.25rem 0 1.25rem;color:#526779}}form{{display:grid;gap:.65rem}}label{{font-weight:700}}input,button{{font:inherit;border-radius:.55rem;padding:.75rem .85rem}}input{{border:1px solid #a9c9dc;background:#fff}}button{{margin-top:.35rem;border:0;background:var(--accent);color:#102a43;font-weight:750;cursor:pointer}}.login-error{{padding:.65rem .75rem;border-radius:.5rem;background:#fce7e7;color:#8a1c1c}}
.system-log-table{{width:100%;border-collapse:collapse}}.system-log-table th,.system-log-table td{{text-align:left;padding:.3rem .5rem;border-bottom:1px solid var(--line);overflow-wrap:anywhere}}.system-log-table th{{width:60%}}.system-log-device{{margin:.7rem 0}}.system-log-device>summary{{cursor:pointer;font-weight:750;padding:.5rem}}
</style>
</head>
<body><main class="login"><div class="brand">OSE Cameras</div><h1>Sign in</h1>
<p>Use the recorder owner password.</p>{notice}
<form method="post" action="/actions/login">{self._csrf()}
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button></form></main>
<script src="/camera-setup.js" defer></script></body></html>"""

    def _layout(
        self,
        title: str,
        content: str,
        notice: str | None,
        *,
        factory: bool = False,
    ) -> str:
        navigation = _NAVIGATION
        nav = "".join(
            f'<a href="{href}"{" aria-current=\"page\"" if title == label else ""}>{label}</a>'
            for label, href in navigation
        )
        notice_text = _NOTICES.get(notice or "")
        banner = (
            f'<div class="notice" role="status">{_text(notice_text)}</div>'
            if notice_text
            else ""
        )
        factory_link = (
            '<a class="factory-link" href="/settings">Return to Settings</a>'
            if factory
            else (
                '<a class="factory-link" href="/provisioning">Factory Provisioning</a>'
                if self.factory_enabled
                else ""
            )
        )
        session_action = ""
        if self._authentication_required():
            session_action = (
                '<form class="session-form" method="post" action="/actions/logout">'
                f'{self._csrf()}<button type="submit">Log out</button></form>'
            )
        body_class = (
            "wall-page"
            if title == "Feeds"
            else "settings-page" if title == "Settings" else ""
        )
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#50abe7">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="OSE Cameras">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/app-icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/app-icon.svg">
<title>{_text(title)} · OSE Cameras</title>
<style>
:root{{--ink:#102a43;--muted:#526779;--paper:#edf2fb;--card:#ffffff;--accent:#50abe7;--accent-text:#176c9e;--line:#cbdceb}}
*{{box-sizing:border-box}}[hidden]{{display:none!important}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}
header{{background:var(--card);border-bottom:1px solid var(--line)}}.brand{{grid-area:brand;text-align:center;color:var(--accent-text);font-weight:800;letter-spacing:.08em}}
.top{{max-width:1120px;margin:auto;padding:.45rem 1.25rem;display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"brand brand" "nav actions";align-items:center;gap:.15rem .75rem}}
nav{{grid-area:nav;display:flex;gap:.25rem;min-width:0;flex-wrap:nowrap}}nav a{{color:var(--ink);padding:.35rem .55rem;border-radius:.5rem;text-decoration:none}}
nav a[aria-current]{{background:#bae0f3;color:#102a43}}.header-actions{{grid-area:actions;display:flex;align-items:center;justify-content:flex-end;gap:.25rem;min-width:0}}main{{max-width:1120px;margin:auto;padding:2.5rem 1.25rem 4rem}}
.hero{{max-width:720px;margin-bottom:2rem}}h1{{font-size:clamp(2.1rem,5vw,4rem);line-height:1;margin:.25rem 0 1rem}}h2{{margin-top:0}}
.eyebrow{{color:var(--accent-text);font-weight:750;text-transform:uppercase;letter-spacing:.12em}}.summary,.columns{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1rem;margin:1.5rem 0}}
.summary article,.panel{{background:var(--card);border:1px solid var(--line);border-radius:1rem;padding:1.3rem;box-shadow:0 8px 25px #17211b0a}}
.summary span,.camera-list span{{display:block;color:var(--muted);font-size:.9rem}}.summary strong{{display:block;margin-top:.3rem;font-size:1.15rem}}
.three{{grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}}.narrow{{max-width:560px}}form{{display:grid;gap:.65rem}}
label{{font-weight:700;margin-top:.3rem}}input,select,button{{font:inherit;border-radius:.55rem;padding:.7rem .8rem;border:1px solid #a9c9dc}}input,select{{background:#fff;color:var(--ink)}}
button{{margin-top:.5rem;border:0;background:var(--accent);color:#102a43;font-weight:750;cursor:pointer}}button:hover{{filter:brightness(.92)}}
.session-form{{display:block;margin:0}}.session-form button{{margin:0;padding:.3rem .55rem;background:transparent;color:var(--muted);font-size:.8rem}}.wall-page .session-form button{{color:#edf2fb}}
.notice{{background:#dff1e6;border:1px solid #a7d3b7;padding:.75rem 1rem;border-radius:.7rem;margin-bottom:1.5rem}}
.camera-list{{list-style:none;padding:0;margin:0;display:grid}}.camera-list li{{display:flex;align-items:center;justify-content:space-between;gap:1rem;border-bottom:1px solid var(--line);padding:.55rem 0}}.camera-list a{{color:var(--accent-text);text-decoration:none}}.empty{{color:var(--muted)}}
.settings-page .top{{max-width:760px;padding:.35rem .8rem}}.settings-page main{{max-width:760px;padding:.8rem 1rem 2rem}}.settings-heading{{display:flex;align-items:baseline;gap:.75rem;margin:0 0 .6rem;padding:0 .15rem}}.settings-heading h1{{margin:0;font-size:1.45rem;line-height:1.2}}.settings-heading p{{margin:0;color:var(--muted);font-size:.88rem}}.settings-list{{overflow:hidden;background:var(--card);border:1px solid var(--line);border-radius:.75rem;box-shadow:0 8px 24px #102a4312}}.settings-row{{border-bottom:1px solid var(--line)}}.settings-row:last-child{{border-bottom:0}}.settings-row>summary{{display:grid;grid-template-columns:minmax(0,1fr) auto 1rem;align-items:center;gap:.7rem;min-height:3.15rem;padding:.65rem .85rem;cursor:pointer;list-style:none}}.settings-row>summary::-webkit-details-marker{{display:none}}.settings-row>summary:hover,.settings-row>summary:focus-visible,.settings-row[open]>summary{{background:#edf2fb}}.settings-row-title{{font-weight:800}}.settings-row-value{{color:var(--muted);font-size:.86rem;text-align:right}}.settings-row-chevron{{color:var(--accent-text);font-size:1.35rem;line-height:1;transition:transform .15s ease}}.settings-row[open] .settings-row-chevron{{transform:rotate(90deg)}}.settings-row-body{{padding:.85rem 1rem 1.05rem;border-top:1px solid var(--line);background:#f8fbfe}}.settings-row-body>form{{max-width:520px}}.settings-extra{{margin-top:.75rem}}.settings-extra>summary{{color:var(--accent-text);cursor:pointer;font-weight:750}}.settings-extra form{{margin-top:.75rem}}.camera-claim-help{{margin:.1rem 0;color:var(--muted);font-size:.86rem}}
.live-heading{{margin-bottom:1.25rem}}.live-heading h1{{font-size:2.4rem;margin-bottom:.5rem}}.live-heading p{{margin:.25rem 0;color:var(--muted)}}
.visually-hidden{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}.live-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),480px));gap:1rem;align-items:start}}.live-card{{display:block;background:#111;color:#fff;border-radius:.7rem;overflow:hidden;border:1px solid #252525}}.live-card.selected{{border-color:#87ceeb;box-shadow:0 0 0 1px #87ceeb}}.live-card.dragging{{opacity:.55}}.live-select{{display:grid;grid-template-rows:minmax(0,1fr) auto;width:100%;height:100%;margin:0;padding:0;border:0;border-radius:0;background:#111;color:#fff;text-align:left}}.live-visual{{display:block;min-width:0;min-height:0;background:#000}}.live-visual:fullscreen{{display:grid;place-items:center;width:100vw;height:100vh}}.live-visual:fullscreen img,.live-visual:fullscreen video{{width:100vw;height:100vh;object-fit:contain}}.live-card img,.live-card video,.camera-view,.live-unavailable{{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#111}}.live-caption{{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.7rem .85rem}}.live-caption-metrics{{display:flex;align-items:center;justify-content:flex-end;gap:.65rem;white-space:nowrap}}.live-card small{{color:#bae0f3}}.live-card img.offline{{opacity:.15}}.live-unavailable{{display:grid;place-items:center;color:#bae0f3}}.camera-actions{{display:flex;align-items:center;gap:.25rem;padding:.35rem;background:#14293a;border-top:1px solid #29485f;overflow-x:auto}}.camera-action-form{{display:block;margin:0}}.camera-action{{display:flex;align-items:center;justify-content:center;gap:.35rem;min-width:0;margin:0;padding:.4rem .55rem;border:0;border-radius:.35rem;background:transparent;color:#edf2fb;font-size:.8rem;font-weight:700;text-decoration:none;white-space:nowrap}}.camera-action:hover,.camera-action:focus-visible{{background:#1c4f70;filter:none}}.camera-action span[aria-hidden]{{font-size:1rem}}.setting-toggle.active span[aria-hidden]{{color:#87ceeb}}.setting-toggle.inactive span[aria-hidden]{{color:#b8c4cf}}.camera-control{{flex:1 0 auto;margin:0}}.camera-control summary{{list-style:none;cursor:pointer}}.camera-control summary::-webkit-details-marker{{display:none}}.camera-control-form{{display:block;margin:.25rem 0 0;padding:.35rem;border-radius:.35rem;background:#1c4f70;color:#edf2fb}}.camera-control-form button{{width:100%;margin:0;padding:.4rem .5rem;background:#50abe7;font-size:.8rem;white-space:nowrap}}.camera-position:disabled{{opacity:.28;cursor:default}}.camera-position:disabled:hover{{background:transparent}}.camera-drag{{cursor:grab;user-select:none}}.camera-drag:active{{cursor:grabbing}}.camera-rename{{display:grid;grid-template-columns:minmax(8rem,1fr) auto auto;gap:.35rem;padding:.45rem;background:#14293a;border-top:1px solid #29485f}}.camera-rename input,.camera-rename button{{margin:0;padding:.45rem .6rem;border-radius:.35rem}}.camera-rename button.secondary{{background:#1c4f70;color:#edf2fb}}.camera-diagnostics-dropdown{{border-top:1px solid #29485f;background:#102a43}}.camera-diagnostics-dropdown summary{{cursor:pointer;padding:.55rem .75rem;color:#edf2fb;font-size:.82rem;font-weight:700}}.camera-diagnostics-content{{padding:0 .75rem .75rem}}.camera-diagnostics-content .health-list{{margin:.2rem 0 .6rem}}.camera-diagnostics-content form{{margin:.7rem 0}}.camera-diagnostics-content .download{{color:#87ceeb}}.camera-view{{max-width:960px;border-radius:.7rem}}.camera-settings,.camera-diagnostics{{margin-top:1rem}}.health-checked{{margin-bottom:0;color:var(--muted);font-size:.82rem}}
.live-card[hidden]{{display:none!important}}
.wall-page{{background:#071726;color:#edf2fb}}.wall-page header{{background:#102a43;border-color:#50abe7}}.wall-page .top{{max-width:none;min-height:0;margin:0;padding:.2rem .65rem;gap:.05rem .5rem}}.wall-page .brand{{font-size:.82rem;color:#87ceeb}}.wall-page nav{{gap:.1rem;flex-wrap:nowrap}}.wall-page nav a{{color:#edf2fb;font-size:.82rem;padding:.25rem .45rem}}.wall-page nav a[aria-current]{{background:#50abe7;color:#102a43}}.wall-page main{{max-width:none;margin:0;padding:.4rem}}.wall-page .live-heading{{display:flex;align-items:center;gap:.75rem;margin:0 .15rem .35rem}}.wall-page .live-heading h1{{font-size:1rem;line-height:1.2;margin:0}}.wall-page .live-heading p{{font-size:.78rem;margin:0;color:#bae0f3}}.global-alert-control{{margin-left:auto;color:#edf2fb;font-size:.78rem}}.global-alert-control>summary{{cursor:pointer;font-weight:700;list-style:none}}.global-alert-control>summary::-webkit-details-marker{{display:none}}.global-alert-control form{{display:block;margin:.3rem 0 0;padding:.35rem;background:#1c4f70;border-radius:.35rem}}.global-alert-control button{{width:100%;margin:0;padding:.35rem .45rem;background:#50abe7;font-size:.78rem;white-space:nowrap}}.wall-page .live-grid{{grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:auto;gap:.4rem;align-items:start}}.wall-page .live-card{{display:block;min-width:0;border-radius:.2rem}}.wall-page .live-card[hidden]{{display:none}}.wall-page .live-select{{display:block;height:auto;min-height:0}}.wall-page .live-visual{{display:block;width:100%;aspect-ratio:16/9;overflow:hidden}}.wall-page .live-card img,.wall-page .live-card video,.wall-page .live-unavailable{{width:100%;height:100%;aspect-ratio:auto;object-fit:contain}}.wall-page .live-caption{{padding:.3rem .5rem;font-size:.85rem;line-height:1.2}}.wall-page .live-card small{{font-size:.72rem}}.wall-page .camera-actions{{overflow-x:auto}}.wall-page .camera-action,.wall-page .camera-action-form{{flex:1 0 auto}}.offline-strip{{display:flex;align-items:center;gap:.6rem;margin-top:.4rem;padding:.45rem .6rem;border:1px solid #50abe7;border-radius:.25rem;background:#102a43;color:#edf2fb;font-size:.8rem}}.offline-strip[hidden]{{display:none}}.offline-strip a{{margin-left:auto;color:#87ceeb}}
.recording-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}}.recording-grid article{{background:var(--card);border:1px solid var(--line);border-radius:.5rem;overflow:hidden}}.event-view,.active-event-view{{position:relative;display:block;width:100%;margin:0;padding:0;border:0;border-radius:0;background:#111}}.recording-grid img,.recording-grid video{{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#111}}.event-play,.event-live{{position:absolute;left:.6rem;bottom:.6rem;display:flex!important;gap:.3rem;padding:.35rem .55rem!important;border-radius:1rem;background:#101310d9;color:#fff!important;font-size:.8rem!important;font-weight:700}}.event-live{{background:#9f1239e6}}.saved-clip-view{{aspect-ratio:16/9;background:linear-gradient(135deg,#102a43,#176c9e)}}.saved-clip-play{{left:50%;bottom:50%;transform:translate(-50%,50%);white-space:nowrap}}.recording-grid strong,.recording-grid time,.event-kind,.event-duration{{display:block;padding:.65rem .8rem 0}}.event-kind,.event-duration{{padding-top:.2rem;color:var(--muted)}}.event-duration{{font-size:.9rem}}.recording-grid time{{padding-top:.2rem;padding-bottom:.8rem;color:var(--muted);font-size:.9rem}}.event-download{{display:block;padding:0 .8rem .8rem;color:var(--accent-text);font-weight:700}}.event-actions{{display:flex;align-items:center;gap:.4rem;padding:.15rem .8rem .8rem;flex-wrap:wrap}}.event-actions form{{display:block;margin:0}}.event-actions button,.event-actions a{{display:inline-flex;align-items:center;margin:0;padding:.45rem .6rem;border-radius:.45rem;font-size:.85rem;font-weight:750;text-decoration:none}}.event-actions a{{background:#edf2fb;color:var(--accent-text)}}.event-actions form:last-child button{{background:#8a1538}}.saved-clips{{margin-top:2rem}}.saved-clips>p{{color:var(--muted)}}.saved-clip-pending{{display:grid;place-items:center;width:100%;aspect-ratio:16/9;background:#111;color:#fff;font-weight:700}}
.health-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1rem}}.health-card{{border:1px solid var(--line);border-radius:.7rem;padding:1rem}}.health-card h3{{margin-top:0}}
.health-list{{margin:0}}.health-list div{{display:flex;justify-content:space-between;gap:1rem;padding:.3rem 0}}.health-list dt{{font-weight:700}}.health-list dd{{margin:0}}.health-ok{{color:#166534}}.health-attention{{color:#9f1239;font-weight:700}}.health-unknown{{color:var(--muted)}}.download{{color:var(--accent-text);font-weight:700}}
.live-card .recording-alert{{color:#ffb4b4;font-weight:800}}.recording-alert[hidden]{{display:none}}
.live-caption{{align-items:flex-start}}.live-caption-metrics{{flex-wrap:wrap;gap:.2rem .5rem;white-space:normal}}
.storage-state{{display:grid;gap:.15rem}}.storage-state strong{{font-size:1.35rem;color:var(--accent-text)}}.storage-state span,.storage-note{{color:var(--muted)}}progress{{width:100%;height:1rem;accent-color:var(--accent)}}.storage-facts{{display:grid;grid-template-columns:repeat(3,1fr);gap:.7rem;margin:1.2rem 0}}.storage-facts div{{display:grid;gap:.15rem}}.storage-facts dt{{color:var(--muted);font-size:.85rem}}.storage-facts dd{{margin:0;font-weight:750}}
.local-access{{display:grid;gap:.15rem;margin-bottom:1rem}}.local-access span,.switch-row small,.remote-off,.remote-ready p,.remote-setup p,.remote-feedback{{color:var(--muted)}}.local-access a{{color:var(--accent-text);font-weight:700}}.remote-toggle-form{{padding:1rem 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}}.switch-row{{display:flex;align-items:center;justify-content:space-between;gap:1rem;margin:0;cursor:pointer}}.switch-row span{{display:grid;gap:.15rem}}.switch-row small{{font-weight:400}}.switch-row input{{width:2rem;height:2rem;flex:0 0 auto;margin:0;accent-color:var(--accent)}}.remote-setup{{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:1rem;margin-top:1rem}}.remote-setup h3{{margin:.2rem 0}}.remote-ready{{margin-top:1rem}}.remote-actions{{display:flex;flex-wrap:wrap;gap:.5rem;margin:.8rem 0}}.remote-actions button,.primary-action{{display:inline-flex;align-items:center;justify-content:center;margin:0;padding:.62rem .78rem;border:0;border-radius:.55rem;background:#edf2fb;color:var(--accent-text);font:inherit;font-weight:750;text-decoration:none;cursor:pointer}}.remote-actions .primary-action,.remote-setup .primary-action{{background:var(--accent);color:#102a43}}.remote-qr{{display:block;width:180px;max-width:100%;height:auto;margin-top:.8rem;border:8px solid #fff;border-radius:.5rem}}.remote-feedback{{min-height:1.5em;margin:.4rem 0}}.remote-off{{margin:1rem 0 0}}
.push-status{{min-height:2.8rem;margin:.8rem 0;padding:.7rem .8rem;border:1px solid var(--line);border-radius:.55rem;background:#f3f5f3;font-weight:750}}.push-status[data-state="working"]{{color:#176c9e;background:#edf2fb;border-color:#87ceeb}}.push-status[data-state="ready"]{{color:#14532d;background:#e5f6eb;border-color:#9bc8aa}}.push-status[data-state="error"]{{color:#8a1538;background:#fff0f3;border-color:#e5a4b6}}
.factory-link{{font-size:.85rem;color:var(--muted);text-decoration:none}}@media(max-width:720px){{main{{padding-top:1.5rem}}.settings-page main{{padding:.65rem .6rem 1.5rem}}.settings-page .top{{padding:.45rem .6rem}}.settings-heading{{margin-bottom:.45rem}}.settings-heading p{{display:none}}.settings-row>summary{{padding:.6rem .7rem}}.settings-row-body{{padding:.75rem}}.storage-facts{{gap:.35rem;font-size:.88rem}}.wall-page main{{padding:.3rem}}.wall-page .live-grid{{grid-template-columns:1fr;grid-auto-rows:auto}}}}
</style>
</head>
<body class="{body_class}"><header><div class="top"><div class="brand">OSE Cameras</div><nav aria-label="Main">{nav}</nav><div class="header-actions">{session_action}{factory_link}</div></div></header>
<main>{banner}{content}</main><script src="/camera-setup.js" defer></script></body></html>"""

    @staticmethod
    def _respond(
        start_response,
        status: HTTPStatus,
        body: str | bytes,
        *,
        head: bool,
        content_type: str = "text/html; charset=utf-8",
        download_name: str | None = None,
        extra_headers: Sequence[tuple[str, str]] = (),
    ):
        if status == HTTPStatus.SEE_OTHER:
            headers = [
                ("Location", str(body)),
                ("Content-Length", "0"),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
            ]
            payload = b""
        else:
            payload = body if isinstance(body, bytes) else body.encode("utf-8")
            headers = [
                ("Content-Type", content_type),
                ("Content-Length", str(len(payload))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                (
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                    "media-src 'self'; script-src 'self'; connect-src 'self'; "
                    "worker-src 'self'; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                ),
            ]
            if download_name is not None:
                headers.append(
                    ("Content-Disposition", f'attachment; filename="{download_name}"')
                )
        headers.extend(extra_headers)
        start_response(f"{status.value} {status.phrase}", headers)
        return [] if head else [payload]


class _PrivateRequestHandler(WSGIRequestHandler):
    """Avoid recording request targets; submitted values never reach logs."""

    def log_message(self, format: str, *args: object) -> None:
        return None


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Keep one unavailable camera from blocking every other live tile."""

    daemon_threads = True


def create_application(
    facade: ApplicationFacade,
    *,
    csrf_token: str | None = None,
    factory_enabled: bool = False,
    wifi_country_default: str | None = None,
    time_source: Callable[[], float] = time.monotonic,
) -> LocalWebApplication:
    """Create an injectable WSGI application without starting a server."""

    return LocalWebApplication(
        facade,
        csrf_token=csrf_token,
        factory_enabled=factory_enabled,
        wifi_country_default=wifi_country_default,
        time_source=time_source,
    )


def create_server(
    facade: ApplicationFacade,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    csrf_token: str | None = None,
    factory_enabled: bool = False,
    wifi_country_default: str | None = None,
) -> WSGIServer:
    """Create a local server; the caller controls its lifetime."""

    application = create_application(
        facade,
        csrf_token=csrf_token,
        factory_enabled=factory_enabled,
        wifi_country_default=wifi_country_default,
    )
    return make_server(
        host,
        port,
        application,
        server_class=_ThreadingWSGIServer,
        handler_class=_PrivateRequestHandler,
    )
