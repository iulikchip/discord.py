# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-2016 Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import sys
import websockets
import asyncio
import aiohttp
from . import utils, endpoints, compat
from .enums import Status
from .game import Game
from .errors import GatewayNotFound, ConnectionClosed, InvalidArgument
import logging
import zlib, time, json
from collections import namedtuple
import threading
import struct

log = logging.getLogger(__name__)

__all__ = [ 'ReconnectWebSocket', 'get_gateway', 'DiscordWebSocket',
            'KeepAliveHandler', 'VoiceKeepAliveHandler',
            'DiscordVoiceWebSocket' ]

class ReconnectWebSocket(Exception):
    """Signals to handle the RECONNECT opcode."""
    pass

EventListener = namedtuple('EventListener', 'predicate event result future')

class KeepAliveHandler(threading.Thread):
    def __init__(self, *args, **kwargs):
        ws = kwargs.pop('ws', None)
        interval = kwargs.pop('interval', None)
        threading.Thread.__init__(self, *args, **kwargs)
        self.ws = ws
        self.interval = interval
        self.daemon = True
        self.msg = 'Keeping websocket alive with sequence {0[d]}'
        self._stop_ev = threading.Event()

    def run(self):
        while not self._stop_ev.wait(self.interval):
            data = self.get_payload()
            log.debug(self.msg.format(data))
            coro = self.ws.send_as_json(data)
            f = compat.run_coroutine_threadsafe(coro, loop=self.ws.loop)
            try:
                # block until sending is complete
                f.result()
            except Exception:
                self.stop()

    def get_payload(self):
        return {
            'op': self.ws.HEARTBEAT,
            'd': self.ws._connection.sequence
        }

    def stop(self):
        self._stop_ev.set()

class VoiceKeepAliveHandler(KeepAliveHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.msg = 'Keeping voice websocket alive with timestamp {0[d]}'

    def get_payload(self):
        return {
            'op': self.ws.HEARTBEAT,
            'd': int(time.time() * 1000)
        }


@asyncio.coroutine
def get_gateway(token, *, loop=None):
    """Returns the gateway URL for connecting to the WebSocket.

    Parameters
    -----------
    token : str
        The discord authentication token.
    loop
        The event loop.

    Raises
    ------
    GatewayNotFound
        When the gateway is not returned gracefully.
    """
    headers = {
        'authorization': token,
        'content-type': 'application/json'
    }

    with aiohttp.ClientSession(loop=loop) as session:
        resp = yield from session.get(endpoints.GATEWAY, headers=headers)
        if resp.status != 200:
            yield from resp.release()
            raise GatewayNotFound()
        data = yield from resp.json(encoding='utf-8')
        return data.get('url')

class DiscordWebSocket(websockets.client.WebSocketClientProtocol):
    """Implements a WebSocket for Discord's gateway v4.

    This is created through :func:`create_main_websocket`. Library
    users should never create this manually.

    Attributes
    -----------
    DISPATCH
        Receive only. Denotes an event to be sent to Discord, such as READY.
    HEARTBEAT
        When received tells Discord to keep the connection alive.
        When sent asks if your connection is currently alive.
    IDENTIFY
        Send only. Starts a new session.
    PRESENCE
        Send only. Updates your presence.
    VOICE_STATE
        Send only. Starts a new connection to a voice server.
    VOICE_PING
        Send only. Checks ping time to a voice server, do not use.
    RESUME
        Send only. Resumes an existing connection.
    RECONNECT
        Receive only. Tells the client to reconnect to a new gateway.
    REQUEST_MEMBERS
        Send only. Asks for the full member list of a server.
    INVALIDATE_SESSION
        Receive only. Tells the client to invalidate the session and IDENTIFY
        again.
    gateway
        The gateway we are currently connected to.
    token
        The authentication token for discord.
    """

    DISPATCH           = 0
    HEARTBEAT          = 1
    IDENTIFY           = 2
    PRESENCE           = 3
    VOICE_STATE        = 4
    VOICE_PING         = 5
    RESUME             = 6
    RECONNECT          = 7
    REQUEST_MEMBERS    = 8
    INVALIDATE_SESSION = 9

    def __init__(self, *args, **kwargs):
        super().__init__(*args, max_size=None, **kwargs)
        # an empty dispatcher to prevent crashes
        self._dispatch = lambda *args: None
        # generic event listeners
        self._dispatch_listeners = []
        # the keep alive
        self._keep_alive = None


    @classmethod
    @asyncio.coroutine
    def from_client(cls, client):
        """Creates a main websocket for Discord from a :class:`Client`.

        This is for internal use only.
        """
        gateway = yield from get_gateway(client.token, loop=client.loop)
        ws = yield from websockets.connect(gateway, loop=client.loop, klass=cls)

        # dynamically add attributes needed
        ws.token = client.token
        ws._connection = client.connection
        ws._dispatch = client.dispatch
        ws.gateway = gateway

        log.info('Created websocket connected to {}'.format(gateway))
        yield from ws.identify()
        log.info('sent the identify payload to create the websocket')
        return ws

    def wait_for(self, event, predicate, result=None):
        """Waits for a DISPATCH'd event that meets the predicate.

        Parameters
        -----------
        event : str
            The event name in all upper case to wait for.
        predicate
            A function that takes a data parameter to check for event
            properties. The data parameter is the 'd' key in the JSON message.
        result
            A function that takes the same data parameter and executes to send
            the result to the future. If None, returns the data.

        Returns
        --------
        asyncio.Future
            A future to wait for.
        """

        future = asyncio.Future(loop=self.loop)
        entry = EventListener(event=event, predicate=predicate, result=result, future=future)
        self._dispatch_listeners.append(entry)
        return future

    @asyncio.coroutine
    def identify(self):
        """Sends the IDENTIFY packet."""
        payload = {
            'op': self.IDENTIFY,
            'd': {
                'token': self.token,
                'properties': {
                    '$os': sys.platform,
                    '$browser': 'discord.py',
                    '$device': 'discord.py',
                    '$referrer': '',
                    '$referring_domain': ''
                },
                'compress': True,
                'large_threshold': 250,
                'v': 3
            }
        }
        yield from self.send_as_json(payload)

    @asyncio.coroutine
    def received_message(self, msg):
        self._dispatch('socket_raw_receive', msg)

        if isinstance(msg, bytes):
            msg = zlib.decompress(msg, 15, 10490000) # This is 10 MiB
            msg = msg.decode('utf-8')

        msg = json.loads(msg)
        state = self._connection

        log.debug('WebSocket Event: {}'.format(msg))
        self._dispatch('socket_response', msg)

        op = msg.get('op')
        data = msg.get('d')

        if 's' in msg:
            state.sequence = msg['s']

        if op == self.RECONNECT:
            # "reconnect" can only be handled by the Client
            # so we terminate our connection and raise an
            # internal exception signalling to reconnect.
            log.info('Receivede RECONNECT opcode.')
            yield from self.close()
            raise ReconnectWebSocket()

        if op == self.INVALIDATE_SESSION:
            state.sequence = None
            state.session_id = None
            return

        if op != self.DISPATCH:
            log.info('Unhandled op {}'.format(op))
            return

        event = msg.get('t')
        is_ready = event == 'READY'

        if is_ready:
            state.clear()
            state.sequence = msg['s']
            state.session_id = data['session_id']

        if is_ready or event == 'RESUMED':
            interval = data['heartbeat_interval'] / 1000.0
            self._keep_alive = KeepAliveHandler(ws=self, interval=interval)
            self._keep_alive.start()

        parser = 'parse_' + event.lower()

        try:
            func = getattr(self._connection, parser)
        except AttributeError:
            log.info('Unhandled event {}'.format(event))
        else:
            func(data)

        # remove the dispatched listeners
        removed = []
        for index, entry in enumerate(self._dispatch_listeners):
            if entry.event != event:
                continue

            future = entry.future
            if future.cancelled():
                removed.append(index)

            try:
                valid = entry.predicate(data)
            except Exception as e:
                future.set_exception(e)
                removed.append(index)
            else:
                if valid:
                    ret = data if entry.result is None else entry.result(data)
                    future.set_result(ret)
                    removed.append(index)

        for index in reversed(removed):
            del self._dispatch_listeners[index]

    def _can_handle_close(self, code):
        return code in (4006, 4008, 4009) or code in range(1001, 1015)

    @asyncio.coroutine
    def poll_event(self):
        """Polls for a DISPATCH event and handles the general gateway loop.

        Raises
        ------
        ConnectionClosed
            The websocket connection was terminated for unhandled reasons.
        """
        try:
            msg = yield from self.recv()
            yield from self.received_message(msg)
        except websockets.exceptions.ConnectionClosed as e:
            if self._can_handle_close(e.code):
                log.info('Websocket closed with {0.code}, attempting a reconnect.'.format(e))
                raise ReconnectWebSocket() from e
            else:
                raise ConnectionClosed(e) from e

    @asyncio.coroutine
    def send(self, data):
        self._dispatch('socket_raw_send', data)
        yield from super().send(data)

    @asyncio.coroutine
    def send_as_json(self, data):
        try:
            yield from super().send(utils.to_json(data))
        except websockets.exceptions.ConnectionClosed as e:
            if not self._can_handle_close(e.code):
                raise ConnectionClosed(e) from e

    @asyncio.coroutine
    def change_presence(self, *, game=None, idle=None):
        if game is not None and not isinstance(game, Game):
            raise InvalidArgument('game must be of Game or None')

        idle_since = None if idle == False else int(time.time() * 1000)
        sent_game = dict(game) if game else None

        payload = {
            'op': self.PRESENCE,
            'd': {
                'game': sent_game,
                'idle_since': idle_since
            }
        }

        sent = utils.to_json(payload)
        log.debug('Sending "{}" to change status'.format(sent))
        yield from self.send(sent)

        for server in self._connection.servers:
            me = server.me
            if me is None:
                continue

            me.game = game
            status = Status.idle if idle_since else Status.online
            me.status = status

    @asyncio.coroutine
    def voice_state(self, guild_id, channel_id, self_mute=False, self_deaf=False):
        payload = {
            'op': self.VOICE_STATE,
            'd': {
                'guild_id': guild_id,
                'channel_id': channel_id,
                'self_mute': self_mute,
                'self_deaf': self_deaf
            }
        }

        yield from self.send_as_json(payload)

        # we're leaving a voice channel so remove it from the client list
        if channel_id is None:
            self._connection._remove_voice_client(guild_id)

    @asyncio.coroutine
    def close(self, code=1000, reason=''):
        if self._keep_alive:
            self._keep_alive.stop()

        yield from super().close(code, reason)

class DiscordVoiceWebSocket(websockets.client.WebSocketClientProtocol):
    """Implements the websocket protocol for handling voice connections.

    Attributes
    -----------
    IDENTIFY
        Send only. Starts a new voice session.
    SELECT_PROTOCOL
        Send only. Tells discord what encryption mode and how to connect for voice.
    READY
        Receive only. Tells the websocket that the initial connection has completed.
    HEARTBEAT
        Send only. Keeps your websocket connection alive.
    SESSION_DESCRIPTION
        Receive only. Gives you the secret key required for voice.
    SPEAKING
        Send only. Notifies the client if you are currently speaking.
    """

    IDENTIFY            = 0
    SELECT_PROTOCOL     = 1
    READY               = 2
    HEARTBEAT           = 3
    SESSION_DESCRIPTION = 4
    SPEAKING            = 5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_size = None
        self._keep_alive = None

    @asyncio.coroutine
    def send_as_json(self, data):
        yield from self.send(utils.to_json(data))

    @classmethod
    @asyncio.coroutine
    def from_client(cls, client):
        """Creates a voice websocket for the :class:`VoiceClient`."""
        gateway = 'wss://' + client.endpoint
        ws = yield from websockets.connect(gateway, loop=client.loop, klass=cls)
        ws.gateway = gateway
        ws._connection = client

        identify = {
            'op': cls.IDENTIFY,
            'd': {
                'server_id': client.guild_id,
                'user_id': client.user.id,
                'session_id': client.session_id,
                'token': client.token
            }
        }

        yield from ws.send_as_json(identify)
        return ws

    @asyncio.coroutine
    def select_protocol(self, ip, port):
        payload = {
            'op': self.SELECT_PROTOCOL,
            'd': {
                'protocol': 'udp',
                'data': {
                    'address': ip,
                    'port': port,
                    'mode': 'xsalsa20_poly1305'
                }
            }
        }

        yield from self.send_as_json(payload)
        log.debug('Selected protocol as {}'.format(payload))

    @asyncio.coroutine
    def speak(self, is_speaking=True):
        payload = {
            'op': self.SPEAKING,
            'd': {
                'speaking': is_speaking,
                'delay': 0
            }
        }

        yield from self.send_as_json(payload)
        log.debug('Voice speaking now set to {}'.format(is_speaking))

    @asyncio.coroutine
    def received_message(self, msg):
        log.debug('Voice websocket frame received: {}'.format(msg))
        op = msg.get('op')
        data = msg.get('d')

        if op == self.READY:
            interval = (data['heartbeat_interval'] / 100.0) - 5
            self._keep_alive = VoiceKeepAliveHandler(ws=self, interval=interval)
            self._keep_alive.start()
            yield from self.initial_connection(data)
        elif op == self.SESSION_DESCRIPTION:
            yield from self.load_secret_key(data)

    @asyncio.coroutine
    def initial_connection(self, data):
        state = self._connection
        state.ssrc = data.get('ssrc')
        state.voice_port = data.get('port')
        packet = bytearray(70)
        struct.pack_into('>I', packet, 0, state.ssrc)
        state.socket.sendto(packet, (state.endpoint_ip, state.voice_port))
        recv = yield from self.loop.sock_recv(state.socket, 70)
        log.debug('received packet in initial_connection: {}'.format(recv))

        # the ip is ascii starting at the 4th byte and ending at the first null
        ip_start = 4
        ip_end = recv.index(0, ip_start)
        state.ip = recv[ip_start:ip_end].decode('ascii')

        # the port is a little endian unsigned short in the last two bytes
        # yes, this is different endianness from everything else
        state.port = struct.unpack_from('<H', recv, len(recv) - 2)[0]

        log.debug('detected ip: {0.ip} port: {0.port}'.format(state))
        yield from self.select_protocol(state.ip, state.port)
        log.info('selected the voice protocol for use')

    @asyncio.coroutine
    def load_secret_key(self, data):
        log.info('received secret key for voice connection')
        self._connection.secret_key = data.get('secret_key')
        yield from self.speak()

    @asyncio.coroutine
    def poll_event(self):
        try:
            msg = yield from asyncio.wait_for(self.recv(), timeout=30.0, loop=self.loop)
            yield from self.received_message(json.loads(msg))
        except websockets.exceptions.ConnectionClosed as e:
            raise ConnectionClosed(e) from e

    @asyncio.coroutine
    def close(self, code=1000, reason=''):
        if self._keep_alive:
            self._keep_alive.stop()

        yield from super().close(code, reason)


