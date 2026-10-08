"""Test-only, synchronous Playwright input-to-observed-paint measurement.

The endpoint is matching pixels in a CDP screencast frame with a swap timestamp,
not a DOM/state/API acknowledgement. It is an upper bound on first paint because
Chrome may omit frames; it is not physical-monitor photon latency. Only loopback
or in-memory pages are accepted. No PhotoCraft runtime hooks are installed.

PixelOracle coordinates are physical PNG pixels, not CSS/document coordinates.
Use small, unique canvas patches, away from selection handles and local cursors.
The caller owns browser/context lifecycle, native document correctness and cleanup.
"""

import base64
from collections import Counter, deque
from dataclasses import asdict, dataclass
import hashlib
import io
import math
import time
from urllib.parse import urlparse
import uuid

from PIL import Image, ImageChops


def _local(page):
    url = urlparse(page.url)
    if url.scheme not in {'about', 'data'} and not (
        url.scheme in {'http', 'https'} and url.hostname in {'127.0.0.1', '::1', 'localhost'}
    ):
        raise ValueError('Paint probes require loopback or in-memory pages')


def _ms():
    return time.perf_counter_ns() / 1_000_000


def _roi_size(box):
    if len(box) != 4 or not all(type(v) is int for v in box):
        raise ValueError('Oracle requires four integer pixel coordinates')
    left, top, right, bottom = box
    if left < 0 or top < 0 or right <= left or bottom <= top or (right-left)*(bottom-top) > 16_000_000:
        raise ValueError('Oracle must be a positive region of at most 16 million pixels')
    return right-left, bottom-top


@dataclass(frozen=True)
class PixelOracle:
    box: tuple
    expected: Image.Image
    tolerance: int = 0
    minimum_fraction: float = 1.0

    def __post_init__(self):
        if (self.expected.size != _roi_size(self.box)
                or not 0 <= self.tolerance <= 255 or not 0 < self.minimum_fraction <= 1):
            raise ValueError('Invalid pixel oracle geometry or tolerance')

    @classmethod
    def solid(cls, box, rgb, **kwargs):
        return cls(tuple(box), Image.new('RGB', _roi_size(box), rgb), **kwargs)

    def matches(self, frame):
        if self.box[2] > frame.width or self.box[3] > frame.height:
            raise ValueError('Oracle lies outside captured PNG; check DPR/viewport')
        difference = ImageChops.difference(frame.crop(self.box).convert('RGB'), self.expected.convert('RGB'))
        bad = sum(max(rgb) > self.tolerance for rgb in difference.get_flattened_data())
        return 1 - bad / (difference.width * difference.height) >= self.minimum_fraction


@dataclass(frozen=True)
class ClockCalibration:
    event_offset_ms: float
    frame_offset_ms: float
    uncertainty_ms: float
    round_trip_ms: float

    @classmethod
    def capture(cls, page, rounds=5):
        """Bound a browser clock reading by its host request/response interval.

        Input event timestamps use performance.timeOrigin. CDP's widely available
        swap timestamp uses epoch time, so calibrate Date.now separately. Its 1 ms
        quantization is included. Optional CDP monotonicTimestamp is retained as
        evidence only; this implementation does not assume its clock origin.
        """
        readings = []
        for _ in range(rounds):
            before = _ms()
            clock = page.evaluate('() => ({event: performance.timeOrigin + performance.now(), wall: Date.now()})')
            after = _ms()
            midpoint = (before + after) / 2
            readings.append(cls(midpoint-clock['event'], midpoint-clock['wall'],
                                (after-before)/2 + 1, after-before))
        return min(readings, key=lambda reading: reading.round_trip_ms)


def _valid_stamp(stamp):
    return type(stamp) in {float, int} and math.isfinite(stamp) and stamp > 0


def _frame_swap_ms(metadata, clock, received):
    stamp = metadata.get('timestamp')
    if not _valid_stamp(stamp):
        raise ValueError('missing_or_invalid_frame_timestamp')
    swapped = stamp * 1000 + clock.frame_offset_ms
    # Epoch/time-origin mismatches must not masquerade as an ordinary timeout.
    # Frames cannot swap after their receipt beyond calibration uncertainty.
    if not math.isfinite(swapped) or swapped > received + clock.uncertainty_ms:
        raise ValueError('frame_timestamp_after_receive')
    if received - swapped > 60000:
        raise ValueError('frame_timestamp_too_old')
    return swapped


def classify_latency(latency_ms, uncertainty_ms, target_ms, errors=(), max_uncertainty_ms=15):
    """Never upgrade a failed/invalid observation to a passing percentile sample."""
    if errors or not all(math.isfinite(v) for v in [latency_ms, uncertainty_ms, target_ms]):
        return 'invalid'
    if uncertainty_ms < 0 or uncertainty_ms > max_uncertainty_ms or latency_ms < -uncertainty_ms:
        return 'invalid'
    return 'passed' if latency_ms + uncertainty_ms < target_ms else 'over_budget'


def summarize_results(results):
    counts = dict(Counter(result['status'] for result in results))
    values = sorted(result['latency_upper_ms'] for result in results
                    if result['status'] in {'passed', 'over_budget'})
    distribution = {'count': len(values)}
    if values:
        distribution.update({f'p{p}_ms': values[math.ceil(len(values)*p/100)-1] for p in (50, 95, 99)})
        distribution['max_ms'] = values[-1]
    return {'total': len(results), 'counts': counts,
            'all_passed': bool(results) and counts.get('passed', 0) == len(results),
            'observed_upper_bounds': distribution,
            'tail_note': 'Nearest-rank descriptive percentiles; small samples do not establish tail reliability.',
            'failure_note': 'Timeout/invalid samples remain in counts and fail all_passed; no finite latency is invented.'}


class PaintObserver:
    """Observe one receiving page; measure() returns a JSON-safe result, even on failure.

    with PaintObserver(receiver) as observer:
        result = observer.measure(sender, lambda: sender.mouse.click(x, y),
                                  PixelOracle.solid((100, 100, 108, 108), (255, 0, 0)))

    The trigger must produce exactly the intended first event of event_type. Move
    the mouse to its starting point before measuring pointermove. Use separate
    observers for fan-out peers; measure() is a sequential convenience API, not a
    concurrent multi-peer scheduler. last_matching_png can be saved as evidence.
    Each measurement first requires an acknowledged baseline PNG from the same
    CDP stream. Chrome's screencast surface may differ from an emulated viewport;
    capture_baseline records its actual dimensions. No screenshot API is used.
    """

    def __init__(self, page, max_frames=64, max_queue_bytes=32*1024*1024, max_frame_bytes=8*1024*1024,
                 max_frame_pixels=16_000_000):
        _local(page)
        if min(max_frames, max_queue_bytes, max_frame_bytes, max_frame_pixels) <= 0:
            raise ValueError('Capture bounds must be positive')
        self.page = page
        self.max_frames, self.max_queue_bytes, self.max_frame_bytes = max_frames, max_queue_bytes, max_frame_bytes
        self.max_frame_pixels = max_frame_pixels
        self.frames = deque()
        self.queue_bytes = 0
        self.errors = []
        self.error_count = 0
        self.frame_count = 0
        self.last_matching_png = None
        self.last_baseline_png = None
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.on('Page.screencastFrame', self._frame)

    def _error(self, code):
        self.error_count += 1
        if len(self.errors) < 16:
            self.errors.append(code)

    def _frame(self, event):
        # Never decode PNGs in this callback or hold Chrome's acknowledgement.
        received = _ms()
        try:
            self.cdp.send('Page.screencastFrameAck', {'sessionId': event['sessionId']})
        except Exception:
            self._error('frame_ack_failed')
        encoded = event.get('data', '')
        self.frame_count += 1
        if not _valid_stamp(event.get('metadata', {}).get('timestamp')):
            self._error('missing_or_invalid_frame_timestamp')
        elif len(encoded) > self.max_frame_bytes * 4 // 3 + 4:
            self._error('frame_too_large')
        elif len(self.frames) >= self.max_frames or self.queue_bytes + len(encoded) > self.max_queue_bytes:
            self._error('capture_queue_overflow')
        else:
            self.frames.append((encoded, event.get('metadata', {}), received))
            self.queue_bytes += len(encoded)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        try:
            self.cdp.send('Page.stopScreencast')
        finally:
            self.cdp.detach()

    def _baseline(self, oracle, clock, result):
        # captureScreenshot can use a different emulated surface and does not
        # establish that the screencast is running. Await its own acknowledged
        # PNG before arming input. Never force a canvas repaint during timing.
        self.cdp.send('Page.stopScreencast')
        self.frames.clear()
        self.queue_bytes = 0
        self.cdp.send('Page.startScreencast', {'format': 'png', 'everyNthFrame': 1})
        deadline = _ms() + 2000
        while not self.frames and not self.errors and _ms() < deadline:
            self.page.wait_for_timeout(5)
        if self.errors:
            return False
        if not self.frames:
            result['errors'].append('capture_not_ready')
            return False
        encoded, metadata, received = self.frames.popleft()
        self.queue_bytes -= len(encoded)
        try:
            swapped = _frame_swap_ms(metadata, clock, received)
        except ValueError as error:
            result['errors'].append(str(error))
            return False
        png = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(png)) as frame:
            if frame.width * frame.height > self.max_frame_pixels:
                result['errors'].append('decoded_frame_too_large')
                return False
            result['capture_baseline'] = {'width': frame.width, 'height': frame.height,
                                          'metadata': metadata, 'swap_host_ms': swapped,
                                          'received_host_ms': received,
                                          'png_sha256': hashlib.sha256(png).hexdigest()}
            self.last_baseline_png = png
            if oracle.matches(frame):
                result['errors'].append('expected_pixels_already_visible_before_input')
                return False
        return True

    def measure(self, sender, trigger, oracle, *, event_type='pointerdown', target_ms=500,
                timeout_ms=2000, max_uncertainty_ms=15):
        _local(sender)
        if event_type not in {'pointerdown', 'pointermove', 'pointerup', 'keydown', 'input'}:
            raise ValueError('Unsupported captured input event')
        if not 0 < target_ms <= timeout_ms <= 60000:
            raise ValueError('Require 0 < target <= timeout <= 60000 ms')
        key = '__paint_input_' + uuid.uuid4().hex
        result = {'status': 'invalid', 'target_ms': target_ms, 'timeout_ms': timeout_ms,
                  'endpoint': 'first observed matching CDP swapped PNG; not physical display',
                  'input': None, 'frame': None, 'latency_ms': None, 'latency_lower_ms': None,
                  'latency_upper_ms': None, 'uncertainty_ms': None, 'errors': []}
        self.last_matching_png = None
        self.last_baseline_png = None
        start_error = self.error_count
        self.errors.clear()
        initial_frame_count = self.frame_count
        armed_frame_count = None
        try:
            source_clock = ClockCalibration.capture(sender)
            receiver_clock = ClockCalibration.capture(self.page)
            result['clock_before'] = {'sender': asdict(source_clock), 'receiver': asdict(receiver_clock)}
            if not self._baseline(oracle, receiver_clock, result):
                return result
            self.frames.clear()
            self.queue_bytes = 0
            sender.evaluate('''([key, type]) => {
                const state = {event: null};
                state.listener = event => {
                    if (state.event) return;
                    state.event = {type: event.type, trusted: event.isTrusted,
                        epoch_ms: performance.timeOrigin + event.timeStamp,
                        x: event.clientX ?? null, y: event.clientY ?? null, key: event.key ?? null};
                };
                window[key] = state;
                window.addEventListener(type, state.listener, true);
            }''', [key, event_type])
            dispatch_started = _ms()
            armed_frame_count = self.frame_count
            trigger()
            result['trigger_elapsed_ms'] = _ms()-dispatch_started
            input_event = sender.evaluate('(key) => window[key].event', key)
            result['input'] = input_event
            if not input_event or not input_event['trusted']:
                result['errors'].append('missing_or_untrusted_input')
                return result
            started = input_event['epoch_ms'] + source_clock.event_offset_ms
            deadline = started + timeout_ms
            result['input_host_ms'] = started
            matched = None
            while True:
                while self.frames:
                    encoded, metadata, received = self.frames.popleft()
                    self.queue_bytes -= len(encoded)
                    try:
                        swapped = _frame_swap_ms(metadata, receiver_clock, received)
                    except ValueError as error:
                        self._error(str(error))
                        continue
                    if swapped < started or swapped > deadline:
                        continue
                    png = base64.b64decode(encoded, validate=True)
                    with Image.open(io.BytesIO(png)) as image:
                        if image.width * image.height > self.max_frame_pixels:
                            self._error('decoded_frame_too_large')
                            continue
                        if (image.width, image.height) != (result['capture_baseline']['width'], result['capture_baseline']['height']):
                            self._error('capture_geometry_changed_after_input')
                            continue
                        if oracle.matches(image):
                            matched = swapped
                            self.last_matching_png = png
                            result['frame'] = {'metadata': metadata, 'png_sha256': hashlib.sha256(png).hexdigest(),
                                               'width': image.width, 'height': image.height,
                                               'swap_host_ms': swapped, 'received_host_ms': received}
                            break
                if matched is not None or self.error_count > start_error or _ms() > deadline:
                    break
                self.page.wait_for_timeout(5)  # pump CDP; arrival time is never the endpoint
            source_after = ClockCalibration.capture(sender, rounds=3)
            receiver_after = ClockCalibration.capture(self.page, rounds=3)
            result['clock_after'] = {'sender': asdict(source_after), 'receiver': asdict(receiver_after)}
            source_drift = abs(source_after.event_offset_ms - source_clock.event_offset_ms)
            receiver_drift = abs(receiver_after.frame_offset_ms - receiver_clock.frame_offset_ms)
            if source_drift > source_clock.uncertainty_ms + source_after.uncertainty_ms + 2:
                result['errors'].append('sender_clock_drift')
            if receiver_drift > receiver_clock.uncertainty_ms + receiver_after.uncertainty_ms + 2:
                result['errors'].append('receiver_clock_drift')
            uncertainty = source_clock.uncertainty_ms + receiver_clock.uncertainty_ms + source_drift + receiver_drift
            result['uncertainty_ms'] = uncertainty
            if uncertainty > max_uncertainty_ms:
                result['errors'].append('clock_uncertainty_exceeded')
            if matched is None:
                result['status'] = 'timeout'
                result['censored_after_ms'] = max(0, _ms()-started)
            else:
                latency = matched-started
                result.update(latency_ms=latency, latency_lower_ms=max(0, latency-uncertainty),
                              latency_upper_ms=latency+uncertainty)
                result['status'] = classify_latency(latency, uncertainty, target_ms,
                                                     result['errors'] + self.errors, max_uncertainty_ms)
        except Exception as error:
            result['errors'].append('measurement_exception:' + type(error).__name__)
        finally:
            try:
                sender.evaluate('''([key, type]) => {
                    if (window[key]) window.removeEventListener(type, window[key].listener, true);
                    delete window[key];
                }''', [key, event_type])
            except Exception:
                result['errors'].append('input_listener_cleanup_failed')
            result['errors'].extend(self.errors)
            result['capture_errors'] = self.error_count-start_error
            result['captured_frames'] = self.frame_count-initial_frame_count
            result['timed_captured_frames'] = None if armed_frame_count is None else self.frame_count-armed_frame_count
            if result['errors']:
                result['status'] = 'invalid'
        return result
