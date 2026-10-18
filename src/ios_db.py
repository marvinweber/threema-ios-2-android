"""
Read access to an iOS Threema Core Data store (ThreemaData.sqlite).

Core Data stores all message subclasses (TextMessage, ImageMessage, ...) in a
single table ZMESSAGE, distinguished by Z_ENT. When two subclasses define an
attribute with the same name, Core Data may give them separate columns with a
numeric suffix (ZTHUMBNAIL, ZTHUMBNAIL1, ...). The exact naming depends on the
model version, so columns are resolved at runtime instead of being hard-coded.
"""

import os
import re
import sqlite3

# Core Data reference date (2001-01-01) as unix timestamp
CORE_DATA_EPOCH = 978307200


def core_data_to_ms(value):
    if value is None or value == '':
        return None
    try:
        return int((float(value) + CORE_DATA_EPOCH) * 1000)
    except (TypeError, ValueError):
        return None


def to_hex(value):
    if value is None:
        return ''
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


class MediaRef:
    """Points to binary content: either inline bytes or a file on disk."""

    def __init__(self, data=None, path=None):
        self.data = data
        self.path = path

    @property
    def size(self):
        if self.data is not None:
            return len(self.data)
        return os.path.getsize(self.path)

    def head(self, n=32):
        if self.data is not None:
            return self.data[:n]
        with open(self.path, 'rb') as f:
            return f.read(n)

    def read(self):
        if self.data is not None:
            return self.data
        with open(self.path, 'rb') as f:
            return f.read()


class IosThreemaDb:

    def __init__(self, db_path, support_dir=None):
        self.conn = sqlite3.connect('file:' + db_path + '?mode=ro', uri=True)
        self.conn.row_factory = sqlite3.Row

        if support_dir is None:
            support_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), '.ThreemaData_SUPPORT')
        self.external_dir = os.path.join(support_dir, '_EXTERNAL_DATA')

        self.missing_external_files = 0

        self._columns = {}
        for (table,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            self._columns[table] = [r[1] for r in self.conn.execute('PRAGMA table_info("%s")' % table)]

        # entity name -> Z_ENT, and parent relations
        self.entity_ids = {}
        self._entity_super = {}
        for row in self.conn.execute('SELECT Z_ENT, Z_NAME, Z_SUPER FROM Z_PRIMARYKEY'):
            self.entity_ids[row['Z_NAME']] = row['Z_ENT']
            self._entity_super[row['Z_ENT']] = row['Z_SUPER']
        self.entity_names = {v: k for k, v in self.entity_ids.items()}

        self._col_cache = {}

    # ------------------------------------------------------------------ schema

    def has_table(self, table):
        return table in self._columns

    def has_column(self, table, column):
        return column in self._columns.get(table, [])

    def column(self, table, attribute, entity=None):
        """
        Resolve the SQL column of a Core Data attribute.

        `attribute` is the Core Data name (e.g. 'thumbnail'). If `entity` is given
        and several candidate columns exist (ZTHUMBNAIL, ZTHUMBNAIL1, ...), the one
        actually used by rows of that entity is picked.
        Returns None if no such column exists.
        """
        key = (table, attribute, entity)
        if key in self._col_cache:
            return self._col_cache[key]

        base = 'Z' + attribute.upper()
        pattern = re.compile('^' + re.escape(base) + r'\d*$')
        candidates = [c for c in self._columns.get(table, []) if pattern.match(c)]

        result = None
        if len(candidates) == 1:
            result = candidates[0]
        elif len(candidates) > 1:
            if entity is not None and entity in self.entity_ids and self.has_column(table, 'Z_ENT'):
                best, best_count = None, -1
                for c in candidates:
                    count = self.conn.execute(
                        'SELECT count(*) FROM "%s" WHERE Z_ENT=? AND "%s" IS NOT NULL' % (table, c),
                        (self.entity_ids[entity],)
                    ).fetchone()[0]
                    if count > best_count:
                        best, best_count = c, count
                result = best
            else:
                result = base if base in candidates else candidates[0]

        self._col_cache[key] = result
        return result

    def entity_of(self, z_ent):
        return self.entity_names.get(z_ent)

    # ------------------------------------------------------------------- media

    def media_ref(self, raw):
        """
        Decode a binary attribute with 'allowsExternalBinaryDataStorage'.
        0x01 + bytes        -> stored inline
        0x02 + UUID + 0x00  -> stored in .ThreemaData_SUPPORT/_EXTERNAL_DATA/<UUID>
        """
        if raw is None:
            return None
        raw = bytes(raw)
        if len(raw) == 0:
            return None
        if raw[0] == 0x02 and len(raw) < 200:
            name = raw[1:].split(b'\x00', 1)[0].decode('ascii', errors='ignore').strip()
            path = os.path.join(self.external_dir, name)
            if name and os.path.isfile(path):
                return MediaRef(path=path)
            self.missing_external_files += 1
            return None
        if raw[0] == 0x01:
            return MediaRef(data=raw[1:])
        return MediaRef(data=raw)

    def data_entity_media(self, table, pk):
        """Load the 'data' attribute of ImageData/VideoData/AudioData/FileData by primary key."""
        if pk is None or not self.has_table(table):
            return None
        col = self.column(table, 'data')
        if col is None:
            return None
        row = self.conn.execute('SELECT "%s" FROM "%s" WHERE Z_PK=?' % (col, table), (pk,)).fetchone()
        return self.media_ref(row[0]) if row else None

    def image_dimensions(self, pk):
        if pk is None or not self.has_table('ZIMAGEDATA'):
            return None, None
        w, h = self.column('ZIMAGEDATA', 'width'), self.column('ZIMAGEDATA', 'height')
        if not w or not h:
            return None, None
        row = self.conn.execute('SELECT "%s", "%s" FROM ZIMAGEDATA WHERE Z_PK=?' % (w, h), (pk,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    # ---------------------------------------------------------- encrypted check

    def encrypted_field_usage(self):
        """
        Threema Work with 'Remote Secret' stores content only in *ENCRYPTED columns,
        which cannot be decrypted here. Returns (plain_count, encrypted_only_count)
        for text messages.
        """
        text = self.column('ZMESSAGE', 'text', 'TextMessage')
        enc = self.column('ZMESSAGE', 'textEncrypted', 'TextMessage')
        if not text or not enc or 'TextMessage' not in self.entity_ids:
            return None
        ent = self.entity_ids['TextMessage']
        plain = self.conn.execute(
            'SELECT count(*) FROM ZMESSAGE WHERE Z_ENT=? AND "%s" IS NOT NULL' % text, (ent,)).fetchone()[0]
        only_enc = self.conn.execute(
            'SELECT count(*) FROM ZMESSAGE WHERE Z_ENT=? AND "%s" IS NULL AND "%s" IS NOT NULL' % (text, enc),
            (ent,)).fetchone()[0]
        return plain, only_enc
