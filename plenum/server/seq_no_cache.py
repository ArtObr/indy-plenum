from storage.kv_store_rocksdb_int_keys import KeyValueStorageRocksdbIntKeys



class SeqNoCache():
    def __init__(self, kv_store: KeyValueStorageRocksdbIntKeys):
        self.storage = kv_store