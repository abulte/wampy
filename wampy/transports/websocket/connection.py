import logging
import socket
import ssl
import uuid
from base64 import encodestring
from socket import error as socket_error

import eventlet

from wampy.constants import WEBSOCKET_SUBPROTOCOLS, WEBSOCKET_VERSION
from wampy.errors import (
    IncompleteFrameError, ConnectionError, WampProtocolError, WampyError)
from wampy.mixins import ParseUrlMixin

from . frames import ClientFrame, ServerFrame

logger = logging.getLogger(__name__)


class WampWebSocket(ParseUrlMixin):

    def __init__(self, router):
        self.url = router.url

        self.host = None
        self.port = None
        self.ipv = router.ipv
        self.resource = None

        self.parse_url()
        self.websocket_location = self.resource
        self.key = encodestring(uuid.uuid4().bytes).decode('utf-8').strip()
        self.socket = None

    def _connect(self):
        if self.ipv == 4:
            _socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            try:
                _socket.connect((self.host.encode(), self.port))
            except socket_error as exc:
                if exc.errno == 61:
                    logger.error(
                        'unable to connect to %s:%s (IPV%s)',
                        self.host, self.port, self.ipv
                    )

                raise

        elif self.ipv == 6:
            _socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)

            try:
                _socket.connect(("::", self.port))
            except socket_error as exc:
                if exc.errno == 61:
                    logger.error(
                        'unable to connect to %s:%s (IPV%s)',
                        self.host, self.port, self.ipv
                    )

                raise

        else:
            raise WampyError(
                "unknown IPV: {}".format(self.ipv)
            )

        self.socket = _socket
        logger.debug("socket connected")

    def _upgrade(self):
        handshake_headers = self._get_handshake_headers()
        handshake = '\r\n'.join(handshake_headers) + "\r\n\r\n"

        self.socket.send(handshake.encode())

        try:
            with eventlet.Timeout(5):
                self.status, self.headers = self._read_handshake_response()
        except eventlet.Timeout:
            raise WampyError(
                'No response after handshake "{}"'.format(handshake)
            )

        logger.debug("connection upgraded")

    def _get_handshake_headers(self):
        """ Do an HTTP upgrade handshake with the server.

        Websockets upgrade from HTTP rather than TCP largely because it was
        assumed that servers which provide websockets will always be talking to
        a browser. Maybe a reasonable assumption once upon a time...

        The headers here will go a little further and also agree the
        WAMP websocket JSON subprotocols.

        """
        headers = []
        # https://tools.ietf.org/html/rfc6455
        headers.append("GET /{} HTTP/1.1".format(self.websocket_location))
        headers.append("Host: {}:{}".format(self.host, self.port))
        headers.append("Upgrade: websocket")
        headers.append("Connection: Upgrade")
        # Sec-WebSocket-Key header containing base64-encoded random bytes,
        # and the server replies with a hash of the key in the
        # Sec-WebSocket-Accept header. This is intended to prevent a caching
        # proxy from re-sending a previous WebSocket conversation and does not
        # provide any authentication, privacy or integrity
        headers.append("Sec-WebSocket-Key: {}".format(self.key))
        headers.append("Origin: ws://{}:{}".format(self.host, self.port))
        headers.append("Sec-WebSocket-Version: {}".format(WEBSOCKET_VERSION))
        headers.append("Sec-WebSocket-Protocol: {}".format(
            WEBSOCKET_SUBPROTOCOLS))

        logger.debug("connection headers: %s", headers)

        return headers

    def _read_handshake_response(self):
        # each header ends with \r\n and there's an extra \r\n after the last
        # one
        status = None
        headers = {}

        def read_line():
            bytes_cache = []
            received_bytes = None
            while received_bytes != b'\r\n':
                received_bytes = self.socket.recv(2)
                bytes_cache.append(received_bytes)
            return b''.join(bytes_cache)

        while True:
            # we need this to guarantee we can context switch back to the
            # Timeout.
            eventlet.sleep()

            received_bytes = read_line()
            if received_bytes == b'\r\n':
                # end of the response
                break

            bytes_as_str = received_bytes.decode()
            line = bytes_as_str.strip()

            if not status:
                status_info = line.split(" ", 2)
                try:
                    status = int(status_info[1])
                except IndexError:
                    logger.warning('unexpected handshake resposne')
                    logger.error('%s', status_info)
                    raise

                headers['status_info'] = status_info
                headers['status'] = status
                continue

            kv = line.split(":", 1)
            if len(kv) != 2:
                raise Exception(
                    'Invalid header: "{}"'.format(line)
                )

            key, value = kv
            headers[key.lower()] = value.strip().lower()

        logger.info("handshake complete: %s : %s", status, headers)

        return status, headers

    def connect(self):
        self._connect()
        self._upgrade()

    def read_websocket_frame(self, bufsize=1):
        frame = None
        received_bytes = bytearray()

        while True:
            logger.debug("waiting for %s bytes", bufsize)

            try:
                bytes = self.socket.recv(bufsize)
            except eventlet.greenlet.GreenletExit as exc:
                raise ConnectionError('Connection closed: "{}"'.format(exc))
            except socket.timeout as e:
                message = str(e)
                raise ConnectionError('timeout: "{}"'.format(message))
            except Exception as exc:
                raise ConnectionError('error: "{}"'.format(exc))

            if not bytes:
                break

            logger.debug("received %s bytes", bufsize)
            received_bytes.extend(bytes)

            try:
                frame = ServerFrame(received_bytes)
            except IncompleteFrameError as exc:
                bufsize = exc.required_bytes
            else:
                break

        if frame is None:
            raise WampProtocolError("No frame returned")

        return frame

    def send_websocket_frame(self, message):
        frame = ClientFrame(message)
        self.socket.sendall(frame.payload)


class TLSWampWebSocket(WampWebSocket):
    def __init__(self, router):
        super(TLSWampWebSocket, self).__init__(router)

        self.ipv = router.ipv

        # PROTOCOL_TLSv1_1 and PROTOCOL_TLSv1_2 are only available if Python is
        # linked with OpenSSL 1.0.1 or later.
        try:
            self.ssl_version = ssl.PROTOCOL_TLSv1_2
        except AttributeError:
            raise WampyError("Your Python Environment does not support TLS")

        self.certificate = router.certificate

    def _connect(self):
        _socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        wrapped_socket = ssl.wrap_socket(
            _socket,
            ssl_version=self.ssl_version,
            ciphers="ECDH+AESGCM:DH+AESGCM:ECDH+AES256:DH+AES256:ECDH+AES128:\
            DH+AES:ECDH+3DES:DH+3DES:RSA+AES:RSA+3DES:!ADH:!AECDH:!MD5:!DSS",
            cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=self.certificate,
        )

        try:
            wrapped_socket.connect((self.host, self.port))
        except socket_error as exc:
            if exc.errno == 61:
                logger.error(
                    'unable to connect to %s:%s', self.host, self.port
                )

            raise

        self.socket = wrapped_socket
