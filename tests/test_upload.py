"""Upload protocol and connection reuse tests, using only the standard library."""

from __future__ import print_function

import base64
import io
import os
import sys
import tempfile
import threading
import unittest

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

import requests
import click
from click.testing import CliRunner

from bepasty_cli import cli


class QuietOutput(object):
    def write(self, value):
        pass

    def flush(self):
        pass


class UploadHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        self.connection.settimeout(3)

    def do_POST(self):
        payload = self.rfile.read(int(self.headers['Content-Length']))
        data = base64.b64decode(payload)
        unit, content_range = self.headers['Content-Range'].split(' ', 1)
        byte_range, total = content_range.split('/')
        begin, end = [int(value) for value in byte_range.split('-')]
        total = int(total)
        valid = (unit == 'bytes' and begin == self.server.received_size and
                 begin <= end < total and end - begin + 1 == len(data))
        complete = valid and end + 1 == total
        status_code = (201 if complete else 200) if valid else 400
        self.server.chunks.append((dict(self.headers), data))
        self.server.received_size += len(data)
        self.server.statuses.append(status_code)
        self.send_response(status_code)
        self.send_header('Content-Length', '2')
        self.send_header('Transaction-ID', 'test-transaction')
        if complete:
            self.send_header('Content-Location', '/items/test-file')
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, format, *args):
        pass


class UploadServer(HTTPServer):
    def __init__(self):
        HTTPServer.__init__(self, ('127.0.0.1', 0), UploadHandler)
        self.connections = 0
        self.chunks = []
        self.received_size = 0
        self.statuses = []

    def get_request(self):
        connection = HTTPServer.get_request(self)
        self.connections += 1
        return connection


class TrackingSession(requests.Session):
    def __init__(self, status_code=None):
        super(TrackingSession, self).__init__()
        self.status_code = status_code
        self.closed = False
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append(kwargs)
        if self.status_code is None:
            raise requests.ConnectionError('test connection failure')
        response = requests.Response()
        response.status_code = self.status_code
        response._content = b'test server failure'
        response.headers['Transaction-ID'] = 'test-transaction'
        if self.status_code == 201:
            response.headers['Content-Location'] = '/items/test-file'
        return response

    def close(self):
        self.closed = True
        super(TrackingSession, self).close()


class BoundedInput(io.BytesIO):
    def __init__(self, payload):
        io.BytesIO.__init__(self, payload)
        self.read_sizes = []

    def read(self, size=-1):
        if size <= 0 or size > 1024 * 1024:
            raise AssertionError('stdin must be read in bounded chunks')
        self.read_sizes.append(size)
        return io.BytesIO.read(self, size)


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.stdout = sys.stdout
        sys.stdout = QuietOutput()
        self.addCleanup(setattr, sys, 'stdout', self.stdout)
        self.payload = b'x' * (1024 * 1024) + b'last chunk'
        fd, self.filename = tempfile.mkstemp()
        with os.fdopen(fd, 'wb') as fileobj:
            fileobj.write(self.payload)
        self.addCleanup(os.unlink, self.filename)
        self.files = []

        def open_file(*args, **kwargs):
            fileobj = open(*args, **kwargs)
            self.files.append(fileobj)
            return fileobj

        cli.open = open_file
        self.addCleanup(delattr, cli, 'open')

    def upload(self, url):
        cli.upload('test-token', self.filename, '测试 文件.bin', url,
                   'application/octet-stream', False, ('2', 'DAYS'))

    def test_chunks_share_one_connection_and_preserve_protocol(self):
        server = UploadServer()
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            self.upload('http://127.0.0.1:%d' % server.server_port)
            self.assertTrue(self.files[0].closed)
            self.assertEqual(server.connections, 1)
            self.assertEqual(len(server.chunks), 2)
            self.assertEqual(server.statuses, [200, 201])
            self.assertEqual(b''.join(chunk for headers, chunk in server.chunks),
                             self.payload)
            first_headers = server.chunks[0][0]
            second_headers = server.chunks[1][0]
            self.assertEqual(first_headers['Content-Range'],
                             'bytes 0-1048575/%d' % len(self.payload))
            self.assertEqual(second_headers['Content-Range'],
                             'bytes 1048576-%d/%d' %
                             (len(self.payload) - 1, len(self.payload)))
            self.assertNotIn('Transaction-ID', first_headers)
            self.assertEqual(second_headers['Transaction-ID'], 'test-transaction')
            for headers, chunk in server.chunks:
                self.assertEqual(headers['Content-Type'], 'application/octet-stream')
                # http.server decodes header bytes as latin-1
                self.assertEqual(headers['Content-Filename'].encode('latin-1').decode('utf-8'),
                                 '测试 文件.bin')
                self.assertEqual(headers['Content-Length'],
                                 str(len(base64.b64encode(chunk))))
                self.assertEqual(headers['Maxlife-Value'], '2')
                self.assertEqual(headers['Maxlife-Unit'], 'DAYS')
                self.assertEqual(headers['Authorization'],
                                 'Basic ' + base64.b64encode(b'user:test-token').decode('ascii'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def use_session(self, session):
        original = cli.requests.Session
        cli.requests.Session = lambda: session
        self.addCleanup(setattr, cli.requests, 'Session', original)

    def test_session_is_closed_after_server_error(self):
        session = TrackingSession(403)
        self.use_session(session)
        with self.assertRaises(click.ClickException):
            self.upload('http://localhost')
        self.assertTrue(session.closed)
        self.assertTrue(self.files[0].closed)

    def test_http_error_has_nonzero_exit_status(self):
        session = TrackingSession(403)
        self.use_session(session)
        result = CliRunner().invoke(cli.main, ['--url', 'http://localhost',
                                              '--type', 'application/octet-stream', self.filename])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('Upload failed: HTTP 403', result.output)
        self.assertTrue(session.closed)
        self.assertTrue(self.files[0].closed)

    def test_session_is_closed_after_connection_failure(self):
        session = TrackingSession()
        self.use_session(session)
        with self.assertRaises(SystemExit) as caught:
            self.upload('http://localhost')
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(session.closed)
        self.assertTrue(self.files[0].closed)

    def test_stdin_remains_open_after_server_error(self):
        session = TrackingSession(403)
        self.use_session(session)
        stream = io.BytesIO(b'piped input')
        self.addCleanup(stream.close)
        original = cli.click.get_binary_stream
        cli.click.get_binary_stream = lambda name: stream
        self.addCleanup(setattr, cli.click, 'get_binary_stream', original)
        with self.assertRaises(click.ClickException):
            cli.upload('test-token', None, 'stdin.bin', 'http://localhost',
                       'application/octet-stream', False, ('2', 'DAYS'))
        self.assertTrue(session.closed)
        self.assertFalse(stream.closed)

    def use_stdin(self, payload):
        stream = BoundedInput(payload)
        self.addCleanup(stream.close)
        original = cli.click.get_binary_stream
        cli.click.get_binary_stream = lambda name: stream
        self.addCleanup(setattr, cli.click, 'get_binary_stream', original)
        return stream

    def check_stdin_upload(self, payload):
        stream = self.use_stdin(payload)
        server = UploadServer()
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            cli.upload('test-token', None, 'stdin.bin',
                       'http://127.0.0.1:%d' % server.server_port,
                       'application/octet-stream', False, ('2', 'DAYS'))
            self.assertFalse(stream.closed)
            self.assertEqual(server.connections, 1)
            self.assertEqual(b''.join(chunk for headers, chunk in server.chunks), payload)
            self.assertEqual(server.statuses,
                             [200] * (len(server.chunks) - 1) + [201])
            offset = 0
            for index, (headers, chunk) in enumerate(server.chunks):
                offset += len(chunk)
                total = len(payload) if index == len(server.chunks) - 1 else offset + 1
                self.assertEqual(headers['Content-Range'],
                                 'bytes %d-%d/%d' % (offset - len(chunk), offset - 1, total))
                self.assertLessEqual(len(chunk), 1024 * 1024)
            self.assertEqual(stream.read_sizes.count(1), len(server.chunks))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_stdin_one_full_chunk_completes(self):
        self.check_stdin_upload(b'x' * (1024 * 1024))

    def test_stdin_two_full_chunks_complete(self):
        self.check_stdin_upload(b'x' * (1024 * 1024) + b'y' * (1024 * 1024))

    def test_stdin_full_chunk_and_tail_complete(self):
        self.check_stdin_upload(b'x' * (1024 * 1024) + b'distinct tail')

    def test_empty_stdin_exits_with_error_without_request(self):
        stream = self.use_stdin(b'')
        session = TrackingSession(201)
        self.use_session(session)
        result = CliRunner().invoke(cli.main,
                                    ['--url', 'http://localhost', '--type',
                                     'application/octet-stream'], input=b'')
        self.assertEqual(result.exit_code, 1)
        self.assertIn('Cannot upload empty input', result.output)
        self.assertFalse(stream.closed)
        self.assertEqual(session.posts, [])

    def check_mime_result(self, mime_result, expected_type):
        self.use_stdin(b'text input')
        session = TrackingSession(201)
        self.use_session(session)

        class FixedMagic(object):
            def from_buffer(self, data):
                return mime_result

        original = cli.magic.Magic
        cli.magic.Magic = lambda **kwargs: FixedMagic()
        self.addCleanup(setattr, cli.magic, 'Magic', original)
        cli.upload('test-token', None, 'stdin.txt', 'http://localhost',
                   None, False, ('2', 'DAYS'))
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]['headers']['Content-Type'], expected_type)

    def test_mime_bytes_result(self):
        self.check_mime_result(b'text/plain', 'text/plain')

    def test_mime_text_result(self):
        self.check_mime_result(u'text/plain', 'text/plain')

    def test_empty_mime_result_falls_back_to_text(self):
        self.check_mime_result(None, 'text/plain')


if __name__ == '__main__':
    unittest.main()
