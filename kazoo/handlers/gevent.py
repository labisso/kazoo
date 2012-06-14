"""gevent Handler"""
from __future__ import absolute_import

import fcntl
import os

import gevent
import gevent.event
try:
    from gevent import get_hub
except ImportError:  # pragma: nocover
    from gevent.hub import get_hub

from gevent.queue import Empty
from gevent.queue import Queue
from zope.interface import implementer

from kazoo.interfaces import IAsyncResult
from kazoo.interfaces import IHandler


if gevent.__version__.startswith('1.'):
    _using_libevent = False
else:
    _using_libevent = True


# Simple wrapper to os.pipe() - but sets to non-block
def _pipe():
    r, w = os.pipe()
    fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
    fcntl.fcntl(w, fcntl.F_SETFL, os.O_NONBLOCK)
    return r, w

_core_pipe_read, _core_pipe_write = _pipe()


def _core_pipe_read_callback(event, evtype):
    try:
        os.read(event.fd, 1)
    except:
        # TODO: I'd prefer `except Exception:`
        pass

if _using_libevent:
    # The os pipe trick to wake gevent is only need for gevent pre-1.0
    gevent.core.event(gevent.core.EV_READ | gevent.core.EV_PERSIST,
                     _core_pipe_read, _core_pipe_read_callback).add()


@implementer(IAsyncResult)
class AsyncResult(gevent.event.AsyncResult):
    """A gevent AsyncResult capable of waking the gevent thread when used
    from the Zookeeper thread"""
    def __init__(self, handler):
        self._handler = handler
        gevent.event.AsyncResult.__init__(self)

    def set(self, value=None):
        # Proxy the set call to the gevent thread
        self._handler.completion_queue.put(
            lambda: gevent.event.AsyncResult.set(self, value)
        )
        self._handler.wake()

    def set_exception(self, exception):
        # Proxy the set_exception call to the gevent thread
        self._handler.completion_queue.put(
            lambda: gevent.event.AsyncResult.set_exception(self, exception)
        )
        self._handler.wake()


@implementer(IHandler)
class SequentialGeventHandler(object):
    """gevent Handler for sequentially executing callbacks

    This handler executes callbacks in a sequential manner from the Zookeeper
    thread. A queue is created for each of the callback events, so that each
    type of event has its callback type run sequentially. These are split into
    three queues, therefore it's possible that a session event arriving after a
    watch event may have its callback executed at the same time or slightly
    before the watch event callback.

    Each queue type has a greenlet worker that pulls the callback event off the
    queue and runs it in the order Zookeeper sent it.

    This split helps ensure that watch callbacks won't block session
    re-establishment should the connection be lost during a Zookeeper client
    call.

    Watch callbacks and session callbacks should avoid blocking behavior as the
    next callback of that type won't be run until it completes. If you need
    to block, spawn a new greenlet and return immediately so callbacks can
    proceed.

    """
    name = "sequential_gevent_handler"
    timeout_exception = gevent.event.Timeout

    def __init__(self, hub=None):
        """Create a :class:`SequentialGeventHandler` instance"""
        self.completion_queue = Queue()
        self.callback_queue = Queue()
        self.session_queue = Queue()
        self._running = True
        self._hub = hub or get_hub()
        self._async = None

        # Startup the async watcher to notify the gevent loop from other
        # threads when using gevent 1.0
        if not _using_libevent:
            self._async = self._hub.loop.async()

        # Spawn our worker greenlets, we have
        # - A completion worker for when values come back to be set on
        #   the AsyncResult object
        # - A callback worker for watch events to be called
        # - A session worker for session events to be called
        self._create_greenlet_worker(self.completion_queue)
        self._create_greenlet_worker(self.callback_queue)
        self._create_greenlet_worker(self.session_queue)

    def _create_greenlet_worker(self, queue):
        def greenlet_worker():
            while self._running:
                # We timeout after 1 and repeat so that we can gracefully
                # shutdown if self_running is set to false
                try:
                    func = queue.get(timeout=1)
                    func()
                except Empty:
                    continue
        gevent.spawn(greenlet_worker)

    def wake(self):
        """Wake the gevent hub the appropriate way"""
        if _using_libevent:
            # Wake gevent wait/gets
            os.write(_core_pipe_write, '\0')
        else:
            self._async.send()

    def event_object(self):
        """Create an appropriate Event object"""
        return gevent.event.Event()

    def async_result(self):
        """Create a :class:`AsyncResult` instance

        The :class:`AsyncResult` instance will have its completion
        callbacks executed in the thread the :class:`SequentialGeventHandler`
        is created in (which should be the gevent/main thread).

        """
        return AsyncResult(self)

    def dispatch_callback(self, callback):
        """Dispatch to the callback object

        The callback is put on separate queues to run depending on the type
        as documented for the :class:`SequentialGeventHandler`.

        """
        if callback.type == 'session':
            self.session_queue.put(lambda: callback.func(*callback.args))
        else:
            self.callback_queue.put(lambda: callback.func(*callback.args))
        self.wake()
