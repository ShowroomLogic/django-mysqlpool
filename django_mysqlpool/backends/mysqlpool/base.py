import os
import logging

from django.conf import settings
from django.db.backends.mysql import base
from django.core.exceptions import ImproperlyConfigured

try:
    from sqlalchemy import pool, util, exc
except ImportError as e:
    raise ImproperlyConfigured("Error loading SQLAlchemy module: %s" % e)


# Global variable to hold the actual connection pool.
MYSQLPOOL = None
# Default pool type (QueuePool, SingletonThreadPool, AssertionPool, NullPool,
# StaticPool).
DEFAULT_BACKEND = 'QueuePool'
# Needs to be less than MySQL connection timeout (server setting). The default
# is 120, so default to 119.
DEFAULT_POOL_TIMEOUT = 119

logger = logging.getLogger('django.db.backends')


class QueuePool(pool.QueuePool):

    def __init__(self, *args, **kwargs):
        super(QueuePool, self).__init__(*args, **kwargs)
        logger.info(
            "Creating connection pool with size {}, overflow {}, and timeout {}".format(
                self.size(), self._max_overflow, self._timeout
            )
        )

    def _do_get(self):

        use_overflow = self._max_overflow > -1

        try:
            wait = use_overflow and self._overflow >= self._max_overflow
            return self._pool.get(wait, self._timeout)
        except pool.sqla_queue.Empty:
            if use_overflow and self._overflow >= self._max_overflow:
                logger.warning((
                    "Database connection pool full. size: {}, "
                    "overflow: {} of {}"
                ).format(self.size(), self.overflow(), self._max_overflow))
                if not wait:
                    return self._do_get()
                else:
                    logger.error((
                        "Unable to establish database connection. Timeout {}"
                    ).format(self._timeout))
                    raise exc.TimeoutError(
                        "QueuePool limit of size %d overflow %d reached, "
                        "connection timed out, timeout %d" %
                        (self.size(), self.overflow(), self._timeout))

            if self._inc_overflow():
                
                if self._overflow >= 0:
                    logger.warning((
                        "Database connection pool full. size: {}, "
                        "overflow: {} of {}"
                    ).format(self.size(), self.overflow(), self._max_overflow))
                
                try:
                    return self._create_connection()
                except:
                    with util.safe_reraise():
                        self._dec_overflow()
            else:
                return self._do_get()


def isiterable(value):
    """Determine whether ``value`` is iterable."""
    try:
        iter(value)
        return True
    except TypeError:
        return False


class OldDatabaseProxy():

    """Saves a reference to the old connect function.

    Proxies calls to its own connect() method to the old function.
    """

    def __init__(self, old_connect):
        """Store ``old_connect`` to be used whenever we connect."""
        self.old_connect = old_connect

    def connect(self, **kwargs):
        """Delegate to the old ``connect``."""
        # Bounce the call to the old function.
        return self.old_connect(**kwargs)


class HashableDict(dict):

    """A dictionary that is hashable.

    This is not generally useful, but created specifically to hold the ``conv``
    parameter that needs to be passed to MySQLdb.
    """

    def __hash__(self):
        """Calculate the hash of this ``dict``.

        The hash is determined by converting to a sorted tuple of key-value
        pairs and hashing that.
        """
        items = [(n, tuple(v)) for n, v in self.items() if isiterable(v)]
        return hash(tuple(items))


# Define this here so Django can import it.
DatabaseWrapper = base.DatabaseWrapper


# Wrap the old connect() function so our pool can call it.
OldDatabase = OldDatabaseProxy(base.Database.connect)


def get_pool():
    """Create one and only one pool using the configured settings."""
    global MYSQLPOOL
    if MYSQLPOOL is None:
        backend = QueuePool
        kwargs = getattr(settings, 'MYSQLPOOL_ARGUMENTS', {})
        kwargs.setdefault('poolclass', backend)
        kwargs.setdefault('recycle', DEFAULT_POOL_TIMEOUT)
        kwargs['echo'] = False
        MYSQLPOOL = pool.manage(OldDatabase, **kwargs)
        setattr(MYSQLPOOL, '_pid', os.getpid())

    if getattr(MYSQLPOOL, '_pid', None) != os.getpid():
        pool.clear_managers()
    return MYSQLPOOL


def connect(**kwargs):
    """Obtain a database connection from the connection pool."""
    # SQLAlchemy serializes the parameters to keep unique connection
    # parameter groups in their own pool. We need to store certain
    # values in a manner that is compatible with their serialization.
    conv = kwargs.pop('conv', None)
    ssl = kwargs.pop('ssl', None)
    if conv:
        kwargs['conv'] = HashableDict(conv)

    if ssl:
        kwargs['ssl'] = HashableDict(ssl)

    # Open the connection via the pool.
    return get_pool().connect(**kwargs)


# Monkey-patch the regular mysql backend to use our hacked-up connect()
# function.
base.Database.connect = connect
