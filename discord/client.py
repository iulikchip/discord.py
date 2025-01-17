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

from . import __version__ as library_version
from . import endpoints
from .user import User
from .member import Member
from .channel import Channel, PrivateChannel
from .server import Server
from .message import Message
from .invite import Invite
from .object import Object
from .role import Role
from .errors import *
from .state import ConnectionState
from .permissions import Permissions
from . import utils, compat
from .enums import ChannelType, ServerRegion
from .voice_client import VoiceClient
from .iterators import LogsFromIterator
from .gateway import *

import asyncio
import aiohttp
import websockets

import logging, traceback
import sys, re
import tempfile, os, hashlib
import itertools
import datetime
from random import randint as random_integer

PY35 = sys.version_info >= (3, 5)
log = logging.getLogger(__name__)
request_logging_format = '{method} {response.url} has returned {response.status}'
request_success_log = '{response.url} with {json} received {data}'

class Client:
    """Represents a client connection that connects to Discord.
    This class is used to interact with the Discord WebSocket and API.

    A number of options can be passed to the :class:`Client`.

    .. _deque: https://docs.python.org/3.4/library/collections.html#collections.deque
    .. _event loop: https://docs.python.org/3/library/asyncio-eventloops.html
    .. _connector: http://aiohttp.readthedocs.org/en/stable/client_reference.html#connectors
    .. _ProxyConnector: http://aiohttp.readthedocs.org/en/stable/client_reference.html#proxyconnector

    Parameters
    ----------
    max_messages : Optional[int]
        The maximum number of messages to store in :attr:`messages`.
        This defaults to 5000. Passing in `None` or a value less than 100
        will use the default instead of the passed in value.
    loop : Optional[event loop].
        The `event loop`_ to use for asynchronous operations. Defaults to ``None``,
        in which case the default event loop is used via ``asyncio.get_event_loop()``.
    cache_auth : Optional[bool]
        Indicates if :meth:`login` should cache the authentication tokens. Defaults
        to ``True``. The method in which the cache is written is done by writing to
        disk to a temporary directory.
    connector : aiohttp.BaseConnector
        The `connector`_ to use for connection pooling. Useful for proxies, e.g.
        with a `ProxyConnector`_.

    Attributes
    -----------
    user : Optional[:class:`User`]
        Represents the connected client. None if not logged in.
    voice_clients : iterable of :class:`VoiceClient`
        Represents a list of voice connections. To connect to voice use
        :meth:`join_voice_channel`. To query the voice connection state use
        :meth:`is_voice_connected`.
    servers : iterable of :class:`Server`
        The servers that the connected client is a member of.
    private_channels : iterable of :class:`PrivateChannel`
        The private channels that the connected client is participating on.
    messages
        A deque_ of :class:`Message` that the client has received from all
        servers and private messages. The number of messages stored in this
        deque is controlled by the ``max_messages`` parameter.
    email
        The email used to login. This is only set if login is successful,
        otherwise it's None.
    gateway
        The websocket gateway the client is currently connected to. Could be None.
    loop
        The `event loop`_ that the client uses for HTTP requests and websocket operations.

    """
    def __init__(self, *, loop=None, **options):
        self.ws = None
        self.token = None
        self.loop = asyncio.get_event_loop() if loop is None else loop
        self._listeners = []
        self.cache_auth = options.get('cache_auth', True)

        max_messages = options.get('max_messages')
        if max_messages is None or max_messages < 100:
            max_messages = 5000

        self.connection = ConnectionState(self.dispatch, self.request_offline_members, max_messages, loop=self.loop)

        # Blame Jake for this
        user_agent = 'DiscordBot (https://github.com/Rapptz/discord.py {0}) Python/{1[0]}.{1[1]} aiohttp/{2}'

        self.headers = {
            'content-type': 'application/json',
            'user-agent': user_agent.format(library_version, sys.version_info, aiohttp.__version__)
        }

        connector = options.pop('connector', None)
        self.session = aiohttp.ClientSession(loop=self.loop, connector=connector)

        self._closed = asyncio.Event(loop=self.loop)
        self._is_logged_in = asyncio.Event(loop=self.loop)
        self._is_ready = asyncio.Event(loop=self.loop)

    # internals

    def _get_cache_filename(self, email):
        filename = hashlib.md5(email.encode('utf-8')).hexdigest()
        return os.path.join(tempfile.gettempdir(), 'discord_py', filename)

    @asyncio.coroutine
    def _login_via_cache(self, email, password):
        try:
            log.info('attempting to login via cache')
            cache_file = self._get_cache_filename(email)
            self.email = email
            with open(cache_file, 'r') as f:
                log.info('login cache file found')
                self.token = f.read()
                self.headers['authorization'] = self.token

            # at this point our check failed
            # so we have to login and get the proper token and then
            # redo the cache
        except OSError:
            log.info('a problem occurred while opening login cache')
            pass # file not found et al

    def _update_cache(self, email, password):
        try:
            cache_file = self._get_cache_filename(email)
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with os.fdopen(os.open(cache_file, os.O_WRONLY | os.O_CREAT, 0o0600), 'w') as f:
                log.info('updating login cache')
                f.write(self.token)
        except OSError:
            log.info('a problem occurred while updating the login cache')
            pass

    def handle_message(self, message):
        removed = []
        for i, (condition, future) in enumerate(self._listeners):
            if future.cancelled():
                removed.append(i)
                continue

            try:
                result = condition(message)
            except Exception as e:
                future.set_exception(e)
                removed.append(i)
            else:
                if result:
                    future.set_result(message)
                    removed.append(i)


        for idx in reversed(removed):
            del self._listeners[idx]

    def handle_ready(self):
        self._is_ready.set()

    def _resolve_invite(self, invite):
        if isinstance(invite, Invite) or isinstance(invite, Object):
            return invite.id
        else:
            rx = r'(?:https?\:\/\/)?discord\.gg\/(.+)'
            m = re.match(rx, invite)
            if m:
                return m.group(1)
        return invite

    @asyncio.coroutine
    def _resolve_destination(self, destination):
        if isinstance(destination, (Channel, PrivateChannel, Server)):
            return destination.id
        elif isinstance(destination, User):
            found = self.connection._get_private_channel_by_user(destination.id)
            if found is None:
                # Couldn't find the user, so start a PM with them first.
                channel = yield from self.start_private_message(destination)
                return channel.id
            else:
                return found.id
        elif isinstance(destination, Object):
            return destination.id
        else:
            raise InvalidArgument('Destination must be Channel, PrivateChannel, User, or Object')

    def __getattr__(self, name):
        if name in ('user', 'servers', 'private_channels', 'messages', 'voice_clients'):
            return getattr(self.connection, name)
        else:
            msg = "'{}' object has no attribute '{}'"
            raise AttributeError(msg.format(self.__class__, name))

    def __setattr__(self, name, value):
        if name in ('user', 'servers', 'private_channels', 'messages', 'voice_clients'):
            return setattr(self.connection, name, value)
        else:
            object.__setattr__(self, name, value)

    @asyncio.coroutine
    def _run_event(self, event, *args, **kwargs):
        try:
            yield from getattr(self, event)(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                yield from self.on_error(event, *args, **kwargs)
            except asyncio.CancelledError:
                pass

    def dispatch(self, event, *args, **kwargs):
        log.debug('Dispatching event {}'.format(event))
        method = 'on_' + event
        handler = 'handle_' + event

        if hasattr(self, handler):
            getattr(self, handler)(*args, **kwargs)

        if hasattr(self, method):
            compat.create_task(self._run_event(method, *args, **kwargs), loop=self.loop)

    @asyncio.coroutine
    def on_error(self, event_method, *args, **kwargs):
        """|coro|

        The default error handler provided by the client.

        By default this prints to ``sys.stderr`` however it could be
        overridden to have a different implementation.
        Check :func:`discord.on_error` for more details.
        """
        print('Ignoring exception in {}'.format(event_method), file=sys.stderr)
        traceback.print_exc()

    # login state management

    @asyncio.coroutine
    def _login_1(self, token):
        log.info('logging in using static token')
        self.token = token
        self.email = None
        self.headers['authorization'] = 'Bot {}'.format(self.token)
        resp = yield from self.session.get(endpoints.ME, headers=self.headers)
        yield from resp.release()
        log.debug(request_logging_format.format(method='GET', response=resp))

        if resp.status != 200:
            if resp.status == 401:
                raise LoginFailure('Improper token has been passed.')
            else:
                raise HTTPException(resp, None)

        log.info('token auth returned status code {}'.format(resp.status))
        self._is_logged_in.set()

    @asyncio.coroutine
    def _login_2(self, email, password):
        # attempt to read the token from cache
        if self.cache_auth:
            yield from self._login_via_cache(email, password)
            if self.is_logged_in:
                return

        payload = {
            'email': email,
            'password': password
        }

        data = utils.to_json(payload)
        resp = yield from self.session.post(endpoints.LOGIN, data=data, headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=resp))
        if resp.status != 200:
            yield from resp.release()
            if resp.status == 400:
                raise LoginFailure('Improper credentials have been passed.')
            else:
                raise HTTPException(resp, None)

        log.info('logging in returned status code {}'.format(resp.status))
        self.email = email

        body = yield from resp.json(encoding='utf-8')
        self.token = body['token']
        self.headers['authorization'] = self.token
        self._is_logged_in.set()

        # since we went through all this trouble
        # let's make sure we don't have to do it again
        if self.cache_auth:
            self._update_cache(email, password)

    @asyncio.coroutine
    def login(self, *args):
        """|coro|

        Logs in the client with the specified credentials.

        This function can be used in two different ways.

        .. code-block:: python

            await client.login('token')

            # or

            await client.login('email', 'password')

        More than 2 parameters or less than 1 parameter raises a
        :exc:`TypeError`.

        Raises
        ------
        LoginFailure
            The wrong credentials are passed.
        HTTPException
            An unknown HTTP related error occurred,
            usually when it isn't 200 or the known incorrect credentials
            passing status code.
        TypeError
            The incorrect number of parameters is passed.
        """

        n = len(args)
        if n in (2, 1):
            yield from getattr(self, '_login_' + str(n))(*args)
        else:
            raise TypeError('login() takes 1 or 2 positional arguments but {} were given'.format(n))


    @asyncio.coroutine
    def logout(self):
        """|coro|

        Logs out of Discord and closes all connections."""
        response = yield from self.session.post(endpoints.LOGOUT, headers=self.headers)
        yield from response.release()
        yield from self.close()
        self._is_logged_in.clear()
        log.debug(request_logging_format.format(method='POST', response=response))

    @asyncio.coroutine
    def connect(self):
        """|coro|

        Creates a websocket connection and lets the websocket listen
        to messages from discord.

        Raises
        -------
        GatewayNotFound
            If the gateway to connect to discord is not found. Usually if this
            is thrown then there is a discord API outage.
        ConnectionClosed
            The websocket connection has been terminated.
        """
        self.ws = yield from DiscordWebSocket.from_client(self)

        while not self.is_closed:
            try:
                yield from self.ws.poll_event()
            except ReconnectWebSocket:
                log.info('Reconnecting the websocket.')
                self.ws = yield from DiscordWebSocket.from_client(self)
            except ConnectionClosed as e:
                yield from self.close()
                if e.code != 1000:
                    raise

    @asyncio.coroutine
    def close(self):
        """|coro|

        Closes the connection to discord.
        """
        if self.is_closed:
            return

        for voice in list(self.voice_clients):
            try:
                yield from voice.disconnect()
            except:
                # if an error happens during disconnects, disregard it.
                pass

            self.connection._remove_voice_client(voice.server.id)

        if self.ws is not None and self.ws.open:
            yield from self.ws.close()


        yield from self.session.close()
        self._closed.set()
        self._is_ready.clear()

    @asyncio.coroutine
    def start(self, *args):
        """|coro|

        A shorthand coroutine for :meth:`login` + :meth:`connect`.
        """
        yield from self.login(*args)
        yield from self.connect()

    def run(self, *args):
        """A blocking call that abstracts away the `event loop`_
        initialisation from you.

        If you want more control over the event loop then this
        function should not be used. Use :meth:`start` coroutine
        or :meth:`connect` + :meth:`login`.

        Roughly Equivalent to: ::

            try:
                loop.run_until_complete(start(*args))
            except KeyboardInterrupt:
                loop.run_until_complete(logout())
                # cancel all tasks lingering
            finally:
                loop.close()

        Warning
        --------
        This function must be the last function to call due to the fact that it
        is blocking. That means that registration of events or anything being
        called after this function call will not execute until it returns.
        """

        try:
            self.loop.run_until_complete(self.start(*args))
        except KeyboardInterrupt:
            self.loop.run_until_complete(self.logout())
            pending = asyncio.Task.all_tasks()
            gathered = asyncio.gather(*pending)
            try:
                gathered.cancel()
                self.loop.run_until_complete(gathered)

                # we want to retrieve any exceptions to make sure that
                # they don't nag us about it being un-retrieved.
                gathered.exception()
            except:
                pass
        finally:
            self.loop.close()

        # properties

    @property
    def is_logged_in(self):
        """bool: Indicates if the client has logged in successfully."""
        return self._is_logged_in.is_set()

    @property
    def is_closed(self):
        """bool: Indicates if the websocket connection is closed."""
        return self._closed.is_set()

    # helpers/getters

    def get_channel(self, id):
        """Returns a :class:`Channel` or :class:`PrivateChannel` with the following ID. If not found, returns None."""
        return self.connection.get_channel(id)

    def get_server(self, id):
        """Returns a :class:`Server` with the given ID. If not found, returns None."""
        return self.connection._get_server(id)

    def get_all_channels(self):
        """A generator that retrieves every :class:`Channel` the client can 'access'.

        This is equivalent to: ::

            for server in client.servers:
                for channel in server.channels:
                    yield channel

        Note
        -----
        Just because you receive a :class:`Channel` does not mean that
        you can communicate in said channel. :meth:`Channel.permissions_for` should
        be used for that.
        """

        for server in self.servers:
            for channel in server.channels:
                yield channel

    def get_all_members(self):
        """Returns a generator with every :class:`Member` the client can see.

        This is equivalent to: ::

            for server in client.servers:
                for member in server.members:
                    yield member

        """
        for server in self.servers:
            for member in server.members:
                yield member

    # listeners/waiters

    @asyncio.coroutine
    def wait_until_ready(self):
        """|coro|

        This coroutine waits until the client is all ready. This could be considered
        another way of asking for :func:`discord.on_ready` except meant for your own
        background tasks.
        """
        yield from self._is_ready.wait()

    @asyncio.coroutine
    def wait_until_login(self):
        """|coro|

        This coroutine waits until the client is logged on successfully. This
        is different from waiting until the client's state is all ready. For
        that check :func:`discord.on_ready` and :meth:`wait_until_ready`.
        """
        yield from self._is_logged_in.wait()

    @asyncio.coroutine
    def wait_for_message(self, timeout=None, *, author=None, channel=None, content=None, check=None):
        """|coro|

        Waits for a message reply from Discord. This could be seen as another
        :func:`discord.on_message` event outside of the actual event. This could
        also be used for follow-ups and easier user interactions.

        The keyword arguments passed into this function are combined using the logical and
        operator. The ``check`` keyword argument can be used to pass in more complicated
        checks and must be a regular function (not a coroutine).

        The ``timeout`` parameter is passed into `asyncio.wait_for`_. By default, it
        does not timeout. Instead of throwing ``asyncio.TimeoutError`` the coroutine
        catches the exception and returns ``None`` instead of a :class:`Message`.

        If the ``check`` predicate throws an exception, then the exception is propagated.

        This function returns the **first message that meets the requirements**.

        .. _asyncio.wait_for: https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for

        Examples
        ----------

        Basic example:

        .. code-block:: python
            :emphasize-lines: 5

            @client.async_event
            def on_message(message):
                if message.content.startswith('$greet')
                    yield from client.send_message(message.channel, 'Say hello')
                    msg = yield from client.wait_for_message(author=message.author, content='hello')
                    yield from client.send_message(message.channel, 'Hello.')

        Asking for a follow-up question:

        .. code-block:: python
            :emphasize-lines: 6

            @client.async_event
            def on_message(message):
                if message.content.startswith('$start')
                    yield from client.send_message(message.channel, 'Type $stop 4 times.')
                    for i in range(4):
                        msg = yield from client.wait_for_message(author=message.author, content='$stop')
                        fmt = '{} left to go...'
                        yield from client.send_message(message.channel, fmt.format(3 - i))

                    yield from client.send_message(message.channel, 'Good job!')

        Advanced filters using ``check``:

        .. code-block:: python
            :emphasize-lines: 9

            @client.async_event
            def on_message(message):
                if message.content.startswith('$cool'):
                    yield from client.send_message(message.channel, 'Who is cool? Type $name namehere')

                    def check(msg):
                        return msg.content.startswith('$name')

                    message = yield from client.wait_for_message(author=message.author, check=check)
                    name = message.content[len('$name'):].strip()
                    yield from client.send_message(message.channel, '{} is cool indeed'.format(name))


        Parameters
        -----------
        timeout : float
            The number of seconds to wait before returning ``None``.
        author : :class:`Member` or :class:`User`
            The author the message must be from.
        channel : :class:`Channel` or :class:`PrivateChannel` or :class:`Object`
            The channel the message must be from.
        content : str
            The exact content the message must have.
        check : function
            A predicate for other complicated checks. The predicate must take
            a :class:`Message` as its only parameter.

        Returns
        --------
        :class:`Message`
            The message that you requested for.
        """

        def predicate(message):
            result = True
            if author is not None:
                result = result and message.author == author

            if content is not None:
                result = result and message.content == content

            if channel is not None:
                result = result and message.channel.id == channel.id

            if callable(check):
                # the exception thrown by check is propagated through the future.
                result = result and check(message)

            return result

        future = asyncio.Future(loop=self.loop)
        self._listeners.append((predicate, future))
        try:
            message = yield from asyncio.wait_for(future, timeout, loop=self.loop)
        except asyncio.TimeoutError:
            message = None
        return message

    # event registration

    def event(self, coro):
        """A decorator that registers an event to listen to.

        You can find more info about the events on the :ref:`documentation below <discord-api-events>`.

        The events must be a |corourl|_, if not, :exc:`ClientException` is raised.

        Examples
        ---------

        Using the basic :meth:`event` decorator: ::

            @client.event
            @asyncio.coroutine
            def on_ready():
                print('Ready!')

        Saving characters by using the :meth:`async_event` decorator: ::

            @client.async_event
            def on_ready():
                print('Ready!')

        """

        if not asyncio.iscoroutinefunction(coro):
            raise ClientException('event registered must be a coroutine function')

        setattr(self, coro.__name__, coro)
        log.info('{0.__name__} has successfully been registered as an event'.format(coro))
        return coro

    def async_event(self, coro):
        """A shorthand decorator for ``asyncio.coroutine`` + :meth:`event`."""
        if not asyncio.iscoroutinefunction(coro):
            coro = asyncio.coroutine(coro)

        return self.event(coro)

    # Message sending/management

    @asyncio.coroutine
    def start_private_message(self, user):
        """|coro|

        Starts a private message with the user. This allows you to
        :meth:`send_message` to the user.

        Note
        -----
        This method should rarely be called as :meth:`send_message`
        does it automatically for you.

        Parameters
        -----------
        user : :class:`User`
            The user to start the private message with.

        Raises
        ------
        HTTPException
            The request failed.
        InvalidArgument
            The user argument was not of :class:`User`.
        """

        if not isinstance(user, User):
            raise InvalidArgument('user argument must be a User')

        payload = {
            'recipient_id': user.id
        }

        url = '{}/channels'.format(endpoints.ME)
        r = yield from self.session.post(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=r))
        yield from utils._verify_successful_response(r)
        data = yield from r.json(encoding='utf-8')
        log.debug(request_success_log.format(response=r, json=payload, data=data))
        channel = PrivateChannel(id=data['id'], user=user)
        self.connection._add_private_channel(channel)
        return channel

    @asyncio.coroutine
    def _retry_helper(self, name, *args, retries=0, **kwargs):
        req_kwargs = {'headers': self.headers}
        req_kwargs.update(kwargs)
        resp = yield from self.session.request(*args, **req_kwargs)
        tmp = request_logging_format.format(method=resp.method, response=resp)
        log_fmt = 'In {}, {}'.format(name, tmp)
        log.debug(log_fmt)

        if resp.status == 502 and retries < 5:
            # retry the 502 request unconditionally
            log.info('Retrying the 502 request to ' + name)
            yield from asyncio.sleep(retries + 1)
            return (yield from self._retry_helper(name, *args, retries=retries + 1, **kwargs))

        if resp.status == 429:
            retry = float(resp.headers['Retry-After']) / 1000.0
            yield from resp.release()
            yield from asyncio.sleep(retry)
            return (yield from self._retry_helper(name, *args, retries=retries, **kwargs))

        return resp

    @asyncio.coroutine
    def send_message(self, destination, content, *, tts=False):
        """|coro|

        Sends a message to the destination given with the content given.

        The destination could be a :class:`Channel`, :class:`PrivateChannel` or :class:`Server`.
        For convenience it could also be a :class:`User`. If it's a :class:`User` or :class:`PrivateChannel`
        then it sends the message via private message, otherwise it sends the message to the channel.
        If the destination is a :class:`Server` then it's equivalent to calling
        :attr:`Server.default_channel` and sending it there.

        If it is a :class:`Object` instance then it is assumed to be the
        destination ID. The destination ID is a *channel* so passing in a user
        ID will not be a valid destination.

        .. versionchanged:: 0.9.0
            ``str`` being allowed was removed and replaced with :class:`Object`.

        The content must be a type that can convert to a string through ``str(content)``.

        Parameters
        ------------
        destination
            The location to send the message.
        content
            The content of the message to send.
        tts : bool
            Indicates if the message should be sent using text-to-speech.

        Raises
        --------
        HTTPException
            Sending the message failed.
        Forbidden
            You do not have the proper permissions to send the message.
        NotFound
            The destination was not found and hence is invalid.
        InvalidArgument
            The destination parameter is invalid.

        Returns
        ---------
        :class:`Message`
            The message that was sent.
        """

        channel_id = yield from self._resolve_destination(destination)

        content = str(content)

        url = '{base}/{id}/messages'.format(base=endpoints.CHANNELS, id=channel_id)
        payload = {
            'content': content,
            'nonce': random_integer(-2**63, 2**63 - 1)
        }

        if tts:
            payload['tts'] = True

        resp = yield from self._retry_helper('send_message', 'POST', url, data=utils.to_json(payload))
        yield from utils._verify_successful_response(resp)
        data = yield from resp.json(encoding='utf-8')
        log.debug(request_success_log.format(response=resp, json=payload, data=data))
        channel = self.get_channel(data.get('channel_id'))
        message = Message(channel=channel, **data)
        return message

    @asyncio.coroutine
    def send_typing(self, destination):
        """|coro|

        Send a *typing* status to the destination.

        *Typing* status will go away after 10 seconds, or after a message is sent.

        The destination parameter follows the same rules as :meth:`send_message`.

        Parameters
        ----------
        destination
            The location to send the typing update.
        """

        channel_id = yield from self._resolve_destination(destination)

        url = '{base}/{id}/typing'.format(base=endpoints.CHANNELS, id=channel_id)

        response = yield from self.session.post(url, headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def send_file(self, destination, fp, *, filename=None, content=None, tts=False):
        """|coro|

        Sends a message to the destination given with the file given.

        The destination parameter follows the same rules as :meth:`send_message`.

        The ``fp`` parameter should be either a string denoting the location for a
        file or a *file-like object*. The *file-like object* passed is **not closed**
        at the end of execution. You are responsible for closing it yourself.

        .. note::

            If the file-like object passed is opened via ``open`` then the modes
            'rb' should be used.

        The ``filename`` parameter is the filename of the file.
        If this is not given then it defaults to ``fp.name`` or if ``fp`` is a string
        then the ``filename`` will default to the string given. You can overwrite
        this value by passing this in.

        Parameters
        ------------
        destination
            The location to send the message.
        fp
            The *file-like object* or file path to send.
        filename : str
            The filename of the file. Defaults to ``fp.name`` if it's available.
        content
            The content of the message to send along with the file. This is
            forced into a string by a ``str(content)`` call.
        tts : bool
            If the content of the message should be sent with TTS enabled.

        Raises
        -------
        HTTPException
            Sending the file failed.

        Returns
        --------
        :class:`Message`
            The message sent.
        """

        channel_id = yield from self._resolve_destination(destination)

        url = '{base}/{id}/messages'.format(base=endpoints.CHANNELS, id=channel_id)
        form = aiohttp.FormData()

        if content is not None:
            form.add_field('content', str(content))

        form.add_field('tts', 'true' if tts else 'false')

        # we don't want the content-type json in this request
        headers = self.headers.copy()
        headers.pop('content-type', None)

        try:
            # attempt to open the file and send the request
            with open(fp, 'rb') as f:
                form.add_field('file', f, filename=filename, content_type='application/octet-stream')
                response = yield from self._retry_helper("send_file", "POST", url, data=form, headers=headers)
        except TypeError:
            form.add_field('file', fp, filename=filename, content_type='application/octet-stream')
            response = yield from self._retry_helper("send_file", "POST", url, data=form, headers=headers)

        log.debug(request_logging_format.format(method='POST', response=response))
        yield from utils._verify_successful_response(response)
        data = yield from response.json(encoding='utf-8')
        msg = 'POST {0.url} returned {0.status} with {1} response'
        log.debug(msg.format(response, data))
        channel = self.get_channel(data.get('channel_id'))
        message = Message(channel=channel, **data)
        return message

    @asyncio.coroutine
    def delete_message(self, message):
        """|coro|

        Deletes a :class:`Message`.

        Your own messages could be deleted without any proper permissions. However to
        delete other people's messages, you need the proper permissions to do so.

        Parameters
        -----------
        message : :class:`Message`
            The message to delete.

        Raises
        ------
        Forbidden
            You do not have proper permissions to delete the message.
        HTTPException
            Deleting the message failed.
        """

        url = '{}/{}/messages/{}'.format(endpoints.CHANNELS, message.channel.id, message.id)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def delete_messages(self, messages):
        """|coro|

        Deletes a list of messages. This is similar to :func:`delete_message`
        except it bulk deletes multiple messages.

        The channel to check where the message is deleted from is handled via
        the first element of the iterable's ``.channel.id`` attributes. If the
        channel is not consistent throughout the entire sequence, then an
        :exc:`HTTPException` will be raised.

        Usable only by bot accounts.

        Parameters
        -----------
        messages : iterable of :class:`Message`
            An iterable of messages denoting which ones to bulk delete.

        Raises
        ------
        ClientException
            The number of messages to delete is less than 2 or more than 100.
        Forbidden
            You do not have proper permissions to delete the messages or
            you're not using a bot account.
        HTTPException
            Deleting the messages failed.
        """

        messages = list(messages)
        if len(messages) > 100 or len(messages) < 2:
            raise ClientException('Can only delete messages in the range of [2, 100]')

        channel_id = messages[0].channel.id
        url = '{0}/{1}/messages/bulk_delete'.format(endpoints.CHANNELS, channel_id)
        payload = {
            'messages': [m.id for m in messages]
        }

        response = yield from self.session.post(url, headers=self.headers, data=utils.to_json(payload))
        log.debug(request_logging_format.format(method='POST', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def purge_from(self, channel, *, limit=100, check=None, before=None, after=None):
        """|coro|

        Purges a list of messages that meet the criteria given by the predicate
        ``check``. If a ``check`` is not provided then all messages are deleted
        without discrimination.

        You must have Manage Messages permission to delete messages that aren't
        your own. The Read Message History permission is also needed to retrieve
        message history.

        Usable only by bot accounts.

        Parameters
        -----------
        channel : :class:`Channel`
            The channel to purge from.
        limit : int
            The number of messages to search through. This is not the number
            of messages that will be deleted, though it can be.
        check : predicate
            The function used to check if a message should be deleted.
            It must take a :class:`Message` as its sole parameter.
        before : :class:`Message`
            The message before scanning for purging must be.
        after : :class:`Message`
            The message after scanning for purging must be.

        Raises
        -------
        Forbidden
            You do not have proper permissions to do the actions required or
            you're not using a bot account.
        HTTPException
            Purging the messages failed.

        Examples
        ---------

        Deleting bot's messages ::

            def is_me(m):
                return m.author == client.user

            deleted = await client.purge_from(channel, limit=100, check=is_me)
            await client.send_message(channel, 'Deleted {} message(s)'.format(len(deleted)))

        Returns
        --------
        list
            The list of messages that were deleted.
        """

        if check is None:
            check = lambda m: True

        iterator = LogsFromIterator.create(self, channel, limit, before=before, after=after)
        ret = []
        count = 0

        while True:
            try:
                msg = yield from iterator.iterate()
            except asyncio.QueueEmpty:
                # no more messages to poll
                if count >= 2:
                    # more than 2 messages -> bulk delete
                    to_delete = ret[-count:]
                    yield from self.delete_messages(to_delete)
                elif count == 1:
                    # delete a single message
                    yield from self.delete_message(ret[-1])

                return ret
            else:
                if count == 100:
                    # we've reached a full 'queue'
                    to_delete = ret[-100:]
                    yield from self.delete_messages(to_delete)
                    count = 0
                    yield from asyncio.sleep(1)

                if check(msg):
                    count += 1
                    ret.append(msg)

    @asyncio.coroutine
    def edit_message(self, message, new_content):
        """|coro|

        Edits a :class:`Message` with the new message content.

        The new_content must be able to be transformed into a string via ``str(new_content)``.

        Parameters
        -----------
        message : :class:`Message`
            The message to edit.
        new_content
            The new content to replace the message with.

        Raises
        -------
        HTTPException
            Editing the message failed.

        Returns
        --------
        :class:`Message`
            The new edited message.
        """

        channel = message.channel
        content = str(new_content)

        url = '{}/{}/messages/{}'.format(endpoints.CHANNELS, channel.id, message.id)
        payload = {
            'content': content
        }

        response = yield from self._retry_helper('edit_message', 'PATCH', url, data=utils.to_json(payload))
        log.debug(request_logging_format.format(method='PATCH', response=response))
        yield from utils._verify_successful_response(response)
        data = yield from response.json(encoding='utf-8')
        log.debug(request_success_log.format(response=response, json=payload, data=data))
        return Message(channel=channel, **data)

    @asyncio.coroutine
    def _logs_from(self, channel, limit=100, before=None, after=None):
        """|coro|

        This coroutine returns a generator that obtains logs from a specified channel.

        Parameters
        -----------
        channel : :class:`Channel`
            The channel to obtain the logs from.
        limit : int
            The number of messages to retrieve.
        before : :class:`Message` or `datetime`
            The message or date before which all returned messages must be.
            If a date is provided it must be a timezone-naive datetime representing UTC time.
        after : :class:`Message` or `datetime`
            The message or date after which all returned messages must be.
            If a date is provided it must be a timezone-naive datetime representing UTC time.

        Raises
        ------
        Forbidden
            You do not have permissions to get channel logs.
        NotFound
            The channel you are requesting for doesn't exist.
        HTTPException
            The request to get logs failed.

        Yields
        -------
        :class:`Message`
            The message with the message data parsed.

        Examples
        ---------

        Basic logging: ::

            logs = yield from client.logs_from(channel)
            for message in logs:
                if message.content.startswith('!hello'):
                    if message.author == client.user:
                        yield from client.edit_message(message, 'goodbye')

        Python 3.5 Usage ::

            counter = 0
            async for message in client.logs_from(channel, limit=500):
                if message.author == client.user:
                    counter += 1
        """
        url = '{}/{}/messages'.format(endpoints.CHANNELS, channel.id)
        params = {
            'limit': limit
        }

        if before:
            params['before'] = before.id
        if after:
            params['after'] = after.id

        response = yield from self.session.get(url, params=params, headers=self.headers)
        log.debug(request_logging_format.format(method='GET', response=response))
        yield from utils._verify_successful_response(response)
        messages = yield from response.json(encoding='utf-8')
        return messages

    if PY35:
        def logs_from(self, channel, limit=100, *, before=None, after=None, reverse=False):
            if isinstance(before, datetime.datetime):
                before = Object(utils.time_snowflake(before, high=False))
            if isinstance(after, datetime.datetime):
                after = Object(utils.time_snowflake(after, high=True))

            return LogsFromIterator.create(self, channel, limit, before=before, after=after, reverse=reverse)
    else:
        @asyncio.coroutine
        def logs_from(self, channel, limit=100, *, before=None, after=None):
            if isinstance(before, datetime.datetime):
                before = Object(utils.time_snowflake(before, high=False))
            if isinstance(after, datetime.datetime):
                after = Object(utils.time_snowflake(after, high=True))

            def generator(data):
                for message in data:
                    yield Message(channel=channel, **message)

            result = []
            while limit > 0:
                retrieve = limit if limit <= 100 else 100
                data = yield from self._logs_from(channel, retrieve, before, after)
                if len(data):
                    limit -= retrieve
                    result.extend(data)
                    before = Object(id=data[-1]['id'])
                else:
                    break

            return generator(result)

    logs_from.__doc__ = _logs_from.__doc__

    # Member management

    @asyncio.coroutine
    def request_offline_members(self, server):
        """|coro|

        Requests previously offline members from the server to be filled up
        into the :attr:`Server.members` cache. This function is usually not
        called.

        When the client logs on and connects to the websocket, Discord does
        not provide the library with offline members if the number of members
        in the server is larger than 250. You can check if a server is large
        if :attr:`Server.large` is ``True``.

        Parameters
        -----------
        server : :class:`Server` or iterable
            The server to request offline members for. If this parameter is a
            iterable then it is interpreted as an iterator of servers to
            request offline members for.
        """

        if hasattr(server, 'id'):
            guild_id = server.id
        else:
            guild_id = [s.id for s in server]

        payload = {
            'op': 8,
            'd': {
                'guild_id': guild_id,
                'query': '',
                'limit': 0
            }
        }

        yield from self.ws.send_as_json(payload)

    @asyncio.coroutine
    def kick(self, member):
        """|coro|

        Kicks a :class:`Member` from the server they belong to.

        Warning
        --------
        This function kicks the :class:`Member` based on the server it
        belongs to, which is accessed via :attr:`Member.server`. So you
        must have the proper permissions in that server.

        Parameters
        -----------
        member : :class:`Member`
            The member to kick from their server.

        Raises
        -------
        Forbidden
            You do not have the proper permissions to kick.
        HTTPException
            Kicking failed.
        """

        url = '{0}/{1.server.id}/members/{1.id}'.format(endpoints.SERVERS, member)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def ban(self, member, delete_message_days=1):
        """|coro|

        Bans a :class:`Member` from the server they belong to.

        Warning
        --------
        This function bans the :class:`Member` based on the server it
        belongs to, which is accessed via :attr:`Member.server`. So you
        must have the proper permissions in that server.

        Parameters
        -----------
        member : :class:`Member`
            The member to ban from their server.
        delete_message_days : int
            The number of days worth of messages to delete from the user
            in the server. The minimum is 0 and the maximum is 7.

        Raises
        -------
        Forbidden
            You do not have the proper permissions to ban.
        HTTPException
            Banning failed.
        """

        params = {
            'delete-message-days': delete_message_days
        }

        url = '{0}/{1.server.id}/bans/{1.id}'.format(endpoints.SERVERS, member)
        response = yield from self.session.put(url, params=params, headers=self.headers)
        log.debug(request_logging_format.format(method='PUT', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def unban(self, server, user):
        """|coro|

        Unbans a :class:`User` from the server they are banned from.

        Parameters
        -----------
        server : :class:`Server`
            The server to unban the user from.
        user : :class:`User`
            The user to unban.

        Raises
        -------
        Forbidden
            You do not have the proper permissions to unban.
        HTTPException
            Unbanning failed.
        """

        url = '{0}/{1.id}/bans/{2.id}'.format(endpoints.SERVERS, server, user)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def server_voice_state(self, member, *, mute=False, deafen=False):
        """|coro|

        Server mutes or deafens a specific :class:`Member`.

        Warning
        --------
        This function mutes or un-deafens the :class:`Member` based on the
        server it belongs to, which is accessed via :attr:`Member.server`.
        So you must have the proper permissions in that server.

        Parameters
        -----------
        member : :class:`Member`
            The member to unban from their server.
        mute : bool
            Indicates if the member should be server muted or un-muted.
        deafen : bool
            Indicates if the member should be server deafened or un-deafened.

        Raises
        -------
        Forbidden
            You do not have the proper permissions to deafen or mute.
        HTTPException
            The operation failed.
        """

        url = '{0}/{1.server.id}/members/{1.id}'.format(endpoints.SERVERS, member)
        payload = {
            'mute': mute,
            'deaf': deafen
        }

        response = yield from self.session.patch(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def edit_profile(self, password=None, **fields):
        """|coro|

        Edits the current profile of the client.

        If a bot account is used then the password field is optional,
        otherwise it is required.

        The profile is **not** edited in place.

        Note
        -----
        To upload an avatar, a *bytes-like object* must be passed in that
        represents the image being uploaded. If this is done through a file
        then the file must be opened via ``open('some_filename', 'rb')`` and
        the *bytes-like object* is given through the use of ``fp.read()``.

        The only image formats supported for uploading is JPEG and PNG.

        Parameters
        -----------
        password : str
            The current password for the client's account. Not used
            for bot accounts.
        new_password : str
            The new password you wish to change to.
        email : str
            The new email you wish to change to.
        username :str
            The new username you wish to change to.
        avatar : bytes
            A *bytes-like object* representing the image to upload.
            Could be ``None`` to denote no avatar.

        Raises
        ------
        HTTPException
            Editing your profile failed.
        InvalidArgument
            Wrong image format passed for ``avatar``.
        ClientException
            Password is required for non-bot accounts.
        """

        try:
            avatar_bytes = fields['avatar']
        except KeyError:
            avatar = self.user.avatar
        else:
            if avatar_bytes is not None:
                avatar = utils._bytes_to_base64_data(avatar_bytes)
            else:
                avatar = None

        not_bot_account = not self.user.bot
        if not_bot_account and password is None:
            raise ClientException('Password is required for non-bot accounts.')

        payload = {
            'password': password,
            'username': fields.get('username', self.user.name),
            'avatar': avatar
        }

        if not_bot_account:
            payload['email'] = fields.get('email', self.email)

            if 'new_password' in fields:
                payload['new_password'] = fields['new_password']


        r = yield from self.session.patch(endpoints.ME, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=r))
        yield from utils._verify_successful_response(r)

        data = yield from r.json(encoding='utf-8')
        log.debug(request_success_log.format(response=r, json=payload, data=data))

        if not_bot_account:
            self.token = data['token']
            self.email = data['email']
            self.headers['authorization'] = self.token

            if self.cache_auth:
                self._update_cache(self.email, password)

    @asyncio.coroutine
    def change_status(self, game=None, idle=False):
        """|coro|

        Changes the client's status.

        The game parameter is a Game object (not a string) that represents
        a game being played currently.

        The idle parameter is a boolean parameter that indicates whether the
        client should go idle or not.

        .. _game_list: https://gist.github.com/Rapptz/a82b82381b70a60c281b

        Parameters
        ----------
        game : Optional[:class:`Game`]
            The game being played. None if no game is being played.
        idle : bool
            Indicates if the client should go idle.

        Raises
        ------
        InvalidArgument
            If the ``game`` parameter is not :class:`Game` or None.
        """
        yield from self.ws.change_presence(game=game, idle=idle)

    @asyncio.coroutine
    def change_nickname(self, member, nickname):
        """|coro|

        Changes a member's nickname.

        You must have the proper permissions to change someone's
        (or your own) nickname.

        Parameters
        ----------
        member : :class:`Member`
            The member to change the nickname for.
        nickname : Optional[str]
            The nickname to change it to. ``None`` to remove
            the nickname.

        Raises
        ------
        Forbidden
            You do not have permissions to change the nickname.
        HTTPException
            Changing the nickname failed.
        """

        if member == self.user:
            fmt = '{0}/{1.server.id}/members/@me/nick'
        else:
            fmt = '{0}/{1.server.id}/members/{1.id}'

        url = fmt.format(endpoints.SERVERS, member)

        payload = {
            # oddly enough, this endpoint requires '' to clear the nickname
            # instead of the more consistent 'null', this might change in the
            # future, or not.
            'nick': nickname if nickname else ''
        }

        r = yield from self.session.patch(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=r))
        yield from utils._verify_successful_response(r)
        yield from r.release()

    # Channel management

    @asyncio.coroutine
    def edit_channel(self, channel, **options):
        """|coro|

        Edits a :class:`Channel`.

        You must have the proper permissions to edit the channel.

        The channel is **not** edited in-place.

        Parameters
        ----------
        channel : :class:`Channel`
            The channel to update.
        name : str
            The new channel name.
        position : int
            The new channel's position in the GUI.
        topic : str
            The new channel's topic.
        bitrate : int
            The new channel's bitrate. Voice only.
        user_limit : int
            The new channel's user limit. Voice only.

        Raises
        ------
        Forbidden
            You do not have permissions to edit the channel.
        HTTPException
            Editing the channel failed.
        """

        url = '{0}/{1.id}'.format(endpoints.CHANNELS, channel)
        payload = {
            'name': options.get('name', channel.name),
            'topic': options.get('topic', channel.topic),
            'position': options.get('position', channel.position),
        }

        user_limit = options.get('user_limit')
        if user_limit is not None:
            payload['user_limit'] = user_limit

        bitrate = options.get('bitrate')
        if bitrate is not None:
            payload['bitrate'] = bitrate

        r = yield from self.session.patch(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=r))
        yield from utils._verify_successful_response(r)

        data = yield from r.json(encoding='utf-8')
        log.debug(request_success_log.format(response=r, json=payload, data=data))

    @asyncio.coroutine
    def create_channel(self, server, name, type=None):
        """|coro|

        Creates a :class:`Channel` in the specified :class:`Server`.

        Note that you need the proper permissions to create the channel.

        Parameters
        -----------
        server : :class:`Server`
            The server to create the channel in.
        name : str
            The channel's name.
        type : :class:`ChannelType`
            The type of channel to create. Defaults to :attr:`ChannelType.text`.

        Raises
        -------
        Forbidden
            You do not have the proper permissions to create the channel.
        NotFound
            The server specified was not found.
        HTTPException
            Creating the channel failed.

        Returns
        -------
        :class:`Channel`
            The channel that was just created. This channel is
            different than the one that will be added in cache.
        """

        if type is None:
            type = ChannelType.text

        payload = {
            'name': name,
            'type': str(type)
        }

        url = '{0}/{1.id}/channels'.format(endpoints.SERVERS, server)
        response = yield from self.session.post(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=response))
        yield from utils._verify_successful_response(response)

        data = yield from response.json(encoding='utf-8')
        log.debug(request_success_log.format(response=response, data=data, json=payload))
        channel = Channel(server=server, **data)
        return channel

    @asyncio.coroutine
    def delete_channel(self, channel):
        """|coro|

        Deletes a :class:`Channel`.

        In order to delete the channel, the client must have the proper permissions
        in the server the channel belongs to.

        Parameters
        ------------
        channel : :class:`Channel`
            The channel to delete.

        Raises
        -------
        Forbidden
            You do not have proper permissions to delete the channel.
        NotFound
            The specified channel was not found.
        HTTPException
            Deleting the channel failed.
        """

        url = '{}/{}'.format(endpoints.CHANNELS, channel.id)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    # Server management

    @asyncio.coroutine
    def leave_server(self, server):
        """|coro|

        Leaves a :class:`Server`.

        Note
        --------
        You cannot leave the server that you own, you must delete it instead
        via :meth:`delete_server`.

        Parameters
        ----------
        server : :class:`Server`
            The server to leave.

        Raises
        --------
        HTTPException
            If leaving the server failed.
        """

        url = '{}/@me/guilds/{.id}'.format(endpoints.USERS, server)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def delete_server(self, server):
        """|coro|

        Deletes a :class:`Server`. You must be the server owner to delete the
        server.

        Parameters
        ----------
        server : :class:`Server`
            The server to delete.

        Raises
        --------
        HTTPException
            If deleting the server failed.
        Forbidden
            You do not have permissions to delete the server.
        """

        url = '{0}/{1.id}'.format(endpoints.SERVERS, server)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def create_server(self, name, region=None, icon=None):
        """|coro|

        Creates a :class:`Server`.

        Parameters
        ----------
        name : str
            The name of the server.
        region : :class:`ServerRegion`
            The region for the voice communication server.
            Defaults to :attr:`ServerRegion.us_west`.
        icon : bytes
            The *bytes-like* object representing the icon. See :meth:`edit_profile`
            for more details on what is expected.

        Raises
        ------
        HTTPException
            Server creation failed.
        InvalidArgument
            Invalid icon image format given. Must be PNG or JPG.

        Returns
        -------
        :class:`Server`
            The server created. This is not the same server that is
            added to cache.
        """
        if icon is not None:
            icon = utils._bytes_to_base64_data(icon)

        if region is None:
            region = ServerRegion.us_west.name
        else:
            region = region.name

        payload = {
            'icon': icon,
            'name': name,
            'region': region
        }

        r = yield from self.session.post(endpoints.SERVERS, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=r))
        yield from utils._verify_successful_response(r)
        data = yield from r.json(encoding='utf-8')
        log.debug(request_success_log.format(response=r, json=payload, data=data))
        return Server(**data)

    @asyncio.coroutine
    def edit_server(self, server, **fields):
        """|coro|

        Edits a :class:`Server`.

        You must have the proper permissions to edit the server.

        The server is **not** edited in-place.

        Parameters
        ----------
        server : :class:`Server`
            The server to edit.
        name : str
            The new name of the server.
        icon : bytes
            A *bytes-like* object representing the icon. See :meth:`edit_profile`
            for more details. Could be ``None`` to denote
        region : :class:`ServerRegion`
            The new region for the server's voice communication.
        afk_channel : :class:`Channel`
            The new channel that is the AFK channel. Could be ``None`` for no AFK channel.
        afk_timeout : int
            The number of seconds until someone is moved to the AFK channel.
        owner : :class:`Member`
            The new owner of the server to transfer ownership to. Note that you must
            be owner of the server to do this.

        Raises
        -------
        Forbidden
            You do not have permissions to edit the server.
        NotFound
            The server you are trying to edit does not exist.
        HTTPException
            Editing the server failed.
        InvalidArgument
            The image format passed in to ``icon`` is invalid. It must be
            PNG or JPG. This is also raised if you are not the owner of the
            server and request an ownership transfer.
        """

        try:
            icon_bytes = fields['icon']
        except KeyError:
            icon = server.icon
        else:
            if icon_bytes is not None:
                icon = utils._bytes_to_base64_data(icon_bytes)
            else:
                icon = None

        payload = {
            'region': str(fields.get('region', server.region)),
            'afk_timeout': fields.get('afk_timeout', server.afk_timeout),
            'icon': icon,
            'name': fields.get('name', server.name),
        }

        afk_channel = fields.get('afk_channel')
        if afk_channel is None:
            afk_channel = server.afk_channel

        payload['afk_channel'] = getattr(afk_channel, 'id', None)

        if 'owner' in fields:
            if server.owner != server.me:
                raise InvalidArgument('To transfer ownership you must be the owner of the server.')

            payload['owner_id'] = fields['owner'].id

        url = '{0}/{1.id}'.format(endpoints.SERVERS, server)
        r = yield from self.session.patch(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=r))
        yield from utils._verify_successful_response(r)
        yield from r.release()

    @asyncio.coroutine
    def get_bans(self, server):
        """|coro|

        Retrieves all the :class:`User` s that are banned from the specified
        server.

        You must have proper permissions to get this information.

        Parameters
        ----------
        server : :class:`Server`
            The server to get ban information from.

        Raises
        -------
        Forbidden
            You do not have proper permissions to get the information.
        HTTPException
            An error occurred while fetching the information.

        Returns
        --------
        list
            A list of :class:`User` that have been banned.
        """

        url = '{0}/{1.id}/bans'.format(endpoints.SERVERS, server)
        resp = yield from self.session.get(url, headers=self.headers)
        log.debug(request_logging_format.format(method='GET', response=resp))
        yield from utils._verify_successful_response(resp)
        data = yield from resp.json(encoding='utf-8')
        return [User(**user['user']) for user in data]

    # Invite management

    def _fill_invite_data(self, data):
        server = self.connection._get_server(data['guild']['id'])
        if server is not None:
            ch_id = data['channel']['id']
            channel = server.get_channel(ch_id)
        else:
            server = Object(id=data['guild']['id'])
            server.name = data['guild']['name']
            channel = Object(id=data['channel']['id'])
            channel.name = data['channel']['name']
        data['server'] = server
        data['channel'] = channel

    @asyncio.coroutine
    def create_invite(self, destination, **options):
        """|coro|

        Creates an invite for the destination which could be either a
        :class:`Server` or :class:`Channel`.

        Parameters
        ------------
        destination
            The :class:`Server` or :class:`Channel` to create the invite to.
        max_age : int
            How long the invite should last. If it's 0 then the invite
            doesn't expire. Defaults to 0.
        max_uses : int
            How many uses the invite could be used for. If it's 0 then there
            are unlimited uses. Defaults to 0.
        temporary : bool
            Denotes that the invite grants temporary membership
            (i.e. they get kicked after they disconnect). Defaults to False.
        xkcd : bool
            Indicates if the invite URL is human readable. Defaults to False.

        Raises
        -------
        HTTPException
            Invite creation failed.

        Returns
        --------
        :class:`Invite`
            The invite that was created.
        """

        payload = {
            'max_age': options.get('max_age', 0),
            'max_uses': options.get('max_uses', 0),
            'temporary': options.get('temporary', False),
            'xkcdpass': options.get('xkcd', False)
        }

        url = '{0}/{1.id}/invites'.format(endpoints.CHANNELS, destination)
        response = yield from self.session.post(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=response))

        yield from utils._verify_successful_response(response)
        data = yield from response.json(encoding='utf-8')
        log.debug(request_success_log.format(json=payload, response=response, data=data))
        self._fill_invite_data(data)
        return Invite(**data)

    @asyncio.coroutine
    def get_invite(self, url):
        """|coro|

        Gets a :class:`Invite` from a discord.gg URL or ID.

        Note
        ------
        If the invite is for a server you have not joined, the server and channel
        attributes of the returned invite will be :class:`Object` with the names
        patched in.

        Parameters
        -----------
        url : str
            The discord invite ID or URL (must be a discord.gg URL).

        Raises
        -------
        NotFound
            The invite has expired or is invalid.
        HTTPException
            Getting the invite failed.

        Returns
        --------
        :class:`Invite`
            The invite from the URL/ID.
        """

        destination = self._resolve_invite(url)
        rurl = '{0}/invite/{1}'.format(endpoints.API_BASE, destination)
        response = yield from self.session.get(rurl, headers=self.headers)
        log.debug(request_logging_format.format(method='GET', response=response))
        yield from utils._verify_successful_response(response)
        data = yield from response.json(encoding='utf-8')
        self._fill_invite_data(data)
        return Invite(**data)

    @asyncio.coroutine
    def invites_from(self, server):
        """|coro|

        Returns a list of all active instant invites from a :class:`Server`.

        You must have proper permissions to get this information.

        Parameters
        ----------
        server : :class:`Server`
            The server to get invites from.

        Raises
        -------
        Forbidden
            You do not have proper permissions to get the information.
        HTTPException
            An error occurred while fetching the information.

        Returns
        -------
        list of :class:`Invite`
            The list of invites that are currently active.
        """

        url = '{0}/{1.id}/invites'.format(endpoints.SERVERS, server)
        resp = yield from self.session.get(url, headers=self.headers)
        log.debug(request_logging_format.format(method='GET', response=resp))
        yield from utils._verify_successful_response(resp)
        data = yield from resp.json(encoding='utf-8')
        result = []
        for invite in data:
            channel = server.get_channel(invite['channel']['id'])
            invite['channel'] = channel
            invite['server'] = server
            result.append(Invite(**invite))

        return result

    @asyncio.coroutine
    def accept_invite(self, invite):
        """|coro|

        Accepts an :class:`Invite`, URL or ID to an invite.

        The URL must be a discord.gg URL. e.g. "http://discord.gg/codehere".
        An ID for the invite is just the "codehere" portion of the invite URL.

        Parameters
        -----------
        invite
            The :class:`Invite` or URL to an invite to accept.

        Raises
        -------
        HTTPException
            Accepting the invite failed.
        NotFound
            The invite is invalid or expired.
        """

        destination = self._resolve_invite(invite)
        url = '{0}/invite/{1}'.format(endpoints.API_BASE, destination)
        response = yield from self.session.post(url, headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def delete_invite(self, invite):
        """|coro|

        Revokes an :class:`Invite`, URL, or ID to an invite.

        The ``invite`` parameter follows the same rules as
        :meth:`accept_invite`.

        Parameters
        ----------
        invite
            The invite to revoke.

        Raises
        -------
        Forbidden
            You do not have permissions to revoke invites.
        NotFound
            The invite is invalid or expired.
        HTTPException
            Revoking the invite failed.
        """

        destination = self._resolve_invite(invite)
        url = '{0}/invite/{1}'.format(endpoints.API_BASE, destination)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    # Role management

    @asyncio.coroutine
    def move_role(self, server, role, position):
        """|coro|

        Moves the specified :class:`Role` to the given position in the :class:`Server`.

        This does **not** edit the role ordering in place.

        Parameters
        -----------
        server : :class:`Server`
            The server the role belongs to.
        role : :class:`Role`
            The role to edit.
        position : int
            The position to insert the role to.

        Raises
        -------
        InvalidArgument
            If position is 0, or role is server.default_role
        Forbidden
            You do not have permissions to change role order.
        HTTPException
            If moving the role failed, or you are of too low rank to move the role.
        """

        if position == 0:
            raise InvalidArgument("Cannot move role to position 0")

        if role == server.default_role:
            raise InvalidArgument("Cannot move default role")

        if role.position == position:
            return  # Save discord the extra request.

        url = '{0}/{1.id}/roles'.format(endpoints.SERVERS, server)

        change_range = range(min(role.position, position), max(role.position, position) + 1)

        roles = [r.id for r in sorted(filter(lambda x: (x.position in change_range) and x != role, server.roles), key=lambda x: x.position)]

        if role.position > position:
            roles.insert(0, role.id)
        else:
            roles.append(role.id)

        payload = [{"id": z[0], "position": z[1]} for z in zip(roles, change_range)]

        r = yield from self.session.patch(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=r))
        yield from utils._verify_successful_response(r)

        data = yield from r.json()
        log.debug(request_success_log.format(json=payload, response=r, data=data))

    @asyncio.coroutine
    def edit_role(self, server, role, **fields):
        """|coro|

        Edits the specified :class:`Role` for the entire :class:`Server`.

        This does **not** edit the role in place.

        All fields except ``server`` and ``role`` are optional.

        .. versionchanged:: 0.8.0
            Editing now uses keyword arguments instead of editing the :class:`Role` object directly.

        Note
        -----
        At the moment, the Discord API allows you to set the colour to any
        RGB value. This might change in the future so it is recommended that
        you use the constants in the :class:`Colour` instead such as
        :meth:`Colour.green`.

        Parameters
        -----------
        server : :class:`Server`
            The server the role belongs to.
        role : :class:`Role`
            The role to edit.
        name : str
            The new role name to change to.
        permissions : :class:`Permissions`
            The new permissions to change to.
        colour : :class:`Colour`
            The new colour to change to. (aliased to color as well)
        hoist : bool
            Indicates if the role should be shown separately in the online list.

        Raises
        -------
        Forbidden
            You do not have permissions to change the role.
        HTTPException
            Editing the role failed.
        """

        url = '{0}/{1.id}/roles/{2.id}'.format(endpoints.SERVERS, server, role)
        color = fields.get('color')
        if color is None:
            color = fields.get('colour', role.colour)

        payload = {
            'name': fields.get('name', role.name),
            'permissions': fields.get('permissions', role.permissions).value,
            'color': color.value,
            'hoist': fields.get('hoist', role.hoist)
        }

        r = yield from self.session.patch(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=r))
        yield from utils._verify_successful_response(r)

        data = yield from r.json(encoding='utf-8')
        log.debug(request_success_log.format(json=payload, response=r, data=data))

    @asyncio.coroutine
    def delete_role(self, server, role):
        """|coro|

        Deletes the specified :class:`Role` for the entire :class:`Server`.

        Works in a similar matter to :func:`edit_role`.

        Parameters
        -----------
        server : :class:`Server`
            The server the role belongs to.
        role : :class:`Role`
            The role to delete.

        Raises
        --------
        Forbidden
            You do not have permissions to delete the role.
        HTTPException
            Deleting the role failed.
        """

        url = '{0}/{1.id}/roles/{2.id}'.format(endpoints.SERVERS, server, role)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def _replace_roles(self, member, roles):
        url = '{0}/{1.server.id}/members/{1.id}'.format(endpoints.SERVERS, member)

        payload = {
            'roles': roles
        }

        r = yield from self.session.patch(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=r))
        yield from utils._verify_successful_response(r)
        yield from r.release()

    @asyncio.coroutine
    def add_roles(self, member, *roles):
        """|coro|

        Gives the specified :class:`Member` a number of :class:`Role` s.

        You must have the proper permissions to use this function.

        This method **appends** a role to a member but does **not** do it
        in-place.

        Parameters
        -----------
        member : :class:`Member`
            The member to give roles to.
        \*roles
            An argument list of :class:`Role` s to give the member.

        Raises
        -------
        Forbidden
            You do not have permissions to add roles.
        HTTPException
            Adding roles failed.
        """

        new_roles = utils._unique(role.id for role in itertools.chain(member.roles, roles))
        yield from self._replace_roles(member, new_roles)

    @asyncio.coroutine
    def remove_roles(self, member, *roles):
        """|coro|

        Removes the :class:`Role` s from the :class:`Member`.

        You must have the proper permissions to use this function.

        This method does **not** do edit the member in-place.

        Parameters
        -----------
        member : :class:`Member`
            The member to revoke roles from.
        \*roles
            An argument list of :class:`Role` s to revoke the member.

        Raises
        -------
        Forbidden
            You do not have permissions to revoke roles.
        HTTPException
            Removing roles failed.
        """
        new_roles = [x.id for x in member.roles]
        remove = []
        for role in roles:
            try:
                index = new_roles.index(role.id)
                remove.append(index)
            except ValueError:
                continue

        for index in reversed(remove):
            del new_roles[index]

        yield from self._replace_roles(member, new_roles)

    @asyncio.coroutine
    def replace_roles(self, member, *roles):
        """|coro|

        Replaces the :class:`Member`'s roles.

        You must have the proper permissions to use this function.

        This function **replaces** all roles that the member has.
        For example if the member has roles ``[a, b, c]`` and the
        call is ``client.replace_roles(member, d, e, c)`` then
        the member has the roles ``[d, e, c]``.

        This method does **not** do edit the member in-place.

        Parameters
        -----------
        member : :class:`Member`
            The member to replace roles from.
        \*roles
            An argument list of :class:`Role` s to replace the roles with.

        Raises
        -------
        Forbidden
            You do not have permissions to revoke roles.
        HTTPException
            Removing roles failed.
        """

        new_roles = utils._unique(role.id for role in roles)
        yield from self._replace_roles(member, new_roles)

    @asyncio.coroutine
    def create_role(self, server, **fields):
        """|coro|

        Creates a :class:`Role`.

        This function is similar to :class:`edit_role` in both
        the fields taken and exceptions thrown.

        Returns
        --------
        :class:`Role`
            The newly created role. This not the same role that
            is stored in cache.
        """

        url = '{0}/{1.id}/roles'.format(endpoints.SERVERS, server)
        r = yield from self.session.post(url, headers=self.headers)
        log.debug(request_logging_format.format(method='POST', response=r))
        yield from utils._verify_successful_response(r)

        data = yield from r.json(encoding='utf-8')
        everyone = server.id == data.get('id')
        role = Role(everyone=everyone, **data)

        # we have to call edit because you can't pass a payload to the
        # http request currently.
        yield from self.edit_role(server, role, **fields)
        return role

    @asyncio.coroutine
    def edit_channel_permissions(self, channel, target, *, allow=None, deny=None):
        """|coro|

        Sets the channel specific permission overwrites for a target in the
        specified :class:`Channel`.

        The ``target`` parameter should either be a :class:`Member` or a
        :class:`Role` that belongs to the channel's server.

        You must have the proper permissions to do this.

        Examples
        ----------

        Setting allow and deny: ::

            allow = discord.Permissions.none()
            deny = discord.Permissions.none()
            allow.mention_everyone = True
            deny.manage_messages = True
            yield from client.edit_channel_permissions(message.channel, message.author, allow=allow, deny=deny)

        Parameters
        -----------
        channel : :class:`Channel`
            The channel to give the specific permissions for.
        target
            The :class:`Member` or :class:`Role` to overwrite permissions for.
        allow : :class:`Permissions`
            The permissions to explicitly allow. (optional)
        deny : :class:`Permissions`
            The permissions to explicitly deny. (optional)

        Raises
        -------
        Forbidden
            You do not have permissions to edit channel specific permissions.
        NotFound
            The channel specified was not found.
        HTTPException
            Editing channel specific permissions failed.
        InvalidArgument
            The allow or deny arguments were not of type :class:`Permissions`
            or the target type was not :class:`Role` or :class:`Member`.
        """

        url = '{0}/{1.id}/permissions/{2.id}'.format(endpoints.CHANNELS, channel, target)

        allow = Permissions.none() if allow is None else allow
        deny = Permissions.none() if deny is None else deny

        if not (isinstance(allow, Permissions) and isinstance(deny, Permissions)):
            raise InvalidArgument('allow and deny parameters must be discord.Permissions')

        deny =  deny.value
        allow = allow.value

        payload = {
            'id': target.id,
            'allow': allow,
            'deny': deny
        }

        if isinstance(target, Member):
            payload['type'] = 'member'
        elif isinstance(target, Role):
            payload['type'] = 'role'
        else:
            raise InvalidArgument('target parameter must be either discord.Member or discord.Role')

        r = yield from self.session.put(url, data=utils.to_json(payload), headers=self.headers)
        log.debug(request_logging_format.format(method='PUT', response=r))
        yield from utils._verify_successful_response(r)
        yield from r.release()

    @asyncio.coroutine
    def delete_channel_permissions(self, channel, target):
        """|coro|

        Removes a channel specific permission overwrites for a target
        in the specified :class:`Channel`.

        The target parameter follows the same rules as :meth:`edit_channel_permissions`.

        You must have the proper permissions to do this.

        Parameters
        ----------
        channel : :class:`Channel`
            The channel to give the specific permissions for.
        target
            The :class:`Member` or :class:`Role` to overwrite permissions for.

        Raises
        ------
        Forbidden
            You do not have permissions to delete channel specific permissions.
        NotFound
            The channel specified was not found.
        HTTPException
            Deleting channel specific permissions failed.
        """

        url = '{0}/{1.id}/permissions/{2.id}'.format(endpoints.CHANNELS, channel, target)
        response = yield from self.session.delete(url, headers=self.headers)
        log.debug(request_logging_format.format(method='DELETE', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    # Voice management

    @asyncio.coroutine
    def move_member(self, member, channel):
        """|coro|

        Moves a :class:`Member` to a different voice channel.

        You must have proper permissions to do this.

        Note
        -----
        You cannot pass in a :class:`Object` in place of a :class:`Channel`
        object in this function.

        Parameters
        -----------
        member : :class:`Member`
            The member to move to another voice channel.
        channel : :class:`Channel`
            The voice channel to move the member to.

        Raises
        -------
        InvalidArgument
            The channel provided is not a voice channel.
        HTTPException
            Moving the member failed.
        Forbidden
            You do not have permissions to move the member.
        """

        url = '{0}/{1.server.id}/members/{1.id}'.format(endpoints.SERVERS, member)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise InvalidArgument('The channel provided must be a voice channel.')

        payload = utils.to_json({
            'channel_id': channel.id
        })
        response = yield from self.session.patch(url, data=payload, headers=self.headers)
        log.debug(request_logging_format.format(method='PATCH', response=response))
        yield from utils._verify_successful_response(response)
        yield from response.release()

    @asyncio.coroutine
    def join_voice_channel(self, channel):
        """|coro|

        Joins a voice channel and creates a :class:`VoiceClient` to
        establish your connection to the voice server.

        After this function is successfully called, :attr:`voice` is
        set to the returned :class:`VoiceClient`.

        Parameters
        ----------
        channel : :class:`Channel`
            The voice channel to join to.

        Raises
        -------
        InvalidArgument
            The channel was not a voice channel.
        asyncio.TimeoutError
            Could not connect to the voice channel in time.
        ClientException
            You are already connected to a voice channel.
        OpusNotLoaded
            The opus library has not been loaded.

        Returns
        -------
        :class:`VoiceClient`
            A voice client that is fully connected to the voice server.
        """
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise InvalidArgument('Channel passed must be a voice channel')

        server = channel.server

        if self.is_voice_connected(server):
            raise ClientException('Already connected to a voice channel in this server')

        log.info('attempting to join voice channel {0.name}'.format(channel))

        def session_id_found(data):
            user_id = data.get('user_id')
            return user_id == self.user.id

        # register the futures for waiting
        session_id_future = self.ws.wait_for('VOICE_STATE_UPDATE', session_id_found)
        voice_data_future = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: True)

        # request joining
        yield from self.ws.voice_state(server.id, channel.id)
        session_id_data = yield from asyncio.wait_for(session_id_future, timeout=10.0, loop=self.loop)
        data = yield from asyncio.wait_for(voice_data_future, timeout=10.0, loop=self.loop)

        kwargs = {
            'user': self.user,
            'channel': channel,
            'data': data,
            'loop': self.loop,
            'session_id': session_id_data.get('session_id'),
            'main_ws': self.ws
        }

        voice = VoiceClient(**kwargs)
        try:
            yield from voice.connect()
        except asyncio.TimeoutError as e:
            try:
                yield from voice.disconnect()
            except:
                # we don't care if disconnect failed because connection failed
                pass
            raise e # re-raise

        self.connection._add_voice_client(server.id, voice)
        return voice

    def is_voice_connected(self, server):
        """Indicates if we are currently connected to a voice channel in the
        specified server.

        Parameters
        -----------
        server : :class:`Server`
            The server to query if we're connected to it.
        """
        voice = self.voice_client_in(server)
        return voice is not None

    def voice_client_in(self, server):
        """Returns the voice client associated with a server.

        If no voice client is found then ``None`` is returned.

        Parameters
        -----------
        server : :class:`Server`
            The server to query if we have a voice client for.

        Returns
        --------
        :class:`VoiceClient`
            The voice client associated with the server.
        """
        return self.connection._get_voice_client(server.id)
