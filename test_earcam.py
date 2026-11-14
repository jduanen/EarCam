import contextlib
import io
import queue
import socket
import struct
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from earcam import CHUNK_HDR_FMT, JpgFrame, SoulearClient, _parse_device_info


class FrameTests(unittest.TestCase):
    def test_out_of_order_chunks_across_counter_rollover(self):
        frame = JpgFrame(42)
        frame.add_chunk(0, b'end\xff\xd9', 3)
        frame.add_chunk(255, b'middle', 3)
        frame.add_chunk(254, b'\xff\xd8start', 3)
        self.assertTrue(frame.complete)
        self.assertEqual(frame.data, b'\xff\xd8startmiddleend\xff\xd9')

    def test_lost_chunk_is_not_complete(self):
        frame = JpgFrame(42)
        frame.add_chunk(254, b'\xff\xd8start', 3)
        frame.add_chunk(0, b'end\xff\xd9', 3)
        frame.add_chunk(0, b'end\xff\xd9', 3)
        self.assertFalse(frame.complete)
        self.assertEqual(frame.data, b'')

    def test_noncontiguous_chunk_ids_are_not_complete(self):
        frame = JpgFrame(42)
        frame.add_chunk(10, b'\xff\xd8start', 3)
        frame.add_chunk(12, b'middle', 3)
        frame.add_chunk(13, b'end\xff\xd9', 3)
        self.assertFalse(frame.complete)

    def test_total_on_final_chunk_only(self):
        frame = JpgFrame(42)
        frame.add_chunk(10, b'\xff\xd8start', 0)
        frame.add_chunk(11, b'middle', 0)
        frame.add_chunk(12, b'end\xff\xd9', 3)
        self.assertTrue(frame.complete)

    def test_real_jpeg_decodes_after_shuffled_packets(self):
        image = np.full((480, 480, 3), 123, np.uint8)
        ok, encoded = cv2.imencode('.jpg', image)
        self.assertTrue(ok)
        jpeg = encoded.tobytes()
        chunks = [jpeg[i:i + 1280] for i in range(0, len(jpeg), 1280)]
        frame = JpgFrame(42)
        for i in reversed(range(len(chunks))):
            frame.add_chunk((254 + i) % 256, chunks[i], len(chunks))
        decoded = cv2.imdecode(np.frombuffer(frame.data, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, image.shape)
        np.testing.assert_array_equal(decoded, image)

    def test_device_strings_stop_at_first_nul(self):
        payload = bytearray(128)
        payload[1:10] = b'YPC\x00noise'
        self.assertEqual(_parse_device_info(payload)['vendor'], 'YPC')


class StreamTests(unittest.TestCase):
    def test_queue_replaces_stale_frame(self):
        client = SoulearClient()
        client._publish_frame(JpgFrame(1))
        newest = JpgFrame(2)
        client._publish_frame(newest)
        self.assertIs(client._frame_queue.get_nowait(), newest)
        self.assertEqual(client.stats['queue_drops'], 1)

    def test_receive_socket_is_bound_before_stream_request(self):
        client = SoulearClient()
        receiver, command = MagicMock(), MagicMock()

        def check_bound(*args, **kwargs):
            receiver.bind.assert_called_once_with(('0.0.0.0', 22789))
            return {'err_code': 0}

        with patch('earcam.socket.socket', side_effect=[receiver, command]), \
                patch.object(client, '_send_cmd', side_effect=check_bound):
            client.open_stream()
        receiver.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                                   4 * 1024 * 1024)
        command.close.assert_called_once()

    def test_stream_request_failure_closes_receive_socket(self):
        client = SoulearClient()
        receiver, command = MagicMock(), MagicMock()
        with patch('earcam.socket.socket', side_effect=[receiver, command]), \
                patch.object(client, '_send_cmd', side_effect=socket.timeout):
            with self.assertRaises(socket.timeout):
                client.open_stream()
        receiver.close.assert_called_once()
        command.close.assert_called_once()
        self.assertIsNone(client._stream_sock)

    def test_bind_failure_closes_receive_socket(self):
        client = SoulearClient()
        receiver = MagicMock()
        receiver.bind.side_effect = OSError('Address already in use')
        with patch('earcam.socket.socket', return_value=receiver), \
                patch.object(client, '_send_cmd') as send_command:
            with self.assertRaises(OSError):
                client.open_stream()
        receiver.close.assert_called_once()
        send_command.assert_not_called()
        self.assertIsNone(client._stream_sock)

    def test_rejected_stream_request_closes_both_sockets(self):
        client = SoulearClient()
        receiver, command = MagicMock(), MagicMock()
        with patch('earcam.socket.socket', side_effect=[receiver, command]), \
                patch.object(client, '_send_cmd', return_value={'err_code': 2}):
            with self.assertRaisesRegex(OSError, 'OpenVideo command failed'):
                client.open_stream()
        receiver.close.assert_called_once()
        command.close.assert_called_once()
        self.assertIsNone(client._stream_sock)

    def test_receiver_accepts_large_packets_and_filters_other_senders(self):
        client = SoulearClient()
        client._stream_sock = MagicMock()
        header = struct.pack(CHUNK_HDR_FMT, 1, 20, 1, 1, 1, 0, 0, 0, 0, 480, 480)
        jpeg = b'\xff\xd8' + b'x' * 3000 + b'\xff\xd9'
        client._stream_sock.recvfrom.side_effect = [
            (header + jpeg, ('192.168.0.1', 10006)),
            (header + jpeg, ('192.168.1.1', 10006)),
            OSError(),
        ]
        client._recv_loop()
        self.assertEqual(client.stats['packets'], 1)
        self.assertEqual(client._frame_queue.get_nowait().data, jpeg)

    def test_expired_incomplete_frame_is_counted(self):
        client = SoulearClient()
        client._stream_sock = MagicMock()
        partial = struct.pack(CHUNK_HDR_FMT, 1, 1, 1, 0, 2, 0, 0, 0, 0, 480, 480)
        complete = struct.pack(CHUNK_HDR_FMT, 1, 2, 2, 1, 1, 0, 0, 0, 0, 480, 480)
        client._stream_sock.recvfrom.side_effect = [
            (partial + b'\xff\xd8part', ('192.168.1.1', 10006)),
            (complete + b'\xff\xd8whole\xff\xd9', ('192.168.1.1', 10006)),
            OSError(),
        ]
        with patch('earcam.time.monotonic', side_effect=[0.0, 0.0, 1.0, 1.0]):
            client._recv_loop()
        self.assertEqual(client.stats['incomplete'], 1)
        self.assertEqual(client.stats['frames'], 1)

    def test_quit_still_works_when_no_video_arrives(self):
        client = SoulearClient()
        with patch.object(client, 'get_device_info', return_value={}), \
                patch.object(client, 'open_stream'), \
                patch.object(client, 'set_led'), \
                patch('earcam.threading.Thread'), \
                patch('earcam.cv2.namedWindow'), \
                patch('earcam.cv2.imshow'), \
                patch('earcam.cv2.destroyAllWindows'), \
                patch.object(client._frame_queue, 'get', side_effect=[
                    queue.Empty, AssertionError('GUI events were not processed')
                ]), \
                patch('earcam.cv2.waitKey', return_value=ord('q')) as wait_key, \
                contextlib.redirect_stdout(io.StringIO()):
            client.run()
        wait_key.assert_called_once()
        self.assertTrue(client._stop.is_set())

    def test_quit_still_works_after_invalid_jpeg(self):
        client = SoulearClient()
        frame = JpgFrame(1)
        frame.add_chunk(1, b'\xff\xd8invalid\xff\xd9', 1)
        with patch.object(client, 'get_device_info', return_value={}), \
                patch.object(client, 'open_stream'), \
                patch.object(client, 'set_led'), \
                patch('earcam.threading.Thread'), \
                patch('earcam.cv2.namedWindow'), \
                patch('earcam.cv2.imshow'), \
                patch('earcam.cv2.destroyAllWindows'), \
                patch('earcam.cv2.imdecode', return_value=None), \
                patch.object(client._frame_queue, 'get', side_effect=[
                    frame, AssertionError('GUI events were not processed')
                ]), \
                patch('earcam.cv2.waitKey', return_value=ord('q')) as wait_key, \
                contextlib.redirect_stdout(io.StringIO()):
            client.run()
        wait_key.assert_called_once()
        self.assertEqual(client.stats['decode_errors'], 1)
        self.assertTrue(client._stop.is_set())

    def test_window_close_still_works_when_no_video_arrives(self):
        client = SoulearClient()
        with patch.object(client, 'get_device_info', return_value={}), \
                patch.object(client, 'open_stream'), \
                patch.object(client, 'set_led'), \
                patch('earcam.threading.Thread'), \
                patch('earcam.cv2.namedWindow'), \
                patch('earcam.cv2.imshow'), \
                patch('earcam.cv2.destroyAllWindows'), \
                patch.object(client._frame_queue, 'get', side_effect=[
                    queue.Empty, AssertionError('GUI events were not processed')
                ]), \
                patch('earcam.cv2.waitKey', return_value=-1), \
                patch('earcam.cv2.getWindowProperty', return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            client.run()
        self.assertTrue(client._stop.is_set())


if __name__ == '__main__':
    unittest.main()
