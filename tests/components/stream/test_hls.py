"""The tests for hls streams."""
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlparse

import av
import pytest

from homeassistant.components.stream import create_stream
from homeassistant.components.stream.const import (
    HLS_PROVIDER,
    MAX_SEGMENTS,
    NUM_PLAYLIST_SEGMENTS,
)
from homeassistant.components.stream.core import Segment
from homeassistant.const import HTTP_NOT_FOUND
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util

from tests.common import async_fire_time_changed
from tests.components.stream.common import generate_h264_video

STREAM_SOURCE = "some-stream-source"
INIT_BYTES = b"init"
MOOF_BYTES = b"some-bytes"
DURATION = 10
TEST_TIMEOUT = 5.0  # Lower than 9s home assistant timeout
MAX_ABORT_SEGMENTS = 20  # Abort test to avoid looping forever


class HlsClient:
    """Test fixture for fetching the hls stream."""

    def __init__(self, http_client, parsed_url):
        """Initialize HlsClient."""
        self.http_client = http_client
        self.parsed_url = parsed_url

    async def get(self, path=None):
        """Fetch the hls stream for the specified path."""
        url = self.parsed_url.path
        if path:
            # Strip off the master playlist suffix and replace with path
            url = "/".join(self.parsed_url.path.split("/")[:-1]) + path
        return await self.http_client.get(url)


@pytest.fixture
def hls_stream(hass, hass_client):
    """Create test fixture for creating an HLS client for a stream."""

    async def create_client_for_stream(stream):
        http_client = await hass_client()
        parsed_url = urlparse(stream.endpoint_url(HLS_PROVIDER))
        return HlsClient(http_client, parsed_url)

    return create_client_for_stream


def make_segment(segment, discontinuity=False):
    """Create a playlist response for a segment."""
    response = []
    if discontinuity:
        response.append("#EXT-X-DISCONTINUITY")
    response.extend(["#EXTINF:10.0000,", f"./segment/{segment}.m4s"]),
    return "\n".join(response)


def make_playlist(sequence, discontinuity_sequence=0, segments=[]):
    """Create a an hls playlist response for tests to assert on."""
    response = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-TARGETDURATION:10",
        '#EXT-X-MAP:URI="init.mp4"',
        f"#EXT-X-MEDIA-SEQUENCE:{sequence}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}",
    ]
    response.extend(segments)
    response.append("")
    return "\n".join(response)


async def test_hls_stream(hass, hls_stream, stream_worker_sync):
    """
    Test hls stream.

    Purposefully not mocking anything here to test full
    integration with the stream component.
    """
    await async_setup_component(hass, "stream", {"stream": {}})

    stream_worker_sync.pause()

    # Setup demo HLS track
    source = generate_h264_video()
    stream = create_stream(hass, source)

    # Request stream
    stream.add_provider(HLS_PROVIDER)
    stream.start()

    hls_client = await hls_stream(stream)

    # Fetch playlist
    playlist_response = await hls_client.get()
    assert playlist_response.status == 200

    # Fetch init
    playlist = await playlist_response.text()
    init_response = await hls_client.get("/init.mp4")
    assert init_response.status == 200

    # Fetch segment
    playlist = await playlist_response.text()
    segment_url = "/" + playlist.splitlines()[-1]
    segment_response = await hls_client.get(segment_url)
    assert segment_response.status == 200

    stream_worker_sync.resume()

    # Stop stream, if it hasn't quit already
    stream.stop()

    # Ensure playlist not accessible after stream ends
    fail_response = await hls_client.get()
    assert fail_response.status == HTTP_NOT_FOUND


async def test_stream_timeout(hass, hass_client, stream_worker_sync):
    """Test hls stream timeout."""
    await async_setup_component(hass, "stream", {"stream": {}})

    stream_worker_sync.pause()

    # Setup demo HLS track
    source = generate_h264_video()
    stream = create_stream(hass, source)

    # Request stream
    stream.add_provider(HLS_PROVIDER)
    stream.start()
    url = stream.endpoint_url(HLS_PROVIDER)

    http_client = await hass_client()

    # Fetch playlist
    parsed_url = urlparse(url)
    playlist_response = await http_client.get(parsed_url.path)
    assert playlist_response.status == 200

    # Wait a minute
    future = dt_util.utcnow() + timedelta(minutes=1)
    async_fire_time_changed(hass, future)

    # Fetch again to reset timer
    playlist_response = await http_client.get(parsed_url.path)
    assert playlist_response.status == 200

    stream_worker_sync.resume()

    # Wait 5 minutes
    future = dt_util.utcnow() + timedelta(minutes=5)
    async_fire_time_changed(hass, future)
    await hass.async_block_till_done()

    # Ensure playlist not accessible
    fail_response = await http_client.get(parsed_url.path)
    assert fail_response.status == HTTP_NOT_FOUND


async def test_stream_timeout_after_stop(hass, hass_client, stream_worker_sync):
    """Test hls stream timeout after the stream has been stopped already."""
    await async_setup_component(hass, "stream", {"stream": {}})

    stream_worker_sync.pause()

    # Setup demo HLS track
    source = generate_h264_video()
    stream = create_stream(hass, source)

    # Request stream
    stream.add_provider(HLS_PROVIDER)
    stream.start()

    stream_worker_sync.resume()
    stream.stop()

    # Wait 5 minutes and fire callback.  Stream should already have been
    # stopped so this is a no-op.
    future = dt_util.utcnow() + timedelta(minutes=5)
    async_fire_time_changed(hass, future)
    await hass.async_block_till_done()


async def test_stream_keepalive(hass):
    """Test hls stream retries the stream when keepalive=True."""
    await async_setup_component(hass, "stream", {"stream": {}})

    # Setup demo HLS track
    source = "test_stream_keepalive_source"
    stream = create_stream(hass, source)
    track = stream.add_provider(HLS_PROVIDER)
    track.num_segments = 2

    cur_time = 0

    def time_side_effect():
        nonlocal cur_time
        if cur_time >= 80:
            stream.keepalive = False  # Thread should exit and be joinable.
        cur_time += 40
        return cur_time

    with patch("av.open") as av_open, patch(
        "homeassistant.components.stream.time"
    ) as mock_time, patch(
        "homeassistant.components.stream.STREAM_RESTART_INCREMENT", 0
    ):
        av_open.side_effect = av.error.InvalidDataError(-2, "error")
        mock_time.time.side_effect = time_side_effect
        # Request stream
        stream.keepalive = True
        stream.start()
        stream._thread.join()
        stream._thread = None
        assert av_open.call_count == 2

    # Stop stream, if it hasn't quit already
    stream.stop()


async def test_hls_playlist_view_no_output(hass, hass_client, hls_stream):
    """Test rendering the hls playlist with no output segments."""
    await async_setup_component(hass, "stream", {"stream": {}})

    stream = create_stream(hass, STREAM_SOURCE)
    stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    # Fetch playlist
    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 404


async def test_hls_playlist_view(hass, hls_stream, stream_worker_sync):
    """Test rendering the hls playlist with 1 and 2 output segments."""
    await async_setup_component(hass, "stream", {"stream": {}})

    stream = create_stream(hass, STREAM_SOURCE)
    stream_worker_sync.pause()
    hls = stream.add_provider(HLS_PROVIDER)

    hls.put(Segment(1, INIT_BYTES, MOOF_BYTES, DURATION))
    await hass.async_block_till_done()

    hls_client = await hls_stream(stream)

    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 200
    assert await resp.text() == make_playlist(sequence=1, segments=[make_segment(1)])

    hls.put(Segment(2, INIT_BYTES, MOOF_BYTES, DURATION))
    await hass.async_block_till_done()
    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 200
    assert await resp.text() == make_playlist(
        sequence=1, segments=[make_segment(1), make_segment(2)]
    )

    stream_worker_sync.resume()
    stream.stop()


async def test_hls_max_segments(hass, hls_stream, stream_worker_sync):
    """Test rendering the hls playlist with more segments than the segment deque can hold."""
    await async_setup_component(hass, "stream", {"stream": {}})

    stream = create_stream(hass, STREAM_SOURCE)
    stream_worker_sync.pause()
    hls = stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    # Produce enough segments to overfill the output buffer by one
    for sequence in range(1, MAX_SEGMENTS + 2):
        hls.put(Segment(sequence, INIT_BYTES, MOOF_BYTES, DURATION))
        await hass.async_block_till_done()

    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 200

    # Only NUM_PLAYLIST_SEGMENTS are returned in the playlist.
    start = MAX_SEGMENTS + 2 - NUM_PLAYLIST_SEGMENTS
    segments = []
    for sequence in range(start, MAX_SEGMENTS + 2):
        segments.append(make_segment(sequence))
    assert await resp.text() == make_playlist(
        sequence=start,
        segments=segments,
    )

    # The segment that fell off the buffer is not accessible
    segment_response = await hls_client.get("/segment/1.m4s")
    assert segment_response.status == 404

    # However all segments in the buffer are accessible, even those that were not in the playlist.
    for sequence in range(2, MAX_SEGMENTS + 2):
        segment_response = await hls_client.get(f"/segment/{sequence}.m4s")
        assert segment_response.status == 200

    stream_worker_sync.resume()
    stream.stop()


async def test_hls_playlist_view_discontinuity(hass, hls_stream, stream_worker_sync):
    """Test a discontinuity across segments in the stream with 3 segments."""
    await async_setup_component(hass, "stream", {"stream": {}})

    stream = create_stream(hass, STREAM_SOURCE)
    stream_worker_sync.pause()
    hls = stream.add_provider(HLS_PROVIDER)

    hls.put(Segment(1, INIT_BYTES, MOOF_BYTES, DURATION, stream_id=0))
    hls.put(Segment(2, INIT_BYTES, MOOF_BYTES, DURATION, stream_id=0))
    hls.put(Segment(3, INIT_BYTES, MOOF_BYTES, DURATION, stream_id=1))
    await hass.async_block_till_done()

    hls_client = await hls_stream(stream)

    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 200
    assert await resp.text() == make_playlist(
        sequence=1,
        segments=[
            make_segment(1),
            make_segment(2),
            make_segment(3, discontinuity=True),
        ],
    )

    stream_worker_sync.resume()
    stream.stop()


async def test_hls_max_segments_discontinuity(hass, hls_stream, stream_worker_sync):
    """Test a discontinuity with more segments than the segment deque can hold."""
    await async_setup_component(hass, "stream", {"stream": {}})

    stream = create_stream(hass, STREAM_SOURCE)
    stream_worker_sync.pause()
    hls = stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    hls.put(Segment(1, INIT_BYTES, MOOF_BYTES, DURATION, stream_id=0))

    # Produce enough segments to overfill the output buffer by one
    for sequence in range(1, MAX_SEGMENTS + 2):
        hls.put(Segment(sequence, INIT_BYTES, MOOF_BYTES, DURATION, stream_id=1))
    await hass.async_block_till_done()

    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 200

    # Only NUM_PLAYLIST_SEGMENTS are returned in the playlist causing the
    # EXT-X-DISCONTINUITY tag to be omitted and EXT-X-DISCONTINUITY-SEQUENCE
    # returned instead.
    start = MAX_SEGMENTS + 2 - NUM_PLAYLIST_SEGMENTS
    segments = []
    for sequence in range(start, MAX_SEGMENTS + 2):
        segments.append(make_segment(sequence))
    assert await resp.text() == make_playlist(
        sequence=start,
        discontinuity_sequence=1,
        segments=segments,
    )

    stream_worker_sync.resume()
    stream.stop()
