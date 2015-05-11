import six
import struct
import threading
import time
import transaction
import warnings
from ZODB.DB import Pickler, BytesIO, _protocol, z64,\
        ConnectionPool, KeyedConnectionPool, IMVCCStorage
from ZODB.DB import DB as BaseDB
from ZEO.Exceptions import StorageError
from ZODB.POSException import POSKeyError
from ZODB.Connection import Connection as BaseConnection
from ZODB.Connection import RootConvenience
import base
from zerodb.crypto import sha256
from zerodb.storage import ServerStorage


def create_root(storage, oid=z64, check_new=True):
    """
    Creates public or private root in storage.
    Root has the type PersistentMapping.

    :param storage: ZODB storage to create the root in
    :param str oid: Object id to give to the root (z64 is global root)
    :param bool check_new: If True, do nothing if the root exists
    """
    if check_new:
        try:
            storage.load(oid, '')
            return
        except KeyError:
            pass
    # Create the database's root in the storage if it doesn't exist
    from persistent.mapping import PersistentMapping
    root = PersistentMapping()
    # Manually create a pickle for the root to put in the storage.
    # The pickle must be in the special ZODB format.
    file = BytesIO()
    p = Pickler(file, _protocol)
    p.dump((root.__class__, None))
    p.dump(root.__getstate__())
    t = transaction.Transaction()
    t.description = 'initial database creation'
    storage.tpc_begin(t)
    storage.store(oid, None, file.getvalue(), '', t)
    storage.tpc_vote(t)
    storage.tpc_finish(t)


class StorageClass(ServerStorage):
    def set_database(self, database):
        """
        :param zerodb.permissions.base.PermissionsDatabase database: Database
        """
        assert isinstance(database, base.PermissionsDatabase)
        self.database = database
        self.noncekey = database.noncekey

    def _get_time(self):
        """Return a string representing the current time."""
        t = int(time.time())
        return struct.pack("i", t)

    def _get_nonce(self):
        s = ":".join([
            str(self.connection.addr),
            self._get_time(),
            self.noncekey])
        return sha256(s)

    def setup_delegation(self):
        # We use insert a hook to create a no-write root here
        ServerStorage.setup_delegation(self)
        create_root(self.storage)

    def _check_permissions(self, data, oid=None):
        if not data.endswith(self.user_id):
            raise StorageError("Attempt to access encrypted data of others at <%s> by <%s>" % (oid, self.user_id.encode("hex")))

    def loadEx(self, oid):
        data, tid = ServerStorage.loadEx(self, oid)
        self._check_permissions(data, oid)
        return data[:-len(self.user_id)], tid

    def storea(self, oid, serial, data, id):
        try:
            old_data, old_tid = ServerStorage.loadEx(self, oid)
            self._check_permissions(old_data, oid)
        except POSKeyError:
            pass  # We store a new one
        data += self.user_id
        return ServerStorage.storea(self, oid, serial, data, id)

    def get_root_id(self):
        """
        Gets 8-byte private root ID. Creates it if it doesn't exist.
        :return: <root_id>, <is root new>?
        :rtype: str, bool
        """
        uid = struct.unpack(self.database.uid_pack, self.user_id)[0]
        user = self.database.db_root["users"][uid]
        if user.root:
            return user.root, False
        else:
            oid = self.storage.new_oid()
            with transaction.manager:
                user.root = oid
            return oid, True

    extensions = [get_root_id]

    # TODO
    # We certainly need to implement more methods for storage in here:
    # loadEx, loadBefore, deleteObject, storea, restorea, storeBlobEnd, storeBlobShared,
    # sendBlob, loadSerial, loadBulk


class Connection(BaseConnection):
    @property
    def root(self):
        return RootConvenience(self.get(self._db._root_oid))


class DB(BaseDB):
    klass = Connection
    # TODO make serious change to __init__ here:
    # Create *private* root and save its oid
    # Use saved root oid after that

    # The __init__ method is largely replication of original ZODB.DB.DB.__init__
    # with the exception of root creation
    def __init__(self, storage,
                 pool_size=7,
                 pool_timeout=(1 << 31),
                 cache_size=400,
                 cache_size_bytes=0,
                 historical_pool_size=3,
                 historical_cache_size=1000,
                 historical_cache_size_bytes=0,
                 historical_timeout=300,
                 database_name='unnamed',
                 databases=None,
                 xrefs=True,
                 large_record_size=(1 << 24),
                 **storage_args):
        """Create an object database.

        :Parameters:
          - `storage`: the storage used by the database, e.g. FileStorage
          - `pool_size`: expected maximum number of open connections
          - `cache_size`: target size of Connection object cache
          - `cache_size_bytes`: target size measured in total estimated size
               of objects in the Connection object cache.
               "0" means unlimited.
          - `historical_pool_size`: expected maximum number of total
            historical connections
          - `historical_cache_size`: target size of Connection object cache for
            historical (`at` or `before`) connections
          - `historical_cache_size_bytes` -- similar to `cache_size_bytes` for
            the historical connection.
          - `historical_timeout`: minimum number of seconds that
            an unused historical connection will be kept, or None.
          - `xrefs` - Boolian flag indicating whether implicit cross-database
            references are allowed
        """
        if isinstance(storage, six.string_types):
            import ZODB.FileStorage
            storage = ZODB.FileStorage.FileStorage(storage, **storage_args)
        elif storage is None:
            import ZODB.MappingStorage
            storage = ZODB.MappingStorage.MappingStorage(**storage_args)
        import ZODB  # Where did it go??

        # Allocate lock.
        x = threading.RLock()
        self._a = x.acquire
        self._r = x.release

        # pools and cache sizes
        self.pool = ConnectionPool(pool_size, pool_timeout)
        self.historical_pool = KeyedConnectionPool(historical_pool_size,
                                                   historical_timeout)
        self._cache_size = cache_size
        self._cache_size_bytes = cache_size_bytes
        self._historical_cache_size = historical_cache_size
        self._historical_cache_size_bytes = historical_cache_size_bytes

        # Setup storage
        self.storage = storage
        self.references = ZODB.serialize.referencesf
        try:
            storage.registerDB(self)
        except TypeError:
            storage.registerDB(self, None)  # Backward compat

        if (not hasattr(storage, 'tpc_vote')) and not storage.isReadOnly():
            warnings.warn(
                "Storage doesn't have a tpc_vote and this violates "
                "the storage API. Violently monkeypatching in a do-nothing "
                "tpc_vote.",
                DeprecationWarning, 2)
            storage.tpc_vote = lambda *args: None

        if IMVCCStorage.providedBy(storage):
            temp_storage = storage.new_instance()
        else:
            temp_storage = storage
        try:
            oid, new = temp_storage.get_root_id()
            if new:
                create_root(temp_storage, oid=oid, check_new=False)
            self._root_oid = oid
        finally:
            if IMVCCStorage.providedBy(temp_storage):
                temp_storage.release()

        # Multi-database setup.
        if databases is None:
            databases = {}
        self.databases = databases
        self.database_name = database_name
        if database_name in databases:
            raise ValueError("database_name %r already in databases" %
                             database_name)
        databases[database_name] = self
        self.xrefs = xrefs

        self.large_record_size = large_record_size
