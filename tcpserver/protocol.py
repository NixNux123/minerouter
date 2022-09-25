import uuid
import selectors
import socket
import time
from types import SimpleNamespace
from typing import Tuple

from protocol import State, VarInt, HandshakePacket
from utils.config import Config
from utils.logs import get_logger

log = get_logger(__name__)


class Messenger:
    sock: socket.socket
    addr: Tuple[str, int]
    selector: selectors.DefaultSelector

    _recv_buffer: bytes = b""
    _send_buffer: bytes = b""

    packet_length: int | None = None
    packet_read_complete: bool = False

    use_complete_packets: bool = False

    def __init__(self, sock: socket.socket, selector: selectors.DefaultSelector, addr: Tuple[str, int]):
        self._last_read = time.time()

        self.sock = sock
        self.selector = selector
        self.addr = addr

    def _read(self):
        try:
            # Should be ready to read
            data = self.sock.recv(4096)
        except BlockingIOError:
            # Resource temporarily unavailable
            pass
        # except OSError:
        #     # Resource temporarily unavailable
        #     pass
        else:
            if data:
                self._recv_buffer += data
            else:
                raise RuntimeError("Peer closed.")

    def _write(self):
        if self._send_buffer:
            try:
                # Should be ready to write
                bytes_sent = self.sock.send(self._send_buffer)
            except BlockingIOError:
                # Resource temporarily unavailable
                pass
            # except OSError:
            #     # Resource temporarily unavailable
            #     pass
            else:
                self._send_buffer = self._send_buffer[bytes_sent:]

    def _read_header(self):
        if len(self._recv_buffer) > 0:
            try:
                value, n = VarInt.read(self._recv_buffer)
            except ValueError:
                return
            else:
                self.packet_length = value + n

    def _check_read_complete(self):
        if len(self._recv_buffer) >= self.packet_length:
            return True
        else:
            return False

    def read(self):
        self._read()

        if self.packet_read_complete:# or not self.use_complete_packets:
            return
        if self.packet_length is None:
            self._read_header()
        if self.packet_length is not None:
            # print(f"{self.packet_length=}")
            self.packet_read_complete = self._check_read_complete()

    def write(self):
        self._write()

    def read_packet(self) -> bytes | None:
        if self.packet_read_complete:
            packet = self._recv_buffer[:self.packet_length]
            self._recv_buffer = self._recv_buffer[self.packet_length:]
            self.packet_length = None
            self.packet_read_complete = False
            return packet
        return None

    def write_packet(self, data: bytes):
        self._send_buffer += data

    def process_events(self, mask):
        if mask & selectors.EVENT_READ:
            self.read()
        if mask & selectors.EVENT_WRITE:
            self.write()

    def close(self):
        self.selector.unregister(self.sock)
        if self.sock:
            self.sock.close()


class CompressedMessenger(Messenger):
    pass


class Protocol:
    id: uuid.UUID
    isCompressed: bool = False
    state: State = State.HANDSHAKING

    client_messenger: Messenger
    server_messenger: Messenger

    selector: selectors.DefaultSelector

    use_read_packets: bool = False

    def __init__(self, client: socket.socket, addr: Tuple[str, int], selector: selectors.DefaultSelector):
        self.id = uuid.uuid1()
        log.debug(f"New Protocol Bridge: {self.id} for {addr=}")
        self.selector = selector
        self.client_messenger = Messenger(sock=client, addr=addr, selector=selector)

        self.set_packet_config()

    def process_client_events(self, mask):
        try:
            self.client_messenger.process_events(mask)
        except RuntimeError as e:
            if str(e) == "Peer closed.":
                self.close()
                return
            raise

    def process_server_events(self, mask):
        try:
            self.server_messenger.process_events(mask)
        except RuntimeError as e:
            if str(e) == "Peer closed.":
                self.close()
                return
            raise

    def process_protocol(self):
        match self.state:
            case State.HANDSHAKING:
                self.handle_handshake()
            case _:
                self.handle_passthrough()

    def handle_handshake(self):
        packet = self.client_messenger.read_packet()
        if packet is None:
            return

        decoded_packet = HandshakePacket(packet)
        log.debug(f"{self.id}: {decoded_packet}")

        self.create_server_connection(host=decoded_packet.server_addr)
        self.set_packet_config()
        self.pipe_to_server(packet)

        self.state = decoded_packet.next_state

    def create_server_connection(self, host: str):
        addr = Config.get_addr(host, None)
        if addr is None:
            raise ValueError(f"{host} is not configured")

        log.debug(f"{self.id}: Creating Upstream Connection to {addr=} for {host=}")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        code = sock.connect_ex(addr)

        data = SimpleNamespace(target="server", proto=self)
        events = selectors.EVENT_READ | selectors.EVENT_WRITE
        self.selector.register(fileobj=sock, events=events, data=data)

        self.server_messenger = Messenger(sock=sock, addr=addr, selector=self.selector)

    def pipe_to_client(self, data: bytes):
        self.client_messenger.write_packet(data)

    def pipe_to_server(self, data: bytes):
        self.server_messenger.write_packet(data)

    def handle_passthrough(self):
        ...

    def _passthrough_direct(self):
        if self.client_messenger._recv_buffer:
            self.pipe_to_server(self.client_messenger._recv_buffer)
            self.client_messenger._recv_buffer = b""

        if self.server_messenger._recv_buffer:
            self.pipe_to_client(self.server_messenger._recv_buffer)
            self.server_messenger._recv_buffer = b""

    def _passthrough_complete_packets(self):
        client_packet = self.client_messenger.read_packet()
        if client_packet is not None:
            self.pipe_to_server(client_packet)

        server_packet = self.server_messenger.read_packet()
        if server_packet is not None:
            self.pipe_to_client(server_packet)

    def set_packet_config(self):
        self.use_read_packets = Config.get_system_conf("COMPLETE_PACKETS", False)
        if self.use_read_packets:
            self.handle_passthrough = self._passthrough_complete_packets
        else:
            self.handle_passthrough = self._passthrough_direct

        if self.state is not State.HANDSHAKING:
            self.client_messenger.use_complete_packets = self.use_read_packets
            self.server_messenger.use_complete_packets = self.use_read_packets

    def close(self):
        log.debug(f"{self.id}: Closing Protocol Bridge")
        self.client_messenger.close()
        if hasattr(self, "server_messenger"):
            self.server_messenger.close()
